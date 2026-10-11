"""遵循 UniversalCrawlerProplus 约定的小红书 Spider 实现。"""

from __future__ import annotations

import os
import threading
from concurrent.futures import FIRST_COMPLETED, CancelledError, ThreadPoolExecutor, wait
from typing import Any
from urllib.parse import quote

import requests
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

from app.config import DEFAULT_USER_AGENT, cfg, get_setting_default
from app.debug_logger import debug_logger
from app.exceptions import SpiderAuthError, SpiderParseError
from app.spiders.base import BaseSpider
from app.services.auth_service import AuthService
from app.utils.user_agents import resolve_user_agent
from shared.network_proxy import requests_proxy_mapping
from shared.runtime_options import DomainPolicyViolation

from .client import XiaohongshuClient
from .helpers import (
    AUTHOR_NAME_KEYS,
    RED_ID_KEYS,
    USER_ID_KEYS,
    XSEC_SOURCE_KEYS,
    XSEC_TOKEN_KEYS,
    CreatorLookupInfo,
    CreatorUrlInfo,
    NoteUrlInfo,
    build_search_id,
    embedded_note_user,
    extract_first_url,
    extract_note_ref,
    note_display_title,
    is_creator_url,
    is_note_url,
    note_has_downloadable_media,
    parse_creator_lookup_input,
    parse_creator_info_from_url,
    parse_note_info_from_note_url,
)
from .parser import XiaohongshuParser
from .task_builder import XiaohongshuTaskBuilder

