from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.services.frontend_state_shutdown as shutdown_runtime
from app.services.frontend_state_shutdown import destroy_frontend_state_service


class _PendingThenDrainedStore:
    def __init__(self) -> None:
        self.calls = 0
        self.drained = threading.Event()

    def shutdown(self, timeout: float = 1.0) -> bool:
        del timeout
        self.calls += 1
        if self.calls == 1:
            return False
        self.drained.set()
        return True


class _InterruptedThenDrainedStore:
    def __init__(self, primary: BaseException) -> None:
        self.primary = primary
        self.calls = 0
        self.drained = threading.Event()

    def shutdown(self, timeout: float = 1.0) -> bool:
        del timeout
        self.calls += 1
        if self.calls == 1:
            raise self.primary
        self.drained.set()
        return True


class _HostileInterrupt(KeyboardInterrupt):
    def with_traceback(self, _traceback):
        raise SystemExit("with_traceback replaced the primary")

    def add_note(self, _note: str) -> None:
        raise GeneratorExit("add_note replaced the primary")


class _HostileCleanupError(RuntimeError):
    def __str__(self) -> str:
        raise ValueError("string formatting replaced the cleanup error")

    def with_traceback(self, _traceback):
        raise SystemExit("with_traceback replaced the cleanup error")

    def add_note(self, _note: str) -> None:
        raise GeneratorExit("add_note replaced the cleanup error")


