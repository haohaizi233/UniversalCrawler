from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, call, patch

from playwright.sync_api import Error as PlaywrightError

from app.spiders.missav.challenge_session import BrowserAttachment, SystemBrowserUnavailable
from app.spiders.missav.local_proxy import chromium_proxy_launch_args
from app.spiders.missav.parser import MissAVParser
from app.spiders.missav.spider import MissAVSpider, _ChallengeBrowserRuntime


def _spider() -> MissAVSpider:
    spider = MissAVSpider.__new__(MissAVSpider)
    spider.config = {}
    spider.log = Mock()
    spider.is_running = True
    return spider


def test_headed_missav_prefers_system_chrome_channel() -> None:
    spider = _spider()
    spider._playwright_launch_kwargs = Mock(
        return_value={"headless": False, "args": ["--disable-blink-features=AutomationControlled"]}
    )
    playwright = Mock()
    browser = Mock(version="150.0.7871.124")
    playwright.chromium.launch.return_value = browser

    result = spider._launch_challenge_browser(playwright, headless=False)

    assert result is browser
    playwright.chromium.launch.assert_called_once_with(
        headless=False,
        args=["--disable-blink-features=AutomationControlled", "--no-proxy-server"],
        channel="chrome",
    )


def test_headed_missav_falls_back_from_system_channels_to_bundled_browser() -> None:
    spider = _spider()
    launch_kwargs = {"headless": False, "args": []}
    spider._playwright_launch_kwargs = Mock(return_value=launch_kwargs)
    playwright = Mock()
    bundled = Mock(version="143.0.7499.4")
    playwright.chromium.launch.side_effect = [
        PlaywrightError("chrome unavailable"),
        PlaywrightError("edge unavailable"),
        bundled,
    ]

    result = spider._launch_challenge_browser(playwright, headless=False)

    assert result is bundled
    expected = {"headless": False, "args": ["--no-proxy-server"]}
    assert playwright.chromium.launch.call_args_list == [
        call(**expected, channel="chrome"),
        call(**expected, channel="msedge"),
        call(**expected),
    ]


def test_missav_context_preserves_native_user_agent_and_web_apis() -> None:
    spider = _spider()
    spider._playwright_context_kwargs = Mock(
        return_value={
            "user_agent": "Mozilla/5.0 Chrome/135.0.0.0",
            "locale": "zh-CN",
            "service_workers": "block",
        }
    )
    spider._apply_stealth_to_context = Mock()
    browser = Mock()
    context = Mock()
    browser.new_context.return_value = context

    result = spider._create_challenge_context(browser)

    assert result is context
    browser.new_context.assert_called_once_with(locale="zh-CN", service_workers="block")
    spider._apply_stealth_to_context.assert_not_called()


def test_missav_detects_chinese_cloudflare_unsupported_page() -> None:
    spider = _spider()
    page = Mock()
    page.title.return_value = "请稍候..."
    page.locator.return_value.inner_text.return_value = (
        "正在进行安全验证\n浏览器不支持\n您的浏览器不支持所需的安全验证"
    )

    assert spider._cloudflare_challenge_state(page) == "unsupported"


def test_missav_waits_until_cloudflare_challenge_clears() -> None:
    spider = _spider()
    spider._cloudflare_challenge_state = Mock(side_effect=["pending", "pending", "clear"])
    spider.interruptible_sleep = Mock(return_value=True)

    assert spider._wait_for_cloudflare_challenge(Mock(), timeout_ms=5000) is True
    assert spider.interruptible_sleep.call_count == 2


def test_missav_stops_when_cloudflare_rejects_browser() -> None:
    spider = _spider()
    spider._cloudflare_challenge_state = Mock(return_value="unsupported")
    spider.interruptible_sleep = Mock(return_value=True)

    assert spider._wait_for_cloudflare_challenge(Mock(), timeout_ms=5000) is False
    spider.interruptible_sleep.assert_not_called()
    assert "不支持" in spider.log.call_args.args[0]


