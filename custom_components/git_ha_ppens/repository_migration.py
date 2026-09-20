"""Safe migration of an ESPHome Device Builder repository into its parent."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .git_manager import GitError, GitManager

_ESPHOME_PATH = "esphome"
_DEVICE_BUILDER_EMAIL = "device-builder@esphome.io"
_DEVICE_BUILDER_INITIAL_COMMIT = "Initialize version history"
_RECOVERY_DIRECTORY = "git-ha-ppens-recovery"

_ACTIVE_GIT_MARKERS = (
    "BISECT_LOG",
    "CHERRY_PICK_HEAD",
    "MERGE_HEAD",
    "REBASE_HEAD",
    "REVERT_HEAD",
    "rebase-apply",
    "rebase-merge",
    "sequencer",
)
_SENSITIVE_EXACT_PATHS = frozenset(
    {
        "esphome/.device-builder.json",
        "esphome/.device-builder-peer-link-key.bin",
        "esphome/.offloader_pairings.json",
        "esphome/.receiver_peers.json",
        "esphome/secrets.yaml",
    }
)
_SENSITIVE_KEY_SUFFIXES = (".key", ".p12", ".pem", ".pfx")
_SENSITIVE_KEY_NAMES = frozenset(
    {"id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"}
)
_REQUIRED_IGNORED_PATHS = (
    "esphome/secrets.yaml",
    "esphome/.esphome/",
    "esphome/.device-builder.json",
    "esphome/.device-builder-peer-link-key.bin",
    "esphome/.receiver_peers.json",
    "esphome/.offloader_pairings.json",
)
_SENSITIVE_IGNORE_PATTERNS = (
    "esphome/[sS][eE][cC][rR][eE][tT][sS].[yY][aA][mM][lL]",
    "esphome/.[dD][eE][vV][iI][cC][eE]-[bB][uU][iI][lL][dD][eE][rR].[jJ][sS][oO][nN]",
    "esphome/.[dD][eE][vV][iI][cC][eE]-[bB][uU][iI][lL][dD][eE][rR]-[pP][eE][eE][rR]-[lL][iI][nN][kK]-[kK][eE][yY].[bB][iI][nN]",
    "esphome/.[rR][eE][cC][eE][iI][vV][eE][rR]_[pP][eE][eE][rR][sS].[jJ][sS][oO][nN]",
    "esphome/.[oO][fF][fF][lL][oO][aA][dD][eE][rR]_[pP][aA][iI][rR][iI][nN][gG][sS].[jJ][sS][oO][nN]",
    "esphome/.[eE][sS][pP][hH][oO][mM][eE]/",
    "*.[kK][eE][yY]",
    "*.[pP]12",
    "*.[pP][eE][mM]",
    "*.[pP][fF][xX]",
    "[iI][dD]_[dD][sS][aA]",
    "[iI][dD]_[eE][cC][dD][sS][aA]",
    "[iI][dD]_[eE][dD]25519",
    "[iI][dD]_[rR][sS][aA]",
    "*[pP][aA][iI][rR][iI][nN][gG]*",
)


@dataclass(frozen=True)
class RepositoryMigrationAssessment:
    """Describe whether the ESPHome repository can be migrated automatically."""

    eligible: bool
    reason: str
    path: str = _ESPHOME_PATH
    inner_head: str = ""
    inner_dirty: bool = False
    managed_by_device_builder: bool = False
    inner_status: str = field(default="", repr=False)


@dataclass(frozen=True)
class RepositoryMigrationResult:
    """Describe a completed repository migration."""

    backup_path: str


class RepositoryMigrationError(GitError):
    """Raised when a repository migration cannot finish safely."""

    def __init__(self, reason: str) -> None:
        """Store a stable, non-sensitive failure reason."""
        self.reason = reason
        super().__init__(f"ESPHome repository migration failed: {reason}")


@dataclass(frozen=True)
class _IndexFingerprint:
    """Identify an outer Git index without relying on its pathname alone."""

    exists: bool
    device: int | None = None
    inode: int | None = None
    size: int | None = None
    modified_ns: int | None = None
    digest: str | None = None


def _path_kind(path: Path) -> str:
    """Return a path kind without following symbolic links."""
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if stat.S_ISLNK(path_stat.st_mode):
        return "symlink"
    if stat.S_ISDIR(path_stat.st_mode):
        return "directory"
    if stat.S_ISREG(path_stat.st_mode):
        return "file"
    return "other"


def _has_active_operation(git_dir: Path) -> bool:
    """Return whether Git operation state exists without following links."""
    return any(_path_kind(git_dir / marker) != "missing" for marker in _ACTIVE_GIT_MARKERS)


def _parse_index_entries(output: str) -> list[tuple[str, str, str]]:
    """Parse mode, stage, and path from ``git ls-files --stage -z``."""
    entries: list[tuple[str, str, str]] = []
    for record in output.split("\x00"):
        if not record:
            continue
        if "\t" not in record:
            raise RepositoryMigrationError("index_parse_failed")
        metadata, path = record.split("\t", 1)
        fields = metadata.split()
        if len(fields) != 3 or not path:
            raise RepositoryMigrationError("index_parse_failed")
        entries.append((fields[0], fields[2], path))
    return entries


def _outer_index_supported(entries: list[tuple[str, str, str]]) -> bool:
    """Return whether the outer index has a safely replaceable ESPHome shape."""
    if not entries:
        return True
    if entries == [("160000", "0", _ESPHOME_PATH)]:
        return True
    return all(
        stage == "0"
        and mode in {"100644", "100755"}
        and path.startswith(f"{_ESPHOME_PATH}/")
        for mode, stage, path in entries
    )


def _is_sensitive_path(path: str) -> bool:
    """Return whether a repository-relative path contains private state."""
    normalized = path.replace("\\", "/").casefold()
    name = normalized.rsplit("/", 1)[-1]
    return (
        normalized in _SENSITIVE_EXACT_PATHS
        or normalized.startswith("esphome/.esphome/")
        or name in _SENSITIVE_KEY_NAMES
        or name.endswith(_SENSITIVE_KEY_SUFFIXES)
        or "pairing" in name
    )


def _contains_sensitive_paths(entries: list[tuple[str, str, str]]) -> bool:
    """Return whether a prepared index contains private ESPHome state."""
    return any(_is_sensitive_path(path) for _mode, _stage, path in entries)


def _find_unsafe_worktree_paths(
    outer_path: Path, esphome_path: Path
) -> list[str]:
    """Find sensitive files, symlinks, and nested repositories without following."""
    found: set[str] = set()

    def _walk(directory: Path, *, root: bool = False) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError as err:
            raise RepositoryMigrationError("worktree_scan_failed") from err
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(outer_path).as_posix()
            if root and entry.name == ".git":
                continue
            if entry.name == ".git":
                found.add(directory.relative_to(outer_path).as_posix())
                continue
            if entry.is_symlink():
                found.add(relative)
                continue
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError as err:
                raise RepositoryMigrationError("worktree_scan_failed") from err
            if is_directory:
                _walk(path)
            elif _is_sensitive_path(relative):
                found.add(relative)

    _walk(esphome_path, root=True)
    return sorted(found)


async def _assert_worktree_paths_safe(
    git_manager: GitManager,
    outer_path: Path,
    esphome_path: Path,
) -> None:
    """Reject unsafe working-tree paths unless the outer repository ignores them."""
    paths = await asyncio.to_thread(
        _find_unsafe_worktree_paths, outer_path, esphome_path
    )
    for path in paths:
        if not await git_manager._is_path_ignored(path):
            raise RepositoryMigrationError("worktree_contains_unsafe_paths")


async def _assert_required_sensitive_ignores(
    git_manager: GitManager,
) -> None:
    """Require durable outer ignore rules for known ESPHome private state."""
    for path in _REQUIRED_IGNORED_PATHS:
        if not await git_manager._is_path_ignored(path):
            raise RepositoryMigrationError("sensitive_paths_not_ignored")


def _index_fingerprint(index_path: Path) -> _IndexFingerprint:
    """Return a content and identity fingerprint for a regular Git index."""
    try:
        index_stat = index_path.lstat()
    except FileNotFoundError:
        return _IndexFingerprint(exists=False)
    except OSError as err:
        raise RepositoryMigrationError("outer_index_unreadable") from err
    if not stat.S_ISREG(index_stat.st_mode) or stat.S_ISLNK(index_stat.st_mode):
        raise RepositoryMigrationError("outer_index_not_regular")
    try:
        digest = hashlib.sha256(index_path.read_bytes()).hexdigest()
    except OSError as err:
        raise RepositoryMigrationError("outer_index_unreadable") from err
    return _IndexFingerprint(
        exists=True,
        device=index_stat.st_dev,
        inode=index_stat.st_ino,
        size=index_stat.st_size,
        modified_ns=index_stat.st_mtime_ns,
        digest=digest,
    )


def _create_recovery_directory(git_dir: Path) -> Path:
    """Create a private, unique recovery directory below the outer Git dir."""
    recovery_root = git_dir / _RECOVERY_DIRECTORY
    recovery_kind = _path_kind(recovery_root)
    if recovery_kind == "missing":
        recovery_root.mkdir(mode=0o700)
    elif recovery_kind != "directory":
        raise RepositoryMigrationError("recovery_directory_unsafe")
    os.chmod(recovery_root, 0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for _attempt in range(10):
        recovery_path = recovery_root / (
            f"esphome-{timestamp}-{uuid.uuid4().hex[:8]}"
        )
        try:
            recovery_path.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return recovery_path
    raise RepositoryMigrationError("recovery_directory_unavailable")


def _write_manifest(
    recovery_path: Path,
    assessment: RepositoryMigrationAssessment,
    index_fingerprint: _IndexFingerprint,
) -> None:
    """Write non-content migration metadata beside the recovery artifacts."""
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "inner_head": assessment.inner_head,
        "inner_dirty": assessment.inner_dirty,
        "inner_status": assessment.inner_status,
        "managed_by_device_builder": assessment.managed_by_device_builder,
        "outer_index_existed": index_fingerprint.exists,
        "outer_index_sha256": index_fingerprint.digest,
    }
    (recovery_path / "migration.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _publish_index(
    index_path: Path,
    prepared_index: Path,
    expected: _IndexFingerprint,
) -> None:
    """Publish a prepared index through Git's lock protocol."""
    if _index_fingerprint(index_path) != expected:
        raise RepositoryMigrationError("outer_index_changed")

    lock_path = index_path.with_name(f"{index_path.name}.lock")
    try:
        lock_fd = os.open(
            lock_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as err:
        raise RepositoryMigrationError("outer_index_lock_present") from err
    except OSError as err:
        raise RepositoryMigrationError("outer_index_lock_unavailable") from err

    published = False
    try:
        if _index_fingerprint(index_path) != expected:
            raise RepositoryMigrationError("outer_index_changed")
        prepared_bytes = prepared_index.read_bytes()
        offset = 0
        while offset < len(prepared_bytes):
            written = os.write(lock_fd, prepared_bytes[offset:])
            if written <= 0:
                raise RepositoryMigrationError("outer_index_publish_failed")
            offset += written
        os.fsync(lock_fd)
        os.close(lock_fd)
        lock_fd = -1
        if expected.exists:
            os.chmod(lock_path, stat.S_IMODE(index_path.lstat().st_mode))
        os.replace(lock_path, index_path)
        published = True
        try:
            directory_fd = os.open(index_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # The rename is already complete; a directory fsync failure must
            # not be reported as a pre-publication failure and trigger a
            # misleading metadata rollback.
            pass
    except OSError as err:
        raise RepositoryMigrationError("outer_index_publish_failed") from err
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        if not published:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


async def _git_directory(git_manager: GitManager) -> Path:
    """Return the resolved Git metadata directory."""
    output = await git_manager._run_git("rev-parse", "--absolute-git-dir")
    return await asyncio.to_thread(Path(output).resolve)


async def _device_builder_fingerprint(inner: GitManager) -> bool:
    """Return whether commit history strictly matches Device Builder."""
    managed = await inner._run_git(
        "config",
        "--local",
        "--bool",
        "--get",
        "device-builder.managed",
        check=False,
    )
    if managed.strip().casefold() == "true":
        return True

    head = await inner.get_head_sha()
    if not head:
        return False
    roots = [
        line
        for line in (
            await inner._run_git("rev-list", "--max-parents=0", "HEAD")
        ).splitlines()
        if line
    ]
    if len(roots) != 1:
        return False
    root_email = await inner._run_git(
        "show", "-s", "--format=%ae", roots[0]
    )
    root_subject = await inner._run_git(
        "show", "-s", "--format=%s", roots[0]
    )
    if (
        root_email.casefold() != _DEVICE_BUILDER_EMAIL
        or root_subject != _DEVICE_BUILDER_INITIAL_COMMIT
    ):
        return False
    authors = await inner._run_git("log", "--format=%ae", "HEAD")
    return bool(authors) and all(
        author.casefold() == _DEVICE_BUILDER_EMAIL
        for author in authors.splitlines()
        if author
    )


async def async_assess_esphome_repository_migration(
    git_manager: GitManager,
    unsafe_paths: list[str] | None = None,
) -> RepositoryMigrationAssessment:
    """Assess a nested ESPHome repository without changing either repository."""
    try:
        paths = (
            unsafe_paths
            if unsafe_paths is not None
            else await git_manager.get_unsafe_repository_paths()
        )
        if paths != [_ESPHOME_PATH]:
            reason = "multiple_or_non_esphome_paths" if paths else "layout_safe"
            return RepositoryMigrationAssessment(False, reason)

        outer_path = await asyncio.to_thread(
            Path(git_manager.repo_path).resolve
        )
        esphome_path = outer_path / _ESPHOME_PATH
        marker = esphome_path / ".git"
        marker_kind = await asyncio.to_thread(_path_kind, marker)
        if marker_kind != "directory":
            return RepositoryMigrationAssessment(
                False,
                "marker_symlink" if marker_kind == "symlink" else "marker_not_directory",
            )

        inner = GitManager(str(esphome_path))
        if not await inner.is_repo_initialized():
            return RepositoryMigrationAssessment(False, "inner_not_repository")
        inner_top = await inner._run_git("rev-parse", "--show-toplevel")
        resolved_inner_top = await asyncio.to_thread(Path(inner_top).resolve)
        if resolved_inner_top != esphome_path:
            return RepositoryMigrationAssessment(False, "inner_root_mismatch")

        if await git_manager._is_path_ignored(_ESPHOME_PATH):
            return RepositoryMigrationAssessment(False, "outer_path_ignored")
        await _assert_required_sensitive_ignores(git_manager)
        await _assert_worktree_paths_safe(
            git_manager, outer_path, esphome_path
        )
        if await inner._run_git("remote", check=False):
            return RepositoryMigrationAssessment(False, "inner_remote_configured")
        if (
            await asyncio.to_thread(
                _path_kind, esphome_path / ".gitmodules"
            )
            != "missing"
        ):
            return RepositoryMigrationAssessment(False, "inner_submodules")
        if await inner._get_tracked_gitlink_paths():
            return RepositoryMigrationAssessment(False, "inner_submodules")

        worktrees = await inner._run_git("worktree", "list", "--porcelain")
        worktree_paths = [
            line.removeprefix("worktree ")
            for line in worktrees.splitlines()
            if line.startswith("worktree ")
        ]
        if len(worktree_paths) != 1:
            return RepositoryMigrationAssessment(False, "additional_worktrees")
        resolved_worktree = await asyncio.to_thread(
            Path(worktree_paths[0]).resolve
        )
        if resolved_worktree != esphome_path:
            return RepositoryMigrationAssessment(False, "additional_worktrees")

        outer_git_dir = await _git_directory(git_manager)
        if await asyncio.to_thread(_has_active_operation, marker):
            return RepositoryMigrationAssessment(False, "inner_operation_active")
        if await asyncio.to_thread(_has_active_operation, outer_git_dir):
            return RepositoryMigrationAssessment(False, "outer_operation_active")
        if (
            await asyncio.to_thread(_path_kind, marker / "index.lock")
            != "missing"
        ):
            return RepositoryMigrationAssessment(False, "inner_index_lock_present")
        if (
            await asyncio.to_thread(
                _path_kind, outer_git_dir / "index.lock"
            )
            != "missing"
        ):
            return RepositoryMigrationAssessment(False, "outer_index_lock_present")

        outer_entries = _parse_index_entries(
            await git_manager._run_git(
                "ls-files", "--stage", "-z", "--", _ESPHOME_PATH
            )
        )
        if not _outer_index_supported(outer_entries):
            return RepositoryMigrationAssessment(False, "unsupported_outer_index")

        marker_stat = await asyncio.to_thread(marker.lstat)
        git_dir_stat = await asyncio.to_thread(outer_git_dir.stat)
        if marker_stat.st_dev != git_dir_stat.st_dev:
            return RepositoryMigrationAssessment(False, "different_filesystems")

        managed = await _device_builder_fingerprint(inner)
        if not managed:
            return RepositoryMigrationAssessment(False, "not_device_builder_managed")

        inner_head = await inner.get_head_sha()
        inner_status = await inner._run_git(
            "status", "--porcelain=v1", "-z", check=False
        )
        return RepositoryMigrationAssessment(
            True,
            "eligible",
            inner_head=inner_head,
            inner_dirty=bool(inner_status),
            managed_by_device_builder=True,
            inner_status=inner_status,
        )
    except (GitError, OSError, RuntimeError) as err:
        if isinstance(err, RepositoryMigrationError):
            reason = err.reason
        else:
            reason = "assessment_failed"
        return RepositoryMigrationAssessment(False, reason)


async def async_migrate_esphome_repository(
    git_manager: GitManager,
) -> RepositoryMigrationResult:
    """Move ESPHome Git metadata aside and atomically stage its real files."""
    assessment = await async_assess_esphome_repository_migration(git_manager)
    if not assessment.eligible:
        raise RepositoryMigrationError(assessment.reason)

    try:
        outer_path = await asyncio.to_thread(
            Path(git_manager.repo_path).resolve
        )
        marker = outer_path / _ESPHOME_PATH / ".git"
        outer_git_dir = await _git_directory(git_manager)
        index_path = outer_git_dir / "index"
        expected_index = await asyncio.to_thread(
            _index_fingerprint, index_path
        )
        recovery_path = await asyncio.to_thread(
            _create_recovery_directory, outer_git_dir
        )
    except RepositoryMigrationError:
        raise
    except (GitError, OSError, RuntimeError) as err:
        raise RepositoryMigrationError("migration_failed") from err
    backup_git = recovery_path / "dot-git"
    backup_index = recovery_path / "outer-index.before"
    prepared_index = recovery_path / "outer-index.prepared"
    safety_excludes = recovery_path / "safety-excludes"
    marker_moved = False
    index_published = False

    try:
        await asyncio.to_thread(
            _write_manifest, recovery_path, assessment, expected_index
        )
        await asyncio.to_thread(
            safety_excludes.write_text,
            "\n".join(_SENSITIVE_IGNORE_PATTERNS) + "\n",
            encoding="utf-8",
        )
        if await asyncio.to_thread(_path_kind, marker) != "directory":
            raise RepositoryMigrationError("inner_repository_changed")
        await asyncio.to_thread(os.replace, marker, backup_git)
        marker_moved = True

        if expected_index.exists:
            await asyncio.to_thread(shutil.copy2, index_path, backup_index)
            await asyncio.to_thread(shutil.copy2, index_path, prepared_index)

        index_env = {
            "GIT_INDEX_FILE": str(prepared_index),
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": str(outer_path),
            "GIT_CONFIG_KEY_1": "core.excludesFile",
            "GIT_CONFIG_VALUE_1": str(safety_excludes),
        }
        if not expected_index.exists:
            if await git_manager.has_commits():
                await git_manager._run_git(
                    "read-tree", "HEAD", extra_env=index_env
                )
            else:
                await git_manager._run_git(
                    "read-tree", "--empty", extra_env=index_env
                )

        await git_manager._run_git(
            "rm",
            "-r",
            "--cached",
            "--force",
            "--ignore-unmatch",
            "--",
            _ESPHOME_PATH,
            extra_env=index_env,
        )
        await git_manager._run_git(
            "add", "-A", "--", _ESPHOME_PATH, extra_env=index_env
        )
        prepared_entries = _parse_index_entries(
            await git_manager._run_git(
                "ls-files",
                "--stage",
                "-z",
                "--",
                _ESPHOME_PATH,
                extra_env=index_env,
            )
        )
        if not prepared_entries:
            raise RepositoryMigrationError("prepared_index_empty")
        if any(mode == "160000" for mode, _stage, _path in prepared_entries):
            raise RepositoryMigrationError("prepared_index_contains_gitlink")
        if any(
            stage != "0" or mode not in {"100644", "100755"}
            for mode, stage, _path in prepared_entries
        ):
            raise RepositoryMigrationError("prepared_index_has_unsafe_mode")
        if any(
            ".git" in path.replace("\\", "/").split("/")
            for _mode, _stage, path in prepared_entries
        ):
            raise RepositoryMigrationError("prepared_index_contains_git_metadata")
        if _contains_sensitive_paths(prepared_entries):
            raise RepositoryMigrationError("prepared_index_contains_sensitive_paths")

        await _assert_worktree_paths_safe(
            git_manager, outer_path, outer_path / _ESPHOME_PATH
        )
        await _assert_required_sensitive_ignores(git_manager)
        if await asyncio.to_thread(_path_kind, marker) != "missing":
            raise RepositoryMigrationError("inner_repository_recreated")
        await asyncio.to_thread(
            _publish_index,
            index_path,
            prepared_index,
            expected_index,
        )
        index_published = True
        return RepositoryMigrationResult(backup_path=str(recovery_path))
    except RepositoryMigrationError:
        raise
    except (GitError, OSError) as err:
        raise RepositoryMigrationError("migration_failed") from err
    finally:
        if marker_moved and not index_published:
            try:
                if await asyncio.to_thread(_path_kind, marker) != "missing":
                    raise RepositoryMigrationError("inner_repository_recreated")
                await asyncio.to_thread(os.replace, backup_git, marker)
            except (OSError, RepositoryMigrationError) as rollback_err:
                raise RepositoryMigrationError("rollback_failed") from rollback_err
