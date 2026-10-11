from __future__ import annotations

import uuid
from functools import partial

from app.core.events import (
    DomainEvent,
    DomainEventType,
    build_crawl_state_event,
    build_item_found_event,
    build_items_found_event,
    build_log_event,
    build_selection_required_event,
)
from app.config import cfg
from app.core.media_filter import should_skip_for_video_only
from app.controllers.desktop_host import SelectionDialogOpenError
from app.core.plugin_registry import registry
from app.core.state import CrawlStatus
from app.debug_logger import debug_logger
from app.models import VideoItem
from shared.spider_session_runtime import SpiderSession, SpiderSessionBindings

class CrawlControllerMixin:
    """协调宿主控制器中的爬取会话与状态流转。"""

    def _video_only_mode_enabled(self) -> bool:
        manager_value = getattr(getattr(self, "dl_manager", None), "video_only", None)
        if isinstance(manager_value, bool):
            return manager_value
        return bool(cfg.get("download", "video_only", False))

    def _should_skip_for_video_only(self, item: VideoItem) -> bool:
        return self._video_only_mode_enabled() and should_skip_for_video_only(item)

    def _log_video_only_skip(self, item: VideoItem) -> None:
        title = getattr(item, "title", "") or getattr(item, "url", "")
        self._host().append_log(
            f"Video-only mode skipped non-video resource: {title}",
            trace_id=self._item_trace_id(item),
            source="Downloader",
            level="INFO",
        )
        debug_logger.log(
            component="ApplicationController",
            action="skip_non_video_resource",
            message="Video-only mode skipped a non-video resource",
            status_code="APP_SKIP_VIDEO_ONLY",
            context=self._item_context(item),
            details=self._item_details(item),
            trace_id=self._item_trace_id(item),
        )

    @staticmethod
    def _tag_spider_session(event: DomainEvent, session_id: str | None) -> DomainEvent:
        if session_id:
            event.payload["session_id"] = session_id
        return event

    def _emit_spider_log_event(self, message: str, session_id: str | None = None) -> None:
        trace_id = getattr(getattr(self, "current_spider", None), "ui_trace_id", None)
        source = getattr(getattr(self, "current_spider", None), "source_id", None) or "Spider"
        event = build_log_event(message, trace_id=trace_id, source=source, level="INFO")
        self._spider_bridge.sig_event.emit(self._tag_spider_session(event, session_id))

    def _emit_spider_item_found_event(self, item: VideoItem, session_id: str | None = None) -> None:
        event = build_item_found_event(item)
        self._spider_bridge.sig_event.emit(self._tag_spider_session(event, session_id))

    def _emit_spider_items_found_event(
        self,
        items: list[VideoItem],
        session_id: str | None = None,
    ) -> None:
        event = build_items_found_event(items)
        self._spider_bridge.sig_event.emit(self._tag_spider_session(event, session_id))

    def _emit_spider_selection_event(self, items: list, session_id: str | None = None) -> None:
        event = build_selection_required_event(items)
        self._spider_bridge.sig_event.emit(self._tag_spider_session(event, session_id))

    def _emit_spider_finished_event(self, session_id: str | None = None) -> None:
        event = build_crawl_state_event(CrawlStatus.FINISHED)
        self._spider_bridge.sig_event.emit(self._tag_spider_session(event, session_id))

    def _handle_spider_log_event(self, event: DomainEvent) -> None:
        payload = self._event_payload(event)
        self._host().append_log(
            payload.get("message", ""),
            trace_id=payload.get("trace_id") or event.trace_id,
            source=payload.get("source") or "Spider",
            level=payload.get("level") or "INFO",
        )

    def _handle_spider_item_found_event(self, event: DomainEvent) -> None:
        item = self._event_payload(event).get("item")
        if item is not None:
            self._on_spider_item_found(item)

    def _handle_spider_items_found_event(self, event: DomainEvent) -> None:
        items = self._event_payload(event).get("items")
        if items is not None:
            self._on_spider_items_found(items)

    def _handle_spider_selection_event(self, event: DomainEvent) -> None:
        payload = self._event_payload(event)
        items = payload.get("items")
        if items is not None:
            session_id = payload.get("session_id")
            if session_id is None:
                self._schedule_spider_selection(items)
            else:
                self._schedule_spider_selection(items, session_id)

    def _handle_spider_crawl_state_event(self, event: DomainEvent) -> None:
        if self._event_payload(event).get("status") == CrawlStatus.FINISHED.value:
            self._on_spider_finished()

    def _spider_event_handlers(self) -> dict[DomainEventType, callable]:
        return {
            DomainEventType.LOG: self._handle_spider_log_event,
            DomainEventType.ITEM_FOUND: self._handle_spider_item_found_event,
            DomainEventType.ITEMS_FOUND: self._handle_spider_items_found_event,
            DomainEventType.SELECTION_REQUIRED: self._handle_spider_selection_event,
            DomainEventType.CRAWL_STATE_CHANGED: self._handle_spider_crawl_state_event,
        }

    def _dispatch_spider_event(self, event: DomainEvent) -> None:
        session_id = self._event_payload(event).get("session_id")
        active_session_id = getattr(self, "_active_spider_session_id", None)
        # 旧 worker 可能在新任务启动后才送达收尾事件；会话令牌可防止其改写新任务状态。
        if session_id is not None and session_id != active_session_id:
            if not self._is_finish_for_stopped_session(event, session_id):
                return
        if self._should_drop_stopped_spider_intake(event):
            return
        handler = self._spider_event_handlers().get(event.event_type)
        if handler:
            handler(event)

    def _is_finish_for_stopped_session(self, event: DomainEvent, session_id: str) -> bool:
        if event.event_type != DomainEventType.CRAWL_STATE_CHANGED:
            return False
        if self._event_payload(event).get("status") != CrawlStatus.FINISHED.value:
            return False
        if session_id != getattr(self, "_stopped_spider_session_id", None):
            return False
        stopped = getattr(self, "_stopped_spider", None)
        return stopped is not None and stopped is getattr(self, "current_spider", None)

    def _should_drop_stopped_spider_intake(self, event: DomainEvent) -> bool:
        if event.event_type not in {
            DomainEventType.ITEM_FOUND,
            DomainEventType.ITEMS_FOUND,
            DomainEventType.SELECTION_REQUIRED,
        }:
            return False
        return self._crawl_intake_closed()

    def _crawl_intake_closed(self) -> bool:
        if getattr(self, "_shutdown_rejects_new_work", False):
            return True
        spider = getattr(self, "current_spider", None)
        if spider is not None and getattr(spider, "interrupt_requested", False) is True:
            return True
        stopped = getattr(self, "_stopped_spider", None)
        if stopped is not None and stopped is spider:
            active = getattr(self, "_active_spider_session_id", None)
            stopped_id = getattr(self, "_stopped_spider_session_id", None)
            if active != stopped_id:
                return True
        return False

    def _invalidate_active_spider_session(self) -> None:
        spider = getattr(self, "current_spider", None)
        if spider is None:
            return
        self._stopped_spider = spider
        self._stopped_spider_session_id = getattr(self, "_active_spider_session_id", None)
        self._active_spider_session_id = uuid.uuid4().hex

    def _schedule_spider_selection(self, items: list, session_id: str | None = None) -> None:
        """将模态选择框延后到 EventBus 发布栈之外，避免同步回调重入。"""
        selected_items = list(items)
        if session_id is None:
            callback = partial(self._on_spider_select_tasks, selected_items)
        else:
            callback = partial(self._on_spider_select_tasks, selected_items, session_id)
        self._host()._queue_on_ui(callback)

    def _create_spider(self, source_id: str, keyword: str, config: dict):
        """通过共享会话运行时创建 Spider，并向宿主呈现创建失败。"""
        spider_session = getattr(self, "spider_session", SpiderSession(registry))
        try:
            return spider_session.create_spider(source_id, keyword, config)
        except ValueError:
            self._host().notify_unknown_source()
            return None, None
        except Exception as exc:
            self._host().notify_spider_create_failed(exc)
            debug_logger.log_exception(
                "ApplicationController",
                "create_spider",
                exc,
                context={"source_id": source_id, "keyword": keyword},
            )
            plugin = registry.get_plugin(source_id)
            return plugin, None

    def _build_spider_session_bindings(self, session_id: str | None = None) -> SpiderSessionBindings:
        """为共享 Spider 运行时构造宿主回调。"""
        if session_id is None:
            return SpiderSessionBindings(
                on_log=self._emit_spider_log_event,
                on_item_found=self._emit_spider_item_found_event,
                on_items_found=self._emit_spider_items_found_event,
                on_select_tasks=self._emit_spider_selection_event,
                on_finished=self._emit_spider_finished_event,
            )
        return SpiderSessionBindings(
            on_log=lambda message: self._emit_spider_log_event(message, session_id),
            on_item_found=lambda item: self._emit_spider_item_found_event(item, session_id),
            on_items_found=lambda items: self._emit_spider_items_found_event(items, session_id),
            on_select_tasks=lambda items: self._emit_spider_selection_event(items, session_id),
            on_finished=lambda: self._emit_spider_finished_event(session_id),
        )

    def _bind_spider_signals(self, spider) -> None:
        """经统一宿主事件桥绑定 Spider 生命周期回调。"""
        spider_session = getattr(self, "spider_session", SpiderSession(registry))
        spider_session.bind_spider(spider, self._build_spider_session_bindings())

    def _cleanup_dead_spider(self) -> None:
        spider = getattr(self, "current_spider", None)
        if spider is None or spider.isRunning():
            return
        bindings = getattr(self, "_active_spider_bindings", None)
        if bindings is not None:
            SpiderSession.unbind_spider(spider, bindings)
        self._active_spider_bindings = None
        self._active_spider_session_id = None
        self._host().append_log("⚠️ 上次任务未正常结束，正在清理...")
        self._host().finish_crawl()
        self.current_spider = None

    def on_start_crawl(self, keyword, source_id, config):
        """通过共享 Spider 会话创建、绑定并启动爬取任务。"""
        if getattr(self, "_shutdown_rejects_new_work", False):
            self._host().append_log("应用正在关闭，已拒绝开始爬取")
            return
        self._cleanup_dead_spider()
        if self._has_active_spider():
            self._host().notify_crawl_already_running()
            return
        plugin, spider = self._create_spider(source_id, keyword, config)
        if not plugin or not spider:
            return
        self._host().begin_crawl(plugin.name)
        self._log_crawl_start(plugin.name, keyword, source_id, config)

        try:
            spider.ui_trace_id = debug_logger.new_trace_id(f"{source_id}-crawl")
            spider.source_id = source_id
            self.current_spider = spider
            # 每次启动都换用新令牌，使上一会话的迟到回调在分派边界被丢弃。
            self._active_spider_session_id = uuid.uuid4().hex
            self._active_spider_bindings = self._build_spider_session_bindings(
                self._active_spider_session_id
            )
            spider_session = getattr(self, "spider_session", SpiderSession(registry))
            spider_session.activate_spider(
                self.current_spider,
                self._active_spider_bindings,
            )
        except Exception as exc:
            self._host().fail_crawl_start(exc)
            debug_logger.log_exception(
                "ApplicationController",
                "start_crawl",
                exc,
                context={"source_id": source_id, "keyword": keyword},
            )
            self.current_spider = None
            self._active_spider_session_id = None

    def _log_crawl_start(self, plugin_name: str, keyword: str, source_id: str, config: dict) -> None:
        """记录实际生效的爬取输入，供排障和审计使用。"""
        debug_logger.log(
            component="ApplicationController",
            action="start_crawl",
            message="用户启动爬虫任务",
            status_code="APP_CRAWL_START",
            details={
                "keyword": keyword,
                "source_id": source_id,
                "plugin_name": plugin_name,
                "active_config": self._summarize_active_config(config),
            },
        )

    def _on_spider_item_found(self, item):
        """把单个新资源交给统一的批量接收路径。"""
        self._on_spider_items_found([item])

    def _on_spider_items_found(self, items):
        """接收一批资源，并尽量用一次队列唤醒完成下载入队。"""
        if self._crawl_intake_closed():
            return
        accepted_items: list[VideoItem] = []
        for item in list(items or []):
            if self._should_skip_for_video_only(item):
                self._log_video_only_skip(item)
                continue
            self._prepare_pending_item(item)
            accepted_items.append(item)
        if not accepted_items or self._crawl_intake_closed():
            return
        self._accept_spider_items(accepted_items)

    def _accept_spider_items(self, accepted_items: list[VideoItem]) -> None:
        stored: list[VideoItem] = []
        try:
            self._stage_spider_items_for_enqueue(accepted_items)
            stored.extend(accepted_items)
            self._enqueue_download_items(stored)
        except Exception as exc:
            if not stored:
                stored = list(accepted_items)
            self._retract_spider_items(stored)
            self._report_download_enqueue_failure(stored, exc)
            return
        self._publish_enqueued_spider_items(stored)
        self._present_spider_items(stored)

    def _stage_spider_items_for_enqueue(self, items: list[VideoItem]) -> None:
        store_many = getattr(self, "_store_video_items", None)
        if len(items) > 1 and callable(store_many):
            store_many(items, publish=False)
            return
        for item in items:
            self._store_video_item(item, publish=False)

    def _publish_enqueued_spider_items(self, items: list[VideoItem]) -> None:
        app_state = getattr(self, "app_state", None)
        publish = getattr(app_state, "_publish_change", None) if app_state is not None else None
        if not callable(publish):
            return
        video_ids = [str(item.id) for item in items if getattr(item, "id", None)]
        if not video_ids:
            return
        if len(video_ids) == 1:
            publish("videos.upsert", {"video_id": video_ids[0]})
            return
        publish("videos.upsert_many", {"video_ids": video_ids, "count": len(video_ids)})

    def _enqueue_download_items(self, items: list[VideoItem]) -> None:
        if not items:
            return
        if self._crawl_intake_closed():
            raise RuntimeError("已停止接受新的下载任务")
        save_dir = self._host().current_save_dir
        manager = self.dl_manager
        add_tasks = getattr(manager, "add_tasks", None)
        if len(items) == 1 or not callable(add_tasks):
            for item in items:
                result = manager.add_task(item, save_dir)
                if result is False:
                    raise RuntimeError("加入下载队列失败")
            return
        result = add_tasks(items, save_dir)
        if result is False or (isinstance(result, int) and result <= 0):
            raise RuntimeError("加入下载队列失败")

    def _present_spider_items(self, items: list[VideoItem]) -> None:
        if len(items) == 1:
            self._host().add_video_row(items[0])
        else:
            add_rows = getattr(self._host(), "add_video_rows", None)
            if callable(add_rows):
                add_rows(items)
            else:
                for item in items:
                    self._host().add_video_row(item)
        for item in items:
            self._log_spider_item_found(item)

    def _retract_spider_items(self, items: list[VideoItem]) -> None:
        for item in items:
            video_id = getattr(item, "id", None)
            remover = getattr(self, "_remove_video_item", None)
            if callable(remover) and video_id:
                remover(video_id)
            else:
                videos = getattr(self, "videos", None)
                if isinstance(videos, dict) and video_id:
                    videos.pop(video_id, None)
            remove_row = getattr(self._host(), "remove_video_row", None)
            if callable(remove_row) and video_id:
                try:
                    remove_row(-1, video_id)
                except TypeError:
                    remove_row(-1)

    def _report_download_enqueue_failure(self, items: list[VideoItem], exc: Exception) -> None:
        message = f"❌ 加入下载队列失败: {exc}"
        append_log = getattr(self._host(), "append_log", None)
        if callable(append_log):
            try:
                append_log(message, level="ERROR")
            except TypeError:
                append_log(message)
        debug_logger.log_exception(
            "ApplicationController",
            "enqueue_download",
            exc,
            details={"item_count": len(items)},
        )

    def _log_spider_item_found(self, item: VideoItem) -> None:
        debug_logger.log(
            component="ApplicationController",
            action="item_found",
            message="爬虫发现可下载资源",
            status_code="APP_ITEM_FOUND",
            context=self._item_context(item),
            details=self._item_details(item),
            trace_id=self._item_trace_id(item),
        )

    def _dismiss_selection_dialog(self) -> None:
        dismiss = getattr(self._host(), "dismiss_selection_dialog", None)
        if callable(dismiss):
            dismiss()

    def _selection_was_stopped(self, spider, session_id: str | None) -> bool:
        if getattr(self, "_shutdown_rejects_new_work", False):
            return True
        if spider is None or spider is not getattr(self, "current_spider", None):
            return True
        if getattr(spider, "interrupt_requested", False) is True:
            return True
        return session_id is not None and session_id != getattr(self, "_active_spider_session_id", None)

    def _resume_closed_selection(self, spider, session_id: str | None) -> None:
        """关闭选择框，并用 None 唤醒等待点，不把确认结果记成成功。"""
        self._dismiss_selection_dialog()
        target = None
        stopped = getattr(self, "_stopped_spider", None)
        if stopped is not None and session_id == getattr(self, "_stopped_spider_session_id", None):
            target = stopped
        elif session_id is None and spider is getattr(self, "current_spider", None):
            target = spider
        elif (
            spider is getattr(self, "current_spider", None)
            and getattr(spider, "interrupt_requested", False) is True
            and (session_id is None or session_id == getattr(self, "_active_spider_session_id", None))
        ):
            target = spider
        resume = getattr(target, "resume_from_ui", None)
        if callable(resume):
            resume(None)

    @staticmethod
    def _selection_result_is_open_failure(selected) -> bool:
        return getattr(selected, "reason", None) == "open_failed"

    def _on_spider_select_tasks(self, items, session_id: str | None = None):
        """把宿主选择结果回填给暂停中的 Spider。"""
        spider = getattr(self, "current_spider", None)
        if spider is None and getattr(self, "_stopped_spider", None) is None:
            return
        if self._selection_was_stopped(spider, session_id):
            self._resume_closed_selection(spider, session_id)
            return
        try:
            selected = self._host().show_selection_dialog(items)
        except SelectionDialogOpenError as exc:
            self._fail_selection_dialog_open(spider, exc)
            return
        if self._selection_result_is_open_failure(selected):
            self._fail_selection_dialog_open(spider, getattr(selected, "error", None) or "任务选择框打开失败")
            return
        # 模态框期间事件循环仍可能完成停止或启动新任务，确认结果不能记到失效会话上。
        if self._selection_was_stopped(spider, session_id) or spider is not self.current_spider:
            self._resume_closed_selection(spider, session_id)
            return
        if selected is None or getattr(selected, "reason", None) in {"cancelled", "cancel"}:
            self._stop_spider_after_selection_cancel(spider)
            spider.resume_from_ui(None)
            return
        confirmed = selected.selected if getattr(selected, "reason", None) else selected
        spider.resume_from_ui(confirmed)

    def _fail_selection_dialog_open(self, spider, error) -> None:
        """打开失败记错误并停止，不走用户取消日志。"""
        message = f"❌ 任务选择框打开失败: {error}"
        append_log = getattr(self._host(), "append_log", None)
        if callable(append_log):
            try:
                append_log(message, level="ERROR")
            except TypeError:
                append_log(message)
        debug_logger.log(
            component="ApplicationController",
            action="selection_dialog_open_failed",
            level="ERROR",
            message="任务选择框打开失败，停止爬虫任务",
            status_code="APP_CRAWL_SELECTION_OPEN_FAILED",
            details={"error": str(error)},
        )
        if spider is not None and getattr(spider, "interrupt_requested", False) is not True:
            self._invalidate_active_spider_session()
            spider_session = getattr(self, "spider_session", SpiderSession(registry))
            bindings = getattr(self, "_active_spider_bindings", None)
            spider_session.stop_session(spider, bindings)
            self._host().notify_crawl_stop_requested()
        if spider is not None:
            resume = getattr(spider, "resume_from_ui", None)
            if callable(resume):
                resume(None)

    def _stop_spider_after_selection_cancel(self, spider) -> None:
        """把关闭选择框视为显式停止请求，并向 Spider 传播取消。"""
        if getattr(spider, "interrupt_requested", False):
            return
        spider_session = getattr(self, "spider_session", SpiderSession(registry))
        bindings = getattr(self, "_active_spider_bindings", None)
        spider_session.stop_session(spider, bindings)
        self._host().notify_crawl_stop_requested()
        debug_logger.log(
            component="ApplicationController",
            action="selection_cancel_stop_crawl",
            level="WARN",
            message="用户取消任务选择，停止爬虫任务",
            status_code="APP_CRAWL_SELECTION_CANCEL",
        )

    def _on_spider_finished(self):
        """当前 Spider 会话结束后重置宿主爬取状态。"""
        spider = self.current_spider
        bindings = getattr(self, "_active_spider_bindings", None)
        if spider is not None and bindings is not None:
            SpiderSession.unbind_spider(spider, bindings)
        self._active_spider_bindings = None
        self._active_spider_session_id = None
        if spider is getattr(self, "_stopped_spider", None):
            self._stopped_spider = None
            self._stopped_spider_session_id = None
        run_succeeded = getattr(spider, "run_succeeded", None) if spider is not None else None
        if run_succeeded is False:
            reporter = getattr(self._host(), "report_crawl_finished_unsuccessful", None)
            if callable(reporter):
                reporter()
            else:
                self._host().append_log("❌ 爬虫任务未成功结束", level="ERROR")
                setter = getattr(self._host(), "set_crawl_running_state", None)
                if callable(setter):
                    setter(False)
            debug_logger.log(
                component="ApplicationController",
                action="crawl_finished",
                level="ERROR",
                message="爬虫任务未成功结束",
                status_code="APP_CRAWL_FINISH_FAILED",
            )
        else:
            self._host().finish_crawl()
            debug_logger.log(
                component="ApplicationController",
                action="crawl_finished",
                message="爬虫任务结束",
                status_code="APP_CRAWL_FINISH",
            )
        self.current_spider = None

    def on_stop_crawl(self):
        """请求停止当前 Spider 会话，并把停止状态同步给宿主。"""
        # 先关闭模态选择框，并作废会话令牌，停止后的 items_found 不再入队。
        self._dismiss_selection_dialog()
        if self.current_spider:
            self._invalidate_active_spider_session()
            spider_session = getattr(self, "spider_session", SpiderSession(registry))
            bindings = getattr(self, "_active_spider_bindings", None)
            spider_session.stop_session(self.current_spider, bindings)
            self._host().notify_crawl_stop_requested()
            debug_logger.log(
                component="ApplicationController",
                action="stop_crawl",
                level="WARN",
                message="用户请求停止爬虫任务",
                status_code="APP_CRAWL_STOP",
            )
