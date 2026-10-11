"""禁止跟随重解析点的媒体重命名句柄。"""

import ctypes
import errno
import os
import secrets
import stat
import sys

from app.services.path_policy import ApprovedChildPath
from shared.filesystem_directory_capability import is_link_or_reparse


def _checked_regular_identity(
    candidate: os.stat_result,
    expected: os.stat_result,
) -> bool:
    return (
        stat.S_ISREG(candidate.st_mode)
        and not is_link_or_reparse(candidate)
        and os.path.samestat(candidate, expected)
    )


def _open_nofollow_rename_descriptor(source: ApprovedChildPath) -> int:
    """打开禁止跟随重解析点的源文件句柄，供随后按该句柄改名。"""
    if os.name == "nt":
        return _open_windows_nofollow_rename_descriptor(source.absolute_path)
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    directory_fd = getattr(source.parent, "_descriptor", None)
    if not isinstance(directory_fd, int):
        raise OSError("approved directory handle is unavailable for rename")
    return os.open(source.name, flags, dir_fd=directory_fd)


def _open_windows_nofollow_rename_descriptor(path: str) -> int:
    import msvcrt
    from ctypes import wintypes

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
    handle = create_file(
        path,
        0x00100000 | 0x00010000 | 0x00000080,  # SYNCHRONIZE | DELETE | FILE_READ_ATTRIBUTES
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,  # OPEN_EXISTING
        0x00200000 | 0x00000080,  # FILE_FLAG_OPEN_REPARSE_POINT | FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in {None, invalid_handle}:
        raise OSError(ctypes.get_last_error(), "rename handle could not be opened", path)

    def close_raw() -> None:
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        close_handle(handle)

    try:
        if _windows_handle_is_reparse_or_directory(kernel32, handle):
            raise OSError("rename handle is a link or reparse point")
        descriptor = msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        close_raw()
        raise
    try:
        os.set_inheritable(descriptor, False)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _windows_handle_is_reparse_or_directory(kernel32, handle: int) -> bool:
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    get_information.restype = wintypes.BOOL
    information = ByHandleFileInformation()
    if not get_information(handle, ctypes.byref(information)):
        raise OSError(ctypes.get_last_error(), "rename handle could not be inspected")
    attributes = int(information.dwFileAttributes)
    return bool(attributes & 0x400) or bool(attributes & 0x10)


def _rename_windows_descriptor(descriptor: int, target_path: str, *, replace: bool) -> None:
    import msvcrt

    handle = int(msvcrt.get_osfhandle(descriptor))
    if handle == -1:
        raise OSError(errno.EBADF, "rename handle is invalid")
    _set_windows_rename_by_handle(handle, target_path, replace=replace)


def _set_windows_rename_by_handle(handle: int, target_path: str, *, replace: bool) -> None:
    from ctypes import wintypes

    class FileRenameInformation(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BOOLEAN),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    encoded_name = os.fspath(target_path).encode("utf-16-le")
    buffer_size = FileRenameInformation.FileName.offset + len(encoded_name) + 2
    buffer = ctypes.create_string_buffer(buffer_size)
    information = ctypes.cast(buffer, ctypes.POINTER(FileRenameInformation)).contents
    information.ReplaceIfExists = bool(replace)
    information.RootDirectory = 0
    information.FileNameLength = len(encoded_name)
    ctypes.memmove(
        ctypes.addressof(buffer) + FileRenameInformation.FileName.offset,
        encoded_name,
        len(encoded_name),
    )
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    set_information.restype = wintypes.BOOL
    if set_information(handle, 3, buffer, buffer_size):
        return
    error_number = ctypes.get_last_error()
    if error_number in {80, 183}:
        raise FileExistsError(error_number, "rename target already exists", target_path)
    raise OSError(error_number, "rename by handle failed", target_path)


_AT_FDCWD = -100
_AT_EMPTY_PATH = 0x1000


def _symlink_follow_flag() -> int:
    if sys.platform == "darwin":
        return 0x40
    return 0x400


def _rename_posix_nofollow(
    descriptor: int,
    source: ApprovedChildPath,
    target: ApprovedChildPath,
    *,
    replace: bool,
) -> None:
    """用已经打开的 O_NOFOLLOW 描述符提交改名。

    检查之后不再按源名称 renameat。名称若被换成另一个文件，那个文件留在原地。
    提交结果必须仍是检查时的 inode。
    """

    if not getattr(os, "O_NOFOLLOW", 0):
        raise OSError(errno.ENOTSUP, "O_NOFOLLOW is required to commit a rename")
    source_dir = getattr(source.parent, "_descriptor", None)
    target_dir = getattr(target.parent, "_descriptor", None)
    if not isinstance(source_dir, int) or not isinstance(target_dir, int):
        raise OSError("approved directory handle is unavailable for rename")
    if not isinstance(descriptor, int) or descriptor < 0:
        raise OSError(errno.EBADF, "rename descriptor is invalid")
    checked = os.fstat(descriptor)
    if not _checked_regular_identity(checked, checked):
        raise OSError("rename descriptor is not the checked regular file")
    if replace:
        _publish_open_descriptor_replace(descriptor, target_dir, target.name)
    else:
        _link_open_descriptor(descriptor, target_dir, target.name)
    published = os.stat(target.name, dir_fd=target_dir, follow_symlinks=False)
    if not _checked_regular_identity(published, checked) or not os.path.samestat(
        os.fstat(descriptor),
        checked,
    ):
        raise OSError("rename committed a different inode")
    _drop_source_name_if_same_inode(source_dir, source.name, checked)


def _publish_open_descriptor_replace(
    descriptor: int,
    target_dir: int,
    target_name: str,
) -> None:
    temporary = f".ucrawl-rename-{secrets.token_hex(16)}"
    _link_open_descriptor(descriptor, target_dir, temporary)
    try:
        os.replace(
            temporary,
            target_name,
            src_dir_fd=target_dir,
            dst_dir_fd=target_dir,
        )
    except BaseException:
        try:
            current = os.stat(temporary, dir_fd=target_dir, follow_symlinks=False)
            if _checked_regular_identity(current, os.fstat(descriptor)):
                os.unlink(temporary, dir_fd=target_dir)
        except OSError:
            pass
        raise


def _link_open_descriptor(descriptor: int, target_dir: int, target_name: str) -> None:
    encoded_target = os.fsencode(target_name)
    fallback_errors = {
        errno.EINVAL,
        errno.ENOENT,
        errno.ENOTSUP,
        errno.EPERM,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    }
    try:
        _call_linkat(descriptor, b"", target_dir, encoded_target, _AT_EMPTY_PATH)
        return
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in fallback_errors:
            raise
    last_error: OSError | None = None
    for source_name in (
        f"/proc/self/fd/{descriptor}".encode("ascii"),
        f"/dev/fd/{descriptor}".encode("ascii"),
    ):
        try:
            _call_linkat(
                _AT_FDCWD,
                source_name,
                target_dir,
                encoded_target,
                _symlink_follow_flag(),
            )
            return
        except FileExistsError:
            raise
        except OSError as exc:
            last_error = exc
            if exc.errno not in fallback_errors:
                raise
    if last_error is not None:
        raise last_error
    raise OSError(errno.ENOTSUP, "descriptor link is unavailable")


def _call_linkat(
    source_directory: int,
    source_name: bytes,
    target_directory: int,
    target_name: bytes,
    flags: int,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    linkat = getattr(libc, "linkat", None)
    if linkat is None:
        raise OSError(errno.ENOTSUP, "linkat is unavailable")
    linkat.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    linkat.restype = ctypes.c_int
    if (
        linkat(source_directory, source_name, target_directory, target_name, flags)
        == 0
    ):
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number), target_name)
    raise OSError(error_number, os.strerror(error_number), target_name)


def _drop_source_name_if_same_inode(
    source_dir: int,
    source_name: str,
    checked: os.stat_result,
) -> None:
    try:
        current = os.stat(source_name, dir_fd=source_dir, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not _checked_regular_identity(current, checked):
        return
    os.unlink(source_name, dir_fd=source_dir)
