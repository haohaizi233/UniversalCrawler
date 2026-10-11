from __future__ import annotations

import threading
from unittest.mock import Mock

from playwright.sync_api import Error as PlaywrightError

from app.spiders.kuaishou.spider import KuaishouSpider


def _spider(keyword: str) -> KuaishouSpider:
    spider = KuaishouSpider.__new__(KuaishouSpider)
    spider.keyword = keyword
    spider.config = {"timeout": 60}
    spider.log = Mock()
    spider.is_running = True
    spider._goto_with_retry = Mock(return_value=True)
    return spider


def test_target_navigation_reuses_equivalent_current_page() -> None:
    target = "https://www.kuaishou.com/profile/demo?source=SEARCH&tab=video"
    spider = _spider(target)
    page = Mock()
    page.url = "https://www.kuaishou.com/profile/demo?tab=video&source=SEARCH"

    result = spider._navigate_to_target_page(page, Mock())

    assert result is page
    spider._goto_with_retry.assert_not_called()


def test_keyword_search_uses_direct_video_results_route() -> None:
    spider = _spider("测试主播")
    page = Mock()
    page.url = "https://www.kuaishou.com/"

    result = spider._search_keyword_via_site(page, spider.keyword)

    assert result is page
    spider._goto_with_retry.assert_called_once_with(
        page,
        "https://www.kuaishou.com/search/video?searchKey=%E6%B5%8B%E8%AF%95%E4%B8%BB%E6%92%AD",
        description="打开快手搜索页",
    )
    page.locator.assert_not_called()


def test_keyword_search_reuses_result_page_loaded_during_login() -> None:
    spider = _spider("测试 主播&1")
    page = Mock()
    page.url = (
        "https://www.kuaishou.com/search/video?"
        "searchKey=%E6%B5%8B%E8%AF%95+%E4%B8%BB%E6%92%AD%261"
    )

    result = spider._search_keyword_via_site(page, spider.keyword)

    assert result is page
    spider._goto_with_retry.assert_not_called()


def test_plain_keyword_preloads_search_results_for_login_check() -> None:
    spider = _spider("测试 主播&1")

    assert spider._entry_url_for_login() == (
        "https://www.kuaishou.com/search/video?"
        "searchKey=%E6%B5%8B%E8%AF%95+%E4%B8%BB%E6%92%AD%261"
    )


def test_keyword_result_navigation_waits_for_commit_with_bounded_timeout() -> None:
    spider = _spider("测试主播")
    spider.__dict__.pop("_goto_with_retry", None)
    spider.config["timeout"] = 90
    spider.interruptible_playwright_goto = Mock(return_value=True)
    spider.interruptible_page_wait = Mock(return_value=True)
    page = Mock()
    target = spider._keyword_search_url(spider.keyword)

    assert spider._goto_with_retry(page, target, description="关键词结果页") is True

    spider.interruptible_playwright_goto.assert_called_once_with(
        page,
        target,
        timeout=spider.SEARCH_NAVIGATION_TIMEOUT_MS,
        wait_until="commit",
    )


def test_visible_session_preflights_target_before_browser_launch() -> None:
    target = "https://www.kuaishou.com/profile/demo"
    spider = _spider(target)
    spider._playwright_launch_kwargs = Mock(return_value={"headless": False})
    spider._entry_url_for_login = Mock(return_value=target)
    spider._ensure_login = Mock(return_value=False)
    spider._track_playwright_browser = Mock()
    spider._close_tracked_playwright_browser = Mock()
    policy = Mock()
    spider._public_domain_policy_engine = Mock(return_value=policy)
    playwright = Mock()
    browser = Mock()
    events: list[str] = []
    policy.require_public_url.side_effect = lambda _url: events.append("preflight")
    playwright.chromium.launch.side_effect = lambda **_kwargs: (
        events.append("launch") or browser
    )
    spider._create_browser_context = Mock()

    spider._run_browser_session(
        playwright,
        "ks_auth.json",
        headless=False,
        allow_manual_login=True,
    )

    assert events[:2] == ["preflight", "launch"]
    policy.require_public_url.assert_called_once_with(target)


def test_manual_login_success_does_not_force_navigation_to_homepage() -> None:
    spider = _spider("demo")
    spider._goto_with_retry = Mock(return_value=False)
    spider._navigation_error_reason = Mock(return_value=None)
    spider._user_cookie_values = Mock(return_value=set())
    spider._open_login_entry = Mock()
    spider._wait_for_manual_login = Mock(return_value=True)
    spider.interruptible_playwright_goto = Mock(return_value=True)
    page = Mock()

    assert spider._ensure_login(page, Mock(), "missing-auth.json") is True

    spider.interruptible_playwright_goto.assert_not_called()


