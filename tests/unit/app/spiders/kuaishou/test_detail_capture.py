from __future__ import annotations

import threading
from unittest.mock import Mock

from app.spiders.kuaishou.spider import KuaishouSpider


def _spider() -> KuaishouSpider:
    spider = KuaishouSpider.__new__(KuaishouSpider)
    spider.is_running = True
    spider.config = {}
    spider.log = Mock()
    spider.debug_state = Mock()
    spider.new_trace_id = Mock(return_value="ks-share-1")
    spider.task_builder = Mock()
    spider.task_builder.build_download_meta.return_value = {"trace_id": "ks-share-1"}
    spider.emit_video = Mock()
    spider._extract_detail_title = Mock(return_value="shared work")
    spider._extract_detail_dom_media_url = Mock(return_value="")
    spider.interruptible_page_wait = Mock(return_value=True)
    spider.interruptible_playwright_reload = Mock(return_value=True)
    return spider


def _page() -> Mock:
    page = Mock()
    page.url = "https://www.kuaishou.com/short-video/3xj8abcde"
    page.locator.return_value.first = page.locator.return_value
    return page


def test_detail_capture_does_not_reload_when_media_is_unavailable() -> None:
    spider = _spider()
    page = _page()

    assert spider._capture_single_detail_page(page) is False

    spider.interruptible_playwright_reload.assert_not_called()


def test_detail_capture_returns_as_soon_as_response_event_has_media() -> None:
    spider = _spider()
    page = _page()
    handlers: dict[str, object] = {}
    page.on.side_effect = lambda event, handler: handlers.__setitem__(event, handler)
    response = Mock()
    response.url = "https://cdn.example.com/video.mp4"
    response.headers = {"content-type": "video/mp4"}
    response.request.resource_type = "media"

    def pump_events(_page, wait_ms, **_kwargs):
        assert wait_ms <= 250
        handlers["response"](response)
        return True

    spider.interruptible_page_wait.side_effect = pump_events

    assert spider._capture_single_detail_page(page) is True

    spider.emit_video.assert_called_once()
    spider.interruptible_playwright_reload.assert_not_called()


def test_media_policy_rejects_blob_and_non_playlist_transport_stream() -> None:
    spider = _spider()

    def response(url: str, content_type: str):
        item = Mock()
        item.url = url
        item.headers = {"content-type": content_type}
        item.request.resource_type = "media"
        return item

    assert spider._share_media_response_url(
        response("blob:https://www.kuaishou.com/8b6d", "video/mp4")
    ) == ""
    assert spider._share_media_response_url(
        response("https://cdn.example.com/segment.ts", "video/mp2t")
    ) == ""
    playlist = "https://cdn.example.com/index.m3u8"
    assert spider._share_media_response_url(
        response(playlist, "video/mp2t")
    ) == playlist


def test_small_playlist_replaces_earlier_small_mp4_for_same_work() -> None:
    spider = _spider()
    small_mp4 = "https://cdn.example.com/preview.mp4"
    small_playlist = "https://cdn.example.com/index.m3u8"

    chosen = spider._choose_kuaishou_stream(None, small_mp4, "video/mp4", 800)
    chosen = spider._choose_kuaishou_stream(
        chosen,
        small_playlist,
        "application/vnd.apple.mpegurl",
        300,
    )
    later_file = spider._choose_kuaishou_stream(
        chosen,
        "https://cdn.example.com/full.mp4",
        "video/mp4",
        900000,
    )

    assert chosen == ("playlist", small_playlist)
    assert later_file == chosen


def test_capture_pipeline_does_not_let_first_small_mp4_block_playlist() -> None:
    spider = _spider()
    spider._selected_indices = [0]
    spider._lock = threading.Lock()
    spider._capture_scroll_budget = Mock(return_value=1)
    spider.parser = Mock()
    spider.parser.extract_all_possible_ids.return_value = set()
    handlers: dict[str, object] = {}
    page = _page()
    page.context = Mock()
    page.on.side_effect = lambda event, handler: handlers.__setitem__(event, handler)
    page.locator.return_value.is_visible.return_value = True

    def resolve(active, _context):
        return active

    spider._resolve_active_page = resolve

    def pump(_page, _wait_ms, **_kwargs):
        if handlers:
            mp4 = Mock()
            mp4.url = "https://cdn.example.com/preview.mp4"
            mp4.headers = {"content-type": "video/mp4", "content-length": "800"}
            playlist = Mock()
            playlist.url = "https://cdn.example.com/index.m3u8"
            playlist.headers = {
                "content-type": "application/vnd.apple.mpegurl",
                "content-length": "240",
            }
            handlers["response"](mp4)
            handlers["response"](playlist)
            handlers.clear()
        return False

    spider.interruptible_page_wait.side_effect = pump

    spider._run_capture_pipeline(page, [{"title": "作品标题", "index": 0}], {})

    spider.emit_video.assert_called_once()
    assert spider.emit_video.call_args.kwargs["url"].endswith("index.m3u8")
    page.remove_listener.assert_called_once()
