import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.controllers.application_lifecycle_mixin import ApplicationLifecycleMixin

class _DummyLifecycleController(ApplicationLifecycleMixin):
    def __init__(self):
        self.host = Mock()
        self.current_spider = None
        self.dl_manager = Mock()
        self.app = Mock()

    def _host(self):
        return self.host

class ApplicationLifecycleMixinTests(unittest.TestCase):
    def test_stop_active_spider_ignores_missing_or_idle_spider(self):
        controller = _DummyLifecycleController()

        controller._stop_active_spider()

        controller.current_spider = Mock()
        controller.current_spider.isRunning.return_value = False
        controller._stop_active_spider()

        controller.current_spider.stop.assert_not_called()
        controller.current_spider.wait.assert_not_called()

    def test_stop_active_spider_stops_running_spider(self):
        controller = _DummyLifecycleController()
        controller.current_spider = Mock()
        controller.current_spider.isRunning.return_value = True

        controller._stop_active_spider()

        controller.current_spider.stop.assert_called_once()
        controller.current_spider.wait.assert_called_once_with(2000)

    def test_shutdown_cleans_media_spider_and_downloads(self):
        controller = _DummyLifecycleController()
        controller._stop_active_spider = Mock()

        fake_logger = Mock()
        with patch("app.controllers.application_lifecycle_mixin.debug_logger", fake_logger):
            controller.shutdown()

        fake_logger.log.assert_called_once()
        controller.host.cleanup_media.assert_called_once()
        controller._stop_active_spider.assert_called_once()
        controller.dl_manager.stop_all.assert_called_once()

    def test_shutdown_releases_frontend_state_and_cache_resources(self):
        controller = _DummyLifecycleController()
        controller._stop_active_spider = Mock()
        controller.frontend_state_service = Mock()
        controller.app_state = Mock()
        controller.cache_service = Mock()

        fake_logger = Mock()
        with patch("app.controllers.application_lifecycle_mixin.debug_logger", fake_logger):
            controller.shutdown()

        controller.frontend_state_service.destroy.assert_called_once()
        controller.app_state.shutdown.assert_called_once()
        controller.cache_service.close.assert_called_once()
        controller.host.cleanup_media.assert_called_once()

    def test_shutdown_control_flow_failure_cannot_skip_later_cleanup(self):
        controller = _DummyLifecycleController()
        primary = KeyboardInterrupt("frontend shutdown interrupted")
        secondary = GeneratorExit("app state shutdown interrupted")
        controller.frontend_state_service = Mock()
        controller.frontend_state_service.destroy.side_effect = primary
        controller.app_state = Mock()
        controller.app_state.shutdown.side_effect = secondary
        controller.cache_service = Mock()
        controller.event_bus = Mock()
        controller._stop_active_spider = Mock()
        controller.stop_media_directory_watch = Mock()
        controller._short_task_runner = Mock()

        fake_logger = Mock()
        with (
            patch("app.controllers.application_lifecycle_mixin.debug_logger", fake_logger),
            self.assertRaises(KeyboardInterrupt) as caught,
        ):
            controller.shutdown()

        self.assertIs(caught.exception, primary)
        controller.frontend_state_service.destroy.assert_called_once_with()
        controller.app_state.shutdown.assert_called_once_with()
        controller.cache_service.close.assert_called_once_with()
        controller.event_bus.shutdown.assert_called_once_with()
        controller.host.cleanup_media.assert_called_once_with()
        controller._stop_active_spider.assert_called_once_with()
        controller.dl_manager.stop_all.assert_called_once_with()

    def test_shutdown_preserves_exact_first_exception(self):
        controller = _DummyLifecycleController()
        primary = RuntimeError("download shutdown failed")
        secondary = ValueError("frontend shutdown failed")
        controller.dl_manager.stop_all.side_effect = primary
        controller.frontend_state_service = Mock()
        controller.frontend_state_service.destroy.side_effect = secondary
        controller.app_state = Mock()
        controller.cache_service = Mock()
        controller.event_bus = Mock()
        controller._stop_active_spider = Mock()

        with (
            patch("app.controllers.application_lifecycle_mixin.debug_logger", Mock()),
            self.assertRaises(RuntimeError) as caught,
        ):
            controller.shutdown()

        self.assertIs(caught.exception, primary)
        controller.frontend_state_service.destroy.assert_called_once_with()
        controller.app_state.shutdown.assert_called_once_with()
        controller.cache_service.close.assert_called_once_with()
        controller.event_bus.shutdown.assert_called_once_with()

    def test_shutdown_stop_control_flow_failure_attempts_cleanup_and_retries_only_stop(self):
        controller = _DummyLifecycleController()
        primary = KeyboardInterrupt("download shutdown interrupted")
        controller.dl_manager.stop_all.side_effect = [primary, True]
        controller.frontend_state_service = Mock()
        controller.app_state = Mock()
        controller.cache_service = Mock()
        controller.event_bus = Mock()
        controller._stop_active_spider = Mock()

        with patch("app.controllers.application_lifecycle_mixin.debug_logger", Mock()):
            with self.assertRaises(KeyboardInterrupt) as caught:
                controller.shutdown()
            completed = controller.shutdown()

        self.assertIs(caught.exception, primary)
        self.assertIs(completed, True)
        self.assertEqual(controller.dl_manager.request_stop.call_count, 1)
        self.assertEqual(controller.dl_manager.stop_all.call_count, 2)
        controller.frontend_state_service.destroy.assert_called_once_with()
        controller.app_state.shutdown.assert_called_once_with()
        controller.cache_service.close.assert_called_once_with()
        controller.event_bus.shutdown.assert_called_once_with()
        controller.host.cleanup_media.assert_called_once_with()
        controller._stop_active_spider.assert_called_once_with()

    def test_shutdown_stops_downloads_before_dependent_services(self):
        controller = _DummyLifecycleController()
        order = []
        controller.dl_manager.request_stop.side_effect = lambda: order.append("request_stop")
        controller.dl_manager.stop_all.side_effect = lambda: order.append("stop_all") or True
        controller.frontend_state_service = Mock()
        controller.frontend_state_service.destroy.side_effect = lambda: order.append("frontend") or True
        controller.app_state = Mock()
        controller.app_state.shutdown.side_effect = lambda: order.append("app_state")
        controller.cache_service = Mock()
        controller.cache_service.close.side_effect = lambda: order.append("cache")
        controller.event_bus = Mock()
        controller.event_bus.shutdown.side_effect = lambda: order.append("event_bus")
        controller.host.cleanup_media.side_effect = lambda: order.append("media")
        controller._stop_active_spider = Mock(side_effect=lambda: order.append("spider"))

        with patch("app.controllers.application_lifecycle_mixin.debug_logger", Mock()):
            drained = controller.shutdown()

        self.assertIs(drained, True)
        self.assertEqual(order[:2], ["request_stop", "stop_all"])
        for dependent in ("frontend", "app_state", "cache", "event_bus", "media", "spider"):
            self.assertGreater(order.index(dependent), order.index("stop_all"))

    def test_shutdown_timeout_hands_same_stop_call_to_unique_non_daemon_guardian(self):
        controller = _DummyLifecycleController()
        entered = threading.Event()
        release = threading.Event()
        cleanup_finished = threading.Event()
        stop_threads = []

        def stop_all():
            stop_threads.append(threading.current_thread())
            entered.set()
            release.wait(2.0)
            return True

        controller.dl_manager.stop_all.side_effect = stop_all
        controller.frontend_state_service = Mock()
        controller.frontend_state_service.destroy.side_effect = lambda: cleanup_finished.set() or True
        controller.app_state = Mock()
        controller.cache_service = Mock()
        controller.event_bus = Mock()
        controller._stop_active_spider = Mock()

        with (
            patch("app.controllers.application_lifecycle_mixin.debug_logger", Mock()),
            patch("app.controllers.application_lifecycle_mixin.DOWNLOAD_STOP_WAIT_SECONDS", 0.02, create=True),
        ):
            first = controller.shutdown()
            second = controller.shutdown()

        self.assertTrue(entered.is_set())
        self.assertIs(first, False)
        self.assertIs(second, False)
        self.assertEqual(controller.dl_manager.stop_all.call_count, 1)
        self.assertEqual(len(stop_threads), 1)
        self.assertFalse(stop_threads[0].daemon)
        controller.frontend_state_service.destroy.assert_not_called()

        release.set()
        self.assertTrue(cleanup_finished.wait(2.0))
        controller.frontend_state_service.destroy.assert_called_once_with()

    def test_shutdown_timeout_keeps_qt_affine_cleanup_on_calling_thread(self):
        controller = _DummyLifecycleController()
        caller_thread_id = threading.get_ident()
        release = threading.Event()
        backend_finished = threading.Event()
        callback_threads = {}

        def record(name, result=None):
            def callback(*_args, **_kwargs):
                callback_threads[name] = threading.get_ident()
                return result

            return callback

        controller.dl_manager.request_stop.side_effect = record("request_stop")
        controller.dl_manager.stop_all.side_effect = lambda: release.wait(2.0) or True
        for signal_name in ("task_started", "task_progress", "task_finished", "task_error"):
            setattr(
                controller.dl_manager,
                signal_name,
                SimpleNamespace(disconnect=record(f"signal.{signal_name}")),
            )
        controller._media_release_timer = SimpleNamespace(
            stop=record("timer.stop"),
            timeout=SimpleNamespace(disconnect=record("timer.disconnect")),
        )
        controller.window = SimpleNamespace(
            _ui_update_scheduler=SimpleNamespace(stop=record("scheduler.stop"))
        )
        controller.stop_media_directory_watch = record("media_watch.stop")
        controller._short_task_runner = SimpleNamespace(cancel_all=record("short_tasks.stop"))
        controller.current_spider = SimpleNamespace(
            isRunning=lambda: True,
            stop=record("spider.stop"),
            wait=record("spider.wait"),
        )
        controller.host.cleanup_media.side_effect = record("media.cleanup")
        controller.frontend_state_service = Mock()
        controller.frontend_state_service.destroy.side_effect = (
            lambda: callback_threads.__setitem__("frontend", threading.get_ident())
            or backend_finished.set()
            or True
        )
        controller.app_state = Mock()
        controller.cache_service = Mock()
        controller.event_bus = Mock()

        try:
            with (
                patch("app.controllers.application_lifecycle_mixin.debug_logger", Mock()),
                patch("app.controllers.application_lifecycle_mixin.DOWNLOAD_STOP_WAIT_SECONDS", 0.02),
            ):
                completed = controller.shutdown()

            self.assertIs(completed, False)
            qt_callbacks = {
                "request_stop",
                "timer.stop",
                "timer.disconnect",
                "scheduler.stop",
                "media_watch.stop",
                "short_tasks.stop",
                "media.cleanup",
                "spider.stop",
                "spider.wait",
                "signal.task_started",
                "signal.task_progress",
                "signal.task_finished",
                "signal.task_error",
            }
            self.assertEqual(set(callback_threads), qt_callbacks)
            self.assertTrue(
                all(callback_threads[name] == caller_thread_id for name in qt_callbacks)
            )
            controller.frontend_state_service.destroy.assert_not_called()
        finally:
            release.set()
            state = getattr(controller, "_application_shutdown_state", None)
            if state is not None:
                state.done.wait(2.0)

        self.assertTrue(backend_finished.is_set())
        self.assertNotEqual(callback_threads["frontend"], caller_thread_id)

    def test_shutdown_waits_for_frontend_guardian_handoff_to_complete(self):
        controller = _DummyLifecycleController()
        controller.frontend_state_service = Mock()
        controller.frontend_state_service.destroy.side_effect = [False, True]
        controller.app_state = Mock()
        controller.cache_service = Mock()
        controller.event_bus = Mock()
        controller._stop_active_spider = Mock()

        with patch("app.controllers.application_lifecycle_mixin.debug_logger", Mock()):
            completed = controller.shutdown()

        self.assertIs(completed, True)
        self.assertEqual(controller.frontend_state_service.destroy.call_count, 2)
        controller.app_state.shutdown.assert_called_once_with()
        controller.cache_service.close.assert_called_once_with()
        controller.event_bus.shutdown.assert_called_once_with()
        controller.host.cleanup_media.assert_called_once_with()

    def test_single_shutdown_retries_only_frontend_false_step(self):
        controller = _DummyLifecycleController()
        controller.frontend_state_service = Mock()
        controller.frontend_state_service.destroy.side_effect = [False, True]
        controller.app_state = Mock()
        controller.cache_service = Mock()
        controller.event_bus = Mock()
        controller._stop_active_spider = Mock()

        with patch("app.controllers.application_lifecycle_mixin.debug_logger", Mock()):
            completed = controller.shutdown()

        self.assertIs(completed, True)
        controller.dl_manager.request_stop.assert_called_once_with()
        controller.dl_manager.stop_all.assert_called_once_with()
        self.assertEqual(controller.frontend_state_service.destroy.call_count, 2)
        controller.app_state.shutdown.assert_called_once_with()
        controller.cache_service.close.assert_called_once_with()
        controller.event_bus.shutdown.assert_called_once_with()
        controller.host.cleanup_media.assert_called_once_with()
        controller._stop_active_spider.assert_called_once_with()

    def test_shutdown_stop_all_false_attempts_dependencies_and_retries_only_stop(self):
        controller = _DummyLifecycleController()
        controller.dl_manager.stop_all.side_effect = [False, True]
        controller.frontend_state_service = Mock()
        controller.app_state = Mock()
        controller.cache_service = Mock()
        controller.event_bus = Mock()
        controller._stop_active_spider = Mock()

        with patch("app.controllers.application_lifecycle_mixin.debug_logger", Mock()):
            first = controller.shutdown()

            controller.frontend_state_service.destroy.assert_called_once_with()
            controller.app_state.shutdown.assert_called_once_with()
            controller.cache_service.close.assert_called_once_with()
            controller.event_bus.shutdown.assert_called_once_with()
            controller.host.cleanup_media.assert_called_once_with()
            controller._stop_active_spider.assert_called_once_with()

            second = controller.shutdown()

        self.assertIs(first, False)
        self.assertIs(second, True)
        self.assertEqual(controller.dl_manager.request_stop.call_count, 1)
        self.assertEqual(controller.dl_manager.stop_all.call_count, 2)
        controller.frontend_state_service.destroy.assert_called_once_with()
        controller.app_state.shutdown.assert_called_once_with()
        controller.cache_service.close.assert_called_once_with()
        controller.event_bus.shutdown.assert_called_once_with()
        controller.host.cleanup_media.assert_called_once_with()
        controller._stop_active_spider.assert_called_once_with()

    def test_shutdown_retries_only_frontend_exception_step(self):
        controller = _DummyLifecycleController()
        primary = RuntimeError("frontend still stopping")
        controller.frontend_state_service = Mock()
        controller.frontend_state_service.destroy.side_effect = [primary, True]
        controller.app_state = Mock()
        controller.cache_service = Mock()
        controller.event_bus = Mock()
        controller._stop_active_spider = Mock()

        with patch("app.controllers.application_lifecycle_mixin.debug_logger", Mock()):
            with self.assertRaises(RuntimeError) as caught:
                controller.shutdown()
            second = controller.shutdown()

        self.assertIs(caught.exception, primary)
        self.assertIs(second, True)
        controller.dl_manager.request_stop.assert_called_once_with()
        controller.dl_manager.stop_all.assert_called_once_with()
        self.assertEqual(controller.frontend_state_service.destroy.call_count, 2)
        controller.app_state.shutdown.assert_called_once_with()
        controller.cache_service.close.assert_called_once_with()
        controller.event_bus.shutdown.assert_called_once_with()

    def test_shutdown_unsubscribes_deferred_domain_event_handlers(self):
        controller = _DummyLifecycleController()
        controller._stop_active_spider = Mock()
        controller.event_bus = Mock()
        controller._spider_domain_event_handler = Mock()
        controller._download_domain_event_handler = Mock()

        fake_logger = Mock()
        with patch("app.controllers.application_lifecycle_mixin.debug_logger", fake_logger):
            controller.shutdown()

        controller.event_bus.unsubscribe.assert_any_call(
            "spider.domain_event",
            controller._spider_domain_event_handler,
        )
        controller.event_bus.unsubscribe.assert_any_call(
            "download.domain_event",
            controller._download_domain_event_handler,
        )

    def test_run_exits_with_app_exec_code(self):
        controller = _DummyLifecycleController()
        controller.app.exec.return_value = 7

        with patch("app.controllers.application_lifecycle_mixin.sys.exit") as exit_mock:
            controller.run()

        controller.app.exec.assert_called_once()
        exit_mock.assert_called_once_with(7)

if __name__ == "__main__":
    unittest.main()
