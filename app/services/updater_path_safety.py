"""No-follow filesystem primitives for updater-owned cache directories."""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from app.services.updater_cleanup import run_cleanup_actions, run_typed_cleanup_actions
from shared.filesystem_directory_capability import (
    FilesystemDirectoryCapability,
    ensure_regular_directory,
    filesystem_child_directory_capability,
    filesystem_directory_capability,
    is_link_or_reparse as _shared_is_link_or_reparse,
)


def ensure_approved_directory(path: Path, *, create: bool = False) -> Path:
    """Validate every directory component before optionally creating missing ones."""

    return ensure_regular_directory(path, create=create)


@contextmanager
def approved_directory_lease(
    path: Path,
) -> Iterator[FilesystemDirectoryCapability]:
    """Hold one approved directory identity for an entire updater operation."""

    root = Path(os.path.abspath(os.fspath(path)))
    _require_regular_directory(root)
    with filesystem_directory_capability(root) as capability:
        yield capability


@contextmanager
def temporary_approved_directory(
    parent: Path,
    *,
    prefix: str,
) -> Iterator[Path]:
    """Create and hold one private temporary directory under a live parent authority.

    The randomly named directory is intentionally retained after the lease closes.
    Removing it by parent/name cannot atomically prove that the name still refers to
    the generation created here, so best-effort cleanup could delete a successor.
    """

    root = ensure_approved_directory(parent)
    with approved_directory_lease(root) as parent_capability:
        name = ""
        for _attempt in range(64):
            candidate_name = f"{prefix}{secrets.token_hex(16)}"
            try:
                parent_capability.mkdir_child(candidate_name)
            except FileExistsError:
                continue
            name = candidate_name
            break
        if not name:
            raise OSError("could not allocate updater temporary directory")
        with filesystem_child_directory_capability(
            parent_capability,
            name,
        ):
            yield root / name


def ensure_regular_or_missing(path: Path, *, approved_root: Path) -> Path:
    """Return one direct approved child, rejecting links and special files."""

    root, candidate = _approved_child(path, approved_root=approved_root)
    with approved_directory_lease(root) as capability:
        value = _regular_or_missing(capability, candidate.name)
        if value is None:
            return candidate
    return candidate


def require_regular_file(path: Path, *, approved_root: Path) -> Path:
    root, candidate = _approved_child(path, approved_root=approved_root)
    with approved_directory_lease(root) as capability:
        value = _regular_or_missing(capability, candidate.name)
        if value is None:
            raise OSError("updater regular file is missing")
    return candidate


def open_regular_binary(
    path: Path,
    *,
    approved_root: Path,
    mode: str,
) -> BinaryIO:
    """Open one approved regular file without following a link during races."""

    if mode not in {"rb", "wb", "ab", "a+b"}:
        raise ValueError("unsupported updater file mode")
    root, candidate = _approved_child(path, approved_root=approved_root)
    with approved_directory_lease(root) as capability:
        before = _regular_or_missing(capability, candidate.name)
        if before is None and mode == "rb":
            raise FileNotFoundError(candidate)

        if mode == "rb":
            flags = os.O_RDONLY
        elif mode == "a+b":
            flags = os.O_RDWR
        else:
            flags = os.O_WRONLY
        if mode in {"ab", "a+b"}:
            flags |= os.O_APPEND
        if before is None:
            flags |= os.O_CREAT | os.O_EXCL
        try:
            descriptor = capability.open_child(candidate.name, flags, 0o600)
        except FileExistsError:
            if before is not None or mode not in {"ab", "a+b"}:
                raise
            before = _regular_or_missing(capability, candidate.name)
            if before is None:
                raise OSError("updater append target changed during creation")
            descriptor = capability.open_child(
                candidate.name,
                flags & ~(os.O_CREAT | os.O_EXCL),
                0o600,
            )
        primary_error: BaseException | None = None
        try:
            opened = os.fstat(descriptor)
            after = capability.stat_child(candidate.name)
            opened_identity = (opened.st_dev, opened.st_ino)
            if (
                not _safe_regular_file(opened)
                or not _safe_regular_file(after)
                or opened_identity != (after.st_dev, after.st_ino)
                or (
                    before is not None
                    and opened_identity != (before.st_dev, before.st_ino)
                )
            ):
                raise OSError("updater file changed during open")
            if mode == "wb":
                os.ftruncate(descriptor, 0)
            stream = os.fdopen(descriptor, mode, buffering=0)
            descriptor = -1
            return stream
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if descriptor >= 0:
                run_cleanup_actions(
                    primary_error,
                    ((lambda: os.close(descriptor), "updater descriptor cleanup failed"),),
                )


