"""MissAV 两遍列表扫描与 m3u8 嗅探实现。"""

import re
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from app.config import DEFAULT_USER_AGENT
from app.spiders.base import BaseSpider
from app.spiders.missav.browser_executable import (
    browser_launch_label,
    missing_browser_request,
    resolve_challenge_browser,
)
from app.spiders.missav.challenge_session import (
    ChallengeWaitCancelled,
    ChallengeWaitTimeout,
    ExternalChromeChallengeSession,
    SystemBrowserUnavailable,
)
from app.spiders.missav.local_proxy import (
    browser_egress_proxy,
    chromium_proxy_launch_args,
    discover_client_proxy,
    is_unreachable_loopback_proxy,
)
from app.spiders.missav.parser import MissAVParser
from app.spiders.missav.playlist_capture import PlaylistCapture
from app.spiders.missav.task_builder import MissAVTaskBuilder
from shared.runtime_options import DomainPolicyViolation


@dataclass
class _ChallengeBrowserRuntime:
    """记录一次 MissAV 扫描当前可用的浏览器接管对象。"""

    playwright: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    external_session: ExternalChromeChallengeSession | None = None

    def bind(
        self,
        playwright: Any,
        browser: Any,
        context: Any,
        page: Any,
    ) -> None:
        self.playwright = playwright
        self.browser = browser
        self.context = context
        self.page = page


