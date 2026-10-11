"""Windows file lease binding updater verification to process creation."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from app.services.updater_cleanup import run_typed_cleanup_actions
from app.services.updater_path_safety import (
    approved_directory_lease,
    require_regular_file,
)


@contextmanager
def verified_installer_lease(
    path: Path,
    *,
    approved_root: Path,
    error_factory: Callable[[str], BaseException],
    cleanup_error_callback: Callable[[BaseException], object] | None = None,
    bind_descriptor: Callable[[int], None] | None = None,
) -> Iterator[Path]:
    """Deny writes and deletes while this lease handle remains open.

    ``launch_verified_installer`` inherits the handle into the installer
    process. That inherited handle keeps the sharing mode until the
    installer exits, even after this context closes the parent descriptor.
    """

    with verified_file_lease(
        path,
        approved_root=approved_root,
        error_factory=error_factory,
        cleanup_error_callback=cleanup_error_callback,
        target_label="installer",
        bind_descriptor=bind_descriptor,
    ) as candidate:
        yield candidate


@contextmanager
def verified_file_lease(
    path: Path,
    *,
    approved_root: Path,
    error_factory: Callable[[str], BaseException],
    cleanup_error_callback: Callable[[BaseException], object] | None = None,
    target_label: str = "executable",
    bind_descriptor: Callable[[int], None] | None = None,
) -> Iterator[Path]:
    """Bind one verified Windows file and its approved directory."""

    root = Path(os.path.abspath(os.fspath(approved_root)))
    candidate = Path(os.path.abspath(os.fspath(path)))
    if os.path.normcase(os.fspath(candidate.parent)) != os.path.normcase(
        os.fspath(root)
    ):
        raise error_factory(f"{target_label} lease target is outside its approved root")
    body_succeeded = False
    try:
        with approved_directory_lease(root) as directory_capability:
            try:
                expected = directory_capability.stat_child(candidate.name)
                if not _safe_regular_file(expected):
                    raise OSError("lease target is not a safe regular file")
                candidate = require_regular_file(candidate, approved_root=root)
            except OSError as exc:
                raise error_factory(
                    f"{target_label} lease target is missing or unsafe"
                ) from exc
            if os.name != "nt":
                raise error_factory("verified file leases require Windows")
            descriptor = _open_windows_read_lease(
                candidate,
                expected_identity=(int(expected.st_dev), int(expected.st_ino)),
                error_factory=error_factory,
                target_label=target_label,
            )
            yield from _yield_leased_file(
                candidate,
                descriptor=descriptor,
                error_factory=error_factory,
                cleanup_error_callback=cleanup_error_callback,
                target_label=target_label,
                bind_descriptor=bind_descriptor,
            )
            body_succeeded = True
    except BaseException as exc:
        if not body_succeeded:
            raise
        _notify_cleanup_error(cleanup_error_callback, exc)


def _yield_leased_file(
    candidate: Path,
    *,
    descriptor: int,
    error_factory: Callable[[str], BaseException],
    cleanup_error_callback: Callable[[BaseException], object] | None,
    target_label: str,
    bind_descriptor: Callable[[int], None] | None,
) -> Iterator[Path]:
    primary_error: BaseException | None = None
    try:
        if bind_descriptor is not None:
            bind_descriptor(descriptor)
        yield candidate
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if primary_error is not None:
            run_typed_cleanup_actions(
                primary_error,
                (
                    (
                        lambda: os.close(descriptor),
                        f"verified {target_label} lease cleanup failed",
                    ),
                ),
                error_factory=error_factory,
                failure_message=f"verified {target_label} lease cleanup failed",
            )
        else:
            try:
                os.close(descriptor)
            except BaseException as cleanup_error:
                _notify_cleanup_error(cleanup_error_callback, cleanup_error)


def _notify_cleanup_error(
    callback: Callable[[BaseException], object] | None,
    error: BaseException,
) -> None:
    if callback is None:
        return
    try:
        callback(error)
    except BaseException:
        pass


def _open_windows_read_lease(
    path: Path,
    *,
    expected_identity: tuple[int, int],
    error_factory: Callable[[str], BaseException],
    target_label: str,
) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    before = os.lstat(path)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or _has_reparse_attribute(before)
    ):
        raise error_factory("installer lease target is not a regular file")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = create_file(
        os.fspath(path),
        0x80000000,  # GENERIC_READ
        0x00000001,  # FILE_SHARE_READ only: deny WRITE and DELETE sharing
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in {None, invalid_handle}:
        raise error_factory(
            f"installer lease could not be acquired (winerror={ctypes.get_last_error()})"
        )
    descriptor = -1
    try:
        descriptor = msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        os.set_inheritable(descriptor, False)
        opened = os.fstat(descriptor)
        after = os.lstat(path)
        opened_identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or opened.st_nlink != 1
            or after.st_nlink != 1
            or _has_reparse_attribute(opened)
            or _has_reparse_attribute(after)
            or opened_identity != expected_identity
            or opened_identity != (before.st_dev, before.st_ino)
            or opened_identity != (after.st_dev, after.st_ino)
        ):
            raise error_factory(
                f"{target_label} changed while its verification lease was acquired"
            )
        # FILE_SHARE_READ denies delete for every outstanding handle. A parent
        # that only waits inside this process is protected until it closes the
        # descriptor. A detached installer must inherit the handle so the
        # sharing mode survives after the parent descriptor is closed.
        return descriptor
    except BaseException as exc:
        if descriptor >= 0:
            cleanup_actions = (
                (
                    lambda descriptor=descriptor: os.close(descriptor),
                    "installer lease acquisition cleanup failed",
                ),
            )
        else:
            cleanup_actions = (
                (
                    lambda: _close_windows_handle(kernel32, handle),
                    "installer lease acquisition cleanup failed",
                ),
            )
        run_typed_cleanup_actions(
            exc,
            cleanup_actions,
            error_factory=error_factory,
            failure_message="installer lease acquisition cleanup failed",
        )
        raise


def _close_windows_handle(kernel32: object, handle: object) -> None:
    if not kernel32.CloseHandle(handle):
        import ctypes

        raise OSError(ctypes.get_last_error(), "installer lease handle cleanup failed")


def _has_reparse_attribute(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(value.st_mode) or bool(attributes & 0x400)


def _safe_regular_file(value: os.stat_result) -> bool:
    return (
        stat.S_ISREG(value.st_mode)
        and value.st_nlink == 1
        and not _has_reparse_attribute(value)
    )
