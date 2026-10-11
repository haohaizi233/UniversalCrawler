"""Process-exit guard for failed-record persistence still draining at shutdown."""

from __future__ import annotations

import threading
import time
import weakref
from typing import Any

from app.debug_logger import debug_logger


_GUARDIAN_LOCK = threading.RLock()
_GUARDIANS: dict[int, list["_GuardianState"]] = {}
_PENDING_RETRY_SECONDS = 0.05
_MAX_EXCEPTION_RETRY_SECONDS = 1.0
_MAX_DRAIN_ATTEMPTS = 24
_MAX_DRAIN_DIAGNOSTIC_CATEGORIES = 16
_TERMINAL_RETENTION_SECONDS = 5.0
_FALLBACK_WAIT_LOCK = threading.Lock()
_FALLBACK_WAIT_LOCK.acquire()
_WAIT_DIAGNOSTIC_LOCK = threading.Lock()
_WAIT_DIAGNOSTICS_RECORDED: set[str] = set()


class _GuardianState:
    def __init__(self, store: Any) -> None:
        self.store: Any | None = store
        self.store_ref: weakref.ReferenceType[Any] | None = None
        self.drained = False
        self.thread: threading.Thread | None = None
        self.expiry_timer: threading.Timer | None = None

    def matches(self, store: Any) -> bool:
        if self.store is store:
            return True
        store_ref = self.store_ref
        return store_ref is not None and store_ref() is store


def _state_for_store_locked(store_key: int, store: Any) -> _GuardianState | None:
    for state in _GUARDIANS.get(store_key, ()):
        if state.matches(store):
            return state
    return None


def _register_state_locked(store_key: int, state: _GuardianState) -> None:
    _GUARDIANS.setdefault(store_key, []).append(state)


def _remove_state_locked(store_key: int, state: _GuardianState) -> None:
    states = _GUARDIANS.get(store_key)
    if states is not None:
        remaining = [candidate for candidate in states if candidate is not state]
        if remaining:
            _GUARDIANS[store_key] = remaining
        else:
            _GUARDIANS.pop(store_key, None)
    timer = state.expiry_timer
    state.expiry_timer = None
    if timer is not None:
        try:
            timer.cancel()
        except BaseException:
            pass
    state.store = None
    state.store_ref = None


def _state_is_registered_locked(store_key: int, state: _GuardianState) -> bool:
    return any(candidate is state for candidate in _GUARDIANS.get(store_key, ()))


def _expire_completed_state(store_key: int, state: _GuardianState) -> None:
    with _GUARDIAN_LOCK:
        if _state_is_registered_locked(store_key, state):
            _remove_state_locked(store_key, state)


def _retain_completed_state_locked(store_key: int, state: _GuardianState) -> None:
    if not _state_is_registered_locked(store_key, state):
        return
    store = state.store
    if store is None:
        _remove_state_locked(store_key, state)
        return
    try:
        state_ref = weakref.ref(state)

        def _store_collected(_store_ref: weakref.ReferenceType[Any]) -> None:
            completed_state = state_ref()
            if completed_state is None:
                return
            with _GUARDIAN_LOCK:
                if _state_is_registered_locked(store_key, completed_state):
                    _remove_state_locked(store_key, completed_state)

        state.store_ref = weakref.ref(store, _store_collected)
        state.store = None
    except TypeError:
        pass

    try:
        timer = threading.Timer(
            _TERMINAL_RETENTION_SECONDS,
            _expire_completed_state,
            args=(store_key, state),
        )
        timer.daemon = True
        state.expiry_timer = timer
        timer.start()
    except BaseException as exc:
        _remove_state_locked(store_key, state)
        log_shutdown_exception_safely(
            "FailedRecordShutdownGuard",
            "retain_completed_state",
            exc,
        )


def ensure_failed_record_store_drained(store: Any) -> bool:
    """Keep the process alive until one already-pending store reports drained."""

    store_key = id(store)
    with _GUARDIAN_LOCK:
        existing = _state_for_store_locked(store_key, store)
        if existing is not None:
            if existing.drained:
                _remove_state_locked(store_key, existing)
                return True
            if existing.thread is not None and existing.thread.is_alive():
                return False
            _remove_state_locked(store_key, existing)
        state = _GuardianState(store)
        try:
            guardian = threading.Thread(
                target=_drain_in_guardian,
                args=(store, store_key, state),
                name=f"failed-record-drain-{store_key:x}",
                daemon=False,
            )
            state.thread = guardian
            _register_state_locked(store_key, state)
            guardian.start()
        except BaseException as exc:
            _remove_state_locked(store_key, state)
            log_shutdown_exception_safely(
                "FailedRecordShutdownGuard",
                "start_guardian",
                exc,
            )
            drained = _drain_synchronously(store)
            if not isinstance(exc, Exception):
                raise
            return drained
    return False


