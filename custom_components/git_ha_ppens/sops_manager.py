"""SOPS (Secrets OPerationS) manager for git-ha-ppens.

Provides age key management, binary discovery/download, and transparent
encryption/decryption of Home Assistant secrets for GitOps workflows.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import sys
import time
from typing import Final, Mapping, Sequence
import urllib.request

import yaml

from .const import (
    DEFAULT_SOPS_SECRETS_FILES,
    DEFAULT_SOPS_VERSION,
    SOPS_ENC_EXTENSION,
)

_LOGGER = logging.getLogger(__name__)


# RFC 7748 Curve25519 constants

_P = 2**255 - 19
_A24 = 121665

# BIP173 Bech32 character set
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

# HRP prefixes for age
AGE_SECRET_KEY_HRP: Final = "AGE-SECRET-KEY-"
AGE_RECIPIENT_HRP: Final = "age"

# Official standalone SOPS release binary SHA256 checksums
SOPS_BINARY_SHA256: Final[dict[str, str]] = {
    "sops-v3.9.4.linux.arm64": "16564c6b181d88505d9e0dfef62771894293d85cde5884d9b1a843859eee174b",
    "sops-v3.9.4.linux.amd64": "5488e32bc471de7982ad895dd054bbab3ab91c417a118426134551e9626e4e85",
    "sops-v3.9.4.darwin.arm64": "51ee2c3ec2c4331cfe1c0c25168e1c4c8036900842700b9bb074dda92a6017f2",
    "sops-v3.9.4.darwin.amd64": "f48d73efc278326e54d0e6a056b285fd8f5f28549b19aff9b0fedbbdd846b20c",
}


class SopsError(Exception):
    """Base exception for all SOPS operations."""


class SopsBinaryNotFoundError(SopsError):
    """Raised when the SOPS executable cannot be found or downloaded."""


class SopsConfigError(SopsError):
    """Raised when SOPS configuration or age keys are invalid."""


class SopsEncryptionError(SopsError):
    """Raised when file encryption fails."""


class SopsDecryptionError(SopsError):
    """Raised when file decryption fails or produces invalid YAML."""


# --- Pure-Python RFC 7748 Curve25519 & BIP 173 Bech32 for Age Keys ---


def _x25519(k: int, u: int) -> int:
    """Perform X25519 scalar multiplication (RFC 7748 Montgomery ladder)."""
    x_1 = u
    x_2 = 1
    z_2 = 0
    x_3 = u
    z_3 = 1
    swap = 0
    for t in range(254, -1, -1):
        k_t = (k >> t) & 1
        swap ^= k_t
        if swap:
            x_2, x_3 = x_3, x_2
            z_2, z_3 = z_3, z_2
        swap = k_t
        a = (x_2 + z_2) % _P
        aa = (a * a) % _P
        b = (x_2 - z_2) % _P
        bb = (b * b) % _P
        e = (aa - bb) % _P
        c = (x_3 + z_3) % _P
        d = (x_3 - z_3) % _P
        da = (d * a) % _P
        cb = (c * b) % _P
        x_3 = ((da + cb) ** 2) % _P
        z_3 = (x_1 * ((da - cb) ** 2)) % _P
        x_2 = (aa * bb) % _P
        z_2 = (e * (aa + _A24 * e)) % _P
    if swap:
        x_2, x_3 = x_3, x_2
        z_2, z_3 = z_3, z_2
    return (x_2 * pow(z_2, _P - 2, _P)) % _P


def _clamp(k_bytes: bytes) -> int:
    """Clamp scalar bytes according to RFC 7748 Section 5."""
    b = bytearray(k_bytes)
    b[0] &= 248
    b[31] &= 127
    b[31] |= 64
    return int.from_bytes(b, "little")


def _bech32_polymod(values: Sequence[int]) -> int:
    """Compute Bech32 polymod checksum."""
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    """Expand HRP string for Bech32 checksum calculation."""
    hrp_lower = hrp.lower()
    return (
        [ord(x) >> 5 for x in hrp_lower]
        + [0]
        + [ord(x) & 31 for x in hrp_lower]
    )


def _bech32_create_checksum(hrp: str, data: Sequence[int]) -> list[int]:
    """Compute Bech32 checksum bytes."""
    values = _bech32_hrp_expand(hrp) + list(data)
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _bech32_verify_checksum(hrp: str, data: Sequence[int]) -> bool:
    """Verify Bech32 checksum."""
    return _bech32_polymod(_bech32_hrp_expand(hrp) + list(data)) == 1


def _convertbits(
    data: bytes | Sequence[int],
    frombits: int,
    tobits: int,
    pad: bool = True,
) -> list[int] | None:
    """General power-of-2 base conversion."""
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    elif not pad and (bits >= frombits or ((acc << (tobits - bits)) & maxv)):
        return None
    return ret


def _bech32_encode(hrp: str, data: Sequence[int]) -> str:
    """Encode HRP and 5-bit data as Bech32 string."""
    combined = list(data) + _bech32_create_checksum(hrp, data)
    return hrp + "1" + "".join([_BECH32_CHARSET[d] for d in combined])


def _bech32_decode(bech: str) -> tuple[str | None, list[int] | None]:
    """Validate and decode a Bech32 string.

    Per BIP 173, a Bech32 string must be entirely lowercase or entirely
    uppercase; mixed case is invalid (and ambiguous, since the checksum
    algorithm itself is case-insensitive) and must be rejected rather than
    silently accepted via case-folding.
    """
    bech = bech.strip()
    if bech != bech.lower() and bech != bech.upper():
        return (None, None)
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech):
        return (None, None)
    hrp = bech[:pos]
    data: list[int] = []
    for c in bech[pos + 1 :]:
        d = _BECH32_CHARSET.find(c.lower())
        if d == -1:
            return (None, None)
        data.append(d)
    if not _bech32_verify_checksum(hrp, data):
        return (None, None)
    return (hrp, data[:-6])


def generate_age_keypair() -> tuple[str, str]:
    """Generate a new age keypair (secret key, recipient) in pure Python.

    Returns:
        tuple[str, str]: (secret_key, recipient)
            secret_key starts with 'AGE-SECRET-KEY-1...' (uppercase)
            recipient starts with 'age1...' (lowercase)
    """
    raw_priv = secrets.token_bytes(32)
    k = _clamp(raw_priv)
    pub_int = _x25519(k, 9)
    raw_pub = pub_int.to_bytes(32, "little")

    secret_5bit = _convertbits(raw_priv, 8, 5, pad=True)
    if secret_5bit is None:
        raise SopsConfigError("Failed to convert private key bits")
    secret_key = _bech32_encode(AGE_SECRET_KEY_HRP, secret_5bit).upper()

    pub_5bit = _convertbits(raw_pub, 8, 5, pad=True)
    if pub_5bit is None:
        raise SopsConfigError("Failed to convert public key bits")
    recipient = _bech32_encode(AGE_RECIPIENT_HRP, pub_5bit).lower()

    return secret_key, recipient


def validate_age_secret_key(secret_key: str) -> bool:
    """Validate whether an age secret key has valid HRP and checksum."""
    secret_key = secret_key.strip()
    if not secret_key.upper().startswith(f"{AGE_SECRET_KEY_HRP}1"):
        return False
    hrp, data = _bech32_decode(secret_key)
    if hrp is None or data is None or hrp.upper() != AGE_SECRET_KEY_HRP:
        return False
    raw = _convertbits(data, 5, 8, pad=False)
    return raw is not None and len(raw) == 32


def validate_age_recipient(recipient: str) -> bool:
    """Validate whether an age recipient string has valid HRP and checksum."""
    recipient = recipient.strip()
    if not recipient.lower().startswith(f"{AGE_RECIPIENT_HRP}1"):
        return False
    hrp, data = _bech32_decode(recipient)
    if hrp is None or data is None or hrp.lower() != AGE_RECIPIENT_HRP:
        return False
    raw = _convertbits(data, 5, 8, pad=False)
    return raw is not None and len(raw) == 32


def age_secret_to_recipient(secret_key: str) -> str:
    """Derive the public age recipient from an age secret key."""
    secret_key = secret_key.strip()
    if not validate_age_secret_key(secret_key):
        raise SopsConfigError("Invalid age secret key format or checksum")

    hrp, data = _bech32_decode(secret_key)
    if data is None:
        raise SopsConfigError("Could not decode secret key data")

    raw_priv_list = _convertbits(data, 5, 8, pad=False)
    if raw_priv_list is None or len(raw_priv_list) != 32:
        raise SopsConfigError("Decoded secret key is not exactly 32 bytes")

    raw_priv = bytes(raw_priv_list)
    k = _clamp(raw_priv)
    pub_int = _x25519(k, 9)
    raw_pub = pub_int.to_bytes(32, "little")

    pub_5bit = _convertbits(raw_pub, 8, 5, pad=True)
    if pub_5bit is None:
        raise SopsConfigError("Failed to convert public key bits")
    return _bech32_encode(AGE_RECIPIENT_HRP, pub_5bit).lower()


# --- SOPS Platform & Binary Management ---


def get_sops_platform_asset(version: str = DEFAULT_SOPS_VERSION) -> tuple[str, str] | None:
    """Determine the release asset name and download URL for the current system.

    Returns:
        tuple[filename, url] or None if platform/architecture is not supported.
    """
    sys_plat = sys.platform.lower()
    machine = platform.machine().lower()

    os_part = "linux"
    if "darwin" in sys_plat:
        os_part = "darwin"
    elif "linux" not in sys_plat:
        return None

    arch_part: str | None = None
    if machine in ("x86_64", "amd64"):
        arch_part = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch_part = "arm64"

    if not arch_part:
        return None

    filename = f"sops-{version}.{os_part}.{arch_part}"
    url = f"https://github.com/getsops/sops/releases/download/{version}/{filename}"
    return filename, url


async def async_ensure_sops_binary(
    storage_dir: Path,
    custom_path: str = "",
    version: str = DEFAULT_SOPS_VERSION,
) -> Path:
    """Locate or download the SOPS binary.

    Args:
        storage_dir: Directory where downloaded binaries are stored (e.g. .storage/git_ha_ppens)
        custom_path: User-configured path to sops executable
        version: Release version tag to download if not installed

    Returns:
        Path to verified executable sops binary.

    Raises:
        SopsBinaryNotFoundError: If no executable is found or download fails.
    """
    # 1. Custom path check
    if custom_path:
        custom_exec = Path(custom_path)
        if custom_exec.is_file() and os.access(custom_exec, os.X_OK):
            return custom_exec
        raise SopsBinaryNotFoundError(
            f"Configured SOPS binary at '{custom_path}' does not exist or is not executable."
        )

    # 2. System PATH check
    which_sops = shutil.which("sops")
    if which_sops:
        which_path = Path(which_sops)
        if os.access(which_path, os.X_OK):
            return which_path

    # 3. Storage directory check
    bin_dir = storage_dir / "bin"
    bin_path = bin_dir / "sops"
    if bin_path.is_file() and os.access(bin_path, os.X_OK):
        return bin_path

    # 4. Download binary from official GitHub releases
    asset = get_sops_platform_asset(version)
    if not asset:
        raise SopsBinaryNotFoundError(
            f"No official SOPS binary available for platform {sys.platform} ({platform.machine()}). "
            "Please install SOPS manually and specify the path in integration options."
        )

    filename, download_url = asset
    bin_dir.mkdir(parents=True, exist_ok=True)
    temp_file = bin_dir / f"sops.{os.getpid()}.{secrets.token_hex(8)}.tmp"

    _LOGGER.info("Downloading official SOPS binary from %s", download_url)

    def _download_sync() -> None:
        req = urllib.request.Request(
            download_url,
            headers={"User-Agent": "HomeAssistant-git-ha-ppens"},
        )
        hasher = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=30) as response:
            with open(temp_file, "wb") as out_file:
                while chunk := response.read(65536):
                    hasher.update(chunk)
                    out_file.write(chunk)

        expected_hash = SOPS_BINARY_SHA256.get(filename)
        if not expected_hash:
            raise SopsBinaryNotFoundError(
                f"Refusing to install SOPS binary '{filename}': no pinned SHA-256 "
                "checksum is known for this release/platform combination. "
                "Install SOPS manually and set the binary path in integration options."
            )
        actual_hash = hasher.hexdigest()
        if actual_hash.lower() != expected_hash.lower():
            raise SopsBinaryNotFoundError(
                f"Integrity check failed for downloaded SOPS binary '{filename}': "
                f"expected sha256 {expected_hash}, got {actual_hash}"
            )

        temp_file.chmod(0o755)
        temp_file.replace(bin_path)

    try:
        await asyncio.to_thread(_download_sync)
    except Exception as err:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except OSError:
                pass
        raise SopsBinaryNotFoundError(
            f"Failed to download SOPS binary from {download_url}: {err}"
        ) from err

    _LOGGER.info("SOPS binary successfully installed at %s", bin_path)
    return bin_path


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a repo-relative glob into a case-insensitive regex.

    ``*``/``?`` never cross a path separator, ``**/`` matches zero or more
    directories and ``**`` matches anything, mirroring ``Path.glob`` on
    Python 3.12+. Used both to pick the right plain filename for an
    encrypted sidecar and as the ``Path.glob(case_sensitive=...)`` fallback
    on older Pythons.
    """
    pat = pattern.lstrip("/")
    out: list[str] = []
    i = 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        elif pat[i] == "[" and (end := pat.find("]", i + 2)) != -1:
            body = pat[i + 1 : end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append(f"[{body}]")
            i = end + 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("".join(out) + r"\Z", re.IGNORECASE)


# --- SOPS Manager Class ---


class SopsManager:
    """Manages encryption and decryption of secrets using SOPS and age."""

    def __init__(
        self,
        repo_path: str | Path,
        age_key: str = "",
        age_recipient: str = "",
        custom_binary_path: str = "",
        storage_dir: Path | None = None,
        secrets_files: Sequence[str] | None = None,
    ) -> None:
        """Initialize the SOPS manager."""
        self._repo_path = Path(repo_path)
        self._age_key = age_key.strip()
        self._age_recipient = age_recipient.strip()
        self._custom_binary_path = custom_binary_path.strip()
        self._storage_dir = storage_dir or (self._repo_path / ".storage" / "git_ha_ppens")
        self._cached_binary_path: Path | None = None
        self._binary_lookup_failed_at: float = 0.0
        self._binary_lookup_failure: SopsError | None = None
        # Optimistic default until the first is_available() check actually
        # runs, so sops_status doesn't report "binary_missing" before that
        # check has ever had a chance to execute. Callers previously guarded
        # this with hasattr()/getattr(..., True), which had the same effect
        # less explicitly; the attribute is now always present.
        self._binary_available: bool = True
        self._availability_checked_at: float = 0.0
        self._secrets_files: list[str] = [
            f.strip()
            for f in (secrets_files or DEFAULT_SOPS_SECRETS_FILES)
            if f.strip() and not f.strip().startswith("#")
        ]

        # Hash cache to track plain secret file modifications and prevent unnecessary re-encryption
        self._plain_hashes: dict[str, str] = {}
        self._load_cached_hashes()

        # If recipient is missing but secret key is present, auto-derive recipient
        if not self._age_recipient and self._age_key:
            try:
                self._age_recipient = age_secret_to_recipient(self._age_key)
            except SopsConfigError as err:
                _LOGGER.warning("Could not derive age recipient from secret key: %s", err)

    @property
    def repo_path(self) -> Path:
        """Return the repository root path."""
        return self._repo_path

    @property
    def age_key(self) -> str:
        """Return the configured age secret key."""
        return self._age_key

    @property
    def age_recipient(self) -> str:
        """Return the configured age recipient."""
        return self._age_recipient

    @property
    def secrets_files(self) -> list[str]:
        """Return the configured secret files or patterns."""
        return list(self._secrets_files)

    def update_credentials(
        self,
        age_key: str | None = None,
        age_recipient: str | None = None,
        custom_binary_path: str | None = None,
        secrets_files: Sequence[str] | None = None,
    ) -> None:
        """Update runtime credentials and paths."""
        if age_key is not None:
            self._age_key = age_key.strip()
        if age_recipient is not None:
            self._age_recipient = age_recipient.strip()
        if custom_binary_path is not None:
            self._custom_binary_path = custom_binary_path.strip()
            self._cached_binary_path = None
            self._availability_checked_at = 0.0
        if secrets_files is not None:
            self._secrets_files = [
                f.strip()
                for f in secrets_files
                if f.strip() and not f.strip().startswith("#")
            ]

        if not self._age_recipient and self._age_key:
            try:
                self._age_recipient = age_secret_to_recipient(self._age_key)
            except SopsConfigError:
                pass

    # Minimum time to wait before retrying a failed download/lookup, so a
    # missing or unsupported binary does not trigger a fresh download attempt
    # on every coordinator refresh interval.
    _BINARY_LOOKUP_RETRY_COOLDOWN: Final[float] = 300.0

    # How long a successful/failed is_available() result stays valid, so a
    # `sops --version` subprocess isn't spawned on every coordinator refresh
    # (default every 30s).
    _AVAILABILITY_CACHE_TTL: Final[float] = 60.0

    async def get_binary_path(self) -> Path:
        """Get or discover the verified SOPS binary path."""
        if self._cached_binary_path and self._cached_binary_path.is_file():
            return self._cached_binary_path

        if self._binary_lookup_failure is not None:
            elapsed = time.monotonic() - self._binary_lookup_failed_at
            if elapsed < self._BINARY_LOOKUP_RETRY_COOLDOWN:
                raise self._binary_lookup_failure

        try:
            binary_path = await async_ensure_sops_binary(
                storage_dir=self._storage_dir,
                custom_path=self._custom_binary_path,
            )
        except SopsError as err:
            self._binary_lookup_failure = err
            self._binary_lookup_failed_at = time.monotonic()
            raise

        self._cached_binary_path = binary_path
        self._binary_lookup_failure = None
        return binary_path

    async def is_available(self) -> bool:
        """Check whether SOPS binary and key configuration are functional.

        The result is cached for ``_AVAILABILITY_CACHE_TTL`` seconds so
        frequent callers (e.g. the coordinator's poll loop) don't spawn a
        `sops --version` subprocess on every call.
        """
        elapsed = time.monotonic() - self._availability_checked_at
        if elapsed < self._AVAILABILITY_CACHE_TTL:
            return self._binary_available

        try:
            bin_path = await self.get_binary_path()
            version_str = await self._run_sops_cmd(bin_path, "--version")
            self._binary_available = bool(version_str)
        except (SopsError, OSError):
            self._binary_available = False
        finally:
            self._availability_checked_at = time.monotonic()
        return self._binary_available

    def _redact_output(self, text: str) -> str:
        """Redact sensitive keys from command output or errors."""
        redacted = text
        if self._age_key:
            redacted = redacted.replace(self._age_key, "[REDACTED_AGE_KEY]")
        redacted = re.sub(
            r"AGE-SECRET-KEY-1[0-9A-Z]{58}",
            "[REDACTED_AGE_KEY]",
            redacted,
            flags=re.IGNORECASE,
        )
        return redacted

    async def _exec_sops(
        self,
        bin_path: Path,
        *args: str,
        extra_env: Mapping[str, str] | None = None,
    ) -> bytes:
        """Execute SOPS binary with sanitized environment; return raw stdout bytes.

        Shared by ``_run_sops_cmd`` (text output) and ``_run_sops_cmd_bytes``
        (raw binary output, used for non-YAML/JSON secrets) so both go
        through identical process handling and error reporting.
        """
        cmd = [str(bin_path), *args]
        env = {
            **os.environ,
            "LC_ALL": "C",
        }
        if self._age_key:
            env["SOPS_AGE_KEY"] = self._age_key
        if extra_env:
            env.update(extra_env)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._repo_path),
                env=env,
            )
            stdout_bytes, stderr_bytes = await process.communicate()
        except OSError as err:
            raise SopsError(f"Failed to execute SOPS binary: {err}") from err

        if process.returncode != 0:
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
            stdout_preview = stdout_bytes.decode("utf-8", errors="replace").strip()
            err_msg = self._redact_output(stderr or stdout_preview or f"code {process.returncode}")
            raise SopsError(f"SOPS command failed: {err_msg}")

        return stdout_bytes

    async def _run_sops_cmd(
        self,
        bin_path: Path,
        *args: str,
        extra_env: Mapping[str, str] | None = None,
    ) -> str:
        """Execute SOPS binary and return stdout decoded as UTF-8 text."""
        stdout_bytes = await self._exec_sops(bin_path, *args, extra_env=extra_env)
        return stdout_bytes.decode("utf-8", errors="replace")

    async def _run_sops_cmd_bytes(
        self,
        bin_path: Path,
        *args: str,
        extra_env: Mapping[str, str] | None = None,
    ) -> bytes:
        """Execute SOPS binary and return raw stdout bytes, undecoded.

        Used for non-YAML/JSON secrets (``--output-type binary``) so
        arbitrary bytes round-trip exactly instead of being mangled by a
        UTF-8 decode/re-encode cycle.
        """
        return await self._exec_sops(bin_path, *args, extra_env=extra_env)

    async def ensure_sops_config(self, recipient: str | None = None) -> bool:
        """Ensure .sops.yaml configuration exists in repository root.

        Creates .sops.yaml if it does not exist.
        If it exists and the effective recipient is not referenced in the existing file,
        it appends the recipient without destroying existing multi-recipient rules.
        """
        config_path = self._repo_path / ".sops.yaml"
        effective_recipient = recipient or self._age_recipient
        if not effective_recipient:
            return False

        # Read existing file to check whether recipient is already present
        if config_path.is_file():
            existing = await asyncio.to_thread(
                config_path.read_text, encoding="utf-8"
            )
            if effective_recipient in existing:
                return False  # Already correctly configured

            # Try to safely parse and append recipient without destroying existing configuration
            try:
                parsed = await asyncio.to_thread(yaml.safe_load, existing)
            except yaml.YAMLError as err:
                _LOGGER.warning(
                    "Could not append recipient to existing .sops.yaml because it is "
                    "not valid YAML; leaving it untouched: %s", err,
                )
                return False

            if (
                isinstance(parsed, dict)
                and "creation_rules" in parsed
                and isinstance(parsed["creation_rules"], list)
                and parsed["creation_rules"]
                and isinstance(parsed["creation_rules"][0], dict)
            ):
                first_rule = parsed["creation_rules"][0]
                existing_age = first_rule.get("age", "")
                if isinstance(existing_age, str) and existing_age.strip():
                    first_rule["age"] = f"{existing_age.strip()},{effective_recipient}"
                elif isinstance(existing_age, list):
                    first_rule["age"].append(effective_recipient)
                else:
                    first_rule["age"] = effective_recipient

                updated = await asyncio.to_thread(
                    yaml.safe_dump, parsed, sort_keys=False
                )
                await asyncio.to_thread(config_path.write_text, updated, encoding="utf-8")
                _LOGGER.info(
                    "Appended age recipient %s to existing .sops.yaml",
                    effective_recipient,
                )
                return True

            # The existing file doesn't match the shape we know how to safely
            # merge into (e.g. no creation_rules, or an unexpected first
            # entry). Leave it untouched rather than falling through to the
            # "create a new file" branch below, which would silently
            # overwrite -- and discard -- whatever the user has configured
            # there (other creation_rules entries, comments, path_regex
            # scoping, etc.).
            _LOGGER.warning(
                "Existing .sops.yaml at %s does not have a recognizable "
                "creation_rules structure; leaving it untouched. Add recipient "
                "%s to it manually, or encryption will fail with 'no matching "
                "creation rules'.",
                config_path,
                effective_recipient,
            )
            return False

        content = (
            "# git-ha-ppens: Auto-generated SOPS configuration for Home Assistant\n"
            "creation_rules:\n"
            "  - path_regex: .*\n"
            f"    age: >-\n      {effective_recipient}\n"
        )
        await asyncio.to_thread(config_path.write_text, content, encoding="utf-8")
        _LOGGER.info(
            "Wrote .sops.yaml for age recipient %s", effective_recipient
        )
        return True

    def _get_hash_cache_path(self) -> Path:
        """Return the path to the persistent hash cache file."""
        return self._storage_dir / "git_ha_ppens_sops_hashes.json"

    def _load_cached_hashes(self) -> None:
        """Load cached plain secret file hashes from storage."""
        cache_path = self._get_hash_cache_path()
        if cache_path.is_file():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._plain_hashes = {str(k): str(v) for k, v in data.items()}
            except Exception as err:
                _LOGGER.debug("Could not load SOPS hash cache: %s", err)

    def _save_cached_hashes_sync(self) -> None:
        """Persist cached plain secret file hashes to storage synchronously."""
        cache_path = self._get_hash_cache_path()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(self._plain_hashes), encoding="utf-8")
        except Exception as err:
            _LOGGER.debug("Could not save SOPS hash cache: %s", err)

    def _save_cached_hashes(self) -> None:
        """Persist cached plain secret file hashes to storage without blocking event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            loop.run_in_executor(None, self._save_cached_hashes_sync)
        else:
            self._save_cached_hashes_sync()

    async def ensure_sops_gitignore(self) -> bool:
        """Ensure that encrypted secret files are never ignored by .gitignore."""
        gitignore_path = self._repo_path / ".gitignore"
        if not gitignore_path.is_file():
            return False

        try:
            content = await asyncio.to_thread(gitignore_path.read_text, encoding="utf-8")
        except OSError:
            return False

        patterns = ["!*.enc.yaml", "!*.enc.yml", "!*.enc.json"]
        lines = [line.strip() for line in content.splitlines()]
        missing = [p for p in patterns if p not in lines]
        if not missing:
            return False

        if content and not content.endswith("\n"):
            content += "\n"
        content += "\n# Allow SOPS encrypted secrets files\n"
        content += "\n".join(missing) + "\n"
        await asyncio.to_thread(gitignore_path.write_text, content, encoding="utf-8")
        _LOGGER.info("Appended SOPS unignore patterns to .gitignore: %s", missing)
        return True

    @staticmethod
    def _sops_content_type(relative_path: str) -> str:
        """Classify a plain secret's content as a SOPS ``--input``/``--output-type``.

        SOPS structurally parses YAML/JSON so it can encrypt individual
        values, and treats everything else as an opaque blob ("binary"
        store). Passing this explicitly (instead of letting SOPS guess from
        a file extension it may not recognise, such as .txt or .pem) is what
        makes encrypt_file()/decrypt_file() round-trip exactly for
        non-YAML/JSON secrets.
        """
        ext = Path(relative_path).name.lower()
        if ext.endswith((".yaml", ".yml")):
            return "yaml"
        if ext.endswith(".json"):
            return "json"
        return "binary"

    @staticmethod
    def _sops_envelope_type(enc_relative_path: str) -> str:
        """Return the SOPS file format of an encrypted sidecar's own envelope.

        This is always yaml or json (get_encrypted_relative_path never
        produces another extension), independent of what content type the
        wrapped plaintext has.
        """
        return "json" if enc_relative_path.lower().endswith(".json") else "yaml"

    @staticmethod
    def get_encrypted_relative_path(plain_relative_path: str) -> str:
        """Convert a plain secrets filename to its encrypted counterpart.

        Idempotent: a path that is already an encrypted sidecar name (as
        produced by this same function, or passed in directly by a caller
        such as the ``decrypt_secrets`` service) is returned unchanged
        instead of being encrypted a second time.

        Example:
            'secrets.yaml' -> 'secrets.enc.yaml'
            'esphome/secrets.yaml' -> 'esphome/secrets.enc.yaml'
            'SERVICE_ACCOUNT.json' -> 'SERVICE_ACCOUNT.enc.json'
            'secrets.enc.yaml' -> 'secrets.enc.yaml'
        """
        p = Path(plain_relative_path)
        name = p.name
        name_lower = name.lower()
        if name_lower.endswith((".enc.yaml", ".enc.yml", ".enc.json")) or name_lower.endswith(
            SOPS_ENC_EXTENSION
        ):
            return plain_relative_path
        if name.endswith(".yaml"):
            new_name = name[:-5] + ".enc.yaml"
        elif name.endswith(".yml"):
            new_name = name[:-4] + ".enc.yml"
        elif name.endswith(".json"):
            new_name = name[:-5] + ".enc.json"
        else:
            new_name = name + SOPS_ENC_EXTENSION
        return str(p.parent / new_name) if str(p.parent) != "." else new_name

    @staticmethod
    def get_plain_relative_path(enc_relative_path: str) -> str:
        """Convert an encrypted relative path to its plain counterpart.

        Example:
            'secrets.enc.yaml' -> 'secrets.yaml'
            'esphome/secrets.enc.yaml' -> 'esphome/secrets.yaml'
            'SERVICE_ACCOUNT.enc.json' -> 'SERVICE_ACCOUNT.json'
            'SERVICE_ACCOUNT.json.enc.yaml' -> 'SERVICE_ACCOUNT.json'
        """
        p = Path(enc_relative_path)
        name = p.name
        for ext in (".json", ".txt", ".conf", ".cfg", ".ini", ".env", ".yaml", ".yml"):
            if name.endswith(f"{ext}.enc.yaml"):
                new_name = name[:-9]
                return str(p.parent / new_name) if str(p.parent) != "." else new_name
        if name.endswith(".enc.json"):
            new_name = name[:-9] + ".json"
        elif name.endswith(".enc.yaml"):
            new_name = name[:-9] + ".yaml"
        elif name.endswith(".enc.yml"):
            new_name = name[:-8] + ".yml"
        elif ".enc." in name:
            new_name = name.replace(".enc.", ".")
        elif name.endswith(SOPS_ENC_EXTENSION):
            new_name = name[:-len(SOPS_ENC_EXTENSION)]
        else:
            new_name = name
        return str(p.parent / new_name) if str(p.parent) != "." else new_name

    async def encrypt_file(
        self,
        plain_relative_path: str,
        enc_relative_path: str | None = None,
        force: bool = False,
    ) -> bool:
        """Encrypt a plain secrets file to its encrypted counterpart.

        Returns True if the encrypted file was created or updated, False if skipped.
        """
        plain_file = self._repo_path / plain_relative_path
        if not plain_file.is_file():
            return False

        target_rel = enc_relative_path or self.get_encrypted_relative_path(plain_relative_path)
        enc_file = self._repo_path / target_rel

        # Read plain content and compute sha256 to avoid unnecessary re-encryption
        try:
            content_bytes = await asyncio.to_thread(plain_file.read_bytes)
        except OSError as err:
            _LOGGER.warning("Could not read secret file '%s': %s", plain_relative_path, err)
            return False

        current_hash = hashlib.sha256(content_bytes).hexdigest()

        # If encrypted file already exists, check if plain content has actually changed
        if not force and enc_file.is_file():
            cached_hash = self._plain_hashes.get(plain_relative_path)
            if cached_hash == current_hash:
                return False

            if not cached_hash:
                try:
                    plain_mtime = plain_file.stat().st_mtime
                    enc_mtime = enc_file.stat().st_mtime
                    if enc_mtime >= plain_mtime:
                        self._plain_hashes[plain_relative_path] = current_hash
                        self._save_cached_hashes()
                        return False
                except OSError:
                    pass

        # Ensure .sops.yaml exists if recipient is known
        if self._age_recipient:
            await self.ensure_sops_config(self._age_recipient)

        bin_path = await self.get_binary_path()
        enc_file.parent.mkdir(parents=True, exist_ok=True)

        # Tell SOPS explicitly how to parse the plaintext instead of letting it
        # guess from the input filename's extension, which it does not
        # recognise for things like .txt/.pem/.conf. Without this, such files
        # are silently treated as YAML/JSON on decrypt and come back mangled
        # (see decrypt_file()).
        content_type = self._sops_content_type(plain_relative_path)
        args = ["--encrypt", "--input-type", content_type]
        # If .sops.yaml exists in repo, do NOT pass --age so SOPS uses the creation_rules
        # in .sops.yaml (enabling multi-recipient & team sharing).
        # Only pass --age as a fallback if .sops.yaml is absent.
        sops_config_exists = (self._repo_path / ".sops.yaml").is_file()
        if not sops_config_exists:
            if self._age_recipient:
                args.extend(["--age", self._age_recipient])
            else:
                raise SopsConfigError(
                    "Cannot encrypt: No age recipient specified and no .sops.yaml found."
                )

        temp_enc = enc_file.parent / f".{enc_file.name}.{os.getpid()}.tmp"
        args.extend(["--output", str(temp_enc), str(plain_file)])

        try:
            await self._run_sops_cmd(bin_path, *args)
            if not temp_enc.is_file():
                raise SopsEncryptionError(f"SOPS did not produce output at {temp_enc}")

            # Atomically replace destination
            temp_enc.replace(enc_file)
            self._plain_hashes[plain_relative_path] = current_hash
            self._save_cached_hashes()
            _LOGGER.info("Encrypted %s -> %s", plain_relative_path, target_rel)
            return True
        except Exception as err:
            if temp_enc.exists():
                try:
                    temp_enc.unlink()
                except OSError:
                    pass
            raise SopsEncryptionError(
                f"Failed to encrypt '{plain_relative_path}': {self._redact_output(str(err))}"
            ) from err

    async def decrypt_file(
        self,
        enc_relative_path: str,
        plain_relative_path: str | None = None,
        validate_yaml: bool = True,
    ) -> bool:
        """Decrypt an encrypted secrets file to its plain counterpart.

        Validates YAML structure before atomically writing to target.
        Returns True if decrypted successfully.
        """
        enc_file = self._repo_path / enc_relative_path
        if not enc_file.is_file():
            return False

        if not self._age_key:
            raise SopsConfigError(
                "Cannot decrypt: No age secret key configured. "
                "Please configure your SOPS age secret key in integration options."
            )

        target_rel = plain_relative_path or self.get_plain_relative_path(enc_relative_path)
        plain_file = self._repo_path / target_rel

        # The envelope (the .enc.* file itself) is always YAML/JSON; the
        # content it wraps may not be. Passing --output-type explicitly makes
        # SOPS unwrap non-YAML/JSON secrets back to their original raw bytes
        # instead of printing the wrapped envelope structure as text (which
        # would then get written into the plain file verbatim).
        envelope_type = self._sops_envelope_type(enc_relative_path)
        content_type = self._sops_content_type(target_rel)
        is_binary_content = content_type == "binary"

        bin_path = await self.get_binary_path()
        plain_file.parent.mkdir(parents=True, exist_ok=True)

        decrypt_args = (
            "--decrypt",
            "--input-type", envelope_type,
            "--output-type", content_type,
            str(enc_file),
        )
        try:
            if is_binary_content:
                output_bytes = await self._run_sops_cmd_bytes(bin_path, *decrypt_args)
            else:
                output_text = await self._run_sops_cmd(bin_path, *decrypt_args)
                output_bytes = output_text.encode("utf-8")
        except Exception as err:
            raise SopsDecryptionError(
                f"Failed to decrypt '{enc_relative_path}': {self._redact_output(str(err))}"
            ) from err

        if validate_yaml and not is_binary_content:
            def _validate_content() -> None:
                if content_type == "json":
                    parsed = json.loads(output_bytes.decode("utf-8"))
                else:
                    parsed = yaml.safe_load(output_bytes.decode("utf-8"))
                if not isinstance(parsed, dict) and parsed is not None:
                    raise SopsDecryptionError(
                        f"Decrypted '{enc_relative_path}' is not a valid dictionary mapping."
                    )

            try:
                await asyncio.to_thread(_validate_content)
            except json.JSONDecodeError as err:
                # json's error text (e.g. "Expecting value: line 3 column 1
                # (char 12)") never embeds the source content, so it is safe
                # to include as-is.
                raise SopsDecryptionError(
                    f"Decrypted '{enc_relative_path}' contains invalid JSON syntax: {err}"
                ) from err
            except yaml.YAMLError as err:
                # PyYAML's default str(err) embeds a snippet of the offending
                # line via problem_mark.get_snippet() -- for a secrets file
                # that snippet is decrypted secret content, and this message
                # is logged and fired on EVENT_SOPS_ERROR. Report only the
                # location, never the parser's rendered snippet.
                mark = getattr(err, "problem_mark", None)
                location = (
                    f"line {mark.line + 1}, column {mark.column + 1}"
                    if mark is not None
                    else "unknown location"
                )
                problem = getattr(err, "problem", None) or "invalid YAML syntax"
                raise SopsDecryptionError(
                    f"Decrypted '{enc_relative_path}' contains invalid YAML syntax "
                    f"at {location}: {problem}"
                ) from err
            except UnicodeDecodeError as err:
                raise SopsDecryptionError(
                    f"Decrypted '{enc_relative_path}' is not valid UTF-8 text"
                ) from err

        temp_plain = plain_file.parent / f".{plain_file.name}.{os.getpid()}.tmp"
        try:
            await asyncio.to_thread(temp_plain.write_bytes, output_bytes)
            # Secure permissions: only owner can read secrets
            temp_plain.chmod(0o600)
            temp_plain.replace(plain_file)
            # Update hash cache and sync mtime with encrypted file
            self._plain_hashes[target_rel] = hashlib.sha256(output_bytes).hexdigest()
            self._save_cached_hashes()
            try:
                enc_mtime = enc_file.stat().st_mtime
                os.utime(plain_file, (enc_mtime, enc_mtime))
            except OSError:
                pass
            _LOGGER.info("Decrypted %s -> %s", enc_relative_path, target_rel)
            return True
        except Exception as err:
            if temp_plain.exists():
                try:
                    temp_plain.unlink()
                except OSError:
                    pass
            raise SopsDecryptionError(
                f"Failed to write decrypted file '{target_rel}': {err}"
            ) from err

    @staticmethod
    def _safe_glob(root: Path, pat: str) -> list[Path]:
        """Glob paths case-insensitively with backward-compatible fallback.

        Rejects any pattern containing a '..' path segment and drops any
        glob result that resolves outside ``root``, so a malicious or
        misconfigured pattern (e.g. from the encrypt/decrypt service call)
        cannot read or write files outside the repository.
        """
        cleaned = pat.lstrip("/")
        if any(part == ".." for part in cleaned.split("/")):
            _LOGGER.warning(
                "Rejected secrets file pattern with parent-directory traversal: %s",
                pat,
            )
            return []

        try:
            results = list(root.glob(cleaned, case_sensitive=False))
        except TypeError:
            # Python < 3.12 has no case_sensitive argument.
            regex = _glob_to_regex(cleaned)
            results = [
                p
                for p in root.rglob("*")
                if regex.match(p.relative_to(root).as_posix())
            ]

        root_resolved = root.resolve()
        safe_results = []
        for p in results:
            try:
                p.resolve().relative_to(root_resolved)
            except ValueError:
                continue
            safe_results.append(p)
        return safe_results

    def resolve_plain_secret_files(
        self, files: Sequence[str] | None = None
    ) -> list[str]:
        """Resolve configured files and glob patterns to existing relative filepaths."""
        patterns = files if files is not None else self._secrets_files
        found: set[str] = set()

        for pattern in patterns:
            pattern = pattern.strip()
            if not pattern or pattern.startswith("#"):
                continue

            paths = self._safe_glob(self._repo_path, pattern)
            for path in paths:
                if path.is_file():
                    try:
                        rel = str(path.relative_to(self._repo_path))
                    except ValueError:
                        continue
                    rel_lower = rel.lower()
                    if (
                        not rel.startswith((".git", ".storage"))
                        and not rel_lower.endswith((".enc.yaml", ".enc.yml", ".enc.json"))
                        and not rel_lower.endswith((".sops.yaml", ".sops.yml"))
                        and ".example." not in rel_lower
                        and ".sample." not in rel_lower
                        and ".template." not in rel_lower
                    ):
                        found.add(rel)

        return sorted(found)

    def _plain_path_for_pattern(self, enc_rel: str, pattern: str) -> str:
        """Map an encrypted sidecar back to its plain path using its pattern.

        ``get_encrypted_relative_path`` is not injective: ``k.pem`` and
        ``k.pem.yaml`` both become ``k.pem.enc.yaml``. Reversing the name
        without context guesses wrong for extensions it does not know, which
        would decrypt a secret to an unexpected filename that no ignore rule
        covers. The configured pattern that matched the sidecar tells us
        which original name is the intended one.
        """
        lower = enc_rel.lower()
        for suffix in (".enc.yaml", ".enc.yml", ".enc.json"):
            if lower.endswith(suffix):
                base = enc_rel[: -len(suffix)]
                regex = _glob_to_regex(pattern)
                for candidate in (base + suffix[len(".enc") :], base):
                    if (
                        self.get_encrypted_relative_path(candidate) == enc_rel
                        and regex.match(candidate)
                    ):
                        return candidate
                break
        return self.get_plain_relative_path(enc_rel)

    def resolve_encrypted_secret_files(
        self, files: Sequence[str] | None = None
    ) -> list[tuple[str, str]]:
        """Find encrypted files (.enc.yaml, .enc.json) matching configured secrets.

        Only patterns explicitly configured (via ``secrets_files``/options) or
        passed in ``files`` are considered. This intentionally does NOT
        discover arbitrary ``*.enc.*`` files anywhere in the repository:
        - it would decrypt third-party/unrelated ``.enc.*`` files (e.g. under
          ``www/`` or ``node_modules/``), causing unrelated pulls/restores to
          fail and roll back;
        - it would silently produce a plaintext file for any ``.enc.*`` sidecar
          a remote pushes, even one outside the user's configured secrets, which
          may not be covered by the .gitignore protection this integration adds.

        Returns a list of tuples: (enc_relative_path, plain_relative_path).
        """
        pairs: dict[str, str] = {}
        patterns = files if files is not None else self._secrets_files

        for pattern in patterns:
            pattern = pattern.strip()
            if not pattern or pattern.startswith("#"):
                continue
            enc_pattern = self.get_encrypted_relative_path(pattern)
            paths = self._safe_glob(self._repo_path, enc_pattern)
            for path in paths:
                if path.is_file():
                    try:
                        enc_rel = str(path.relative_to(self._repo_path))
                    except ValueError:
                        continue
                    if not enc_rel.startswith((".git", ".storage")):
                        pairs[enc_rel] = self._plain_path_for_pattern(
                            enc_rel, pattern
                        )

        return sorted(pairs.items())

    async def sync_secrets_to_encrypted(
        self,
        files: Sequence[str] | None = None,
        force: bool = False,
    ) -> list[str]:
        """Encrypt all existing plain secret files to .enc.yaml/.enc.json.

        Returns list of encrypted relative filepaths that were processed.
        Path.glob() is blocking I/O and must run in an executor thread.
        """
        # Ensure .gitignore does not ignore .enc.* files
        await self.ensure_sops_gitignore()

        plain_files = await asyncio.to_thread(self.resolve_plain_secret_files, files)
        processed: list[str] = []
        for rel_path in plain_files:
            enc_rel = self.get_encrypted_relative_path(rel_path)
            try:
                updated = await self.encrypt_file(rel_path, enc_rel, force=force)
                if updated:
                    processed.append(enc_rel)
            except SopsError as err:
                _LOGGER.error("Could not encrypt secret file '%s': %s", rel_path, err)
                raise
        return processed

    async def sync_encrypted_to_secrets(
        self,
        files: Sequence[str] | None = None,
    ) -> list[str]:
        """Decrypt all existing encrypted secret files to plain secrets.yaml.

        Returns list of plain relative filepaths that were restored.
        Path.glob() is blocking I/O and must run in an executor thread.
        """
        enc_pairs = await asyncio.to_thread(self.resolve_encrypted_secret_files, files)
        restored: list[str] = []
        for enc_rel, plain_rel in enc_pairs:
            try:
                decrypted = await self.decrypt_file(enc_rel, plain_rel)
                if decrypted:
                    restored.append(plain_rel)
            except SopsError as err:
                _LOGGER.error("Could not decrypt secret file '%s': %s", enc_rel, err)
                raise
        return restored

    def backup_plain_secrets(
        self, files: Sequence[str] | None = None
    ) -> dict[str, bytes | None]:
        """Snapshot current contents of all plain secret files for atomic rollback.

        Returns a dictionary mapping relative plain paths to their bytes, or None if the file did not exist.
        """
        enc_pairs = self.resolve_encrypted_secret_files(files)
        backup: dict[str, bytes | None] = {}
        for _, plain_rel in enc_pairs:
            plain_file = self._repo_path / plain_rel
            if plain_file.is_file():
                try:
                    backup[plain_rel] = plain_file.read_bytes()
                except OSError:
                    backup[plain_rel] = None
            else:
                backup[plain_rel] = None
        return backup

    def restore_plain_secrets_backup(
        self, backup: dict[str, bytes | None]
    ) -> None:
        """Restore plain secret files from a previous backup snapshot."""
        for plain_rel, content in backup.items():
            plain_file = self._repo_path / plain_rel
            try:
                if content is None:
                    if plain_file.is_file():
                        plain_file.unlink()
                else:
                    plain_file.parent.mkdir(parents=True, exist_ok=True)
                    temp_file = plain_file.parent / f".{plain_file.name}.rollback.{os.getpid()}"
                    fd = os.open(temp_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with open(fd, "wb") as f:
                        f.write(content)
                    temp_file.replace(plain_file)
            except OSError as err:
                _LOGGER.error(
                    "Failed to restore plain secret '%s' during rollback: %s",
                    plain_rel,
                    err,
                )
