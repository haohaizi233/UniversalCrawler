from __future__ import annotations

import unittest
from threading import get_ident
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from curl_cffi import CurlError
from curl_cffi.requests.exceptions import RequestException

from shared.network.pinned_transport import (
    PinnedResponse,
    PinnedTransportNetworkError,
)
from shared.runtime_options import DomainPolicyViolation

from app.core.lib.douyin.link.extractor import Extractor
from app.core.lib.douyin.link import requester as requester_module

Requester = requester_module.Requester


def is_douyin_public_host(host: str) -> bool:
    validator = getattr(requester_module, "is_douyin_public_host", None)
    assert callable(validator), "requester must expose complete-label host validation"
    return bool(validator(host))


def is_douyin_public_url(url: str) -> bool:
    validator = getattr(requester_module, "is_douyin_public_url", None)
    assert callable(validator), "requester must expose canonical public URL validation"
    return bool(validator(url))


def is_douyin_live_reflow_url(url: str) -> bool:
    validator = getattr(requester_module, "is_douyin_live_reflow_url", None)
    assert callable(validator), "requester must expose bounded live-path validation"
    return bool(validator(url))


class _RecordingLogger:
    def __init__(self) -> None:
        self.entries: list[tuple[str, tuple[Any, ...]]] = []

    def info(self, message: str, *args: Any) -> None:
        self.entries.append(("info", (message, *args)))

    def warning(self, message: str, *args: Any) -> None:
        self.entries.append(("warning", (message, *args)))

    def error(self, message: str, *args: Any) -> None:
        self.entries.append(("error", (message, *args)))


