"""目录租约必须覆盖爬取和搜索的完整执行，而不能只做启动时快照。"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.web.search_service import SearchRouteRuntime, WebSearchService
from app.web.workflow_route_service import WebWorkflowRouteService
from app.web.workflows import start_crawl_with_directory_lease
from app.web.ws_dispatcher import WebSocketMessageDispatcher


class _RootLease:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    def __enter__(self):
        self.entered += 1
        return (("D:/approved", (1, 2)),)

    def __exit__(self, *_args):
        self.exited += 1
        return False


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class CrawlDirectoryLeaseTests(unittest.TestCase):
    def _context(self, spider, result):
        root_lease = _RootLease()
        opened = {"path": None}

        @contextmanager
        def open_operation_directory(directory):
            opened["path"] = directory
            yield "D:/approved/out" if directory else None

        async def start_crawl(payload, *, log_error):
            del log_error
            self.assertEqual(payload.get("save_dir"), opened["path"] or None)
            if payload.get("save_dir") is None and "save_dir" in payload:
                self.payload = payload
            else:
                self.payload = payload
            return result

        context = SimpleNamespace(
            controller=SimpleNamespace(current_spider=spider),
            workflow=SimpleNamespace(start_crawl=start_crawl),
            lease_approved_root_grants_snapshot=lambda: root_lease,
            open_operation_directory=open_operation_directory,
        )
        return context, root_lease

    def test_crawl_lease_outlives_start_and_ends_with_the_spider(self):
        finished = threading.Event()
        spider = Mock()
        spider.isRunning.return_value = True
        spider.wait.side_effect = lambda *_args, **_kwargs: finished.wait(2.0)
        context, root_lease = self._context(spider, {"status": "ok"})

        result = asyncio.run(
            start_crawl_with_directory_lease(
                context,
                {"source": "douyin", "keyword": "cats", "save_dir": "D:/approved/out"},
                log_error=False,
            )
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.payload["save_dir"], "D:/approved/out")
        self.assertEqual(root_lease.entered, 1)
        self.assertEqual(root_lease.exited, 0)
        finished.set()
        self.assertTrue(_wait_until(lambda: root_lease.exited == 1))

    def test_failed_crawl_releases_the_lease_immediately(self):
        context, root_lease = self._context(None, {"status": "error", "error": "busy"})

        result = asyncio.run(
            start_crawl_with_directory_lease(
                context,
                {"source": "douyin", "keyword": "cats"},
                log_error=True,
            )
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(root_lease.exited, 1)

    def test_route_and_websocket_hold_the_crawl_lease(self):
        finished = threading.Event()
        spider = Mock()
        spider.isRunning.return_value = True
        spider.wait.side_effect = lambda *_args, **_kwargs: finished.wait(2.0)
        context, root_lease = self._context(spider, {"status": "ok"})
        route = WebWorkflowRouteService(get_request_context=Mock(return_value=context))
        dispatcher = WebSocketMessageDispatcher()
        context.send = AsyncMock()

        route_result = asyncio.run(
            route.start_crawl(
                object(),
                {"source": "douyin", "keyword": "cats", "save_dir": "D:/approved/out"},
            )
        )
        self.assertEqual(route_result["status"], "ok")
        self.assertEqual(root_lease.exited, 0)

        finished.set()
        self.assertTrue(_wait_until(lambda: root_lease.exited == 1))

        finished.clear()
        spider.isRunning.return_value = True
        ws_context, ws_lease = self._context(spider, {"status": "ok"})
        ws_context.send = AsyncMock()
        asyncio.run(
            dispatcher.handle(
                {
                    "type": "start_crawl",
                    "data": {"source": "douyin", "keyword": "cats", "save_dir": "D:/approved/out"},
                },
                ws_context,
            )
        )
        self.assertEqual(ws_lease.exited, 0)
        finished.set()
        self.assertTrue(_wait_until(lambda: ws_lease.exited == 1))


class SearchDirectoryLeaseTests(unittest.TestCase):
    def test_search_holds_directory_lease_during_execution(self):
        lease_active = False
        seen = {}

        @contextmanager
        def open_operation_directory(directory):
            nonlocal lease_active
            lease_active = True
            try:
                yield directory
            finally:
                lease_active = False

        def run_cli_search(**kwargs):
            seen.update(kwargs)
            seen["lease_active"] = lease_active
            return {"status": "ok", "items": []}

        context = SimpleNamespace(
            session_id="session-1",
            controller=SimpleNamespace(current_save_dir="D:/approved"),
            open_operation_directory=open_operation_directory,
            approved_roots_snapshot=lambda: ("D:/approved",),
        )
        service = WebSearchService(
            get_request_context=lambda _request: context,
            runtime_provider=lambda: SearchRouteRuntime(
                build_selection_strategy=lambda _selection: object(),
                merge_default_config=lambda _source, config: dict(config),
                validate_config_types=lambda _config: None,
                run_cli_search=run_cli_search,
            ),
        )

        result = asyncio.run(
            service.search(
                object(),
                {"source": "douyin", "keyword": "cats", "download": False},
            )
        )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(seen["lease_active"])
        self.assertFalse(lease_active)
        self.assertEqual(str(seen["save_dir"]).replace("\\", "/"), "D:/approved")
        roots = {
            str(root).replace("\\", "/")
            for root in seen["execution_profile"].approved_roots
        }
        self.assertTrue(any(root.endswith("D:/approved") or root.endswith("/approved") for root in roots))


if __name__ == "__main__":
    unittest.main()
