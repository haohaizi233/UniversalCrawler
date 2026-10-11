"""Best-effort shutdown orchestration for the desktop main window."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

from app.config import cfg
from app.ui import window_state_persistence

_T = TypeVar("_T")
ShutdownFailure = tuple[str, BaseException]


def _attempt(
    failures: list[ShutdownFailure],
    action: str,
    callback: Callable[[], _T],
) -> _T | None:
    try:
        return callback()
    except BaseException as exc:
        failures.append((action, exc))
        return None


def _mark_update_download_shutdown(window: Any) -> None:
    lock = window.__dict__.get("_update_download_lock")
    if lock is None:
        lock = threading.RLock()
        window._update_download_lock = lock
    with lock:
        window._update_download_shutdown = True
        sequence = int(window.__dict__.get("_update_download_sequence", 0) or 0)
        window._update_download_sequence = sequence + 1


def _signal_update_download_cancel(window: Any) -> None:
    cancel_event = window.__dict__.get("_update_download_cancel_event")
    if cancel_event is not None:
        cancel_event.set()


def _join_update_download_thread(window: Any) -> None:
    worker = window.__dict__.get("_update_download_thread")
    if worker is threading.current_thread():
        return
    is_alive = getattr(worker, "is_alive", None)
    if callable(is_alive) and is_alive():
        join = getattr(worker, "join", None)
        if not callable(join):
            return
        join(timeout=window.UPDATE_DOWNLOAD_SHUTDOWN_JOIN_SECONDS)


def _shutdown_owned_service(window: Any, owner_flag: str, attribute: str) -> None:
    service = window.__dict__.get(attribute)
    if not window.__dict__.get(owner_flag, False) or service is None:
        return
    method_name = "destroy" if attribute == "_frontend_state_service" else "shutdown"
    shutdown = getattr(service, method_name, None)
    if callable(shutdown):
        shutdown()


def _shutdown_worker(window: Any, attribute: str) -> None:
    worker = window.__dict__.get(attribute)
    if worker is not None:
        worker.shutdown()


def _shutdown_shell(window: Any) -> None:
    shell = window.__dict__.get("app_shell")
    shutdown = getattr(shell, "shutdown", None)
    if not callable(shutdown):
        window.cleanup_media()
        return

    primary: BaseException | None = None
    try:
        shutdown()
    except BaseException as exc:
        primary = exc
    if primary is None:
        return
    try:
        window.cleanup_media()
    except BaseException:
        pass
    raise primary


def _uninstall_window_chrome(window: Any) -> None:
    uninstall = getattr(window, "_uninstall_window_chrome", None)
    if callable(uninstall):
        uninstall()
        return
    window._chrome_controller().uninstall()


def _shutdown_owned_event_bus(window: Any) -> None:
    if not window.__dict__.get("_owns_event_bus", False):
        return
    shutdown = getattr(window.event_bus, "shutdown", None)
    if callable(shutdown):
        shutdown()


def shutdown_main_window(window: Any, event: Any) -> ShutdownFailure | None:
    """Run every shutdown step and always drive the close event to completion."""

    failures: list[ShutdownFailure] = []
    snapshot = _attempt(
        failures,
        "window_state.capture",
        lambda: window_state_persistence.capture_window_state(window),
    )
    try:
        steps: tuple[tuple[str, Callable[[], Any]], ...] = (
            ("update_download.mark_shutdown", lambda: _mark_update_download_shutdown(window)),
            ("update_download.cancel", lambda: _signal_update_download_cancel(window)),
            ("update_download.join", lambda: _join_update_download_thread(window)),
            ("ui_update_scheduler.stop", lambda: window._ui_update_scheduler.stop()),
            (
                "frontend_snapshot_worker.shutdown",
                lambda: _shutdown_worker(window, "_frontend_snapshot_worker"),
            ),
            (
                "frontend_action_worker.shutdown",
                lambda: _shutdown_worker(window, "_frontend_action_worker"),
            ),
            (
                "update_check_worker.shutdown",
                lambda: _shutdown_worker(window, "_update_check_worker"),
            ),
            (
                "frontend_state_service.destroy",
                lambda: _shutdown_owned_service(
                    window,
                    "_owns_frontend_state_service",
                    "_frontend_state_service",
                ),
            ),
            (
                "app_state.shutdown",
                lambda: _shutdown_owned_service(window, "_owns_app_state", "app_state"),
            ),
            (
                "connections.disconnect_all",
                lambda: window._connections.disconnect_all(),
            ),
            (
                "chrome_controller.uninstall",
                lambda: _uninstall_window_chrome(window),
            ),
            ("app_shell.shutdown", lambda: _shutdown_shell(window)),
            (
                "event_bus.unsubscribe",
                lambda: window.event_bus.unsubscribe(
                    "app_state.changed",
                    window._app_state_handler,
                ),
            ),
            (
                "event_bus.shutdown",
                lambda: _shutdown_owned_event_bus(window),
            ),
        )
        for action, callback in steps:
            _attempt(failures, action, callback)

        if snapshot is not None:
            _attempt(
                failures,
                "window_state.persist",
                lambda: window_state_persistence.start_window_state_persistence(
                    window,
                    snapshot,
                    cfg.save_ui_state,
                ),
            )
    finally:
        _attempt(failures, "event.accept", lambda: event.accept())
    return failures[0] if failures else None


__all__ = ["ShutdownFailure", "shutdown_main_window"]