def test_profile_search_does_not_recurse_into_blank_popup() -> None:
    spider = _spider("demo")
    spider._switch_search_to_user_tab = Mock()
    spider._has_video_list = Mock(return_value=False)
    spider.interruptible_page_wait = Mock(return_value=True)
    page = Mock()
    page.url = "https://www.kuaishou.com/search/author?searchKey=demo"
    page.is_closed.return_value = False
    blank_popup = Mock()
    blank_popup.url = "about:blank"
    blank_popup.is_closed.return_value = False

    def close_blank() -> None:
        blank_popup.is_closed.return_value = True

    blank_popup.close.side_effect = close_blank
    context = Mock()
    context.pages = [page]

    hidden = Mock()
    hidden.first = hidden
    hidden.is_visible.return_value = False
    user_link = Mock()
    user_link.first = user_link
    user_link.is_visible.return_value = True
    user_link.inner_text.return_value = "demo"
    user_link.click.side_effect = lambda: context.pages.append(blank_popup)

    def locator(selector: str):
        if selector in {
            ".card-item .detail-user-name",
            "a[href*='/profile/']",
            "[class*='user-name']",
            "[class*='author'] a",
        }:
            return user_link
        return hidden

    page.locator.side_effect = locator
    spider._locator_visible = lambda item: item.is_visible()

    assert spider._open_profile_from_search_results(page, context, "demo") is None

    user_link.click.assert_called_once()
    spider._has_video_list.assert_not_called()
    blank_popup.bring_to_front.assert_not_called()
    blank_popup.close.assert_called_once()


def test_failed_search_navigation_closes_new_page() -> None:
    spider = _spider("demo")
    page = Mock()
    page.url = "https://www.kuaishou.com/search/author?searchKey=demo"
    popup = Mock()
    popup.url = "about:blank"
    popup.is_closed.return_value = False
    popup.wait_for_url.side_effect = PlaywrightError("navigation failed")
    context = Mock()
    context.pages = [page, popup]

    assert spider._page_after_search_click(page, context, [page], page.url) == (None, True)

    popup.close.assert_called_once()


def test_search_click_closes_popup_when_playwright_error_follows_open() -> None:
    spider = _spider("demo")
    spider._switch_search_to_user_tab = Mock()
    spider.interruptible_page_wait = Mock(return_value=True)
    page = Mock()
    page.url = "https://www.kuaishou.com/search/author?searchKey=demo"
    page.is_closed.return_value = False
    popup = Mock()
    popup.is_closed.return_value = False
    context = Mock()
    context.pages = [page]
    visible_calls = {"count": 0}

    def visible(_item) -> bool:
        visible_calls["count"] += 1
        return visible_calls["count"] == 1

    spider._locator_visible = visible
    name_link = Mock()
    name_link.first = name_link

    def click() -> None:
        context.pages.append(popup)
        raise PlaywrightError("popup opened then click failed")

    name_link.click.side_effect = click
    page.locator.return_value = name_link

    assert spider._open_profile_from_search_results(page, context, "demo") is None

    popup.close.assert_called()


def test_browser_navigation_whitelist_checks_each_document_hop() -> None:
    spider = _spider("https://www.kuaishou.com/")
    handlers: list[object] = []
    context = Mock()
    context.route.side_effect = lambda _pattern, handler: handlers.append(handler)
    page = Mock()
    page.context = context

    spider._ensure_playwright_public_route(page)

    def invoke(url: str, *, navigation: bool, resource_type: str):
        route = Mock()
        request = Mock()
        request.url = url
        request.is_navigation_request.return_value = navigation
        request.resource_type = resource_type
        handlers[-1](route, request)
        return route

    blocked = invoke(
        "https://evil.example/phish",
        navigation=True,
        resource_type="document",
    )
    blocked.abort.assert_called_once_with("blockedbyclient")
    blocked.fallback.assert_not_called()
    assert spider._kuaishou_navigation_blocked is True

    allowed = invoke(
        "https://www.chenzhongtech.com/short-video/1",
        navigation=True,
        resource_type="document",
    )
    allowed.fallback.assert_called_once()
    allowed.abort.assert_not_called()

    media = invoke(
        "https://cdn.example.com/video.mp4",
        navigation=False,
        resource_type="media",
    )
    media.fallback.assert_called_once()
    media.abort.assert_not_called()

    foreign_document = invoke(
        "javascript:alert(1)",
        navigation=True,
        resource_type="document",
    )
    foreign_document.abort.assert_called_once_with("blockedbyclient")
    foreign_document.continue_.assert_not_called()
    foreign_document.fallback.assert_not_called()