def test_headed_runtime_waits_for_challenge_before_starting_playwright() -> None:
    spider = _spider()
    spider._browser_headless = Mock(return_value=False)
    spider._configured_timeout_ms = Mock(return_value=60_000)
    events: list[str] = []

    session = Mock()
    session.browser_executable = Path("chrome.exe")
    session.start.side_effect = lambda _url: events.append("start")
    session.wait_for_ready_page.side_effect = lambda *_args, **_kwargs: (
        events.append("ready")
    )
    browser = Mock()
    context = Mock()
    page = Mock()
    session.attach.side_effect = lambda *_args, **_kwargs: (
        events.append("attach")
        or BrowserAttachment(browser=browser, context=context, page=page)
    )
    playwright = Mock()
    playwright_manager = Mock()
    playwright_manager.start.side_effect = lambda: (
        events.append("playwright") or playwright
    )

    with (
        patch(
            "app.spiders.missav.spider.resolve_challenge_browser",
            return_value=Path("chrome.exe"),
        ),
        patch(
            "app.spiders.missav.spider.ExternalChromeChallengeSession",
            return_value=session,
        ),
        patch(
            "app.spiders.missav.spider.sync_playwright",
            return_value=playwright_manager,
        ),
    ):
        spider._require_missav_page_url = Mock(side_effect=lambda url: url)
        with spider._challenge_browser_runtime(
            "https://missav.ai/cn/search/CAWD-377",
            None,
        ) as runtime:
            assert runtime.page is page
    spider._require_missav_page_url.assert_called_with("https://missav.ai/cn/search/CAWD-377")

    assert events[:4] == ["start", "ready", "playwright", "attach"]


def test_external_second_navigation_disconnects_before_opening_url() -> None:
    spider = _spider()
    events: list[str] = []
    session = Mock()
    session.open_url.side_effect = lambda _url: events.append("open")
    runtime = _ChallengeBrowserRuntime(
        playwright=Mock(),
        browser=Mock(),
        context=Mock(),
        page=Mock(),
        external_session=session,
    )
    spider._disconnect_external_browser = Mock(
        side_effect=lambda _runtime: events.append("disconnect")
    )
    spider._wait_for_external_challenge = Mock(
        side_effect=lambda *_args, **_kwargs: events.append("ready") or True
    )
    spider._attach_external_browser = Mock(
        side_effect=lambda *_args, **_kwargs: events.append("attach") or True
    )

    spider._require_missav_page_url = Mock(side_effect=lambda url: url)
    target = "https://missav.ai/cn/search/CAWD-377?filters=chinese-subtitle"
    assert (
        spider._navigate_external_challenge(
            runtime,
            target,
            timeout_ms=60_000,
        )
        is True
    )
    spider._require_missav_page_url.assert_called_once_with(target)
    assert events == ["disconnect", "open", "ready", "attach"]


def test_disconnect_external_browser_does_not_close_chrome() -> None:
    spider = _spider()
    playwright = Mock()
    browser = Mock()
    runtime = _ChallengeBrowserRuntime(
        playwright=playwright,
        browser=browser,
        context=Mock(),
        page=Mock(),
        external_session=Mock(),
    )
    spider._clear_playwright_browser = Mock()
    spider._stop_tracked_playwright_instance = Mock()

    spider._disconnect_external_browser(runtime)

    browser.close.assert_not_called()
    spider._clear_playwright_browser.assert_called_once_with(browser)
    spider._stop_tracked_playwright_instance.assert_called_once_with(playwright)
    assert runtime.playwright is None
    assert runtime.browser is None
    assert runtime.context is None
    assert runtime.page is None


def test_player_warning_is_not_mistaken_for_cloudflare_rejection() -> None:
    spider = _spider()
    page = Mock()
    page.title.return_value = "CAWD-377 | MissAV"
    page.locator.return_value.inner_text.return_value = (
        "Video player error: browser not supported"
    )

    assert spider._cloudflare_challenge_state(page) == "clear"


def test_visible_and_headless_modes_share_proxy_launch_args() -> None:
    spider = _spider()
    spider._playwright_launch_kwargs = Mock(
        side_effect=lambda **kwargs: {
            "headless": kwargs["headless"],
            "args": list(kwargs.get("args") or []),
            "proxy": {"server": "http://127.0.0.1:1"},
        }
    )
    playwright = Mock()
    playwright.chromium.launch.return_value = Mock(version="150.0")
    proxy = "http://127.0.0.1:7890"

    spider._launch_challenge_browser(playwright, headless=False, proxy_server=proxy)
    spider._launch_challenge_browser(playwright, headless=True, proxy_server=proxy)

    expected = chromium_proxy_launch_args(proxy)
    headed = playwright.chromium.launch.call_args_list[0].kwargs
    headless = playwright.chromium.launch.call_args_list[1].kwargs
    assert [item for item in headed["args"] if item.startswith("--proxy-")] == expected
    assert [item for item in headless["args"] if item.startswith("--proxy-")] == expected
    assert "proxy" not in headed
    assert "proxy" not in headless