class MissAVSpider(BaseSpider):
    """MissAV 爬虫，先扫列表再进入详情页嗅探 m3u8。"""

    ALLOW_SYSTEM_PROXY_FALLBACK = True

    GRID_READY_TIMEOUT_MS = 30000
    PLAYER_READY_TIMEOUT_MS = 30000
    M3U8_SNIFF_SECONDS = 45
    PAGE_HOSTS = ("missav.ai",)
    URL_TRAILING_PUNCTUATION = " \t\r\n`'\"，。！？；：、,.!?;:)]}）】》>"
    CLOUDFLARE_MIN_WAIT_MS = 120000
    SYSTEM_BROWSER_CHANNELS = (("chrome", "Chrome"), ("msedge", "Edge"))
    CLOUDFLARE_UNSUPPORTED_MARKERS = (
        "浏览器不支持",
        "browser not supported",
        "browser is not supported",
        "unsupported browser",
        "update your browser",
    )
    CLOUDFLARE_PENDING_MARKERS = (
        "just a moment",
        "请稍候",
        "正在进行安全验证",
        "checking your browser",
        "security verification",
        "attention required",
    )

    def __init__(self, keyword: str, config: dict):
        """配置候选解析器和 HLS 下载任务装配器。"""
        super().__init__(keyword, config)
        self.parser = MissAVParser()
        self.task_builder = MissAVTaskBuilder()

    def _require_missav_page_url(self, url: str) -> str:
        """拒绝指向 MissAV 站点之外的用户浏览器入口。"""
        if not self._url_matches_hosts(url, self.PAGE_HOSTS):
            raise DomainPolicyViolation("MissAV 页面链接不属于受支持的平台域名")
        return self._public_domain_policy_engine().require_public_url(url)

    def _launch_challenge_browser(
        self,
        playwright,
        *,
        headless: bool,
        proxy_server: str | None = None,
    ):
        """人工验证优先使用系统稳定版浏览器，缺失时再退回随包内核。"""
        launch_kwargs = dict(
            self._playwright_launch_kwargs(
                headless=headless,
                proxy="直连",
                args=["--disable-blink-features=AutomationControlled"],
            )
        )
        launch_args = [
            item
            for item in list(launch_kwargs.get("args") or [])
            if not str(item).startswith("--proxy-") and str(item) != "--no-proxy-server"
        ]
        launch_args.extend(chromium_proxy_launch_args(proxy_server))
        launch_kwargs["args"] = launch_args
        launch_kwargs.pop("proxy", None)
        if not headless:
            for channel, label in self.SYSTEM_BROWSER_CHANNELS:
                try:
                    browser = playwright.chromium.launch(**launch_kwargs, channel=channel)
                except PlaywrightError:
                    continue
                self.log(f"🌐 MissAV 人工验证使用系统 {label} {browser.version}")
                return browser

        browser = playwright.chromium.launch(**launch_kwargs)
        if not headless:
            self.log(
                f"⚠️ 未找到可用的系统 Chrome/Edge，改用内置浏览器 {browser.version}；"
                "Cloudflare 可能拒绝过旧内核"
            )
        return browser

    def _create_challenge_context(self, browser):
        """保留浏览器原生 UA/Web API，避免挑战看到互相矛盾的伪装指纹。"""
        context_kwargs = self._playwright_context_kwargs(
            user_agent="",
            referer="https://missav.ai/",
            viewport={"width": 1280, "height": 800},
        )
        # 通用构造器会生成轮换 UA；MissAV 的 Cloudflare 挑战要求它与真实内核一致。
        context_kwargs.pop("user_agent", None)
        # 不注入全局 stealth.js：其中 Canvas/WebGL 改写会让挑战环境失去官方支持。
        return browser.new_context(**context_kwargs)

    def _wait_for_external_challenge(
        self,
        session: ExternalChromeChallengeSession,
        target_url: str,
        *,
        timeout_ms: int,
    ) -> bool:
        """在不连接 CDP 的前提下等待系统浏览器离开挑战页。"""
        challenge_reported = False

        def report_challenge(_title: str) -> None:
            nonlocal challenge_reported
            challenge_reported = True
            self.log("🛡️ 检测到 Cloudflare，请在浏览器中完成人工验证...")

        try:
            session.wait_for_ready_page(
                target_url,
                timeout_seconds=max(
                    self.CLOUDFLARE_MIN_WAIT_MS,
                    int(timeout_ms or 0),
                )
                / 1000,
                cancelled=lambda: not self.is_running or self.interrupt_requested,
                on_challenge=report_challenge,
            )
        except ChallengeWaitCancelled:
            return False
        except ChallengeWaitTimeout as exc:
            if challenge_reported:
                self.log(f"❌ Cloudflare 验证等待超时: {exc}")
            else:
                self.log(f"❌ 页面加载等待超时: {exc}")
            self.debug_state(
                "MISSAV_BROWSER_WAIT_FAILED", message=str(exc), level="ERROR",
                context={"target_url": target_url, "challenge_detected": challenge_reported},
            )
            return False
        if challenge_reported:
            self.log("✅ Cloudflare 验证已通过")
        return True

    def _attach_external_browser(
        self,
        runtime: _ChallengeBrowserRuntime,
        target_url: str,
    ) -> bool:
        """挑战结束后才启动 Playwright，并接管目标标签页。"""
        session = runtime.external_session
        if session is None:
            return False
        playwright = sync_playwright().start()
        self._track_playwright_instance(playwright)
        try:
            attachment = session.attach(playwright, target_url)
        except Exception:
            self._stop_tracked_playwright_instance(playwright)
            raise
        runtime.bind(
            playwright,
            attachment.browser,
            attachment.context,
            attachment.page,
        )
        self._track_playwright_browser(attachment.browser)
        self.log(f"🌐 MissAV 已接管系统浏览器 {attachment.browser.version}")
        return True

    def _disconnect_external_browser(
        self,
        runtime: _ChallengeBrowserRuntime,
    ) -> None:
        """仅断开 Playwright/CDP，保留系统 Chrome 进程和用户目录。"""
        playwright = runtime.playwright
        browser = runtime.browser
        runtime.bind(None, None, None, None)
        if browser is not None:
            self._clear_playwright_browser(browser)
        if playwright is not None:
            self._stop_tracked_playwright_instance(playwright)

    def _navigate_external_challenge(
        self,
        runtime: _ChallengeBrowserRuntime,
        target_url: str,
        *,
        timeout_ms: int,
    ) -> bool:
        """断开控制后在同一浏览器资料中打开 URL，通过挑战后再接管。"""
        session = runtime.external_session
        if session is None:
            return False
        target_url = self._require_missav_page_url(target_url)
        self._disconnect_external_browser(runtime)
        session.open_url(target_url)
        if not self._wait_for_external_challenge(
            session,
            target_url,
            timeout_ms=timeout_ms,
        ):
            return False
        return self._attach_external_browser(runtime, target_url)

    def _reopen_missav_page(
        self,
        runtime: _ChallengeBrowserRuntime,
        page,
        url: str,
        *,
        timeout_ms: int,
        wait_for_challenge: bool = False,
    ):
        """校验后再打开页面。可能重新触发挑战的外部导航先断开接管。"""
        target_url = self._require_missav_page_url(url)
        if runtime.external_session is not None:
            if not self._navigate_external_challenge(
                runtime,
                target_url,
                timeout_ms=timeout_ms,
            ):
                return None
            return runtime.page
        if wait_for_challenge:
            self.log("⚠️ 没有外部浏览器会话，可能再次触发挑战的导航已中止，不会打开已连接页面。")
            return None
        if not self.interruptible_playwright_goto(page, target_url, timeout=timeout_ms):
            return None
        return page

    def _open_actress_catalog(
        self,
        runtime: _ChallengeBrowserRuntime,
        page,
        list_url: str,
        enable_individual: bool,
        timeout_ms: int,
    ):
        """演员入口使用已校验地址，不从跳转后的 page.url 读取主机。"""
        try:
            self.interruptible_sleep(2)
            links = page.query_selector_all('a[href*="/actresses/"]')
        except PlaywrightError:
            return page, list_url
        actress_link = None
        for link in links:
            try:
                href = link.get_attribute("href")
            except PlaywrightError:
                continue
            if not href or "ranking" in href or "search" in href:
                continue
            try:
                if not link.is_visible():
                    continue
            except PlaywrightError:
                continue
            actress_link = link
            break
        if actress_link is None:
            return page, list_url
        try:
            href = actress_link.get_attribute("href")
        except PlaywrightError:
            return page, list_url
        actress_url = urllib.parse.urljoin(list_url, str(href or ""))
        self.log(f"✨ 发现演员主页，自动跳转: {href}")
        try:
            actress_url = self._require_missav_page_url(actress_url)
        except DomainPolicyViolation:
            return page, list_url
        opened = self._reopen_missav_page(
            runtime,
            page,
            actress_url,
            timeout_ms=timeout_ms,
        )
        if opened is None:
            return None, list_url
        page = opened
        filtered_url = self.parser.inject_url_params(actress_url, enable_individual)
        if filtered_url == actress_url:
            return page, actress_url
        try:
            filtered_url = self._require_missav_page_url(filtered_url)
        except DomainPolicyViolation:
            return page, actress_url
        opened = self._reopen_missav_page(
            runtime,
            page,
            filtered_url,
            timeout_ms=timeout_ms,
            wait_for_challenge=runtime.external_session is None,
        )
        if opened is None:
            return None, actress_url
        return opened, filtered_url

    def _chinese_verification_url(self, validated_url: str) -> str | None:
        """只在已经校验过的列表地址上追加中文筛选查询参数。"""
        filtered = self.parser.add_chinese_filter(validated_url)
        if filtered == validated_url:
            return None
        parsed = urllib.parse.urlsplit(filtered)
        pairs = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if key != "page"
        ]
        return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(pairs)))

    def _release_challenge_browser_runtime(
        self,
        runtime: _ChallengeBrowserRuntime,
    ) -> None:
        """按所有权释放直连浏览器或外部系统浏览器会话。"""
        if runtime.external_session is not None:
            self._disconnect_external_browser(runtime)
            runtime.external_session.close()
            runtime.external_session = None
            return
        browser = runtime.browser
        playwright = runtime.playwright
        runtime.bind(None, None, None, None)
        if browser is not None:
            self._close_tracked_playwright_browser(browser)
        if playwright is not None:
            self._stop_tracked_playwright_instance(playwright)

    @contextmanager
    def _challenge_browser_runtime(
        self,
        target_url: str,
        proxy_server: str | None,
    ) -> Iterator[_ChallengeBrowserRuntime]:
        """创建挑战安全的浏览器运行时，退出时统一回收资源。"""
        runtime = _ChallengeBrowserRuntime()
        browser_timeout_ms = self._configured_timeout_ms(default=60)
        try:
            configured_browser = str((getattr(self, "config", {}) or {}).get("browser_executable") or "")
            try:
                browser_executable = resolve_challenge_browser(configured_browser)
                runtime.external_session = ExternalChromeChallengeSession(
                    proxy_server=proxy_server,
                    browser_executable=browser_executable,
                    headless=self._browser_headless(),
                )
            except SystemBrowserUnavailable:
                self.log("⚠️ 未找到系统 Chrome/Edge，可能再次触发挑战的导航已中止，不会打开自动化客户端连接的页面。")
            else:
                missing_browser = missing_browser_request(configured_browser)
                if missing_browser:
                    self.log(f"⚠️ 指定的浏览器不存在: {missing_browser}")
                self.log(
                    "🌐 启动独立浏览器: "
                    f"{browser_launch_label(runtime.external_session.browser_executable)}"
                )
                runtime.external_session.start(self._require_missav_page_url(target_url))
                if self._wait_for_external_challenge(
                    runtime.external_session,
                    target_url,
                    timeout_ms=browser_timeout_ms,
                ):
                    self._attach_external_browser(runtime, target_url)

            if runtime.external_session is None:
                self.log("⚠️ 没有外部浏览器会话，可能再次触发挑战的导航已中止，不会对已连接页面执行跳转。")

            yield runtime
        finally:
            self._release_challenge_browser_runtime(runtime)

    def _cloudflare_challenge_state(self, page) -> str:
        """区分正常页面、等待中的挑战和明确拒绝当前浏览器的页面。"""
        try:
            title = str(page.title() or "")
        except (PlaywrightError, TypeError, AttributeError):
            title = ""
        try:
            body_text = str(page.locator("body").inner_text(timeout=1000) or "")
        except (PlaywrightError, TypeError, AttributeError):
            body_text = ""
        title_text = title.lower()
        body_text = body_text.lower()
        challenge_text = f"{title_text}\n{body_text}"
        pending = any(
            marker in challenge_text for marker in self.CLOUDFLARE_PENDING_MARKERS
        )
        challenge_evidence = (
            pending
            or "cloudflare" in challenge_text
            or "ray id" in challenge_text
        )
        if challenge_evidence and any(
            marker in challenge_text
            for marker in self.CLOUDFLARE_UNSUPPORTED_MARKERS
        ):
            return "unsupported"
        if pending:
            return "pending"
        return "clear"

    def _wait_for_cloudflare_challenge(self, page, *, timeout_ms: int) -> bool:
        """可中断地等待人工/自动验证完成，并对不支持页给出明确诊断。"""
        state = self._cloudflare_challenge_state(page)
        if state == "clear":
            return True
        if state == "unsupported":
            self.log("❌ Cloudflare 不支持当前浏览器环境，请更新系统 Chrome/Edge 后重试")
            return False

        self.log("🛡️ 检测到 Cloudflare，请在浏览器中完成人工验证...")
        remaining_ms = max(self.CLOUDFLARE_MIN_WAIT_MS, int(timeout_ms or 0))
        while remaining_ms > 0:
            if not self.interruptible_sleep(1):
                return False
            remaining_ms -= 1000
            state = self._cloudflare_challenge_state(page)
            if state == "clear":
                self.log("✅ Cloudflare 验证已通过")
                return True
            if state == "unsupported":
                self.log("❌ Cloudflare 不支持当前浏览器环境，请更新系统 Chrome/Edge 后重试")
                return False
        self.log("❌ Cloudflare 验证等待超时")
        return False

    def run(self):
        """执行候选扫描、版本筛选、用户选择和详情页 HLS 嗅探。"""
        self.run_succeeded = False

        def succeed() -> None:
            if self.is_running and not self.interrupt_requested:
                self.run_succeeded = True

        try:
            proxy_server = browser_egress_proxy(self._effective_proxy_server())
            if proxy_server:
                self.log(f"🌍 使用代理: {proxy_server}")
            enable_individual = self.config.get('individual_only', False)
            priority_text = self.config.get('priority', "中文字幕优先")
            priority_map = {
                "中文字幕优先": ["中文字幕", "无码流出", "英文字幕", "普通版"],
                "无码流出优先": ["无码流出", "中文字幕", "英文字幕", "普通版"]
            }
            self.priority_list = priority_map.get(priority_text, priority_map["中文字幕优先"])
            self.log(f"⚙️ 偏好设置: 单体={enable_individual}, 优先级={self.priority_list}")
            my_ua = DEFAULT_USER_AGENT
            target_url = ""
            is_single_video_mode = False
            is_search_mode = False
            raw_input = self._normalize_keyword(self.keyword)
            if raw_input != self.keyword.strip():
                self.log(f"🔗 MissAV 输入已归一化: {raw_input}")
            if "http" in raw_input:
                parsed = urllib.parse.urlparse(raw_input)
                path_parts = parsed.path.strip('/').split('/')
                keywords_list = ['search', 'actresses', 'tags', 'series', 'makers', 'directors', 'labels']
                if not any(k in parsed.path for k in keywords_list) and re.search(r'\w+-\d+', path_parts[-1]):
                    is_single_video_mode = True
                    target_url = raw_input
                    self.log("🔗 识别为单体视频链接")
                else:
                    target_url = raw_input
                    self.log("🔗 识别为列表/分类链接")
            else:
                is_search_mode = True
                encoded_kw = urllib.parse.quote(raw_input)
                target_url = f"https://missav.ai/cn/search/{encoded_kw}"
                self.log(f"🔍 构造搜索链接: {target_url}")
            if not is_single_video_mode:
                target_url = self.parser.inject_url_params(target_url, enable_individual)
                self.log(f"🔧 修正后 URL: {target_url}")
            target_url = self._require_missav_page_url(target_url)
            if not self.is_running:
                return
            with self._challenge_browser_runtime(
                target_url,
                proxy_server,
            ) as runtime:
                browser = runtime.browser
                context = runtime.context
                page = runtime.page
                if page is None:
                    return
                try:
                    try:
                        my_ua = str(page.evaluate("navigator.userAgent") or my_ua)
                    except (PlaywrightError, TypeError, AttributeError):
                        pass
                    browser_timeout_ms = self._configured_timeout_ms(default=60)
                    validated_list_url = target_url
                    # 搜索页可能优先返回演员卡片，转入其主页后才能扫描完整作品列表。
                    if is_search_mode and self.is_running:
                        page, validated_list_url = self._open_actress_catalog(
                            runtime,
                            page,
                            validated_list_url,
                            enable_individual,
                            browser_timeout_ms,
                        )
                        if page is None:
                            return
                        browser = runtime.browser or browser
                        context = runtime.context or context
                    if not self.is_running:
                        self._close_tracked_playwright_browser(browser)
                        return
                    final_tasks = []
                    if is_single_video_mode:
                        title = page.title().replace('| MissAV', '').strip()
                        final_tasks.append({'title': title, 'url': page.url})
                    else:
                        scraped_data = {}
                        verified_chinese = set()
                        # 第一遍收集全部候选，用户中途停止时仍可保留已经得到的部分结果。
                        self.log("📜 开始第一遍扫描 (获取所有视频)...")
                        self._scan_pages(
                            page,
                            scraped_data,
                            is_chinese_pass=False,
                            page_url=validated_list_url,
                            runtime=runtime,
                        )
                        page = runtime.page or page
                        browser = runtime.browser or browser
                        context = runtime.context or context

                        if not self.is_running:
                            self.log("⏸️ 扫描被中断，跳过中文校验，准备生成清单...")
                        if not scraped_data:
                            self.log("❌ 未找到任何视频")
                            self._close_tracked_playwright_browser(browser)
                            succeed()
                            return
                        # 第二遍只校验中文字幕；停止后跳过，避免为补充标签继续发起页面请求。
                        if self.is_running:
                            self.log("🇨🇳 开始第二遍扫描 (校验中文字幕)...")
                            chinese_url = self._chinese_verification_url(validated_list_url)

                            if chinese_url:
                                self.log(f"   跳转校验: {chinese_url}")
                                try:
                                    opened = self._reopen_missav_page(
                                        runtime,
                                        page,
                                        chinese_url,
                                        timeout_ms=browser_timeout_ms,
                                        wait_for_challenge=runtime.external_session is None,
                                    )
                                    if opened is None:
                                        return
                                    page = opened
                                    browser = runtime.browser or browser
                                    context = runtime.context or context
                                    chinese_data = {}
                                    self._scan_pages(
                                        page,
                                        chinese_data,
                                        is_chinese_pass=True,
                                        page_url=chinese_url,
                                        runtime=runtime,
                                    )
                                    page = runtime.page or page
                                    verified_chinese = set(chinese_data.keys())
                                    scraped_data.update(chinese_data)
                                except (PlaywrightError, DomainPolicyViolation) as e:
                                    self.log(f"   ⚠️ 中文校验异常: {e}")

                        if not self.revive_for_partial_selection(len(scraped_data), "个候选结果"):
                            self._close_tracked_playwright_browser(browser)
                            return
                        # 同番号的不同版本先分组再打分，最终只保留用户偏好最高的一项。
                        self.log(f"🧠 智能筛选中 (共 {len(scraped_data)} 个候选)...")
                        grouped = self.parser.group_candidates(scraped_data)

                        for _code, items in grouped.items():
                            sorted_items = sorted(
                                items,
                                key=lambda x: self.parser.calculate_score(x[0], x[1], verified_chinese, self.priority_list),
                                reverse=True
                            )
                            best_url, best_title = sorted_items[0]
                            final_title = self.parser.generate_display_title(best_url, best_title, verified_chinese)
                            final_tasks.append({'title': final_title, 'url': best_url})

                    if not final_tasks:
                        self.log("❌ 筛选后无有效结果")
                        self._close_tracked_playwright_browser(browser)
                        succeed()
                        return
                    final_tasks = self._trim_final_tasks(final_tasks)
                    self.log(f"🔔 扫描完成，共 {len(final_tasks)} 个最佳版本")

                    selected_indices = self.ask_user_selection(final_tasks)
                    if not selected_indices:
                        self.log("❌ 用户取消下载")
                        self._close_tracked_playwright_browser(browser)
                        succeed()
                        return
                    self.log(f"✅ 选中 {len(selected_indices)} 个，开始嗅探 m3u8...")

                    # 每个选择项单独进入详情页嗅探 playlist.m3u8，避免跨作品复用过期流地址。
                    success_count = 0
                    for i, idx in enumerate(selected_indices):
                        if not self.is_running:
                            break
                        task = final_tasks[idx]
                        try:
                            target_page_url = self._require_missav_page_url(task['url'])
                        except DomainPolicyViolation as exc:
                            self.log(f"   ❌ 跳过不受支持的详情链接: {exc}")
                            continue
                        title = task['title']
                        self.log(f"🕵️ [{i + 1}/{len(selected_indices)}] 嗅探: {title[:15]}...")
                        playlist_capture = PlaylistCapture()
                        # 监听器闭包只归属当前详情页，并在 finally 中移除，防止迟到响应污染下一项。
                        def handle_response(resp, capture=playlist_capture):
                            try:
                                req = resp.request
                                req_url = req.url
                            except (PlaywrightError, TypeError, AttributeError):
                                return
                            if not self._is_m3u8_request_url(req_url):
                                return
                            try:
                                status = int(resp.status)
                            except (TypeError, ValueError):
                                status = 0
                            try:
                                playlist_text = resp.text() if status in (200, 206) else ""
                            except (PlaywrightError, UnicodeDecodeError, TypeError, ValueError):
                                playlist_text = ""
                            capture.observe(
                                req_url,
                                status,
                                self._headers_from_request(req),
                                playlist_text,
                            )
                        def on_popup(popup):
                            active_page = runtime.page or page
                            if popup != active_page:
                                try:
                                    popup.close()
                                except PlaywrightError:
                                    pass
                        listeners_attached = False
                        try:
                            if runtime.external_session is not None:
                                if not self._navigate_external_challenge(
                                    runtime,
                                    target_page_url,
                                    timeout_ms=browser_timeout_ms,
                                ):
                                    break
                                page = runtime.page
                                context = runtime.context
                                browser = runtime.browser
                                if page is None or context is None:
                                    break
                            context.on("page", on_popup)
                            page.on("response", handle_response)
                            listeners_attached = True
                            if runtime.external_session is None:
                                self.log("⚠️ 详情跳转仍由自动化客户端连接，已停止以避免在挑战页上轮询。")
                                break
                            try:
                                self.interruptible_wait_for_selector(page, ".plyr", timeout=self.PLAYER_READY_TIMEOUT_MS)
                                if not self.is_running or self.interrupt_requested:
                                    break
                                page.mouse.click(400, 300)
                                self.interruptible_sleep(2)
                                if playlist_capture.selected is None:
                                    page.mouse.click(400, 300)
                            except PlaywrightError:
                                pass
                            for _ in range(self.M3U8_SNIFF_SECONDS):
                                if playlist_capture.selected is not None or not self.is_running:
                                    break
                                self.interruptible_sleep(1)
                            if not self.is_running:
                                break
                            submission = playlist_capture.submission()
                            if submission is not None:
                                m3u8_url, m3u8_playlist_cache = submission
                                selected_playlist = playlist_capture.selected
                                m3u8_headers = {} if selected_playlist is None else selected_playlist.headers
                                m3u8_status = None if selected_playlist is None else selected_playlist.status
                                trace_id = self.new_trace_id("m3u8")
                                download_headers = self._download_headers_for_context(
                                    context,
                                    target_page_url,
                                    my_ua,
                                    stream_url=m3u8_url,
                                    request_headers=m3u8_headers,
                                )
                                cookie_header = download_headers.get("Cookie", "")
                                browser_storage_state = {}
                                try:
                                    browser_storage_state = context.storage_state()
                                except (PlaywrightError, TypeError, AttributeError):
                                    browser_storage_state = {}
                                self.log("   ✨ 嗅探成功")
                                self.debug_state(
                                    action="emit_download_task",
                                    message="MissAV m3u8 嗅探成功并提交下载",
                                    status_code="MISSAV_TASK_EMIT",
                                    context={"trace_id": trace_id},
                                    details={
                                        "title": title,
                                        "stream_url": m3u8_url,
                                        "referer": target_page_url,
                                        "proxy": proxy_server,
                                        "has_cookie": bool(cookie_header),
                                        "m3u8_status": m3u8_status,
                                        "header_names": sorted(download_headers),
                                    },
                                )
                                self.emit_video(
                                    url=m3u8_url,
                                    title=title,
                                    source="missav",
                                    meta=self.task_builder.build_video_meta(
                                        trace_id,
                                        target_page_url,
                                        my_ua,
                                        proxy_server,
                                        headers=download_headers,
                                        cookie=cookie_header,
                                        include_cookies=bool(cookie_header),
                                        use_browser_headers=True,
                                        browser_storage_state=browser_storage_state,
                                        playlist_cache=m3u8_playlist_cache,
                                    )
                                )
                                success_count += 1
                            else:
                                selected_playlist = playlist_capture.selected
                                self.log("   ⚠️ 嗅探超时 (未找到 playlist.m3u8)")
                                self.debug_state(
                                    action="sniff_m3u8_timeout",
                                    message="MissAV 详情页嗅探超时，未发现 playlist.m3u8",
                                    status_code="MISSAV_SNIFF_TIMEOUT",
                                    details={
                                        "title": title,
                                        "target_page_url": target_page_url,
                                        "selected_index": idx,
                                        "stream_url": None if selected_playlist is None else selected_playlist.url,
                                        "m3u8_status": None if selected_playlist is None else selected_playlist.status,
                                        "m3u8_ready": selected_playlist is not None,
                                    },
                                    level="WARNING",
                                )
                        except PlaywrightError as e:
                            self.log(f"   ❌ 页面加载错误: {e}")
                            self.debug_state(
                                action="sniff_page_error",
                                message="MissAV 详情页加载失败",
                                status_code="MISSAV_PAGE_ERROR",
                                details={
                                    "title": title,
                                    "target_page_url": target_page_url,
                                    "error": str(e),
                                },
                                level="ERROR",
                            )
                        finally:
                            if listeners_attached and page is not None:
                                try:
                                    page.remove_listener("response", handle_response)
                                except PlaywrightError:
                                    pass
                            if listeners_attached and context is not None:
                                try:
                                    context.remove_listener("page", on_popup)
                                except PlaywrightError:
                                    pass
                        self.interruptible_sleep(1)
                    if self.is_running:
                        self.log(f"🎉 任务结束，成功提交: {success_count}")
                        succeed()
                    else:
                        self.log("🛑 任务强制中止")
                finally:
                    self._release_challenge_browser_runtime(runtime)
        except (PlaywrightError, OSError, ValueError, RuntimeError) as e:
            self.log(f"💥 爬虫错误: {e}")
        finally:
            browser = self._tracked_playwright_browser()
            if browser is not None:
                try:
                    self._close_tracked_playwright_browser(browser)
                except PlaywrightError:
                    pass
            self._clear_playwright_browser(browser)
            self._stop_tracked_playwright_instance()
            self._emit_finished()

    @classmethod
    def _strip_url_trailing_punctuation(cls, value: str) -> str:
        return str(value or "").strip().strip("`").rstrip(cls.URL_TRAILING_PUNCTUATION)

    @classmethod
    def _extract_first_url(cls, raw_text: str) -> str:
        text = str(raw_text or "").strip()
        match = re.search(r"https?://[^\s`'\"<>，。！？；;,)）\]}]+", text)
        if match:
            return cls._strip_url_trailing_punctuation(match.group(0))
        match = re.search(r"(?://)?(?:www\.)?missav\.[^\s`'\"<>，。！？；;,)）\]}]+/[^\s`'\"<>，。！？；;,)）\]}]+", text, re.I)
        if match:
            candidate = match.group(0)
            if "://" not in candidate:
                candidate = f"https://{candidate.lstrip('/')}"
            return cls._strip_url_trailing_punctuation(candidate)
        return text.strip().strip("`")

    def _normalize_keyword(self, raw_text: str) -> str:
        return self._extract_first_url(raw_text)

    @staticmethod
    def _is_m3u8_request_url(url: str) -> bool:
        path = urllib.parse.urlsplit(str(url or "")).path.lower()
        return ".m3u8" in path

    def _scan_pages(self, page, data_dict, is_chinese_pass=False, *, page_url: str | None = None, runtime=None):
        """列表扫描允许局部页失败，但尽量保住已抓到的候选数据。"""
        current_page = 1
        max_pages = self._max_scan_pages()
        max_items = self._max_items_limit()
        base_url = str(page_url or getattr(page, "url", "") or "")
        visited_urls = set()

        def navigation_key(url):
            parts = urllib.parse.urlsplit(url)
            return (parts.scheme.lower(), parts.netloc.lower(), parts.path,
                    tuple(sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))))

        def report_stop(reason):
            self.debug_state(
                "MISSAV_LIST_SCAN_STOPPED", context={
                    "reason": reason, "scan_page": current_page, "max_pages": max_pages,
                    "items": len(data_dict), "max_items": max_items,
                    "url": base_url, "chinese_pass": is_chinese_pass,
                },
            )

        while self.is_running and current_page <= max_pages:
            self.log(f"   📄 扫描第 {current_page} 页...")
            try:
                ready = self.interruptible_wait_for_selector(page, "div.grid", timeout=self.GRID_READY_TIMEOUT_MS)
                if not self.is_running or self.interrupt_requested:
                    break
                if not ready:
                    self.log("   ⚠️ 列表加载失败，停止本轮扫描")
                    report_stop("grid_not_ready")
                    break
                actual_url = getattr(page, "url", "")
                if isinstance(actual_url, str) and self._url_matches_hosts(actual_url, self.PAGE_HOSTS):
                    base_url = actual_url
                visited_urls.add(navigation_key(base_url))
                items = page.evaluate('''() => {
                    return Array.from(document.querySelectorAll('div.grid a')).map(a => {
                        const img = a.querySelector('img');
                        const title = img ? img.getAttribute('alt') : a.textContent.trim();
                        return {
                            url: a.href,
                            title: title || "" 
                        };
                    });
                }''')

                code_pattern = re.compile(r'/cn/.*[a-zA-Z]+-\d+')
                new_count = 0
                for item in items:
                    if len(data_dict) >= max_items:
                        break
                    url = item['url']
                    title = item['title']
                    if not self._url_matches_hosts(url, self.PAGE_HOSTS):
                        continue
                    if "/cn/" in url and code_pattern.search(url):
                        if not any(x in url for x in ['contact', 'dmca']):
                            if url not in data_dict:
                                data_dict[url] = title
                                new_count += 1

                self.debug_state(
                    "MISSAV_LIST_PAGE_SCANNED", context={
                        "url": base_url, "scan_page": current_page,
                        "new_items": new_count, "items": len(data_dict),
                        "chinese_pass": is_chinese_pass,
                    },
                )
                if len(data_dict) >= max_items:
                    self.log(f"   ✅ 已达到视频数上限 {max_items}，停止翻页")
                    report_stop("item_limit")
                    break
                if current_page >= max_pages:
                    self.log(f"   ✅ 已达到扫描页数上限 {max_pages}，停止翻页")
                    report_stop("page_limit")
                    break
                next_link = page.query_selector("a[rel~='next']")
                if next_link is None:
                    report_stop("last_page")
                    break
                href = next_link.get_attribute("href")
                if not isinstance(href, str) or not href.strip():
                    report_stop("missing_next_href")
                    break
                next_url = urllib.parse.urljoin(base_url, href)
                if navigation_key(next_url) in visited_urls:
                    self.log("   ⚠️ 下一页链接重复，停止翻页")
                    report_stop("repeated_next_url")
                    break
                try:
                    next_url = self._require_missav_page_url(next_url)
                    self.debug_state("MISSAV_LIST_PAGE_NAVIGATE", context={
                        "from_url": base_url, "target_url": next_url,
                        "scan_page": current_page + 1, "chinese_pass": is_chinese_pass,
                    })
                    timeout_ms = self._configured_timeout_ms(default=60)
                    if runtime is not None and runtime.external_session is not None:
                        if not self._navigate_external_challenge(
                            runtime,
                            next_url,
                            timeout_ms=timeout_ms,
                        ):
                            report_stop("navigation_failed")
                            break
                        page = runtime.page
                        if page is None:
                            report_stop("missing_reattached_page")
                            break
                    elif runtime is not None and runtime.external_session is None:
                        self.log("⚠️ 没有外部浏览器会话，已停止可能再次触发挑战的翻页，不会跳转已连接页面。")
                        report_stop("missing_external_session")
                        break
                    elif not self.interruptible_playwright_goto(
                        page,
                        next_url,
                        timeout=timeout_ms,
                    ):
                        report_stop("navigation_failed")
                        break
                    current_page += 1
                    base_url = next_url
                except (PlaywrightError, DomainPolicyViolation) as exc:
                    self.log(f"   ⚠️ 翻页失败: {exc}")
                    self.debug_state("MISSAV_LIST_PAGE_FAILED", message=str(exc), level="ERROR",
                                     context={"target_url": next_url, "scan_page": current_page + 1})
                    break
            except (PlaywrightError, ValueError, TypeError) as e:
                self.log(f"   ⚠️ 页面扫描异常: {e}")
                self.debug_state("MISSAV_LIST_PAGE_FAILED", message=str(e), level="ERROR",
                                 context={"url": base_url, "scan_page": current_page,
                                          "chinese_pass": is_chinese_pass})
                break

    def _max_items_limit(self) -> int:
        config = getattr(self, "config", {}) or {}
        try:
            value = int(config.get("max_items") or config.get("limit") or 20)
        except (TypeError, ValueError):
            value = 20
        return max(1, value)

    def _max_scan_pages(self) -> int:
        config = getattr(self, "config", {}) or {}
        try:
            value = int(config.get("search_max_pages") or config.get("max_pages") or 100)
        except (TypeError, ValueError):
            value = 100
        return max(1, value)

    def _trim_final_tasks(self, tasks):
        limit = self._max_items_limit()
        if len(tasks) <= limit:
            return list(tasks)
        self.log(f"   ✂️ 按视频数上限裁剪: {len(tasks)} -> {limit}")
        return list(tasks)[:limit]

    @staticmethod
    def _looks_like_hls_playlist(text: str | None) -> bool:
        return "#EXTM3U" in str(text or "")

    def _headers_from_request(self, request) -> dict[str, str]:
        try:
            raw_headers = request.all_headers()
        except (PlaywrightError, TypeError, AttributeError):
            try:
                raw_headers = request.headers
            except (PlaywrightError, TypeError, AttributeError):
                raw_headers = {}
        return self._sanitize_download_headers(raw_headers)

    def _download_headers_for_context(
        self,
        context,
        referer: str,
        user_agent: str,
        *,
        stream_url: str = "",
        request_headers: dict | None = None,
    ) -> dict[str, str]:
        has_browser_request_headers = bool(request_headers)
        headers = self._sanitize_download_headers(request_headers or {})
        headers.setdefault("User-Agent", user_agent or DEFAULT_USER_AGENT)
        headers.setdefault("Referer", referer)
        self._apply_browser_download_header_defaults(headers, referer, preserve_captured=has_browser_request_headers)
        if self._is_surrit_stream_url(stream_url):
            headers.setdefault("Range", "bytes=0-")
        if "Cookie" not in headers:
            cookie_header = self._cookie_header_for_context(context, stream_url, referer)
            if cookie_header:
                headers["Cookie"] = cookie_header
        return headers

    @staticmethod
    def _is_surrit_stream_url(stream_url: str) -> bool:
        try:
            host = urllib.parse.urlparse(str(stream_url or "")).netloc.lower()
        except (TypeError, ValueError):
            return False
        return host == "surrit.com" or host.endswith(".surrit.com")

    def _cookie_header_for_context(self, context, stream_url: str, referer: str) -> str:
        urls = [url for url in (stream_url, referer) if url][:1]
        try:
            cookies = context.cookies(urls) if urls else context.cookies()
        except TypeError:
            try:
                cookies = context.cookies()
            except (PlaywrightError, TypeError, AttributeError):
                cookies = []
        except (PlaywrightError, AttributeError):
            cookies = []
        return "; ".join(
            f"{cookie.get('name')}={cookie.get('value')}"
            for cookie in cookies
            if isinstance(cookie, dict) and cookie.get("name") and cookie.get("value") is not None
        )

    @staticmethod
    def _sanitize_download_headers(raw_headers: dict | None) -> dict[str, str]:
        blocked = {
            "host",
            "content-length",
            "connection",
            "transfer-encoding",
        }
        headers: dict[str, str] = {}
        for key, value in (raw_headers or {}).items():
            key_text = str(key or "").strip()
            value_text = str(value or "").strip()
            if not key_text or not value_text:
                continue
            lowered = key_text.lower()
            if lowered.startswith(":") or lowered in blocked:
                continue
            canonical = "-".join(part[:1].upper() + part[1:].lower() for part in lowered.split("-"))
            headers[canonical] = value_text
        return headers

    @classmethod
    def _apply_browser_download_header_defaults(
        cls,
        headers: dict[str, str],
        referer: str,
        *,
        preserve_captured: bool = False,
    ) -> None:
        headers.setdefault("Accept", "*/*")
        headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9,en-CN;q=0.8,en;q=0.7")
        headers.setdefault("Cache-Control", "no-cache")
        headers.setdefault("Pragma", "no-cache")
        headers.setdefault("Priority", "u=1, i")
        if preserve_captured:
            return
        origin = cls._origin_from_referer(referer)
        if origin:
            headers.setdefault("Origin", origin)
        headers.setdefault("Sec-Fetch-Dest", "empty")
        headers.setdefault("Sec-Fetch-Mode", "cors")
        headers.setdefault("Sec-Fetch-Site", "cross-site")
        headers.setdefault("Sec-Ch-Ua-Mobile", "?0")
        headers.setdefault("Sec-Ch-Ua-Platform", '"Windows"')

    @staticmethod
    def _origin_from_referer(referer: str) -> str:
        try:
            parsed = urllib.parse.urlparse(str(referer or ""))
        except (TypeError, ValueError):
            return ""
        if not parsed.scheme or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}"

    def _effective_proxy_server(
        self,
        configured: object = None,
        *,
        allow_system_fallback: bool | None = None,
    ) -> str | None:
        if configured is None:
            configured = (getattr(self, "config", {}) or {}).get("proxy")
        # MissAV 默认继承系统代理，但仍需遵守基类调用方的显式关闭请求。
        fallback = True if allow_system_fallback is None else bool(allow_system_fallback)
        configured_text = str(configured or "").strip()
        cache = getattr(self, "_proxy_resolution_cache", None)
        if cache is None:
            cache = {}
            self._proxy_resolution_cache = cache
        cache_key = (configured_text, fallback)
        if cache_key in cache:
            return cache[cache_key]
        if configured_text.lower() in {"none", "direct"} or configured_text == "直连":
            cache[cache_key] = None
            return None
        resolved = super()._effective_proxy_server(configured, allow_system_fallback=fallback)
        if not fallback or (resolved is not None and not is_unreachable_loopback_proxy(resolved)):
            cache[cache_key] = resolved
            return resolved
        discovered = discover_client_proxy()
        if not discovered or discovered == resolved:
            cache[cache_key] = resolved
            return resolved
        if getattr(self, "sig_log", None) is not None:
            if resolved:
                self.log(f"🌍 配置的本机代理不可用，改用已识别的代理: {discovered}")
            else:
                self.log(f"🌍 已识别本机代理: {discovered}")
        cache[cache_key] = discovered
        return discovered

    @classmethod
    def _proxy_from_environment(cls) -> str | None:
        return BaseSpider._proxy_from_environment()

    @classmethod
    def _proxy_from_windows_settings(cls) -> str | None:
        return BaseSpider._proxy_from_windows_settings()

    @classmethod
    def _proxy_from_proxy_server_string(cls, value: str | None) -> str | None:
        return BaseSpider._proxy_from_proxy_server_string(value)

    @staticmethod
    def _normalize_proxy_server(value: object) -> str | None:
        return BaseSpider._normalize_proxy_server(value)