def read_bounded_regular_bytes(
    path: Path,
    *,
    approved_root: Path,
    maximum: int,
) -> bytes:
    """Read at most ``maximum + 1`` bytes from one approved regular file."""

    handle: BinaryIO | None = None
    primary_error: BaseException | None = None
    try:
        handle = open_regular_binary(path, approved_root=approved_root, mode="rb")
        return handle.read(maximum + 1)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if handle is not None:
            run_typed_cleanup_actions(
                primary_error,
                ((handle.close, "bounded updater file cleanup failed"),),
                error_factory=OSError,
                failure_message="bounded updater file cleanup failed",
            )


def replace_regular_file(source: Path, target: Path, *, approved_root: Path) -> None:
    """Replace with the source path generation selected by the OS rename.

    Callers that trusted an earlier source generation must validate ``target``
    after this path-linearizable operation and before consuming it.
    """

    root, source_path = _approved_child(source, approved_root=approved_root)
    target_root, target_path = _approved_child(target, approved_root=approved_root)
    if os.path.normcase(os.fspath(root)) != os.path.normcase(os.fspath(target_root)):
        raise OSError("updater replace paths must share one approved root")
    with approved_directory_lease(root) as capability:
        if _regular_or_missing(capability, source_path.name) is None:
            raise FileNotFoundError(source_path)
        _regular_or_missing(capability, target_path.name)
        capability.replace_child(source_path.name, capability, target_path.name)


def replace_between_approved_roots(
    source: Path,
    target: Path,
    *,
    source_root: Path,
    target_root: Path,
) -> None:
    """Replace with the source path generation selected by the OS rename.

    The destination is authoritative; verified-source callers must revalidate
    it before returning or consuming the result.
    """

    source_approved, source_path = _approved_child(
        source,
        approved_root=source_root,
    )
    target_approved, target_path = _approved_child(
        target,
        approved_root=target_root,
    )
    with approved_directory_lease(source_approved) as source_capability:
        with approved_directory_lease(target_approved) as target_capability:
            if _regular_or_missing(source_capability, source_path.name) is None:
                raise FileNotFoundError(source_path)
            _regular_or_missing(target_capability, target_path.name)
            source_capability.replace_child(
                source_path.name,
                target_capability,
                target_path.name,
            )


def is_link_or_reparse(value: os.stat_result) -> bool:
    return _shared_is_link_or_reparse(value)


def _approved_child(path: Path, *, approved_root: Path) -> tuple[Path, Path]:
    root = Path(os.path.abspath(os.fspath(approved_root)))
    candidate = Path(os.path.abspath(os.fspath(path)))
    if os.path.normcase(os.fspath(candidate.parent)) != os.path.normcase(os.fspath(root)):
        raise OSError("updater path escapes its approved root")
    return root, candidate


def _require_regular_directory(path: Path) -> None:
    ensure_regular_directory(path)


def _regular_or_missing(
    capability: FilesystemDirectoryCapability,
    name: str,
) -> os.stat_result | None:
    try:
        value = capability.stat_child(name)
    except FileNotFoundError:
        return None
    if not _safe_regular_file(value):
        raise OSError("updater path is not a regular file")
    return value


def _safe_regular_file(value: os.stat_result) -> bool:
    return (
        stat.S_ISREG(value.st_mode)
        and value.st_nlink == 1
        and not is_link_or_reparse(value)
    )


def _stat_identity(value: os.stat_result) -> tuple[int, int]:
    return int(value.st_dev), int(value.st_ino)
