"""Capture-count outcomes drive Kuaishou completion and cancellation status."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.spiders.kuaishou.spider import KuaishouSpider


def _session(
    *,
    captured_indices: set[int],
    stop_after_first_capture: bool = False,
    abort_after_first_response: bool = False,
    abort_before_capture_flush: bool = False,
) -> KuaishouSpider:
    spider = KuaishouSpider.__new__(KuaishouSpider)
    spider.keyword = "demo"
    spider.config = {}
    spider.log = Mock()
    spider.debug_state = Mock()
    spider.is_running = True
    spider._interrupt_requested = False
    spider._selected_indices = [0, 1]
    spider._lock = threading.Lock()
    spider.parser = Mock()
    spider.parser.extract_all_possible_ids.return_value = set()
    spider.task_builder = Mock()
    spider.task_builder.build_download_meta.side_effect = (
        lambda trace_id, *_args: {"trace_id": trace_id}
    )
    spider.new_trace_id = Mock(side_effect=["trace-0", "trace-1"])
    spider.emit_video = Mock()

    page = Mock()
    page.url = "https://www.kuaishou.com/profile/demo"
    context = Mock()
    page.context = context
    handlers: dict[str, object] = {}
    page.on.side_effect = lambda event, handler: handlers.__setitem__(event, handler)
    page.remove_listener = Mock()
    locator = Mock()
    locator.first = locator
    locator.is_visible.return_value = True
    page.locator.return_value = locator
    spider._resolve_active_page = Mock(side_effect=lambda current, _context: current)
    spider._capture_scroll_budget = Mock(return_value=2)

    wait_count = 0

    def send_response(index: int) -> None:
        response = SimpleNamespace(
            url=f"https://video.example.com/{index}.mp4",
            headers={"content-type": "video/mp4", "content-length": "6000"},
        )
        handlers["response"](response)

    def wait(_page, _wait_ms: int) -> bool:
        nonlocal wait_count
        wait_count += 1
        if wait_count == 1 and abort_before_capture_flush and 0 in captured_indices:
            send_response(0)
            return False
        if wait_count == 2 and 0 in captured_indices:
            send_response(0)
        elif wait_count == 3 and 1 in captured_indices:
            send_response(1)
        if wait_count == 2 and stop_after_first_capture:
            spider.is_running = False
        return not (wait_count == 2 and abort_after_first_response)

    spider.interruptible_page_wait = Mock(side_effect=wait)

    spider._entry_url_for_login = Mock(return_value="https://www.kuaishou.com/")
    spider._public_domain_policy_engine = Mock()
    spider._playwright_launch_kwargs = Mock(return_value={"headless": True})
    spider._track_playwright_browser = Mock()
    spider._create_browser_context = Mock(return_value=context)
    context.new_page.return_value = page
    spider._ensure_login = Mock(return_value=True)
    spider._navigate_to_target_page = Mock(return_value=page)
    spider._wait_for_video_list = Mock(return_value=True)
    spider._scan_video_cards = Mock(return_value=2)
    spider.revive_for_partial_selection = Mock(return_value=True)
    items = [{"title": "作品 0", "index": 0}, {"title": "作品 1", "index": 1}]
    spider._extract_items_for_dialog = Mock(return_value=(items, {}))
    spider._collect_selected_indices = Mock(return_value=[0, 1])
    spider._close_playwright_session = Mock()

    return spider


@pytest.mark.parametrize(
    ("captured_indices", "stop", "expected_result", "expected_emits"),
    [
        (set(), False, "failed", 0),
        ({0}, False, "partial", 1),
        ({0, 1}, False, "completed", 2),
        ({0}, True, "stopped", 1),
    ],
)
def test_browser_session_reports_actual_capture_outcome(
    captured_indices: set[int],
    stop: bool,
    expected_result: str,
    expected_emits: int,
) -> None:
    spider = _session(
        captured_indices=captured_indices,
        stop_after_first_capture=stop,
    )

    result = spider._run_browser_session(
        Mock(),
        "ks_auth.json",
        headless=True,
        allow_manual_login=False,
    )

    assert result == expected_result
    assert spider.emit_video.call_count == expected_emits
    messages = [str(call.args[0]) for call in spider.log.call_args_list if call.args]
    if expected_result == "failed":
        assert any("未捕获到可下载媒体流" in message for message in messages)
    if expected_result == "partial":
        assert any("已提交 1/2" in message for message in messages)


def test_capture_finish_summary_includes_task_emitted_by_final_flush() -> None:
    spider = _session(captured_indices={0}, abort_after_first_response=True)

    result = spider._run_browser_session(
        Mock(),
        "ks_auth.json",
        headless=True,
        allow_manual_login=False,
    )

    assert result == "partial"
    finish_call = next(
        call
        for call in spider.debug_state.call_args_list
        if call.kwargs.get("action") == "capture_pipeline_finish"
    )
    assert finish_call.kwargs["details"]["submitted_count"] == 1
    assert spider.emit_video.call_count == 1
    assert any(
        "已提交 1/2" in str(call.args[0])
        for call in spider.log.call_args_list
        if call.args
    )


def test_final_flush_emit_failure_is_not_counted_or_reported_as_submitted() -> None:
    spider = _session(
        captured_indices={0},
        abort_before_capture_flush=True,
    )
    spider.emit_video.side_effect = RuntimeError("signal no longer available")

    result = spider._run_browser_session(
        Mock(),
        "ks_auth.json",
        headless=True,
        allow_manual_login=False,
    )

    assert result == "failed"
    assert spider.emit_video.call_count == 1
    finish_call = next(
        call
        for call in spider.debug_state.call_args_list
        if call.kwargs.get("action") == "capture_pipeline_finish"
    )
    assert finish_call.kwargs["details"]["submitted_count"] == 0
    assert not any(
        call.kwargs.get("action") == "emit_download_task"
        for call in spider.debug_state.call_args_list
    )
    assert not any(
        "加入下载队列" in str(call.args[0])
        for call in spider.log.call_args_list
        if call.args
    )


@pytest.mark.parametrize(
    ("session_result", "expected_success"),
    [("partial", True), ("failed", False), ("stopped", False)],
)
def test_list_session_success_flag_matches_capture_outcome(
    session_result: str,
    expected_success: bool,
) -> None:
    spider = KuaishouSpider.__new__(KuaishouSpider)
    spider.keyword = "demo"
    spider.config = {}
    spider.log = Mock()
    spider.is_running = True
    spider._interrupt_requested = False
    spider._extract_first_url = Mock(return_value="demo")
    spider._is_short_share_url = Mock(return_value=False)
    spider._normalize_keyword = Mock(return_value="demo")
    spider._is_detail_url = Mock(return_value=False)
    spider._browser_headless = Mock(return_value=True)
    spider._run_browser_session = Mock(return_value=session_result)
    spider._emit_finished = Mock()

    with patch("app.spiders.kuaishou.spider.sync_playwright"):
        spider.run()

    assert spider.run_succeeded is expected_success
