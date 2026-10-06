"""Unit tests for SOPS and age cryptographic management."""

import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

# Allow imports from repo root and stub homeassistant dependencies if needed
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

if "custom_components" not in sys.modules:
    pkg1 = types.ModuleType("custom_components")
    pkg2 = types.ModuleType("custom_components.git_ha_ppens")
    pkg2.__path__ = [os.path.join(REPO_ROOT, "custom_components", "git_ha_ppens")]
    sys.modules["custom_components"] = pkg1
    sys.modules["custom_components.git_ha_ppens"] = pkg2

    import importlib.util
    spec_const = importlib.util.spec_from_file_location(
        "custom_components.git_ha_ppens.const",
        os.path.join(REPO_ROOT, "custom_components", "git_ha_ppens", "const.py"),
    )
    mod_const = importlib.util.module_from_spec(spec_const)
    sys.modules["custom_components.git_ha_ppens.const"] = mod_const
    spec_const.loader.exec_module(mod_const)

from custom_components.git_ha_ppens.sops_manager import (
    SopsError,
    SopsManager,
    _bech32_decode,
    _glob_to_regex,
    _bech32_encode,
    _convertbits,
    age_secret_to_recipient,
    generate_age_keypair,
    validate_age_recipient,
    validate_age_secret_key,
)
import yaml


class TestAgeCrypto(unittest.TestCase):
    """Test pure-Python age X25519 & Bech32 implementation."""

    def test_bech32_basic(self):
        """Test bech32 encode and decode."""
        data = [1, 2, 3, 4, 5]
        encoded = _bech32_encode("test", data)
        self.assertTrue(encoded.startswith("test1"))
        hrp, decoded = _bech32_decode(encoded)
        self.assertEqual(hrp, "test")
        self.assertEqual(decoded, data)

    def test_convertbits(self):
        """Test convertbits 8-to-5 and 5-to-8 roundtrip."""
        raw = b"Hello, World! 1234567890abcdef"
        data_5bit = _convertbits(list(raw), 8, 5, True)
        self.assertIsNotNone(data_5bit)
        roundtrip = _convertbits(data_5bit, 5, 8, False)
        self.assertEqual(bytes(roundtrip), raw)

    def test_keypair_generation_and_validation(self):
        """Test generating keypairs and validating their formats."""
        sec, pub = generate_age_keypair()
        self.assertTrue(sec.startswith("AGE-SECRET-KEY-1"))
        self.assertTrue(pub.startswith("age1"))
        self.assertTrue(validate_age_secret_key(sec))
        self.assertTrue(validate_age_recipient(pub))

        # Check derivation matches
        derived = age_secret_to_recipient(sec)
        self.assertEqual(derived, pub)

    def test_multiple_keypair_derivations(self):
        """Test that multiple random keypairs correctly derive recipient."""
        for _ in range(5):
            sec, pub = generate_age_keypair()
            self.assertEqual(age_secret_to_recipient(sec), pub)

    def test_invalid_keys(self):
        """Test validation failures on corrupt or wrong-format keys."""
        self.assertFalse(validate_age_secret_key("AGE-SECRET-KEY-1INVALID"))
        self.assertFalse(validate_age_secret_key("AGE-SECRET-KEY-2FOOBAR"))
        self.assertFalse(validate_age_secret_key("not-a-key"))
        self.assertFalse(validate_age_secret_key(""))

        self.assertFalse(validate_age_recipient("age1invalid"))
        self.assertFalse(validate_age_recipient("age2invalid"))
        self.assertFalse(validate_age_recipient("not-a-recipient"))
        self.assertFalse(validate_age_recipient(""))

    def test_mixed_case_bech32_rejected(self):
        """BIP 173: mixed-case Bech32 strings are invalid, not case-folded.

        A valid recipient/key must be entirely lowercase or entirely
        uppercase; a mixed-case string (however it decodes after folding)
        must be rejected rather than silently accepted.
        """
        _, recipient = generate_age_keypair()
        assert recipient == recipient.lower()
        # Flip the case of just one letter in the data part (after the "1"
        # separator), keeping the checksum bytes unchanged so this fails
        # only because of case-mixing, not because of a bad checksum.
        sep = recipient.index("1")
        for i in range(sep + 1, len(recipient)):
            if recipient[i].isalpha():
                mixed = recipient[:i] + recipient[i].upper() + recipient[i + 1 :]
                break
        else:
            self.fail("recipient has no alphabetic character to flip")

        self.assertNotEqual(mixed, recipient)
        self.assertFalse(validate_age_recipient(mixed))


