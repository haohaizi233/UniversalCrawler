"""Bilibili 扫描、解析与任务装配流水线。"""

import os
import re
import time
import requests
import urllib.parse
import threading
import queue
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import Error as PlaywrightError, sync_playwright
from app.config import DEFAULT_USER_AGENT, cfg, get_setting_default
from app.exceptions import (
    LoginCancelledError,
    LoginCheckError,
    LoginTimeoutError,
    SpiderAuthError,
    SpiderParseError,
)
from shared.runtime_options import DomainPolicyViolation
from app.spiders.base import BaseSpider
from app.spiders.bilibili import input_router
from app.spiders.bilibili.api_client import HEADERS, BiliAPI, BilibiliRequestStopped
from app.spiders.bilibili.input_router import BilibiliInputRoute
from app.spiders.bilibili.parser import BilibiliParser
from app.spiders.bilibili.task_builder import BilibiliTaskBuilder
from app.debug_logger import debug_logger
from app.models import VideoItem
from app.services.auth_service import AuthService
from app.utils.user_agents import resolve_user_agent
from shared.network_proxy import requests_proxy_mapping


@dataclass(frozen=True)
class BilibiliBrowserPageState:
    kind: str
    reason: str = ""
    terminal: bool = False


class BilibiliSpider(BaseSpider):
    """Bilibili 爬虫，采用浏览器扫描和 API 解析并行的流水线模型。"""

    BROWSER_EMPTY_TEXT_MARKERS = (
        "空间主人还没投过视频",
        "这里什么也没有",
        "暂无视频",
        "暂无投稿",
        "没有相关数据",
        "没有找到相关结果",
        "没有更多内容",
        "还没有发布任何视频",
        "未找到相关内容",
    )
    BROWSER_RISK_TEXT_MARKERS = (
        "安全验证",
        "验证码",
        "请完成验证",
        "访问过于频繁",
        "网络环境存在异常",
        "账号存在风险",
        "风控",
        "滑块验证",
        "人机验证",
    )
    BROWSER_LOGIN_TEXT_MARKERS = (
        "扫码登录",
        "密码登录",
        "请先登录",
        "登录后继续",
        "请登录后",
    )

    def __init__(self, keyword: str, config: dict):
        """配置解析器、任务装配器及线程私有的 API 客户端状态。"""
        super().__init__(keyword, config)
        self.parser = BilibiliParser()
        self.task_builder = BilibiliTaskBuilder(self.parser)
        self.auth_service = AuthService()
        self.user_agent = resolve_user_agent(
            "bilibili",
            self.config,
            configured_user_agent=cfg.get("bilibili", "user_agent", DEFAULT_USER_AGENT),
            default_user_agent=DEFAULT_USER_AGENT,
        )
        HEADERS["User-Agent"] = self.user_agent
        self._browser_thread: threading.Thread | None = None
        self._api_pool_thread: threading.Thread | None = None
        self._worker_api_local = threading.local()
        self._worker_apis: list[BiliAPI] = []
        self._worker_apis_lock = threading.RLock()

    def _bilibili_cookie_file(self) -> str:
        return cfg.get(
            "auth",
            "bilibili_cookie_file",
            cfg.get("bilibili", "auth_file", get_setting_default("bilibili", "auth_file")),
        )

    def _bilibili_request_timeout_seconds(self) -> int:
        return self._configured_timeout_seconds(
            default=cfg.get("bilibili", "timeout", get_setting_default("bilibili", "timeout"))
        )

    def _bilibili_api_worker_count(self, total: int | None = None) -> int:
        try:
            configured = int(
                (getattr(self, "config", {}) or {}).get(
                    "api_workers",
                    cfg.get("bilibili", "api_workers", get_setting_default("bilibili", "api_workers")),
                )
            )
        except (TypeError, ValueError):
            configured = int(get_setting_default("bilibili", "api_workers"))
        count = max(1, min(configured, 16))
        if total is not None:
            count = min(count, max(1, int(total)))
        return count

    def _worker_api_guard(self) -> threading.RLock:
        lock = getattr(self, "_worker_apis_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._worker_apis_lock = lock
        return lock

    def _worker_api_for_thread(self) -> BiliAPI:
        local = getattr(self, "_worker_api_local", None)
        if local is None:
            local = threading.local()
            self._worker_api_local = local
        api = getattr(local, "api", None)
        if api is not None:
            return api
        parser = getattr(self, "parser", None) or BilibiliParser()
        api = BiliAPI(self._bilibili_cookie_file(), parser=parser)
        api.request_timeout = self._bilibili_request_timeout_seconds()
        self._bind_api_stop_check(api)
        local.api = api
        with self._worker_api_guard():
            apis = getattr(self, "_worker_apis", None)
            if apis is None:
                apis = []
                self._worker_apis = apis
            apis.append(api)
        return api

    def _crawl_stopped(self) -> bool:
        """已初始化的运行标志或中断请求才算停止；裸构造的测试对象不算已停止。"""
        if bool(getattr(self, "_interrupt_requested", False)):
            return True
        if not hasattr(self, "_is_running"):
            return False
        return not self.is_running

    def _bind_api_stop_check(self, api: BiliAPI | None) -> None:
        if api is None:
            return
        api.stop_check = self._crawl_stopped

    def _close_api_sessions(self) -> None:
        self._close_worker_apis()
        api = getattr(self, "api", None)
        if api is None:
            return
        try:
            api.close()
        except Exception as exc:
            debug_logger.log_exception("BilibiliSpider", "close_api_session", exc)

    def _shutdown_executor(self, executor: ThreadPoolExecutor) -> None:
        """停止时关闭会话，并取消尚未开始的线程池任务。"""
        stopped = self._crawl_stopped()
        if stopped:
            self._close_api_sessions()
        executor.shutdown(wait=True, cancel_futures=stopped)

    def _close_worker_apis(self) -> None:
        with self._worker_api_guard():
            apis = list(getattr(self, "_worker_apis", []) or [])
            self._worker_apis = []
        for api in apis:
            try:
                api.close()
            except Exception as exc:
                debug_logger.log_exception("BilibiliSpider", "close_worker_api", exc)

    def _max_items_limit(self) -> int:
        default_limit = get_setting_default("bilibili", "max_items")
        value = (getattr(self, "config", {}) or {}).get(
            "max_items",
            cfg.get("bilibili", "max_items", default_limit),
        )
        try:
            return max(1, min(int(value), 9999))
        except (TypeError, ValueError):
            return int(default_limit)

    def _effective_scan_pages(self) -> int:
        default_pages = get_setting_default("bilibili", "max_pages")
        value = (getattr(self, "config", {}) or {}).get(
            "max_pages",
            cfg.get("bilibili", "max_pages", default_pages),
        )
        if str(value).strip().lower() in {"max", "unlimited"}:
            return 9999
        try:
            return max(1, min(int(value), 9999))
        except (TypeError, ValueError):
            return int(default_pages)

    def _collected_bvid_count(self, bv_set: set[str], excluded_bvids: set[str] | None = None) -> int:
        excluded = {self._normalize_bvid(value) for value in (excluded_bvids or set()) if value}
        return sum(1 for bvid in bv_set if bvid and bvid not in excluded)

    def _has_bvid_capacity(self, bv_set: set[str], excluded_bvids: set[str] | None = None) -> bool:
        return self._collected_bvid_count(bv_set, excluded_bvids) < self._max_items_limit()

    @staticmethod
    def _format_episode_choice(parent_title: str, episode: dict, fallback_index: int) -> str:
        """格式化第二层候选，保留父级标题，避免只剩 `[01] xxx` 的裸信息。"""
        parent = parent_title or "未命名项目"
        num_str = str(episode.get("page_num", fallback_index + 1)).zfill(2)
        episode_title = episode.get("title") or f"第 {fallback_index + 1} 集"
        return f"{parent} · P{num_str} · {episode_title}"

    def _record_episode_parse_failure(self, label: object) -> None:
        message = f"⚠️ 视频信息解析失败: {label}"
        self.log(message)
        debug = getattr(self, "debug_state", None)
        if not callable(debug):
            return
        debug(
            action="episode_list_invalid",
            message="Bilibili 分集为空或下标超出范围，已跳过",
            status_code="BILI_EPISODE_PARSE_FAIL",
            details={"label": str(label)},
            level="WARNING",
        )

    @staticmethod
    def _episode_index(value: object, size: int) -> int | None:
        try:
            index = int(value)
        except (TypeError, ValueError):
            return None
        if index < 0 or index >= size:
            return None
        return index

    def _build_selected_download_tasks(self, stage1_indices, cached_data: dict) -> list[dict]:
        """按用户选择装配下载任务；空分集或越界下标记为解析失败并跳过。"""
        final_download_queue: list[dict] = []
        max_download_items = self._max_items_limit()
        for idx in stage1_indices or []:
            if not self.is_running or len(final_download_queue) >= max_download_items:
                break
            item = cached_data.get(idx) if isinstance(cached_data, dict) else None
            if not isinstance(item, dict):
                self._record_episode_parse_failure(f"选择项不存在: {idx}")
                continue
            info = item.get("info") if isinstance(item.get("info"), dict) else {}
            episodes = info.get("episodes") or []
            if not isinstance(episodes, list) or not episodes:
                self._record_episode_parse_failure(info.get("title") or info.get("bvid") or "未知视频")
                continue
            item_type = item.get("type")
            if item_type == "single":
                ep = episodes[0]
                final_download_queue.append(
                    self.task_builder.build_single_task(
                        ep,
                        referer=f"https://www.bilibili.com/video/{ep['bvid']}",
                        video_title=info.get("title"),
                    )
                )
                continue
            sub_dialog_items = []
            remaining_slots = max_download_items - len(final_download_queue)
            parent_label = info.get("season_title") or info.get("title") or "未命名项目"
            selectable = episodes[:remaining_slots]
            for i, ep in enumerate(selectable):
                sub_dialog_items.append({
                    "title": ep.get("title") or f"第 {i + 1} 集",
                    "subtitle": f"P{str(ep.get('page_num', i + 1)).zfill(2)}",
                    "group_title": parent_label,
                    "index": i,
                })
            if len(episodes) > remaining_slots:
                self.log(f"ℹ️ 已按视频数上限 {max_download_items} 裁剪可选分集")
            self.log(f"🔔 正在展开: {info.get('season_title') or info.get('title') or '未命名项目'}")
            sub_indices = self.ask_user_selection(sub_dialog_items)
            if not sub_indices:
                continue
            for sub_idx in sub_indices:
                if len(final_download_queue) >= max_download_items:
                    break
                episode_index = self._episode_index(sub_idx, len(episodes))
                if episode_index is None:
                    self._record_episode_parse_failure(f"分集下标超出范围: {sub_idx}")
                    continue
                ep = episodes[episode_index]
                final_download_queue.append(self.task_builder.build_episode_task(info, ep, episode_index))
        return final_download_queue

    def _resolve_download_item(self, task: dict, api: BiliAPI | None = None) -> VideoItem | None:
        """把单个 Bilibili 任务解析成下载项，但暂不发射。"""
        self.log(f"🎬 解析流: {task['file_name'][:15]}...")
        try:
            api = api or self.api
            v_url, a_url, q_id = api.get_play_url(task['bvid'], task['cid'], trace_id=task['trace_id'])
            if not isinstance(v_url, str) or not v_url:
                self.log("   ❌ 获取流失败")
                self.debug_state(
                    action="resolve_stream_empty",
                    message="Bilibili 播放流响应为空",
                    status_code="BILI_STREAM_EMPTY",
                    context={"trace_id": task["trace_id"], "bvid": task["bvid"], "cid": task["cid"]},
                    details={
                        "file_name": task["file_name"],
                        "audio_url": a_url,
                        "quality_id": q_id,
                    },
                    level="WARNING",
                    trace_id=task["trace_id"],
                )
                return None

            q_map = {127: "8K", 120: "4K", 116: "1080P60", 80: "1080P", 64: "720P"}
            q_text = q_map.get(q_id, "高清")
            self.log(f"   ✨ 获取成功 [{q_text}]")
            folder_name = task.get("folder_name")
            proxy_str = self._effective_proxy_server((getattr(self, "config", {}) or {}).get("proxy"))
            # 登录 Cookie 会随 VideoItem.meta 广播到前端；下载器仍从 meta 读 cookie 时也不再写入。
            meta = {
                "trace_id": task["trace_id"],
                "content_type": "video",
                "media_label": "视频",
                "audio_url": a_url,
                "ua": getattr(self, "user_agent", HEADERS["User-Agent"]),
                "referer": task['referer'],
                "use_subdir": bool(folder_name),
                "bvid": task["bvid"],
                "cid": task["cid"],
                "preferred_filename": task["file_name"],
            }
            if proxy_str:
                meta["proxy"] = proxy_str
            if folder_name:
                meta["folder_name"] = folder_name
            item = VideoItem(url=v_url, title=os.path.splitext(task["file_name"])[0], source="bilibili")
            item.meta = meta
            self.debug_state(
                action="emit_download_task",
                message="Bilibili 下载任务已提交到下载队列",
                status_code="BILI_TASK_EMIT",
                context={"trace_id": task["trace_id"], "bvid": task["bvid"], "cid": task["cid"]},
                details={
                    "file_name": task["file_name"],
                    "folder_name": folder_name,
                    "quality_id": q_id,
                    "video_url": v_url,
                    "audio_url": a_url,
                },
                trace_id=task["trace_id"],
            )
            return item
        except BilibiliRequestStopped:
            return None
        except Exception as exc:
            self.log(f"   ❌ 获取流失败: {exc}")
            self.debug_state(
                action="resolve_stream_failed",
                message="Bilibili 获取播放流失败",
                status_code="BILI_STREAM_FAIL",
                context={"trace_id": task["trace_id"], "bvid": task["bvid"], "cid": task["cid"]},
                details={
                    "file_name": task["file_name"],
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                level="ERROR",
                trace_id=task["trace_id"],
            )
            return None

    def _process_download_task(self, task: dict) -> bool:
        """逐条取流并提交下载，单条失败不影响后续任务继续执行。"""
        item = self._resolve_download_item(task, api=getattr(self, "api", None))
        if not isinstance(item, VideoItem):
            self.interruptible_sleep(0.5)
            return False
        self.emit_video(url=item.url, title=item.title, source=item.source, meta=item.meta)
        self.interruptible_sleep(0.5)
        return True

    def _process_download_tasks_async(self, tasks: list[dict]) -> tuple[int, int]:
        """并发解析播放地址，再批量发射已就绪的下载项。"""
        task_list = list(tasks or [])
        if not task_list:
            return 0, 0
        worker_count = self._bilibili_api_worker_count(len(task_list))
        if worker_count <= 1 or len(task_list) == 1:
            success_count = 0
            failure_count = 0
            for task in task_list:
                if not self.is_running or self.interrupt_requested:
                    break
                try:
                    succeeded = self._process_download_task(task)
                except Exception as exc:
                    failure_count += 1
                    self.log(f"   ❌ 获取流失败: {exc}")
                    continue
                if succeeded:
                    success_count += 1
                else:
                    failure_count += 1
            return success_count, failure_count

        self.debug_state(
            action="download_submit_pool_start",
            message="Bilibili 并发解析播放流并批量提交下载项",
            status_code="BILI_SUBMIT_POOL_START",
            details={"task_count": len(task_list), "worker_count": worker_count},
        )
        success_count = 0
        failure_count = 0
        ready_items: list[VideoItem] = []

        def resolve_one(task: dict) -> VideoItem | None:
            if not self.is_running or self.interrupt_requested:
                return None
            return self._resolve_download_item(task, api=self._worker_api_for_thread())

        executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="bili-submit")
        try:
            future_map = {executor.submit(resolve_one, task): task for task in task_list}
            for future in as_completed(future_map):
                if not self.is_running or self.interrupt_requested:
                    break
                try:
                    item = future.result()
                except Exception as exc:
                    task = future_map[future]
                    failure_count += 1
                    self.log(f"   ❌ 获取流失败: {exc}")
                    self.debug_state(
                        action="resolve_stream_worker_failed",
                        message="Bilibili 并发取流线程失败",
                        status_code="BILI_STREAM_WORKER_FAIL",
                        context={
                            "trace_id": task.get("trace_id"),
                            "bvid": task.get("bvid"),
                            "cid": task.get("cid"),
                        },
                        details={"file_name": task.get("file_name"), "error": str(exc)},
                        level="ERROR",
                        trace_id=task.get("trace_id"),
                    )
                    continue
                if not isinstance(item, VideoItem):
                    failure_count += 1
                    continue
                ready_items.append(item)
                success_count += 1
        finally:
            self._shutdown_executor(executor)

        # worker 只汇总结果，统一由当前线程批量发射，避免并发回调持续挤压宿主队列。
        emitted = self.emit_videos(ready_items)
        if emitted != len(ready_items):
            failure_count += len(ready_items) - emitted
            success_count = max(0, success_count - (len(ready_items) - emitted))
        return success_count, failure_count

    def run(self):
        """执行登录回退、并行扫描解析、交互选择和下载任务提交。"""
        self.run_succeeded = False

        def succeed() -> None:
            if self.is_running and not self.interrupt_requested:
                self.run_succeeded = True

        try:
            self.debug_state(
                action="run_start",
                message="启动 Bilibili 爬虫任务",
                status_code="BILI_SPIDER_START",
                context={"keyword": self.keyword},
                details={"config": self.config},
            )
            if not self.is_running or self.interrupt_requested:
                return
            cookie_file = self._bilibili_cookie_file()
            self.api = BiliAPI(cookie_file, parser=self.parser)
            self.api.request_timeout = self._bilibili_request_timeout_seconds()
            self._bind_api_stop_check(self.api)
            # 登录探测异常仍可尝试扫码；扫码失败再降级游客态，公开内容不会被认证故障阻断。
            try:
                is_logged_in = self.api.check_login()
            except BilibiliRequestStopped:
                return
            except LoginCheckError as exc:
                self.log(f"⚠️ 登录状态校验失败，尝试继续执行: {exc}")
                is_logged_in = False
            if not self.is_running or self.interrupt_requested:
                return
            if not is_logged_in:
                self.log("🔒 未检测到登录，启动浏览器扫码...")
                try:
                    self._perform_login_scan(cookie_file)
                except LoginCancelledError:
                    return
                except BilibiliRequestStopped:
                    return
                except (PlaywrightError, OSError) as exc:
                    self.log(f"❌ Bilibili 登录浏览器异常，任务结束: {exc}")
                    return
                except SpiderAuthError as exc:
                    self.log(f"⚠️ 登录失败，以游客身份爬取: {exc}")
                else:
                    self.log("✅ 已完成扫码登录")
            else:
                self.log("👤 已登录，Cookie 有效")
            if not self.is_running or self.interrupt_requested:
                return
            self.log(f"🚀 启动 Bilibili 任务 | 目标: {self.keyword}")
            # 固定 API worker 数只限制同时执行的请求；原始队列和执行器待执行项均无容量上限，
            # 生产速度更快时仍可能在内存积压，解析结果再回流主线程。
            self.raw_bv_queue = queue.Queue()
            self.parsed_info_queue = queue.Queue()
            self.browser_finished = threading.Event()
            self.api_pool_finished = threading.Event()
            self.api_failure_queue = queue.Queue()
            # 生产者线程负责翻页找 BV，加工线程负责打 API，主线程负责聚合和交互。
            self._browser_thread = threading.Thread(target=self._producer_browser_task, name="bilibili-browser")
            self._browser_thread.start()
            self._api_pool_thread = threading.Thread(target=self._worker_api_pool, name="bilibili-api")
            self._api_pool_thread.start()
            display_items = []
            cached_data = {}
            seen_season_ids = set()
            seen_bvid_singles = set()
            valid_idx = 0
            self.log("⚡ 流水线已建立: 扫描 -> 解析 -> 聚合 同时进行中...")
            while True:
                if not self.is_running:
                    break
                try:
                    info = self.parsed_info_queue.get(timeout=0.5)
                    # 主线程只维护展示项和二次选择所需缓存，避免后台线程直接碰 UI 数据。
                    valid_idx = self._append_bilibili_info_for_selection(
                        info,
                        display_items,
                        cached_data,
                        seen_season_ids,
                        seen_bvid_singles,
                        valid_idx,
                    )
                    if valid_idx % 5 == 0:
                        self.log(f"   📊 已聚合 {valid_idx} 个有效资源...")
                except queue.Empty:
                    if self.api_pool_finished.is_set():
                        break
                    continue
            self._join_worker_thread(self._browser_thread, "browser")
            self._join_worker_thread(self._api_pool_thread, "api")
            if not self.is_running:
                self.log("⏹️ Bilibili 爬虫已停止，跳过结果选择")
                return
            if not display_items:
                api_failures = self._collect_api_failures(limit=20)
                valid_idx = self._try_api_failure_browser_fallback(
                    api_failures,
                    display_items,
                    cached_data,
                    seen_season_ids,
                    seen_bvid_singles,
                    valid_idx,
                    max_pages=self._effective_scan_pages(),
                )
                if not display_items:
                    self._log_api_failure_summary(failures=api_failures)
            if not display_items:
                self.log("❌ 未找到任何有效视频")
                succeed()
                return
            # 第一阶段选择视频、合集或多 P 项目。
            self.log(f"🔔 扫描结束，共 {len(display_items)} 个项目，请选择...")
            stage1_indices = self.ask_user_selection(display_items)
            if not stage1_indices:
                self.log("❌ 用户取消下载")
                succeed()
                return
            # 第二阶段展开分集选择，并装配最终下载队列。
            final_download_queue = self._build_selected_download_tasks(stage1_indices, cached_data)
            max_download_items = self._max_items_limit()
            if len(final_download_queue) >= max_download_items:
                self.log(f"ℹ️ 已达到视频数上限 {max_download_items}，剩余选择不会进入下载队列")
            self.log(f"✅ 最终确认 {len(final_download_queue)} 个任务，开始下载...")
            self.debug_state(
                action="download_queue_ready",
                message="Bilibili 下载任务已装配完成",
                status_code="BILI_QUEUE_READY",
                details={
                    "task_count": len(final_download_queue),
                    "sample_tasks": [task["file_name"] for task in final_download_queue[:5]],
                },
            )
            success_count, failure_count = self._process_download_tasks_async(final_download_queue)
            self.log(f"🎉 全部完成: 成功 {success_count}/{len(final_download_queue)} | 失败 {failure_count}")
            succeed()
        finally:
            # worker 可能仍在请求超时窗口内；关闭会话后只做有界等待，防止退出永久挂起。
            self._close_worker_apis()
            api = getattr(self, "api", None)
            if api is not None:
                api.close()
            self._join_worker_thread(self._browser_thread, "browser")
            self._join_worker_thread(self._api_pool_thread, "api")
            self._browser_thread = None
            self._api_pool_thread = None
            self.debug_state(
                action="run_finish",
                message="Bilibili 爬虫任务结束",
                status_code="BILI_SPIDER_FINISH",
            )
            self._emit_finished()

    def _join_worker_thread(self, thread: threading.Thread | None, label: str, timeout: float = 5.0) -> None:
        """等待后台线程退出，避免 stop() 因无超时 join 导致主流程长期卡住。"""
        if thread is None:
            return
        thread.join(timeout=timeout)
        if thread.is_alive():
            self.log(f"⚠️ {label} 线程未在 {timeout:.0f}s 内退出，跳过继续收尾")
    def _producer_browser_task(self):
        """路由原始输入，并把 bvid/aid 或网页扫描结果送入原始队列。"""
        try:
            max_pages = self._effective_scan_pages()
            route = self._classify_input(self.keyword)
            self._execute_input_route(route, max_pages=max_pages)
        except (PlaywrightError, ValueError, RuntimeError) as e:
            self.log(f"Bilibili browser producer error: {e}")
        finally:
            self.browser_finished.set()

    URL_TRAILING_PUNCTUATION = input_router.URL_TRAILING_PUNCTUATION
    COLLECTION_PATH_MARKERS = input_router.COLLECTION_PATH_MARKERS
    COLLECTION_QUERY_KEYS = input_router.COLLECTION_QUERY_KEYS
    UID_LABEL_PATTERN = input_router.UID_LABEL_PATTERN
    BVID_TEXT_PATTERN = input_router.BVID_TEXT_PATTERN
    AVID_TEXT_PATTERN = input_router.AVID_TEXT_PATTERN
    SHORT_LINK_HOSTS = input_router.SHORT_LINK_HOSTS
    MIN_PLAIN_UID_DIGITS = input_router.MIN_PLAIN_UID_DIGITS

    def _execute_input_route(self, route: BilibiliInputRoute, *, max_pages: int) -> None:
        if route.kind == "bvid":
            self.log("Bilibili route: direct BV video")
            self.raw_bv_queue.put(route.value)
            return
        if route.kind == "bvid_with_fallback":
            self.log("Bilibili route: direct BV video with search fallback")
            self.raw_bv_queue.put(route.value)
            kwargs = dict(route.scan_kwargs or {})
            exclude_bvids = set(kwargs.pop("exclude_bvids", set()) or set())
            exclude_bvids.add(route.value)
            fallback_urls = kwargs.pop("fallback_urls", None)
            fallback_url = str(kwargs.pop("fallback_url", "") or "").strip()
            if not fallback_urls and fallback_url:
                fallback_urls = [fallback_url]
            static_search_attempted = False
            static_found = False
            for fallback_url in fallback_urls or []:
                if self._crawl_stopped():
                    break
                fallback_url = str(fallback_url or "").strip()
                if not fallback_url:
                    continue
                scan_kwargs = dict(kwargs)
                is_search_target = "search.bilibili.com" in fallback_url
                if is_search_target:
                    scan_kwargs["is_search"] = True
                elif static_search_attempted and not static_found:
                    self.log("⚠️ 静态搜索没有结果，不再启动浏览器")
                    continue
                found = self._scan_with_browser_queue(
                    fallback_url,
                    max_pages=max_pages,
                    exclude_bvids=exclude_bvids,
                    **scan_kwargs,
                )
                if is_search_target:
                    static_search_attempted = True
                if found:
                    static_found = True
            return
        if route.kind == "aid":
            self.log("Bilibili route: direct av video")
            self.raw_bv_queue.put({"aid": route.value})
            return
        if route.kind == "keyword":
            self.log("Bilibili route: keyword search")
            self._scan_with_browser_queue(route.value, max_pages, is_search=True, is_space=False)
            return
        kwargs = dict(route.scan_kwargs or {})
        self.log(f"Bilibili route: browser scan {route.value}")
        self._scan_with_browser_queue(route.value, max_pages=max_pages, **kwargs)

    def _extract_first_url(self, raw_text: str) -> str:
        return input_router.extract_first_url(raw_text)

    @classmethod
    def _strip_url_trailing_punctuation(cls, value: str) -> str:
        return input_router.strip_url_trailing_punctuation(value)

    def _resolve_short_share_url(self, url: str) -> str:
        """把 Bilibili 分享短链解析为最终目标 URL。"""
        candidate = str(url or "").strip()
        if candidate and not candidate.lower().startswith(("http://", "https://")):
            if any(host in candidate.lower() for host in self.SHORT_LINK_HOSTS):
                candidate = f"https://{candidate.lstrip('/')}"
            elif candidate.lower().startswith(("www.bilibili.com/", "bilibili.com/")):
                candidate = f"https://{candidate.lstrip('/')}"
        if not self._url_matches_hosts(candidate, self.SHORT_LINK_HOSTS, allow_subdomains=False):
            return candidate
        if self._crawl_stopped():
            return candidate
        try:
            request_timeout = getattr(
                getattr(self, "api", None),
                "request_timeout",
                self._configured_timeout_seconds(
                    default=cfg.get("bilibili", "timeout", get_setting_default("bilibili", "timeout"))
                ),
            )
            proxy = self._effective_proxy_server((getattr(self, "config", {}) or {}).get("proxy"))
            proxies = requests_proxy_mapping(proxy)
            request_kwargs = self._restricted_public_request_kwargs(
                candidate,
                allowed_hosts=(*self.SHORT_LINK_HOSTS, "bilibili.com"),
            )
            response = requests.get(
                candidate,
                headers=HEADERS,
                timeout=request_timeout,
                allow_redirects=True,
                proxies=proxies,
                **request_kwargs,
            )
            resolved = response.url or candidate
            if hasattr(self, "sig_log"):
                self.log(f"🔗 [短链解析] {candidate} -> {resolved}")
            return resolved
        except (requests.RequestException, DomainPolicyViolation) as exc:
            if hasattr(self, "sig_log"):
                self.log(f"⚠️ [短链解析失败] {exc}")
            return candidate

    def _normalize_keyword(self, raw_text: str) -> str:
        """路由分类前清理分享文案并解析短链。"""
        extracted = self._extract_first_url(raw_text)
        return self._resolve_short_share_url(extracted)

    def _classify_input(self, raw_text: str) -> BilibiliInputRoute:
        return input_router.classify_input(raw_text, normalize_keyword=self._normalize_keyword)

    def _keyword_route(self, keyword: str) -> BilibiliInputRoute:
        return input_router.keyword_route(keyword)

    @classmethod
    def _looks_like_collection_bvid_hint(cls, raw_text: str) -> bool:
        return input_router.looks_like_collection_bvid_hint(raw_text)

    def _collection_bvid_fallback_urls(self, bvid: str, raw_text: str = "") -> list[str]:
        return input_router.collection_bvid_fallback_urls(bvid, raw_text)

    def _record_api_failure(self, raw_id, error: dict[str, object] | None) -> None:
        if not isinstance(error, dict) or not error:
            return
        failure_queue = getattr(self, "api_failure_queue", None)
        if failure_queue is None:
            return
        try:
            failure_queue.put_nowait(
                {
                    "raw_id": raw_id,
                    "code": error.get("code"),
                    "message": error.get("message"),
                    "http_status": error.get("http_status"),
                }
            )
        except queue.Full:
            pass

    def _collect_api_failures(self, *, limit: int = 5) -> list[dict[str, object]]:
        failure_queue = getattr(self, "api_failure_queue", None)
        if failure_queue is None:
            return []
        failures = []
        while len(failures) < max(1, limit):
            try:
                failures.append(failure_queue.get_nowait())
            except queue.Empty:
                break
        return failures

    def _log_api_failure_summary(self, *, limit: int = 5, failures: list[dict[str, object]] | None = None) -> None:
        if failures is None:
            failures = self._collect_api_failures(limit=limit)
        if not failures:
            return
        parts = []
        for failure in failures:
            raw_id = failure.get("raw_id")
            code = failure.get("code")
            message = failure.get("message") or "unknown"
            status = failure.get("http_status")
            status_part = f", http={status}" if status else ""
            parts.append(f"{raw_id}: code={code}{status_part}, message={message}")
        self.log("Bilibili API did not return usable video details: " + " | ".join(parts))

    def _append_bilibili_info_for_selection(
        self,
        info: dict,
        display_items: list[dict],
        cached_data: dict,
        seen_season_ids: set,
        seen_bvid_singles: set,
        valid_idx: int,
    ) -> int:
        """把一条解析结果加入两阶段选择缓存，并完成合集去重。"""
        if len(display_items) >= self._max_items_limit():
            return valid_idx
        if info.get("is_season"):
            sid = info.get("season_id")
            if sid in seen_season_ids:
                return valid_idx
            seen_season_ids.add(sid)
            count = len(info.get("episodes") or [])
            title_str = f"【合集】{info.get('season_title') or info.get('title')} (共 {count} 集) - {info.get('owner')}"
            display_items.append({"title": title_str, "index": valid_idx})
            cached_data[valid_idx] = {"type": "season", "info": info}
            return valid_idx + 1

        bvid = info.get("bvid")
        if bvid in seen_bvid_singles:
            return valid_idx
        seen_bvid_singles.add(bvid)
        count = len(info.get("episodes") or [])
        if count > 1:
            title_str = f"【多P】{info.get('title')} (共 {count} P) - {info.get('owner')}"
            item_type = "multi_p"
        else:
            title_str = f"【视频】{info.get('title')} - {info.get('owner')}"
            item_type = "single"
        display_items.append({"title": title_str, "index": valid_idx})
        cached_data[valid_idx] = {"type": item_type, "info": info}
        return valid_idx + 1

    def _fallback_scan_urls_for_api_failure(self, raw_id: object) -> list[str]:
        if isinstance(raw_id, dict):
            raw_id = raw_id.get("bvid") or raw_id.get("aid") or ""
        bvid = self._bvid_from_text(str(raw_id or ""))
        if not bvid:
            return []
        return self._collection_bvid_fallback_urls(bvid, str(getattr(self, "keyword", "") or bvid))

    def _try_api_failure_browser_fallback(
        self,
        failures: list[dict[str, object]],
        display_items: list[dict],
        cached_data: dict,
        seen_season_ids: set,
        seen_bvid_singles: set,
        valid_idx: int,
        *,
        max_pages: int,
    ) -> int:
        """仅在 BV 直连接口无可用结果后启用浏览器兜底提取。"""
        if not failures or not self.is_running:
            return valid_idx
        candidate_bvids: list[str] = []
        seen_candidates: set[str] = set()
        failed_bvids = {
            bvid
            for failure in failures
            for bvid in [self._bvid_from_text(str(failure.get("raw_id") or ""))]
            if bvid
        }
        static_search_attempted = False
        static_found = False
        for failure in failures:
            raw_id = failure.get("raw_id")
            for url in self._fallback_scan_urls_for_api_failure(raw_id):
                if self._crawl_stopped():
                    return valid_idx
                is_search_target = "search.bilibili.com" in url
                if not is_search_target and static_search_attempted and not static_found:
                    self.log("⚠️ 静态搜索没有结果，不再启动浏览器")
                    continue
                self.log(f"↩️ Bilibili API 失败，尝试网页兜底扫描: {url}")
                scan_kwargs = {"is_search": is_search_target, "is_space": False}
                found = self._scan_with_browser_queue(
                    url,
                    max_pages=max_pages,
                    enqueue=False,
                    exclude_bvids=failed_bvids,
                    **scan_kwargs,
                )
                if is_search_target:
                    static_search_attempted = True
                if found:
                    static_found = True
                for bvid in found or []:
                    if bvid in seen_candidates:
                        continue
                    seen_candidates.add(bvid)
                    candidate_bvids.append(bvid)
        if not candidate_bvids:
            self.log(
                "Bilibili browser fallback did not find additional BV candidates; "
                "the source BV may be unavailable/private, or the collection/UP homepage link is required."
            )
            return valid_idx
        for bvid in candidate_bvids:
            if not self.is_running:
                break
            if bvid in seen_bvid_singles:
                continue
            try:
                info = self.api.get_video_info(bvid, trace_id=f"bilibili_fallback_{bvid}")
            except SpiderParseError as exc:
                self.log(f"⚠️ Bilibili 网页兜底解析失败: {bvid} | {exc}")
                continue
            if not info:
                continue
            valid_idx = self._append_bilibili_info_for_selection(
                info,
                display_items,
                cached_data,
                seen_season_ids,
                seen_bvid_singles,
                valid_idx,
            )
        return valid_idx

    def _route_url(self, url: str) -> BilibiliInputRoute:
        return input_router.route_url(url)

    @classmethod
    def _is_collection_like_url(cls, parsed: urllib.parse.ParseResult) -> bool:
        return input_router.is_collection_like_url(parsed)

    @staticmethod
    def _is_bvid_ugc_season_entry_url(parsed: urllib.parse.ParseResult) -> bool:
        return input_router.is_bvid_ugc_season_entry_url(parsed)

    @staticmethod
    def _normalize_bvid(value: str) -> str:
        return input_router.normalize_bvid(value)

    @classmethod
    def _bvid_from_url(cls, url: str) -> str:
        return input_router.bvid_from_url(url)

    @classmethod
    def _bvid_from_text(cls, text: str) -> str:
        return input_router.bvid_from_text(text)

    @classmethod
    def _aid_from_text(cls, text: str) -> str:
        return input_router.aid_from_text(text)

    @staticmethod
    def _aid_from_url(url: str) -> str:
        return input_router.aid_from_url(url)

    def _worker_api_pool(self):
        """消费原始 ID 队列，用固定线程池解析详情并提交结构化结果。"""
        def process_one(raw_id):
            """把队列中的 bvid/aid 解析为结构化视频信息。"""
            if not self.is_running or self.interrupt_requested:
                return None, None
            api = self._worker_api_for_thread()
            try:
                if isinstance(raw_id, dict):
                    aid = str(raw_id.get("aid") or "").strip()
                    if aid:
                        result = api.get_video_info(None, trace_id=f"bilibili_av{aid}", aid=aid)
                        error = None if result else api.consume_video_info_error(aid)
                        return result, error
                    bvid = str(raw_id.get("bvid") or "").strip()
                else:
                    bvid = str(raw_id or "").strip()
                if not bvid:
                    return None, None
                result = api.get_video_info(bvid, trace_id=f"bilibili_{bvid}")
                error = None if result else api.consume_video_info_error(bvid)
                return result, error
            except BilibiliRequestStopped:
                return None, None

        try:
            api_workers = self._bilibili_api_worker_count()
        except (TypeError, ValueError):
            api_workers = 1
        executor = ThreadPoolExecutor(max_workers=api_workers, thread_name_prefix="bili-detail")
        try:
            while True:
                if not self.is_running or self.interrupt_requested:
                    break
                try:
                    bvid = self.raw_bv_queue.get(timeout=0.5)
                    future = executor.submit(process_one, bvid)
                    def callback(f, raw_id=bvid):
                        
                        if not self.is_running or self.interrupt_requested:
                            return
                        try:
                            res, error = f.result()
                            if res:
                                self.parsed_info_queue.put(res)
                            else:
                                if isinstance(error, dict) and error:
                                    self._record_api_failure(raw_id, error)
                                    self.log(
                                        f"⚠️ Bilibili API 未返回可用视频信息: {raw_id} "
                                        f"(code={error.get('code')}, message={error.get('message')})"
                                    )
                                else:
                                    self.log(f"⚠️ Bilibili API 未返回可用视频信息: {raw_id}")
                        except BilibiliRequestStopped:
                            return
                        except SpiderParseError as exc:
                            self.log(f"⚠️ 视频信息解析失败: {exc}")
                        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
                            self.log(f"⚠️ API 处理异常: {exc}")
                    future.add_done_callback(callback)
                except queue.Empty:
                    if self.browser_finished.is_set():
                        break
                    continue
        finally:
            # 停止时关闭会话并取消尚未启动的 future；无论哪条路径，都等已运行回调结束后再置完成事件。
            self._shutdown_executor(executor)
            self.api_pool_finished.set()

    def _restore_scan_cookies(self, page) -> None:
        context = getattr(page, "context", None)
        if context is None:
            return
        try:
            cookie_file = cfg.get(
                "auth",
                "bilibili_cookie_file",
                cfg.get("bilibili", "auth_file", get_setting_default("bilibili", "auth_file")),
            )
            auth_service = getattr(self, "auth_service", None) or AuthService()
            auth_service.restore_playwright_cookies(context, cookie_file)
        except Exception as exc:
            self.log(f"⚠️ Bilibili 扫描 Cookie 恢复失败，继续匿名扫描: {exc}")

    def _is_bilibili_error_page(self, page) -> bool:
        """在不可用视频页跳转前识别其错误状态。"""
        try:
            return bool(
                page.evaluate(
                    r"""() => {
                    const initialState = window.__INITIAL_STATE__;
                    if (initialState && initialState.error && (initialState.error.code || initialState.error.trueCode)) {
                        return true;
                    }
                    const title = String(document.title || '');
                    const bodyText = String((document.body && document.body.innerText) || '').slice(0, 1200);
                    return /视频去哪了|视频不见了|稿件不可见|已被UP主删除|无法观看/.test(`${title}\n${bodyText}`);
                }"""
                )
            )
        except (PlaywrightError, TypeError, ValueError):
            return False

    @classmethod
    def _classify_bilibili_page_snapshot(cls, snapshot: dict | None) -> BilibiliBrowserPageState:
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        ready_state = str(snapshot.get("ready_state") or "").lower()
        url = str(snapshot.get("url") or "")
        title = str(snapshot.get("title") or "")
        body_text = str(snapshot.get("body_text") or "")
        combined_text = f"{title}\n{body_text}"
        try:
            candidate_count = int(snapshot.get("candidate_count") or 0)
        except (TypeError, ValueError):
            candidate_count = 0
        try:
            risk_marker_count = int(snapshot.get("risk_marker_count") or 0)
        except (TypeError, ValueError):
            risk_marker_count = 0
        try:
            login_marker_count = int(snapshot.get("login_marker_count") or 0)
        except (TypeError, ValueError):
            login_marker_count = 0

        if ready_state not in {"interactive", "complete"}:
            return BilibiliBrowserPageState("not_loaded", f"readyState={ready_state or 'unknown'}")
        if candidate_count > 0:
            return BilibiliBrowserPageState("ready", f"candidates={candidate_count}")
        if risk_marker_count > 0 or any(marker in combined_text for marker in cls.BROWSER_RISK_TEXT_MARKERS):
            return BilibiliBrowserPageState("risk", "检测到安全验证或风控提示", terminal=True)
        if login_marker_count > 0 or any(marker in combined_text for marker in cls.BROWSER_LOGIN_TEXT_MARKERS):
            return BilibiliBrowserPageState("login", "检测到登录拦截提示", terminal=True)
        has_empty_marker = any(marker in combined_text for marker in cls.BROWSER_EMPTY_TEXT_MARKERS)
        has_nonzero_video_counter = bool(re.search(r"(?:视频|投稿)\s*(?:999\+|[1-9]\d*)", combined_text))
        if has_empty_marker and has_nonzero_video_counter:
            return BilibiliBrowserPageState("risk", "页面空态与非零视频计数矛盾，疑似风控或接口拦截", terminal=True)
        if has_empty_marker:
            return BilibiliBrowserPageState("empty", "页面明确返回空结果", terminal=True)
        if url and url != "about:blank" and not body_text.strip():
            return BilibiliBrowserPageState("not_loaded", "页面主体为空")
        return BilibiliBrowserPageState("unknown", "页面已加载但暂未发现候选视频")

    def _read_bilibili_browser_page_state(self, page) -> BilibiliBrowserPageState:
        try:
            snapshot = page.evaluate(
                r"""() => {
                const riskSelector = [
                    'iframe[src*="captcha"]',
                    'iframe[src*="geetest"]',
                    '[class*="captcha"]',
                    '[class*="geetest"]',
                    '[id*="captcha"]',
                    '[id*="geetest"]'
                ].join(',');
                const loginSelector = [
                    '.login-panel',
                    '.bili-mini-mask',
                    '.bili-login',
                    '.login-scan-box',
                    '[class*="login"] [class*="scan"]'
                ].join(',');
                return {
                    ready_state: document.readyState || '',
                    title: document.title || '',
                    url: location.href || '',
                    body_text: String((document.body && document.body.innerText) || '').slice(0, 3000),
                    candidate_count: document.querySelectorAll(
                        'a[href*="/video/BV"], [data-bvid], .bili-video-card, .video-card'
                    ).length,
                    risk_marker_count: document.querySelectorAll(riskSelector).length,
                    login_marker_count: document.querySelectorAll(loginSelector).length
                };
            }"""
            )
        except (PlaywrightError, TypeError, ValueError):
            return BilibiliBrowserPageState("unknown", "无法读取页面状态")
        return self._classify_bilibili_page_snapshot(snapshot)

    def _log_bilibili_terminal_page_state(
        self,
        state: BilibiliBrowserPageState,
        *,
        url: str,
        page_count: int | None = None,
    ) -> None:
        if state.kind == "empty":
            message = "⚠️ Bilibili 页面已加载，但当前页面明确没有可采集视频"
            level = "WARN"
            status_code = "BILI_PAGE_EMPTY"
        elif state.kind == "risk":
            message = "⛔ Bilibili 页面已加载，但疑似触发安全验证或风控"
            level = "ERROR"
            status_code = "BILI_RISK_CONTROL"
        elif state.kind == "login":
            message = "🔒 Bilibili 页面已加载，但被登录提示拦截"
            level = "WARN"
            status_code = "BILI_LOGIN_REQUIRED"
        else:
            return
        self.log(f"{message}: {state.reason}")
        self.debug_state(
            "browser_page_terminal_state",
            message=message,
            status_code=status_code,
            details={"url": url, "page": page_count, "state": state.kind, "reason": state.reason},
            level=level,
        )

    @staticmethod
    def _space_video_url_from_href(href: str) -> str:
        href_text = str(href or "").strip()
        if href_text.startswith("//"):
            href_text = f"https:{href_text}"
        uid_match = re.search(r"space\.bilibili\.com/(\d+)", href_text)
        if not uid_match:
            return ""
        return f"https://space.bilibili.com/{uid_match.group(1)}/video"

    @classmethod
    def _should_use_static_search_shortcut(cls, url: str) -> bool:
        parsed = urllib.parse.urlparse(str(url or ""))
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        keyword = " ".join(query.get("keyword", []))
        decoded = urllib.parse.unquote(f"{keyword} {url}")
        lowered = decoded.lower()
        collection_markers = (
            "collection",
            "season",
            "series",
            "\u5408\u96c6",
            "\u7cfb\u5217",
            "\u5217\u8868",
        )
        return bool(cls._bvid_from_text(decoded) or any(marker in lowered for marker in collection_markers))

    def _extract_search_up_space_video_url(self, page) -> tuple[str, str]:
        try:
            candidate = page.evaluate(
                r"""() => {
                const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
                const classChain = (node) => {
                    const parts = [];
                    let current = node;
                    for (let depth = 0; current && depth < 5; depth += 1, current = current.parentElement) {
                        parts.push(`${current.id || ''} ${current.className || ''}`);
                    }
                    return parts.join(' ').toLowerCase();
                };
                const userContextRe = /(user-list|b-user-video-card|user-card|search-user|upuser|user-item|bili-user)/;
                const videoContextRe = /(bili-video-card|video-card|video-list)/;
                const nameOrAvatarRe = /(avatar|face|user-name|username|name|up-name|author)/;
                const profileHintRe = /(\u7c89\u4e1d|\u6295\u7a3f|\u89c6\u9891|\u5173\u6ce8|UP\u4e3b|\u7a7a\u95f4)/;
                const scored = [];
                for (const anchor of document.querySelectorAll('a[href*="space.bilibili.com"]')) {
                    const href = anchor.href || anchor.getAttribute('href') || '';
                    if (!/space\.bilibili\.com\/\d+/.test(href)) continue;
                    const context = anchor.closest(
                        '.user-list, .b-user-video-card, [class*="user-card"], [class*="search-user"], ' +
                        '[class*="upuser"], [class*="user-item"], [class*="bili-user"]'
                    ) || anchor.parentElement;
                    const chain = classChain(context || anchor);
                    const anchorClass = String(anchor.className || '').toLowerCase();
                    const contextText = clean((context && context.innerText) || '');
                    let score = 0;
                    if (userContextRe.test(chain)) score += 8;
                    if (nameOrAvatarRe.test(anchorClass)) score += 3;
                    if (profileHintRe.test(contextText)) score += 3;
                    if (videoContextRe.test(chain) && !userContextRe.test(chain)) score -= 8;
                    if (score >= 5) {
                        scored.push({
                            href,
                            name: clean(anchor.innerText || anchor.getAttribute('title') || anchor.getAttribute('aria-label') || ''),
                            score,
                        });
                    }
                }
                scored.sort((a, b) => b.score - a.score);
                return scored[0] || null;
            }"""
            )
        except (PlaywrightError, TypeError, ValueError):
            return "", ""
        if not isinstance(candidate, dict):
            return "", ""
        return self._space_video_url_from_href(str(candidate.get("href") or "")), str(candidate.get("name") or "").strip()

    def _scan_with_browser_queue(
        self,
        url,
        max_pages=1,
        is_search=False,
        is_space=False,
        enqueue: bool = True,
        exclude_bvids: set[str] | None = None,
    ):
        """扫描网页中的 BV 候选，并按需送入详情解析队列。"""
        excluded = {self._normalize_bvid(value) for value in (exclude_bvids or set()) if value}
        bv_set = set(excluded)
        def _scan_result() -> list[str]:
            return [bvid for bvid in bv_set if bvid not in excluded]
        if (is_search or "search.bilibili.com" in str(url or "").lower()) and self._should_use_static_search_shortcut(url):
            static_count = self._scan_static_bilibili_candidates(
                url,
                max_pages=max_pages,
                bv_set=bv_set,
                enqueue=enqueue,
                excluded_bvids=excluded,
            )
            if static_count > 0:
                self.log(f"   📄 静态搜索页: 发现 {static_count} 个")
                return _scan_result()
            self.log("⚠️ 静态搜索没有结果区视频，不再启动浏览器")
            return _scan_result()
        if self._crawl_stopped():
            return _scan_result()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    **self._playwright_launch_kwargs(
                        headless=self._browser_headless(),
                        proxy=(getattr(self, "config", {}) or {}).get("proxy"),
                    )
                )
                self._track_playwright_browser(browser)
                context = browser.new_context(
                    **self._playwright_context_kwargs(
                        user_agent=HEADERS.get("User-Agent", DEFAULT_USER_AGENT),
                        referer="https://www.bilibili.com/",
                        viewport={"width": 1280, "height": 800},
                    )
                )
                self._apply_stealth_to_context(context)
                page = context.new_page()
                self._restore_scan_cookies(page)
                current_url = url
                browser_timeout_ms = self._configured_timeout_ms(default=60)
                if not self.interruptible_playwright_goto(page, url, timeout=browser_timeout_ms):
                    return _scan_result()
                if not self.interruptible_wait_for_load_state(
                    page,
                    "domcontentloaded",
                    timeout=browser_timeout_ms,
                ):
                    return _scan_result()
                if self._is_bilibili_error_page(page):
                    self.log(f"⚠️ Bilibili page is unavailable, skip browser candidate scan: {url}")
                    return _scan_result()
                initial_state = self._read_bilibili_browser_page_state(page)
                if initial_state.terminal:
                    self._log_bilibili_terminal_page_state(initial_state, url=str(getattr(page, "url", url) or url))
                    return _scan_result()
                # 关键词搜索可能命中作者卡片；只在该模式下转到作者视频页继续扫描。
                if is_search and "search.bilibili.com" in url:
                    try:
                        target_video_url, up_name = self._extract_search_up_space_video_url(page)
                        if target_video_url:
                            name_suffix = f": {up_name}" if up_name else ""
                            self.log(f"✨ 检测到 UP 主{name_suffix}")
                            if not self.interruptible_playwright_goto(page, target_video_url, timeout=browser_timeout_ms):
                                return _scan_result()
                            if not self.interruptible_wait_for_load_state(
                                page,
                                "domcontentloaded",
                                timeout=browser_timeout_ms,
                            ):
                                return _scan_result()
                            current_url = page.url
                            is_search = False
                            is_space = True
                            redirected_state = self._read_bilibili_browser_page_state(page)
                            if redirected_state.terminal:
                                self._log_bilibili_terminal_page_state(
                                    redirected_state,
                                    url=str(getattr(page, "url", target_video_url) or target_video_url),
                                )
                                return _scan_result()
                    except (PlaywrightError, ValueError):
                        pass
                page_count = 0
                while self.is_running and page_count < max_pages:
                    page_count += 1
                    if page_count == 1:
                        page_state = self._wait_for_bilibili_candidates(
                            page,
                            timeout_ms=self._configured_timeout_ms(default=60),
                        )
                        if page_state.terminal:
                            self._log_bilibili_terminal_page_state(
                                page_state,
                                url=str(getattr(page, "url", current_url) or current_url),
                                page_count=page_count,
                            )
                            break
                    for _ in range(3):
                        page.evaluate("window.scrollBy(0, 1000)")
                        if not self.interruptible_sleep(0.3):
                            break
                    # 某些列表页会在滚动到底部后才补全卡片，因此空页时需要重新抓取一次。
                    new_count = self._scan_page_for_new_bvids(
                        page,
                        bv_set,
                        enqueue=enqueue,
                        excluded_bvids=excluded,
                        search_results_only=bool(is_search),
                    )
                    self.log(f"   📄 第 {page_count} 页: 发现 {new_count} 个")
                    if not self._has_bvid_capacity(bv_set, excluded):
                        self.log(f"   ✅ 已达到视频数上限 {self._max_items_limit()}，停止继续抓取")
                        break
                    if new_count == 0:
                        break
                    if page_count < max_pages:
                        if is_search:
                            previous_url = str(getattr(page, "url", "") or current_url)
                            previous_number = self._search_page_number(previous_url)
                            next_url = self._build_search_page_url(previous_url, previous_number + 1)
                            current_url = next_url
                            if not self.interruptible_playwright_goto(page, next_url, timeout=browser_timeout_ms):
                                break
                            if not self.interruptible_page_wait(page, 2000):
                                break
                            landed_url = str(getattr(page, "url", "") or "")
                            if self._search_page_number(landed_url) <= previous_number:
                                self.log("⚠️ 搜索页码没有前进，停止翻页")
                                break
                            landed_state = self._read_bilibili_browser_page_state(page)
                            if landed_state.kind == "empty":
                                self._log_bilibili_terminal_page_state(
                                    landed_state,
                                    url=landed_url or next_url,
                                    page_count=page_count + 1,
                                )
                                break
                            continue
                        elif is_space:
                            try:
                                next_btn = page.locator("button:has-text('下一页')").first
                                if next_btn.is_visible() and next_btn.is_enabled():
                                    next_btn.click()
                                    if not self.interruptible_page_wait(page, 2000):
                                        break
                                    current_url = page.url
                                    continue
                                else:
                                    break
                            except PlaywrightError:
                                break
                        else:
                            try:
                                next_btn = page.locator("button.next-page, li.next").first
                                if next_btn.is_visible():
                                    next_btn.click()
                                    if not self.interruptible_page_wait(page, 2000):
                                        break
                                    current_url = page.url
                                    continue
                                else:
                                    break
                            except PlaywrightError:
                                break
                self._close_tracked_playwright_browser(browser)
        except (PlaywrightError, ValueError, RuntimeError) as e:
            self.log(f"⚠️ 扫描异常: {e}")
        finally:
            browser = self._tracked_playwright_browser()
            if browser is not None:
                try:
                    self._close_tracked_playwright_browser(browser)
                except PlaywrightError:
                    pass
            self._clear_playwright_browser(browser)
        return _scan_result()

    def _scan_page_for_new_bvids(
        self,
        page,
        bv_set: set[str],
        *,
        enqueue: bool = True,
        excluded_bvids: set[str] | None = None,
        search_results_only: bool = False,
    ) -> int:
        """提取当前页新增 BV；若首轮为空则补滚动一次再重试。"""
        extract = self._extract_search_result_hrefs if search_results_only else self._extract_video_hrefs
        new_count = self._enqueue_new_bvids(
            extract(page),
            bv_set,
            enqueue=enqueue,
            excluded_bvids=excluded_bvids,
        )
        if new_count > 0:
            return new_count
        if not self._has_bvid_capacity(bv_set, excluded_bvids):
            return new_count
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        if not self.interruptible_sleep(1):
            return new_count
        return self._enqueue_new_bvids(
            extract(page),
            bv_set,
            enqueue=enqueue,
            excluded_bvids=excluded_bvids,
        )

    def _scan_static_bilibili_candidates(
        self,
        url: str,
        *,
        max_pages: int,
        bv_set: set[str],
        enqueue: bool = True,
        excluded_bvids: set[str] | None = None,
    ) -> int:
        """从 Bilibili 服务端渲染的搜索 HTML 中提取 BV 候选。

        合集类输入常指向不可用的代表 BV，但搜索 HTML 仍可能包含其他可播放 BV。
        只抽取结果卡片；静态结果为 0 时调用方不再启动浏览器。
        """
        total = 0
        try:
            page_count = max(1, int(max_pages or 1))
        except (TypeError, ValueError):
            page_count = 1
        request_timeout = self._configured_timeout_seconds(
            default=cfg.get("bilibili", "timeout", get_setting_default("bilibili", "timeout"))
        )
        proxy = self._effective_proxy_server((getattr(self, "config", {}) or {}).get("proxy"))
        proxies = requests_proxy_mapping(proxy)
        headers = {
            **HEADERS,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        for page_num in range(1, page_count + 1):
            if self._crawl_stopped() or not self._has_bvid_capacity(bv_set, excluded_bvids):
                break
            page_url = str(url or "")
            if page_num > 1:
                page_url = self._build_search_page_url(page_url, page_num)
            try:
                response = requests.get(
                    page_url,
                    headers=headers,
                    timeout=request_timeout,
                    allow_redirects=True,
                    proxies=proxies,
                )
                if response.status_code >= 400:
                    break
            except requests.RequestException as exc:
                self.log(f"⚠️ Bilibili 静态搜索页解析失败: {exc}")
                break
            response_url = getattr(response, "url", None)
            if not isinstance(response_url, str) or not response_url:
                response_url = page_url
            if page_num > 1 and self._search_page_number(response_url) < page_num:
                self.log("⚠️ 搜索页码没有前进，停止翻页")
                break
            page_html = str(getattr(response, "text", "") or "")
            if self._search_markup_is_empty(page_html):
                self.log("⚠️ Bilibili 搜索页为空，停止翻页")
                break
            result_hrefs = self._extract_search_result_hrefs_from_text(page_html)
            if not result_hrefs:
                break
            page_new_count = self._enqueue_new_bvids(
                result_hrefs,
                bv_set,
                enqueue=enqueue,
                excluded_bvids=excluded_bvids,
            )
            total += page_new_count
            if page_new_count == 0 or not self._has_bvid_capacity(bv_set, excluded_bvids):
                break
        return total

    _SEARCH_RESULT_CARD_RE = re.compile(
        r"""class=(?P<q>["'])(?P<class>[^"']*(?:bili-video-card|video-list-item|search-video-card)[^"']*)(?P=q)""",
        re.IGNORECASE,
    )
    _SEARCH_EXCLUDED_ATTR_RE = re.compile(
        r"""(?:class|id)=["'][^"']*(?:rank|recommend|trending|popular|hot-list|search-hot)[^"']*["']""",
        re.IGNORECASE,
    )
    _SEARCH_RESULT_CONTAINER_RE = re.compile(
        r"""(?:class|id)=["'][^"']*(?:video-list|search-content|search-page-video)[^"']*["']""",
        re.IGNORECASE,
    )
    _BV_TOKEN_RE = re.compile(r"BV[0-9A-Za-z]{10}", re.IGNORECASE)

    @staticmethod
    def _search_page_number(url: str) -> int:
        try:
            parsed = urllib.parse.urlparse(str(url or ""))
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            raw = (query.get("page") or ["1"])[0]
            return int(str(raw).strip() or "1")
        except (TypeError, ValueError):
            return 1

    @classmethod
    def _search_card_is_excluded(cls, html: str, start: int) -> bool:
        prefix = html[max(0, start - 2000):start]
        excluded = list(cls._SEARCH_EXCLUDED_ATTR_RE.finditer(prefix))
        if not excluded:
            return False
        containers = list(cls._SEARCH_RESULT_CONTAINER_RE.finditer(prefix))
        if not containers:
            return True
        return excluded[-1].start() > containers[-1].start()

    @classmethod
    def _html_element_end(cls, html: str, tag_start: int) -> int:
        """返回起始标签对应元素的结束位置，不把后续兄弟节点算进本卡片。"""
        if tag_start < 0 or tag_start >= len(html):
            return len(html)
        open_end = html.find(">", tag_start)
        if open_end < 0:
            return len(html)
        open_tag = html[tag_start:open_end + 1]
        if open_tag.rstrip().endswith("/>"):
            return open_end + 1
        name_match = re.match(r"<\s*([A-Za-z][\w:-]*)", open_tag)
        if name_match is None:
            return open_end + 1
        token = re.compile(
            rf"</?\s*{re.escape(name_match.group(1))}\b",
            re.IGNORECASE,
        )
        depth = 0
        for match in token.finditer(html, tag_start):
            if html.startswith("</", match.start()):
                depth -= 1
                if depth <= 0:
                    close = html.find(">", match.end())
                    return len(html) if close < 0 else close + 1
            else:
                depth += 1
        return len(html)

    @classmethod
    def _search_card_markup_end(cls, html: str, tag_start: int, class_end: int) -> int:
        """卡片范围停在本元素结束处，并且不越过下一张卡片的起始标签。"""
        window_end = cls._html_element_end(html, tag_start)
        next_card = cls._SEARCH_RESULT_CARD_RE.search(html, class_end)
        if next_card is None:
            return window_end
        next_tag = html.rfind("<", tag_start + 1, next_card.start())
        if next_tag < 0:
            next_tag = next_card.start()
        return min(window_end, next_tag)

    @classmethod
    def _extract_search_result_hrefs_from_text(cls, text: str) -> list[str]:
        """只从搜索结果卡片取 BV，忽略推荐和排行区域。"""
        html = str(text or "")
        values: list[str] = []
        seen: set[str] = set()
        for match in cls._SEARCH_RESULT_CARD_RE.finditer(html):
            if cls._search_card_is_excluded(html, match.start()):
                continue
            tag_start = html.rfind("<", max(0, match.start() - 400), match.start())
            if tag_start < 0:
                tag_start = match.start()
            window_end = cls._search_card_markup_end(html, tag_start, match.end())
            bvid_match = cls._BV_TOKEN_RE.search(html[tag_start:window_end])
            if bvid_match is None:
                continue
            bvid = cls._normalize_bvid(bvid_match.group(0))
            if bvid in seen:
                continue
            seen.add(bvid)
            values.append(f"https://www.bilibili.com/video/{bvid}")
        return values

    def _search_markup_is_empty(self, html: str) -> bool:
        text = str(html or "")
        if self._extract_search_result_hrefs_from_text(text):
            return False
        return any(marker in text for marker in self.BROWSER_EMPTY_TEXT_MARKERS)

    def _extract_search_result_hrefs(self, page) -> list[str]:
        """浏览器搜索页只读取结果卡片，不收推荐和排行链接。"""
        try:
            hrefs = page.evaluate(r'''() => {
            const excludedRe = /rank|recommend|trending|popular|hot-list|search-hot|排行|推荐/i;
            const cardSelector = '.bili-video-card, .video-list-item, .search-video-card';
            const isExcluded = (node) => {
                let current = node;
                for (let depth = 0; current && depth < 8; depth += 1, current = current.parentElement) {
                    const hint = `${current.id || ''} ${current.className || ''}`;
                    if (excludedRe.test(hint)) return true;
                }
                return false;
            };
            const values = [];
            const seen = new Set();
            const add = (value) => {
                const matched = String(value || '').match(/BV[0-9A-Za-z]{10}/i);
                if (!matched) return;
                const bvid = `BV${matched[0].slice(2)}`;
                if (seen.has(bvid)) return;
                seen.add(bvid);
                values.push(`https://www.bilibili.com/video/${bvid}`);
            };
            for (const card of document.querySelectorAll(cardSelector)) {
                if (isExcluded(card)) continue;
                add(card.getAttribute('data-bvid'));
                const link = card.querySelector('a[href*="/video/BV"]');
                if (link) add(link.href || link.getAttribute('href'));
            }
            return values;
        }''')
        except PlaywrightError:
            return []
        return hrefs or []

    @classmethod
    def _extract_video_hrefs_from_text(cls, text: str) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for match in re.finditer(r"(?i)BV[0-9A-Za-z]{10}", str(text or "")):
            bvid = cls._normalize_bvid(match.group(0))
            if bvid in seen:
                continue
            seen.add(bvid)
            values.append(f"https://www.bilibili.com/video/{bvid}")
        return values

    def _wait_for_bilibili_candidates(self, page, *, timeout_ms: int = 15000) -> BilibiliBrowserPageState:
        """短暂等待 Bilibili SPA 视频卡片或明确的终止页面状态。"""
        deadline = time.monotonic() + max(0, timeout_ms) / 1000
        last_state = BilibiliBrowserPageState("unknown", "尚未读取页面状态")
        while self.is_running and not self.interrupt_requested and time.monotonic() < deadline:
            last_state = self._read_bilibili_browser_page_state(page)
            if last_state.kind == "ready" or last_state.terminal:
                return last_state
            if not self.interruptible_page_wait(page, 500):
                return BilibiliBrowserPageState("stopped", "用户停止或页面等待被中断")
        return last_state

    def _extract_video_hrefs(self, page) -> list[str]:
        """从页面 DOM 与常见初始状态对象中提取视频链接。"""
        try:
            hrefs = page.evaluate(r'''() => {
            const initialState = window.__INITIAL_STATE__;
            if (initialState && initialState.error && (initialState.error.code || initialState.error.trueCode)) {
                return [];
            }
            const values = new Set();
            const addBvid = (value) => {
                if (!value) return;
                const text = String(value);
                const matches = text.match(/BV[0-9A-Za-z]{10}/g) || [];
                for (const bvid of matches) values.add(`https://www.bilibili.com/video/${bvid}`);
            };
            const addSemanticBvids = (value) => {
                if (!value) return;
                const text = String(value);
                const patterns = [
                    /(?:href|src)=["'][^"']*\/video\/(BV[0-9A-Za-z]{10})/gi,
                    /\/video\/(BV[0-9A-Za-z]{10})/gi,
                    /player\.html\?[^"'<>]*[?&]bvid=(BV[0-9A-Za-z]{10})/gi,
                    /(?:^|[?"'&])bvid(?:["'\s:=]|%3D)+["']?(BV[0-9A-Za-z]{10})/gi,
                    /(?:bvid|bv_id)["']?\s*[:=]\s*["'](BV[0-9A-Za-z]{10})/gi,
                ];
                for (const pattern of patterns) {
                    let match;
                    while ((match = pattern.exec(text)) !== null) addBvid(match[1]);
                }
            };
            const semanticKey = (key) => {
                const lowered = String(key || '').toLowerCase();
                return /(bvid|bv_id|aid|arcurl|jump_url|url|href|link|uri|redirect|share)/.test(lowered);
            };
            const addBvidFromSemanticString = (key, value) => {
                if (!value) return;
                const text = String(value);
                if (semanticKey(key)) {
                    addBvid(text);
                    return;
                }
                if (/\/video\/BV[0-9A-Za-z]{10}/i.test(text) || /player\.html\?[^"'<>]*[?&]bvid=/i.test(text)) {
                    addBvid(text);
                }
            };
            const walk = (value, depth = 0, keyHint = '') => {
                if (!value || depth > 8) return;
                if (typeof value === 'string') {
                    addBvidFromSemanticString(keyHint, value);
                    return;
                }
                if (Array.isArray(value)) {
                    for (const entry of value) walk(entry, depth + 1, keyHint);
                    return;
                }
                if (typeof value !== 'object') return;
                for (const [key, entry] of Object.entries(value)) {
                    if (semanticKey(key)) {
                        addBvid(entry);
                    }
                    if (entry && typeof entry === 'object') walk(entry, depth + 1, key);
                    else if (typeof entry === 'string') addBvidFromSemanticString(key, entry);
                }
            };
            for (const anchor of document.querySelectorAll('a[href*="/video/BV"]')) {
                values.add(anchor.href);
            }
            for (const node of document.querySelectorAll('[data-bvid]')) {
                addBvid(node.getAttribute('data-bvid'));
            }
            walk(window.__INITIAL_STATE__);
            walk(window.__playinfo__);
            walk(window.__NEXT_DATA__);
            walk(window.__RENDER_DATA__);
            addSemanticBvids(document.documentElement.innerHTML);
            return Array.from(values);
        }''')
        except PlaywrightError:
            return []
        return hrefs or []

    def _enqueue_new_bvids(
        self,
        hrefs: list[str],
        bv_set: set[str],
        *,
        enqueue: bool = True,
        excluded_bvids: set[str] | None = None,
    ) -> int:
        """去重并统计新 BV，按配置决定是否送入详情队列。"""
        new_count = 0
        for href in hrefs:
            if not self._has_bvid_capacity(bv_set, excluded_bvids):
                break
            bvid = self._bvid_from_url(href)
            if not bvid:
                continue
            if bvid in bv_set:
                continue
            bv_set.add(bvid)
            if enqueue:
                self.raw_bv_queue.put(bvid)
            new_count += 1
        return new_count

    def _clean_name(self, name):
        """沿用解析器规则清理文件名。"""
        return self.parser.clean_name(name)

    def _build_search_page_url(self, current_url: str, page_num: int) -> str:
        """构造指定页码的 Bilibili 搜索 URL。"""
        return input_router.build_search_page_url(current_url, page_num)

    def _perform_login_scan(self, save_path):
        """打开扫码登录页，并在可取消等待中持久化登录 Cookie。"""
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    **self._playwright_launch_kwargs(
                        headless=self._browser_headless(login_window=True),
                        proxy=(getattr(self, "config", {}) or {}).get("proxy"),
                    )
                )
                self._track_playwright_browser(browser)
                context = browser.new_context(
                    **self._playwright_context_kwargs(
                        user_agent=HEADERS.get("User-Agent", DEFAULT_USER_AGENT),
                        referer="https://www.bilibili.com/",
                        viewport={"width": 1280, "height": 800},
                    )
                )
                self._apply_stealth_to_context(context)
                page = context.new_page()
                if not self.interruptible_playwright_goto(
                    page,
                    "https://passport.bilibili.com/login",
                    timeout=self._configured_timeout_ms(default=60),
                ):
                    raise LoginCancelledError("用户在登录过程中终止任务")
                self.log("⏳ 请在弹出的窗口中扫码登录...")
                success = self.auth_service.wait_for_cookie_and_persist(
                    context=context,
                    cookie_name="SESSDATA",
                    save_path=save_path,
                    stop_check=lambda: not self.is_running,
                    max_attempts=60,
                )
                if success:
                    self.log("✅ 扫码成功，Cookie 已保存")
                    self._close_tracked_playwright_browser(browser)
                    self.api.load_cookies()
                    return
                if not self.is_running:
                    self._close_tracked_playwright_browser(browser)
                    raise LoginCancelledError("用户在登录过程中终止任务")
                self._close_tracked_playwright_browser(browser)
                raise LoginTimeoutError("等待 Bilibili 扫码登录超时")
        except (PlaywrightError, OSError) as exc:
            if not self.is_running or self.interrupt_requested:
                raise LoginCancelledError("用户在登录过程中终止任务") from exc
            raise
        finally:
            browser = self._tracked_playwright_browser()
            if browser is not None:
                try:
                    self._close_tracked_playwright_browser(browser)
                except PlaywrightError:
                    pass
            self._clear_playwright_browser(browser)
