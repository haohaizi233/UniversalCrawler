import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.controllers.crawl_controller_mixin import CrawlControllerMixin
from app.controllers.desktop_host import SelectionDialogOpenError
from app.core.events import (
    build_crawl_state_event,
    build_item_found_event,
    build_items_found_event,
    build_selection_required_event,
)
from app.core.state import CrawlStatus
from app.models import VideoItem
from shared.controller_session import ControllerSessionMixin

class _DummyCrawlController(CrawlControllerMixin, ControllerSessionMixin):
    DOWNLOAD_LOG_COMPONENT = "DummyCrawlController"
    DOWNLOAD_FINISHED_STATUS_CODE = "DUMMY_OK"
    DOWNLOAD_ERROR_STATUS_CODE = "DUMMY_ERR"
    DOWNLOAD_FINISHED_MESSAGE = "dummy finished"
    DOWNLOAD_ERROR_MESSAGE = "dummy error"

    def __init__(self):
        self.host = Mock()
        self._spider_bridge = Mock()
        self.spider_session = Mock()
        self.dl_manager = Mock()
        self.videos = {}
        self.current_spider = None

    def _host(self):
        return self.host

    @staticmethod
    def _event_payload(event):
        return event.to_payload()

    @staticmethod
    def _item_details(item: VideoItem) -> dict:
        return {"title": item.title}

    @staticmethod
    def _prepare_pending_item(item: VideoItem) -> VideoItem:
        item.status = "⏳ 等待中"
        item.progress = 0
        return item

    def _store_video_item(self, item: VideoItem, *, publish: bool = True) -> None:
        del publish
        self.videos[item.id] = item

