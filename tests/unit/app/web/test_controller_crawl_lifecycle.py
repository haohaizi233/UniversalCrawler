from unittest.mock import Mock

import pytest

from app.spiders.base import BaseSpider
from app.web.controller import WebController


class _ControlledSpider(BaseSpider):
    """Keep thread exit separate from stop(), as with a blocked browser call."""

    def __init__(self, name):
        super().__init__(keyword=name, config={})
        self.thread_running = True

    def isRunning(self):
        return self.thread_running

    def run(self):
        pass


@pytest.fixture
def lifecycle(monkeypatch):
    monkeypatch.setattr("app.web.controller.FrontendStateService", Mock())
    monkeypatch.setattr("app.web.controller.debug_logger", Mock())
    controller = WebController(None, Mock())
    callbacks = []
    controller.bridge = Mock()
    controller.bridge.call_later.side_effect = lambda delay, callback: callbacks.append(callback)
    return controller, callbacks


def _bind(controller, name):
    spider = _ControlledSpider(name)
    controller.current_spider = spider
    controller._bind_spider_signals(spider)
    return spider


def _finish(spider):
    spider.thread_running = False
    spider.sig_finished.emit()


def test_old_stop_poll_does_not_mark_new_crawl_stopped(lifecycle):
    controller, callbacks = lifecycle
    old = _bind(controller, "old")
    controller.stop_crawl()
    old_poll = callbacks.pop()
    _finish(old)
    new = _bind(controller, "new")
    controller._pending_selection_strategy = object()
    strategy = controller._pending_selection_strategy
    controller.bridge.emit.reset_mock()

    old_poll()

    assert controller.current_spider is new
    assert controller._pending_selection_strategy is strategy
    assert not controller.bridge.emit.call_args_list
    new.sig_log.emit("new task still connected")
    assert controller.bridge.emit.call_args.args[1]["message"] == "new task still connected"


def test_old_stop_poll_does_not_advance_new_stop_timeout(lifecycle):
    controller, callbacks = lifecycle
    old = _bind(controller, "old")
    controller.stop_crawl()
    old_poll = callbacks.pop()
    _finish(old)
    new = _bind(controller, "new")
    controller.stop_crawl()
    assert controller._stop_wait_spider is new
    before = controller._stop_wait_count
    pending = len(callbacks)

    old_poll()

    assert controller._stop_wait_count == before
    assert len(callbacks) == pending
    assert controller._stop_wait_spider is new


def test_already_dispatched_old_finish_callback_does_not_finish_new_crawl(lifecycle):
    controller, _ = lifecycle
    old = _bind(controller, "old")
    delayed_finish = controller._spider_signal_handlers["on_finished"]
    _finish(old)
    new = _bind(controller, "new")
    controller.bridge.emit.reset_mock()

    delayed_finish()

    assert controller.current_spider is new
    assert not controller.bridge.emit.call_args_list


def test_stale_stop_completion_cannot_disconnect_new_spider(lifecycle):
    controller, _ = lifecycle
    old = _bind(controller, "old")
    controller.stop_crawl()
    _finish(old)
    new = _bind(controller, "new")
    handlers = controller._spider_signal_handlers
    controller.bridge.emit.reset_mock()

    controller._finish_stop(old, forced=True)

    assert controller.current_spider is new
    assert controller._spider_signal_handlers is handlers
    assert not controller.bridge.emit.call_args_list
    _finish(new)
    assert controller.current_spider is None


def test_normal_stop_poll_cleans_finished_spider(lifecycle):
    controller, callbacks = lifecycle
    spider = _bind(controller, "current")
    controller.stop_crawl()
    spider.thread_running = False
    controller.bridge.emit.reset_mock()

    callbacks.pop()()

    assert controller.current_spider is None
    assert controller._stop_wait_spider is None
    assert controller._stop_wait_count == 0
    controller.bridge.emit.assert_any_call("crawl_state", {"is_running": False})


def test_finished_signal_cancels_its_stop_wait(lifecycle):
    controller, _ = lifecycle
    spider = _bind(controller, "current")
    controller.stop_crawl()

    _finish(spider)

    assert controller.current_spider is None
    assert controller._stop_wait_spider is None
    assert controller._stop_wait_count == 0


def test_forced_stop_disconnects_late_finished_signal(lifecycle):
    controller, callbacks = lifecycle
    spider = _bind(controller, "blocked")
    controller.stop_crawl()
    controller._stop_wait_count = 299
    callbacks.pop()()
    assert controller.current_spider is None
    assert controller._stop_wait_spider is None
    controller.bridge.emit.reset_mock()

    _finish(spider)

    assert not controller.bridge.emit.call_args_list


def test_synchronous_stop_finish_does_not_leave_a_pending_wait(lifecycle):
    controller, callbacks = lifecycle
    spider = _bind(controller, "current")
    original_stop = spider.stop

    def stop_and_finish():
        original_stop()
        _finish(spider)

    spider.stop = stop_and_finish
    controller.stop_crawl()

    assert controller.current_spider is None
    assert controller._stop_wait_spider is None
    assert not callbacks


def test_stop_after_shutdown_does_not_reuse_previous_waiter(lifecycle):
    controller, _ = lifecycle
    old = _bind(controller, "old")
    controller.stop_crawl()
    old.thread_running = False
    controller.shutdown()
    new = _bind(controller, "new")

    controller.stop_crawl()

    assert new.interrupt_requested
    assert controller._stop_wait_spider is new
