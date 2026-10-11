"""把控制器事件桥接到 WebSocket，并调度前端增量快照。"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

from app.debug_logger import debug_logger
from app.services.frontend_event_aggregator import FrontendEventPriority, priority_for_topic, sections_for_topic

DELTA_ONLY_LEGACY_TOPICS = frozenset(
    {
        "video_state_changed",
        "task_progress",
        "videos.update",
        "log",
        "logs.append",
        "videos.reconcile",
    }
)

class WebSocketBridge:
    """线程安全地把控制器事件桥接到 WebSocket，并调度前端增量快照。"""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop | None,
        send_func: Callable,
        *,
        event_recorder: Callable[[str, dict[str, Any]], None] | None = None,
        delta_provider: Callable[[int], dict[str, Any]] | None = None,
        delta_interval_seconds: float = 0.12,
    ):
        self._loop = loop
        self._send_func = send_func
        self._event_recorder = event_recorder
        self._delta_provider = delta_provider
        self._delta_interval_seconds = max(0.02, float(delta_interval_seconds))
        self._pending_tasks: set[asyncio.Task] = set()
        self._delta_lock = threading.RLock()
        self._delta_pending = False
        self._delta_schedule_generation = 0
        self._last_delta_version = 0

    def _get_loop(self) -> asyncio.AbstractEventLoop | None:
        """延迟获取 uvicorn 事件循环，使工作线程可以安全发送事件。"""
        if self._loop is None or self._loop.is_closed():
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                return None
        return self._loop

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        """显式绑定正在运行的 WebSocket 事件循环。"""
        self._loop = loop

    def mark_frontend_version_sent(self, version: int) -> None:
        try:
            normalized = int(version or 0)
        except (TypeError, ValueError):
            normalized = 0
        with self._delta_lock:
            self._last_delta_version = max(self._last_delta_version, normalized)

    def emit(self, event_type: str, data: Any = None):
        """任意线程都可调用：先记录状态事件，再按需发送兼容旧前端的事件。"""
        event_type = str(event_type or "")
        payload = data if isinstance(data, dict) else {}
        self._record_frontend_event(event_type, payload)
        self._schedule_frontend_delta(event_type)
        if self._should_send_legacy_event(event_type):
            self._schedule_send(event_type, data)

    def _should_send_legacy_event(self, event_type: str) -> bool:
        if self._delta_provider is None:
            return True
        return str(event_type or "") not in DELTA_ONLY_LEGACY_TOPICS

    def _record_frontend_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type in {"frontend_state", "frontend_delta", "init_state", "platforms", "config"}:
            return
        if self._event_recorder is None:
            return
        try:
            self._event_recorder(event_type, payload)
        except Exception as exc:
            debug_logger.log_exception("WebSocketBridge", "record_frontend_event", exc)

    def _schedule_frontend_delta(self, event_type: str) -> None:
        """合并短时间内的普通更新；关键事件立即刷新，保证删除/失败等状态及时到达。"""
        if self._delta_provider is None or event_type in {"frontend_state", "frontend_delta"}:
            return
        if sections_for_topic(event_type) is None:
            return
        priority = priority_for_topic(event_type)
        delay = 0.0 if priority == FrontendEventPriority.CRITICAL else self._delta_interval_seconds
        with self._delta_lock:
            if self._delta_pending and priority != FrontendEventPriority.CRITICAL:
                return
            self._delta_pending = True
            self._delta_schedule_generation += 1
            generation = self._delta_schedule_generation
        loop = self._get_loop()

        def _schedule() -> None:
            if delay <= 0:
                loop.call_soon(self._flush_frontend_delta, generation)
            else:
                loop.call_later(delay, self._flush_frontend_delta, generation)

        if loop and loop.is_running():
            loop.call_soon_threadsafe(_schedule)
        else:
            with self._delta_lock:
                if generation == self._delta_schedule_generation:
                    self._delta_pending = False
            debug_logger.log(
                "WebSocketBridge",
                "frontend_delta_deferred",
                level="DEBUG",
                message="Web event loop is unavailable; deferred frontend delta until a later async flush.",
                status_code="WEB_DELTA_DEFERRED",
                details={"event_type": event_type, "generation": generation},
            )

    def _flush_frontend_delta(self, generation: int | None = None) -> None:
        """在事件循环里触发 delta 构建；过期 generation 表示已有更新的刷新计划。"""
        with self._delta_lock:
            if generation is not None and generation != self._delta_schedule_generation:
                return
            self._delta_pending = False
            base_version = self._last_delta_version
        if self._delta_provider is None:
            return
        loop = self._get_loop()
        if loop and loop.is_running() and callable(getattr(loop, "run_in_executor", None)):
            try:
                task = loop.create_task(self._build_and_send_frontend_delta(base_version, generation))
            except RuntimeError:
                pass
            else:
                self._track_task(task)
                return
        debug_logger.log(
            "WebSocketBridge",
            "frontend_delta_skipped",
            level="DEBUG",
            message="Skipped frontend delta flush because no running event loop is available.",
            status_code="WEB_DELTA_NO_LOOP",
            details={"generation": generation},
        )

    async def _build_and_send_frontend_delta(self, base_version: int, generation: int | None = None) -> None:
        if self._delta_provider is None:
            return
        try:
            loop = asyncio.get_running_loop()
            delta = await loop.run_in_executor(None, self._delta_provider, base_version)
        except Exception as exc:
            debug_logger.log_exception("WebSocketBridge", "frontend_delta", exc)
            return
        self._emit_frontend_delta(delta, base_version, generation)

    def _emit_frontend_delta(self, delta: dict[str, Any], base_version: int, generation: int | None = None) -> None:
        if not isinstance(delta, dict):
            return
        with self._delta_lock:
            if generation is not None and generation != self._delta_schedule_generation:
                return
        version = int(delta.get("version") or base_version or 0)
        if version == base_version and not delta.get("changed_sections"):
            return

        def _mark_delivered() -> None:
            with self._delta_lock:
                self._last_delta_version = max(self._last_delta_version, version)

        self._schedule_send("frontend_delta", delta, on_accepted=_mark_delivered)

    def _schedule_send(
        self,
        event_type: str,
        data: Any = None,
        *,
        on_accepted: Callable[[], None] | None = None,
    ) -> bool:
        """把发送动作调度回 Web 事件循环；非协程线程不能直接 await WebSocket。"""
        target_loop = self._get_loop()
        try:
            current_loop = asyncio.get_running_loop()
            if current_loop is target_loop:
                def _schedule():
                    result = self._send_func(event_type, data)
                    self._handle_send_result(result, current_loop, on_accepted=on_accepted)

                current_loop.call_soon(_schedule)
                return True
        except RuntimeError:
            pass

        if target_loop and target_loop.is_running():
            def _schedule():
                result = self._send_func(event_type, data)
                self._handle_send_result(result, target_loop, on_accepted=on_accepted)

            target_loop.call_soon_threadsafe(_schedule)
            return True
        else:
            import logging
            logging.warning(f"WebSocketBridge: event loop is not running; dropped event {event_type}")
            return False

    def _handle_send_result(
        self,
        result: Any,
        loop: asyncio.AbstractEventLoop,
        *,
        on_accepted: Callable[[], None] | None = None,
    ) -> None:
        if asyncio.iscoroutine(result):
            self._track_task(loop.create_task(result), on_accepted=on_accepted)
            return
        if result is not False and on_accepted is not None:
            on_accepted()

    def _track_task(self, task: asyncio.Task, *, on_accepted: Callable[[], None] | None = None) -> None:
        add_done_callback = getattr(task, "add_done_callback", None)
        if not callable(add_done_callback):
            return
        self._pending_tasks.add(task)

        def _discard(done_task: asyncio.Task) -> None:
            self._pending_tasks.discard(done_task)
            try:
                result = done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                debug_logger.log_exception("WebSocketBridge", "send_task", exc)
            else:
                if result is not False and on_accepted is not None:
                    on_accepted()

        add_done_callback(_discard)

    def call_later(self, delay_seconds: float, callback: Callable[[], None]) -> None:
        """在可用的 Web 事件循环上延迟调度回调。"""
        loop = self._get_loop()
        if loop and loop.is_running():
            loop.call_soon_threadsafe(lambda: loop.call_later(delay_seconds, callback))
            return
        callback()
