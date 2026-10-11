import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.core.lib.douyin.interface.template import API
from app.core.lib.douyin.tools import DATA_HEADERS, DOWNLOAD_HEADERS
from app.core.lib.douyin.tools.parameter import MsToken, Parameter, TtWid

class ParameterUpdateParamsTests(unittest.IsolatedAsyncioTestCase):
    def _make_parameter(self, cookie_str: str):
        param = object.__new__(Parameter)
        param.douyin_platform = True
        param.tiktok_platform = False
        param.cookie_dict = {}
        param.cookie_str = cookie_str
        param.cookie_dict_tiktok = {}
        param.cookie_str_tiktok = ""
        param.headers = {}
        param.headers_download = {}
        param.headers_tiktok = {}
        param.headers_download_tiktok = {}
        param.console = SimpleNamespace(info=lambda *args, **kwargs: None)
        param.logger = SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        )
        param.proxy = None
        param.proxy_tiktok = None
        return param

    async def test_update_params_uses_local_ttwid_and_cached_ms_token(self):
        param = self._make_parameter("sessionid_ss=abc; ttwid=local-ttwid")
        param.params = {"msToken": "cached-ms"}
        param._Parameter__get_token_params = AsyncMock(return_value={MsToken.NAME: "remote-ms"})
        param._Parameter__get_tt_wid_params = AsyncMock(return_value={TtWid.NAME: "remote-ttwid"})
        class_params = dict(API.params)

        await param.update_params()

        param._Parameter__get_token_params.assert_not_awaited()
        param._Parameter__get_tt_wid_params.assert_not_awaited()
        self.assertEqual(API.params, class_params)
        self.assertEqual(param.params["msToken"], "cached-ms")
        self.assertIn("ttwid=local-ttwid", param.headers["Cookie"])
        self.assertIn("msToken=cached-ms", param.headers["Cookie"])

    async def test_update_params_fetches_remote_values_when_no_local_or_cached_values(self):
        param = self._make_parameter("sessionid_ss=abc")
        param.params = {}
        param._Parameter__get_token_params = AsyncMock(return_value={MsToken.NAME: "fake-ms"})
        param._Parameter__get_tt_wid_params = AsyncMock(return_value={TtWid.NAME: "remote-ttwid"})
        class_params = dict(API.params)

        await param.update_params()

        param._Parameter__get_token_params.assert_awaited_once()
        param._Parameter__get_tt_wid_params.assert_awaited_once()
        self.assertEqual(API.params, class_params)
        self.assertEqual(param.params["msToken"], "fake-ms")
        self.assertIn("msToken=fake-ms", param.headers["Cookie"])
        self.assertIn("ttwid=remote-ttwid", param.headers["Cookie"])

    def test_headers_and_params_are_isolated_per_parameter(self):
        first = object.__new__(Parameter)
        second = object.__new__(Parameter)
        first._bind_isolated_request_state()
        second._bind_isolated_request_state()

        self.assertIsNot(first.headers, DATA_HEADERS)
        self.assertIsNot(first.headers_download, DOWNLOAD_HEADERS)
        self.assertIsNot(first.params, API.params)
        first.headers["Cookie"] = "session=one"
        first.headers_download["Cookie"] = "session=one"
        first.params["msToken"] = "token-one"

        self.assertNotIn("Cookie", second.headers)
        self.assertNotIn("Cookie", second.headers_download)
        self.assertNotIn("Cookie", DATA_HEADERS)
        self.assertNotIn("Cookie", DOWNLOAD_HEADERS)
        self.assertEqual(second.params.get("msToken"), "")
        self.assertEqual(API.params.get("msToken"), "")

    async def test_token_lookup_does_not_log_ms_token_value(self):
        messages: list[str] = []
        param = self._make_parameter("sessionid_ss=abc; msToken=local-secret-token")
        param.cookie_dict = {"msToken": "local-secret-token"}
        param.logger = SimpleNamespace(
            info=lambda message, *args, **kwargs: messages.append(str(message)),
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        )

        token = await param._Parameter__get_token_params()

        self.assertEqual(token[MsToken.NAME], "local-secret-token")
        self.assertTrue(messages)
        self.assertTrue(all("local-secret-token" not in message for message in messages))

    async def test_get_token_params_prefers_local_value(self):
        param = self._make_parameter("sessionid_ss=abc; msToken=local-ms")
        token = await param._Parameter__get_token_params()

        self.assertEqual(token[MsToken.NAME], "local-ms")

    async def test_get_token_params_uses_fake_value_without_remote_fetch(self):
        param = self._make_parameter("sessionid_ss=abc")
        param.cookie_str = "sessionid_ss=abc"

        with patch(
            "app.core.lib.douyin.tools.parameter.MsToken.get_fake_ms_token",
            return_value={MsToken.NAME: "fake-ms"},
        ) as mock_fake:
            token = await param._Parameter__get_token_params()

        self.assertEqual(token[MsToken.NAME], "fake-ms")
        mock_fake.assert_called_once_with()

    async def test_get_tt_wid_params_use_fast_timeout_cap(self):
        messages: list[str] = []
        param = self._make_parameter("sessionid_ss=abc")
        param.timeout = 10
        param.headers_params = {}
        param.logger = SimpleNamespace(
            info=lambda message, *args, **kwargs: messages.append(str(message)),
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        )

        with patch(
            "app.core.lib.douyin.tools.parameter.TtWid.get_tt_wid",
            AsyncMock(return_value={TtWid.NAME: "remote-ttwid"}),
        ) as mock_ttwid:
            tt_wid = await param._Parameter__get_tt_wid_params()

        self.assertEqual(tt_wid[TtWid.NAME], "remote-ttwid")
        self.assertEqual(mock_ttwid.await_args.kwargs["timeout"], 3)
        self.assertTrue(messages)
        self.assertTrue(all("remote-ttwid" not in message for message in messages))

if __name__ == "__main__":
    unittest.main()