def test_goto_rejects_off_whitelist_target_without_calling_browser() -> None:
    spider = _spider("https://evil.example/phish")
    spider.__dict__.pop("_goto_with_retry", None)
    spider.interruptible_playwright_goto = Mock(return_value=True)
    page = Mock()

    assert spider._goto_with_retry(
        page,
        "https://evil.example/phish",
        description="打开快手目标页",
    ) is False

    spider.interruptible_playwright_goto.assert_not_called()


def test_scan_ends_after_repeated_stalls() -> None:
    spider = _spider("demo")
    spider._max_items_limit = Mock(return_value=100)
    spider.interruptible_page_wait = Mock(return_value=True)
    page = Mock()
    page.viewport_size = None

    def locator(selector: str):
        item = Mock()
        if selector == ".photo-card, .video-card":
            item.count.return_value = 0
            return item
        item.count.return_value = 0
        item.first.is_visible.return_value = False
        return item

    page.locator.side_effect = locator

    assert spider._scan_video_cards(page) == 0

    assert page.evaluate.call_count == spider.LIST_SCAN_STALL_LIMIT
    page.evaluate.assert_called_with("window.scrollBy(0, 800)")
    assert any("结束扫描" in str(call.args[0]) for call in spider.log.call_args_list)


def test_scan_breaks_immediately_when_wait_is_interrupted() -> None:
    spider = _spider("demo")
    spider._max_items_limit = Mock(return_value=100)
    spider.interruptible_page_wait = Mock(return_value=False)
    page = Mock()
    page.viewport_size = None
    page.locator.return_value.count.return_value = 1

    spider._scan_video_cards(page)

    page.evaluate.assert_called_once_with("window.scrollBy(0, 800)")
    page.mouse.wheel.assert_not_called()
    page.locator.assert_not_called()


def test_scan_checks_running_flag_before_scroll() -> None:
    spider = _spider("demo")
    spider._max_items_limit = Mock(return_value=100)
    page = Mock()
    page.viewport_size = {"width": 20, "height": 20}
    page.mouse.move.side_effect = lambda *_args: setattr(spider, "is_running", False)

    spider._scan_video_cards(page)

    page.evaluate.assert_not_called()
    page.mouse.wheel.assert_not_called()


def test_capture_checks_running_flag_before_keypress() -> None:
    spider = _spider("demo")
    spider._selected_indices = [0]
    spider._lock = threading.Lock()
    spider._capture_scroll_budget = Mock(return_value=2)
    spider.parser = Mock()
    spider.parser.extract_all_possible_ids.return_value = set()
    spider.new_trace_id = Mock(return_value="trace")
    spider.debug_state = Mock()
    spider.emit_video = Mock()
    spider.task_builder = Mock()
    visits = {"count": 0}

    def resolve(page, _context):
        visits["count"] += 1
        if visits["count"] > 1:
            spider.is_running = False
        return page

    spider._resolve_active_page = resolve
    spider.interruptible_page_wait = Mock(return_value=True)
    page = Mock()
    page.context = Mock()
    page.locator.return_value.first = page.locator.return_value
    page.locator.return_value.is_visible.return_value = True

    spider._run_capture_pipeline(page, [{"title": "作品", "index": 0}], {})

    page.keyboard.press.assert_not_called()
    page.remove_listener.assert_called_once()


def test_share_session_closes_context_on_creating_thread() -> None:
    spider = _spider("https://www.kuaishou.com/short-video/1")
    spider._playwright_launch_kwargs = Mock(return_value={})
    spider._create_browser_context = Mock()
    spider._goto_with_retry = Mock(return_value=False)
    spider._login_prompt_visible = Mock(return_value=False)
    playwright = Mock()
    browser = playwright.chromium.launch.return_value
    context = spider._create_browser_context.return_value
    context.pages = []
    page = context.new_page.return_value
    closed_on: list[int] = []
    context.close.side_effect = lambda: closed_on.append(threading.get_ident())
    browser.close.side_effect = lambda: closed_on.append(threading.get_ident())

    assert spider._run_share_browser_session(playwright, "ks_auth.json") == "failed"

    context.close.assert_called_once()
    browser.close.assert_called()
    page.remove_listener.assert_called_once()
    assert closed_on == [threading.get_ident(), threading.get_ident()]