def test_external_reopen_does_not_poll_challenge_dom() -> None:
    spider = _spider()
    spider._require_missav_page_url = Mock(side_effect=lambda url: url)
    spider._navigate_external_challenge = Mock(return_value=True)
    spider._wait_for_cloudflare_challenge = Mock(return_value=True)
    spider.interruptible_playwright_goto = Mock(return_value=True)
    page = Mock()
    runtime = _ChallengeBrowserRuntime(external_session=Mock(), page=page)

    assert (
        spider._reopen_missav_page(
            runtime,
            page,
            "https://missav.ai/cn/actresses/demo",
            timeout_ms=1000,
            wait_for_challenge=True,
        )
        is page
    )
    spider._navigate_external_challenge.assert_called_once()
    spider._wait_for_cloudflare_challenge.assert_not_called()
    spider.interruptible_playwright_goto.assert_not_called()


def test_actress_navigation_uses_validated_url_instead_of_click() -> None:
    spider = _spider()
    spider.parser = MissAVParser()
    spider.interruptible_sleep = Mock(return_value=True)
    spider._require_missav_page_url = Mock(side_effect=lambda url: url)
    spider._wait_for_cloudflare_challenge = Mock(return_value=True)
    link = Mock()
    link.get_attribute.return_value = "/cn/actresses/demo"
    link.is_visible.return_value = True
    page = Mock()
    page.query_selector_all.return_value = [link]
    runtime = _ChallengeBrowserRuntime(external_session=Mock(), page=page)

    def navigate(current_runtime, url, timeout_ms):
        assert url.startswith("https://missav.ai/")
        current_runtime.page = page
        return True

    spider._navigate_external_challenge = Mock(side_effect=navigate)

    opened, list_url = spider._open_actress_catalog(
        runtime,
        page,
        "https://missav.ai/cn/search/demo",
        False,
        1000,
    )

    assert opened is page
    assert list_url == "https://missav.ai/cn/actresses/demo"
    link.click.assert_not_called()
    spider._wait_for_cloudflare_challenge.assert_not_called()
    spider._require_missav_page_url.assert_any_call("https://missav.ai/cn/actresses/demo")


def test_chinese_filter_rewrites_query_of_validated_url() -> None:
    spider = _spider()
    spider.parser = MissAVParser()

    filtered = spider._chinese_verification_url(
        "https://missav.ai/cn/search/demo?page=2&filters=individual"
    )

    assert filtered is not None
    assert filtered.startswith("https://missav.ai/cn/search/demo?")
    assert "filters=individual%2Cchinese-subtitle" in filtered or "chinese-subtitle" in filtered
    assert "page=" not in filtered
    assert (
        spider._chinese_verification_url(
            "https://missav.ai/cn/search/demo?filters=chinese-subtitle"
        )
        is None
    )


def test_list_scan_accepts_only_missav_hosts() -> None:
    spider = _spider()
    spider.is_running = True
    spider.config = {}
    page = Mock()
    page.url = "https://evil.example/cn/search/ipx"
    page.evaluate.return_value = [
        {"url": "https://missav.ai/cn/abc-123", "title": "kept"},
        {"url": "https://evil.example/cn/abc-999", "title": "dropped"},
        {"url": "https://missav.ai.evil.example/cn/abc-998", "title": "suffix"},
    ]
    page.query_selector.return_value = None
    collected: dict[str, str] = {}

    spider._scan_pages(page, collected, page_url="https://missav.ai/cn/search/ipx")

    assert collected == {"https://missav.ai/cn/abc-123": "kept"}


def test_external_pagination_does_not_poll_challenge_dom() -> None:
    spider = _spider()
    spider.is_running = True
    spider.config = {"timeout": 30}
    spider._require_missav_page_url = Mock(side_effect=lambda url: url)
    spider._wait_for_cloudflare_challenge = Mock(return_value=True)
    spider.interruptible_playwright_goto = Mock(return_value=True)
    spider.interruptible_wait_for_selector = Mock(return_value=True)
    page = Mock()
    page.evaluate.side_effect = [
        [{"url": "https://missav.ai/cn/abc-123", "title": "A"}],
        [],
    ]
    page.query_selector.side_effect = [object(), None]
    runtime = _ChallengeBrowserRuntime(external_session=Mock(), page=page)

    def navigate(current_runtime, url, timeout_ms):
        assert url == "https://missav.ai/cn/search/ipx?page=2"
        current_runtime.page = page
        return True

    spider._navigate_external_challenge = Mock(side_effect=navigate)
    collected: dict[str, str] = {}

    spider._scan_pages(
        page,
        collected,
        page_url="https://missav.ai/cn/search/ipx",
        runtime=runtime,
    )

    assert collected == {"https://missav.ai/cn/abc-123": "A"}
    spider._navigate_external_challenge.assert_called_once()
    spider._wait_for_cloudflare_challenge.assert_not_called()
    spider.interruptible_playwright_goto.assert_not_called()


