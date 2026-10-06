"""Integration tests for GitManager and SopsManager interaction."""

import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

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

    spec_sops = importlib.util.spec_from_file_location(
        "custom_components.git_ha_ppens.sops_manager",
        os.path.join(REPO_ROOT, "custom_components", "git_ha_ppens", "sops_manager.py"),
    )
    mod_sops = importlib.util.module_from_spec(spec_sops)
    sys.modules["custom_components.git_ha_ppens.sops_manager"] = mod_sops
    spec_sops.loader.exec_module(mod_sops)

    spec_git = importlib.util.spec_from_file_location(
        "custom_components.git_ha_ppens.git_manager",
        os.path.join(REPO_ROOT, "custom_components", "git_ha_ppens", "git_manager.py"),
    )
    mod_git = importlib.util.module_from_spec(spec_git)
    sys.modules["custom_components.git_ha_ppens.git_manager"] = mod_git
    spec_git.loader.exec_module(mod_git)

from custom_components.git_ha_ppens.git_manager import (
    GitError,
    GitManager,
    PreDeployCheckError,
    RestoreValidationError,
    SecretsError,
)
from custom_components.git_ha_ppens.sops_manager import (
    SopsError,
    SopsManager,
    generate_age_keypair,
)


class TestGitManagerSopsIntegration(unittest.IsolatedAsyncioTestCase):
    """Test GitManager with SopsManager attached."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_sops = MagicMock(spec=SopsManager)
        self.mock_sops.sync_secrets_to_encrypted = AsyncMock(return_value=["secrets.enc.yaml"])
        self.mock_sops.sync_encrypted_to_secrets = AsyncMock(return_value=["secrets.yaml"])
        self.mock_sops.backup_plain_secrets = MagicMock(return_value={"secrets.yaml": b"original_secrets"})
        self.mock_sops.restore_plain_secrets_backup = MagicMock()
        self.mock_sops.resolve_plain_secret_files = MagicMock(return_value=[])

        self.git_manager = GitManager(
            repo_path=self.test_dir,
            git_user="Test User",
            git_email="test@example.com",
            sops_manager=self.mock_sops,
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_commit_triggers_sops_encryption(self):
        """Test that calling commit() runs SOPS encryption before git status."""
        with patch.object(self.git_manager, "assert_repository_layout_safe", return_value=None):
            with patch.object(self.git_manager, "_run_git", return_value=""):
                # When git status --porcelain returns empty, nothing to commit
                result = await self.git_manager.commit()
                self.assertIsNone(result)
                self.mock_sops.sync_secrets_to_encrypted.assert_called_once()

    async def test_commit_wraps_sops_encryption_failure_as_secrets_error(self):
        """A SopsError from encryption must surface as SecretsError, not the raw SopsError.

        Regression coverage: callers throughout the integration (coordinator,
        commit/push services) only catch GitError, so an unwrapped SopsError
        would previously propagate as an unhandled exception.
        """
        self.mock_sops.sync_secrets_to_encrypted = AsyncMock(
            side_effect=SopsError("age recipient missing")
        )
        with patch.object(self.git_manager, "assert_repository_layout_safe", return_value=None):
            with self.assertRaises(SecretsError) as ctx:
                await self.git_manager.commit()
            self.assertIsInstance(ctx.exception, GitError)
            self.assertIn("age recipient missing", str(ctx.exception))

    async def test_restore_snapshot_wraps_sops_decryption_failure_and_rolls_back(self):
        """A SopsError while decrypting the restored tree must roll back and
        surface as SecretsError (caught wherever GitError already is)."""
        mock_preview = MagicMock()
        mock_preview.source_head = "11111111"
        mock_preview.target = MagicMock(
            hash="22222222", hash_short="22222222", message="snapshot commit"
        )
        self.mock_sops.sync_encrypted_to_secrets = AsyncMock(
            side_effect=SopsError("no age key configured")
        )

        with patch.object(self.git_manager, "assert_repository_layout_safe", return_value=None):
            with patch.object(
                self.git_manager, "get_restore_preview", new_callable=AsyncMock, return_value=mock_preview
            ):
                with patch.object(
                    self.git_manager, "is_worktree_clean", new_callable=AsyncMock, return_value=True
                ):
                    with patch.object(
                        self.git_manager, "get_head_sha", new_callable=AsyncMock, return_value="11111111"
                    ):
                        with patch.object(
                            self.git_manager, "_run_git", new_callable=AsyncMock
                        ) as mock_git:
                            mock_git.return_value = "secrets.enc.yaml\n"
                            with patch.object(
                                self.git_manager, "reset_hard", new_callable=AsyncMock
                            ) as mock_reset:
                                with self.assertRaises(SecretsError) as ctx:
                                    await self.git_manager.restore_snapshot(
                                        "22222222", expected_head="11111111"
                                    )

                                self.assertIsInstance(ctx.exception, GitError)
                                self.mock_sops.restore_plain_secrets_backup.assert_called_once_with(
                                    {"secrets.yaml": b"original_secrets"}
                                )
                                mock_reset.assert_called_once_with("11111111")

    async def test_scan_for_secrets_ignores_enc_files(self):
        """Test that scan_for_secrets filters out .enc.yaml and .sops.yaml."""
        staged_output = (
            "secrets.enc.yaml\n"
            "esphome/secrets.enc.yaml\n"
            "SERVICE_ACCOUNT.enc.json\n"
            ".sops.yaml\n"
            "regular_config.yaml\n"
        )
        with patch.object(self.git_manager, "_run_git", return_value=staged_output):
            with patch.object(self.git_manager, "_scan_files_for_secrets_sync", return_value=[]) as mock_scan:
                findings = await self.git_manager.scan_for_secrets()
                self.assertEqual(findings, [])
                # Only regular_config.yaml should be passed to the scanner
                mock_scan.assert_called_once()
                args, _ = mock_scan.call_args
                scanned_files = args[0]
                self.assertNotIn("secrets.enc.yaml", scanned_files)
                self.assertNotIn("esphome/secrets.enc.yaml", scanned_files)
                self.assertNotIn("SERVICE_ACCOUNT.enc.json", scanned_files)
                self.assertNotIn(".sops.yaml", scanned_files)
                self.assertIn("regular_config.yaml", scanned_files)

    async def test_pull_decrypts_secrets_when_commits_pulled(self):
        """Test that pull() decrypts secrets when new commits arrive."""
        rev_count_calls = 0

        async def fake_run_git(*args, **kwargs):
            nonlocal rev_count_calls
            if args[0] == "rev-list" and args[1] == "--count":
                rev_count_calls += 1
                return "5" if rev_count_calls == 1 else "6"  # pulled = 1
            if args[0] == "diff":
                return "secrets.enc.yaml\n"
            return ""

        with patch.object(self.git_manager, "is_remote_configured", return_value=True):
            with patch.object(self.git_manager, "get_head_sha", return_value="11111111"):
                with patch.object(self.git_manager, "_run_git", side_effect=fake_run_git):
                    result = await self.git_manager.pull(backup=False)
                    self.assertEqual(result.commits_pulled, 1)
                    self.mock_sops.backup_plain_secrets.assert_called_once()
                    self.mock_sops.sync_encrypted_to_secrets.assert_called_once()
                    self.mock_sops.restore_plain_secrets_backup.assert_not_called()

    async def test_pull_rolls_back_plain_secrets_on_sops_decryption_error(self):
        """Test that failed decryption during pull restores plain secrets and resets git."""
        rev_count_calls = 0

        async def fake_run_git(*args, **kwargs):
            nonlocal rev_count_calls
            if args[0] == "rev-list" and args[1] == "--count":
                rev_count_calls += 1
                return "5" if rev_count_calls == 1 else "6"
            return ""

        self.mock_sops.sync_encrypted_to_secrets.side_effect = SopsError("Corrupt secrets ciphertext")

        with patch.object(self.git_manager, "is_remote_configured", return_value=True):
            with patch.object(self.git_manager, "get_head_sha", return_value="11111111"):
                with patch.object(self.git_manager, "_run_git", side_effect=fake_run_git):
                    with patch.object(self.git_manager, "reset_hard", new_callable=AsyncMock) as mock_reset:
                        with self.assertRaises(SecretsError):
                            await self.git_manager.pull(backup=False)

                        self.mock_sops.restore_plain_secrets_backup.assert_called_once_with(
                            {"secrets.yaml": b"original_secrets"}
                        )
                        mock_reset.assert_called_once_with("11111111")

    async def test_pull_rolls_back_plain_secrets_on_pre_deploy_validation_error(self):
        """Test that pre-deploy validation failure after pull restores plain secrets."""
        rev_count_calls = 0

        async def fake_run_git(*args, **kwargs):
            nonlocal rev_count_calls
            if args[0] == "rev-list" and args[1] == "--count":
                rev_count_calls += 1
                return "5" if rev_count_calls == 1 else "6"
            return ""

        validate_mock = AsyncMock(return_value=["Validation failed: bad config"])

        with patch.object(self.git_manager, "is_remote_configured", return_value=True):
            with patch.object(self.git_manager, "get_head_sha", return_value="11111111"):
                with patch.object(self.git_manager, "_run_git", side_effect=fake_run_git):
                    with patch.object(self.git_manager, "reset_hard", new_callable=AsyncMock) as mock_reset:
                        with self.assertRaises(PreDeployCheckError):
                            await self.git_manager.pull(backup=False, validate=validate_mock)

                        self.mock_sops.restore_plain_secrets_backup.assert_called_once_with(
                            {"secrets.yaml": b"original_secrets"}
                        )
                        mock_reset.assert_called_once_with("11111111")

    async def test_restore_snapshot_rolls_back_plain_secrets_on_failure(self):
        """Test that failure during restore_snapshot rolls back plain secrets."""
        mock_preview = MagicMock()
        mock_preview.source_head = "11111111"
        mock_preview.target = MagicMock(hash="22222222", hash_short="22222222", message="snapshot commit")

        validate_mock = AsyncMock(return_value=["Restore validation failed"])

        with patch.object(self.git_manager, "assert_repository_layout_safe", return_value=None):
            with patch.object(self.git_manager, "get_restore_preview", new_callable=AsyncMock, return_value=mock_preview):
                with patch.object(self.git_manager, "is_worktree_clean", new_callable=AsyncMock, return_value=True):
                    with patch.object(self.git_manager, "get_head_sha", new_callable=AsyncMock, return_value="11111111"):
                        with patch.object(self.git_manager, "_run_git", new_callable=AsyncMock) as mock_git:
                            mock_git.return_value = "secrets.enc.yaml\n"
                            with patch.object(self.git_manager, "reset_hard", new_callable=AsyncMock) as mock_reset:
                                with self.assertRaises(RestoreValidationError):
                                    await self.git_manager.restore_snapshot("22222222", expected_head="11111111", validate=validate_mock)

                                self.mock_sops.backup_plain_secrets.assert_called_once()
                                self.mock_sops.sync_encrypted_to_secrets.assert_called_once()
                                self.mock_sops.restore_plain_secrets_backup.assert_called_once_with(
                                    {"secrets.yaml": b"original_secrets"}
                                )
                                mock_reset.assert_called_once_with("11111111")

    async def test_restore_snapshot_read_tree_failure_does_not_crash(self):
        """Regression test: a read-tree failure before the SOPS step must not raise
        UnboundLocalError. The original GitError must propagate and trigger rollback."""
        mock_preview = MagicMock()
        mock_preview.source_head = "11111111"
        mock_preview.target = MagicMock(
            hash="22222222", hash_short="22222222", message="snapshot commit"
        )

        read_tree_error = GitError("read-tree failed: corrupt object")

        async def fake_run_git(*args, **kwargs):
            if args[0] == "read-tree":
                raise read_tree_error
            return ""

        with patch.object(self.git_manager, "assert_repository_layout_safe", return_value=None):
            with patch.object(
                self.git_manager, "get_restore_preview", new_callable=AsyncMock, return_value=mock_preview
            ):
                with patch.object(
                    self.git_manager, "is_worktree_clean", new_callable=AsyncMock, return_value=True
                ):
                    with patch.object(
                        self.git_manager, "get_head_sha", new_callable=AsyncMock, return_value="11111111"
                    ):
                        with patch.object(
                            self.git_manager, "_run_git", side_effect=fake_run_git
                        ):
                            with patch.object(
                                self.git_manager, "reset_hard", new_callable=AsyncMock
                            ) as mock_reset:
                                with self.assertRaises(GitError) as ctx:
                                    await self.git_manager.restore_snapshot(
                                        "22222222", expected_head="11111111"
                                    )

                                # The original error must propagate unchanged, not an
                                # UnboundLocalError masking it.
                                self.assertIs(ctx.exception, read_tree_error)
                                mock_reset.assert_called_once_with("11111111")
                                # No SOPS backup was ever taken since the failure
                                # happened before that step was reached.
                                self.mock_sops.backup_plain_secrets.assert_not_called()
                                self.mock_sops.restore_plain_secrets_backup.assert_not_called()


class TestGitManagerSopsPlaintextProtection(unittest.IsolatedAsyncioTestCase):
    """Test the .gitignore enforcement that guarantees plaintext secrets never leak."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_sops = MagicMock(spec=SopsManager)
        self.mock_sops.sync_secrets_to_encrypted = AsyncMock(return_value=[])
        self.git_manager = GitManager(
            repo_path=self.test_dir,
            git_user="Test User",
            git_email="test@example.com",
            sops_manager=self.mock_sops,
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_commit_adds_missing_gitignore_rule_for_plain_secret(self):
        """A plain secret file not yet covered by .gitignore gets a rule added automatically."""
        self.mock_sops.resolve_plain_secret_files = MagicMock(return_value=["secrets.yaml"])

        # First check-ignore call: not ignored. After ensure_gitignore_patterns
        # appends a rule, the second check-ignore call reports it as ignored.
        ignore_calls = {"count": 0}

        async def fake_run_git(*args, **kwargs):
            if args[0] == "check-ignore":
                ignore_calls["count"] += 1
                return "secrets.yaml" if ignore_calls["count"] > 1 else ""
            if args[0] == "status":
                return ""
            return ""

        with patch.object(self.git_manager, "assert_repository_layout_safe", return_value=None):
            with patch.object(self.git_manager, "_run_git", side_effect=fake_run_git):
                with patch.object(
                    self.git_manager, "ensure_gitignore_patterns", new_callable=AsyncMock
                ) as mock_ensure:
                    result = await self.git_manager.commit()

        self.mock_sops.sync_secrets_to_encrypted.assert_called_once()
        mock_ensure.assert_called_once()
        probes = mock_ensure.call_args.args[0]
        self.assertEqual(probes, {"/secrets.yaml": "secrets.yaml"})
        # Nothing staged/changed in this scenario -> commit() returns None,
        # but the protection step must still have run before it checked status.
        self.assertIsNone(result)

    async def test_commit_refuses_when_plain_secret_cannot_be_ignored(self):
        """Refuse to commit rather than silently staging a plaintext secret."""
        self.mock_sops.resolve_plain_secret_files = MagicMock(return_value=["secrets.yaml"])

        async def fake_run_git(*args, **kwargs):
            if args[0] == "check-ignore":
                return ""  # Never reports as ignored, even after the "fix" attempt.
            return ""

        with patch.object(self.git_manager, "assert_repository_layout_safe", return_value=None):
            with patch.object(self.git_manager, "_run_git", side_effect=fake_run_git):
                with patch.object(
                    self.git_manager, "ensure_gitignore_patterns", new_callable=AsyncMock
                ):
                    with self.assertRaises(SecretsError):
                        await self.git_manager.commit()

    async def test_commit_untracks_already_tracked_plain_secret(self):
        """A plain secret file that was already committed before SOPS was
        enabled must be untracked (git rm --cached), not just gitignored,
        or the next commit would still capture its plaintext edits."""
        self.mock_sops.resolve_plain_secret_files = MagicMock(return_value=["secrets.yaml"])

        git_calls: list[tuple] = []

        async def fake_run_git(*args, **kwargs):
            git_calls.append(args)
            if args[0] == "check-ignore":
                # Already covered by .gitignore, but was tracked before
                # SOPS was enabled.
                return "secrets.yaml"
            if args[0] == "ls-files":
                return "secrets.yaml"
            if args[0] == "status":
                return ""
            return ""

        with patch.object(self.git_manager, "assert_repository_layout_safe", return_value=None):
            with patch.object(self.git_manager, "_run_git", side_effect=fake_run_git):
                await self.git_manager.commit()

        rm_calls = [call for call in git_calls if call[0] == "rm"]
        self.assertEqual(len(rm_calls), 1)
        self.assertIn("--cached", rm_calls[0])
        self.assertIn("secrets.yaml", rm_calls[0])

    async def test_pull_backup_encrypts_local_secret_edits_before_staging(self):
        """A local secrets.yaml edit must be encrypted (and thus preserved in the
        backup commit) before pull can overwrite it with the remote version."""
        self.mock_sops.resolve_plain_secret_files = MagicMock(return_value=[])
        self.mock_sops.backup_plain_secrets = MagicMock(return_value={})
        self.mock_sops.sync_encrypted_to_secrets = AsyncMock(return_value=[])

        call_order: list[str] = []

        async def fake_sync_secrets_to_encrypted(*args, **kwargs):
            call_order.append("encrypt")
            return []

        self.mock_sops.sync_secrets_to_encrypted = AsyncMock(
            side_effect=fake_sync_secrets_to_encrypted
        )

        async def fake_run_git(*args, **kwargs):
            if args[0] == "status":
                call_order.append("status")
                return "M secrets.yaml"
            if args[0] == "add":
                call_order.append("add")
                return ""
            if args[0] == "commit":
                call_order.append("commit")
                return ""
            if args[0] == "rev-list":
                return "0"
            if args[0] == "rev-parse":
                return "main"
            return ""

        with patch.object(self.git_manager, "is_remote_configured", return_value=True):
            with patch.object(self.git_manager, "get_head_sha", return_value="11111111"):
                with patch.object(self.git_manager, "_run_git", side_effect=fake_run_git):
                    await self.git_manager.pull(backup=True)

        self.mock_sops.sync_secrets_to_encrypted.assert_called_once()
        # Encryption must happen before the backup commit stages anything.
        self.assertEqual(call_order[:4], ["encrypt", "status", "add", "commit"])


@unittest.skipUnless(
    shutil.which("sops") and shutil.which("git"),
    "requires real sops and git binaries on PATH",
)
class TestGitManagerSopsRealRepository(unittest.IsolatedAsyncioTestCase):
    """End-to-end checks against a real git repository and a real sops binary."""

    def setUp(self):
        import subprocess

        self._subprocess = subprocess
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.repo = os.path.join(self.root, "repo")
        self.bare = os.path.join(self.root, "remote.git")
        os.makedirs(self.repo)
        self._git(self.root, "init", "-q", "--bare", "-b", "main", self.bare)
        self._git(self.repo, "init", "-q", "-b", "main")
        self._git(self.repo, "config", "user.email", "test@example.com")
        self._git(self.repo, "config", "user.name", "Test User")
        self._git(self.repo, "remote", "add", "origin", self.bare)

        key, recipient = generate_age_keypair()
        self.sops = SopsManager(
            repo_path=self.repo,
            age_key=key,
            age_recipient=recipient,
            storage_dir=Path(self.root) / "storage",
        )
        self.git_manager = GitManager(
            self.repo, "Test User", "test@example.com", sops_manager=self.sops
        )

    def _git(self, cwd, *args):
        return self._subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout

    def _write(self, rel, content):
        with open(os.path.join(self.repo, rel), "w", encoding="utf-8") as f:
            f.write(content)

    def _history(self):
        return self._git(self.repo, "log", "-p", "--all")

    async def test_commit_never_leaks_plaintext_and_pull_round_trips(self):
        self._write("secrets.yaml", "password: PLAINTEXT_ONE\n")
        self._write("configuration.yaml", "homeassistant: {}\n")
        await self.git_manager.setup_gitignore()
        await self.git_manager.commit("initial")
        await self.git_manager.push(validate=None)

        tracked = self._git(self.repo, "ls-files").split()
        self.assertIn("secrets.enc.yaml", tracked)
        self.assertNotIn("secrets.yaml", tracked)
        self.assertNotIn("PLAINTEXT_ONE", self._history())

        clone = os.path.join(self.root, "clone")
        self._git(self.root, "clone", "-q", self.bare, clone)
        self._git(clone, "config", "user.email", "test@example.com")
        self._git(clone, "config", "user.name", "Test User")
        clone_sops = SopsManager(
            repo_path=clone,
            age_key=self.sops.age_key,
            age_recipient=self.sops.age_recipient,
            storage_dir=Path(self.root) / "clone-storage",
        )
        clone_git = GitManager(clone, "Test User", "test@example.com", sops_manager=clone_sops)
        await clone_git.setup_gitignore()
        await clone_sops.sync_encrypted_to_secrets()

        self._write("secrets.yaml", "password: PLAINTEXT_TWO\n")
        await self.git_manager.commit("second")
        await self.git_manager.push(validate=None)

        result = await clone_git.pull(backup=True)
        self.assertEqual(result.commits_pulled, 1)
        with open(os.path.join(clone, "secrets.yaml"), encoding="utf-8") as f:
            self.assertIn("PLAINTEXT_TWO", f.read())

    async def test_pull_backup_does_not_commit_plaintext_of_previously_tracked_secret(self):
        """A secrets.yaml committed before SOPS was enabled must not leak via the backup commit."""
        self._write("secrets.yaml", "password: OLD\n")
        self._git(self.repo, "add", "-A")
        self._git(self.repo, "commit", "-qm", "before sops")
        self._git(self.repo, "push", "-q", "origin", "main")
        await self.git_manager.setup_gitignore()

        self._write("secrets.yaml", "password: LOCAL_EDIT_MUST_STAY_SECRET\n")
        await self.git_manager.pull(backup=True)

        self.assertNotIn("LOCAL_EDIT_MUST_STAY_SECRET", self._history())
        self.assertNotIn("secrets.yaml", self._git(self.repo, "ls-files").split())

    async def test_unrecognised_extension_secret_round_trips_through_pull(self):
        self.sops.update_credentials(secrets_files=["*.pem"])
        pem = "-----BEGIN KEY-----\nabc\n-----END KEY-----\n"
        self._write("k.pem", pem)
        await self.git_manager.setup_gitignore()
        await self.git_manager.commit("pem")

        os.remove(os.path.join(self.repo, "k.pem"))
        restored = await self.sops.sync_encrypted_to_secrets()

        self.assertEqual(restored, ["k.pem"])
        with open(os.path.join(self.repo, "k.pem"), encoding="utf-8") as f:
            self.assertEqual(f.read(), pem)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "k.pem.yaml")))


if __name__ == "__main__":
    unittest.main()
