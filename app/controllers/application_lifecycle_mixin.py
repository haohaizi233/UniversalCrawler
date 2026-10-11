from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from app.debug_logger import debug_logger

DOWNLOAD_STOP_WAIT_SECONDS = 2.0
_SHUTDOWN_STATE_LOCK = threading.RLock()
_REQUIRED_SHUTDOWN_STEPS = frozenset(
    {
        "downloads.request_stop",
        "downloads.stop_all",
        "release_timer.stop",
        "release_timer.disconnect",
        "event_bus.unsubscribe_spider",
        "event_bus.unsubscribe_download",
        "download_signal.task_started",
        "download_signal.task_progress",
        "download_signal.task_finished",
        "download_signal.task_error",
        "window_scheduler.stop",
        "media_watch.stop",
        "short_task_runner.cancel_all",
        "media.cleanup",
        "spider.stop",
        "frontend_state_service.destroy",
        "app_state.shutdown",
        "cache_service.close",
        "event_bus.shutdown",
    }
)


class _ApplicationShutdownState:
    def __init__(self) -> None:
        self.completed_steps: set[str] = set()
        self.downloads_done = threading.Event()
        self.gui_cleanup_done = threading.Event()
        self.done = threading.Event()
        self.failures: list[tuple[int, str, BaseException]] = []
        self.failure_reported = False
        self.lock = threading.RLock()
        self.thread: threading.Thread | None = None


def _log_shutdown_failure(action: str, failure: BaseException) -> None:
    try:
        debug_logger.log_exception("ApplicationController", action, failure)
    except BaseException:
        pass


def _attempt_shutdown_step(
    state: _ApplicationShutdownState,
    order: int,
    action: str,
    callback: Callable[[], Any],
) -> Any:
    with state.lock:
        if action in state.completed_steps:
            return True
    try:
        result = callback()
    except BaseException as failure:
        with state.lock:
            state.failures.append((order, action, failure))
        _log_shutdown_failure(action, failure)
        return False
    if result is False:
        return False
    with state.lock:
        state.completed_steps.add(action)
    return True


def _call_optional(owner: Any, attribute: str, method: str, *args: Any, **kwargs: Any) -> Any:
    target = getattr(owner, attribute, None)
    callback = getattr(target, method, None)
    if callable(callback):
        return callback(*args, **kwargs)
    return None


def _disconnect_release_timer(controller: Any) -> None:
    timer = getattr(controller, "_media_release_timer", None)
    timeout = getattr(timer, "timeout", None)
    disconnect = getattr(timeout, "disconnect", None)
    if not callable(disconnect):
        return
    try:
        disconnect()
    except (TypeError, RuntimeError):
        pass


def _unsubscribe_domain_handler(controller: Any, topic: str, handler_name: str) -> None:
    event_bus = getattr(controller, "event_bus", None)
    unsubscribe = getattr(event_bus, "unsubscribe", None)
    handler = getattr(controller, handler_name, None)
    if callable(unsubscribe) and handler is not None:
        unsubscribe(topic, handler)


def _disconnect_download_signal(controller: Any, signal_name: str) -> None:
    manager = getattr(controller, "dl_manager", None)
    signal = getattr(manager, signal_name, None)
    disconnect = getattr(signal, "disconnect", None)
    if not callable(disconnect):
        return
    try:
        disconnect()
    except (TypeError, RuntimeError):
        pass


def _destroy_frontend_state(controller: Any) -> bool:
    service = getattr(controller, "frontend_state_service", None)
    destroy = getattr(service, "destroy", None)
    if not callable(destroy):
        return True
    drained = destroy()
    if drained is not False:
        return True
    try:
        debug_logger.log(
            component="ApplicationController",
            action="shutdown_frontend_state_service",
            level="WARN",
            message="失败记录持久化在关闭等待超时后仍在排空",
            status_code="FAILED_RECORD_SHUTDOWN_GUARDIAN",
        )
    except BaseException:
        pass
    return False


def _run_application_shutdown(controller: Any, state: _ApplicationShutdownState) -> None:
    try:
        _attempt_shutdown_step(
            state,
            10,
            "downloads.stop_all",
            lambda: _call_optional(controller, "dl_manager", "stop_all"),
        )
        state.downloads_done.set()
        state.gui_cleanup_done.wait()
        steps: tuple[tuple[int, str, Callable[[], Any]], ...] = (
            (
                100,
                "frontend_state_service.destroy",
                lambda: _destroy_frontend_state(controller),
            ),
            (
                110,
                "app_state.shutdown",
                lambda: _call_optional(controller, "app_state", "shutdown"),
            ),
            (
                120,
                "cache_service.close",
                lambda: _call_optional(controller, "cache_service", "close"),
            ),
            (
                130,
                "event_bus.shutdown",
                lambda: _call_optional(controller, "event_bus", "shutdown"),
            ),
        )
        for order, action, callback in steps:
            failures_before = len(state.failures)
            result = _attempt_shutdown_step(state, order, action, callback)
            if action != "frontend_state_service.destroy" or result is not False:
                continue
            if len(state.failures) != failures_before:
                continue
            delay = 0.01
            while result is False:
                threading.Event().wait(delay)
                failures_before = len(state.failures)
                result = _attempt_shutdown_step(state, order, action, callback)
                if len(state.failures) != failures_before:
                    break
                delay = min(0.25, delay * 2.0)
    finally:
        state.downloads_done.set()
        state.done.set()