class TestSopsManager(unittest.IsolatedAsyncioTestCase):
    """Test SopsManager file operations and workflow."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.secret_key, self.recipient = generate_age_keypair()
        self.manager = SopsManager(
            repo_path=self.test_dir,
            age_key=self.secret_key,
            age_recipient=self.recipient,
            custom_binary_path="/usr/local/bin/sops",
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_redact_output(self):
        """Test secret key redaction."""
        text = f"Command failed with key: {self.secret_key} in config."
        redacted = self.manager._redact_output(text)
        self.assertNotIn(self.secret_key, redacted)
        self.assertIn("[REDACTED_AGE_KEY]", redacted)

    def test_properties(self):
        """Test basic property getters."""
        self.assertEqual(self.manager.age_recipient, self.recipient)
        self.assertEqual(self.manager.age_key, self.secret_key)
        self.assertEqual(str(self.manager.repo_path), self.test_dir)

    async def test_ensure_sops_config(self):
        """Test creation and idempotent update of .sops.yaml."""
        sops_config_path = os.path.join(self.test_dir, ".sops.yaml")
        self.assertFalse(os.path.exists(sops_config_path))

        await self.manager.ensure_sops_config()
        self.assertTrue(os.path.exists(sops_config_path))

        with open(sops_config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        self.assertIn("creation_rules", data)
        self.assertEqual(len(data["creation_rules"]), 1)
        self.assertEqual(data["creation_rules"][0]["age"], self.recipient)

        # Running again should keep it intact
        await self.manager.ensure_sops_config()
        with open(sops_config_path, "r", encoding="utf-8") as f:
            data2 = yaml.safe_load(f)
        self.assertEqual(len(data2["creation_rules"]), 1)

    @patch("asyncio.create_subprocess_exec")
    async def test_encrypt_file_success(self, mock_exec):
        """Test file encryption with mocked SOPS binary."""
        secrets_file = os.path.join(self.test_dir, "secrets.yaml")
        enc_file = os.path.join(self.test_dir, "secrets.enc.yaml")
        with open(secrets_file, "w", encoding="utf-8") as f:
            f.write("wifi_password: SuperSecretPassword123\napi_key: abc123xyz\n")

        encrypted_yaml = (
            "wifi_password: ENC[AES256_GCM,data:xyz...]\n"
            "sops:\n  version: 3.9.0\n"
        )

        async def fake_exec(*args, **kwargs):
            if "--output" in args:
                out_idx = args.index("--output")
                out_path = args[out_idx + 1]
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(encrypted_yaml)
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            return proc

        mock_exec.side_effect = fake_exec

        dummy_bin = os.path.join(self.test_dir, "fake_sops")
        with patch.object(self.manager, "get_binary_path", return_value=dummy_bin):
            result = await self.manager.encrypt_file("secrets.yaml")

        self.assertTrue(result)
        self.assertTrue(os.path.exists(enc_file))
        with open(enc_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, encrypted_yaml)

    @patch("asyncio.create_subprocess_exec")
    async def test_decrypt_file_success(self, mock_exec):
        """Test file decryption with mocked SOPS binary."""
        enc_file = os.path.join(self.test_dir, "secrets.enc.yaml")
        target_file = os.path.join(self.test_dir, "secrets.yaml")

        with open(enc_file, "w", encoding="utf-8") as f:
            f.write("wifi_password: ENC[AES256_GCM,data:...]\n")

        decrypted_yaml = "wifi_password: SuperSecretPassword123\n"
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(decrypted_yaml.encode("utf-8"), b""))
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        dummy_bin = os.path.join(self.test_dir, "fake_sops")
        with patch.object(self.manager, "get_binary_path", return_value=dummy_bin):
            result = await self.manager.decrypt_file("secrets.enc.yaml")

        self.assertTrue(result)
        self.assertTrue(os.path.exists(target_file))
        with open(target_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, decrypted_yaml)

    @patch("asyncio.create_subprocess_exec")
    async def test_decrypt_failure_preserves_target(self, mock_exec):
        """Test that decryption failure rolls back atomic write and preserves original file."""
        target_file = os.path.join(self.test_dir, "secrets.yaml")
        enc_file = os.path.join(self.test_dir, "secrets.enc.yaml")

        original_content = "original: content\n"
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(original_content)
        with open(enc_file, "w", encoding="utf-8") as f:
            f.write("corrupted: enc_data\n")

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"Error: failed to decrypt with age key"))
        mock_proc.returncode = 1
        mock_exec.return_value = mock_proc

        dummy_bin = os.path.join(self.test_dir, "fake_sops")
        with patch.object(self.manager, "get_binary_path", return_value=dummy_bin):
            with self.assertRaises(SopsError):
                await self.manager.decrypt_file("secrets.enc.yaml")

        # Original file should NOT be overwritten or corrupted
        with open(target_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), original_content)

    async def test_sync_secrets_to_encrypted(self):
        """Test syncing plain secrets to encrypted."""
        with patch.object(self.manager, "encrypt_file", return_value=True) as mock_encrypt:
            # Create dummy secrets.yaml
            secrets_path = os.path.join(self.test_dir, "secrets.yaml")
            with open(secrets_path, "w", encoding="utf-8") as f:
                f.write("test: 123\n")

            processed = await self.manager.sync_secrets_to_encrypted()
            self.assertIn("secrets.enc.yaml", processed)
            mock_encrypt.assert_called_once_with("secrets.yaml", "secrets.enc.yaml", force=False)

    async def test_sync_encrypted_to_secrets(self):
        """Test syncing encrypted files back to plain secrets."""
        with patch.object(self.manager, "decrypt_file", return_value=True) as mock_decrypt:
            # Create dummy secrets.enc.yaml
            enc_path = os.path.join(self.test_dir, "secrets.enc.yaml")
            with open(enc_path, "w", encoding="utf-8") as f:
                f.write("sops: enc\n")

            restored = await self.manager.sync_encrypted_to_secrets()
            self.assertIn("secrets.yaml", restored)
            mock_decrypt.assert_called_once_with("secrets.enc.yaml", "secrets.yaml")

    def test_resolve_plain_and_encrypted_secret_files(self):
        """Test resolution of exact paths and wildcard globs."""
        # Create nested directory structure
        esphome_dir = os.path.join(self.test_dir, "esphome")
        custom_dir = os.path.join(self.test_dir, "custom_components", "test")
        os.makedirs(esphome_dir, exist_ok=True)
        os.makedirs(custom_dir, exist_ok=True)

        # Create files
        with open(os.path.join(self.test_dir, "secrets.yaml"), "w") as f:
            f.write("root: 1\n")
        with open(os.path.join(esphome_dir, "secrets.yaml"), "w") as f:
            f.write("esphome: 2\n")
        with open(os.path.join(custom_dir, "secrets.yaml"), "w") as f:
            f.write("custom: 3\n")

        # Test exact resolution
        self.manager.update_credentials(secrets_files=["secrets.yaml", "esphome/secrets.yaml"])
        resolved = self.manager.resolve_plain_secret_files()
        self.assertEqual(resolved, ["esphome/secrets.yaml", "secrets.yaml"])

        # Test glob pattern resolution
        self.manager.update_credentials(secrets_files=["**/secrets.yaml"])
        resolved_glob = self.manager.resolve_plain_secret_files()
        self.assertIn("secrets.yaml", resolved_glob)
        self.assertIn("esphome/secrets.yaml", resolved_glob)
        self.assertIn("custom_components/test/secrets.yaml", resolved_glob)

    def test_resolve_plain_secret_files_rejects_parent_traversal(self):
        """A '../'-prefixed pattern must not escape the repo directory."""
        outside_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside_dir, ignore_errors=True)
        outside_secret = os.path.join(outside_dir, "outside_secret.yaml")
        with open(outside_secret, "w", encoding="utf-8") as f:
            f.write("token: super-secret\n")

        rel_traversal = os.path.relpath(outside_secret, self.test_dir)
        self.manager.update_credentials(secrets_files=[rel_traversal])
        resolved = self.manager.resolve_plain_secret_files()
        self.assertEqual(resolved, [])

    def test_resolve_encrypted_secret_files_rejects_parent_traversal(self):
        """A '../'-prefixed pattern must not escape the repo directory."""
        outside_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside_dir, ignore_errors=True)
        outside_enc = os.path.join(outside_dir, "outside_secret.enc.yaml")
        with open(outside_enc, "w", encoding="utf-8") as f:
            f.write("sops: enc\n")

        rel_traversal = os.path.relpath(outside_enc, self.test_dir).replace(
            ".enc.yaml", ".yaml"
        )
        self.manager.update_credentials(secrets_files=[rel_traversal])
        pairs = dict(self.manager.resolve_encrypted_secret_files())
        self.assertEqual(pairs, {})

    def test_resolve_service_account_case_insensitive(self):
        """Test that uppercase SERVICE_ACCOUNT.json is resolved by lowercase pattern."""
        sa_file = os.path.join(self.test_dir, "SERVICE_ACCOUNT.json")
        with open(sa_file, "w", encoding="utf-8") as f:
            f.write('{"type": "service_account"}\n')

        # Test with default pattern '*service_account*.json'
        self.manager.update_credentials(secrets_files=["*service_account*.json"])
        resolved = self.manager.resolve_plain_secret_files()
        self.assertIn("SERVICE_ACCOUNT.json", resolved)

        # Test with uppercase pattern '*SERVICE_ACCOUNT*.json'
        self.manager.update_credentials(secrets_files=["*SERVICE_ACCOUNT*.json"])
        resolved_upper = self.manager.resolve_plain_secret_files()
        self.assertIn("SERVICE_ACCOUNT.json", resolved_upper)

        # Test nested subfolder with recursive glob
        sub_dir = os.path.join(self.test_dir, "google")
        os.makedirs(sub_dir, exist_ok=True)
        with open(os.path.join(sub_dir, "SERVICE_ACCOUNT.json"), "w", encoding="utf-8") as f:
            f.write('{"type": "service_account"}\n')

        self.manager.update_credentials(secrets_files=["**/*service_account*.json"])
        resolved_nested = self.manager.resolve_plain_secret_files()
        self.assertIn("SERVICE_ACCOUNT.json", resolved_nested)
        self.assertIn("google/SERVICE_ACCOUNT.json", resolved_nested)

    def test_json_and_yaml_path_conversions(self):
        """Test path conversions between plain and encrypted files for json and yaml."""
        cases = [
            ("secrets.yaml", "secrets.enc.yaml"),
            ("esphome/secrets.yaml", "esphome/secrets.enc.yaml"),
            ("secrets.yml", "secrets.enc.yml"),
            ("SERVICE_ACCOUNT.json", "SERVICE_ACCOUNT.enc.json"),
            ("google/SERVICE_ACCOUNT.json", "google/SERVICE_ACCOUNT.enc.json"),
            ("gcp_creds.json", "gcp_creds.enc.json"),
        ]
        for plain, expected_enc in cases:
            enc = self.manager.get_encrypted_relative_path(plain)
            self.assertEqual(enc, expected_enc)
            dec = self.manager.get_plain_relative_path(enc)
            self.assertEqual(dec, plain)

        # Test backward-compatible legacy format (.json.enc.yaml)
        self.assertEqual(
            self.manager.get_plain_relative_path("SERVICE_ACCOUNT.json.enc.yaml"),
            "SERVICE_ACCOUNT.json",
        )

    def test_resolve_encrypted_json_files(self):
        """Test discovery of encrypted JSON files."""
        enc_sa = os.path.join(self.test_dir, "SERVICE_ACCOUNT.enc.json")
        with open(enc_sa, "w", encoding="utf-8") as f:
            f.write('{"sops": {}}\n')

        self.manager.update_credentials(secrets_files=["*service_account*.json"])
        pairs = dict(self.manager.resolve_encrypted_secret_files())
        self.assertIn("SERVICE_ACCOUNT.enc.json", pairs)
        self.assertEqual(pairs["SERVICE_ACCOUNT.enc.json"], "SERVICE_ACCOUNT.json")

    @patch("asyncio.create_subprocess_exec")
    async def test_decrypt_json_success(self, mock_exec):
        """Test JSON secret file decryption and valid JSON validation."""
        enc_file = os.path.join(self.test_dir, "SERVICE_ACCOUNT.enc.json")
        target_file = os.path.join(self.test_dir, "SERVICE_ACCOUNT.json")

        with open(enc_file, "w", encoding="utf-8") as f:
            f.write('{"client_email": "ENC[...]"}\n')

        decrypted_json = '{"client_email": "svc@example.iam.gserviceaccount.com", "private_key_id": "12345"}\n'
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(decrypted_json.encode("utf-8"), b""))
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc

        dummy_bin = os.path.join(self.test_dir, "fake_sops")
        with patch.object(self.manager, "get_binary_path", return_value=dummy_bin):
            result = await self.manager.decrypt_file("SERVICE_ACCOUNT.enc.json")

        self.assertTrue(result)
        self.assertTrue(os.path.exists(target_file))
        with open(target_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, decrypted_json)

    @patch("asyncio.create_subprocess_exec")
    async def test_encrypt_file_skips_when_unchanged(self, mock_exec):
        """Test that encrypt_file skips SOPS execution when file content is unchanged."""
        secrets_file = os.path.join(self.test_dir, "secrets.yaml")
        with open(secrets_file, "w", encoding="utf-8") as f:
            f.write("password: Secret123\n")

        async def fake_exec(*args, **kwargs):
            if "--output" in args:
                out_idx = args.index("--output")
                out_path = args[out_idx + 1]
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write("password: ENC[...]\n")
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            return proc

        mock_exec.side_effect = fake_exec
        dummy_bin = os.path.join(self.test_dir, "fake_sops")

        with patch.object(self.manager, "get_binary_path", return_value=dummy_bin):
            # 1. First encryption: should encrypt
            res1 = await self.manager.encrypt_file("secrets.yaml")
            self.assertTrue(res1)
            self.assertEqual(mock_exec.call_count, 1)

            # 2. Second encryption with unchanged secrets.yaml: must skip!
            res2 = await self.manager.encrypt_file("secrets.yaml")
            self.assertFalse(res2)
            self.assertEqual(mock_exec.call_count, 1)  # No extra sops call

            # 3. Modify secrets.yaml: should encrypt again
            with open(secrets_file, "w", encoding="utf-8") as f:
                f.write("password: ChangedSecret456\n")

            res3 = await self.manager.encrypt_file("secrets.yaml")
            self.assertTrue(res3)
            self.assertEqual(mock_exec.call_count, 2)

    def test_resolve_plain_ignores_examples(self):
        """Test that dummy/template/example files are not picked up as secrets."""
        with open(os.path.join(self.test_dir, "SERVICE_ACCOUNT.example.json"), "w") as f:
            f.write('{"example": true}\n')
        with open(os.path.join(self.test_dir, "secrets.sample.yaml"), "w") as f:
            f.write("sample: 1\n")

        self.manager.update_credentials(secrets_files=["*service_account*.json", "*.yaml"])
        resolved = self.manager.resolve_plain_secret_files()
        self.assertNotIn("SERVICE_ACCOUNT.example.json", resolved)
        self.assertNotIn("secrets.sample.yaml", resolved)

    async def test_ensure_sops_gitignore(self):
        """Test that ensure_sops_gitignore appends !*.enc.* rules."""
        gitignore_path = os.path.join(self.test_dir, ".gitignore")
        with open(gitignore_path, "w", encoding="utf-8") as f:
            f.write("*SERVICE_ACCOUNT*.json\nsecrets.yaml\n")

        updated = await self.manager.ensure_sops_gitignore()
        self.assertTrue(updated)

        with open(gitignore_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("!*.enc.json", content)
        self.assertIn("!*.enc.yaml", content)
        self.assertIn("!*.enc.yml", content)

        # Idempotent call should not modify again
        updated_again = await self.manager.ensure_sops_gitignore()
        self.assertFalse(updated_again)

    async def test_ensure_sops_config_appends_to_existing_creation_rules(self):
        """Test that ensure_sops_config preserves existing recipients and appends new ones."""
        sops_config_path = os.path.join(self.test_dir, ".sops.yaml")
        initial_content = (
            "creation_rules:\n"
            "  - path_regex: .*\n"
            "    age: age1existingrecipient123\n"
        )
        with open(sops_config_path, "w", encoding="utf-8") as f:
            f.write(initial_content)

        updated = await self.manager.ensure_sops_config("age1newrecipient456")
        self.assertTrue(updated)

        with open(sops_config_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("age1existingrecipient123", content)
        self.assertIn("age1newrecipient456", content)

    async def test_ensure_sops_config_does_not_overwrite_unrecognized_existing_file(self):
        """Regression test: an existing .sops.yaml with a shape we can't safely
        merge into must be left untouched, not silently replaced with a
        fresh single-recipient file (which would discard the user's own
        creation_rules, comments, and path_regex scoping)."""
        sops_config_path = os.path.join(self.test_dir, ".sops.yaml")
        initial_content = (
            "# hand-written team config, please keep\n"
            "creation_rules: []\n"
        )
        with open(sops_config_path, "w", encoding="utf-8") as f:
            f.write(initial_content)

        updated = await self.manager.ensure_sops_config("age1newrecipient456")
        self.assertFalse(updated)

        with open(sops_config_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, initial_content)
        self.assertNotIn("age1newrecipient456", content)

    async def test_ensure_sops_config_does_not_overwrite_invalid_yaml(self):
        """An existing .sops.yaml that isn't even valid YAML must also be left alone."""
        sops_config_path = os.path.join(self.test_dir, ".sops.yaml")
        initial_content = "creation_rules: [unterminated\n"
        with open(sops_config_path, "w", encoding="utf-8") as f:
            f.write(initial_content)

        updated = await self.manager.ensure_sops_config("age1newrecipient456")
        self.assertFalse(updated)

        with open(sops_config_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, initial_content)

    async def test_encrypt_file_uses_sops_yaml_without_age_flag(self):
        """Test that encrypt_file omits --age when .sops.yaml exists."""
        secrets_file = os.path.join(self.test_dir, "secrets.yaml")
        with open(secrets_file, "w", encoding="utf-8") as f:
            f.write("test: value\n")

        sops_yaml = os.path.join(self.test_dir, ".sops.yaml")
        with open(sops_yaml, "w", encoding="utf-8") as f:
            f.write("creation_rules:\n  - path_regex: .*\n    age: age1recipient\n")

        with patch.object(self.manager, "get_binary_path", return_value=Path("/bin/sops")):
            with patch.object(self.manager, "_run_sops_cmd") as mock_exec:
                def fake_encrypt(bin_path, *args):
                    out_idx = args.index("--output")
                    out_file = args[out_idx + 1]
                    Path(out_file).write_text("sops_encrypted_data")
                    return ""
                mock_exec.side_effect = fake_encrypt

                res = await self.manager.encrypt_file("secrets.yaml")
                self.assertTrue(res)
                # Verify --age was NOT in arguments
                cmd_args = mock_exec.call_args[0][1:]
                self.assertNotIn("--age", cmd_args)

    async def test_encrypt_file_force_bypasses_cache(self):
        """Test that force=True forces re-encryption even if content hash matches."""
        secrets_file = os.path.join(self.test_dir, "secrets.yaml")
        with open(secrets_file, "w", encoding="utf-8") as f:
            f.write("secret: data\n")

        enc_file = os.path.join(self.test_dir, "secrets.enc.yaml")
        with open(enc_file, "w", encoding="utf-8") as f:
            f.write("encrypted")

        # Prime the hash cache
        content_hash = hashlib.sha256(b"secret: data\n").hexdigest()
        self.manager._plain_hashes["secrets.yaml"] = content_hash

        with patch.object(self.manager, "get_binary_path", return_value=Path("/bin/sops")):
            with patch.object(self.manager, "_run_sops_cmd") as mock_exec:
                def fake_encrypt(bin_path, *args):
                    out_idx = args.index("--output")
                    out_file = args[out_idx + 1]
                    Path(out_file).write_text("re_encrypted")
                    return ""
                mock_exec.side_effect = fake_encrypt

                # Without force: skipped
                res_skip = await self.manager.encrypt_file("secrets.yaml", force=False)
                self.assertFalse(res_skip)
                self.assertEqual(mock_exec.call_count, 0)

                # With force: executed
                res_force = await self.manager.encrypt_file("secrets.yaml", force=True)
                self.assertTrue(res_force)
                self.assertEqual(mock_exec.call_count, 1)

    def test_get_sops_platform_asset_arm32_returns_none(self):
        """Test that 32-bit ARM returns None for platform asset download."""
        from custom_components.git_ha_ppens.sops_manager import get_sops_platform_asset
        with patch("platform.machine", return_value="armv7l"):
            with patch("sys.platform", "linux"):
                asset = get_sops_platform_asset("v3.9.4")
                self.assertIsNone(asset)

    def test_plain_secrets_backup_and_restore(self):
        """Test snapshotting plain secrets and atomically restoring on rollback."""
        secrets_path = os.path.join(self.test_dir, "secrets.yaml")
        with open(secrets_path, "w", encoding="utf-8") as f:
            f.write("original_secret_data: 42\n")

        # Create dummy enc file so resolve_encrypted_secret_files discovers secrets.yaml
        enc_path = os.path.join(self.test_dir, "secrets.enc.yaml")
        with open(enc_path, "w", encoding="utf-8") as f:
            f.write("enc_data")

        backup = self.manager.backup_plain_secrets()
        self.assertIn("secrets.yaml", backup)
        self.assertEqual(backup["secrets.yaml"], b"original_secret_data: 42\n")

        # Corrupt secrets.yaml (simulating bad pull/restore)
        with open(secrets_path, "w", encoding="utf-8") as f:
            f.write("corrupted_data: bad\n")

        # Restore from backup
        self.manager.restore_plain_secrets_backup(backup)

        with open(secrets_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "original_secret_data: 42\n")

        # Check permissions: 0600
        mode = os.stat(secrets_path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_get_encrypted_relative_path_is_idempotent(self):
        """Test that an already-encrypted name is returned unchanged, not double-encrypted."""
        cases = [
            "secrets.enc.yaml",
            "esphome/secrets.enc.yaml",
            "SERVICE_ACCOUNT.enc.json",
            "secrets.enc.yml",
        ]
        for enc_name in cases:
            self.assertEqual(
                self.manager.get_encrypted_relative_path(enc_name), enc_name
            )

    def test_resolve_encrypted_secret_files_ignores_unconfigured_sidecars(self):
        """Test that .enc.* files outside configured patterns are not discovered.

        Regression test: resolve_encrypted_secret_files() previously did a
        blanket **/*.enc.* discovery across the whole repository, which meant
        an unrelated encrypted file (e.g. vendored under www/) would be
        decrypted on every pull/restore and could block them entirely.
        """
        # Configured pattern only covers secrets.yaml
        self.manager.update_credentials(secrets_files=["secrets.yaml"])

        matching_enc = os.path.join(self.test_dir, "secrets.enc.yaml")
        with open(matching_enc, "w", encoding="utf-8") as f:
            f.write("sops: {}\n")

        unrelated_dir = os.path.join(self.test_dir, "www", "vendor")
        os.makedirs(unrelated_dir, exist_ok=True)
        unrelated_enc = os.path.join(unrelated_dir, "bundle.enc.json")
        with open(unrelated_enc, "w", encoding="utf-8") as f:
            f.write("{}\n")

        pairs = dict(self.manager.resolve_encrypted_secret_files())
        self.assertIn("secrets.enc.yaml", pairs)
        self.assertNotIn("www/vendor/bundle.enc.json", pairs)

    def test_resolve_encrypted_secret_files_honors_files_filter(self):
        """Test that an explicit `files` argument scopes decryption, matching the service docs."""
        secrets_enc = os.path.join(self.test_dir, "secrets.enc.yaml")
        other_enc = os.path.join(self.test_dir, "other.enc.yaml")
        with open(secrets_enc, "w", encoding="utf-8") as f:
            f.write("sops: {}\n")
        with open(other_enc, "w", encoding="utf-8") as f:
            f.write("sops: {}\n")

        self.manager.update_credentials(secrets_files=["secrets.yaml", "other.yaml"])

        # Passing the encrypted name directly (as documented in services.yaml)
        # must resolve to itself, not "secrets.enc.enc.yaml".
        pairs = dict(self.manager.resolve_encrypted_secret_files(files=["secrets.enc.yaml"]))
        self.assertEqual(pairs, {"secrets.enc.yaml": "secrets.yaml"})

    async def test_async_ensure_sops_binary_fails_closed_without_pinned_hash(self):
        """Test that a download with no pinned checksum is refused, not installed unverified."""
        from custom_components.git_ha_ppens.sops_manager import (
            SopsBinaryNotFoundError,
            async_ensure_sops_binary,
        )

        storage_dir = Path(self.test_dir) / ".storage" / "git_ha_ppens"

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, _n):
                return b""

        with patch("platform.machine", return_value="amd64"), patch(
            "sys.platform", "linux"
        ), patch(
            "custom_components.git_ha_ppens.sops_manager.SOPS_BINARY_SHA256", {}
        ), patch(
            "urllib.request.urlopen", return_value=_FakeResponse()
        ), patch(
            "shutil.which", return_value=None
        ):
            with self.assertRaises(SopsBinaryNotFoundError):
                await async_ensure_sops_binary(storage_dir=storage_dir, version="v9.9.9")

    async def test_async_ensure_sops_binary_rejects_checksum_mismatch(self):
        """Test that a corrupted/tampered download (wrong hash) is rejected, not installed."""
        from custom_components.git_ha_ppens.sops_manager import (
            SopsBinaryNotFoundError,
            async_ensure_sops_binary,
        )

        storage_dir = Path(self.test_dir) / ".storage" / "git_ha_ppens"

        class _FakeResponse:
            def __init__(self):
                self._served = False

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, _n):
                # Return one chunk, then empty bytes to signal EOF -- matches
                # the real urlopen response object's read() contract that the
                # download loop's `while chunk := response.read(...)` relies on.
                if self._served:
                    return b""
                self._served = True
                return b"not the real sops binary"

        with patch("platform.machine", return_value="amd64"), patch(
            "sys.platform", "linux"
        ), patch(
            "custom_components.git_ha_ppens.sops_manager.SOPS_BINARY_SHA256",
            {"sops-v3.9.4.linux.amd64": "0" * 64},
        ), patch(
            "urllib.request.urlopen", return_value=_FakeResponse()
        ), patch(
            "shutil.which", return_value=None
        ):
            with self.assertRaises(SopsBinaryNotFoundError) as ctx:
                await async_ensure_sops_binary(storage_dir=storage_dir, version="v3.9.4")
            self.assertIn("Integrity check failed", str(ctx.exception))

        # The rejected download must not be left behind as an installed binary.
        self.assertFalse((storage_dir / "bin" / "sops").exists())

    async def test_get_binary_path_custom_path_not_executable(self):
        """Test that a configured but non-executable custom path is refused clearly."""
        from custom_components.git_ha_ppens.sops_manager import SopsBinaryNotFoundError

        not_executable = os.path.join(self.test_dir, "not-a-binary")
        with open(not_executable, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho hi\n")
        # Deliberately do not chmod +x.
        os.chmod(not_executable, 0o644)

        self.manager.update_credentials(custom_binary_path=not_executable)
        with self.assertRaises(SopsBinaryNotFoundError):
            await self.manager.get_binary_path()

    async def test_get_binary_path_reuses_cached_storage_binary_without_downloading(self):
        """Test that a binary already installed under the storage dir is reused, not re-downloaded."""
        storage_dir = Path(self.test_dir) / ".storage" / "git_ha_ppens"
        bin_dir = storage_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        cached_bin = bin_dir / "sops"
        cached_bin.write_text("#!/bin/sh\necho cached\n", encoding="utf-8")
        cached_bin.chmod(0o755)

        manager = SopsManager(
            repo_path=self.test_dir,
            age_key=self.secret_key,
            age_recipient=self.recipient,
            storage_dir=storage_dir,
        )

        with patch("shutil.which", return_value=None), patch(
            "urllib.request.urlopen"
        ) as mock_urlopen:
            result = await manager.get_binary_path()
            self.assertEqual(result, cached_bin)
            mock_urlopen.assert_not_called()

    async def test_is_available_caches_result_briefly(self):
        """Repeated is_available() calls within the cache TTL must not

        re-spawn the `sops --version` subprocess on every call (e.g. every
        coordinator poll, default every 30s).
        """
        manager = SopsManager(
            repo_path=self.test_dir,
            age_key=self.secret_key,
            age_recipient=self.recipient,
        )
        with patch.object(
            manager, "get_binary_path", return_value=Path("/fake/sops")
        ), patch.object(
            manager, "_run_sops_cmd", AsyncMock(return_value="sops 3.9.0")
        ) as mock_run:
            first = await manager.is_available()
            second = await manager.is_available()

        self.assertTrue(first)
        self.assertTrue(second)
        mock_run.assert_called_once()

    async def test_get_binary_path_cools_down_after_failed_lookup(self):
        """Test that a failed binary lookup does not re-download on every call (retry storm)."""
        from custom_components.git_ha_ppens.sops_manager import SopsBinaryNotFoundError

        manager = SopsManager(
            repo_path=self.test_dir,
            age_key=self.secret_key,
            age_recipient=self.recipient,
            custom_binary_path="/definitely/not/a/real/sops-binary",
        )

        with self.assertRaises(SopsBinaryNotFoundError):
            await manager.get_binary_path()

        # A second call within the cooldown window must reuse the cached
        # failure instead of touching the filesystem/network again.
        with patch(
            "custom_components.git_ha_ppens.sops_manager.async_ensure_sops_binary"
        ) as mock_ensure:
            with self.assertRaises(SopsBinaryNotFoundError):
                await manager.get_binary_path()
            mock_ensure.assert_not_called()


@unittest.skipUnless(
    shutil.which("sops"), "requires a real sops binary on PATH for an end-to-end round trip"
)
class TestSopsManagerRealBinaryRoundTrip(unittest.IsolatedAsyncioTestCase):
    """End-to-end tests against the real sops binary (skipped if not installed).

    Regression coverage for a bug where a non-YAML/JSON secret (e.g. a raw
    token or a .pem key) was encrypted without an explicit --input-type,
    causing sops to wrap it in a YAML "binary store" envelope on encrypt but
    decrypt() to print that envelope as plain text instead of unwrapping it
    -- corrupting the file on every round trip.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.secret_key, self.recipient = generate_age_keypair()
        self.manager = SopsManager(
            repo_path=self.test_dir,
            age_key=self.secret_key,
            age_recipient=self.recipient,
            custom_binary_path=shutil.which("sops"),
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def _assert_round_trips(self, relative_name: str, original: bytes) -> None:
        plain_path = os.path.join(self.test_dir, relative_name)
        with open(plain_path, "wb") as f:
            f.write(original)

        self.assertTrue(await self.manager.encrypt_file(relative_name))
        enc_rel = SopsManager.get_encrypted_relative_path(relative_name)
        self.assertTrue(os.path.isfile(os.path.join(self.test_dir, enc_rel)))

        os.remove(plain_path)
        self.assertTrue(await self.manager.decrypt_file(enc_rel, relative_name))

        with open(plain_path, "rb") as f:
            roundtrip = f.read()
        self.assertEqual(roundtrip, original)

    async def test_non_utf8_binary_secret_round_trips_exactly(self):
        """A secret with non-UTF-8 bytes must come back byte-for-byte identical."""
        original = (
            b"line one\nnot: yaml: [unterminated\n"
            b"\xff\xfe binary junk \x00\x01\x02"
        )
        await self._assert_round_trips("token.txt", original)

    async def test_pem_style_secret_round_trips_exactly(self):
        """A PEM-style key file (unrecognised extension) round-trips unchanged."""
        original = (
            b"-----BEGIN PRIVATE KEY-----\n"
            b"MIIExampleNotARealKeyButLooksLikeOne==\n"
            b"-----END PRIVATE KEY-----\n"
        )
        await self._assert_round_trips("service.pem", original)

    async def test_yaml_secret_still_round_trips(self):
        """Existing YAML content type continues to round-trip after the fix.

        SOPS re-serializes structured (YAML/JSON) content -- it encrypts
        individual values -- so byte-exact equality isn't the right bar here
        (unlike the binary-store cases above); semantic equality is.
        """
        original = b"api_key: super-secret\nnested:\n  value: 1\n"
        plain_path = os.path.join(self.test_dir, "secrets.yaml")
        with open(plain_path, "wb") as f:
            f.write(original)

        self.assertTrue(await self.manager.encrypt_file("secrets.yaml"))
        enc_rel = SopsManager.get_encrypted_relative_path("secrets.yaml")

        os.remove(plain_path)
        self.assertTrue(await self.manager.decrypt_file(enc_rel, "secrets.yaml"))

        with open(plain_path, "rb") as f:
            roundtrip = f.read()
        self.assertEqual(yaml.safe_load(roundtrip), yaml.safe_load(original))


class TestEncryptedSidecarPathMapping(unittest.TestCase):
    """Encrypted sidecars must map back to the plain path their pattern names."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.test_dir, ignore_errors=True)
        self.manager = SopsManager(
            repo_path=self.test_dir,
            storage_dir=Path(self.test_dir) / ".storage" / "git_ha_ppens",
        )

    def _touch(self, rel: str) -> None:
        path = os.path.join(self.test_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("sops: enc\n")

    def _pairs(self, patterns):
        self.manager.update_credentials(secrets_files=patterns)
        return dict(self.manager.resolve_encrypted_secret_files())

    def test_unknown_extension_maps_back_to_original_name(self):
        """k.pem.enc.yaml must decrypt to k.pem, not to an unignored k.pem.yaml."""
        self._touch("k.pem.enc.yaml")
        self.assertEqual(self._pairs(["*.pem"]), {"k.pem.enc.yaml": "k.pem"})

    def test_arbitrary_extension_maps_back_to_original_name(self):
        self._touch("api.token.enc.yaml")
        self.assertEqual(self._pairs(["*.token"]), {"api.token.enc.yaml": "api.token"})

    def test_yaml_and_json_sidecars_keep_their_extension(self):
        self._touch("secrets.enc.yaml")
        self._touch("gcp.enc.json")
        self.assertEqual(
            self._pairs(["secrets.yaml", "*gcp*.json"]),
            {"secrets.enc.yaml": "secrets.yaml", "gcp.enc.json": "gcp.json"},
        )

    def test_recursive_pattern_matches_root_and_nested_files(self):
        self._touch("secrets.enc.yaml")
        self._touch("esphome/secrets.enc.yaml")
        self.assertEqual(
            self._pairs(["**/secrets.yaml"]),
            {
                "secrets.enc.yaml": "secrets.yaml",
                "esphome/secrets.enc.yaml": "esphome/secrets.yaml",
            },
        )

    def test_glob_to_regex_semantics(self):
        rx = _glob_to_regex
        self.assertTrue(rx("**/secrets.yaml").match("secrets.yaml"))
        self.assertTrue(rx("**/secrets.yaml").match("a/b/SECRETS.yaml"))
        self.assertFalse(rx("*.pem").match("a/k.pem"))
        self.assertTrue(rx("*.pem").match("K.PEM"))
        self.assertTrue(rx("secrets/*.yaml").match("secrets/x.yaml"))
        self.assertFalse(rx("secrets/*.yaml").match("secrets/a/x.yaml"))
        self.assertTrue(rx("file?.txt").match("file1.txt"))
        self.assertFalse(rx("file?.txt").match("file/.txt"))
        self.assertTrue(rx("k[0-9].pem").match("k7.pem"))
        self.assertFalse(rx("k[!0-9].pem").match("k7.pem"))


if __name__ == "__main__":
    unittest.main()


