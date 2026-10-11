from __future__ import annotations

import gc
import threading
import time
import weakref
from unittest.mock import patch

import app.services.failed_record_shutdown_guard as shutdown_guard
from app.services.failed_record_shutdown_guard import (
    _wait_safely,
    ensure_failed_record_store_drained,
    failed_record_store_guardian_drained,
)


class _ControlledStore:
    def __init__(self) -> None:
        self.allow_drain = threading.Event()
        self.drained = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def shutdown(self, timeout: float = 1.0) -> bool:
        del timeout
        with self._lock:
            self.calls += 1
        if not self.allow_drain.is_set():
            return False
        self.drained.set()
        return True


def _guardian_for_key(store_key: int) -> threading.Thread:
    name = f"failed-record-drain-{store_key:x}"
    return next(thread for thread in threading.enumerate() if thread.name == name)


def test_completed_guardian_cannot_mark_new_store_with_reused_key_drained() -> None:
    reused_key = 0xFEED
    first = _ControlledStore()
    second = _ControlledStore()

    with patch.object(shutdown_guard, "id", return_value=reused_key, create=True):
        assert ensure_failed_record_store_drained(first) is False
        first_guardian = _guardian_for_key(reused_key)
        first.allow_drain.set()
        assert first.drained.wait(2.0)
        first_guardian.join(timeout=2.0)
        assert not first_guardian.is_alive()

        assert ensure_failed_record_store_drained(second) is False
        second_guardian = _guardian_for_key(reused_key)
        assert second_guardian is not first_guardian
        assert not second.drained.is_set()

        second.allow_drain.set()
        assert second.drained.wait(2.0)
        second_guardian.join(timeout=2.0)
        assert not second_guardian.is_alive()

        assert failed_record_store_guardian_drained(first) is True
        assert failed_record_store_guardian_drained(first) is None
        assert failed_record_store_guardian_drained(second) is True
        assert failed_record_store_guardian_drained(second) is None


def test_completed_guardian_notification_is_consumed_once_without_store_shutdown() -> None:
    store_key = 0xBEEF
    store = _ControlledStore()

    with patch.object(shutdown_guard, "id", return_value=store_key, create=True):
        assert ensure_failed_record_store_drained(store) is False
        guardian = _guardian_for_key(store_key)
        store.allow_drain.set()
        assert store.drained.wait(2.0)
        guardian.join(timeout=2.0)
        assert not guardian.is_alive()
        calls_after_drain = store.calls

        assert failed_record_store_guardian_drained(store) is True
        assert store.calls == calls_after_drain
        assert failed_record_store_guardian_drained(store) is None
        with shutdown_guard._GUARDIAN_LOCK:
            assert store_key not in shutdown_guard._GUARDIANS


def test_unconsumed_completed_guardian_does_not_retain_store_identity() -> None:
    store_key = 0xCAFE
    store = _ControlledStore()
    store_ref = weakref.ref(store)

    with patch.object(shutdown_guard, "id", return_value=store_key, create=True):
        assert ensure_failed_record_store_drained(store) is False
        guardian = _guardian_for_key(store_key)
        store.allow_drain.set()
        assert store.drained.wait(2.0)
        guardian.join(timeout=2.0)
        assert not guardian.is_alive()

    del store
    gc.collect()
    assert store_ref() is None
    with shutdown_guard._GUARDIAN_LOCK:
        assert store_key not in shutdown_guard._GUARDIANS


def test_unconsumed_weakrefable_completed_store_expires_while_still_alive() -> None:
    with patch.object(shutdown_guard, "_TERMINAL_RETENTION_SECONDS", 0.01):
        store = _ControlledStore()
        store_key = id(store)
        assert ensure_failed_record_store_drained(store) is False
        guardian = _guardian_for_key(store_key)
        store.allow_drain.set()
        assert store.drained.wait(2.0)
        guardian.join(timeout=2.0)
        assert not guardian.is_alive()

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with shutdown_guard._GUARDIAN_LOCK:
                if store_key not in shutdown_guard._GUARDIANS:
                    break
            time.sleep(0.01)

    assert store.drained.is_set()
    with shutdown_guard._GUARDIAN_LOCK:
        assert store_key not in shutdown_guard._GUARDIANS