class CrawlControllerMixinTests(unittest.TestCase):
    def test_create_spider_reports_unknown_source(self):
        controller = _DummyCrawlController()
        controller.spider_session.create_spider.side_effect = ValueError("unknown")

        plugin, spider = controller._create_spider("unknown", "kw", {})

        self.assertIsNone(plugin)
        self.assertIsNone(spider)
        controller.host.notify_unknown_source.assert_called_once()

    def test_bind_spider_signals_uses_event_bridge_callbacks(self):
        controller = _DummyCrawlController()
        spider = Mock()

        controller._bind_spider_signals(spider)

        bindings = controller.spider_session.bind_spider.call_args.args[1]
        self.assertIs(bindings.on_log.__self__, controller)
        self.assertIs(bindings.on_log.__func__, controller._emit_spider_log_event.__func__)
        self.assertIs(bindings.on_item_found.__self__, controller)
        self.assertIs(bindings.on_item_found.__func__, controller._emit_spider_item_found_event.__func__)
        self.assertIs(bindings.on_items_found.__self__, controller)
        self.assertIs(bindings.on_items_found.__func__, controller._emit_spider_items_found_event.__func__)
        self.assertIs(bindings.on_select_tasks.__self__, controller)
        self.assertIs(bindings.on_select_tasks.__func__, controller._emit_spider_selection_event.__func__)
        self.assertIs(bindings.on_finished.__self__, controller)
        self.assertIs(bindings.on_finished.__func__, controller._emit_spider_finished_event.__func__)

    def test_on_start_crawl_begins_host_and_starts_spider(self):
        controller = _DummyCrawlController()
        plugin = SimpleNamespace(name="抖音")
        spider = Mock()
        controller._create_spider = Mock(return_value=(plugin, spider))
        controller._log_crawl_start = Mock()

        controller.on_start_crawl("关键词", "douyin", {"max_items": 20})

        controller.host.begin_crawl.assert_called_once_with("抖音")
        controller.spider_session.activate_spider.assert_called_once()
        activated_spider, bindings = controller.spider_session.activate_spider.call_args.args
        self.assertIs(activated_spider, spider)
        self.assertTrue(callable(bindings.on_log))
        self.assertTrue(callable(bindings.on_item_found))
        self.assertTrue(callable(bindings.on_items_found))
        self.assertTrue(callable(bindings.on_select_tasks))
        self.assertTrue(callable(bindings.on_finished))
        bindings.on_log("started")
        emitted = controller._spider_bridge.sig_event.emit.call_args.args[0]
        self.assertEqual(emitted.payload["session_id"], controller._active_spider_session_id)
        self.assertIs(controller.current_spider, spider)

    def test_on_start_crawl_rejects_current_spider_before_thread_start(self):
        controller = _DummyCrawlController()
        controller.current_spider = Mock(is_running=True)
        controller._create_spider = Mock()

        controller.on_start_crawl("关键词", "douyin", {"max_items": 20})

        controller.host.notify_crawl_already_running.assert_called_once()
        controller._create_spider.assert_not_called()

    def test_on_stop_crawl_stops_session_and_notifies_host(self):
        controller = _DummyCrawlController()
        controller.current_spider = Mock()

        with patch("app.controllers.crawl_controller_mixin.debug_logger", Mock()):
            controller.on_stop_crawl()

        controller.spider_session.stop_session.assert_called_once_with(
            controller.current_spider,
            getattr(controller, "_active_spider_bindings", None),
        )
        controller.host.notify_crawl_stop_requested.assert_called_once()

    def test_on_stop_crawl_keeps_bindings_until_spider_finished(self):
        controller = _DummyCrawlController()
        bindings = Mock()
        controller._active_spider_bindings = bindings
        controller.current_spider = Mock()

        with patch("app.controllers.crawl_controller_mixin.debug_logger", Mock()):
            controller.on_stop_crawl()

        self.assertIs(controller._active_spider_bindings, bindings)
        controller.host.finish_crawl.assert_not_called()

        controller._on_spider_finished()

        controller.host.finish_crawl.assert_called_once()
        self.assertIsNone(controller._active_spider_bindings)
        self.assertIsNone(controller.current_spider)

    def test_selection_cancel_requests_spider_stop_before_resume(self):
        controller = _DummyCrawlController()
        spider = Mock()
        spider.interrupt_requested = False
        bindings = Mock()
        controller.current_spider = spider
        controller._active_spider_bindings = bindings
        controller.host.show_selection_dialog.return_value = None

        with patch("app.controllers.crawl_controller_mixin.debug_logger", Mock()):
            controller._on_spider_select_tasks([{"title": "A"}])

        controller.spider_session.stop_session.assert_called_once_with(spider, bindings)
        controller.host.notify_crawl_stop_requested.assert_called_once()
        spider.resume_from_ui.assert_called_once_with(None)

    def test_selection_cancel_does_not_duplicate_existing_stop_request(self):
        controller = _DummyCrawlController()
        spider = Mock()
        spider.interrupt_requested = True
        controller.current_spider = spider
        controller.host.show_selection_dialog.return_value = None

        with patch("app.controllers.crawl_controller_mixin.debug_logger", Mock()):
            controller._on_spider_select_tasks([{"title": "A"}])

        controller.spider_session.stop_session.assert_not_called()
        controller.host.notify_crawl_stop_requested.assert_not_called()
        spider.resume_from_ui.assert_called_once_with(None)

    def test_on_spider_item_found_prepares_item_and_enqueues_download(self):
        controller = _DummyCrawlController()
        controller.host.current_save_dir = "downloads"
        item = VideoItem(url="https://example.com/video.mp4", title="demo", source="douyin")

        with patch("app.controllers.crawl_controller_mixin.debug_logger", Mock()):
            controller._on_spider_item_found(item)

        self.assertEqual(item.status, "⏳ 等待中")
        self.assertEqual(item.progress, 0)
        self.assertIs(controller.videos[item.id], item)
        controller.host.add_video_row.assert_called_once_with(item)
        controller.dl_manager.add_task.assert_called_once_with(item, "downloads")

    def test_on_spider_items_found_batches_ui_and_download_queue(self):
        controller = _DummyCrawlController()
        controller.host.current_save_dir = "downloads"
        first = VideoItem(url="https://example.com/1.mp4", title="one", source="bilibili")
        second = VideoItem(url="https://example.com/2.mp4", title="two", source="bilibili")

        with patch("app.controllers.crawl_controller_mixin.debug_logger", Mock()):
            controller._on_spider_items_found([first, second])

        self.assertEqual(first.status, "⏳ 等待中")
        self.assertEqual(second.progress, 0)
        self.assertIs(controller.videos[first.id], first)
        self.assertIs(controller.videos[second.id], second)
        controller.host.add_video_rows.assert_called_once_with([first, second])
        controller.host.add_video_row.assert_not_called()
        controller.dl_manager.add_tasks.assert_called_once_with([first, second], "downloads")
        controller.dl_manager.add_task.assert_not_called()

    def test_dispatch_spider_event_routes_selection_and_finish(self):
        controller = _DummyCrawlController()
        item = VideoItem(url="https://example.com/video.mp4", title="demo", source="douyin")
        controller._on_spider_item_found = Mock()
        controller._on_spider_items_found = Mock()
        controller._schedule_spider_selection = Mock()
        controller._on_spider_finished = Mock()

        controller._dispatch_spider_event(build_item_found_event(item))
        controller._dispatch_spider_event(build_items_found_event([item]))
        controller._dispatch_spider_event(build_selection_required_event([item]))
        controller._dispatch_spider_event(build_crawl_state_event(CrawlStatus.FINISHED))
        controller._dispatch_spider_event(build_crawl_state_event(CrawlStatus.RUNNING, is_running=True))

        controller._on_spider_item_found.assert_called_once_with(item)
        controller._on_spider_items_found.assert_called_once_with([item])
        controller._schedule_spider_selection.assert_called_once_with([item])
        controller._on_spider_finished.assert_called_once()

    def test_selection_event_is_deferred_through_host_ui_queue(self):
        controller = _DummyCrawlController()
        controller._on_spider_select_tasks = Mock()
        callbacks = []
        controller.host._queue_on_ui.side_effect = callbacks.append

        controller._dispatch_spider_event(build_selection_required_event([{"title": "A"}]))

        controller._on_spider_select_tasks.assert_not_called()
        controller.host._queue_on_ui.assert_called_once()
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        controller._on_spider_select_tasks.assert_called_once_with([{"title": "A"}])

    def test_stale_spider_finish_event_does_not_finish_new_session(self):
        controller = _DummyCrawlController()
        controller._active_spider_session_id = "new-session"
        controller._on_spider_finished = Mock()
        event = build_crawl_state_event(CrawlStatus.FINISHED)
        event.payload["session_id"] = "old-session"

        controller._dispatch_spider_event(event)

        controller._on_spider_finished.assert_not_called()

    def test_deferred_selection_is_ignored_after_session_changes(self):
        controller = _DummyCrawlController()
        controller.current_spider = Mock()
        controller._active_spider_session_id = "old-session"
        callbacks = []
        controller.host._queue_on_ui.side_effect = callbacks.append
        event = build_selection_required_event([{"title": "A"}])
        event.payload["session_id"] = "old-session"

        controller._dispatch_spider_event(event)
        controller._active_spider_session_id = "new-session"
        callbacks[0]()

        controller.host.show_selection_dialog.assert_not_called()
        controller.current_spider.resume_from_ui.assert_not_called()

    def test_stop_invalidates_session_and_drops_later_items(self):
        controller = _DummyCrawlController()
        spider = Mock()
        spider.interrupt_requested = False
        controller.current_spider = spider
        controller._active_spider_session_id = "session-a"
        controller._active_spider_bindings = Mock()
        item = VideoItem(url="https://example.com/video.mp4", title="demo", source="douyin")

        with patch("app.controllers.crawl_controller_mixin.debug_logger", Mock()):
            controller.on_stop_crawl()
            event = build_items_found_event([item])
            event.payload["session_id"] = "session-a"
            controller._dispatch_spider_event(event)
            controller._on_spider_items_found([item])

        self.assertNotEqual(controller._active_spider_session_id, "session-a")
        self.assertIs(controller._stopped_spider, spider)
        controller.dl_manager.add_task.assert_not_called()
        controller.host.add_video_row.assert_not_called()
        self.assertNotIn(item.id, controller.videos)

    def test_stopped_session_finish_still_resets_host(self):
        controller = _DummyCrawlController()
        spider = Mock()
        spider.interrupt_requested = False
        controller.current_spider = spider
        controller._active_spider_session_id = "session-a"
        controller._active_spider_bindings = Mock()

        with patch("app.controllers.crawl_controller_mixin.debug_logger", Mock()):
            controller.on_stop_crawl()
            event = build_crawl_state_event(CrawlStatus.FINISHED)
            event.payload["session_id"] = "session-a"
            controller._dispatch_spider_event(event)

        controller.host.finish_crawl.assert_called_once()
        self.assertIsNone(controller.current_spider)

    def test_selection_before_show_is_closed_when_already_stopped(self):
        controller = _DummyCrawlController()
        spider = Mock()
        spider.interrupt_requested = True
        controller.current_spider = spider
        controller.host.show_selection_dialog.return_value = [0, 1]

        with patch("app.controllers.crawl_controller_mixin.debug_logger", Mock()):
            controller._on_spider_select_tasks([{"title": "A"}])

        controller.host.show_selection_dialog.assert_not_called()
        controller.host.dismiss_selection_dialog.assert_called_once()
        spider.resume_from_ui.assert_called_once_with(None)
        controller.spider_session.stop_session.assert_not_called()

    def test_confirmation_after_stop_is_not_recorded(self):
        controller = _DummyCrawlController()
        spider = Mock()
        spider.interrupt_requested = False
        controller.current_spider = spider

        def show_dialog(_items):
            spider.interrupt_requested = True
            return [1, 2]

        controller.host.show_selection_dialog.side_effect = show_dialog

        with patch("app.controllers.crawl_controller_mixin.debug_logger", Mock()):
            controller._on_spider_select_tasks([{"title": "A"}])

        spider.resume_from_ui.assert_called_once_with(None)

    def test_selection_dialog_open_failure_is_not_user_cancel(self):
        controller = _DummyCrawlController()
        spider = Mock()
        spider.interrupt_requested = False
        controller.current_spider = spider
        controller.host.show_selection_dialog.side_effect = SelectionDialogOpenError("dialog unavailable")
        logger = Mock()

        with patch("app.controllers.crawl_controller_mixin.debug_logger", logger):
            controller._on_spider_select_tasks([{"title": "A"}])

        spider.resume_from_ui.assert_called_once_with(None)
        controller.spider_session.stop_session.assert_called_once()
        actions = [call.kwargs.get("action") for call in logger.log.call_args_list]
        self.assertIn("selection_dialog_open_failed", actions)
        self.assertNotIn("selection_cancel_stop_crawl", actions)

    def test_selection_dialog_open_failure_reason_is_not_user_cancel(self):
        controller = _DummyCrawlController()
        spider = Mock()
        spider.interrupt_requested = False
        controller.current_spider = spider
        controller.host.show_selection_dialog.return_value = SimpleNamespace(
            reason="open_failed",
            error="cannot create dialog",
            selected=None,
        )
        logger = Mock()

        with patch("app.controllers.crawl_controller_mixin.debug_logger", logger):
            controller._on_spider_select_tasks([{"title": "A"}])

        spider.resume_from_ui.assert_called_once_with(None)
        actions = [call.kwargs.get("action") for call in logger.log.call_args_list]
        self.assertIn("selection_dialog_open_failed", actions)
        self.assertNotIn("selection_cancel_stop_crawl", actions)

    def test_enqueue_happens_before_ui_and_failure_retracts_row(self):
        controller = _DummyCrawlController()
        controller.host.current_save_dir = "downloads"
        item = VideoItem(url="https://example.com/video.mp4", title="demo", source="douyin")
        order = []
        controller.dl_manager.add_task.side_effect = lambda *_args, **_kwargs: order.append("enqueue") or True
        controller.host.add_video_row.side_effect = lambda *_args, **_kwargs: order.append("ui")

        with patch("app.controllers.crawl_controller_mixin.debug_logger", Mock()):
            controller._on_spider_item_found(item)

        self.assertEqual(order, ["enqueue", "ui"])
        self.assertIn(item.id, controller.videos)

        failed = VideoItem(url="https://example.com/fail.mp4", title="fail", source="douyin")
        controller.dl_manager.add_task.side_effect = RuntimeError("queue closed")

        with patch("app.controllers.crawl_controller_mixin.debug_logger", Mock()):
            controller._on_spider_item_found(failed)

        self.assertNotIn(failed.id, controller.videos)
        controller.host.remove_video_row.assert_called_with(-1, failed.id)
        controller.host.append_log.assert_any_call("❌ 加入下载队列失败: queue closed", level="ERROR")

    def test_enqueue_success_publishes_upsert_only_after_add_task(self):
        controller = _DummyCrawlController()
        timeline: list[str] = []
        controller.app_state = SimpleNamespace(
            _publish_change=lambda topic, _payload: timeline.append(topic),
        )
        controller.host.current_save_dir = "downloads"
        item = VideoItem(url="https://example.com/video.mp4", title="demo", source="douyin")
        controller.dl_manager.add_task.side_effect = lambda *_args, **_kwargs: timeline.append("enqueue") or True
        controller.host.add_video_row.side_effect = lambda *_args, **_kwargs: timeline.append("ui")

        with patch("app.controllers.crawl_controller_mixin.debug_logger", Mock()):
            controller._on_spider_item_found(item)

        self.assertEqual(timeline, ["enqueue", "videos.upsert", "ui"])
        self.assertIn(item.id, controller.videos)

    def test_enqueue_failure_does_not_publish_upsert(self):
        controller = _DummyCrawlController()
        timeline: list[str] = []
        controller.app_state = SimpleNamespace(
            _publish_change=lambda topic, _payload: timeline.append(topic),
        )
        controller.host.current_save_dir = "downloads"
        item = VideoItem(url="https://example.com/fail.mp4", title="fail", source="douyin")
        controller.dl_manager.add_task.side_effect = RuntimeError("queue closed")

        with patch("app.controllers.crawl_controller_mixin.debug_logger", Mock()):
            controller._on_spider_item_found(item)

        self.assertNotIn("videos.upsert", timeline)
        self.assertNotIn(item.id, controller.videos)

    def test_shutdown_rejects_new_crawl_and_enqueue(self):
        controller = _DummyCrawlController()
        controller._shutdown_rejects_new_work = True
        controller._create_spider = Mock()
        item = VideoItem(url="https://example.com/video.mp4", title="demo", source="douyin")

        controller.on_start_crawl("关键词", "douyin", {})
        controller._on_spider_items_found([item])

        controller._create_spider.assert_not_called()
        controller.dl_manager.add_task.assert_not_called()
        controller.host.append_log.assert_any_call("应用正在关闭，已拒绝开始爬取")

    def test_spider_finish_is_not_success_when_run_succeeded_is_false(self):
        controller = _DummyCrawlController()
        spider = Mock()
        spider.run_succeeded = False
        controller.current_spider = spider
        controller._active_spider_bindings = Mock()
        logger = Mock()

        with patch("app.controllers.crawl_controller_mixin.debug_logger", logger):
            controller._on_spider_finished()

        controller.host.finish_crawl.assert_not_called()
        controller.host.report_crawl_finished_unsuccessful.assert_called_once()
        self.assertEqual(logger.log.call_args.kwargs["status_code"], "APP_CRAWL_FINISH_FAILED")
        self.assertIsNone(controller.current_spider)

    def test_spider_finish_stays_successful_without_run_succeeded(self):
        controller = _DummyCrawlController()
        controller.current_spider = SimpleNamespace()
        controller._active_spider_bindings = None

        with patch("app.controllers.crawl_controller_mixin.debug_logger", Mock()):
            controller._on_spider_finished()

        controller.host.finish_crawl.assert_called_once()
        controller.host.report_crawl_finished_unsuccessful.assert_not_called()

if __name__ == "__main__":
    unittest.main()
