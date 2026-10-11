"""Bounded teardown orchestration for :class:`FrontendStateService`."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from app.services.failed_record_shutdown_guard import (
    ensure_failed_record_store_drained,
    failed_record_store_guardian_drained,
    log_shutdown_exception_safely,
    log_shutdown_pending_safely,
)

_REQUIRED_STEPS = frozenset(
    {
        "destroyed",
        "failed_record_store",
        "unsubscribe.app_state",
        "unsubscribe.config",
        "stop_external_config_sync",
        "metadata_retry_tracker",
        "metadata_probe_queue",
        "delta_queue",
        "file_log_cache",
        "tool_runner",
        "media_metadata",
        "app_state",
    }
)
_GUARDIAN_LOCK = threading.RLock()
_GUARDIANS: dict[int, "_FrontendShutdownState"] = {}
_REAL_SLEEP = time.sleep
_MAX_FRONTEND_SHUTDOWN_ATTEMPTS = 24


class _FrontendShutdownState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.thread: threading.Thread | None = None


def destroy_frontend_state_service(host: Any) -> bool:
    """Release resources; a guardian retries exact-False incomplete steps."""

    state = _shutdown_state_for(host)
    with state.lock:
        completed, failures = _destroy_pass(host)
        if failures:
            _discard_inactive_shutdown_state(host, state)
            raise failures[0]
        if completed:
            _discard_inactive_shutdown_state(host, state)
            return True
        try:
            _ensure_frontend_shutdown_guardian(host, state)
        except BaseException:
            _discard_inactive_shutdown_state(host, state)
            raise
        return False


def _destroy_pass(host: Any) -> tuple[bool, list[BaseException]]:
    failures: list[BaseException] = []
    completed_steps: set[str] = set()

    try:
        setattr(host, "_is_shutting_down", True)
    except BaseException as exc:
        failures.append(exc)
        log_shutdown_exception_safely("FrontendStateService", "destroy.shutting_down", exc)

    def record_failure(action: str, exc: BaseException) -> None:
        failures.append(exc)
        log_shutdown_exception_safely("FrontendStateService", f"destroy.{action}", exc)

    try:
        if bool(getattr(host, "_shutdown_completed", False)):
            return True, failures
        setattr(host, "_shutdown_completed", False)
    except BaseException as exc:
        record_failure("completed_state", exc)

    try:
        progress = getattr(host, "_shutdown_completed_steps", None)
        if isinstance(progress, set):
            completed_steps = progress
        else:
            setattr(host, "_shutdown_completed_steps", completed_steps)
    except BaseException as exc:
        record_failure("completed_steps", exc)

    def cleanup(action: str, callback: Callable[[], Any]) -> None:
        if action in completed_steps:
            return
        try:
            result = callback()
        except BaseException as exc:
            record_failure(action, exc)
            return
        if result is not False:
            completed_steps.add(action)

    cleanup("destroyed", lambda: _mark_destroyed(host))
    cleanup("failed_record_store", lambda: _drain_failed_record_store(host.failed_record_store))
    cleanup("unsubscribe.app_state", lambda: _unsubscribe_app_state(host))
    cleanup("unsubscribe.config", lambda: _unsubscribe_config(host))
    cleanup("stop_external_config_sync", lambda: _stop_external_config_sync(host))
    cleanup(
        "metadata_retry_tracker",
        lambda: host._metadata_retry_tracker.cancel_all(clear_failures=True),
    )
    cleanup("metadata_probe_queue", lambda: host._cancel_metadata_probe_queue(close=True))
    cleanup("delta_queue", lambda: _clear_pending_delta_events(host))
    cleanup("file_log_cache", lambda: host._file_log_cache_store.shutdown())
    cleanup(
        "tool_runner",
        lambda: _shutdown_owned_service(
            host,
            "_owns_tool_runner_service",
            "tool_runner_service",
            wait=True,
            timeout=1.0,
        ),
    )
    cleanup(
        "media_metadata",
        lambda: _shutdown_owned_service(
            host,
            "_owns_media_metadata_service",
            "media_metadata_service",
        ),
    )
    cleanup(
        "app_state",
        lambda: _shutdown_owned_service(host, "_owns_app_state", "app_state"),
    )

    completed = _REQUIRED_STEPS <= completed_steps
    if completed:
        try:
            setattr(host, "_shutdown_completed", True)
        except BaseException as exc:
            record_failure("completed_state.write", exc)
            completed = False
    return completed, failures


def _mark_destroyed(host: Any) -> None:
    if host._destroyed is not True:
        host._destroyed = True


def _shutdown_state_for(host: Any) -> _FrontendShutdownState:
    key = id(host)
    with _GUARDIAN_LOCK:
        state = _GUARDIANS.get(key)
        if state is None:
            state = _FrontendShutdownState()
            _GUARDIANS[key] = state
        return state


def _discard_inactive_shutdown_state(host: Any, state: _FrontendShutdownState) -> None:
    """Remove a terminal synchronous state without disturbing an active guardian."""

    try:
        key = id(host)
        with _GUARDIAN_LOCK:
            thread = state.thread
            if thread is not None and thread.is_alive():
                return
            if _GUARDIANS.get(key) is state:
                _GUARDIANS.pop(key, None)
    except BaseException:
        # Registry housekeeping is secondary to the original shutdown result.
        return


def _ensure_frontend_shutdown_guardian(host: Any, state: _FrontendShutdownState) -> None:
    with _GUARDIAN_LOCK:
        if state.thread is not None and state.thread.is_alive():
            return
        guardian = threading.Thread(
            target=_retry_frontend_shutdown,
            args=(host, id(host), state),
            daemon=False,
            name=f"frontend-state-shutdown-{id(host):x}",
        )
        state.thread = guardian
        try:
            guardian.start()
        except BaseException:
            state.thread = None
            raise


def _retry_frontend_shutdown(host: Any, key: int, state: _FrontendShutdownState) -> None:
    delay = 0.01
    logged_failure = False
    attempts = 0
    try:
        while attempts < _MAX_FRONTEND_SHUTDOWN_ATTEMPTS:
            attempts += 1
            _REAL_SLEEP(delay)
            with state.lock:
                completed, failures = _destroy_pass(host)
            if completed:
                return
            if failures and not logged_failure:
                logged_failure = True
                log_shutdown_exception_safely(
                    "FrontendStateService",
                    "destroy.guardian_retry",
                    failures[0],
                )
            delay = min(0.25, delay * 2.0)
    finally:
        with _GUARDIAN_LOCK:
            if _GUARDIANS.get(key) is state:
                _GUARDIANS.pop(key, None)
            state.thread = None


def _unsubscribe_app_state(host: Any) -> None:
    handler = host._app_state_event_handler
    if handler is None:
        return
    _run_with_final_state(
        "unsubscribe.app_state",
        lambda: host.app_state.event_bus.unsubscribe("app_state.changed", handler),
        lambda: setattr(host, "_app_state_event_handler", None),
    )


def _unsubscribe_config(host: Any) -> None:
    handler = host._config_event_handler
    if handler is None:
        return

    def unsubscribe() -> None:
        callback = getattr(host.config, "unsubscribe", None)
        if callable(callback):
            callback("config.changed", handler)

    _run_with_final_state(
        "unsubscribe.config",
        unsubscribe,
        lambda: setattr(host, "_config_event_handler", None),
    )


def _stop_external_config_sync(host: Any) -> None:
    if not host._config_external_sync_started:
        return

    def stop_external_sync() -> None:
        callback = getattr(host.config, "stop_external_sync", None)
        if callable(callback):
            callback()

    _run_with_final_state(
        "stop_external_config_sync",
        stop_external_sync,
        lambda: setattr(host, "_config_external_sync_started", False),
    )


def _clear_pending_delta_events(host: Any) -> None:
    with host._delta_lock:
        host._pending_app_state_events.clear()
        host._pending_app_state_events_overflowed = False


def _run_with_final_state(
    action: str,
    callback: Callable[[], Any],
    finalize: Callable[[], Any],
) -> None:
    primary: BaseException | None = None
    try:
        callback()
    except BaseException as exc:
        primary = exc
    try:
        finalize()
    except BaseException as secondary:
        if primary is None:
            raise
        log_shutdown_exception_safely(
            "FrontendStateService",
            f"destroy.{action}.finalize",
            secondary,
        )
    if primary is not None:
        raise primary


def _shutdown_owned_service(
    host: Any,
    owner_flag: str,
    attribute: str,
    **shutdown_kwargs: Any,
) -> Any:
    if not getattr(host, owner_flag):
        return
    service = getattr(host, attribute)
    shutdown = getattr(service, "shutdown", None)
    if callable(shutdown):
        return shutdown(**shutdown_kwargs)
    return None


def _drain_failed_record_store(store: Any) -> bool:
    guardian_status = failed_record_store_guardian_drained(store)
    if guardian_status is not None:
        return guardian_status
    primary: BaseException | None = None
    try:
        drained = store.shutdown(timeout=1.0)
    except BaseException as exc:
        primary = exc
        log_shutdown_exception_safely(
            "FrontendStateService",
            "destroy.failed_record_store",
            exc,
        )
        drained = False
    if drained is True:
        return True
    log_shutdown_pending_safely()
    try:
        guardian_started = ensure_failed_record_store_drained(store)
    except BaseException as handoff_exc:
        if primary is not None:
            log_shutdown_exception_safely(
                "FrontendStateService",
                "destroy.failed_record_store_handoff",
                handoff_exc,
            )
            raise primary
        raise
    if primary is not None:
        raise primary
    return guardian_started


__all__ = ["destroy_frontend_state_service"]