def test_completed_guardian_notification_has_one_concurrent_consumer() -> None:
    store = _ControlledStore()

    assert ensure_failed_record_store_drained(store) is False
    guardian = _guardian_for_key(id(store))
    store.allow_drain.set()
    assert store.drained.wait(2.0)
    guardian.join(timeout=2.0)
    assert not guardian.is_alive()

    ready = threading.Barrier(3)
    results: list[bool | None] = []

    def consume() -> None:
        ready.wait()
        results.append(failed_record_store_guardian_drained(store))

    consumers = [threading.Thread(target=consume) for _ in range(2)]
    for consumer in consumers:
        consumer.start()
    ready.wait()
    for consumer in consumers:
        consumer.join(timeout=2.0)

    assert not any(consumer.is_alive() for consumer in consumers)
    assert sorted(results, key=lambda result: result is not True) == [True, None]


def test_non_weakrefable_completed_store_is_released_after_bounded_retention() -> None:
    collected = threading.Event()

    class _SlotStore:
        __slots__ = ("allow_drain", "drained")

        def __init__(self) -> None:
            self.allow_drain = threading.Event()
            self.drained = threading.Event()

        def shutdown(self, timeout: float = 1.0) -> bool:
            del timeout
            if not self.allow_drain.is_set():
                return False
            self.drained.set()
            return True

        def __del__(self) -> None:
            collected.set()

    with patch.object(shutdown_guard, "_TERMINAL_RETENTION_SECONDS", 0.01):
        store = _SlotStore()
        store_key = id(store)
        assert ensure_failed_record_store_drained(store) is False
        guardian = _guardian_for_key(store_key)
        store.allow_drain.set()
        assert store.drained.wait(2.0)
        guardian.join(timeout=2.0)
        assert not guardian.is_alive()
        del store
        assert collected.wait(2.0)

    with shutdown_guard._GUARDIAN_LOCK:
        assert store_key not in shutdown_guard._GUARDIANS


def test_repeated_pending_shutdown_starts_one_non_daemon_guardian() -> None:
    store = _ControlledStore()

    assert ensure_failed_record_store_drained(store) is False
    assert ensure_failed_record_store_drained(store) is False

    guardians = [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("failed-record-drain-") and thread.is_alive()
    ]
    assert len(guardians) == 1
    assert guardians[0].daemon is False

    store.allow_drain.set()
    assert store.drained.wait(2.0)
    guardians[0].join(timeout=2.0)
    assert not guardians[0].is_alive()


def test_guardian_survives_shutdown_exception_and_retries() -> None:
    class _FlakyStore:
        def __init__(self) -> None:
            self.calls = 0
            self.drained = threading.Event()

        def shutdown(self, timeout: float = 1.0) -> bool:
            del timeout
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary failure")
            self.drained.set()
            return True

    store = _FlakyStore()
    with patch(
        "app.services.failed_record_shutdown_guard.debug_logger.log_exception"
    ) as log_exception:
        assert ensure_failed_record_store_drained(store) is False
        assert store.drained.wait(2.0)

    assert store.calls >= 2
    log_exception.assert_called()


def test_repeated_drain_exceptions_are_throttled_without_skipping_retry_waits() -> None:
    class _RepeatedFailureStore:
        def __init__(self) -> None:
            self.calls = 0
            self.drained = threading.Event()

        def shutdown(self, timeout: float = 1.0) -> bool:
            del timeout
            self.calls += 1
            if self.calls <= 6:
                raise RuntimeError("persistent storage outage")
            self.drained.set()
            return True

    store = _RepeatedFailureStore()
    waits: list[tuple[float, str]] = []

    def _record_wait(seconds: float, action: str) -> None:
        waits.append((seconds, action))

    with (
        patch(
            "app.services.failed_record_shutdown_guard.debug_logger.log_exception"
        ) as log_exception,
        patch(
            "app.services.failed_record_shutdown_guard._wait_safely",
            side_effect=_record_wait,
        ),
    ):
        assert ensure_failed_record_store_drained(store) is False
        assert store.drained.wait(2.0)

    assert store.calls == 7
    assert len(waits) == 6
    assert all(action == "wait_after_drain_exception" for _, action in waits)
    assert [seconds for seconds, _ in waits] == sorted(seconds for seconds, _ in waits)
    assert log_exception.call_count == 1


