"""Small cross-process lock used for updater metadata transactions."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

from app.services.updater_cleanup import run_cleanup_actions
from app.services.updater_path_safety import (
    approved_directory_lease,
    ensure_approved_directory,
    open_regular_binary,
)


@contextmanager
def exclusive_file_lock(
    path: Path,
    *,
    timeout_seconds: float = 10.0,
    approved_root: Path | None = None,
    cancel_event: object | None = None,
    error_factory: Callable[[str], BaseException] = TimeoutError,
) -> Iterator[None]:
    lock_path = Path(path)
    lock_root = Path(approved_root or lock_path.parent)
    lock_root = _prepare_lock_root(lock_root)
    _require_regular_directory(lock_root)
    with approved_directory_lease(lock_root):
        handle = _open_regular_lock_file(
            lock_path,
            approved_root=lock_root,
        )
        acquired = False
        primary_error: BaseException | None = None
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + max(0.1, float(timeout_seconds))
            while not acquired:
                if cancel_event is not None and bool(cancel_event.is_set()):
                    raise error_factory("updater file lock wait was cancelled")
                try:
                    _try_lock(handle)
                    acquired = True
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise error_factory("updater file lock timed out")
                    time.sleep(0.025)
            yield
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            def unlock() -> None:
                if acquired:
                    _unlock(handle)

            run_cleanup_actions(
                primary_error,
                (
                    (unlock, "updater metadata lock release failed"),
                    (handle.close, "updater metadata lock handle cleanup failed"),
                ),
            )


def _try_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _open_regular_lock_file(path: Path, *, approved_root: Path) -> BinaryIO:
    return open_regular_binary(
        path,
        approved_root=approved_root,
        mode="a+b",
    )


def _prepare_lock_root(path: Path) -> Path:
    try:
        return ensure_approved_directory(path, create=True)
    except OSError as exc:
        raise OSError("updater lock root is unsafe") from exc


def _require_regular_directory(path: Path) -> None:
    ensure_approved_directory(path)
