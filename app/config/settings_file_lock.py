"""OS advisory locking used to serialize persistent configuration writes."""

from __future__ import annotations

import errno
import os
import time as time_module
from pathlib import Path

from app.exceptions import ConfigWriteError


def _attach_cleanup_note(primary_error: BaseException, message: str) -> None:
    try:
        primary_error.add_note(message)
    except BaseException:
        pass


def _advisory_lock_is_contended(exc: OSError) -> bool:
    return exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(
        exc,
        "winerror",
        None,
    ) in {32, 33, 36}


def _try_advisory_file_lock(descriptor: int) -> bool:
    os.lseek(descriptor, 0, os.SEEK_SET)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if _advisory_lock_is_contended(exc):
            return False
        raise
    return True


def _unlock_advisory_file(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def release_file_lock_descriptor(
    descriptor: int,
    *,
    acquired: bool,
    primary_error: BaseException | None = None,
) -> None:
    cleanup_error: BaseException | None = None
    if acquired:
        try:
            _unlock_advisory_file(descriptor)
        except BaseException as exc:
            cleanup_error = exc

    try:
        os.close(descriptor)
    except BaseException as exc:
        if cleanup_error is None:
            cleanup_error = exc
        else:
            _attach_cleanup_note(cleanup_error, f"lock descriptor close failed: {exc!r}")

    if cleanup_error is None:
        return
    if primary_error is not None:
        _attach_cleanup_note(primary_error, f"lock cleanup failed: {cleanup_error!r}")
        return
    raise cleanup_error


def acquire_file_lock_descriptor(path: Path, *, timeout_seconds: float) -> int:
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    descriptor: int | None = None
    acquired = False
    try:
        descriptor = os.open(path, flags, 0o600)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)

        deadline = time_module.monotonic() + timeout_seconds
        while not acquired:
            acquired = _try_advisory_file_lock(descriptor)
            if acquired:
                break
            if time_module.monotonic() >= deadline:
                raise ConfigWriteError(f"timed out waiting for config lock: {path}")
            time_module.sleep(0.02)

        metadata = f"{os.getpid()} {time_module.time()}\n".encode("ascii")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, metadata)
        os.ftruncate(descriptor, len(metadata))
        os.fsync(descriptor)
        return descriptor
    except BaseException as exc:
        if isinstance(exc, ConfigWriteError):
            outgoing_error: BaseException = exc
        elif isinstance(exc, OSError):
            outgoing_error = ConfigWriteError(str(exc))
        else:
            outgoing_error = exc
        if descriptor is not None:
            release_file_lock_descriptor(
                descriptor,
                acquired=acquired,
                primary_error=outgoing_error,
            )
        if outgoing_error is exc:
            raise
        raise outgoing_error from exc
