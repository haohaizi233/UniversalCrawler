import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from httpx import HTTPStatusError, Request, Response

from app.core.lib.douyin.interface.template import (
    API,
    extract_work_id,
    redact_logged_headers,
    redact_logged_text,
    strip_ms_token_query,
)
from app.core.lib.douyin.tools.capture import capture_error_params, capture_error_request


class RequestLogRedactionTests(unittest.IsolatedAsyncioTestCase):
    def test_headers_and_query_drop_credentials_before_logging(self):
        headers = redact_logged_headers(
            {
                "Cookie": "sid=secret-cookie",
                "Set-Cookie": "sid=secret-set-cookie",
                "Authorization": "Bearer secret-auth",
                "User-Agent": "ua",
            }
        )
        query = strip_ms_token_query(
            "https://www.douyin.com/aweme/detail/?aweme_id=123&msToken=secret-token&aid=1"
        )

        self.assertEqual(headers, {"User-Agent": "ua"})
        self.assertNotIn("msToken", query)
        self.assertIn("aweme_id=123", query)
        self.assertEqual(extract_work_id("aweme_id=123&msToken=secret-token"), "123")

    async def test_request_log_keeps_status_code_and_work_id_only(self):
        messages: list[str] = []
        api = object.__new__(API)
        api.log = SimpleNamespace(
            info=lambda message, *args, **kwargs: messages.append(str(message)),
            warning=lambda *args, **kwargs: None,
            error=lambda message, *args, **kwargs: messages.append(str(message)),
        )
        api._logged_work_id = ""
        api._API__record_request_messages(
            "https://www.douyin.com/aweme/detail/?msToken=secret-token",
            "aweme_id=1234567890123456789&msToken=secret-token",
            {"payload": "secret-token"},
            {
                "Cookie": "sid=secret-cookie",
                "Authorization": "Bearer secret-auth",
                "Set-Cookie": "sid=secret-set-cookie",
                "User-Agent": "ua",
            },
        )
        request = Request(
            "GET",
            "https://www.douyin.com/aweme/detail/?msToken=secret-token&aid=1",
        )
        http_response = Response(403, request=request)
        response = SimpleNamespace(
            status_code=403,
            url="https://www.douyin.com/aweme/detail/?msToken=secret-token",
            headers={
                "Set-Cookie": "sid=secret-set-cookie",
                "Authorization": "Bearer secret-auth",
            },
            raise_for_status=lambda: (_ for _ in ()).throw(
                HTTPStatusError("status", request=request, response=http_response)
            ),
            json=lambda: {"ok": True},
        )

        with patch("app.core.lib.douyin.interface.template.wait", new=AsyncMock()):
            result = await api._API__return_response(response)

        rendered = "\n".join(messages)
        self.assertIsNone(result)
        self.assertIn("Work ID: 1234567890123456789", rendered)
        self.assertIn("Response Code: 403", rendered)
        for secret in (
            "secret-token",
            "secret-cookie",
            "secret-set-cookie",
            "secret-auth",
            "Response URL",
            "Response Headers",
            "Headers:",
        ):
            self.assertNotIn(secret, rendered)

    async def test_capture_strips_ms_token_from_http_status_error(self):
        messages: list[str] = []

        @capture_error_params
        async def fail_params(logger):
            request = Request(
                "GET",
                "https://www.douyin.com/aweme/v1/?msToken=secret-token&aid=6383",
            )
            response = Response(403, request=request)
            raise HTTPStatusError(
                "status for url 'https://www.douyin.com/aweme/v1/?msToken=secret-token&aid=6383'",
                request=request,
                response=response,
            )

        @capture_error_request
        async def fail_request(self):
            request = Request(
                "GET",
                "https://www.douyin.com/aweme/v1/?aweme_id=123&msToken=secret-token",
            )
            response = Response(403, request=request)
            raise HTTPStatusError(
                "status for url 'https://www.douyin.com/aweme/v1/?aweme_id=123&msToken=secret-token'",
                request=request,
                response=response,
            )

        logger = SimpleNamespace(
            error=lambda message, *args, **kwargs: messages.append(str(message)),
        )
        owner = SimpleNamespace(
            log=SimpleNamespace(
                error=lambda message, *args, **kwargs: messages.append(str(message)),
            ),
        )

        self.assertIsNone(await fail_params(logger))
        self.assertIsNone(await fail_request(owner))
        rendered = "\n".join(messages)
        self.assertNotIn("secret-token", rendered)
        self.assertIn("aweme_id=123", rendered)
        self.assertIn("aid=6383", redact_logged_text(
            "https://www.douyin.com/aweme/v1/?msToken=secret-token&aid=6383"
        ))


if __name__ == "__main__":
    unittest.main()