def test_connected_page_does_not_goto_when_challenge_may_restart() -> None:
    spider = _spider()
    spider._require_missav_page_url = Mock(side_effect=lambda url: url)
    spider.interruptible_playwright_goto = Mock(return_value=True)
    page = Mock()
    runtime = _ChallengeBrowserRuntime(external_session=None, page=page)

    assert (
        spider._reopen_missav_page(
            runtime,
            page,
            "https://missav.ai/cn/search/demo?filters=chinese-subtitle",
            timeout_ms=1000,
            wait_for_challenge=True,
        )
        is None
    )
    spider.interruptible_playwright_goto.assert_not_called()
    assert any("不会打开已连接页面" in str(call.args[0]) for call in spider.log.call_args_list)


def test_headless_fallback_does_not_open_connected_page() -> None:
    spider = _spider()
    spider._browser_headless = Mock(return_value=True)
    spider._configured_timeout_ms = Mock(return_value=1000)
    spider._require_missav_page_url = Mock(side_effect=lambda url: url)
    spider.interruptible_playwright_goto = Mock(return_value=True)

    with (
        patch(
            "app.spiders.missav.spider.resolve_challenge_browser",
            side_effect=SystemBrowserUnavailable("missing"),
        ),
        patch("app.spiders.missav.spider.sync_playwright") as playwright,
    ):
        with spider._challenge_browser_runtime("https://missav.ai/cn/search/demo", None) as runtime:
            assert runtime.page is None
            assert runtime.external_session is None

    playwright.assert_not_called()
    spider.interruptible_playwright_goto.assert_not_called()


def test_pagination_without_external_session_does_not_goto() -> None:
    spider = _spider()
    spider.is_running = True
    spider.config = {"timeout": 30, "max_items": 20, "search_max_pages": 2}
    spider._require_missav_page_url = Mock(side_effect=lambda url: url)
    spider.interruptible_playwright_goto = Mock(return_value=True)
    spider.interruptible_wait_for_selector = Mock(return_value=True)
    page = Mock()
    page.evaluate.return_value = [{"url": "https://missav.ai/cn/abc-123", "title": "A"}]
    page.query_selector.return_value = object()
    runtime = _ChallengeBrowserRuntime(external_session=None, page=page)
    collected: dict[str, str] = {}

    spider._scan_pages(
        page,
        collected,
        page_url="https://missav.ai/cn/search/ipx",
        runtime=runtime,
    )

    assert collected == {"https://missav.ai/cn/abc-123": "A"}
    spider.interruptible_playwright_goto.assert_not_called()


def _prepare_run(spider: MissAVSpider) -> None:
    spider.keyword = "ipx"
    spider.config = {}
    spider.parser = MissAVParser()
    spider.log = Mock()
    spider.debug_state = Mock()
    spider._emit_finished = Mock()
    spider._effective_proxy_server = Mock(return_value=None)
    spider._playwright_browser = None
    spider._playwright_pw = None
    spider._close_tracked_playwright_browser = Mock()


def test_empty_scan_marks_run_succeeded() -> None:
    spider = _spider()
    _prepare_run(spider)
    page = Mock()
    page.evaluate.return_value = "ua"
    runtime = _ChallengeBrowserRuntime(page=page)

    class _Runtime:
        def __enter__(self):
            return runtime

        def __exit__(self, *_args):
            return False

    spider._challenge_browser_runtime = lambda *_args, **_kwargs: _Runtime()
    spider._open_actress_catalog = Mock(
        return_value=(page, "https://missav.ai/cn/search/ipx")
    )
    spider._scan_pages = Mock()

    spider.run()

    assert spider.run_succeeded is True
    spider._emit_finished.assert_called_once()


def test_stop_before_browser_does_not_mark_run_succeeded() -> None:
    spider = _spider()
    _prepare_run(spider)
    spider.is_running = False
    spider._interrupt_requested = True
    spider._challenge_browser_runtime = Mock()

    spider.run()

    assert spider.run_succeeded is False
    spider._challenge_browser_runtime.assert_not_called()
