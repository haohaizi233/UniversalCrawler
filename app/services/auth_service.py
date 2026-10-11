"""认证/Cookie 文件读写与 Playwright 登录态持久化。"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit

from app.exceptions import CookieLoadError, CookieSaveError

_COOKIE_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_COOKIE_FIELD_UNSET = object()
_WINDOWS_DACL_SECURITY_INFORMATION = 0x4
_WINDOWS_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_WINDOWS_SE_FILE_OBJECT = 1


def _restrict_owner_access(path: str) -> None:
    """让已存在的文件仅当前用户可读写。"""
    if os.name == "nt":
        _restrict_windows_owner_acl(path)
        return
    os.chmod(path, 0o600)


def _restrict_windows_owner_acl(path: str) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sid = _current_windows_user_sid(advapi32, kernel32)
    descriptor = ctypes.c_void_p()
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"D:P(A;;FA;;;{sid})",
        1,
        ctypes.byref(descriptor),
        None,
    ):
        raise OSError(ctypes.get_last_error(), "无法创建仅当前用户可访问的安全描述符")
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        advapi32.GetSecurityDescriptorDacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.BOOL),
        ]
        advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ):
            raise OSError(ctypes.get_last_error(), "无法读取登录态文件 DACL")
        if not present.value or not dacl.value:
            raise OSError("登录态安全描述符缺少 DACL")
        advapi32.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
        status = advapi32.SetNamedSecurityInfoW(
            os.path.abspath(path),
            _WINDOWS_SE_FILE_OBJECT,
            _WINDOWS_DACL_SECURITY_INFORMATION
            | _WINDOWS_PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if status != 0:
            raise OSError(status, "无法将登录态文件限制为当前用户")
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(descriptor)


def _current_windows_user_sid(advapi32, kernel32) -> str:
    import ctypes
    from ctypes import wintypes

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("User", _SidAndAttributes)]

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        0x0008,
        ctypes.byref(token),
    ):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_uint,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        required = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        if ctypes.get_last_error() != 122:
            raise OSError(ctypes.get_last_error(), "GetTokenInformation size query failed")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            1,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        sid_pointer = ctypes.c_void_p()
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        if not advapi32.ConvertSidToStringSidW(
            token_user.User.Sid,
            ctypes.byref(sid_pointer),
        ):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
        try:
            sid = ctypes.cast(sid_pointer, ctypes.c_wchar_p).value
            if not sid:
                raise OSError("当前用户 SID 为空")
            return sid
        finally:
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
            kernel32.LocalFree(sid_pointer)
    finally:
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(token)


def _cookie_value_is_safe(value: str) -> bool:
    return all(
        code == 0x21
        or 0x23 <= code <= 0x2B
        or 0x2D <= code <= 0x3A
        or 0x3C <= code <= 0x5B
        or 0x5D <= code <= 0x7E
        for code in map(ord, value)
    )


class AuthService:
    """统一处理 Cookie JSON 格式，并把文件 IO/JSON 异常收敛为领域异常。

    Cookie 以明文 JSON 保存；``file_path``/``save_path`` 必须来自受信配置。
    临时文件和最终文件都限制为仅当前用户可读写，本服务不加密 Cookie 内容。
    """

    @staticmethod
    def _safe_to_string(value: Any) -> str | None:
        try:
            return str(value)
        except (TypeError, ValueError):
            return None

    def load_json_file(self, file_path: str) -> Any:
        """文件不存在视为尚未登录；读取或 JSON 解析失败统一抛出 CookieLoadError。"""
        if not os.path.exists(file_path):
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CookieLoadError(str(exc)) from exc

    def save_json_file(self, file_path: str, payload: Any) -> None:
        """先在目标目录写入并 fsync 临时文件，再用 os.replace 发布完整 JSON。

        Unix 将临时文件和最终文件设为 ``0o600``。Windows 写入前就套上仅含
        当前用户的保护 DACL，发布后再次套用，避免替换把宽松 ACL 留在目标上。
        """
        temp_path: str | None = None
        try:
            serialized = json.dumps(payload, indent=4, ensure_ascii=False)
            destination = os.path.abspath(file_path)
            directory = os.path.dirname(destination)
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=directory,
                prefix=f".{os.path.basename(destination)}.",
                suffix=".tmp",
                delete=False,
            ) as fp:
                temp_path = fp.name
                _restrict_owner_access(temp_path)
                fp.write(serialized)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(temp_path, destination)
            temp_path = None
            _restrict_owner_access(destination)
        except (OSError, TypeError, ValueError) as exc:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise CookieSaveError(str(exc)) from exc

    @staticmethod
    def extract_cookie_list(payload: list[dict] | dict | None) -> list[dict]:
        """兼容 Cookie 列表和 Playwright storage_state 的 cookies 包装。"""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("cookies"), list):
            return payload["cookies"]
        return []

    @classmethod
    def extract_cookie_dict(cls, payload: list[dict] | dict | None) -> dict[str, str]:
        """接受平面字典或 Playwright Cookie 列表，并过滤无法安全转成字符串的项。"""
        if isinstance(payload, dict) and "cookies" not in payload:
            result: dict[str, str] = {}
            for key, value in payload.items():
                safe_key = cls._safe_to_string(key)
                safe_value = cls._safe_to_string(value)
                if safe_key is not None and safe_value is not None:
                    result[safe_key] = safe_value
            return result
        cookies = cls.extract_cookie_list(payload)
        result: dict[str, str] = {}
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if name and value is not None:
                safe_name = cls._safe_to_string(name)
                safe_value = cls._safe_to_string(value)
                if safe_name is not None and safe_value is not None:
                    result[safe_name] = safe_value
        return result

    @classmethod
    def extract_cookie_dict_for_url(
        cls,
        payload: list[dict] | dict | None,
        url: str,
        *,
        now: float | None = None,
        require_scope: bool = False,
    ) -> dict[str, str]:
        """按浏览器域名、路径、Secure 与过期规则筛出目标 URL 可用的 Cookie。"""
        if isinstance(payload, dict) and "cookies" not in payload:
            return {} if require_scope else cls.extract_cookie_dict(payload)
        parsed = urlsplit(str(url or ""))
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            return {}
        request_path = parsed.path or "/"
        current_time = time.time() if now is None else float(now)
        result: dict[str, str] = {}
        for cookie in cls.extract_cookie_list(payload):
            if not isinstance(cookie, dict):
                continue
            domain = str(cookie.get("domain") or "").strip().lower().rstrip(".")
            raw_path = cookie.get("path")
            if require_scope and (
                not domain
                or not isinstance(raw_path, str)
                or not raw_path.startswith("/")
                or any(ord(char) < 0x21 or ord(char) == 0x7F for char in domain)
                or any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw_path)
            ):
                continue
            if domain:
                include_subdomains = domain.startswith(".")
                normalized_domain = domain.lstrip(".")
                domain_matches = host == normalized_domain or (
                    include_subdomains and host.endswith(f".{normalized_domain}")
                )
                if not domain_matches:
                    continue
            cookie_path = str(cookie.get("path") or "/")
            path_matches = request_path == cookie_path or (
                request_path.startswith(cookie_path)
                and (
                    cookie_path.endswith("/")
                    or request_path[len(cookie_path) :].startswith("/")
                )
            )
            if not path_matches:
                continue
            raw_secure = cookie.get("secure", _COOKIE_FIELD_UNSET)
            if (
                require_scope
                and raw_secure is not _COOKIE_FIELD_UNSET
                and not isinstance(raw_secure, bool)
            ):
                continue
            is_secure = (
                raw_secure is True
                if require_scope
                else bool(False if raw_secure is _COOKIE_FIELD_UNSET else raw_secure)
            )
            if is_secure and parsed.scheme.lower() != "https":
                continue
            raw_expires = cookie.get("expires", _COOKIE_FIELD_UNSET)
            if require_scope:
                if raw_expires is _COOKIE_FIELD_UNSET:
                    expires = -1.0
                elif isinstance(raw_expires, bool) or not isinstance(
                    raw_expires,
                    (int, float),
                ):
                    continue
                else:
                    try:
                        expires = float(raw_expires)
                    except (OverflowError, TypeError, ValueError):
                        continue
                if not math.isfinite(expires):
                    continue
                if expires != -1 and expires <= 0:
                    continue
            else:
                try:
                    expires = float(
                        -1
                        if raw_expires is _COOKIE_FIELD_UNSET
                        else raw_expires or -1
                    )
                except (OverflowError, TypeError, ValueError):
                    expires = -1
            if expires > 0 and expires <= current_time:
                continue
            name = cookie.get("name")
            value = cookie.get("value")
            if not name or value is None:
                continue
            safe_name = cls._safe_to_string(name)
            safe_value = cls._safe_to_string(value)
            if (
                safe_name is not None
                and safe_value is not None
                and (
                    not require_scope
                    or (
                        _COOKIE_NAME_PATTERN.fullmatch(safe_name) is not None
                        and _cookie_value_is_safe(safe_value)
                    )
                )
            ):
                result[safe_name] = safe_value
        return result

    @classmethod
    def build_cookie_string(cls, payload: list[dict] | dict | None, required_cookie: str | None = None) -> str:
        """缺少 required_cookie 时返回空串，避免携带不完整登录态发起请求。"""
        cookie_dict = cls.extract_cookie_dict(payload)
        if required_cookie and required_cookie not in cookie_dict:
            return ""
        return "; ".join(f"{name}={value}" for name, value in cookie_dict.items())

    def restore_playwright_cookies(self, context, file_path: str) -> bool:
        """兼容仍需向已创建 context 注入 Cookie 的旧调用方。"""
        payload = self.load_json_file(file_path)
        cookies = self.extract_cookie_list(payload)
        if not cookies:
            return False
        context.add_cookies(cookies)
        return True

    def load_playwright_storage_state(self, file_path: str) -> dict[str, list[dict]] | None:
        """读取可直接交给 ``browser.new_context`` 的完整登录态。

        新版文件保留 Playwright 的 cookies 和 origins/localStorage；旧版裸 Cookie
        列表会补成合法 storage_state，避免升级后要求用户重新登录。
        """
        payload = self.load_json_file(file_path)
        cookies = self.extract_cookie_list(payload)
        if not cookies:
            return None
        origins = payload.get("origins", []) if isinstance(payload, dict) else []
        if not isinstance(origins, list):
            origins = []
        return {
            "cookies": list(cookies),
            "origins": list(origins),
        }

    def wait_for_cookie_and_persist(
        self,
        *,
        context,
        cookie_name: str,
        save_path: str,
        save_mode: str = "cookies",
        max_attempts: int = 120,
        interval_seconds: float = 1.0,
        stop_check=None,
        wait_callback=None,
    ) -> bool:
        """轮询 Playwright context；stop_check 可中止，wait_callback 由调用方接管等待节奏。"""

        for _ in range(max_attempts):
            if stop_check and stop_check():
                return False
            cookies = context.cookies()
            if self.has_cookie(cookies, cookie_name):
                payload = context.storage_state() if save_mode == "storage_state" else cookies
                self.save_json_file(save_path, payload)
                return True
            if wait_callback:
                wait_callback()
            else:
                time.sleep(interval_seconds)
        return False

    @staticmethod
    def has_cookie(cookies: list[dict] | dict | None, cookie_name: str) -> bool:
        
        if isinstance(cookies, list):
            return any(cookie.get("name") == cookie_name for cookie in cookies)
        if isinstance(cookies, dict):
            return cookie_name in cookies
        return False