def test_exception_category_hash_cannot_terminate_guardian_drain() -> None:
    class _HostileExceptionType(type):
        def __hash__(cls) -> int:
            del cls
            raise KeyboardInterrupt("exception type hash interrupted")

    class _HostileError(RuntimeError, metaclass=_HostileExceptionType):
        pass

    class _HostileStore:
        def __init__(self) -> None:
            self.calls = 0
            self.drained = threading.Event()

        def shutdown(self, timeout: float = 1.0) -> bool:
            del timeout
            self.calls += 1
            if self.calls == 1:
                raise _HostileError("temporary failure")
            self.drained.set()
            return True

    store = _HostileStore()
    assert ensure_failed_record_store_drained(store) is False
    assert store.drained.wait(2.0)
    assert store.calls == 2


def test_guardian_survives_base_exception_and_hostile_diagnostic_logger() -> None:
    class _HostileStore:
        def __init__(self) -> None:
            self.calls = 0
            self.drained = threading.Event()

        def shutdown(self, timeout: float = 1.0) -> bool:
            del timeout
            self.calls += 1
            if self.calls == 1:
                raise SystemExit("shutdown interrupted")
            self.drained.set()
            return True

    store = _HostileStore()
    with patch(
        "app.services.failed_record_shutdown_guard.debug_logger.log_exception",
        side_effect=KeyboardInterrupt("logger interrupted"),
    ):
        assert ensure_failed_record_store_drained(store) is False
        assert store.drained.wait(2.0)

    assert store.calls >= 2


def test_thread_start_failure_drains_synchronously_before_returning() -> None:
    class _FallbackStore:
        def __init__(self) -> None:
            self.calls = 0

        def shutdown(self, timeout: float = 1.0) -> bool:
            del timeout
            self.calls += 1
            return self.calls >= 2

    store = _FallbackStore()
    with patch.object(threading.Thread, "start", side_effect=RuntimeError("start failed")):
        started = time.monotonic()
        assert ensure_failed_record_store_drained(store) is True

    assert store.calls == 2
    assert time.monotonic() - started < 2.0


def test_thread_start_base_exception_drains_before_reraising() -> None:
    class _FallbackStore:
        def __init__(self) -> None:
            self.calls = 0
            self.drained = False

        def shutdown(self, timeout: float = 1.0) -> bool:
            del timeout
            self.calls += 1
            if self.calls == 1:
                return False
            self.drained = True
            return True

    store = _FallbackStore()
    with patch.object(
        threading.Thread,
        "start",
        side_effect=KeyboardInterrupt("thread start interrupted"),
    ):
        try:
            ensure_failed_record_store_drained(store)
        except KeyboardInterrupt as exc:
            assert str(exc) == "thread start interrupted"
        else:
            raise AssertionError("the thread start interruption must be preserved")

    assert store.drained is True
    assert store.calls == 2


def test_synchronous_fallback_survives_base_exception_and_hostile_logger() -> None:
    class _FallbackStore:
        def __init__(self) -> None:
            self.calls = 0

        def shutdown(self, timeout: float = 1.0) -> bool:
            del timeout
            self.calls += 1
            if self.calls == 1:
                raise GeneratorExit("temporary interruption")
            return True

    store = _FallbackStore()
    with (
        patch.object(threading.Thread, "start", side_effect=RuntimeError("start failed")),
        patch(
            "app.services.failed_record_shutdown_guard.debug_logger.log_exception",
            side_effect=SystemExit("logger failed"),
        ),
    ):
        assert ensure_failed_record_store_drained(store) is True

    assert store.calls == 2


def test_thread_constructor_failure_drains_synchronously_before_returning() -> None:
    store = _ControlledStore()
    store.allow_drain.set()

    with patch(
        "app.services.failed_record_shutdown_guard.threading.Thread",
        side_effect=RuntimeError("thread construction failed"),
    ):
        assert ensure_failed_record_store_drained(store) is True

    assert store.drained.is_set()


