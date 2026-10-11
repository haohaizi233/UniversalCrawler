"""Atomic updater text writes with primary-exception-safe cleanup."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import BinaryIO

from app.services.updater_cleanup import run_cleanup_actions
from app.services.updater_path_safety import (
    approved_directory_lease,
    ensure_approved_directory,
    ensure_regular_or_missing,
    read_bounded_regular_bytes,
    replace_regular_file,
)
from shared.filesystem_directory_capability import FilesystemDirectoryCapability


def atomic_write_text(path: Path, content: str) -> None:
    if type(content) is not str:
        raise TypeError("atomic update text content must be text")
    _atomic_write_payload(Path(path), content.encode("utf-8"))


def atomic_write_bytes(path: Path, content: bytes) -> None:
    if type(content) is not bytes:
        raise TypeError("atomic update bytes content must be bytes")
    _atomic_write_payload(Path(path), content)


def _atomic_write_payload(path: Path, content: bytes) -> None:
    target = Path(path)
    root = ensure_approved_directory(target.parent, create=True)
    with approved_directory_lease(root) as capability:
        target = ensure_regular_or_missing(target, approved_root=root)
        temp_name, descriptor = _create_temporary_file(capability, target.name)
        handle: BinaryIO | None = None
        temp_path = root / temp_name
        primary_error: BaseException | None = None
        try:
            handle = os.fdopen(descriptor, "wb", buffering=0)
            descriptor = -1
            written = handle.write(content)
            if written != len(content):
                raise OSError("atomic update write was incomplete")
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            replace_regular_file(temp_path, target, approved_root=root)
            published = read_bounded_regular_bytes(
                target,
                approved_root=root,
                maximum=len(content),
            )
            if published != content:
                raise OSError("atomic update target changed during publish")
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            actions = []
            if descriptor >= 0:
                actions.append(
                    (
                        lambda: os.close(descriptor),
                        "atomic update descriptor cleanup failed",
                    )
                )
            if handle is not None:
                actions.append((handle.close, "atomic update handle cleanup failed"))
            run_cleanup_actions(primary_error, actions)


def _create_temporary_file(
    capability: FilesystemDirectoryCapability,
    target_name: str,
) -> tuple[str, int]:
    for _attempt in range(64):
        name = f".{target_name}.{secrets.token_hex(16)}.tmp"
        try:
            descriptor = capability.open_child(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            continue
        return name, descriptor
    raise OSError("could not allocate atomic update temporary file")