class _UnusedAsyncClient:
    def __init__(self) -> None:
        self.closed = False

    async def get(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Requester must not bypass PinnedTransport")

    async def aclose(self) -> None:
        self.closed = True


class _RecordingTransport:
    def __init__(
        self,
        response: PinnedResponse | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.response = response or PinnedResponse(
            200,
            "https://www.douyin.com:443/video/1",
            {},
            b"ok",
        )
        self.error = error
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.thread_ids: list[int] = []

    def request(self, method: str, url: str, **kwargs: Any) -> PinnedResponse:
        self.thread_ids.append(get_ident())
        self.calls.append((method, url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


def _requester(
    transport: _RecordingTransport,
    *,
    headers: dict[str, str] | None = None,
    max_retry: int = 0,
) -> tuple[Requester, _RecordingLogger, _UnusedAsyncClient]:
    logger = _RecordingLogger()
    client = _UnusedAsyncClient()
    params = SimpleNamespace(
        logger=logger,
        max_retry=max_retry,
        timeout=7,
    )
    return (
        Requester(
            params,
            client,
            headers or {"User-Agent": "test-agent"},
            transport=transport,
        ),
        logger,
        client,
    )


class DouyinPublicAuthorityTests(unittest.TestCase):
    def test_complete_platform_labels_are_accepted_after_idna_canonicalization(self) -> None:
        accepted = (
            "douyin.com",
            "WWW.DOUYIN.COM.",
            "v.douyin.com",
            "www.iesdouyin.com",
            "m.tiktok.com",
            "api16-normal-c-useast1a.tiktokv.com",
            "ｗｗｗ.douyin.com",
        )

        for host in accepted:
            with self.subTest(host=host):
                self.assertTrue(is_douyin_public_host(host))

    def test_confusables_and_partial_or_superdomain_matches_are_rejected(self) -> None:
        rejected = (
            "notdouyin.com",
            "douyin.com.attacker.example",
            "iesdouyin.com.attacker.example",
            "tiktok.com.evil",
            "tiktokv.com.evil",
            "douyın.com",
            "127.0.0.1",
            "",
        )

        for host in rejected:
            with self.subTest(host=host):
                self.assertFalse(is_douyin_public_host(host))

    def test_public_url_requires_https_platform_authority_without_userinfo(self) -> None:
        accepted = (
            "https://WWW.DOUYIN.COM.:443/video/1",
            "https://v.douyin.com./abc",
            "https://www.iesdouyin.com/share/video/1",
            "https://www.tiktok.com/@creator/video/1",
            "https://api16-normal-c-useast1a.tiktokv.com/resource",
        )
        rejected = (
            "http://v.douyin.com./abc",
            "https://attacker.example/?next=https://www.douyin.com/video/1",
            "https://douyin.com.attacker.example/a",
            "https://douyın.com/a",
            "https://user:pass@www.douyin.com/a",
            "https://www.douyin.com:99999/a",
            "ftp://www.douyin.com/a",
            "javascript:https://www.douyin.com/a",
            "https://127.0.0.1/a?next=douyin.com",
        )

        for url in accepted:
            with self.subTest(url=url):
                self.assertTrue(is_douyin_public_url(url))
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(is_douyin_public_url(url))


class RequesterSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_or_blind_substring_urls_are_never_fetched(self) -> None:
        transport = _RecordingTransport()
        requester, _logger, _client = _requester(transport)
        rejected = (
            "https://attacker.example/?next=https://www.douyin.com/video/1",
            "https://douyin.com.attacker.example/a",
            "https://douyın.com/a",
            "https://user:pass@www.douyin.com/a",
            "https://www.douyin.com:99999/a",
            "file://www.douyin.com/a",
            "https://127.0.0.1/a",
        )

        for url in rejected:
            with self.subTest(url=url):
                self.assertEqual(await requester.request_url(url), url)

        self.assertEqual(transport.calls, [])

    async def test_retryable_curl_failures_follow_configured_retry_contract(self) -> None:
        retryable_errors = (
            CurlError("curl-secret-must-not-be-logged"),
            RequestException("request-secret-must-not-be-logged"),
        )

        for error in retryable_errors:
            with self.subTest(error=type(error).__name__):
                transport = _RecordingTransport(error=error)
                requester, logger, _client = _requester(
                    transport,
                    max_retry=2,
                )
                with patch(
                    "app.core.lib.douyin.tools.retry.wait",
                    new_callable=AsyncMock,
                ) as retry_wait:
                    result = await requester.request_url(
                        "https://v.douyin.com/transient-curl-error"
                    )

                self.assertIsNone(result)
                self.assertEqual(len(transport.calls), 3)
                self.assertEqual(retry_wait.await_count, 2)
                rendered = repr(logger.entries)
                self.assertIn("transport request failed", rendered.lower())
                self.assertNotIn("secret-must-not-be-logged", rendered)

    async def test_builtin_timeout_follows_configured_retry_contract(self) -> None:
        transport = _RecordingTransport(
            error=TimeoutError("timeout-secret-must-not-be-logged")
        )
        requester, logger, _client = _requester(transport, max_retry=2)

        with patch(
            "app.core.lib.douyin.tools.retry.wait",
            new_callable=AsyncMock,
        ) as retry_wait:
            result = await requester.request_url(
                "https://v.douyin.com/transient-timeout"
            )

        self.assertIsNone(result)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(retry_wait.await_count, 2)
        rendered = repr(logger.entries)
        self.assertIn("transport request failed", rendered.lower())
        self.assertNotIn("timeout-secret-must-not-be-logged", rendered)

    async def test_pinned_transport_network_failure_follows_retry_contract(self) -> None:
        transport = _RecordingTransport(
            error=PinnedTransportNetworkError("network-secret-must-not-be-logged")
        )
        requester, logger, _client = _requester(transport, max_retry=2)

        with patch(
            "app.core.lib.douyin.tools.retry.wait",
            new_callable=AsyncMock,
        ) as retry_wait:
            result = await requester.request_url(
                "https://v.douyin.com/transient-pinned-network"
            )

        self.assertIsNone(result)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(retry_wait.await_count, 2)
        rendered = repr(logger.entries)
        self.assertIn("transport request failed", rendered.lower())
        self.assertNotIn("network-secret-must-not-be-logged", rendered)

    async def test_full_redirect_chain_is_delegated_once_to_pinned_transport(self) -> None:
        response = PinnedResponse(
            200,
            "https://www.douyin.com:443/video/1",
            {"Content-Type": "text/html"},
            b"page",
            (
                "https://v.douyin.com:443/abc",
                "https://www.iesdouyin.com:443/share/video/1",
                "https://www.douyin.com:443/video/1",
            ),
        )
        transport = _RecordingTransport(response)
        requester, _logger, _client = _requester(transport)

        result = await requester.request_url(
            "https://v.douyin.com/abc",
            proxy="http://127.0.0.1:8080",
        )

        self.assertEqual(result, "https://www.douyin.com:443/video/1")
        self.assertEqual(
            transport.calls,
            [
                (
                    "GET",
                    "https://v.douyin.com/abc",
                    {
                        "headers": {"User-Agent": "test-agent"},
                        "max_redirects": 0,
                    },
                )
            ],
        )

    async def test_private_redirect_policy_failure_returns_original_url(self) -> None:
        transport = _RecordingTransport(
            error=DomainPolicyViolation("redirect resolved to a private address")
        )
        requester, logger, _client = _requester(transport, max_retry=2)
        original = "https://v.douyin.com/private-redirect"

        with patch(
            "app.core.lib.douyin.tools.retry.wait",
            new_callable=AsyncMock,
        ) as retry_wait:
            self.assertEqual(await requester.request_url(original), original)

        self.assertEqual(len(transport.calls), 1)
        retry_wait.assert_not_awaited()
        self.assertNotIn("private address", repr(logger.entries))

    async def test_pinned_transport_runs_outside_the_event_loop_thread(self) -> None:
        event_loop_thread = get_ident()
        transport = _RecordingTransport()
        requester, _logger, _client = _requester(transport)

        await requester.request_url("https://v.douyin.com/thread-check")

        self.assertEqual(len(transport.thread_ids), 1)
        self.assertNotEqual(transport.thread_ids[0], event_loop_thread)

    async def test_content_modes_project_the_bounded_pinned_response(self) -> None:
        headers = {
            "Content-Type": "application/json",
            "Set-Cookie": "session=response-secret",
        }
        response = PinnedResponse(
            200,
            "https://www.douyin.com:443/video/1",
            headers,
            b'{"ok": true}',
        )
        expected = {
            "url": "https://www.douyin.com:443/video/1",
            "text": '{"ok": true}',
            "content": b'{"ok": true}',
            "json": {"ok": True},
            "headers": headers,
        }

        for content, value in expected.items():
            with self.subTest(content=content):
                requester, _logger, _client = _requester(
                    _RecordingTransport(response)
                )
                self.assertEqual(
                    await requester.request_url(
                        "https://v.douyin.com/abc",
                        content=content,
                    ),
                    value,
                )

    async def test_response_headers_and_request_credentials_are_not_logged(self) -> None:
        response = PinnedResponse(
            200,
            "https://www.douyin.com:443/video/1",
            {
                "Authorization": "Bearer response-secret",
                "Set-Cookie": "sid=response-cookie",
            },
            b"page",
        )
        requester, logger, _client = _requester(
            _RecordingTransport(response),
            headers={
                "Authorization": "Bearer request-secret",
                "Cookie": "sid=request-cookie",
            },
        )

        await requester.request_url("https://v.douyin.com/abc")

        rendered = repr(logger.entries)
        for secret in (
            "response-secret",
            "response-cookie",
            "request-secret",
            "request-cookie",
        ):
            self.assertNotIn(secret, rendered)
        self.assertNotIn("Response Headers", rendered)

    async def test_http_first_hop_omits_credentials_and_rejects_later_http_or_off_platform(self) -> None:
        class _RedirectTransport:
            def __init__(self, location: str) -> None:
                self.location = location
                self.calls: list[tuple[str, str, dict[str, Any]]] = []

            def request(self, method: str, url: str, **kwargs: Any) -> PinnedResponse:
                self.calls.append((method, url, kwargs))
                return PinnedResponse(302, url, {"Location": self.location}, b"")

        original = "http://v.douyin.com/abc"
        headers = {
            "User-Agent": "test-agent",
            "Cookie": "sid=request-cookie",
            "Authorization": "Bearer request-secret",
        }
        for location in (
            "http://www.douyin.com/video/1",
            "https://attacker.example/steal",
            "http://127.0.0.1/private",
        ):
            with self.subTest(location=location):
                transport = _RedirectTransport(location)
                requester, logger, _client = _requester(transport, headers=headers, max_retry=2)
                self.assertEqual(await requester.request_url(original), original)
                self.assertEqual(len(transport.calls), 1)
                sent = transport.calls[0][2]["headers"]
                self.assertNotIn("Cookie", sent)
                self.assertNotIn("Authorization", sent)
                self.assertEqual(sent["User-Agent"], "test-agent")
                self.assertEqual(transport.calls[0][2]["max_redirects"], 0)
                self.assertNotIn("request-cookie", repr(logger.entries))
                self.assertNotIn("request-secret", repr(logger.entries))

    async def test_https_redirect_hops_stay_on_platform_hosts(self) -> None:
        class _ChainTransport:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, dict[str, Any]]] = []

            def request(self, method: str, url: str, **kwargs: Any) -> PinnedResponse:
                self.calls.append((method, url, kwargs))
                if "v.douyin.com" in url:
                    return PinnedResponse(
                        302,
                        url,
                        {"Location": "https://www.douyin.com/video/1"},
                        b"",
                    )
                return PinnedResponse(
                    200,
                    "https://www.douyin.com:443/video/1",
                    {},
                    b"ok",
                )

        transport = _ChainTransport()
        requester, _logger, _client = _requester(
            transport,
            headers={"User-Agent": "test-agent", "Cookie": "sid=kept"},
        )

        result = await requester.request_url("https://v.douyin.com/abc")

        self.assertEqual(result, "https://www.douyin.com:443/video/1")
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(transport.calls[1][1].startswith("https://www.douyin.com/video/1"))
        self.assertEqual(transport.calls[0][2]["headers"]["Cookie"], "sid=kept")
        self.assertEqual(transport.calls[1][2]["headers"]["Cookie"], "sid=kept")
        self.assertTrue(all(call[2]["max_redirects"] == 0 for call in transport.calls))

    async def test_stop_flag_closes_client_and_skips_remaining_retries(self) -> None:
        class _StopFlag:
            def __init__(self) -> None:
                self.stopped = False

            def stop_requested(self) -> bool:
                return self.stopped

        class _StopTransport:
            def __init__(self, flag: _StopFlag) -> None:
                self.flag = flag
                self.calls = 0

            def request(self, method: str, url: str, **kwargs: Any) -> PinnedResponse:
                del method, url, kwargs
                self.calls += 1
                self.flag.stopped = True
                raise TimeoutError("timeout-secret-must-not-be-logged")

        flag = _StopFlag()
        transport = _StopTransport(flag)
        logger = _RecordingLogger()
        client = _UnusedAsyncClient()
        params = SimpleNamespace(
            logger=logger,
            max_retry=2,
            timeout=7,
            stop_requested=flag.stop_requested,
        )
        requester = Requester(params, client, {"User-Agent": "test-agent"}, transport=transport)

        with patch("app.core.lib.douyin.tools.retry.wait", new_callable=AsyncMock) as retry_wait:
            result = await requester.request_url("https://v.douyin.com/stop-check")

        self.assertIsNone(result)
        self.assertEqual(transport.calls, 1)
        self.assertTrue(client.closed)
        retry_wait.assert_not_awaited()
        self.assertNotIn("timeout-secret-must-not-be-logged", repr(logger.entries))

    async def test_aclose_preserves_existing_client_lifecycle(self) -> None:
        requester, _logger, client = _requester(_RecordingTransport())

        await requester.aclose()

        self.assertTrue(client.closed)


class RequesterLiveReflowTests(unittest.IsolatedAsyncioTestCase):
    LIVE_URL = "https://webcast.amemv.com/douyin/webcast/reflow/room-token"

    def test_reflow_token_decode_budget_accepts_safe_layers_and_fails_closed(self) -> None:
        prefix = "https://webcast.amemv.com/douyin/webcast/reflow/"

        self.assertTrue(is_douyin_live_reflow_url(f"{prefix}room%25252541"))
        self.assertFalse(is_douyin_live_reflow_url(f"{prefix}room%250Asecret"))
        self.assertFalse(is_douyin_live_reflow_url(f"{prefix}room%2525252541"))

    async def test_extractor_live_fetches_exact_reflow_page_through_pinned_transport(self) -> None:
        response = PinnedResponse(
            200,
            "https://webcast.amemv.com:443/douyin/webcast/reflow/room-token",
            {"Content-Type": "text/html"},
            br'{\"webRid\":\"987654321\"}',
        )
        transport = _RecordingTransport(response)
        requester, _logger, _client = _requester(
            transport,
            headers={
                "User-Agent": "test-agent",
                "Cookie": "sid=live-secret",
                "Authorization": "Bearer live-secret",
                "Proxy-Authorization": "Basic live-secret",
                "Host": "attacker.example",
                "Accept-Language": "zh-CN",
            },
        )
        extractor = object.__new__(Extractor)
        extractor.requester = requester

        result = await extractor.live(self.LIVE_URL)

        self.assertEqual(result, ["987654321"])
        self.assertFalse(is_douyin_public_host("webcast.amemv.com"))
        self.assertFalse(is_douyin_public_url(self.LIVE_URL))
        self.assertEqual(len(transport.calls), 1)
        method, url, kwargs = transport.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, self.LIVE_URL)
        self.assertEqual(kwargs["max_redirects"], 0)
        self.assertEqual(
            kwargs["headers"],
            {
                "User-Agent": "test-agent",
                "Accept-Language": "zh-CN",
            },
        )

    async def test_reflow_pseudo_hosts_wrong_paths_and_non_https_are_not_fetched(self) -> None:
        rejected = (
            "https://webcast.amemv.com.attacker.example/douyin/webcast/reflow/1",
            "https://notwebcast.amemv.com/douyin/webcast/reflow/1",
            "https://webcast.amemv.com/douyin/webcast/reflowevil/1",
            "https://webcast.amemv.com/douyin/webcast/other/1",
            "https://webcast.amemv.com/douyin/webcast/reflow/",
            "https://webcast.amemv.com/douyin/webcast/reflow/../other/1",
            "https://webcast.amemv.com/douyin/webcast/reflow/%2e%2e/other/1",
            "https://webcast.amemv.com/douyin/webcast/reflow/%252e%252e/other/1",
            "https://webcast.amemv.com/douyin/webcast/reflow/%252E%252E/other/1",
            "https://webcast.amemv.com/douyin/webcast/reflow/%252f..%252Fother/1",
            "https://webcast.amemv.com/douyin/webcast/reflow/%255c..%255Cother/1",
            "https://webcast.amemv.com/douyin/webcast/reflow/%25252525252e%25252525252e/other/1",
            "http://webcast.amemv.com/douyin/webcast/reflow/1",
            "https://webcast.amemv.com:444/douyin/webcast/reflow/1",
        )
        transport = _RecordingTransport()
        requester, _logger, _client = _requester(transport)

        for url in rejected:
            with self.subTest(url=url):
                self.assertEqual(
                    await requester.request_url(url, content="text"),
                    url,
                )

        self.assertEqual(transport.calls, [])

    async def test_reflow_endpoint_is_fetchable_only_for_text_projection(self) -> None:
        transport = _RecordingTransport()
        requester, _logger, _client = _requester(transport)

        for content in ("url", "content", "json", "headers", "unsupported"):
            with self.subTest(content=content):
                self.assertEqual(
                    await requester.request_url(self.LIVE_URL, content=content),
                    self.LIVE_URL,
                )

        self.assertEqual(transport.calls, [])
