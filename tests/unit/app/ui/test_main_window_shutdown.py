from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.ui.main_window_shutdown import shutdown_main_window


def _window() -> SimpleNamespace:
    window = SimpleNamespace()
    window.UPDATE_DOWNLOAD_SHUTDOWN_JOIN_SECONDS = 0.01
    window._update_download_lock = threading.RLock()
    window._update_download_sequence = 0
    window._update_download_shutdown = False
    window._update_download_cancel_event = Mock()
    window._update_download_thread = Mock()
    window._update_download_thread.is_alive.return_value = False
    window._ui_update_scheduler = Mock()
    window._frontend_snapshot_worker = Mock()
    window._frontend_action_worker = Mock()
    window._update_check_worker = Mock()
    window._owns_frontend_state_service = False
    window._owns_app_state = False
    window._owns_event_bus = True
    window._connections = Mock()
    window._chrome_controller = Mock(return_value=Mock())
    window.app_shell = Mock()
    window.cleanup_media = Mock()
    window.event_bus = Mock()
    window._app_state_handler = object()
    return window


def test_partial_window_still_runs_later_shutdown_steps() -> None:
    window = _window()
    event = Mock()
    del window._ui_update_scheduler

    with (
        patch(
            "app.ui.main_window_shutdown.window_state_persistence.capture_window_state",
            return_value={},
        ),
        patch(
            "app.ui.main_window_shutdown.window_state_persistence.start_window_state_persistence"
        ),
    ):
        failure = shutdown_main_window(window, event)

    assert failure is not None
    assert failure[0] == "ui_update_scheduler.stop"
    assert isinstance(failure[1], AttributeError)
    window._frontend_action_worker.shutdown.assert_called_once_with()
    window._update_check_worker.shutdown.assert_called_once_with()
    window.event_bus.shutdown.assert_called_once_with()
    event.accept.assert_called_once_with()


def test_hostile_event_bus_lookup_is_isolated() -> None:
    failure = RuntimeError("event bus lookup failed")

    class HostileWindow(SimpleNamespace):
        def __getattribute__(self, name: str):
            if name == "event_bus":
                raise failure
            return super().__getattribute__(name)

    base = _window()
    window = HostileWindow(**base.__dict__)
    event = Mock()

    with (
        patch(
            "app.ui.main_window_shutdown.window_state_persistence.capture_window_state",
            return_value={},
        ),
        patch(
            "app.ui.main_window_shutdown.window_state_persistence.start_window_state_persistence"
        ),
    ):
        result = shutdown_main_window(window, event)

    assert result == ("event_bus.unsubscribe", failure)
    window._frontend_action_worker.shutdown.assert_called_once_with()
    event.accept.assert_called_once_with()


def test_hostile_event_accept_lookup_cannot_replace_first_failure() -> None:
    primary = RuntimeError("snapshot shutdown failed")
    accept_failure = RuntimeError("event accept lookup failed")
    window = _window()
    window._frontend_snapshot_worker.shutdown.side_effect = primary

    class HostileEvent:
        def __init__(self) -> None:
            self.accept_reads = 0

        def __getattribute__(self, name: str):
            if name == "accept":
                reads = object.__getattribute__(self, "accept_reads")
                object.__setattr__(self, "accept_reads", reads + 1)
                raise accept_failure
            return object.__getattribute__(self, name)

    event = HostileEvent()
    with (
        patch(
            "app.ui.main_window_shutdown.window_state_persistence.capture_window_state",
            return_value={},
        ),
        patch(
            "app.ui.main_window_shutdown.window_state_persistence.start_window_state_persistence"
        ),
    ):
        result = shutdown_main_window(window, event)

    assert result == ("frontend_snapshot_worker.shutdown", primary)
    assert event.accept_reads == 1
    window._frontend_action_worker.shutdown.assert_called_once_with()


def test_capture_failure_skips_persistence_but_not_shutdown() -> None:
    primary = RuntimeError("window state capture failed")
    window = _window()
    event = Mock()

    with (
        patch(
            "app.ui.main_window_shutdown.window_state_persistence.capture_window_state",
            side_effect=primary,
        ),
        patch(
            "app.ui.main_window_shutdown.window_state_persistence.start_window_state_persistence"
        ) as persist,
    ):
        result = shutdown_main_window(window, event)

    assert result == ("window_state.capture", primary)
    persist.assert_not_called()
    window._frontend_action_worker.shutdown.assert_called_once_with()
    event.accept.assert_called_once_with()


def test_shell_shutdown_failure_runs_media_fallback_and_later_cleanup() -> None:
    primary = KeyboardInterrupt("shell shutdown interrupted")
    window = _window()
    window.app_shell.shutdown.side_effect = primary
    event = Mock()

    with (
        patch(
            "app.ui.main_window_shutdown.window_state_persistence.capture_window_state",
            return_value={},
        ),
        patch(
            "app.ui.main_window_shutdown.window_state_persistence.start_window_state_persistence"
        ),
    ):
        failure = shutdown_main_window(window, event)

    assert failure == ("app_shell.shutdown", primary)
    window.cleanup_media.assert_called_once_with()
    window.event_bus.unsubscribe.assert_called_once_with(
        "app_state.changed",
        window._app_state_handler,
    )
    window.event_bus.shutdown.assert_called_once_with()
    event.accept.assert_called_once_with()


def test_shutdown_cancels_live_update_download_without_joining() -> None:
    window = _window()
    window._update_download_thread.is_alive.return_value = True
    event = Mock()

    with (
        patch(
            "app.ui.main_window_shutdown.window_state_persistence.capture_window_state",
            return_value={},
        ),
        patch(
            "app.ui.main_window_shutdown.window_state_persistence.start_window_state_persistence"
        ),
    ):
        failure = shutdown_main_window(window, event)

    assert failure is None
    window._update_download_cancel_event.set.assert_called_once_with()
    window._update_download_thread.join.assert_not_called()
    event.accept.assert_called_once_with()


def test_inactive_update_thread_does_not_resolve_join_attribute() -> None:
    class InactiveWorker:
        def is_alive(self) -> bool:
            return False

        def __getattribute__(self, name: str):
            if name == "join":
                raise KeyboardInterrupt("join lookup must not run")
            return object.__getattribute__(self, name)

    window = _window()
    window._update_download_thread = InactiveWorker()
    event = Mock()

    with (
        patch(
            "app.ui.main_window_shutdown.window_state_persistence.capture_window_state",
            return_value={},
        ),
        patch(
            "app.ui.main_window_shutdown.window_state_persistence.start_window_state_persistence"
        ),
    ):
        failure = shutdown_main_window(window, event)

    assert failure is None
    event.accept.assert_called_once_with()
