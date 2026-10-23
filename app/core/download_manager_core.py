"""不依赖界面框架的下载分发核心。"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from app.config import cfg, normalize_download_concurrency
from app.debug_logger import debug_logger
from app.core.media_filter import is_image_like_resource, should_skip_for_video_only
from app.core.download_manager_startup import DownloadManagerStartupMaintenance
from app.core.download_path_policy import resolve_task_save_directory
from app.core.download_shutdown_guardian import stop_download_manager
from app.models import VideoItem
from app.services.download_recovery_store import DownloadRecoveryStore

class PendingDownloadQueue:
    """支持取消与一致性快照的线程安全先进先出队列。

    内部 deque 没有容量上限；管理器的槽位只限制任务执行并发，不限制排队任务在
    内存中的积压量。
    """

    def __init__(self) -> None:
        self._items: deque[tuple[VideoItem, str]] = deque()
        self._condition = threading.Condition()
        self._closed = False
        # 这里存的是“排队中的 id 计数”而不是 set，避免同一资源被重复加入时取消/快照漏算。
        self._queued_ids: dict[str, int] = {}

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def _track_enqueue(self, video_id: str) -> None:
        if video_id:
            self._queued_ids[video_id] = self._queued_ids.get(video_id, 0) + 1

    def _track_dequeue(self, video_id: str) -> None:
        if not video_id:
            return
        count = self._queued_ids.get(video_id, 0)
        if count <= 1:
            self._queued_ids.pop(video_id, None)
        else:
            self._queued_ids[video_id] = count - 1

    def put(self, item: tuple[VideoItem, str]) -> None:
        with self._condition:
            if self._closed:
                raise RuntimeError("download queue is closed")
            self._items.append(item)
            self._track_enqueue(getattr(item[0], "id", ""))
            self._condition.notify()

    def put_many(self, items: Iterable[tuple[VideoItem, str]]) -> int:
        count = 0
        with self._condition:
            if self._closed:
                raise RuntimeError("download queue is closed")
            for item in items:
                self._items.append(item)
                self._track_enqueue(getattr(item[0], "id", ""))
                count += 1
            if count:
                self._condition.notify_all()
        return count
    def get(self, timeout: float | None = None) -> tuple[VideoItem, str]:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while not self._items:
                if timeout is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic() if deadline is not None else 0.0
                if remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)
            item = self._items.popleft()
            self._track_dequeue(getattr(item[0], "id", ""))
            return item

    def get_nowait(self) -> tuple[VideoItem, str]:
        return self.get(timeout=0.0)

    def empty(self) -> bool:
        with self._condition:
            return not self._items

    def qsize(self) -> int:
        with self._condition:
            return len(self._items)

    def drain(self) -> list[tuple[VideoItem, str]]:
        with self._condition:
            # 排空即关闭。stop_all 之后调度线程不能再把已取出的任务塞回内存队列。
            self._closed = True
            items = list(self._items)
            self._items.clear()
            self._queued_ids.clear()
            self._condition.notify_all()
            return items

    def remove_video(self, video_id: str) -> VideoItem | None:
        removed = self.remove_videos({video_id})
        return removed[0] if removed else None

    def remove_video_instance(self, video: VideoItem) -> int:
        """只移除与捕获对象相同的排队项，保留复用同一 ID 的新任务。"""
        return len(self.remove_video_instances((video,)))

    def remove_video_instances(
        self,
        videos: Iterable[VideoItem],
    ) -> list[VideoItem]:
        """Remove captured queue objects in one scan without touching ID reuses."""

        target_identities = {id(video) for video in videos if video is not None}
        if not target_identities:
            return []
        with self._condition:
            retained: deque[tuple[VideoItem, str]] = deque()
            removed: list[VideoItem] = []
            while self._items:
                queued_video, save_dir = self._items.popleft()
                if id(queued_video) in target_identities:
                    removed.append(queued_video)
                    self._track_dequeue(queued_video.id)
                    continue
                retained.append((queued_video, save_dir))
            self._items = retained
            return removed

    def remove_videos(self, video_ids: Iterable[str]) -> list[VideoItem]:
        ids = {str(video_id) for video_id in video_ids if video_id}
        if not ids:
            return []
        with self._condition:
            retained: deque[tuple[VideoItem, str]] = deque()
            removed: list[VideoItem] = []
            while self._items:
                queued_video, save_dir = self._items.popleft()
                if queued_video.id in ids:
                    removed.append(queued_video)
                    self._track_dequeue(queued_video.id)
                    continue
                retained.append((queued_video, save_dir))
            self._items = retained
            return removed

    def snapshot_video_ids(self) -> set[str]:
        with self._condition:
            return set(self._queued_ids)


def _project_url_for_log(value: object) -> str:
    """Expose only a stable HTTP(S) origin and a fixed private-location marker."""

    try:
        parts = urlsplit(str(value or "").strip())
        scheme = parts.scheme.casefold()
        host = (parts.hostname or "").casefold()
        if scheme not in {"http", "https"} or not host:
            return "[redacted]"
        port = parts.port
    except (AttributeError, TypeError, UnicodeError, ValueError):
        return "[redacted]"

    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority = f"{authority}:{port}"
    has_private_location = (
        parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or bool(parts.query)
        or bool(parts.fragment)
    )
    path = "/[redacted]" if has_private_location else "/"
    return urlunsplit((scheme, authority, path, "", ""))

class DownloadManagerCore(DownloadManagerStartupMaintenance):
    """统一管理入队、并发槽位、取消操作及工作线程生命周期。"""

    WORKER_STOP_TIMEOUT_MS = 2000
    LIGHTWEIGHT_MIN_CONCURRENT = 10
    LIGHTWEIGHT_CONCURRENCY_CAP = 10
    _GUARD_INIT_LOCK = threading.RLock()
    _STARTUP_MAINTENANCE_LOCK = threading.RLock()
    _STARTUP_MAINTENANCE_ROOTS: set[str] = set()

    def __init__(self, max_concurrent: int | None = None):
        self.queue = PendingDownloadQueue()
        self.workers: list[Any] = []
        self._dispatching_tasks: list[tuple[VideoItem, str]] = []
        self.max_concurrent = normalize_download_concurrency(max_concurrent or cfg.get("download", "max_concurrent", 3))
        self.max_retries = cfg.get("download", "max_retries", 3)
        self.auto_retry = bool(cfg.get("download", "auto_retry", True))
        self.request_timeout = cfg.get("download", "request_timeout", 60)
        self.resume_enabled = cfg.get("download", "resume_enabled", True)
        self.speed_limit_kb = cfg.get("download", "speed_limit_kb", 0)
        self.video_only = cfg.get("download", "video_only", False)
        self.image_respects_concurrency = bool(cfg.get("download", "image_respects_concurrency", False))
        self.image_fast_lane_limit = self._normalize_image_fast_lane_limit(
            cfg.get("download", "image_fast_lane_limit", 10)
        )
        self._slot_semaphore_capacity = self._slot_capacity()
        self.slot_semaphore = threading.BoundedSemaphore(self._slot_semaphore_capacity)
        self._slot_semaphore_lock = threading.RLock()
        self._dispatch_slot_gate = threading.Event()
        self._dispatch_slot_gate.set()
        self._workers_lock = threading.RLock()
        self._start_stop_lock = threading.RLock()
        self.is_running = True
        self._download_recovery_store = DownloadRecoveryStore()
        self._startup_maintenance_done = threading.Event()
        self._startup_maintenance_thread = threading.Thread(
            target=self._run_startup_maintenance,
            daemon=True,
            name="download-startup-maintenance",
        )
        self._startup_maintenance_thread.start()
        self.dispatcher_thread = threading.Thread(
            target=self._dispatch_loop,
            daemon=True,
            name="download-dispatcher",
        )
        self.dispatcher_thread.start()

    def set_max_concurrent(self, value: int) -> int:
        try:
            new_value = normalize_download_concurrency(value)
        except (TypeError, ValueError):
            new_value = self.max_concurrent
        with self._start_stop_guard():
            old_value = int(getattr(self, "max_concurrent", 1) or 1)
            old_slot_capacity = int(getattr(self, "_slot_semaphore_capacity", old_value) or old_value)
            self.max_concurrent = new_value
            new_slot_capacity = self._slot_capacity_for(new_value)
            if new_slot_capacity > old_slot_capacity:
                self._rebuild_slot_semaphore(old_value=old_slot_capacity, new_value=new_slot_capacity)
                self._slot_semaphore_capacity = new_slot_capacity
                self._get_dispatch_slot_gate().set()
            elif new_value < old_value:
                # 并发下调不抢占已经开始的 worker，只阻止后续调度继续扩张。
                self._get_dispatch_slot_gate().set()
                debug_logger.log(
                    component="DownloadManager",
                    action="set_max_concurrent",
                    level="INFO",
                    message="Reduced concurrency: existing download workers will wind down as they complete.",
                    status_code="DL_CONCURRENCY_DECREASE_DEFERRED",
                    details={"old_value": old_value, "new_value": new_value},
                )
        return self.max_concurrent

    def set_image_respects_concurrency(self, value: Any) -> bool:
        enabled = bool(value)
        with self._start_stop_guard():
            old_enabled = bool(getattr(self, "image_respects_concurrency", False))
            old_slot_capacity = int(
                getattr(self, "_slot_semaphore_capacity", self._slot_capacity()) or self._slot_capacity()
            )
            self.image_respects_concurrency = enabled
            new_slot_capacity = self._slot_capacity()
            if new_slot_capacity > old_slot_capacity:
                self._rebuild_slot_semaphore(old_value=old_slot_capacity, new_value=new_slot_capacity)
                self._slot_semaphore_capacity = new_slot_capacity
            self._get_dispatch_slot_gate().set()
            if enabled and not old_enabled and new_slot_capacity < old_slot_capacity:
                debug_logger.log(
                    component="DownloadManager",
                    action="set_image_respects_concurrency",
                    level="INFO",
                    message="Image fast lane disabled: existing lightweight workers will wind down as they complete.",
                    status_code="DL_IMAGE_CONCURRENCY_LIMIT_ENABLED",
                    details={"old_slot_capacity": old_slot_capacity, "new_slot_capacity": new_slot_capacity},
                )
        return self.image_respects_concurrency

    def set_image_fast_lane_limit(self, value: Any) -> int:
        new_value = self._normalize_image_fast_lane_limit(value)
        with self._start_stop_guard():
            old_slot_capacity = int(
                getattr(self, "_slot_semaphore_capacity", self._slot_capacity()) or self._slot_capacity()
            )
            self.image_fast_lane_limit = new_value
            new_slot_capacity = self._slot_capacity()
            if new_slot_capacity > old_slot_capacity:
                self._rebuild_slot_semaphore(old_value=old_slot_capacity, new_value=new_slot_capacity)
                self._slot_semaphore_capacity = new_slot_capacity
            self._get_dispatch_slot_gate().set()
        return self.image_fast_lane_limit

    def _rebuild_slot_semaphore(self, *, old_value: int, new_value: int) -> None:
        """在运行时扩容调度信号量，同时保留已经被 worker 占用的 token 数。"""
        with self._slot_semaphore_guard():
            available_tokens = 0
            for _ in range(max(0, old_value)):
                if not self.slot_semaphore.acquire(blocking=False):
                    break
                available_tokens += 1
            in_use_tokens = max(0, old_value - available_tokens)
            next_semaphore = threading.BoundedSemaphore(new_value)
            for _ in range(min(in_use_tokens, new_value)):
                next_semaphore.acquire(blocking=False)
            self.slot_semaphore = next_semaphore
        debug_logger.log(
            component="DownloadManager",
            action="set_max_concurrent",
            message="Increased concurrency by rebuilding dispatch semaphore capacity.",
            status_code="DL_CONCURRENCY_INCREASE_REBUILT",
            details={"old_value": old_value, "new_value": new_value, "in_use_tokens": in_use_tokens},
        )

    def set_runtime_options(self, **options: Any) -> dict[str, Any]:
        """应用运行时下载设置；已启动的 worker 保持原参数，新任务读取最新值。"""
        applied: dict[str, Any] = {}
        if "max_concurrent" in options:
            applied["max_concurrent"] = self.set_max_concurrent(options["max_concurrent"])
        if "max_retries" in options:
            try:
                self.max_retries = max(0, min(int(options["max_retries"]), 10))
            except (TypeError, ValueError):
                pass
            applied["max_retries"] = self.max_retries
        if "auto_retry" in options:
            value = options["auto_retry"]
            if isinstance(value, str):
                self.auto_retry = value.strip().lower() not in {"0", "false", "no", "off"}
            else:
                self.auto_retry = True if value is None else bool(value)
            applied["auto_retry"] = self.auto_retry
        if "request_timeout" in options:
            try:
                self.request_timeout = max(10, min(int(options["request_timeout"]), 300))
            except (TypeError, ValueError):
                pass
            applied["request_timeout"] = self.request_timeout
        if "resume_enabled" in options:
            self.resume_enabled = bool(options["resume_enabled"])
            applied["resume_enabled"] = self.resume_enabled
        if "speed_limit_kb" in options:
            try:
                self.speed_limit_kb = max(0, min(int(options["speed_limit_kb"]), 999999))
            except (TypeError, ValueError):
                pass
            applied["speed_limit_kb"] = self.speed_limit_kb
        if "video_only" in options:
            self.video_only = bool(options["video_only"])
            applied["video_only"] = self.video_only
        if "image_respects_concurrency" in options:
            applied["image_respects_concurrency"] = self.set_image_respects_concurrency(
                options["image_respects_concurrency"]
            )
        if "image_fast_lane_limit" in options:
            applied["image_fast_lane_limit"] = self.set_image_fast_lane_limit(options["image_fast_lane_limit"])
        return applied

    def _get_dispatch_slot_gate(self) -> threading.Event:
        gate = getattr(self, "_dispatch_slot_gate", None)
        if gate is None:
            with self._GUARD_INIT_LOCK:
                gate = getattr(self, "_dispatch_slot_gate", None)
                if gate is None:
                    gate = threading.Event()
                    gate.set()
                    self._dispatch_slot_gate = gate
        return gate

    def _has_dispatch_capacity(self) -> bool:
        self.prune_finished_workers()
        with self._workers_lock:
            return len(self.workers) < int(getattr(self, "max_concurrent", 1) or 1)

    def _slot_capacity_for(self, max_concurrent: int) -> int:
        """计算调度槽总量：视频按用户并发限制，图片可走受控的快速通道（fast lane）。"""
        configured = normalize_download_concurrency(max_concurrent)
        if bool(getattr(self, "image_respects_concurrency", False)):
            return configured
        lightweight_floor = max(1, int(getattr(self, "LIGHTWEIGHT_MIN_CONCURRENT", 10)))
        configured_limit = self._normalize_image_fast_lane_limit(
            getattr(self, "image_fast_lane_limit", cfg.get("download", "image_fast_lane_limit", 10))
        )
        lightweight_cap = max(1, min(int(getattr(self, "LIGHTWEIGHT_CONCURRENCY_CAP", 10)), configured_limit))
        return min(max(configured, lightweight_floor), lightweight_cap)

    def _slot_capacity(self) -> int:
        return self._slot_capacity_for(int(getattr(self, "max_concurrent", 1) or 1))

    @staticmethod
    def _normalize_image_fast_lane_limit(value: Any) -> int:
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            numeric = 10
        return max(1, min(numeric, 10))

    def _has_any_dispatch_capacity(self) -> bool:
        self.prune_finished_workers()
        with self._workers_lock:
            return len(self.workers) < self._slot_capacity()

    def _is_lightweight_download(self, video: VideoItem | None) -> bool:
        return is_image_like_resource(video)

    def _should_skip_for_video_only(self, video: VideoItem | None) -> bool:
        return bool(getattr(self, "video_only", False)) and should_skip_for_video_only(video)

    @staticmethod
    def _mark_video_only_skip(video: VideoItem) -> None:
        if not isinstance(getattr(video, "meta", None), dict):
            video.meta = {}
        video.status = "已跳过"
        # 跳过不是下载完成，不能用 100% 污染完成度、遥测或后续状态推断。
        video.progress = 0
        video.meta["skipped_by_video_only"] = True

    def _active_heavy_worker_count(self) -> int:
        self.prune_finished_workers()
        with self._workers_lock:
            return self._active_heavy_worker_count_unlocked()

    def _active_heavy_worker_count_unlocked(self) -> int:
        count = 0
        for worker in list(getattr(self, "workers", [])):
            if not self._is_lightweight_download(getattr(worker, "video", None)):
                count += 1
        return count

    def _has_capacity_for(self, video: VideoItem) -> bool:
        self.prune_finished_workers()
        with self._workers_lock:
            if len(self.workers) >= self._slot_capacity():
                return False
            if self._is_lightweight_download(video):
                return True
            # 重资源仍受 max_concurrent 约束；图片快速通道（fast lane）不能挤占视频下载槽。
            return self._active_heavy_worker_count_unlocked() < int(getattr(self, "max_concurrent", 1) or 1)

    def add_task(self, video: VideoItem, save_dir: str):
        """把单个媒体项目加入下载队列。"""
        return self.add_tasks([video], save_dir) > 0

    def add_tasks(self, videos: Iterable[VideoItem], save_dir: str) -> int:
        """批量入队后只唤醒一次调度器，降低大批任务提交时的锁竞争。"""
        queued: list[tuple[VideoItem, str]] = []
        for video in videos:
            if self._log_and_skip_video_only(video):
                continue
            if not isinstance(getattr(video, "meta", None), dict):
                video.meta = {}
            video.meta["save_directory"] = str(save_dir)
            self._log_queue_task(video, save_dir)
            queued.append((video, save_dir))

        if not queued:
            return 0

        with self._start_stop_guard():
            if not self.is_running:
                raise RuntimeError("\u5df2\u505c\u6b62: DownloadManager cannot add more tasks")
            put_many = getattr(self.queue, "put_many", None)
            if callable(put_many):
                count = int(put_many(queued))
            else:
                for item in queued:
                    self.queue.put(item)
                count = len(queued)
            self._get_dispatch_slot_gate().set()
        return count

    def _log_and_skip_video_only(self, video: VideoItem) -> bool:
        if self._should_skip_for_video_only(video):
            self._mark_video_only_skip(video)
            debug_logger.log(
                component="DownloadManager",
                action="skip_non_video_resource",
                message="Video-only mode skipped a non-video resource",
                status_code="DL_SKIP_VIDEO_ONLY",
                context=debug_logger.pick_used(
                    {"trace_id": video.meta.get("trace_id"), "video_id": video.id, "source": video.source},
                    "trace_id",
                    "video_id",
                    "source",
                ),
                details=debug_logger.pick_used(
                    {
                        "title": video.title,
                        "url": _project_url_for_log(video.url),
                        "content_type": video.meta.get("content_type"),
                    },
                    "title",
                    "url",
                    "content_type",
                ),
                trace_id=video.meta.get("trace_id"),
            )
            return True
        return False

    @staticmethod
    def _log_queue_task(video: VideoItem, save_dir: str) -> None:
        trace_id = video.meta.get("trace_id")
        debug_logger.log(
            component="DownloadManager",
            action="queue_task",
            message="Download task has been queued",
            status_code="DL_QUEUE",
            context=debug_logger.pick_used(
                {"trace_id": trace_id, "video_id": video.id, "source": video.source},
                "trace_id", "video_id", "source",
            ),
            details=debug_logger.pick_used(
                {
                    "title": video.title,
                    "save_dir": save_dir,
                    "url": _project_url_for_log(video.url),
                    "content_type": video.meta.get("content_type"),
                },
                "title", "save_dir", "url", "content_type",
            ),
            trace_id=trace_id,
        )

    def cancel_task(self, video_id: str) -> str | None:
        """取消一个排队中或运行中的任务。"""
        return self.cancel_tasks({video_id}).get(video_id)

    def cancel_task_and_wait(self, video_id: str, timeout_ms: int | None = None) -> str | None:
        """取消指定任务，并等待所有关联工作线程停止。"""
        results, running_workers = self._cancel_tasks_with_workers({video_id})
        result = results.get(video_id)
        if result not in {"dispatching", "running"}:
            return result

        if timeout_ms is None:
            timeout_ms = self.WORKER_STOP_TIMEOUT_MS
        timeout_ms = max(0, int(timeout_ms))
        deadline = time.monotonic() + (timeout_ms / 1000)
        tracked_workers = {id(worker): worker for worker in running_workers}
        stopped_workers: set[int] = set()

        while True:
            newly_running = []
            with self._workers_lock:
                dispatching = any(
                    dispatching_video.id == video_id
                    for dispatching_video, _save_dir in list(getattr(self, "_dispatching_tasks", []))
                )
                for worker in list(self.workers):
                    worker_id = getattr(getattr(worker, "video", None), "id", None)
                    identity = id(worker)
                    if worker_id == video_id and identity not in tracked_workers:
                        tracked_workers[identity] = worker
                        newly_running.append(worker)

            for worker in newly_running:
                worker.video.meta["frontend_status"] = '\u5f85\u4e0b\u8f7d'
                worker.video.meta["user_cancel_requested"] = True
                worker.stop()

            for identity, worker in list(tracked_workers.items()):
                if identity in stopped_workers:
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._cancel_wait_timeout(video_id, timeout_ms)
                wait_timeout_ms = max(1, int(remaining * 1000 + 0.999))
                try:
                    wait_ok = bool(worker.wait(wait_timeout_ms))
                except RuntimeError:
                    wait_ok = False
                if wait_ok:
                    stopped_workers.add(identity)

            if not dispatching and len(stopped_workers) == len(tracked_workers):
                return result

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._cancel_wait_timeout(video_id, timeout_ms)
            threading.Event().wait(min(0.01, remaining))

    def cancel_video_and_wait(self, video: VideoItem, timeout_ms: int | None = None) -> str | None:
        """按 ``VideoItem`` 对象身份取消，避免误伤复用同一 ID 的新任务。"""
        if video is None:
            return None
        video.meta["frontend_status"] = '待下载'
        video.meta["user_cancel_requested"] = True

        remove_instance = getattr(self.queue, "remove_video_instance", None)
        result: str | None = "queued" if callable(remove_instance) and remove_instance(video) else None
        running_workers: list[Any] = []
        with self._workers_lock:
            dispatching = any(
                dispatching_video is video
                for dispatching_video, _save_dir in list(getattr(self, "_dispatching_tasks", []))
            )
            if dispatching:
                result = "dispatching"
            for worker in list(self.workers):
                if getattr(worker, "video", None) is video:
                    running_workers.append(worker)

        for worker in running_workers:
            worker.stop()
        if running_workers:
            result = "running"
        if not dispatching and not running_workers:
            return result

        if timeout_ms is None:
            timeout_ms = self.WORKER_STOP_TIMEOUT_MS
        timeout_ms = max(0, int(timeout_ms))
        deadline = time.monotonic() + (timeout_ms / 1000)
        tracked_workers = {id(worker): worker for worker in running_workers}
        stopped_workers: set[int] = set()

        while True:
            newly_running = []
            with self._workers_lock:
                dispatching = any(
                    dispatching_video is video
                    for dispatching_video, _save_dir in list(getattr(self, "_dispatching_tasks", []))
                )
                for worker in list(self.workers):
                    identity = id(worker)
                    if getattr(worker, "video", None) is video and identity not in tracked_workers:
                        tracked_workers[identity] = worker
                        newly_running.append(worker)

            for worker in newly_running:
                worker.stop()

            for identity, worker in list(tracked_workers.items()):
                if identity in stopped_workers:
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._cancel_wait_timeout(video.id, timeout_ms)
                wait_timeout_ms = max(1, int(remaining * 1000 + 0.999))
                try:
                    wait_ok = bool(worker.wait(wait_timeout_ms))
                except RuntimeError:
                    wait_ok = False
                if wait_ok:
                    stopped_workers.add(identity)

            if not dispatching and len(stopped_workers) == len(tracked_workers):
                return result

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._cancel_wait_timeout(video.id, timeout_ms)
            threading.Event().wait(min(0.01, remaining))

    def cancel_videos_and_wait(
        self,
        videos: Iterable[VideoItem],
        timeout_ms: int | None = None,
    ) -> dict[str, str]:
        """Cancel an exact object batch and prove no captured item can keep running."""

        targets: dict[int, VideoItem] = {}
        for video in videos:
            if video is not None:
                targets.setdefault(id(video), video)
        if not targets:
            return {}

        for video in targets.values():
            video.meta["frontend_status"] = "待下载"
            video.meta["user_cancel_requested"] = True

        remove_instances = getattr(self.queue, "remove_video_instances", None)
        if callable(remove_instances):
            remove_instances(tuple(targets.values()))
        else:
            for video in targets.values():
                self.queue.remove_video_instance(video)

        if timeout_ms is None:
            timeout_ms = self.WORKER_STOP_TIMEOUT_MS
        timeout_ms = max(0, int(timeout_ms))
        deadline = time.monotonic() + (timeout_ms / 1000)
        tracked_workers: dict[int, Any] = {}
        stop_requested: set[int] = set()
        stopped_workers: set[int] = set()
        dispatching_identities: set[int] = set()

        while True:
            with self._workers_lock:
                dispatching_identities = {
                    id(dispatching_video)
                    for dispatching_video, _save_dir in list(
                        getattr(self, "_dispatching_tasks", [])
                    )
                    if id(dispatching_video) in targets
                }
                for worker in list(self.workers):
                    worker_video = getattr(worker, "video", None)
                    if id(worker_video) in targets:
                        tracked_workers.setdefault(id(worker), worker)

            for identity, worker in tuple(tracked_workers.items()):
                if identity in stop_requested:
                    continue
                stop_requested.add(identity)
                try:
                    worker.stop()
                except (AttributeError, RuntimeError):
                    pass

            for identity, worker in tuple(tracked_workers.items()):
                if identity in stopped_workers:
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                wait_timeout_ms = max(1, int(remaining * 1000 + 0.999))
                try:
                    wait_ok = bool(worker.wait(wait_timeout_ms))
                except RuntimeError:
                    wait_ok = False
                if wait_ok:
                    stopped_workers.add(identity)

            if (
                not dispatching_identities
                and len(stopped_workers) == len(tracked_workers)
            ):
                return {video.id: "cancelled" for video in targets.values()}

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                unresolved_identities = set(dispatching_identities)
                unresolved_identities.update(
                    id(getattr(worker, "video", None))
                    for identity, worker in tracked_workers.items()
                    if identity not in stopped_workers
                )
                unresolved_video_ids = [
                    video.id
                    for identity, video in targets.items()
                    if identity in unresolved_identities
                ]
                debug_logger.log(
                    component="DownloadManager",
                    action="cancel_video_batch_wait_timeout",
                    level="WARN",
                    message="Download batch did not stop before rollback timeout",
                    status_code="DL_CANCEL_BATCH_WAIT_TIMEOUT",
                    details={
                        "video_ids": unresolved_video_ids,
                        "timeout_ms": timeout_ms,
                    },
                )
                return {
                    video.id: (
                        "timeout" if identity in unresolved_identities else "cancelled"
                    )
                    for identity, video in targets.items()
                }
            threading.Event().wait(min(0.01, remaining))

    @staticmethod
    def _cancel_wait_timeout(video_id: str, timeout_ms: int) -> str:
        debug_logger.log(
            component="DownloadManager",
            action="cancel_task_wait_timeout",
            level="WARN",
            message="Download worker did not stop before file deletion timeout",
            status_code="DL_CANCEL_WAIT_TIMEOUT",
            details={"video_id": video_id, "timeout_ms": timeout_ms},
        )
        return "timeout"

    def cancel_tasks(self, video_ids: Iterable[str]) -> dict[str, str | None]:
        """通过一次队列扫描批量取消排队中或运行中的任务。"""
        results, _running_workers = self._cancel_tasks_with_workers(video_ids)
        return results

    def _cancel_tasks_with_workers(
        self,
        video_ids: Iterable[str],
    ) -> tuple[dict[str, str | None], list[Any]]:
        ids = {str(video_id) for video_id in video_ids if video_id}
        results: dict[str, str | None] = {video_id: None for video_id in ids}
        if not ids:
            return results, []

        queued_videos = self._remove_queued_tasks(ids)
        for video in queued_videos:
            # 队列中任务还没创建 worker，只需要回退前端状态并标记用户取消。
            video.meta["frontend_status"] = '\u5f85\u4e0b\u8f7d'
            video.meta["user_cancel_requested"] = True
            results[video.id] = "queued"

        running_workers = []
        with self._workers_lock:
            for dispatching_video, _save_dir in list(getattr(self, "_dispatching_tasks", [])):
                if dispatching_video.id in ids:
                    dispatching_video.meta["frontend_status"] = '\u5f85\u4e0b\u8f7d'
                    dispatching_video.meta["user_cancel_requested"] = True
                    if results[dispatching_video.id] is None:
                        results[dispatching_video.id] = "dispatching"
            for worker in list(self.workers):
                worker_id = getattr(getattr(worker, "video", None), "id", None)
                if worker_id in ids:
                    running_workers.append(worker)

        for worker in running_workers:
            video = worker.video
            # 运行中任务通过 worker.stop() 让下载器在下一个停止检查点退出。
            video.meta["frontend_status"] = '\u5f85\u4e0b\u8f7d'
            video.meta["user_cancel_requested"] = True
            worker.stop()
            results[video.id] = "running"

        return results, running_workers

    def pending_work_counts(self) -> tuple[int, int]:
        """返回活动加分发中任务数与排队数的近似快照。

        前者在工作线程锁下读取，后者随后在队列锁下另行读取，因此两项可能来自
        不同时刻，不提供同一锁域的一致性保证。
        """
        with self._workers_lock:
            active = len(self.workers) + len(getattr(self, "_dispatching_tasks", []))
        return active, self.queue.qsize()

    def _mark_dispatching(self, video: VideoItem, save_dir: str) -> None:
        with self._workers_lock:
            tasks = getattr(self, "_dispatching_tasks", None)
            if tasks is None:
                tasks = []
                self._dispatching_tasks = tasks
            tasks.append((video, save_dir))

    def _clear_dispatching(self, video: VideoItem | None) -> None:
        if video is None:
            return
        with self._workers_lock:
            tasks = getattr(self, "_dispatching_tasks", [])
            self._dispatching_tasks = [item for item in tasks if item[0] is not video]

    def _dispatch_loop(self):
        """调度线程：先占槽，再取队列任务，避免并发缩放时短暂超发 worker。"""
        startup_maintenance_done = getattr(self, "_startup_maintenance_done", None)
        while self.is_running and startup_maintenance_done is not None:
            if startup_maintenance_done.wait(timeout=0.1):
                break
        if not self.is_running:
            return
        while self.is_running:
            slot_acquired = False
            recovery_registered = False
            worker = None
            video = None
            save_dir = None
            try:
                if not self._wait_for_dispatch_slot():
                    break
                slot_acquired = True
                try:
                    video, save_dir = self.queue.get(timeout=0.2)
                except queue.Empty:
                    self._release_dispatch_slot("dispatch_slot_queue_empty")
                    slot_acquired = False
                    continue
                self._mark_dispatching(video, save_dir)

                with self._start_stop_guard():
                    if not self.is_running:
                        # 已取出但未启动。不要塞回内存 deque，停止排空后那条记录会丢。
                        self._discard_unstarted_task(
                            video,
                            save_dir,
                            "dispatch_slot_manager_stopped",
                        )
                        self._clear_dispatching(video)
                        self._release_dispatch_slot("dispatch_slot_manager_stopped")
                        slot_acquired = False
                        break

                if not self._has_capacity_for(video):
                    self._requeue_unstarted_task(
                        video,
                        save_dir,
                        "dispatch_slot_task_capacity_full",
                    )
                    self._clear_dispatching(video)
                    self._release_dispatch_slot("dispatch_slot_task_capacity_full")
                    slot_acquired = False
                    time.sleep(0.02)
                    continue

                if video.meta.get("user_cancel_requested"):
                    self._clear_dispatching(video)
                    self._release_dispatch_slot("dispatch_slot_task_cancelled")
                    slot_acquired = False
                    continue

                self._register_download_directory(video, save_dir)
                recovery_registered = True
                worker = self._create_worker(video, save_dir)
                if video.meta.get("user_cancel_requested"):
                    stop_worker = getattr(worker, "stop", None)
                    if callable(stop_worker):
                        stop_worker()
                    self._mark_download_recovery_state(
                        video.id,
                        "cancelled",
                        self._task_recovery_generation(video),
                    )
                    self._clear_dispatching(video)
                    self._release_dispatch_slot("dispatch_slot_task_cancelled_after_create")
                    slot_acquired = False
                    continue
                debug_logger.log(
                    component="DownloadManager",
                    action="dispatch_task",
                    message="Dispatched queued task to a download worker",
                    status_code="DL_DISPATCH",
                    context=debug_logger.pick_used(
                        {"trace_id": video.meta.get("trace_id"), "video_id": video.id, "source": video.source},
                        "trace_id", "video_id", "source",
                    ),
                    details=debug_logger.pick_used(
                        {
                            "title": video.title,
                            "max_concurrent": self.max_concurrent,
                            "slot_capacity": self._slot_capacity(),
                            "lightweight_task": self._is_lightweight_download(video),
                        },
                        "title", "max_concurrent", "slot_capacity", "lightweight_task",
                    ),
                    trace_id=video.meta.get("trace_id"),
                )
                self._connect_worker_callbacks(worker)
                worker._slot_released = False
                worker._completion_callback = self._handle_worker_completion
                with self._start_stop_guard():
                    if not self.is_running:
                        self._mark_download_recovery_state(
                            video.id,
                            "cancelled",
                            self._task_recovery_generation(video),
                        )
                        if slot_acquired:
                            self._release_dispatch_slot("dispatch_slot_manager_stopped_before_worker_append")
                            slot_acquired = False
                        break
                    with self._workers_lock:
                        self.workers.append(worker)
                    self._clear_dispatching(video)
                    worker.start()
                slot_acquired = False
            except queue.Empty:
                continue
            except Exception as e:
                failed_video = locals().get("video")
                if worker is not None:
                    with self._workers_lock:
                        if worker in self.workers:
                            self.workers.remove(worker)
                if slot_acquired:
                    self._release_dispatch_slot("dispatch_slot_worker_create_failed")
                    slot_acquired = False
                with self._workers_lock:
                    active_workers = len(self.workers)
                debug_logger.log_exception(
                    "DownloadManager",
                    "dispatch_loop",
                    e,
                    details={"queued_tasks": self.queue.qsize(), "active_workers": active_workers},
                )
                if failed_video is not None:
                    if recovery_registered:
                        self._mark_download_recovery_state(
                            failed_video.id,
                            "failed",
                            self._task_recovery_generation(failed_video),
                        )
                    self._emit_task_error(failed_video.id, f"\u8c03\u5ea6\u5931\u8d25: {e}")
            finally:
                self._clear_dispatching(video)
                if slot_acquired:
                    self._release_dispatch_slot("dispatch_slot_finally")

    def _register_download_directory(self, video: VideoItem, save_dir: str) -> None:
        recovery_store = getattr(self, "_download_recovery_store", None)
        if recovery_store is None:
            return
        resolved_save_dir = resolve_task_save_directory(video, save_dir)
        video.meta["save_directory"] = resolved_save_dir
        try:
            meta = video.meta if isinstance(getattr(video, "meta", None), dict) else {}
            generation = recovery_store.register_task(
                video_id=video.id,
                save_directory=resolved_save_dir,
                source_url=video.url,
                trace_id=str(meta.get("trace_id") or ""),
                platform=video.source,
            )
            # 提交发生在 worker.start() 之前；generation 固定在这次任务上。
            video.meta["recovery_generation"] = generation
        except Exception as exc:
            debug_logger.log_exception(
                "DownloadManager",
                "download_recovery_store_write_error",
                exc,
                details={"save_dir": str(resolved_save_dir), "video_id": video.id},
            )
            raise

    _RECOVERY_SETTLE_ATTEMPTS = 3

    @staticmethod
    def _explicit_worker_generation(worker: Any) -> Any:
        """只读取已经写入的 generation，避免 Mock 自动造出一个新属性。"""
        worker_dict = getattr(worker, "__dict__", None)
        if not isinstance(worker_dict, dict):
            return None
        generation = worker_dict.get("_recovery_generation")
        if generation is None or (isinstance(generation, str) and not generation.strip()):
            return None
        return generation

    @staticmethod
    def _task_recovery_generation(video: Any) -> Any:
        meta = getattr(video, "meta", None)
        if not isinstance(meta, dict):
            return None
        generation = meta.get("recovery_generation")
        if generation is None:
            return None
        if isinstance(generation, str) and not generation.strip():
            return None
        return generation

    def _mark_download_recovery_state(self, video_id: str, state: str, generation: Any = None) -> None:
        """按本次 generation 销账。删账失败会重试，仍失败则把记录留待启动确认。"""
        recovery_store = getattr(self, "_download_recovery_store", None)
        if recovery_store is None:
            return
        normalized_video_id = str(video_id or "").strip()
        if not normalized_video_id:
            return
        if generation is None or (isinstance(generation, str) and not str(generation).strip()):
            debug_logger.log(
                component="DownloadManager",
                action="download_recovery_settle_deferred",
                level="WARN",
                message="Skipped ledger settlement because this task generation is unknown",
                status_code="DL_RECOVERY_SETTLE_DEFERRED",
                details={"video_id": normalized_video_id, "state": str(state)},
            )
            return
        normalized_state = str(state or "").strip().lower()
        last_error: Exception | None = None
        attempts = max(1, int(getattr(self, "_RECOVERY_SETTLE_ATTEMPTS", 3) or 3))
        for attempt in range(attempts):
            try:
                if normalized_state == "failed":
                    recovery_store.handoff_failed_task(normalized_video_id, generation)
                else:
                    # 完成和取消都只删这一次任务，不把保存目录放进 pending_cleanup。
                    recovery_store.delete_task(normalized_video_id, generation)
                return
            except Exception as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                time.sleep(0.01 * (attempt + 1))
        if last_error is not None:
            debug_logger.log_exception(
                "DownloadManager",
                "download_recovery_store_state_error",
                last_error,
                details={
                    "video_id": normalized_video_id,
                    "state": str(state),
                    "leave_for_confirmation": True,
                },
            )

    def _queue_rejects_new_items(self) -> bool:
        if getattr(self, "_downloads_queue_closed", False):
            return True
        return bool(getattr(self.queue, "closed", False))

    def _requeue_unstarted_task(self, video: VideoItem, save_dir: str, reason: str) -> bool:
        """容量不足时回队前再看一次运行状态；已停止或队列已关闭则丢弃并记日志。"""
        with self._start_stop_guard():
            if not self.is_running or self._queue_rejects_new_items():
                self._discard_unstarted_task(video, save_dir, reason)
                return False
        try:
            self.queue.put((video, save_dir))
        except Exception as exc:
            self._discard_unstarted_task(video, save_dir, reason, error=exc)
            return False
        return True

    def _discard_unstarted_task(
        self,
        video: VideoItem,
        save_dir: str,
        reason: str,
        error: BaseException | None = None,
    ) -> None:
        meta = getattr(video, "meta", None)
        trace_id = meta.get("trace_id") if isinstance(meta, dict) else None
        debug_logger.log(
            component="DownloadManager",
            action="discard_unstarted_task",
            level="WARN",
            message="Discarded a dequeued download that was not started",
            status_code="DL_DISCARD_UNSTARTED",
            context=debug_logger.pick_used(
                {"trace_id": trace_id, "video_id": getattr(video, "id", "")},
                "trace_id",
                "video_id",
            ),
            details={
                "video_id": getattr(video, "id", ""),
                "save_dir": str(save_dir),
                "reason": reason,
                "error": "" if error is None else str(error),
            },
            trace_id=trace_id,
        )

    def _create_worker(self, video: VideoItem, save_dir: str):
        raise NotImplementedError

    def _connect_worker_callbacks(self, worker: Any) -> None:
        generation = self._explicit_worker_generation(worker)
        if not generation:
            generation = self._task_recovery_generation(getattr(worker, "video", None))
        try:
            worker._recovery_generation = generation
        except (AttributeError, TypeError):
            pass
        worker.sig_finished.connect(
            lambda video_id, generation=generation: self._mark_download_recovery_state(
                video_id,
                "completed",
                generation,
            )
        )
        worker.sig_error.connect(
            lambda video_id, _error, generation=generation: self._mark_download_recovery_state(
                video_id,
                "failed",
                generation,
            )
        )
        worker.sig_start.connect(self._emit_task_started)
        worker.sig_progress.connect(self._emit_task_progress)
        worker.sig_finished.connect(self._emit_task_finished)
        worker.sig_error.connect(self._emit_task_error)
        worker.finished.connect(lambda w=worker: self._on_worker_thread_finished(w))

    def _emit_task_started(self, video_id: str) -> None:
        raise NotImplementedError

    def _emit_task_progress(self, video_id: str, progress: int) -> None:
        raise NotImplementedError

    def _emit_task_finished(self, video_id: str) -> None:
        raise NotImplementedError

    def _emit_task_error(self, video_id: str, error: str) -> None:
        raise NotImplementedError

    def _find_worker(self, video_id: str):
        self.prune_finished_workers()
        with self._workers_lock:
            for worker in self.workers:
                if worker.video.id == video_id:
                    return worker
        return None

    def prune_finished_workers(self) -> int:
        """在计算可用容量前剔除已经结束却仍被登记的工作线程。

        正常情况下工作线程会通过 ``_handle_worker_completion`` 注销自己；
        如果完成回调被延迟、断开或因适配器边界情况而遗漏，这层兜底可防止
        陈旧线程永久占用动态并发槽位。
        """
        stale_workers: list[Any] = []
        with self._workers_lock:
            retained_workers: list[Any] = []
            for worker in list(getattr(self, "workers", []) or []):
                if self._worker_has_finished(worker):
                    stale_workers.append(worker)
                else:
                    retained_workers.append(worker)
            if not stale_workers:
                return 0
            self.workers = retained_workers

        for worker in stale_workers:
            if not getattr(worker, "_slot_released", False):
                self._release_worker_slot(worker, "stale_worker_pruned")
            if not getattr(worker, "_manager_cleanup_done", False):
                worker._manager_cleanup_done = True
                try:
                    self._on_worker_thread_finished(worker)
                except Exception as exc:  # pragma: no cover - 防御性清理路径
                    debug_logger.log_exception(
                        "DownloadManager",
                        "stale_worker_cleanup",
                        exc,
                        details={"video_id": getattr(getattr(worker, "video", None), "id", "")},
                    )
        qsize = getattr(self.queue, "qsize", None)
        try:
            queued_tasks = int(qsize()) if callable(qsize) else 0
        except Exception:
            queued_tasks = 0
        debug_logger.log(
            component="DownloadManager",
            action="prune_finished_workers",
            level="WARN",
            message="Pruned stale completed workers from active concurrency accounting.",
            status_code="DL_STALE_WORKERS_PRUNED",
            details={"count": len(stale_workers), "queued_tasks": queued_tasks},
        )
        self._get_dispatch_slot_gate().set()
        return len(stale_workers)

    @staticmethod
    def _worker_has_finished(worker: Any) -> bool:
        is_alive = getattr(worker, "is_alive", None)
        if callable(is_alive):
            try:
                if bool(is_alive()):
                    return False
                return getattr(worker, "ident", None) is not None
            except RuntimeError:
                return True
        is_running = getattr(worker, "isRunning", None)
        if callable(is_running):
            try:
                return not bool(is_running())
            except RuntimeError:
                return True
        if hasattr(worker, "is_running"):
            return not bool(worker.is_running)
        return False

    def _start_stop_guard(self) -> threading.RLock:
        lock = getattr(self, "_start_stop_lock", None)
        if lock is None:
            with self._GUARD_INIT_LOCK:
                lock = getattr(self, "_start_stop_lock", None)
                if lock is None:
                    lock = threading.RLock()
                    self._start_stop_lock = lock
        return lock

    def _remove_queued_task(self, video_id: str) -> VideoItem | None:
        removed = self._remove_queued_tasks({video_id})
        return removed[0] if removed else None

    def _remove_queued_tasks(self, video_ids: Iterable[str]) -> list[VideoItem]:
        remove_videos = getattr(self.queue, "remove_videos", None)
        if callable(remove_videos):
            return list(remove_videos(video_ids))
        remove_video = getattr(self.queue, "remove_video", None)
        if callable(remove_video):
            removed = []
            for video_id in video_ids:
                video = remove_video(video_id)
                if video is not None:
                    removed.append(video)
            return removed
        return []

    def queued_video_ids(self) -> set[str]:
        snapshot_video_ids = getattr(self.queue, "snapshot_video_ids", None)
        if callable(snapshot_video_ids):
            return snapshot_video_ids()
        return set()

    def _wait_for_dispatch_slot(self) -> bool:
        """等待可用调度槽；配置变化或 worker 完成时由 gate 唤醒重新评估容量。"""
        gate = self._get_dispatch_slot_gate()
        gate_wait_s = 0.25
        acquire_timeout_s = 0.25
        while self.is_running:
            if not self._has_any_dispatch_capacity():
                gate.wait(gate_wait_s)
                gate.clear()
                continue
            if not self._acquire_dispatch_slot(acquire_timeout_s):
                continue
            if self._has_any_dispatch_capacity():
                return True
            self._release_dispatch_slot("dispatch_slot_discarded_capacity")
        return False

    def _slot_semaphore_guard(self) -> threading.RLock:
        lock = getattr(self, "_slot_semaphore_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._slot_semaphore_lock = lock
        return lock

    def _acquire_dispatch_slot(self, timeout_s: float) -> bool:
        """占槽、确认仍是当前信号量、放弃旧 token 都在同一把锁里完成。"""
        try:
            deadline = time.monotonic() + max(0.0, float(timeout_s))
        except (TypeError, ValueError):
            deadline = time.monotonic()
        while self.is_running:
            with self._slot_semaphore_guard():
                semaphore = self.slot_semaphore
                if semaphore.acquire(blocking=False):
                    if semaphore is self.slot_semaphore:
                        return True
                    # 这个 token 即将被判定过期，不能留给重建后的新信号量去记账。
                    self._abandon_stale_slot_token(semaphore)
                    return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._get_dispatch_slot_gate().wait(min(0.05, remaining))
        return False

    def _abandon_stale_slot_token(self, semaphore: Any) -> None:
        try:
            semaphore.release()
        except ValueError:
            debug_logger.log(
                component="DownloadManager",
                action="release_stale_slot_skip",
                level="WARN",
                message="Stale dispatch semaphore token could not be returned after concurrency rebuild.",
                status_code="DL_STALE_SLOT_RELEASE_SKIP",
            )

    def _release_worker_slot(self, worker: Any, reason: str) -> None:
        self._release_dispatch_slot(reason, worker=worker)

    def _release_dispatch_slot(self, reason: str, worker: Any | None = None) -> None:
        """释放调度槽并唤醒 dispatcher；worker 标记避免完成钩子和 prune 双重释放。"""
        if worker is not None and getattr(worker, "_slot_released", False):
            return
        if worker is not None:
            worker._slot_released = True
        try:
            with self._slot_semaphore_guard():
                self.slot_semaphore.release()
        except ValueError:
            debug_logger.log(
                component="DownloadManager",
                action="release_slot_skip",
                level="WARN",
                message="Download slot release skipped because semaphore capacity is already full",
                status_code="DL_SLOT_RELEASE_SKIP",
                details={"reason": reason},
            )
            return
        self._get_dispatch_slot_gate().set()

        context = None
        trace_id = None
        if worker is not None:
            context = debug_logger.pick_used(
                {"trace_id": worker.video.meta.get("trace_id"), "video_id": worker.video.id},
                "trace_id", "video_id",
            )
            trace_id = worker.video.meta.get("trace_id")

        if worker is not None:
            debug_logger.log(
                component="DownloadManager",
                action="release_slot",
                message="Released download concurrency slot",
                status_code="DL_SLOT_RELEASE",
                context=context,
                details={"reason": reason},
                trace_id=trace_id,
            )

    def _handle_worker_completion(self, worker: Any, reason: str) -> None:
        with self._workers_lock:
            if worker in self.workers:
                self.workers.remove(worker)
        self._release_worker_slot(worker, reason)
        self._get_dispatch_slot_gate().set()
        if reason == "task_cancelled":
            video = getattr(worker, "video", None)
            generation = self._explicit_worker_generation(worker)
            if not generation:
                generation = self._task_recovery_generation(video)
            self._mark_download_recovery_state(
                getattr(video, "id", ""),
                "cancelled",
                generation,
            )

    def _on_worker_thread_finished(self, worker: Any):
        """供宿主适配器清理线程对象的扩展钩子。"""

    def stop_all(self):
        return stop_download_manager(self)
