"""认证服务的凭据读取、校验与持久化测试。"""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.exceptions import CookieLoadError, CookieSaveError
from app.services.auth_service import AuthService

class AuthServiceTests(unittest.TestCase):
    
    def setUp(self):
        
        self.service = AuthService()

    def test_extract_cookie_list_reads_storage_state_shape(self):
        """验证 `test_extract_cookie_list_reads_storage_state_shape` 对应场景是否符合预期，供 `AuthServiceTests` 使用。"""
        cookies = self.service.extract_cookie_list(
            {"cookies": [{"name": "sessionid_ss", "value": "abc"}]}
        )

        self.assertEqual(cookies, [{"name": "sessionid_ss", "value": "abc"}])

    def test_extract_cookie_dict_supports_list_payload(self):
        """验证 `test_extract_cookie_dict_supports_list_payload` 对应场景是否符合预期，供 `AuthServiceTests` 使用。"""
        cookie_dict = self.service.extract_cookie_dict(
            [{"name": "SESSDATA", "value": "xyz"}]
        )

        self.assertEqual(cookie_dict, {"SESSDATA": "xyz"})

    def test_extract_cookie_dict_for_url_filters_wrong_domain_and_expired_cookie(self):
        payload = {
            "cookies": [
                {
                    "name": "userId",
                    "value": "identity-only",
                    "domain": "id.kuaishou.com",
                    "path": "/",
                    "expires": 2000,
                },
                {
                    "name": "userId",
                    "value": "expired-main-site",
                    "domain": ".kuaishou.com",
                    "path": "/",
                    "expires": 900,
                },
                {
                    "name": "did",
                    "value": "device",
                    "domain": ".kuaishou.com",
                    "path": "/",
                    "expires": 2000,
                },
            ]
        }

        cookie_dict = self.service.extract_cookie_dict_for_url(
            payload,
            "https://www.kuaishou.com/",
            now=1000,
        )

        self.assertEqual(cookie_dict, {"did": "device"})

    def test_extract_cookie_dict_for_url_keeps_legacy_cookie_without_scope(self):
        payload = {"cookies": [{"name": "sessionid_ss", "value": "legacy"}]}

        cookie_dict = self.service.extract_cookie_dict_for_url(
            payload,
            "https://www.douyin.com/",
            now=1000,
        )

        self.assertEqual(cookie_dict, {"sessionid_ss": "legacy"})

    def test_extract_cookie_dict_for_url_enforces_browser_path_boundary(self):
        payload = {
            "cookies": [
                {
                    "name": "share_only",
                    "value": "allowed",
                    "domain": ".kuaishou.com",
                    "path": "/f",
                }
            ]
        }

        self.assertEqual(
            self.service.extract_cookie_dict_for_url(
                payload,
                "https://www.kuaishou.com/f/example",
            ),
            {"share_only": "allowed"},
        )
        self.assertEqual(
            self.service.extract_cookie_dict_for_url(
                payload,
                "https://www.kuaishou.com/foobar",
            ),
            {},
        )

    def test_extract_cookie_dict_for_url_can_require_explicit_scope(self):
        payload = {
            "cookies": [
                {"name": "legacy", "value": "unscoped"},
                {
                    "name": "site",
                    "value": "scoped",
                    "domain": ".kuaishou.com",
                    "path": "/",
                },
            ]
        }

        cookie_dict = self.service.extract_cookie_dict_for_url(
            payload,
            "https://www.kuaishou.com/",
            require_scope=True,
        )

        self.assertEqual(cookie_dict, {"site": "scoped"})

    def test_strict_url_cookie_filter_rejects_ambiguous_expiry_and_secure_types(self):
        base = {
            "domain": ".kuaishou.com",
            "path": "/",
        }
        payload = {
            "cookies": [
                {**base, "name": "missing_expiry", "value": "allowed"},
                {
                    **base,
                    "name": "session",
                    "value": "allowed",
                    "expires": -1,
                    "secure": False,
                },
                {
                    **base,
                    "name": "future_secure",
                    "value": "allowed",
                    "expires": 2000,
                    "secure": True,
                },
                {**base, "name": "zero", "value": "blocked", "expires": 0},
                {**base, "name": "negative", "value": "blocked", "expires": -2},
                {**base, "name": "none", "value": "blocked", "expires": None},
                {**base, "name": "empty", "value": "blocked", "expires": ""},
                {
                    **base,
                    "name": "numeric_string",
                    "value": "blocked",
                    "expires": "2000",
                },
                {**base, "name": "false", "value": "blocked", "expires": False},
                {
                    **base,
                    "name": "secure_int",
                    "value": "blocked",
                    "secure": 0,
                },
                {
                    **base,
                    "name": "secure_list",
                    "value": "blocked",
                    "secure": [],
                },
                {
                    **base,
                    "name": "secure_empty",
                    "value": "blocked",
                    "secure": "",
                },
            ]
        }

        cookie_dict = self.service.extract_cookie_dict_for_url(
            payload,
            "https://www.kuaishou.com/",
            now=1000,
            require_scope=True,
        )

        self.assertEqual(
            cookie_dict,
            {
                "missing_expiry": "allowed",
                "session": "allowed",
                "future_secure": "allowed",
            },
        )

    def test_build_cookie_string_requires_target_cookie_when_requested(self):
        """验证 `test_build_cookie_string_requires_target_cookie_when_requested` 对应场景是否符合预期，供 `AuthServiceTests` 使用。"""
        cookie_str = self.service.build_cookie_string(
            [{"name": "sid_guard", "value": "1"}],
            required_cookie="sessionid_ss",
        )

        self.assertEqual(cookie_str, "")

    def test_extract_cookie_dict_skips_values_that_fail_string_conversion(self):
        """验证 `test_extract_cookie_dict_skips_values_that_fail_string_conversion` 对应场景是否符合预期，供 `AuthServiceTests` 使用。"""
        class BrokenValue:
            
            def __str__(self):
                """提供 `__str__` 对应的内部辅助逻辑，供 `BrokenValue` 使用。"""
                raise TypeError("broken")

        cookie_dict = self.service.extract_cookie_dict({"userId": BrokenValue(), "sessionid": "ok"})

        self.assertEqual(cookie_dict, {"sessionid": "ok"})

    def test_restore_playwright_cookies_loads_saved_cookie_list(self):
        """验证 `test_restore_playwright_cookies_loads_saved_cookie_list` 对应场景是否符合预期，供 `AuthServiceTests` 使用。"""
        context = Mock()
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = f"{temp_dir}/auth.json"
            self.service.save_json_file(file_path, {"cookies": [{"name": "userId", "value": "1001"}]})

            restored = self.service.restore_playwright_cookies(context, file_path)

        self.assertTrue(restored)
        context.add_cookies.assert_called_once_with([{"name": "userId", "value": "1001"}])

    def test_load_playwright_storage_state_preserves_origin_local_storage(self):
        payload = {
            "cookies": [{"name": "userId", "value": "1001", "domain": ".kuaishou.com", "path": "/"}],
            "origins": [
                {
                    "origin": "https://www.kuaishou.com",
                    "localStorage": [{"name": "device-id", "value": "device-1"}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = f"{temp_dir}/auth.json"
            self.service.save_json_file(file_path, payload)

            restored = self.service.load_playwright_storage_state(file_path)

        self.assertEqual(restored, payload)

    def test_load_playwright_storage_state_upgrades_legacy_cookie_list(self):
        payload = [{"name": "userId", "value": "1001", "domain": ".kuaishou.com", "path": "/"}]
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = f"{temp_dir}/auth.json"
            self.service.save_json_file(file_path, payload)

            restored = self.service.load_playwright_storage_state(file_path)

        self.assertEqual(restored, {"cookies": payload, "origins": []})

    @patch("app.services.auth_service.time.sleep", return_value=None)
    def test_wait_for_cookie_and_persist_saves_context_state(self, _mock_sleep):
        """验证 `test_wait_for_cookie_and_persist_saves_context_state` 对应场景是否符合预期，供 `AuthServiceTests` 使用。"""
        context = Mock()
        context.cookies.side_effect = [[], [{"name": "userId", "value": "1001"}]]
        context.storage_state.return_value = {"cookies": [{"name": "userId", "value": "1001"}]}

        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = f"{temp_dir}/auth.json"
            success = self.service.wait_for_cookie_and_persist(
                context=context,
                cookie_name="userId",
                save_path=file_path,
                save_mode="storage_state",
                max_attempts=3,
            )
            payload = self.service.load_json_file(file_path)

        self.assertTrue(success)
        self.assertEqual(payload["cookies"][0]["name"], "userId")

    def test_wait_for_cookie_and_persist_stops_when_stop_check_requests_cancel(self):
        """验证 `test_wait_for_cookie_and_persist_stops_when_stop_check_requests_cancel` 对应场景是否符合预期，供 `AuthServiceTests` 使用。"""
        context = Mock()

        success = self.service.wait_for_cookie_and_persist(
            context=context,
            cookie_name="userId",
            save_path="unused.json",
            stop_check=lambda: True,
            max_attempts=3,
        )

        self.assertFalse(success)
        context.cookies.assert_not_called()

    def test_has_cookie_supports_list_dict_and_none_payloads(self):
        """验证 `test_has_cookie_supports_list_dict_and_none_payloads` 对应场景是否符合预期，供 `AuthServiceTests` 使用。"""
        self.assertTrue(self.service.has_cookie([{"name": "sid_guard", "value": "1"}], "sid_guard"))
        self.assertTrue(self.service.has_cookie({"sid_guard": "1"}, "sid_guard"))
        self.assertFalse(self.service.has_cookie(None, "sid_guard"))

    def test_load_json_file_returns_none_when_file_does_not_exist(self):
        """验证 `test_load_json_file_returns_none_when_file_does_not_exist` 对应场景是否符合预期，供 `AuthServiceTests` 使用。"""
        self.assertIsNone(self.service.load_json_file("missing-auth.json"))

    def test_load_json_file_raises_cookie_load_error_for_invalid_json(self):
        """验证 `test_load_json_file_raises_cookie_load_error_for_invalid_json` 对应场景是否符合预期，供 `AuthServiceTests` 使用。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = f"{temp_dir}/auth.json"
            with open(file_path, "w", encoding="utf-8") as fp:
                fp.write("{bad json")

            with self.assertRaises(CookieLoadError):
                self.service.load_json_file(file_path)

    def test_save_and_load_json_file_round_trip_keeps_payload(self):
        """验证 `test_save_and_load_json_file_round_trip_keeps_payload` 对应场景是否符合预期，供 `AuthServiceTests` 使用。"""
        payload = {"cookies": [{"name": "SESSDATA", "value": "demo"}]}

        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = f"{temp_dir}/auth.json"
            self.service.save_json_file(file_path, payload)
            restored = self.service.load_json_file(file_path)

        self.assertEqual(restored, payload)

    def test_save_json_file_preserves_credentials_when_serialization_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "auth.json"
            original = '{"cookies": [{"name": "session", "value": "old"}]}'
            file_path.write_text(original, encoding="utf-8")

            with self.assertRaises(CookieSaveError):
                self.service.save_json_file(
                    str(file_path),
                    {"cookies": [{"name": "session", "value": "new"}], "bad": object()},
                )

            self.assertEqual(file_path.read_text(encoding="utf-8"), original)

    def test_save_json_file_preserves_credentials_when_temp_write_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "auth.json"
            original = '{"cookies": [{"name": "session", "value": "old"}]}'
            file_path.write_text(original, encoding="utf-8")

            with patch(
                "app.services.auth_service.os.fsync",
                side_effect=OSError("simulated write interruption"),
            ), self.assertRaises(CookieSaveError):
                self.service.save_json_file(
                    str(file_path),
                    {"cookies": [{"name": "session", "value": "new"}]},
                )

            self.assertEqual(file_path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(file_path.parent.iterdir()), [file_path])

    def test_save_json_file_restricts_temp_and_destination_to_current_user(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = os.path.join(temp_dir, "auth.json")
            real_replace = os.replace
            seen: list[str] = []

            def spy(src, dst):
                seen.append(src)
                self.assertTrue(_is_owner_only(src))
                real_replace(src, dst)

            with patch("app.services.auth_service.os.replace", side_effect=spy):
                self.service.save_json_file(
                    file_path,
                    {"cookies": [{"name": "userId", "value": "1"}]},
                )
            self.assertTrue(seen)
            self.assertTrue(_is_owner_only(file_path))
            self.service.save_json_file(
                file_path,
                {"cookies": [{"name": "userId", "value": "2"}]},
            )
            self.assertEqual(
                self.service.load_json_file(file_path)["cookies"][0]["value"],
                "2",
            )
            self.assertTrue(_is_owner_only(file_path))


def _is_owner_only(path: str) -> bool:
    if os.name != "nt":
        return stat.S_IMODE(os.stat(path).st_mode) == 0o600
    return _windows_dacl_is_current_user_only(path)


def _windows_dacl_is_current_user_only(path: str) -> bool:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class AceHeader(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_byte),
            ("AceFlags", ctypes.c_byte),
            ("AceSize", wintypes.WORD),
        ]

    class AccessAllowedAce(ctypes.Structure):
        _fields_ = [
            ("Header", AceHeader),
            ("Mask", wintypes.DWORD),
            ("SidStart", wintypes.DWORD),
        ]

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("User", SidAndAttributes)]

    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    status = advapi32.GetNamedSecurityInfoW(
        os.path.abspath(path),
        1,
        0x4,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if status != 0 or not dacl.value:
        return False
    try:
        info = AclSizeInformation()
        advapi32.GetAclInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_int,
        ]
        advapi32.GetAclInformation.restype = wintypes.BOOL
        if not advapi32.GetAclInformation(
            dacl,
            ctypes.byref(info),
            ctypes.sizeof(info),
            2,
        ):
            return False
        if info.AceCount != 1:
            return False
        ace_pointer = ctypes.c_void_p()
        advapi32.GetAce.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.GetAce.restype = wintypes.BOOL
        if not advapi32.GetAce(dacl, 0, ctypes.byref(ace_pointer)):
            return False
        ace = ctypes.cast(ace_pointer, ctypes.POINTER(AccessAllowedAce)).contents
        if ace.Header.AceType != 0:
            return False
        sid = ctypes.c_void_p(ace_pointer.value + AccessAllowedAce.SidStart.offset)
        token = wintypes.HANDLE()
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return False
        try:
            required = wintypes.DWORD(0)
            advapi32.GetTokenInformation.argtypes = [
                wintypes.HANDLE,
                ctypes.c_uint,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ]
            advapi32.GetTokenInformation.restype = wintypes.BOOL
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
            buffer = ctypes.create_string_buffer(required.value)
            if not advapi32.GetTokenInformation(
                token,
                1,
                buffer,
                required.value,
                ctypes.byref(required),
            ):
                return False
            user_sid = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents.User.Sid
            advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            advapi32.EqualSid.restype = wintypes.BOOL
            return bool(advapi32.EqualSid(user_sid, sid))
        finally:
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(token)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(descriptor)


if __name__ == "__main__":
    unittest.main()