class XiaohongshuSpider(BaseSpider):
    """结合 Web API 与浏览器登录的小红书 Spider。"""

    HOME_URL = "https://www.xiaohongshu.com/"
    PAGE_HOSTS = ("xiaohongshu.com", "xhslink.com", "xhslink.cn")
    DETAIL_WORKER_CAP = 8
    PROFILE_READY_SELECTOR = "xpath=//a[contains(@href, '/user/profile/')]//span[text()='我']"

    def __init__(self, keyword: str, config: dict):
        super().__init__(keyword, config)
        self.parser = XiaohongshuParser()
        self.task_builder = XiaohongshuTaskBuilder()
        self.auth_service = AuthService()
        self.auth_file = cfg.get(
            "auth",
            "xiaohongshu_cookie_file",
            get_setting_default("auth", "xiaohongshu_cookie_file"),
        )
        self._detail_request_count = 0
        self._detail_request_lock = threading.RLock()
        self._detail_circuit_open = False
        self._detail_circuit_lock = threading.Lock()
        self._login_probe_cookie = ""
        self._login_probe_status: bool | None = None
        self._login_probe_cached = False
        self._client: XiaohongshuClient | None = None
        self.user_agent = resolve_user_agent(
            "xiaohongshu",
            self.config,
            configured_user_agent=cfg.get("xiaohongshu", "user_agent", DEFAULT_USER_AGENT),
            default_user_agent=DEFAULT_USER_AGENT,
        )

    def _user_agent(self) -> str:
        return str(getattr(self, "user_agent", "") or DEFAULT_USER_AGENT)

    def _proxy(self) -> str | None:
        return self._effective_proxy_server((getattr(self, "config", {}) or {}).get("proxy"))

    def _max_items_limit(self) -> int:
        default_limit = get_setting_default("xiaohongshu", "max_items")
        value = self.config.get("max_items", cfg.get("xiaohongshu", "max_items", default_limit))
        try:
            return max(1, min(int(value), 9999))
        except (TypeError, ValueError):
            return int(default_limit)

    def _search_max_pages(self) -> int:
        default_pages = get_setting_default("xiaohongshu", "search_max_pages")
        value = self.config.get("search_max_pages", cfg.get("xiaohongshu", "search_max_pages", default_pages))
        try:
            return max(1, min(int(value), 100))
        except (TypeError, ValueError):
            return int(default_pages)

    def _request_interval(self) -> float:
        default_interval = get_setting_default("xiaohongshu", "request_interval")
        value = self.config.get("request_interval", cfg.get("xiaohongshu", "request_interval", default_interval))
        try:
            return max(0.0, min(float(value), 10.0))
        except (TypeError, ValueError):
            return float(default_interval)

    def _pause_between_requests(self, multiplier: float = 1.0) -> None:
        delay = self._request_interval() * max(multiplier, 0.0)
        if delay > 0:
            self.interruptible_sleep(delay, step=min(0.5, delay))

    def _detail_request_interval(self) -> float:
        default_interval = get_setting_default("xiaohongshu", "detail_request_interval")
        value = self.config.get(
            "detail_request_interval",
            cfg.get("xiaohongshu", "detail_request_interval", default_interval),
        )
        try:
            return max(0.0, min(float(value), 5.0))
        except (TypeError, ValueError):
            return float(default_interval)

    def _pause_between_detail_requests(self, multiplier: float = 1.0) -> None:
        delay = self._detail_request_interval() * max(multiplier, 0.0)
        if delay > 0:
            self.interruptible_sleep(delay, step=min(0.5, delay))

    def _detail_pause_multiplier(self) -> float:
        # 详情解析可快于列表翻页，但仍需周期性放慢，避免持续并发请求触发平台限流。
        if self._detail_request_count <= 6:
            return 0.75
        if self._detail_request_count > 0 and self._detail_request_count % 16 == 0:
            return 1.9
        if self._detail_request_count > 0 and self._detail_request_count % 8 == 0:
            return 1.15
        return 1.0

    @staticmethod
    def _should_log_progress(current: int, total: int, *, step: int = 10) -> bool:
        current = max(0, int(current or 0))
        total = max(0, int(total or 0))
        if current <= 3 or current >= total:
            return True
        return step > 0 and current % step == 0

    @staticmethod
    def _first_text(*values: object) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    @classmethod
    def _detail_worker_count(cls, total: int) -> int:
        return max(1, min(int(total or 1), cls.DETAIL_WORKER_CAP))

    def _should_stream_download_items(self) -> bool:
        """默认以用户确认为下载入队边界，仅在显式配置时流式提交。"""
        value = self.config.get("stream_downloads")
        if value is None:
            return False
        return bool(value)

    @staticmethod
    def _extract_search_ref(entry: dict[str, Any]) -> dict[str, str] | None:
        return extract_note_ref(entry, fallback_source="pc_search")

    def _detail_circuit_is_open(self) -> bool:
        with self._detail_circuit_lock:
            return self._detail_circuit_open

    def _open_detail_circuit(self, kind: str) -> None:
        labels = {"461": "461", "login": "登录失效", "captcha": "验证码"}
        with self._detail_circuit_lock:
            already_open = self._detail_circuit_open
            self._detail_circuit_open = True
        if not already_open:
            self.log(f"⛔ 小红书详情熔断（{labels.get(kind, kind)}），已停止后续详情和 HTML 兜底")

    @staticmethod
    def _exception_status_code(exc: BaseException) -> int | None:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(status_code, int):
            return status_code
        return None

    @classmethod
    def _exception_business_code(cls, exc: BaseException) -> int | None:
        response = getattr(exc, "response", None)
        payload = None
        if response is not None:
            try:
                payload = response.json()
            except (TypeError, ValueError, AttributeError):
                payload = None
        if isinstance(payload, dict):
            code = payload.get("code")
            if isinstance(code, int):
                return code
            if isinstance(code, str) and code.lstrip("-").isdigit():
                return int(code)
        text = str(exc)
        marker = "code="
        index = text.find(marker)
        if index < 0:
            return None
        rest = text[index + len(marker):]
        digits = []
        if rest.startswith("-"):
            digits.append("-")
            rest = rest[1:]
        for char in rest:
            if char.isdigit():
                digits.append(char)
                continue
            break
        if digits in {[], ["-"]}:
            return None
        return int("".join(digits))

    @classmethod
    def _detail_failure_kind(cls, exc: BaseException) -> str | None:
        if cls._exception_status_code(exc) == 461:
            return "461"
        code = cls._exception_business_code(exc)
        if code in {-100, -101, 300012}:
            return "login"
        if code == 300011:
            return "captcha"
        response = getattr(exc, "response", None)
        body = str(getattr(response, "text", "") or "")
        blob = f"{exc}\n{body}".lower()
        if "验证码" in blob or "captcha" in blob or "verifytype" in blob or "verify_type" in blob:
            return "captcha"
        if "登录已过期" in blob or "登录失效" in blob or "login expired" in blob or "请先登录" in blob:
            return "login"
        return None

    def _raise_if_detail_blocked(self, exc: BaseException, note_id: str) -> None:
        kind = self._detail_failure_kind(exc)
        if kind:
            self._open_detail_circuit(kind)
            raise SpiderParseError(f"小红书详情触发熔断: {note_id}") from exc
        if isinstance(exc, SpiderParseError):
            raise
        raise SpiderParseError(f"获取小红书笔记失败: {note_id}") from exc

    def _open_page(self, page, url: str, *, timeout_ms: int, settle_ms: int = 3000) -> bool:
        """先提交导航，再分段等待加载，以便停止请求不必耗完整超时。"""
        if not self.interruptible_playwright_goto(
            page,
            url,
            wait_until="commit",
            timeout=timeout_ms,
        ):
            return False
        if not self.is_running or self.interrupt_requested:
            return False
        try:
            loaded = self.interruptible_wait_for_load_state(
                page,
                "domcontentloaded",
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            if not self.is_running or self.interrupt_requested:
                return False
            loaded = True
        if not loaded or not self.is_running or self.interrupt_requested:
            return False
        if settle_ms > 0 and not self.interruptible_page_wait(page, settle_ms):
            return False
        return bool(self.is_running and not self.interrupt_requested)

    def _resolve_short_share_url(self, url: str) -> str:
        candidate = str(url or "").strip()
        short_hosts = ("xhslink.com", "xhslink.cn")
        if not self._url_matches_hosts(candidate, short_hosts, allow_subdomains=False):
            return url.strip()
        try:
            proxy = self._proxy()
            proxies = requests_proxy_mapping(proxy)
            request_kwargs = self._restricted_public_request_kwargs(
                candidate,
                allowed_hosts=(*short_hosts, "xiaohongshu.com"),
            )
            response = requests.get(
                candidate,
                headers={"User-Agent": self._user_agent(), "Referer": self.HOME_URL},
                timeout=self._configured_timeout_seconds(default=30),
                allow_redirects=True,
                proxies=proxies,
                **request_kwargs,
            )
            resolved = response.url or candidate
            self.log(f"🔗 [分享链接解析] {candidate} -> {resolved}")
            return resolved
        except (requests.RequestException, DomainPolicyViolation) as exc:
            self.log(f"⚠️ 小红书分享链接解析失败: {exc}")
            return candidate

    def _normalize_keyword(self, raw_text: str) -> str:
        extracted = extract_first_url(raw_text)
        return self._resolve_short_share_url(extracted)

    def _classify_input(self, normalized_keyword: str) -> tuple[str, str]:
        raw = normalized_keyword.strip()
        creator_lookup = parse_creator_lookup_input(raw)
        if creator_lookup:
            return ("creator_lookup", creator_lookup.keyword)
        if is_note_url(raw):
            return ("note_url", raw)
        if is_creator_url(raw):
            return ("creator_url", raw)
        if len(raw) == 24 and all(ch in "0123456789abcdef" for ch in raw.lower()):
            return ("creator_id", raw)
        return ("keyword", raw)

    def _current_web_session(self, cookies: list[dict[str, Any]] | dict[str, Any] | None) -> str:
        cookie_dict = self.auth_service.extract_cookie_dict(cookies)
        return str(cookie_dict.get("web_session") or "")

    def _discard_saved_cookie_file(self) -> None:
        if not os.path.exists(self.auth_file):
            return
        try:
            os.remove(self.auth_file)
        except OSError:
            pass

    def _probe_cookie_login_status(self, cookie_str: str) -> bool | None:
        if not cookie_str:
            return False
        client = self._build_client(cookie_str)
        try:
            if not client.check_cookie_ready():
                return False
            return client.probe_login_status()
        finally:
            client.close()

    def _page_shows_logged_in_state(self, page, *, context, baseline_web_session: str = "") -> bool:
        try:
            if page.locator(self.PROFILE_READY_SELECTOR).count() > 0:
                return True
        except (PlaywrightError, RuntimeError, AttributeError) as exc:
            debug_logger.log_exception("XiaohongshuSpider", "profile_ready_probe", exc)
        current_web_session = self._current_web_session(context.cookies())
        if baseline_web_session and current_web_session and current_web_session != baseline_web_session:
            return True
        return False

    def _load_saved_cookie_string(self) -> str:
        if not os.path.exists(self.auth_file):
            return ""
        payload = self.auth_service.load_json_file(self.auth_file)
        if not payload:
            return ""
        cookie_str = self.auth_service.build_cookie_string(payload, required_cookie="a1")
        if not cookie_str:
            return ""
        login_status = self._probe_cookie_login_status(cookie_str)
        self._login_probe_cookie = cookie_str
        self._login_probe_status = login_status
        self._login_probe_cached = True
        if login_status is False:
            self._login_probe_cached = False
            self._login_probe_cookie = ""
            self.log("⚠️ 本地小红书 Cookie 已失效，已丢弃并准备重新登录")
            self._discard_saved_cookie_file()
            return ""
        if login_status is None:
            self.log("⚠️ 无法确认本地小红书 Cookie 登录态，继续使用已保存 Cookie")
        return cookie_str

    def _save_context_cookies(self, context) -> str:
        cookies = context.cookies()
        self.auth_service.save_json_file(self.auth_file, cookies)
        return self.auth_service.build_cookie_string(cookies, required_cookie="a1")

    def _bootstrap_cookie_string(self, entry_url: str) -> str:
        self.log("🔐 未找到可用的小红书 Cookie，启动浏览器采集会话...")
        proxy = self._proxy()
        launch_kwargs: dict[str, Any] = {"headless": self._browser_headless(login_window=True)}
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
            self.log(f"🌍 使用代理: {proxy}")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(**launch_kwargs)
            self._track_playwright_instance(playwright)
            self._track_playwright_browser(browser)
            try:
                context = browser.new_context(
                    **self._playwright_context_kwargs(
                        user_agent=self._user_agent(),
                        referer=self.HOME_URL,
                        viewport={"width": 1280, "height": 800},
                    )
                )
                self._apply_stealth_to_context(context)
                if os.path.exists(self.auth_file):
                    try:
                        self.auth_service.restore_playwright_cookies(context, self.auth_file)
                        self.log("📂 已尝试恢复本地小红书 Cookie")
                    except Exception:
                        self.log("⚠️ 本地小红书 Cookie 恢复失败，继续使用新会话")
                page = context.new_page()
                target = entry_url or self.HOME_URL
                baseline_web_session = self._current_web_session(context.cookies())
                timeout_ms = self._configured_timeout_ms(default=60)
                if not self._open_page(page, target, timeout_ms=timeout_ms, settle_ms=3000):
                    return ""
                if self._page_shows_logged_in_state(page, context=context, baseline_web_session=baseline_web_session):
                    cookie_str = self._save_context_cookies(context)
                    if cookie_str:
                        self.log("✅ 检测到已登录的小红书会话，Cookie 已保存")
                    return cookie_str

                self.log("🔑 若页面要求登录，请在浏览器中完成登录；程序会继续等待会话稳定")
                for _ in range(120):
                    if not self.is_running:
                        return ""
                    if not self.interruptible_page_wait(page, 1000):
                        return ""
                    if self._page_shows_logged_in_state(
                        page,
                        context=context,
                        baseline_web_session=baseline_web_session,
                    ):
                        cookie_str = self._save_context_cookies(context)
                        self.log("✅ 小红书登录成功，Cookie 已保存")
                        return cookie_str
                return ""
            finally:
                self._close_tracked_playwright_browser(browser)
                self._clear_playwright_instance(playwright)

    def _ensure_cookie_string(self, entry_url: str) -> str:
        cookie_str = self._load_saved_cookie_string()
        if cookie_str:
            self.log(f"👤 已加载本地小红书 Cookie: {self.auth_file}")
            return cookie_str
        return self._bootstrap_cookie_string(entry_url)

    def _build_client(self, cookie_str: str) -> XiaohongshuClient:
        default_timeout = get_setting_default("xiaohongshu", "timeout")
        return XiaohongshuClient(
            user_agent=self._user_agent(),
            cookie_str=cookie_str,
            proxy=self._proxy(),
            timeout=int(self.config.get("timeout", cfg.get("xiaohongshu", "timeout", default_timeout)) or default_timeout),
        )

    def _fetch_note_detail(self, client: XiaohongshuClient, ref: dict[str, str]) -> dict[str, Any] | None:
        note_id = ref.get("note_id", "")
        xsec_source = ref.get("xsec_source", "")
        xsec_token = ref.get("xsec_token", "")
        if not self.is_running or self._detail_circuit_is_open():
            return None
        with self._detail_request_lock:
            self._detail_request_count += 1
            multiplier = self._detail_pause_multiplier()
        self._pause_between_detail_requests(multiplier=multiplier)
        if not self.is_running or self._detail_circuit_is_open():
            return None
        try:
            detail = client.get_note_detail(note_id=note_id, xsec_source=xsec_source, xsec_token=xsec_token)
        except (requests.HTTPError, SpiderParseError) as exc:
            self._raise_if_detail_blocked(exc, note_id)
            return None
        if not detail:
            if not self.is_running or self._detail_circuit_is_open():
                return None
            try:
                detail = client.get_note_detail_from_html(
                    note_id=note_id,
                    xsec_source=xsec_source,
                    xsec_token=xsec_token,
                )
            except (requests.HTTPError, SpiderParseError) as exc:
                self._raise_if_detail_blocked(exc, note_id)
                return None
        if not detail:
            raise SpiderParseError(f"无法解析小红书笔记详情: {note_id}")
        normalized = self.parser.normalize_note(detail)
        if not note_has_downloadable_media(normalized):
            raise SpiderParseError(f"小红书笔记没有可下载媒体: {note_id}")
        return normalized

    def _emit_note_items(self, note: dict[str, Any], cookie_str: str, referer: str) -> int:
        items = self.task_builder.build_items(
            note,
            trace_id_factory=self.new_trace_id,
            referer=referer,
            user_agent=self._user_agent(),
            cookie_str=cookie_str,
            proxy=self._proxy(),
        )
        return self.emit_videos(items)

    def _handle_note_url(
        self,
        client: XiaohongshuClient,
        note_info: NoteUrlInfo,
        cookie_str: str,
        *,
        referer: str,
    ) -> None:
        note = self._fetch_note_detail(
            client,
            {
                "note_id": note_info.note_id,
                "xsec_source": note_info.xsec_source,
                "xsec_token": note_info.xsec_token,
            },
        )
        if not note:
            if not self.is_running:
                return
            raise SpiderParseError("未能获取指定小红书笔记详情")
        count = self._emit_note_items(note, cookie_str, referer=referer)
        self.log(f"✅ 已生成 {count} 个小红书下载任务")

    def _collect_search_refs(self, client: XiaohongshuClient, keyword: str) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        seen: set[str] = set()
        limit = self._max_items_limit()
        page_size = 20
        max_pages = self._search_max_pages()
        search_id = build_search_id()
        for page in range(1, max_pages + 1):
            if not self.is_running or len(refs) >= limit:
                break
            self.log(f"🔎 正在搜索小红书，第 {page} 页... 已发现 {len(refs)}/{limit}")
            data = client.search_notes(
                keyword=keyword,
                page=page,
                page_size=page_size,
                search_id=search_id,
                sort=str(
                    self.config.get("sort")
                    or cfg.get("xiaohongshu", "sort", get_setting_default("xiaohongshu", "sort"))
                ),
                note_type=int(
                    self.config.get(
                        "note_type",
                        cfg.get("xiaohongshu", "note_type", get_setting_default("xiaohongshu", "note_type")),
                    )
                ),
            )
            items = data.get("items") or []
            page_added = 0
            for entry in items:
                ref = self._extract_search_ref(entry)
                if not ref:
                    continue
                note_id = ref["note_id"]
                if note_id in seen:
                    continue
                seen.add(note_id)
                refs.append(ref)
                page_added += 1
                if self._should_log_progress(len(refs), limit):
                    self.log(f"🧾 搜索候选累计 {len(refs)}/{limit}")
                if len(refs) >= limit:
                    break
            self.log(f"📄 第 {page} 页新增 {page_added} 条候选")
            if not data.get("has_more", False):
                break
            self._pause_between_requests()
        return refs

    def _collect_creator_refs(self, client: XiaohongshuClient, creator: CreatorUrlInfo) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        seen: set[str] = set()
        cursor = ""
        limit = self._max_items_limit()
        max_pages = self._search_max_pages()
        for _page in range(1, max_pages + 1):
            if not self.is_running or len(refs) >= limit:
                break
            self.log(f"👤 正在读取小红书作者笔记列表... 已抓到 {len(refs)}/{limit}")
            data = client.get_creator_notes(
                user_id=creator.user_id,
                cursor=cursor,
                xsec_token=creator.xsec_token,
                xsec_source=creator.xsec_source or "pc_feed",
            )
            notes = data.get("notes") or []
            page_added = 0
            for entry in notes:
                ref = extract_note_ref(
                    entry,
                    default_source=creator.xsec_source or "pc_feed",
                    default_token=creator.xsec_token,
                    default_author=creator.nickname,
                    fallback_source="pc_feed",
                )
                if not ref:
                    continue
                note_id = ref["note_id"]
                if note_id in seen:
                    continue
                seen.add(note_id)
                refs.append(ref)
                page_added += 1
                if self._should_log_progress(len(refs), limit):
                    self.log(f"📚 主页候选累计 {len(refs)}/{limit}")
                if len(refs) >= limit:
                    break
            next_cursor = str(data.get("cursor") or "")
            if (
                len(refs) >= limit
                or page_added == 0
                or not data.get("has_more", False)
                or not next_cursor
                or next_cursor == cursor
            ):
                break
            cursor = next_cursor
            self._pause_between_requests(multiplier=1.5)
        return refs

    def _extract_creator_search_candidates(self, payload: dict[str, Any]) -> list[CreatorUrlInfo]:
        items = payload.get("users") or payload.get("items") or payload.get("user_list") or []
        candidates: list[CreatorUrlInfo] = []
        seen: set[str] = set()
        for entry in items:
            if not isinstance(entry, dict):
                continue
            user = (
                entry.get("user")
                if isinstance(entry.get("user"), dict)
                else entry.get("user_info")
                if isinstance(entry.get("user_info"), dict)
                else entry.get("author")
                if isinstance(entry.get("author"), dict)
                else entry.get("user_card")
                if isinstance(entry.get("user_card"), dict)
                else entry
            )
            user_id = self._first_text(*(user.get(key) for key in USER_ID_KEYS))
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            candidates.append(
                CreatorUrlInfo(
                    user_id=user_id,
                    xsec_token=self._first_text(
                        *(user.get(key) for key in XSEC_TOKEN_KEYS),
                        *(entry.get(key) for key in XSEC_TOKEN_KEYS),
                    ),
                    xsec_source=self._first_text(
                        *(user.get(key) for key in XSEC_SOURCE_KEYS),
                        *(entry.get(key) for key in XSEC_SOURCE_KEYS),
                        "pc_search",
                    ),
                    nickname=self._first_text(
                        *(user.get(key) for key in AUTHOR_NAME_KEYS),
                        entry.get("title"),
                    ),
                    red_id=self._first_text(*(user.get(key) for key in RED_ID_KEYS)),
                )
            )
        return candidates

    def _extract_creator_candidates_from_note_search(
        self, payload: dict[str, Any], lookup_keyword: str
    ) -> list[CreatorUrlInfo]:
        items = payload.get("items") or []
        candidates_by_user: dict[str, CreatorUrlInfo] = {}
        keyword = lookup_keyword.strip().lower()
        for entry in items:
            if not isinstance(entry, dict):
                continue
            user = embedded_note_user(entry)
            user_id = self._first_text(*(user.get(key) for key in USER_ID_KEYS))
            if not user_id:
                continue
            note_hint = note_display_title(entry)
            candidate = candidates_by_user.get(user_id)
            if candidate is None:
                candidates_by_user[user_id] = CreatorUrlInfo(
                    user_id=user_id,
                    xsec_token=self._first_text(*(user.get(key) for key in XSEC_TOKEN_KEYS)),
                    xsec_source=self._first_text(
                        *(entry.get(key) for key in XSEC_SOURCE_KEYS),
                        "pc_search",
                    ),
                    nickname=self._first_text(*(user.get(key) for key in AUTHOR_NAME_KEYS)),
                    red_id=self._first_text(*(user.get(key) for key in RED_ID_KEYS)),
                    note_hint=note_hint,
                )
            elif note_hint and not candidate.note_hint:
                candidate.note_hint = note_hint

        def _score(candidate: CreatorUrlInfo) -> tuple[int, int, int]:
            red_id = candidate.red_id.lower()
            nickname = candidate.nickname.lower()
            note_hint = candidate.note_hint.lower()
            red_id_exact = 0 if keyword and red_id == keyword else 1
            nickname_exact = 0 if keyword and nickname == keyword else 1
            text_contains = 0 if keyword and keyword in f"{red_id} {nickname} {note_hint}" else 1
            return (red_id_exact, nickname_exact, text_contains)

        return sorted(candidates_by_user.values(), key=_score)

    def _pick_creator_candidate(self, candidates: list[CreatorUrlInfo], *, source_label: str) -> CreatorUrlInfo | None:
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        dialog_items = [
            {
                "title": " / ".join(
                    part
                    for part in (candidate.nickname, candidate.red_id, candidate.note_hint[:24], candidate.user_id)
                    if part
                ),
                "index": idx,
            }
            for idx, candidate in enumerate(candidates)
        ]
        self.log(f"🧾 {source_label}共发现 {len(dialog_items)} 个账号候选，请选择主页...")
        selected = self.ask_user_selection(dialog_items)
        if not selected:
            self.log(f"⚠️ 用户取消了{source_label}账号选择流程")
            return None
        chosen_idx = selected[0]
        if not isinstance(chosen_idx, int) or chosen_idx < 0 or chosen_idx >= len(candidates):
            return None
        return candidates[chosen_idx]

    def _lookup_creator_by_keyword(self, client: XiaohongshuClient, lookup: CreatorLookupInfo) -> CreatorUrlInfo | None:
        self.log(f"🔍 正在搜索小红书账号: {lookup.keyword}")
        try:
            data = client.search_notes(keyword=lookup.keyword, page=1, page_size=20)
        except Exception as exc:
            self.log(f"⚠️ 小红书号预搜索失败，回退到网页用户搜索: {exc}")
            return self._lookup_creator_by_browser_search(lookup)
        candidates = self._extract_creator_candidates_from_note_search(data, lookup.keyword)
        if not candidates:
            self.log(f"⚠️ 预搜索未提取到小红书账号候选，回退网页用户搜索: {lookup.keyword}")
            return self._lookup_creator_by_browser_search(lookup)
        return self._pick_creator_candidate(candidates, source_label="预搜索")

    def _lookup_creator_by_browser_search(self, lookup: CreatorLookupInfo) -> CreatorUrlInfo | None:
        self.log(f"🌐 正在通过网页搜索小红书号: {lookup.keyword}")
        proxy = self._proxy()
        launch_kwargs: dict[str, Any] = {"headless": self._browser_headless()}
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(**launch_kwargs)
            self._track_playwright_instance(playwright)
            self._track_playwright_browser(browser)
            try:
                context = browser.new_context(
                    **self._playwright_context_kwargs(
                        user_agent=self._user_agent(),
                        referer=self.HOME_URL,
                        viewport={"width": 1280, "height": 800},
                    )
                )
                self._apply_stealth_to_context(context)
                if os.path.exists(self.auth_file):
                    try:
                        self.auth_service.restore_playwright_cookies(context, self.auth_file)
                    except (OSError, ValueError, RuntimeError, AttributeError) as exc:
                        debug_logger.log_exception("XiaohongshuSpider", "restore_playwright_cookies", exc)
                page = context.new_page()
                search_url = f"{self.HOME_URL}search_result?keyword={quote(lookup.keyword)}&type=51"
                timeout_ms = self._configured_timeout_ms(default=60)
                if not self._open_page(page, search_url, timeout_ms=timeout_ms, settle_ms=3000):
                    return None
                current_url = page.url
                if "/login" in current_url:
                    self.log("⚠️ 网页用户搜索被重定向到登录页，无法直接解析小红书号")
                    return None
                raw_candidates = page.locator("a[href*='/user/profile/']").evaluate_all(
                    """(elements) => elements.map((el) => ({
                        href: el.href || '',
                        text: (el.textContent || '').trim()
                    }))"""
                )
                candidates: list[CreatorUrlInfo] = []
                seen: set[str] = set()
                for item in raw_candidates:
                    href = str(item.get("href") or "").strip()
                    if not href:
                        continue
                    try:
                        parsed = parse_creator_info_from_url(href)
                    except ValueError:
                        continue
                    if parsed.user_id in seen:
                        continue
                    seen.add(parsed.user_id)
                    candidates.append(
                        CreatorUrlInfo(
                            user_id=parsed.user_id,
                            xsec_token=parsed.xsec_token,
                            xsec_source=parsed.xsec_source or "pc_search",
                            nickname=str(item.get("text") or "").strip(),
                        )
                    )
                if not candidates:
                    self.log(f"⚠️ 网页用户搜索未找到匹配的小红书号: {lookup.keyword}")
                    return None
                return self._pick_creator_candidate(candidates, source_label="网页搜索")
            finally:
                self._close_tracked_playwright_browser(browser)
                self._clear_playwright_instance(playwright)

    def _handle_multi_refs(self, client: XiaohongshuClient, refs: list[dict[str, str]], cookie_str: str) -> None:
        if not refs:
            self.log("⚠️ 未找到可处理的小红书结果")
            return

        if self._should_stream_download_items():
            self._handle_multi_refs_streaming(client, refs, cookie_str)
            return

        selected_refs = self._select_refs_before_detail(refs)
        if selected_refs is None:
            return
        if not selected_refs:
            self.log("No XiaoHongShu items selected; crawl finished without queueing downloads.")
            return
        self.log(f"XiaoHongShu user confirmed {len(selected_refs)} candidates; starting parse-to-download pipeline.")
        self._handle_multi_refs_streaming(
            client,
            selected_refs,
            cookie_str,
            require_user_confirmed=True,
        )
        return

    def _build_ref_selection_entry(self, ref: dict[str, Any], index: int) -> dict[str, Any]:
        title = self._first_text(ref.get("title"), ref.get("note_id"), f"XiaoHongShu note {index + 1}")
        author = self._first_text(ref.get("author"))
        note_type = self._first_text(ref.get("note_type"), ref.get("type"))
        parts = [title]
        if author:
            parts.append(author)
        if note_type:
            parts.append(note_type)
        return {
            "title": " | ".join(parts),
            "note_id": ref.get("note_id", ""),
            "note_type": note_type,
            "author": author,
            "index": index,
        }

    def _select_refs_before_detail(self, refs: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        if len(refs) == 1:
            return list(refs)
        selection_entries = [self._build_ref_selection_entry(ref, index) for index, ref in enumerate(refs)]
        self.log(f"XiaoHongShu found {len(selection_entries)} candidates; waiting for user confirmation before parsing details.")
        selected = self.ask_user_selection(selection_entries)
        if selected is None:
            self.log("XiaoHongShu selection was cancelled by the user.")
            return None
        selected_refs: list[dict[str, Any]] = []
        for idx in selected:
            if not isinstance(idx, int) or idx < 0 or idx >= len(refs):
                continue
            selected_refs.append(refs[idx])
        return selected_refs

    def _handle_multi_refs_streaming(
        self,
        client: XiaohongshuClient,
        refs: list[dict[str, str]],
        cookie_str: str,
        *,
        require_user_confirmed: bool = False,
    ) -> None:
        total_refs = len(refs)
        worker_count = self._detail_worker_count(total_refs)
        parsed = 0
        emitted = 0
        detail_failures = 0
        if require_user_confirmed:
            self.log(f"XiaoHongShu confirmed pipeline is active: {total_refs} selected candidates.")
        self.log(f"🚀 小红书流水线模式：详情解析成功后立即投递下载队列 | 候选 {total_refs}")

        def fetch_one(index_ref: tuple[int, dict[str, str]]) -> tuple[int, dict[str, Any] | None]:
            idx, ref = index_ref
            if not self.is_running or self._detail_circuit_is_open():
                return idx, None
            worker_client = self._build_client(cookie_str) if worker_count > 1 else client
            try:
                return idx, self._fetch_note_detail(worker_client, ref)
            finally:
                if worker_client is not client:
                    worker_client.close()

        indexed_refs = list(enumerate(refs, 1))
        # 并发任务各自持有 HTTP 会话；停止后取消尚未启动的任务，并等待已运行任务退出。
        executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="xhs-detail")
        future_map: dict[Any, int] = {}
        consumed: set[Any] = set()

        def consume(future) -> None:
            nonlocal parsed, emitted, detail_failures
            if future in consumed:
                return
            consumed.add(future)
            idx = future_map[future]
            try:
                _idx, detail = future.result()
            except CancelledError:
                return
            except Exception as exc:
                self.log(f"⚠️ 小红书笔记详情线程失败 {idx} | {exc}")
                detail = None
                detail_failures += 1
            if detail:
                parsed += 1
                referer = f"{self.HOME_URL}explore/{detail.get('note_id', '')}"
                emitted += self._emit_note_items(detail, cookie_str, referer=referer)
            if self._should_log_progress(len(consumed), total_refs):
                self.log(f"📥 已解析详情 {len(consumed)}/{total_refs} | 成功 {parsed} | 已投递 {emitted}")

        try:
            future_map = {executor.submit(fetch_one, index_ref): index_ref[0] for index_ref in indexed_refs}
            pending = set(future_map)
            while pending and self.is_running and not self._detail_circuit_is_open():
                done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for future in done:
                    consume(future)
                if not self.is_running or self._detail_circuit_is_open():
                    break
            for future in list(pending):
                if future.done():
                    pending.discard(future)
                    consume(future)
        finally:
            stop_pool = not self.is_running or self.interrupt_requested or self._detail_circuit_is_open()
            executor.shutdown(wait=True, cancel_futures=stop_pool)
            for future in list(future_map):
                if future.done():
                    consume(future)

        if not self.is_running and emitted > 0:
            self.log("🛑 小红书任务已停止，下载已入队，保持停止状态")
            return
        if not self.is_running and parsed > 0:
            self.log("🛑 小红书任务已停止，未投递的解析结果不会恢复运行")
            return
        if self._detail_circuit_is_open():
            raise SpiderParseError("小红书详情触发熔断，任务终止")
        if parsed <= 0 and detail_failures:
            raise SpiderParseError("小红书笔记详情解析失败")
        if not self.revive_for_partial_selection(parsed, "条候选笔记"):
            return
        if parsed <= 0:
            self.log("⚠️ 未能成功解析任何小红书笔记详情，全程未投递下载项")
            return
        self.log(f"✅ 小红书流水线投递完成，共投递 {emitted} 个下载项")

    def run(self) -> None:
        self.run_succeeded = False
        self.log(f"🚀 启动小红书任务 | 目标: {self.keyword}")
        self.debug_state(
            action="run_start",
            message="启动小红书爬虫任务",
            status_code="XHS_SPIDER_START",
            context={"keyword": self.keyword},
            details={"config": self.config},
        )
        try:
            normalized_keyword = self._normalize_keyword(self.keyword)
            if normalized_keyword != self.keyword.strip():
                self.log(f"🔗 小红书输入已归一化: {normalized_keyword}")
            route, route_value = self._classify_input(normalized_keyword)
            entry_url = (
                normalized_keyword
                if self._url_matches_hosts(normalized_keyword, self.PAGE_HOSTS)
                else self.HOME_URL
            )
            cookie_str = self._ensure_cookie_string(entry_url)
            if not cookie_str:
                raise SpiderAuthError("无法获取小红书会话 Cookie（至少需要 a1）")

            client = self._build_client(cookie_str)
            self._client = client
            if not client.check_cookie_ready():
                raise SpiderAuthError("小红书 Cookie 缺少 a1，无法进行签名请求")
            if self._login_probe_cached and self._login_probe_cookie == cookie_str:
                login_status = self._login_probe_status
            else:
                login_status = client.probe_login_status()
                self._login_probe_cookie = cookie_str
                self._login_probe_status = login_status
                self._login_probe_cached = True
            if login_status is False:
                raise SpiderAuthError("小红书 Cookie 无有效登录态，请重新登录后再试")
            if login_status is None:
                self.log("⚠️ 小红书登录态探活失败，继续尝试使用当前浏览器确认过的会话")

            if route == "note_url":
                self._handle_note_url(
                    client,
                    parse_note_info_from_note_url(route_value),
                    cookie_str,
                    referer=route_value,
                )
            elif route in {"creator_url", "creator_id"}:
                creator = parse_creator_info_from_url(route_value)
                refs = self._collect_creator_refs(client, creator)
                self._handle_multi_refs(client, refs, cookie_str)
            elif route == "creator_lookup":
                creator = self._lookup_creator_by_keyword(client, CreatorLookupInfo(keyword=route_value))
                if creator:
                    refs = self._collect_creator_refs(client, creator)
                else:
                    self.log(f"↩️ 小红书号未命中主页结果，回退为关键词搜索: {route_value}")
                    refs = self._collect_search_refs(client, route_value)
                self._handle_multi_refs(client, refs, cookie_str)
            else:
                refs = self._collect_search_refs(client, route_value)
                self._handle_multi_refs(client, refs, cookie_str)
            if self.is_running and not self.interrupt_requested:
                self.run_succeeded = True
        except (SpiderAuthError, SpiderParseError, ValueError, PlaywrightError) as exc:
            self.log(f"❌ 小红书任务失败: {exc}")
            self.debug_state(
                action="run_error",
                message="小红书爬虫运行异常",
                status_code="XHS_SPIDER_ERROR",
                details={"error": str(exc)},
            )
        except Exception as exc:
            self.log(f"💥 小红书运行时异常: {exc}")
            self.debug_state(
                action="run_error",
                message="小红书爬虫运行异常",
                status_code="XHS_SPIDER_ERROR",
                details={"error": str(exc)},
            )
        finally:
            client = getattr(self, "_client", None)
            if client is not None:
                client.close()
                self._client = None
            self.debug_state(
                action="run_finish",
                message="小红书爬虫任务结束",
                status_code="XHS_SPIDER_FINISH",
            )
            self._emit_finished()