class _CleanupStep:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = 0

    def run(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.calls += 1
        if self.error is not None:
            raise self.error


class _ResultStep:
    def __init__(self, *results: bool) -> None:
        self.results = list(results)
        self.calls = 0

    def run(self, *args: object, **kwargs: object) -> bool:
        del args, kwargs
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


class _ReadyStore:
    def __init__(self) -> None:
        self.calls = 0

    def shutdown(self, timeout: float = 1.0) -> bool:
        del timeout
        self.calls += 1
        return True


def _host(*, metadata_error: BaseException | None = None, file_log_error: BaseException | None = None):
    metadata = _CleanupStep(metadata_error)
    probe = _CleanupStep()
    file_log = _CleanupStep(file_log_error)
    failed_store = _PendingThenDrainedStore()
    host = SimpleNamespace(
        _destroyed=False,
        _app_state_event_handler=None,
        _config_event_handler=None,
        _config_external_sync_started=False,
        _metadata_retry_tracker=SimpleNamespace(cancel_all=metadata.run),
        _cancel_metadata_probe_queue=probe.run,
        _delta_lock=threading.RLock(),
        _pending_app_state_events=[],
        _pending_app_state_events_overflowed=False,
        _file_log_cache_store=SimpleNamespace(shutdown=file_log.run),
        failed_record_store=failed_store,
        _owns_tool_runner_service=False,
        _owns_media_metadata_service=False,
        _owns_app_state=False,
    )
    return host, failed_store, metadata, probe, file_log


def test_synchronous_destroy_releases_frontend_shutdown_registry_state() -> None:
    host, _store, _metadata, _probe, _file_log = _host()
    host.failed_record_store = _ReadyStore()
    key = id(host)

    assert destroy_frontend_state_service(host) is True

    with shutdown_runtime._GUARDIAN_LOCK:
        assert key not in shutdown_runtime._GUARDIANS


def test_failed_synchronous_destroy_releases_inactive_registry_state() -> None:
    primary = RuntimeError("metadata cleanup failed")
    host, _store, _metadata, _probe, _file_log = _host(metadata_error=primary)
    host.failed_record_store = _ReadyStore()
    key = id(host)

    with pytest.raises(RuntimeError, match="metadata cleanup failed"):
        destroy_frontend_state_service(host)

    with shutdown_runtime._GUARDIAN_LOCK:
        assert key not in shutdown_runtime._GUARDIANS


def test_reused_host_identity_never_reuses_completed_shutdown_state() -> None:
    first, _store, _metadata, _probe, _file_log = _host()
    second, _store2, _metadata2, _probe2, _file_log2 = _host()
    first.failed_record_store = _ReadyStore()
    second.failed_record_store = _ReadyStore()
    reused_key = 0xCAFE

    with patch.object(shutdown_runtime, "id", return_value=reused_key, create=True):
        assert destroy_frontend_state_service(first) is True
        with shutdown_runtime._GUARDIAN_LOCK:
            assert reused_key not in shutdown_runtime._GUARDIANS
        assert destroy_frontend_state_service(second) is True
        with shutdown_runtime._GUARDIAN_LOCK:
            assert reused_key not in shutdown_runtime._GUARDIANS


@pytest.mark.parametrize(
    ("metadata_error", "file_log_error", "error_type", "message"),
    (
        (RuntimeError("metadata cleanup failed"), None, RuntimeError, "metadata cleanup failed"),
        (None, KeyboardInterrupt("file log cleanup interrupted"), KeyboardInterrupt, "file log cleanup interrupted"),
    ),
)
def test_cleanup_failure_still_hands_failed_records_to_guardian_and_preserves_error(
    metadata_error: BaseException | None,
    file_log_error: BaseException | None,
    error_type: type[BaseException],
    message: str,
) -> None:
    host, store, _metadata, probe, _file_log = _host(
        metadata_error=metadata_error,
        file_log_error=file_log_error,
    )

    with pytest.raises(error_type, match=message):
        destroy_frontend_state_service(host)

    assert store.drained.wait(2.0)
    assert store.calls >= 2
    assert probe.calls == 1


def test_failed_store_interrupt_is_handed_off_then_rethrown_exactly() -> None:
    primary = _HostileInterrupt("shutdown interrupted")
    host, _store, metadata, probe, file_log = _host(
        metadata_error=RuntimeError("later cleanup failure"),
    )
    store = _InterruptedThenDrainedStore(primary)
    host.failed_record_store = store

    caught: BaseException | None = None
    with patch(
        "app.services.failed_record_shutdown_guard.debug_logger.log_exception",
        side_effect=SystemExit("hostile logger"),
    ):
        try:
            destroy_frontend_state_service(host)
        except BaseException as exc:
            caught = exc

    assert caught is primary
    assert store.drained.wait(2.0)
    assert store.calls >= 2
    assert metadata.calls == 1
    assert probe.calls == 1
    assert file_log.calls == 1


def test_cleanup_primary_is_rethrown_without_calling_exception_methods() -> None:
    primary = _HostileCleanupError("metadata cleanup failed")
    host, store, metadata, probe, file_log = _host(metadata_error=primary)

    caught: BaseException | None = None
    with patch(
        "app.services.failed_record_shutdown_guard.debug_logger.log_exception",
        side_effect=lambda _component, _action, exc: str(exc),
    ):
        try:
            destroy_frontend_state_service(host)
        except BaseException as exc:
            caught = exc

    assert caught is primary
    assert store.drained.wait(2.0)
    assert metadata.calls == 1
    assert probe.calls == 1
    assert file_log.calls == 1


def test_owned_service_shutdown_lookup_failure_does_not_skip_later_services() -> None:
    primary = KeyboardInterrupt("metadata shutdown lookup failed")
    host, store, _metadata, _probe, _file_log = _host()
    tool_runner = _CleanupStep()
    app_state = _CleanupStep()

    class HostileMetadataService:
        def __getattribute__(self, name: str):
            if name == "shutdown":
                raise primary
            return object.__getattribute__(self, name)

    host._owns_tool_runner_service = True
    host.tool_runner_service = SimpleNamespace(shutdown=tool_runner.run)
    host._owns_media_metadata_service = True
    host.media_metadata_service = HostileMetadataService()
    host._owns_app_state = True
    host.app_state = SimpleNamespace(shutdown=app_state.run)

    caught: BaseException | None = None
    try:
        destroy_frontend_state_service(host)
    except BaseException as exc:
        caught = exc

    assert caught is primary
    assert store.drained.wait(2.0)
    assert tool_runner.calls == 1
    assert app_state.calls == 1


def test_unsubscribe_state_write_failure_cannot_replace_primary() -> None:
    primary = RuntimeError("unsubscribe failed")
    secondary = KeyboardInterrupt("handler state write failed")
    base, store, _metadata, probe, file_log = _host()

    class HostileHost(SimpleNamespace):
        def __setattr__(self, name: str, value: object) -> None:
            if (
                name == "_app_state_event_handler"
                and value is None
                and self.__dict__.get("_reject_handler_clear", False)
            ):
                raise secondary
            super().__setattr__(name, value)

    host = HostileHost(**base.__dict__)
    handler = object()
    event_bus = SimpleNamespace(unsubscribe=_CleanupStep(primary).run)
    host._app_state_event_handler = handler
    host.app_state = SimpleNamespace(event_bus=event_bus)
    host._reject_handler_clear = True

    caught: BaseException | None = None
    try:
        destroy_frontend_state_service(host)
    except BaseException as exc:
        caught = exc

    assert caught is primary
    assert store.drained.wait(2.0)
    assert probe.calls == 1
    assert file_log.calls == 1


def test_hostile_config_unsubscribe_lookup_still_clears_handler_state() -> None:
    primary = KeyboardInterrupt("config unsubscribe lookup interrupted")
    host, store, _metadata, probe, file_log = _host()

    class HostileConfig:
        def __getattribute__(self, name: str):
            if name == "unsubscribe":
                raise primary
            return object.__getattribute__(self, name)

    host.config = HostileConfig()
    host._config_event_handler = object()

    caught: BaseException | None = None
    try:
        destroy_frontend_state_service(host)
    except BaseException as exc:
        caught = exc

    assert caught is primary
    assert host._config_event_handler is None
    assert store.drained.wait(2.0)
    assert probe.calls == 1
    assert file_log.calls == 1


def test_hostile_external_sync_lookup_still_clears_started_state() -> None:
    primary = GeneratorExit("external sync lookup interrupted")
    host, store, _metadata, probe, file_log = _host()

    class HostileConfig:
        def __getattribute__(self, name: str):
            if name == "stop_external_sync":
                raise primary
            return object.__getattribute__(self, name)

    host.config = HostileConfig()
    host._config_external_sync_started = True

    caught: BaseException | None = None
    try:
        destroy_frontend_state_service(host)
    except BaseException as exc:
        caught = exc

    assert caught is primary
    assert host._config_external_sync_started is False
    assert store.drained.wait(2.0)
    assert probe.calls == 1
    assert file_log.calls == 1


def test_guardian_handoff_base_exception_cannot_replace_store_primary() -> None:
    primary = RuntimeError("initial failed-record shutdown failed")
    secondary = KeyboardInterrupt("guardian handoff interrupted")
    host, _store, metadata, probe, file_log = _host()
    host.failed_record_store = _InterruptedThenDrainedStore(primary)

    caught: BaseException | None = None
    with patch(
        "app.services.frontend_state_shutdown.ensure_failed_record_store_drained",
        side_effect=secondary,
    ):
        try:
            destroy_frontend_state_service(host)
        except BaseException as exc:
            caught = exc

    assert caught is primary
    assert metadata.calls == 1
    assert probe.calls == 1
    assert file_log.calls == 1


def test_destroyed_flag_lookup_failure_cannot_skip_failed_record_handoff() -> None:
    primary = KeyboardInterrupt("destroyed flag lookup interrupted")
    base, store, metadata, probe, file_log = _host()

    class HostileDestroyedLookup(SimpleNamespace):
        def __getattribute__(self, name: str):
            if name == "_destroyed":
                raise primary
            return super().__getattribute__(name)

    host = HostileDestroyedLookup(**base.__dict__)
    caught: BaseException | None = None
    try:
        destroy_frontend_state_service(host)
    except BaseException as exc:
        caught = exc

    assert caught is primary
    assert store.drained.wait(2.0)
    assert metadata.calls == 1
    assert probe.calls == 1
    assert file_log.calls == 1


def test_destroyed_flag_write_failure_cannot_skip_failed_record_handoff() -> None:
    primary = GeneratorExit("destroyed flag write interrupted")
    base, store, metadata, probe, file_log = _host()

    class HostileDestroyedWrite(SimpleNamespace):
        def __setattr__(self, name: str, value: object) -> None:
            if (
                name == "_destroyed"
                and value is True
                and self.__dict__.get("_reject_destroyed_write", False)
            ):
                raise primary
            super().__setattr__(name, value)

    host = HostileDestroyedWrite(**base.__dict__)
    host._reject_destroyed_write = True
    caught: BaseException | None = None
    try:
        destroy_frontend_state_service(host)
    except BaseException as exc:
        caught = exc

    assert caught is primary
    assert store.drained.wait(2.0)
    assert metadata.calls == 1
    assert probe.calls == 1
    assert file_log.calls == 1


def test_destroy_retries_only_failed_owned_service_steps() -> None:
    host, store, metadata, probe, file_log = _host()
    tool_runner = _CleanupStep(RuntimeError("tool runner still stopping"))
    app_state = _CleanupStep()
    host._owns_tool_runner_service = True
    host.tool_runner_service = SimpleNamespace(shutdown=tool_runner.run)
    host._owns_app_state = True
    host.app_state = SimpleNamespace(shutdown=app_state.run)

    with pytest.raises(RuntimeError, match="tool runner still stopping"):
        destroy_frontend_state_service(host)

    tool_runner.error = None
    assert destroy_frontend_state_service(host) is True

    assert store.calls == 2
    assert metadata.calls == 1
    assert probe.calls == 1
    assert file_log.calls == 1
    assert tool_runner.calls == 2
    assert app_state.calls == 1


def test_destroy_false_steps_retry_without_replaying_completed_cleanup() -> None:
    host, _store, metadata, probe, _file_log = _host()
    ready_store = _ReadyStore()
    file_log = _ResultStep(False, True)
    tool_runner = _ResultStep(False, True)
    app_state = _ResultStep(False, True)
    host.failed_record_store = ready_store
    host._file_log_cache_store = SimpleNamespace(shutdown=file_log.run)
    host._owns_tool_runner_service = True
    host.tool_runner_service = SimpleNamespace(shutdown=tool_runner.run)
    host._owns_app_state = True
    host.app_state = SimpleNamespace(shutdown=app_state.run)

    assert destroy_frontend_state_service(host) is False
    assert getattr(host, "_shutdown_completed", False) is False
    assert destroy_frontend_state_service(host) is True
    assert host._shutdown_completed is True

    assert metadata.calls == 1
    assert probe.calls == 1
    assert ready_store.calls == 1
    assert file_log.calls == 2
    assert tool_runner.calls == 2
    assert app_state.calls == 2


def test_destroy_waits_for_guardian_drain_before_marking_completed() -> None:
    host, _store, _metadata, _probe, _file_log = _host()

    class ControlledStore:
        def __init__(self) -> None:
            self.allow_drain = threading.Event()
            self.drained = threading.Event()
            self.calls = 0

        def shutdown(self, timeout: float = 1.0) -> bool:
            del timeout
            self.calls += 1
            if not self.allow_drain.is_set():
                return False
            self.drained.set()
            return True

    store = ControlledStore()
    host.failed_record_store = store

    assert destroy_frontend_state_service(host) is False
    assert getattr(host, "_shutdown_completed", False) is False
    assert destroy_frontend_state_service(host) is False
    store.allow_drain.set()
    assert store.drained.wait(2.0)
    calls_after_drain = store.calls

    assert destroy_frontend_state_service(host) is True
    assert host._shutdown_completed is True
    assert store.calls == calls_after_drain


def test_failed_store_exception_is_handed_off_then_rethrown_exactly() -> None:
    primary = RuntimeError("failed-record shutdown failed")
    host, _store, _metadata, _probe, _file_log = _host()
    store = _InterruptedThenDrainedStore(primary)
    host.failed_record_store = store

    caught: BaseException | None = None
    try:
        destroy_frontend_state_service(host)
    except BaseException as exc:
        caught = exc

    assert caught is primary
    assert store.drained.wait(2.0)


def test_single_destroy_call_guardian_retries_owned_tool_runner_false_to_true() -> None:
    host, _store, metadata, probe, _file_log = _host()
    host.failed_record_store = _ReadyStore()
    completed = threading.Event()

    class FalseThenTrueToolRunner:
        def __init__(self) -> None:
            self.calls = 0

        def shutdown(self, *, wait: bool, timeout: float) -> bool:
            assert wait is True
            assert timeout == 1.0
            self.calls += 1
            if self.calls == 1:
                return False
            completed.set()
            return True

    tool_runner = FalseThenTrueToolRunner()
    host._owns_tool_runner_service = True
    host.tool_runner_service = tool_runner

    assert destroy_frontend_state_service(host) is False
    assert completed.wait(2.0)
    deadline = time.monotonic() + 2.0
    while not getattr(host, "_shutdown_completed", False) and time.monotonic() < deadline:
        time.sleep(0.01)

    assert host._shutdown_completed is True
    assert tool_runner.calls == 2
    assert metadata.calls == 1
    assert probe.calls == 1
