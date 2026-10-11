from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.responses import JSONResponse

from app.web.http_session import HttpSessionCoordinator
from app.web.session_runtime import WebSessionCapacityError, WebSessionRegistry


class HttpSessionCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.registry = WebSessionRegistry(
            send_factory=lambda _session_id: lambda _event_type, _data=None: None,
            controller_factory=lambda _loop, _send: SimpleNamespace(current_save_dir="downloads"),
            workflow_factory=lambda _controller, _send: object(),
        )
        self.coordinator = HttpSessionCoordinator(
            session_registry=self.registry,
            session_cookie_name="ucrawl_session",
            session_token_cookie_name="ucrawl_session_token",
            csrf_cookie_name="ucrawl_csrf_token",
            session_token_header="X-Ucrawl-Session-Token",
            default_session_id="default",
        )

    @staticmethod
    def _request(*, client_host: str, cookies=None, headers=None, method="GET"):
        return SimpleNamespace(
            client=SimpleNamespace(host=client_host),
            cookies=cookies or {},
            headers=headers or {},
            method=method,
            state=SimpleNamespace(),
            url=SimpleNamespace(
                path="/api/frontend/state",
                scheme="http",
                netloc="127.0.0.1:8000",
                hostname="127.0.0.1",
            ),
        )

    async def test_remote_api_request_without_session_token_is_rejected(self) -> None:
        request = self._request(client_host="192.0.2.10")

        async def call_next(_request):
            return JSONResponse({"status": "ok"})

        response = await self.coordinator.handle(
            request,
            call_next,
        )

        self.assertEqual(response.status_code, 403)

    async def test_local_api_request_without_token_keeps_desktop_compatibility(self) -> None:
        request = self._request(client_host="127.0.0.1")

        async def call_next(_request):
            return JSONResponse({"status": "ok"})

        response = await self.coordinator.handle(
            request,
            call_next,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(request.state.session_context.has_active_http_request())

    async def test_http_context_is_released_for_exception_and_cancellation(self) -> None:
        class RequestAbort(BaseException):
            pass

        for primary in (
            RuntimeError("request failed"),
            asyncio.CancelledError("request cancelled"),
            RequestAbort("request aborted"),
        ):
            with self.subTest(error_type=type(primary).__name__):
                request = self._request(client_host="127.0.0.1")

                async def call_next(_request, *, error=primary):
                    raise error

                with self.assertRaises(type(primary)) as raised:
                    await self.coordinator.handle(request, call_next)

                self.assertIs(raised.exception, primary)
                self.assertFalse(
                    request.state.session_context.has_active_http_request()
                )

    async def test_release_failure_cannot_replace_the_request_primary_error(self) -> None:
        class HostileCleanup(BaseException):
            def __str__(self) -> str:
                raise AssertionError("cleanup must not be stringified")

        request = self._request(client_host="127.0.0.1")
        primary = RuntimeError("request failed")
        cleanup = HostileCleanup()
        original_release = self.registry.release_http_context

        def hostile_release(context):
            original_release(context)
            raise cleanup

        self.registry.release_http_context = hostile_release

        async def call_next(_request):
            raise primary

        with self.assertRaises(RuntimeError) as raised:
            await self.coordinator.handle(request, call_next)

        self.assertIs(raised.exception, primary)
        self.assertFalse(request.state.session_context.has_active_http_request())

    async def test_release_control_flow_after_success_is_propagated_exactly(self) -> None:
        class ReleaseAbort(BaseException):
            pass

        request = self._request(client_host="127.0.0.1")
        cleanup = ReleaseAbort("release interrupted")
        original_release = self.registry.release_http_context

        def aborting_release(context):
            original_release(context)
            raise cleanup

        self.registry.release_http_context = aborting_release

        async def call_next(_request):
            return JSONResponse({"status": "ok"})

        with self.assertRaises(ReleaseAbort) as raised:
            await self.coordinator.handle(request, call_next)

        self.assertIs(raised.exception, cleanup)
        self.assertFalse(request.state.session_context.has_active_http_request())

    async def test_http_context_is_atomically_pinned_and_released_on_control_flow(
        self,
    ) -> None:
        class RequestAbort(BaseException):
            pass

        request = self._request(client_host="127.0.0.1")
        primary = RequestAbort("request stopped")
        original_get_or_create = self.registry.get_or_create
        acquired_contexts = []
        release_context = Mock()

        def acquire_http_context(session_id):
            context = original_get_or_create(session_id)
            acquired_contexts.append(context)
            return context

        self.registry.acquire_http_context = acquire_http_context
        self.registry.release_http_context = release_context
        self.registry.get_or_create = Mock(
            side_effect=AssertionError("HTTP acquisition must be atomic")
        )

        async def call_next(_request):
            self.assertEqual(len(acquired_contexts), 1)
            raise primary

        with self.assertRaises(RequestAbort) as raised:
            await self.coordinator.handle(request, call_next)

        self.assertIs(raised.exception, primary)
        release_context.assert_called_once_with(acquired_contexts[0])

    async def test_capacity_exhaustion_returns_retryable_response_without_cookies(self) -> None:
        request = self._request(client_host="127.0.0.1")
        call_next_called = False

        async def call_next(_request):
            nonlocal call_next_called
            call_next_called = True
            return JSONResponse({"status": "ok"})

        original_acquire_http_context = getattr(
            self.registry,
            "acquire_http_context",
            None,
        )

        def reject_session(_session_id):
            raise WebSessionCapacityError("session capacity exhausted")

        self.registry.acquire_http_context = reject_session
        try:
            response = await self.coordinator.handle(request, call_next)
        finally:
            if original_acquire_http_context is None:
                del self.registry.acquire_http_context
            else:
                self.registry.acquire_http_context = original_acquire_http_context

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "1")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("set-cookie", response.headers)
        self.assertFalse(call_next_called)

    def test_csrf_cookie_is_accepted_as_double_submit_session_token(self) -> None:
        context = self.registry.get_or_create("session-a")
        request = self._request(
            client_host="192.0.2.10",
            cookies={
                "ucrawl_session": "session-a",
                "ucrawl_csrf_token": context.csrf_token,
            },
        )
        request.state.session_context = context

        self.assertTrue(self.coordinator.has_valid_session_token(request, context))

    def test_request_context_missing_from_middleware_fails_closed(self) -> None:
        request = self._request(client_host="127.0.0.1")
        request.state.session_id = "untrusted-fallback"

        with self.assertRaisesRegex(RuntimeError, "HTTP session context is unavailable"):
            self.coordinator.get_request_context(request)

        self.assertNotIn("untrusted-fallback", self.registry._contexts)
        self.assertNotIn("default", self.registry._contexts)


if __name__ == "__main__":
    unittest.main()
