from unittest.mock import Mock

from app.spiders.missav.spider import MissAVSpider, _ChallengeBrowserRuntime


def _scan_setup(*, max_pages=3, max_items=20):
    spider = MissAVSpider.__new__(MissAVSpider)
    spider.config = {"search_max_pages": max_pages, "max_items": max_items}
    spider.is_running = True
    spider.log = Mock()
    spider.debug_state = Mock()
    spider.interruptible_wait_for_selector = Mock(return_value=True)
    spider._require_missav_page_url = Mock(side_effect=lambda url: url)
    page = Mock(url="https://missav.ai/dm37/cn/actresses/demo?filters=individual")
    page.evaluate.return_value = [{"url": "https://missav.ai/cn/abc-123", "title": "A"}]
    link = Mock()
    link.get_attribute.return_value = "?filters=individual&page=2"
    page.query_selector.return_value = link
    runtime = _ChallengeBrowserRuntime(external_session=Mock(), page=page)
    spider._navigate_external_challenge = Mock(return_value=False)
    return spider, page, runtime, link


def test_page_limit_stops_before_opening_an_unscanned_page():
    spider, page, runtime, _ = _scan_setup(max_pages=1)
    collected = {}

    spider._scan_pages(page, collected, page_url=page.url, runtime=runtime)

    assert len(collected) == 1
    spider._navigate_external_challenge.assert_not_called()
    assert any("扫描页数上限" in call.args[0] for call in spider.log.call_args_list)


def test_pagination_follows_site_href_using_actual_page_path():
    spider, page, runtime, link = _scan_setup()
    link.get_attribute.return_value = "/dm37/cn/actresses/demo?filters=individual&page=7"

    spider._scan_pages(page, {}, page_url="https://missav.ai/cn/actresses/demo", runtime=runtime)

    assert spider._navigate_external_challenge.call_args.args[1] == (
        "https://missav.ai/dm37/cn/actresses/demo?filters=individual&page=7"
    )


def test_second_page_is_scanned_after_external_browser_reattachment():
    spider, page, runtime, _ = _scan_setup(max_pages=2)
    second = Mock(url=page.url + "&page=2")
    second.evaluate.return_value = [{"url": "https://missav.ai/cn/abc-124", "title": "B"}]
    second.query_selector.return_value = Mock()

    def navigate(current_runtime, url, *, timeout_ms):
        current_runtime.page = second
        return True

    spider._navigate_external_challenge.side_effect = navigate
    collected = {}
    spider._scan_pages(page, collected, page_url=page.url, runtime=runtime)

    assert set(collected) == {"https://missav.ai/cn/abc-123", "https://missav.ai/cn/abc-124"}
    spider._navigate_external_challenge.assert_called_once()
    second.evaluate.assert_called_once()


def test_missing_grid_does_not_scan_or_open_next_page():
    spider, page, runtime, _ = _scan_setup()
    spider.interruptible_wait_for_selector.return_value = False

    spider._scan_pages(page, {}, page_url=page.url, runtime=runtime)

    page.evaluate.assert_not_called()
    spider._navigate_external_challenge.assert_not_called()
    assert any("列表加载失败" in call.args[0] for call in spider.log.call_args_list)


def test_next_link_without_href_stops_without_synthesizing_a_url():
    spider, page, runtime, link = _scan_setup()
    link.get_attribute.return_value = None

    spider._scan_pages(page, {}, page_url=page.url, runtime=runtime)

    spider._navigate_external_challenge.assert_not_called()


def test_next_link_to_current_page_does_not_reopen_it():
    spider, page, runtime, link = _scan_setup()
    link.get_attribute.return_value = "?filters=individual#top"

    spider._scan_pages(page, {}, page_url=page.url, runtime=runtime)

    spider._navigate_external_challenge.assert_not_called()


def test_item_limit_still_stops_before_opening_next_page():
    spider, page, runtime, _ = _scan_setup(max_items=1)

    spider._scan_pages(page, {}, page_url=page.url, runtime=runtime)

    spider._navigate_external_challenge.assert_not_called()


def test_next_link_outside_missav_is_rejected_before_navigation():
    spider, page, runtime, link = _scan_setup()
    spider._require_missav_page_url = MissAVSpider._require_missav_page_url.__get__(spider)
    link.get_attribute.return_value = "https://evil.example/cn/search/demo?page=2"
    collected = {}

    spider._scan_pages(page, collected, page_url=page.url, runtime=runtime)

    assert len(collected) == 1
    spider._navigate_external_challenge.assert_not_called()
    assert spider.debug_state.call_args.args[0] == "MISSAV_LIST_PAGE_FAILED"