def test_thread_constructor_base_exception_drains_before_reraising() -> None:
    store = _ControlledStore()
    store.allow_drain.set()

    with patch(
        "app.services.failed_record_shutdown_guard.threading.Thread",
        side_effect=KeyboardInterrupt("thread construction interrupted"),
    ):
        try:
            ensure_failed_record_store_drained(store)
        except KeyboardInterrupt as exc:
            assert str(exc) == "thread construction interrupted"
        else:
            raise AssertionError("the thread construction interruption must be preserved")

    assert store.drained.is_set()


def test_guardian_survives_retry_wait_base_exception() -> None:
    class _RetryStore:
        def __init__(self) -> None:
            self.calls = 0
            self.drained = threading.Event()

        def shutdown(self, timeout: float = 1.0) -> bool:
            del timeout
            self.calls += 1
            if self.calls == 1:
                raise GeneratorExit("retry requested")
            self.drained.set()
            return True

    store = _RetryStore()
    waits = iter((KeyboardInterrupt("wait interrupted"), None))

    def _hostile_sleep(_seconds: float) -> None:
        outcome = next(waits, None)
        if isinstance(outcome, BaseException):
            raise outcome

    with patch("app.services.failed_record_shutdown_guard.time.sleep", _hostile_sleep):
        assert ensure_failed_record_store_drained(store) is False
        assert store.drained.wait(2.0)

    assert store.calls >= 2


def test_synchronous_fallback_survives_retry_wait_base_exception() -> None:
    class _RetryStore:
        def __init__(self) -> None:
            self.calls = 0

        def shutdown(self, timeout: float = 1.0) -> bool:
            del timeout
            self.calls += 1
            if self.calls == 1:
                raise GeneratorExit("retry requested")
            return True

    store = _RetryStore()
    waits = iter((KeyboardInterrupt("wait interrupted"), None))

    def _hostile_sleep(_seconds: float) -> None:
        outcome = next(waits, None)
        if isinstance(outcome, BaseException):
            raise outcome

    with (
        patch.object(threading.Thread, "start", side_effect=RuntimeError("start failed")),
        patch("app.services.failed_record_shutdown_guard.time.sleep", _hostile_sleep),
    ):
        assert ensure_failed_record_store_drained(store) is True

    assert store.calls >= 2


def test_retry_wait_remains_throttled_when_both_wait_apis_raise() -> None:
    with (
        patch(
            "app.services.failed_record_shutdown_guard.time.sleep",
            side_effect=KeyboardInterrupt("sleep interrupted"),
        ),
        patch(
            "app.services.failed_record_shutdown_guard.debug_logger.log_exception"
        ),
        patch(
            "app.services.failed_record_shutdown_guard.threading.Event"
        ) as event_type,
    ):
        event_type.return_value.wait.side_effect = SystemExit("event wait interrupted")
        started = time.monotonic()
        _wait_safely(0.03, "hostile_wait")

    assert time.monotonic() - started >= 0.02


def test_guardian_stops_after_attempt_limit_when_store_never_drains() -> None:
    class _NeverStore:
        def __init__(self) -> None:
            self.calls = 0

        def shutdown(self, timeout: float = 1.0) -> bool:
            del timeout
            self.calls += 1
            return False

    store = _NeverStore()
    with (
        patch.object(shutdown_guard, "_MAX_DRAIN_ATTEMPTS", 3),
        patch.object(shutdown_guard, "_wait_safely", lambda *_args, **_kwargs: None),
    ):
        assert ensure_failed_record_store_drained(store) is False
        guardian = _guardian_for_key(id(store))
        guardian.join(timeout=2.0)

    assert not guardian.is_alive()
    assert store.calls == 3
    assert failed_record_store_guardian_drained(store) is None


def test_synchronous_drain_returns_false_after_attempt_limit() -> None:
    class _NeverStore:
        def __init__(self) -> None:
            self.calls = 0

        def shutdown(self, timeout: float = 1.0) -> bool:
            del timeout
            self.calls += 1
            return False

    store = _NeverStore()
    with (
        patch.object(threading.Thread, "start", side_effect=RuntimeError("start failed")),
        patch.object(shutdown_guard, "_MAX_DRAIN_ATTEMPTS", 3),
        patch.object(shutdown_guard, "_wait_safely", lambda *_args, **_kwargs: None),
        patch("app.services.failed_record_shutdown_guard.debug_logger.log_exception"),
    ):
        assert ensure_failed_record_store_drained(store) is False

    assert store.calls == 3