def _run_gui_cleanup(controller: Any, state: _ApplicationShutdownState) -> None:
    try:
        steps: tuple[tuple[int, str, Callable[[], Any]], ...] = (
            (
                20,
                "release_timer.stop",
                lambda: _call_optional(controller, "_media_release_timer", "stop"),
            ),
            (30, "release_timer.disconnect", lambda: _disconnect_release_timer(controller)),
            (
                40,
                "event_bus.unsubscribe_spider",
                lambda: _unsubscribe_domain_handler(
                    controller, "spider.domain_event", "_spider_domain_event_handler"
                ),
            ),
            (
                41,
                "event_bus.unsubscribe_download",
                lambda: _unsubscribe_domain_handler(
                    controller, "download.domain_event", "_download_domain_event_handler"
                ),
            ),
            *tuple(
                (
                    50 + index,
                    f"download_signal.{signal_name}",
                    lambda signal_name=signal_name: _disconnect_download_signal(
                        controller, signal_name
                    ),
                )
                for index, signal_name in enumerate(
                    ("task_started", "task_progress", "task_finished", "task_error")
                )
            ),
            (
                60,
                "window_scheduler.stop",
                lambda: _call_optional(
                    getattr(controller, "window", None),
                    "_ui_update_scheduler",
                    "stop",
                ),
            ),
            (
                70,
                "media_watch.stop",
                lambda: _call_optional(controller, "stop_media_directory_watch", "__call__"),
            ),
            (
                80,
                "short_task_runner.cancel_all",
                lambda: _call_optional(
                    controller,
                    "_short_task_runner",
                    "cancel_all",
                    timeout_ms=1000,
                ),
            ),
            (90, "media.cleanup", lambda: controller._host().cleanup_media()),
            (91, "spider.stop", lambda: controller._stop_active_spider()),
        )
        for order, action, callback in steps:
            _attempt_shutdown_step(state, order, action, callback)
    finally:
        state.gui_cleanup_done.set()


def _get_or_start_shutdown_state(controller: Any) -> _ApplicationShutdownState:
    with _SHUTDOWN_STATE_LOCK:
        state = getattr(controller, "_application_shutdown_state", None)
        if isinstance(state, _ApplicationShutdownState):
            if not state.done.is_set():
                return state
            if state.failures and not state.failure_reported:
                return state
            state.failures.clear()
            state.failure_reported = False
            if _REQUIRED_SHUTDOWN_STEPS <= state.completed_steps:
                return state
            state.done.clear()
            state.downloads_done.clear()
            state.gui_cleanup_done.clear()
        else:
            state = _ApplicationShutdownState()
            setattr(controller, "_application_shutdown_state", state)
            try:
                debug_logger.log(
                    component="ApplicationController",
                    action="shutdown",
                    level="WARN",
                    message="应用开始退出清理",
                    status_code="APP_SHUTDOWN",
                )
            except BaseException:
                pass
        _attempt_shutdown_step(
            state,
            0,
            "downloads.request_stop",
            lambda: _call_optional(controller, "dl_manager", "request_stop"),
        )
        try:
            state.thread = threading.Thread(
                target=_run_application_shutdown,
                args=(controller, state),
                daemon=False,
                name=f"application-shutdown-{id(controller):x}",
            )
            state.thread.start()
        except BaseException as failure:
            state.thread = None
            state.failures.append((5, "shutdown_guardian.start", failure))
            _log_shutdown_failure("shutdown_guardian.start", failure)
            state.downloads_done.set()
        return state


class ApplicationLifecycleMixin:
    """桌面应用的退出清理与事件循环辅助逻辑。"""

    def _stop_active_spider(self) -> None:
        spider = getattr(self, "current_spider", None)
        if spider and spider.isRunning():
            spider.stop()
            spider.wait(2000)

    def shutdown(self) -> bool:
        """在应用退出前停止媒体播放、爬虫线程和下载线程。"""
        deadline = time.monotonic() + DOWNLOAD_STOP_WAIT_SECONDS
        state = _get_or_start_shutdown_state(self)
        state.downloads_done.wait(max(0.0, deadline - time.monotonic()))
        _run_gui_cleanup(self, state)
        if state.thread is None and not state.done.is_set():
            _run_application_shutdown(self, state)
        state.done.wait(max(0.0, deadline - time.monotonic()))
        if not state.done.is_set():
            return False
        if state.failures:
            state.failure_reported = True
            raise min(state.failures, key=lambda item: item[0])[2]
        return _REQUIRED_SHUTDOWN_STEPS <= state.completed_steps

    def run(self):
        sys.exit(self.app.exec())