def failed_record_store_guardian_drained(store: Any) -> bool | None:
    """Return guardian completion without invoking the store again."""

    store_key = id(store)
    with _GUARDIAN_LOCK:
        state = _state_for_store_locked(store_key, store)
        if state is None:
            return None
        if not state.drained:
            return False
        _remove_state_locked(store_key, state)
        return True


def _drain_in_guardian(store: Any, store_key: int, state: _GuardianState) -> None:
    exception_delay = _PENDING_RETRY_SECONDS
    recorded_exception_categories: set[tuple[str, int]] = set()
    attempts = 0
    try:
        while attempts < _MAX_DRAIN_ATTEMPTS:
            attempts += 1
            try:
                if store.shutdown(timeout=1.0) is True:
                    with _GUARDIAN_LOCK:
                        state.drained = True
                    return
                exception_delay = _PENDING_RETRY_SECONDS
                _wait_safely(
                    _PENDING_RETRY_SECONDS,
                    "wait_for_pending_store",
                )
            except BaseException as exc:
                _log_drain_exception_bounded(
                    "drain_pending_store",
                    exc,
                    recorded_exception_categories,
                )
                _wait_safely(exception_delay, "wait_after_drain_exception")
                exception_delay = min(
                    _MAX_EXCEPTION_RETRY_SECONDS,
                    max(_PENDING_RETRY_SECONDS, exception_delay * 2.0),
                )
    finally:
        with _GUARDIAN_LOCK:
            state.thread = None
            if state.drained:
                _retain_completed_state_locked(store_key, state)
            else:
                _remove_state_locked(store_key, state)


def _drain_synchronously(store: Any) -> bool:
    exception_delay = _PENDING_RETRY_SECONDS
    recorded_exception_categories: set[tuple[str, int]] = set()
    attempts = 0
    while attempts < _MAX_DRAIN_ATTEMPTS:
        attempts += 1
        try:
            if store.shutdown(timeout=1.0) is True:
                return True
            exception_delay = _PENDING_RETRY_SECONDS
            _wait_safely(
                _PENDING_RETRY_SECONDS,
                "wait_for_pending_store_synchronously",
            )
        except BaseException as exc:
            _log_drain_exception_bounded(
                "drain_pending_store_synchronously",
                exc,
                recorded_exception_categories,
            )
            _wait_safely(
                exception_delay,
                "wait_after_synchronous_drain_exception",
            )
            exception_delay = min(
                _MAX_EXCEPTION_RETRY_SECONDS,
                max(_PENDING_RETRY_SECONDS, exception_delay * 2.0),
            )
    return False


def _wait_safely(seconds: float, action: str) -> None:
    try:
        time.sleep(seconds)
        return
    except BaseException as exc:
        _log_wait_exception_once(action, exc)
    try:
        threading.Event().wait(seconds)
    except BaseException as exc:
        _log_wait_exception_once(f"{action}_fallback", exc)
        try:
            _FALLBACK_WAIT_LOCK.acquire(timeout=max(0.0, seconds))
        except BaseException as fallback_exc:
            _log_wait_exception_once(f"{action}_bounded_fallback", fallback_exc)


def _log_wait_exception_once(action: str, exc: BaseException) -> None:
    with _WAIT_DIAGNOSTIC_LOCK:
        if action in _WAIT_DIAGNOSTICS_RECORDED:
            return
        _WAIT_DIAGNOSTICS_RECORDED.add(action)
    log_shutdown_exception_safely(
        "FailedRecordShutdownGuard",
        action,
        exc,
    )


def _log_drain_exception_bounded(
    action: str,
    exc: BaseException,
    recorded_categories: set[tuple[str, int]],
) -> None:
    category = (action, id(type(exc)))
    if category in recorded_categories:
        return
    if len(recorded_categories) >= _MAX_DRAIN_DIAGNOSTIC_CATEGORIES:
        return
    recorded_categories.add(category)
    log_shutdown_exception_safely(
        "FailedRecordShutdownGuard",
        action,
        exc,
    )


def log_shutdown_exception_safely(
    component: str,
    action: str,
    exc: BaseException,
) -> None:
    try:
        debug_logger.log_exception(component, action, exc)
    except BaseException:
        return


def log_shutdown_pending_safely() -> None:
    try:
        debug_logger.log(
            component="FrontendStateService",
            action="destroy.failed_record_store_pending",
            level="WARN",
            message=(
                "Failed-record persistence is still draining after the shutdown wait "
                "expired"
            ),
            status_code="FAILED_RECORD_SHUTDOWN_PENDING",
        )
    except BaseException:
        return


__all__ = ["ensure_failed_record_store_drained", "failed_record_store_guardian_drained"]
