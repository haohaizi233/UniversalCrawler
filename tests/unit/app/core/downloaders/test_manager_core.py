"""纯 Python 下载调度内核测试。"""

import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.core.download_manager import DownloadManager
from app.core.download_manager_core import DownloadManagerCore, PendingDownloadQueue
from app.models import VideoItem
from app.services.download_recovery_store import DownloadRecoveryStore

class _CallbackSignal:
    def __init__(self):
        self.targets = []

    def connect(self, target, *_args):
        self.targets.append(target)

    def emit(self, *args):
        for target in list(self.targets):
            target(*args)

class _FakeWorker:
    """最小 worker 替身：同步触发信号，避免测试依赖真实线程调度时序。"""

    def __init__(self, video, save_dir):
        self.video = video
        self.save_dir = save_dir
        self.sig_start = _CallbackSignal()
        self.sig_progress = _CallbackSignal()
        self.sig_finished = _CallbackSignal()
        self.sig_error = _CallbackSignal()
        self.finished = _CallbackSignal()
        self._slot_released = False
        self._completion_callback = None
        self.stop = Mock()
        self.wait = Mock(return_value=True)
        self.deleted = False

    def start(self):
        self.sig_start.emit(self.video.id)
        self.sig_progress.emit(self.video.id, 50)
        self.sig_finished.emit(self.video.id)
        if callable(self._completion_callback):
            self._completion_callback(self, "task_finished")
        self.finished.emit()

    def deleteLater(self):
        self.deleted = True

class _CoreManager(DownloadManagerCore):
    """测试用 DownloadManagerCore 子类，只记录事件，不接入 Qt/前端适配层。"""

    def __init__(self):
        self.started = []
        self.progress = []
        self.finished = []
        self.errors = []
        self.started_event = threading.Event()
        self.error_event = threading.Event()
        super().__init__(max_concurrent=1)

    def _create_worker(self, video, save_dir):
        return _FakeWorker(video, save_dir)

    def _emit_task_started(self, video_id: str) -> None:
        self.started.append(video_id)
        self.started_event.set()

    def _emit_task_progress(self, video_id: str, progress: int) -> None:
        self.progress.append((video_id, progress))

    def _emit_task_finished(self, video_id: str) -> None:
        self.finished.append(video_id)

    def _emit_task_error(self, video_id: str, error: str) -> None:
        self.errors.append((video_id, error))
        self.error_event.set()

    def _on_worker_thread_finished(self, worker):
        worker.deleteLater()

class DownloadManagerCoreTests(unittest.TestCase):
    def test_pending_download_queue_removes_item_without_private_queue_state(self):
        pending = PendingDownloadQueue()
        first = VideoItem(url="https://example.com/1.mp4", title="first", source="douyin")
        second = VideoItem(url="https://example.com/2.mp4", title="second", source="douyin")
        third = VideoItem(url="https://example.com/3.mp4", title="third", source="douyin")
        pending.put((first, "downloads"))
        pending.put((second, "downloads"))
        pending.put((third, "downloads"))

        removed = pending.remove_video(second.id)

        self.assertIs(removed, second)
        self.assertEqual(pending.snapshot_video_ids(), {first.id, third.id})
        self.assertEqual(pending.get_nowait()[0].id, first.id)
        self.assertEqual(pending.get_nowait()[0].id, third.id)

    def test_video_only_mode_skips_image_before_queue(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = PendingDownloadQueue()
        manager.video_only = True
        image = VideoItem(url="https://example.com/cover.jpg", title="cover", source="xiaohongshu")
        image.meta["content_type"] = "image/jpeg"

        queued = manager.add_task(image, "downloads")

        self.assertFalse(queued)
        self.assertTrue(manager.queue.empty())
        self.assertEqual(image.status, "\u5df2\u8df3\u8fc7")
        self.assertEqual(image.progress, 0)
        self.assertTrue(image.meta["skipped_by_video_only"])

    def test_queue_log_redacts_private_url_components(self):
        video = VideoItem(
            url="https://user:password@example.com/private/share/path?token=secret#fragment",
            title="private candidate",
            source="generic",
        )
        video.meta["content_type"] = "video"

        with patch("app.core.download_manager_core.debug_logger.log") as log:
            DownloadManagerCore._log_queue_task(video, "downloads")

        details = log.call_args.kwargs["details"]
        self.assertEqual(details["url"], "https://example.com/[redacted]")
        serialized = repr(log.call_args.kwargs)
        self.assertNotIn("password", serialized)
        self.assertNotIn("private/share/path", serialized)
        self.assertNotIn("token=secret", serialized)
        self.assertNotIn("fragment", serialized)

    def test_video_only_skip_log_redacts_private_url_components(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.video_only = True
        image = VideoItem(
            url="https://example.com/private/cover.jpg?signature=secret",
            title="private cover",
            source="generic",
        )
        image.meta["content_type"] = "image/jpeg"

        with patch("app.core.download_manager_core.debug_logger.log") as log:
            skipped = manager._log_and_skip_video_only(image)

        self.assertTrue(skipped)
        details = log.call_args.kwargs["details"]
        self.assertEqual(details["url"], "https://example.com/[redacted]")
        serialized = repr(log.call_args.kwargs)
        self.assertNotIn("private/cover.jpg", serialized)
        self.assertNotIn("signature=secret", serialized)

    def test_startup_sweep_removes_non_hls_orphan_download_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.core.download_manager_core.cfg.get",
            return_value=temp_dir,
        ):
            # 同时覆盖 B站分流缓存和 HTTP `.downloading`，确认启动清扫不会误删成品。
            audio_temp = Path(temp_dir) / "demo_audio.m4s"
            http_temp = Path(temp_dir) / "demo.mp4.downloading"
            keep_file = Path(temp_dir) / "demo.mp4"
            for path in (audio_temp, http_temp, keep_file):
                path.write_bytes(b"test")
            manager = DownloadManagerCore.__new__(DownloadManagerCore)

            manager._sweep_m3u8_orphan_workspaces_on_startup()

            self.assertFalse(audio_temp.exists())
            self.assertFalse(http_temp.exists())
            self.assertTrue(keep_file.exists())

    def test_startup_sweep_consumes_exact_recovery_paths_even_when_one_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            exact_dir = root / "collection" / "season"
            exact_dir.mkdir(parents=True)
            missing_dir = root / "removed-before-restart"
            stale_temp = exact_dir / "demo.mp4.downloading"
            stale_temp.write_bytes(b"partial")
            store = DownloadRecoveryStore(db_path=root / "recovery.sqlite3")
            store.register_task(video_id="failed", save_directory=exact_dir)
            store.handoff_failed_task("failed")
            store.register_task(video_id="interrupted", save_directory=missing_dir)
            store.mark_legacy_sweep_complete(root)
            manager = DownloadManagerCore.__new__(DownloadManagerCore)
            manager._download_recovery_store = store
            maintenance_root = os.path.normcase(os.path.abspath(root))
            DownloadManagerCore._STARTUP_MAINTENANCE_ROOTS.discard(maintenance_root)

            with patch("app.core.download_manager_core.cfg.get", return_value=str(root)):
                stats = manager._sweep_m3u8_orphan_workspaces_on_startup()

            self.assertFalse(stale_temp.exists())
            self.assertEqual(store.recovery_counts(), {"active": 0, "pending_cleanup": 0})
            self.assertEqual(stats["recovery_records_consumed"], 2)

    def test_startup_sweep_preserves_tasks_registered_while_cleanup_runs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_dir = root / "old"
            new_dir = root / "new"
            old_dir.mkdir()
            new_dir.mkdir()
            store = DownloadRecoveryStore(db_path=root / "recovery.sqlite3")
            store.register_task(video_id="old", save_directory=old_dir)
            store.mark_legacy_sweep_complete(root)
            manager = DownloadManagerCore.__new__(DownloadManagerCore)
            manager._download_recovery_store = store
            maintenance_root = os.path.normcase(os.path.abspath(root))
            DownloadManagerCore._STARTUP_MAINTENANCE_ROOTS.discard(maintenance_root)
            registered_during_scan = False

            def scan_directory(*_args, **_kwargs):
                nonlocal registered_during_scan
                if not registered_during_scan:
                    registered_during_scan = True
                    store.register_task(video_id="new", save_directory=new_dir)
                return SimpleNamespace(
                    removed_count=0,
                    error="",
                    children=[],
                    truncated=False,
                    scanned_entries=0,
                )

            with patch("app.core.download_manager_core.cfg.get", return_value=str(root)), patch(
                "app.services.file_service.MediaLibraryService.sweep_orphan_download_temp_directory",
                side_effect=scan_directory,
            ):
                stats = manager._sweep_m3u8_orphan_workspaces_on_startup()

            self.assertIsNone(store.task_path("old"))
            self.assertIsNotNone(store.task_path("new"))
            self.assertEqual(stats["recovery_records_consumed"], 1)

    def test_startup_maintenance_does_not_block_manager_construction(self):
        release_sweep = threading.Event()
        sweep_started = threading.Event()

        def blocking_sweep(_manager):
            sweep_started.set()
            release_sweep.wait(timeout=2.0)

        with patch.object(DownloadManagerCore, "_sweep_m3u8_orphan_workspaces_on_startup", blocking_sweep):
            started_at = time.monotonic()
            manager = DownloadManagerCore(max_concurrent=1)
            elapsed = time.monotonic() - started_at
            try:
                self.assertTrue(sweep_started.wait(timeout=0.5))
                self.assertLess(elapsed, 1.0)
            finally:
                release_sweep.set()
                manager.stop_all()

    def test_dispatch_waits_for_startup_maintenance(self):
        release_sweep = threading.Event()
        sweep_started = threading.Event()

        def blocking_sweep(_manager):
            sweep_started.set()
            release_sweep.wait()

        with patch.object(DownloadManagerCore, "_sweep_m3u8_orphan_workspaces_on_startup", blocking_sweep):
            manager = _CoreManager()
            video = VideoItem(url="https://example.com/video.mp4", title="video", source="douyin")
            try:
                self.assertTrue(sweep_started.wait(timeout=5.0))
                self.assertTrue(manager.add_task(video, "downloads"))
                self.assertFalse(manager.started_event.wait(timeout=0.15))
                self.assertEqual(manager.started, [])

                release_sweep.set()
                self.assertTrue(manager.started_event.wait(timeout=5.0))
                self.assertEqual(manager.started, [video.id])
            finally:
                release_sweep.set()
                manager.stop_all()

    def test_dispatch_does_not_wait_for_startup_completion_log_io(self):
        release_completion_log = threading.Event()
        completion_log_started = threading.Event()

        def blocking_completion_log(**kwargs):
            if kwargs.get("action") == "startup_maintenance_completed":
                completion_log_started.set()
                release_completion_log.wait()

        with (
            patch.object(
                DownloadManagerCore,
                "_sweep_m3u8_orphan_workspaces_on_startup",
                return_value=None,
            ),
            patch(
                "app.core.download_manager_core.debug_logger.log",
                side_effect=blocking_completion_log,
            ),
        ):
            manager = _CoreManager()
            video = VideoItem(
                url="https://example.com/video.mp4",
                title="video",
                source="douyin",
            )
            try:
                self.assertTrue(completion_log_started.wait(timeout=5.0))
                self.assertTrue(manager.add_task(video, "downloads"))

                self.assertTrue(manager._startup_maintenance_done.wait(timeout=5.0))
                self.assertTrue(manager.started_event.wait(timeout=5.0))
            finally:
                release_completion_log.set()
                manager.stop_all()

    def test_dispatch_does_not_wait_for_startup_error_log_io(self):
        release_error_log = threading.Event()
        error_log_started = threading.Event()

        def blocking_error_log(*_args, **_kwargs):
            error_log_started.set()
            release_error_log.wait()

        with (
            patch.object(
                DownloadManagerCore,
                "_sweep_m3u8_orphan_workspaces_on_startup",
                side_effect=RuntimeError("startup sweep failed"),
            ),
            patch(
                "app.core.download_manager_core.debug_logger.log_exception",
                side_effect=blocking_error_log,
            ),
        ):
            manager = _CoreManager()
            video = VideoItem(
                url="https://example.com/video.mp4",
                title="video",
                source="douyin",
            )
            try:
                self.assertTrue(error_log_started.wait(timeout=5.0))
                self.assertTrue(manager.add_task(video, "downloads"))

                self.assertTrue(manager._startup_maintenance_done.wait(timeout=5.0))
                self.assertTrue(manager.started_event.wait(timeout=5.0))
            finally:
                release_error_log.set()
                manager.stop_all()

    def test_dispatch_persists_path_before_worker_and_deletes_it_on_completion(self):
        manager = _CoreManager()
        recovery_store = Mock()
        recovery_store.register_task.return_value = "generation-1"
        manager._download_recovery_store = recovery_store
        video = VideoItem(
            url="https://example.com/video.mp4",
            title="video",
            source="bilibili",
        )
        video.meta["trace_id"] = "trace-1"
        try:
            self.assertTrue(manager._startup_maintenance_done.wait(timeout=1.0))
            self.assertTrue(manager.add_task(video, "downloads"))
            self.assertTrue(manager.started_event.wait(timeout=1.0))

            recovery_store.register_task.assert_called_once_with(
                video_id=video.id,
                save_directory="downloads",
                source_url=video.url,
                trace_id="trace-1",
                platform="bilibili",
            )
            recovery_store.delete_task.assert_called_once_with(video.id, "generation-1")
            self.assertEqual(video.meta["save_directory"], "downloads")
        finally:
            manager.stop_all()

    def test_concrete_manager_keeps_core_recovery_callbacks(self):
        manager = DownloadManager.__new__(DownloadManager)
        manager._download_recovery_store = Mock()
        manager._emit_task_started = Mock()
        manager._emit_task_progress = Mock()
        manager._emit_task_finished = Mock()
        manager._emit_task_error = Mock()
        manager._on_worker_thread_finished = Mock()
        video = VideoItem(
            url="https://example.com/video.mp4",
            title="video",
            source="bilibili",
        )
        video.meta["recovery_generation"] = "generation-1"
        worker = _FakeWorker(video, "downloads")

        manager._connect_worker_callbacks(worker)
        worker.sig_error.emit(video.id, "network")

        manager._download_recovery_store.handoff_failed_task.assert_called_once_with(
            video.id,
            "generation-1",
        )

    def test_dispatch_persists_the_exact_collection_directory_before_worker_start(self):
        with patch.object(
            DownloadManagerCore,
            "_sweep_m3u8_orphan_workspaces_on_startup",
            return_value=None,
        ):
            manager = _CoreManager()
        recovery_store = Mock()
        manager._download_recovery_store = recovery_store
        video = VideoItem(
            url="https://example.com/video.mp4",
            title="video",
            source="bilibili",
        )
        video.meta.update({"folder_name": "Album", "use_subdir": True})
        try:
            self.assertTrue(manager._startup_maintenance_done.wait(timeout=1.0))
            self.assertTrue(manager.add_task(video, "downloads"))
            self.assertTrue(manager.started_event.wait(timeout=1.0))

            recovery_store.register_task.assert_called_once_with(
                video_id=video.id,
                save_directory=str(Path("downloads") / "Album"),
                source_url=video.url,
                trace_id="",
                platform="bilibili",
            )
        finally:
            manager.stop_all()

    def test_dispatch_fails_closed_when_recovery_path_cannot_be_committed(self):
        with patch.object(
            DownloadManagerCore,
            "_sweep_m3u8_orphan_workspaces_on_startup",
            return_value=None,
        ):
            manager = _CoreManager()
        manager._download_recovery_store = Mock()
        manager._download_recovery_store.register_task.side_effect = OSError("disk unavailable")
        video = VideoItem(
            url="https://example.com/video.mp4",
            title="video",
            source="bilibili",
        )
        try:
            self.assertTrue(manager._startup_maintenance_done.wait(timeout=1.0))
            self.assertTrue(manager.add_task(video, "downloads"))
            self.assertTrue(manager.error_event.wait(timeout=1.0))

            self.assertEqual(manager.started, [])
            self.assertFalse(manager._download_recovery_store.handoff_failed_task.called)
        finally:
            manager.stop_all()

    def test_worker_creation_failure_hands_registered_path_to_startup_cleanup(self):
        with patch.object(
            DownloadManagerCore,
            "_sweep_m3u8_orphan_workspaces_on_startup",
            return_value=None,
        ):
            manager = _CoreManager()
        manager._download_recovery_store = Mock()
        manager._download_recovery_store.register_task.return_value = "generation-1"
        manager._create_worker = Mock(side_effect=RuntimeError("worker init failed"))
        video = VideoItem(
            url="https://example.com/video.mp4",
            title="video",
            source="bilibili",
        )
        try:
            self.assertTrue(manager._startup_maintenance_done.wait(timeout=1.0))
            self.assertTrue(manager.add_task(video, "downloads"))
            self.assertTrue(manager.error_event.wait(timeout=1.0))

            manager._download_recovery_store.register_task.assert_called_once()
            manager._download_recovery_store.handoff_failed_task.assert_called_once_with(
                video.id,
                "generation-1",
            )
        finally:
            manager.stop_all()

    def test_pending_download_queue_removes_many_items_in_one_pass(self):
        pending = PendingDownloadQueue()
        first = VideoItem(url="https://example.com/1.mp4", title="first", source="douyin")
        second = VideoItem(url="https://example.com/2.mp4", title="second", source="douyin")
        third = VideoItem(url="https://example.com/3.mp4", title="third", source="douyin")
        pending.put((first, "downloads"))
        pending.put((second, "downloads"))
        pending.put((third, "downloads"))

        removed = pending.remove_videos({first.id, third.id})

        self.assertEqual([item.id for item in removed], [first.id, third.id])
        self.assertEqual(pending.snapshot_video_ids(), {second.id})
        self.assertEqual(pending.get_nowait()[0].id, second.id)

    def test_add_tasks_batches_pending_queue_wakeup(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = PendingDownloadQueue()
        manager.video_only = False
        manager.is_running = True
        first = VideoItem(url="https://example.com/1.mp4", title="first", source="douyin")
        second = VideoItem(url="https://example.com/2.mp4", title="second", source="douyin")

        queued = manager.add_tasks([first, second], "downloads")

        self.assertEqual(queued, 2)
        self.assertEqual(manager.queue.snapshot_video_ids(), {first.id, second.id})
        self.assertEqual(manager.queue.get_nowait()[0].id, first.id)
        self.assertEqual(manager.queue.get_nowait()[0].id, second.id)

    def test_cancel_tasks_batches_queued_and_running_items(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = PendingDownloadQueue()
        manager._workers_lock = threading.Lock()
        manager.workers = []
        queued = VideoItem(url="https://example.com/q.mp4", title="queued", source="douyin")
        keep = VideoItem(url="https://example.com/k.mp4", title="keep", source="douyin")
        running = VideoItem(url="https://example.com/r.mp4", title="running", source="douyin")
        worker = _FakeWorker(running, "downloads")
        manager.workers = [worker]
        manager.queue.put((queued, "downloads"))
        manager.queue.put((keep, "downloads"))

        result = manager.cancel_tasks({queued.id, running.id, "missing"})

        self.assertEqual(result[queued.id], "queued")
        self.assertEqual(result[running.id], "running")
        self.assertIsNone(result["missing"])
        self.assertEqual(manager.queued_video_ids(), {keep.id})
        worker.stop.assert_called_once()
        self.assertTrue(queued.meta["user_cancel_requested"])
        self.assertTrue(running.meta["user_cancel_requested"])

    def test_cancel_task_stops_running_duplicate_even_when_same_id_is_queued(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = PendingDownloadQueue()
        manager._workers_lock = threading.RLock()
        manager.workers = []
        queued = VideoItem(url="https://example.com/q.mp4", title="queued", source="douyin")
        running = VideoItem(url="https://example.com/r.mp4", title="running", source="douyin")
        running.id = queued.id
        worker = _FakeWorker(running, "downloads")
        manager.workers = [worker]
        manager.queue.put((queued, "downloads"))

        result = manager.cancel_task(queued.id)

        self.assertEqual(result, "running")
        self.assertTrue(queued.meta["user_cancel_requested"])
        self.assertTrue(running.meta["user_cancel_requested"])
        worker.stop.assert_called_once()

    def test_cancel_video_and_wait_matches_only_the_captured_instance(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = PendingDownloadQueue()
        manager._workers_lock = threading.RLock()
        old = VideoItem(url="https://example.com/old.mp4", title="old", source="douyin")
        replacement = VideoItem(
            url="https://example.com/replacement.mp4",
            title="replacement",
            source="douyin",
        )
        replacement.id = old.id
        old_worker = _FakeWorker(old, "downloads")
        replacement_worker = _FakeWorker(replacement, "downloads")
        manager.workers = [old_worker, replacement_worker]
        manager._dispatching_tasks = [(replacement, "downloads")]
        manager.queue.put((old, "downloads"))
        manager.queue.put((replacement, "downloads"))

        result = manager.cancel_video_and_wait(old, timeout_ms=50)

        self.assertEqual(result, "running")
        old_worker.stop.assert_called_once()
        old_worker.wait.assert_called_once()
        replacement_worker.stop.assert_not_called()
        replacement_worker.wait.assert_not_called()
        self.assertFalse(replacement.meta.get("user_cancel_requested", False))
        self.assertIs(manager.queue.get_nowait()[0], replacement)
        self.assertTrue(manager.queue.empty())

    def test_cancel_videos_and_wait_confirms_exact_batch_with_one_deadline(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = PendingDownloadQueue()
        manager._workers_lock = threading.RLock()
        queued = VideoItem(url="https://example.com/q.mp4", title="queued", source="douyin")
        running = VideoItem(url="https://example.com/r.mp4", title="running", source="douyin")
        keep = VideoItem(url="https://example.com/k.mp4", title="keep", source="douyin")
        running_worker = _FakeWorker(running, "downloads")
        manager.workers = [running_worker]
        manager._dispatching_tasks = []
        manager.queue.put((queued, "downloads"))
        manager.queue.put((keep, "downloads"))

        result = manager.cancel_videos_and_wait((queued, running), timeout_ms=50)

        self.assertEqual(result, {queued.id: "cancelled", running.id: "cancelled"})
        self.assertTrue(queued.meta["user_cancel_requested"])
        self.assertTrue(running.meta["user_cancel_requested"])
        running_worker.stop.assert_called_once()
        running_worker.wait.assert_called_once()
        self.assertIs(manager.queue.get_nowait()[0], keep)
        self.assertTrue(manager.queue.empty())

    def test_cancel_videos_and_wait_reports_only_unconfirmed_exact_workers(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = PendingDownloadQueue()
        manager._workers_lock = threading.RLock()
        stopped = VideoItem(
            url="https://example.com/stopped.mp4",
            title="stopped",
            source="douyin",
        )
        unresolved = VideoItem(
            url="https://example.com/unresolved.mp4",
            title="unresolved",
            source="douyin",
        )
        stopped_worker = _FakeWorker(stopped, "downloads")
        unresolved_worker = _FakeWorker(unresolved, "downloads")
        unresolved_worker.wait.return_value = False
        manager.workers = [stopped_worker, unresolved_worker]
        manager._dispatching_tasks = []

        result = manager.cancel_videos_and_wait((stopped, unresolved), timeout_ms=50)

        self.assertEqual(
            result,
            {stopped.id: "cancelled", unresolved.id: "timeout"},
        )
        stopped_worker.stop.assert_called_once()
        unresolved_worker.stop.assert_called_once()
        stopped_worker.wait.assert_called_once()
        unresolved_worker.wait.assert_called()

    def test_cancel_videos_and_wait_treats_wait_errors_as_unconfirmed(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = PendingDownloadQueue()
        manager._workers_lock = threading.RLock()
        manager._dispatching_tasks = []
        captured = VideoItem(
            url="https://example.com/wait-error.mp4",
            title="wait-error",
            source="douyin",
        )
        worker = _FakeWorker(captured, "downloads")
        worker.wait.side_effect = RuntimeError("worker wait unavailable")
        manager.workers = [worker]

        result = manager.cancel_videos_and_wait((captured,), timeout_ms=50)

        self.assertEqual(result, {captured.id: "timeout"})
        worker.stop.assert_called_once()
        worker.wait.assert_called()

    def test_cancel_videos_and_wait_preserves_same_id_replacement_instances(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = PendingDownloadQueue()
        manager._workers_lock = threading.RLock()
        captured = VideoItem(
            url="https://example.com/captured.mp4",
            title="captured",
            source="douyin",
        )
        replacement = VideoItem(
            url="https://example.com/replacement.mp4",
            title="replacement",
            source="douyin",
        )
        replacement.id = captured.id
        replacement_worker = _FakeWorker(replacement, "downloads")
        manager.workers = [replacement_worker]
        manager._dispatching_tasks = [(replacement, "downloads")]
        manager.queue.put((captured, "downloads"))
        manager.queue.put((replacement, "downloads"))

        result = manager.cancel_videos_and_wait((captured,), timeout_ms=50)

        self.assertEqual(result, {captured.id: "cancelled"})
        replacement_worker.stop.assert_not_called()
        replacement_worker.wait.assert_not_called()
        self.assertFalse(replacement.meta.get("user_cancel_requested", False))
        self.assertIs(manager.queue.get_nowait()[0], replacement)
        self.assertTrue(manager.queue.empty())

    def test_cancel_videos_and_wait_covers_queue_to_dispatch_handoff_window(self):
        popped = threading.Event()
        resume_dispatch = threading.Event()

        class HandoffQueue(PendingDownloadQueue):
            def get(self, *args, **kwargs):
                item = super().get(*args, **kwargs)
                popped.set()
                resume_dispatch.wait(timeout=2)
                return item

        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = HandoffQueue()
        manager._workers_lock = threading.RLock()
        manager._start_stop_lock = threading.RLock()
        manager._startup_maintenance_done = threading.Event()
        manager._startup_maintenance_done.set()
        manager._dispatching_tasks = []
        manager.workers = []
        manager.is_running = True
        manager._wait_for_dispatch_slot = Mock(side_effect=[True, False])
        manager._release_dispatch_slot = Mock()
        manager._has_capacity_for = Mock(return_value=True)
        manager._register_download_directory = Mock()
        manager._create_worker = Mock()

        captured = VideoItem(
            url="https://example.com/handoff.mp4",
            title="handoff",
            source="douyin",
        )
        manager.queue.put((captured, "downloads"))
        dispatcher = threading.Thread(target=manager._dispatch_loop)
        dispatcher.start()
        self.assertTrue(popped.wait(timeout=1))

        result = manager.cancel_videos_and_wait((captured,), timeout_ms=50)
        resume_dispatch.set()
        dispatcher.join(timeout=2)

        self.assertEqual(result, {captured.id: "cancelled"})
        self.assertFalse(dispatcher.is_alive())
        self.assertTrue(captured.meta["user_cancel_requested"])
        manager._create_worker.assert_not_called()
        self.assertEqual(manager._dispatching_tasks, [])

    def test_pending_work_counts_includes_dequeued_dispatching_tasks(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = PendingDownloadQueue()
        manager._workers_lock = threading.RLock()
        manager.workers = []
        dispatching = VideoItem(url="https://example.com/d.mp4", title="dispatching", source="douyin")
        manager._dispatching_tasks = [(dispatching, "downloads")]

        active, queued = manager.pending_work_counts()

        self.assertEqual((active, queued), (1, 0))

    def test_dispatch_loop_runs_without_qt_adapter(self):
        manager = _CoreManager()
        manager.is_running = False
        manager.dispatcher_thread.join(timeout=2)
        manager.queue = queue.Queue()
        manager.workers = []
        manager._workers_lock = threading.Lock()
        manager.is_running = True
        manager.slot_semaphore = Mock()
        manager.slot_semaphore.acquire.return_value = True
        item = VideoItem(url="https://example.com/video.mp4", title="demo", source="douyin")
        manager.queue.put((item, "downloads"))

        def stop_after_release():
            manager.is_running = False

        manager.slot_semaphore.release.side_effect = stop_after_release

        manager._dispatch_loop()

        self.assertEqual(manager.started, [item.id])
        self.assertEqual(manager.progress, [(item.id, 50)])
        self.assertEqual(manager.finished, [item.id])
        self.assertEqual(manager.errors, [])
        self.assertEqual(manager.workers, [])
        manager.slot_semaphore.release.assert_called_once()

    def test_dispatch_loop_releases_slot_when_worker_start_fails(self):
        class FailingWorker(_FakeWorker):
            def start(self):
                raise RuntimeError("start failed")

        class FailingManager(DownloadManagerCore):
            def _create_worker(self, video, save_dir):
                return FailingWorker(video, save_dir)

            def _emit_task_started(self, video_id: str) -> None:
                raise AssertionError("worker should not start")

            def _emit_task_progress(self, video_id: str, progress: int) -> None:
                raise AssertionError("worker should not report progress")

            def _emit_task_finished(self, video_id: str) -> None:
                raise AssertionError("worker should not finish")

            def _emit_task_error(self, video_id: str, error: str) -> None:
                self.errors.append((video_id, error))
                self.is_running = False

        manager = FailingManager.__new__(FailingManager)
        manager.queue = queue.Queue()
        manager.workers = []
        manager._workers_lock = threading.Lock()
        manager.is_running = True
        manager.errors = []
        manager.max_concurrent = 1
        manager.slot_semaphore = Mock()
        manager.slot_semaphore.acquire.return_value = True
        item = VideoItem(url="https://example.com/video.mp4", title="demo", source="douyin")
        manager.queue.put((item, "downloads"))

        # worker.start 抛错时，dispatcher 已经占用的并发槽必须释放，否则后续任务会永久阻塞。
        manager._dispatch_loop()

        self.assertEqual(manager.workers, [])
        self.assertEqual(manager.errors[0][0], item.id)
        manager.slot_semaphore.release.assert_called_once()

    def test_dispatch_requeues_dequeued_task_when_manager_stops(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = PendingDownloadQueue()
        manager.workers = []
        manager._workers_lock = threading.RLock()
        manager._start_stop_lock = threading.RLock()
        manager.max_concurrent = 1
        manager.is_running = True
        manager.slot_semaphore = threading.Semaphore(1)
        item = VideoItem(url="https://example.com/x", title="demo", source="douyin")
        manager.queue.put((item, "downloads"))
        manager._create_worker = Mock(side_effect=AssertionError("worker should not start"))

        original_get = manager.queue.get

        def get_and_stop(timeout=1):
            video, save_dir = original_get(timeout=timeout)
            manager.is_running = False
            return video, save_dir

        manager.queue.get = get_and_stop
        # 已取出但未启动的任务不能塞回内存队列，否则 stop_all 排空后会丢。
        with patch("app.core.download_manager_core.debug_logger.log") as log:
            manager._dispatch_loop()

        self.assertEqual(manager.queue.snapshot_video_ids(), set())
        self.assertEqual(manager.workers, [])
        manager._create_worker.assert_not_called()
        self.assertIn(
            "discard_unstarted_task",
            [call.kwargs.get("action") for call in log.call_args_list],
        )

    def test_stop_all_fallback_drains_queue_without_empty_probe(self):
        class QueueWithoutReliableEmpty:
            def __init__(self):
                self.items = [object(), object()]

            def empty(self):
                raise AssertionError("stop_all must not rely on Queue.empty()")

            def get_nowait(self):
                if not self.items:
                    raise queue.Empty
                return self.items.pop()

        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = QueueWithoutReliableEmpty()
        manager.workers = []
        manager._workers_lock = threading.RLock()
        manager._start_stop_lock = threading.RLock()
        manager._dispatch_slot_gate = threading.Event()
        manager.dispatcher_thread = Mock()
        manager.dispatcher_thread.join.return_value = None
        manager.dispatcher_thread.is_alive.return_value = False
        manager.is_running = True

        with patch("app.core.download_manager_core.debug_logger.log") as log:
            manager.stop_all()

        self.assertEqual(manager.queue.items, [])
        manager.dispatcher_thread.join.assert_called_once_with(timeout=2)
        manager.dispatcher_thread.is_alive.assert_called_once_with()
        self.assertNotIn(
            "stop_all_dispatcher_timeout",
            [call.kwargs.get("action") for call in log.call_args_list],
        )

    def test_stop_all_preserves_queue_failure_after_stopping_workers_and_dispatcher(self):
        primary = KeyboardInterrupt("queue drain interrupted")
        queue_owner = Mock()
        queue_owner.drain.side_effect = primary
        queue_owner.get_nowait.side_effect = queue.Empty
        first = Mock()
        first.stop.return_value = None
        first.wait.return_value = True
        second = Mock()
        second.stop.return_value = None
        second.wait.return_value = True
        manager = self._stop_manager(queue_owner, [first, second])

        with (
            patch("app.core.download_manager_core.debug_logger.log"),
            self.assertRaises(KeyboardInterrupt) as caught,
        ):
            manager.stop_all()

        self.assertIs(caught.exception, primary)
        first.stop.assert_called_once_with()
        first.wait.assert_called_once_with(manager.WORKER_STOP_TIMEOUT_MS)
        second.stop.assert_called_once_with()
        second.wait.assert_called_once_with(manager.WORKER_STOP_TIMEOUT_MS)
        manager.dispatcher_thread.join.assert_called_once_with(timeout=2)
        manager.dispatcher_thread.is_alive.assert_called_once_with()

    def test_stop_all_preserves_first_worker_failure_and_attempts_every_step(self):
        primary = SystemExit("first worker stop failed")
        secondary = KeyboardInterrupt("first worker wait failed")
        first = Mock()
        first.stop.side_effect = primary
        first.wait.side_effect = [secondary, True]
        second = Mock()
        second.stop.return_value = None
        second.wait.return_value = True
        manager = self._stop_manager(Mock(), [first, second])
        manager.queue.drain.return_value = []

        with (
            patch("app.core.download_manager_core.debug_logger.log"),
            self.assertRaises(SystemExit) as caught,
        ):
            manager.stop_all()

        self.assertIs(caught.exception, primary)
        first.stop.assert_called_once_with()
        self.assertGreaterEqual(first.wait.call_count, 2)
        second.stop.assert_called_once_with()
        second.wait.assert_called_once_with(manager.WORKER_STOP_TIMEOUT_MS)
        manager.dispatcher_thread.join.assert_called_once_with(timeout=2)
        manager.dispatcher_thread.is_alive.assert_called_once_with()

    def test_stop_all_wait_false_uses_bounded_backoff_and_single_timeout_log(self):
        class HostileVideo:
            def __getattribute__(self, _name):
                raise KeyboardInterrupt("telemetry lookup failed")

        first = SimpleNamespace(
            stop=Mock(return_value=None),
            wait=Mock(side_effect=[False, False, False, False, True]),
            video=HostileVideo(),
        )
        second = Mock()
        second.stop.return_value = None
        second.wait.return_value = True
        manager = self._stop_manager(Mock(), [first, second])
        manager.queue.drain.return_value = []

        with (
            patch("app.core.download_shutdown_guardian.time.sleep") as retry_sleep,
            patch("app.core.download_shutdown_guardian.debug_logger.log") as log,
        ):
            stopped = manager.stop_all()

        self.assertIs(stopped, True)
        self.assertEqual(first.wait.call_count, 5)
        self.assertEqual(
            [call.args[0] for call in retry_sleep.call_args_list],
            [0.01, 0.02, 0.04, 0.08],
        )
        timeout_logs = [
            call
            for call in log.call_args_list
            if call.kwargs.get("action") == "stop_all_worker_timeout"
        ]
        self.assertEqual(len(timeout_logs), 1)
        second.stop.assert_called_once_with()
        second.wait.assert_called_once_with(manager.WORKER_STOP_TIMEOUT_MS)
        manager.dispatcher_thread.join.assert_called_once_with(timeout=2)

    def test_stop_all_keeps_waiting_for_dispatcher_after_initial_timeout(self):
        manager = self._stop_manager(Mock(), [])
        manager.queue.drain.return_value = []
        manager.dispatcher_thread.is_alive.side_effect = [True, False]

        with patch("app.core.download_manager_core.debug_logger.log"):
            stopped = manager.stop_all()

        self.assertIs(stopped, True)
        self.assertEqual(manager.dispatcher_thread.join.call_count, 2)
        manager.dispatcher_thread.join.assert_called_with(timeout=2)

    def test_stop_all_wait_exception_uses_liveness_probe_and_bounded_backoff(self):
        primary = RuntimeError("worker wait failed")

        class PermanentlyFailingWaitWorker:
            def __init__(self):
                self.stop = Mock()
                self.wait_calls = 0
                self.liveness_calls = 0
                self.ident = 7

            def wait(self, _timeout_ms):
                self.wait_calls += 1
                if self.wait_calls > 3:
                    return True
                raise primary

            def is_alive(self):
                self.liveness_calls += 1
                return self.liveness_calls < 3

        worker = PermanentlyFailingWaitWorker()
        manager = self._stop_manager(Mock(), [worker])
        manager.queue.drain.return_value = []

        with (
            patch("app.core.download_manager_core.debug_logger.log"),
            patch("app.core.download_manager_core.debug_logger.log_exception") as log_exception,
            patch("app.core.download_manager_core.time.sleep") as retry_sleep,
            self.assertRaises(RuntimeError) as caught,
        ):
            manager.stop_all()

        self.assertIs(caught.exception, primary)
        self.assertEqual(worker.wait_calls, 3)
        self.assertEqual(worker.liveness_calls, 3)
        self.assertEqual([call.args[0] for call in retry_sleep.call_args_list], [0.01, 0.02])
        self.assertEqual(log_exception.call_count, 1)
        manager.dispatcher_thread.join.assert_called_once_with(timeout=2)

    def test_stop_all_broadcasts_stop_to_every_worker_before_waiting(self):
        first_wait_entered = threading.Event()
        release_first = threading.Event()
        second_stop_received = threading.Event()

        first = Mock()
        first.stop.return_value = None

        def wait_for_release(_timeout_ms):
            first_wait_entered.set()
            release_first.wait(2.0)
            return True

        first.wait.side_effect = wait_for_release
        second = Mock()
        second.stop.side_effect = lambda: second_stop_received.set()
        second.wait.return_value = True
        manager = self._stop_manager(Mock(), [first, second])
        manager.queue.drain.return_value = []
        failures = []

        def stop_all():
            try:
                manager.stop_all()
            except BaseException as exc:
                failures.append(exc)

        caller = threading.Thread(target=stop_all, daemon=False)
        try:
            with patch("app.core.download_manager_core.debug_logger.log"):
                caller.start()
                self.assertTrue(first_wait_entered.wait(1.0))
                self.assertTrue(second_stop_received.wait(0.2))
        finally:
            release_first.set()
            caller.join(timeout=2.0)

        self.assertFalse(caller.is_alive())
        self.assertEqual(failures, [])
        first.stop.assert_called_once_with()
        second.stop.assert_called_once_with()
        manager.dispatcher_thread.join.assert_called_once_with(timeout=2)

    @staticmethod
    def _stop_manager(queue_owner, workers):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = queue_owner
        manager.workers = list(workers)
        manager._workers_lock = threading.RLock()
        manager._start_stop_lock = threading.RLock()
        manager._dispatch_slot_gate = threading.Event()
        manager._startup_maintenance_done = threading.Event()
        manager.dispatcher_thread = Mock()
        manager.dispatcher_thread.join.return_value = None
        manager.dispatcher_thread.is_alive.return_value = False
        manager.is_running = True
        return manager

    def test_cancel_task_uses_pending_queue_public_remove_api(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = PendingDownloadQueue()
        manager._workers_lock = threading.Lock()
        manager.workers = []
        keep_item = VideoItem(url="https://example.com/1.mp4", title="keep", source="douyin")
        cancel_item = VideoItem(url="https://example.com/2.mp4", title="cancel", source="douyin")
        manager.queue.put((keep_item, "downloads"))
        manager.queue.put((cancel_item, "downloads"))

        result = manager.cancel_task(cancel_item.id)

        self.assertEqual(result, "queued")
        self.assertEqual(manager.queued_video_ids(), {keep_item.id})

    def test_set_max_concurrent_rebuilds_capacity_and_preserves_in_use_slots(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.max_concurrent = 2
        manager.slot_semaphore = threading.BoundedSemaphore(2)
        manager.slot_semaphore.acquire(blocking=False)
        manager._start_stop_lock = threading.RLock()

        result = manager.set_max_concurrent(5)

        self.assertEqual(result, 5)
        self.assertEqual(manager.max_concurrent, 5)
        acquired = 0
        while manager.slot_semaphore.acquire(blocking=False):
            acquired += 1
        self.assertEqual(acquired, DownloadManagerCore.LIGHTWEIGHT_MIN_CONCURRENT - 1)
        self.assertEqual(manager._slot_semaphore_capacity, DownloadManagerCore.LIGHTWEIGHT_MIN_CONCURRENT)

    def test_set_max_concurrent_caps_regular_workers_but_keeps_image_fast_lane(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.max_concurrent = 5
        manager.image_respects_concurrency = False
        manager.image_fast_lane_limit = 10
        manager.slot_semaphore = threading.BoundedSemaphore(5)
        manager._start_stop_lock = threading.RLock()

        result = manager.set_max_concurrent(32)

        self.assertEqual(result, 5)
        self.assertEqual(manager.max_concurrent, 5)
        self.assertEqual(manager._slot_semaphore_capacity, 10)

    def test_dispatch_capacity_uses_live_max_concurrent(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.max_concurrent = 2
        manager._workers_lock = threading.RLock()
        manager.workers = [object(), object()]

        self.assertFalse(manager._has_dispatch_capacity())

        manager.max_concurrent = 3

        self.assertTrue(manager._has_dispatch_capacity())

    def test_stale_finished_workers_do_not_keep_concurrency_slots(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = PendingDownloadQueue()
        manager.max_concurrent = 3
        manager.image_respects_concurrency = True
        manager._workers_lock = threading.RLock()
        manager._slot_semaphore_lock = threading.RLock()
        manager._dispatch_slot_gate = threading.Event()
        manager.slot_semaphore = threading.BoundedSemaphore(3)
        manager.workers = []

        stale_workers = []
        for index in range(3):
            video = VideoItem(url=f"https://example.com/{index}.mp4", title="done", source="douyin")
            video.meta["content_type"] = "video"
            worker = SimpleNamespace(
                video=video,
                ident=index + 1,
                is_alive=lambda: False,
                _slot_released=False,
            )
            stale_workers.append(worker)
            manager.workers.append(worker)
            self.assertTrue(manager.slot_semaphore.acquire(blocking=False))

        next_video = VideoItem(url="https://example.com/next.mp4", title="next", source="douyin")
        next_video.meta["content_type"] = "video"

        self.assertTrue(manager._has_capacity_for(next_video))
        self.assertEqual(manager.workers, [])
        self.assertTrue(all(worker._slot_released for worker in stale_workers))

        available_tokens = 0
        while manager.slot_semaphore.acquire(blocking=False):
            available_tokens += 1
        self.assertEqual(available_tokens, 3)

    def test_lightweight_downloads_can_dispatch_above_video_limit(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.max_concurrent = 3
        manager.image_respects_concurrency = False
        manager._workers_lock = threading.RLock()
        manager.workers = []
        for index in range(3):
            video = VideoItem(url=f"https://example.com/{index}.mp4", title="video", source="douyin")
            video.meta["content_type"] = "video"
            manager.workers.append(SimpleNamespace(video=video))

        image = VideoItem(url="https://example.com/image.jpg", title="image", source="douyin")
        image.meta["content_type"] = "image"
        heavy_video = VideoItem(url="https://example.com/next.mp4", title="next", source="douyin")
        heavy_video.meta["content_type"] = "video"

        self.assertFalse(manager._has_dispatch_capacity())
        self.assertTrue(manager._has_any_dispatch_capacity())
        self.assertTrue(manager._has_capacity_for(image))
        self.assertFalse(manager._has_capacity_for(heavy_video))

    def test_lightweight_downloads_can_respect_regular_concurrency_when_enabled(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.max_concurrent = 3
        manager.image_respects_concurrency = True
        manager._workers_lock = threading.RLock()
        manager.workers = []
        for index in range(3):
            image_worker = VideoItem(url=f"https://example.com/{index}.jpg", title="image", source="xiaohongshu")
            image_worker.meta["content_type"] = "image"
            manager.workers.append(SimpleNamespace(video=image_worker))

        next_image = VideoItem(url="https://example.com/next.jpg", title="next", source="xiaohongshu")
        next_image.meta["content_type"] = "image"

        self.assertEqual(manager._slot_capacity(), 3)
        self.assertFalse(manager._has_any_dispatch_capacity())
        self.assertFalse(manager._has_capacity_for(next_image))

    def test_drain_closes_the_queue_against_later_puts(self):
        pending = PendingDownloadQueue()
        item = VideoItem(url="https://example.com/queued.mp4", title="queued", source="douyin")
        pending.put((item, "downloads"))

        drained = pending.drain()

        self.assertEqual([video.id for video, _save_dir in drained], [item.id])
        self.assertTrue(pending.closed)
        with self.assertRaises(RuntimeError):
            pending.put((item, "downloads"))

    def test_stop_all_drains_again_after_the_dispatcher_joins(self):
        class LateQueue:
            def __init__(self):
                self.items = ["first"]
                self.drain_calls = 0

            def drain(self):
                self.drain_calls += 1
                items = list(self.items)
                self.items.clear()
                if self.drain_calls == 1:
                    self.items.append("late")
                return items

        queue_owner = LateQueue()
        manager = self._stop_manager(queue_owner, [])

        with patch("app.core.download_shutdown_guardian.debug_logger.log"):
            stopped = manager.stop_all()

        self.assertIs(stopped, True)
        self.assertEqual(queue_owner.items, [])
        self.assertEqual(queue_owner.drain_calls, 2)
        self.assertTrue(manager._downloads_queue_closed)

    def test_capacity_requeue_drops_the_task_when_the_manager_has_stopped(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.queue = PendingDownloadQueue()
        manager._start_stop_lock = threading.RLock()
        manager.is_running = True
        video = VideoItem(url="https://example.com/capacity.mp4", title="capacity", source="douyin")

        self.assertTrue(manager._requeue_unstarted_task(video, "downloads", "capacity"))
        self.assertEqual(manager.queue.snapshot_video_ids(), {video.id})
        manager.queue.get_nowait()
        manager.is_running = False

        with patch("app.core.download_manager_core.debug_logger.log") as log:
            requeued = manager._requeue_unstarted_task(video, "downloads", "capacity")

        self.assertFalse(requeued)
        self.assertTrue(manager.queue.empty())
        self.assertIn(
            "discard_unstarted_task",
            [call.kwargs.get("action") for call in log.call_args_list],
        )

    def test_cancelled_completion_does_not_handoff_a_newer_registration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_dir = root / "downloads"
            save_dir.mkdir()
            store = DownloadRecoveryStore(db_path=root / "recovery.sqlite3")
            video = VideoItem(url="https://example.com/cancel.mp4", title="cancel", source="douyin")
            first = store.register_task(video_id=video.id, save_directory=save_dir)
            second = store.register_task(video_id=video.id, save_directory=save_dir)
            manager = DownloadManagerCore.__new__(DownloadManagerCore)
            manager._download_recovery_store = store
            manager._workers_lock = threading.RLock()
            manager.workers = []
            manager._slot_semaphore_lock = threading.RLock()
            manager.slot_semaphore = threading.BoundedSemaphore(1)
            manager._dispatch_slot_gate = threading.Event()
            worker = SimpleNamespace(
                video=video,
                _recovery_generation=first,
                _slot_released=True,
            )

            manager._handle_worker_completion(worker, "task_cancelled")

            self.assertNotEqual(first, second)
            self.assertIsNotNone(store.task_path(video.id))
            self.assertEqual(store.recovery_counts(), {"active": 1, "pending_cleanup": 0})

    def test_delete_failure_retries_and_then_leaves_the_row_for_confirmation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_dir = root / "downloads"
            save_dir.mkdir()
            store = DownloadRecoveryStore(db_path=root / "recovery.sqlite3")
            video = VideoItem(url="https://example.com/retry.mp4", title="retry", source="douyin")
            generation = store.register_task(video_id=video.id, save_directory=save_dir)
            manager = DownloadManagerCore.__new__(DownloadManagerCore)
            manager._download_recovery_store = store
            calls = {"n": 0}
            original = store.delete_task

            def flaky(video_id, task_generation=None):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise OSError("busy")
                return original(video_id, task_generation)

            store.delete_task = flaky
            manager._mark_download_recovery_state(video.id, "completed", generation)

            self.assertEqual(calls["n"], 3)
            self.assertIsNone(store.task_path(video.id))

            replacement = store.register_task(video_id=video.id, save_directory=save_dir)

            def always_fail(*_args, **_kwargs):
                raise OSError("still busy")

            store.delete_task = always_fail
            with patch("app.core.download_manager_core.debug_logger.log_exception"):
                manager._mark_download_recovery_state(video.id, "cancelled", replacement)

            self.assertIsNotNone(store.task_path(video.id))
            self.assertEqual(store.recovery_counts(), {"active": 1, "pending_cleanup": 0})

    def test_confirmed_acquire_is_preserved_when_concurrency_increases(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.max_concurrent = 1
        manager.image_respects_concurrency = True
        manager.image_fast_lane_limit = 10
        manager._start_stop_lock = threading.RLock()
        manager._slot_semaphore_lock = threading.RLock()
        manager._dispatch_slot_gate = threading.Event()
        manager._slot_semaphore_capacity = 1
        manager.slot_semaphore = threading.BoundedSemaphore(1)
        manager.is_running = True

        self.assertTrue(manager._acquire_dispatch_slot(0.2))
        manager.set_max_concurrent(5)

        available = 0
        while manager.slot_semaphore.acquire(blocking=False):
            available += 1
        self.assertEqual(available, 4)
        self.assertEqual(manager._slot_semaphore_capacity, 5)

    def test_overlapping_acquire_and_rebuild_do_not_lose_a_slot(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        manager.max_concurrent = 1
        manager.image_respects_concurrency = True
        manager.image_fast_lane_limit = 10
        manager._start_stop_lock = threading.RLock()
        manager._slot_semaphore_lock = threading.RLock()
        manager._dispatch_slot_gate = threading.Event()
        manager._dispatch_slot_gate.set()
        manager._slot_semaphore_capacity = 1
        manager.slot_semaphore = threading.BoundedSemaphore(1)
        manager.is_running = True
        stop = threading.Event()

        def hammer():
            while not stop.is_set():
                if manager._acquire_dispatch_slot(0.05):
                    manager._release_dispatch_slot("test")

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for thread in threads:
            thread.start()
        try:
            for value in (5, 1, 5, 3, 5):
                manager.set_max_concurrent(value)
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        available = 0
        while manager.slot_semaphore.acquire(blocking=False):
            available += 1
        self.assertEqual(available, manager._slot_semaphore_capacity)

    def test_startup_sweep_leaves_recovery_paths_outside_the_save_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            save_root = base / "save"
            outside = base / "outside"
            save_root.mkdir()
            outside.mkdir()
            inside_temp = save_root / "keep.mp4.downloading"
            outside_temp = outside / "leave.mp4.downloading"
            inside_temp.write_bytes(b"inside")
            outside_temp.write_bytes(b"outside")
            store = DownloadRecoveryStore(db_path=base / "recovery.sqlite3")
            store.register_task(video_id="inside", save_directory=save_root)
            store.handoff_failed_task("inside")
            store.register_task(video_id="outside", save_directory=outside)
            store.handoff_failed_task("outside")
            store.mark_legacy_sweep_complete(save_root)
            manager = DownloadManagerCore.__new__(DownloadManagerCore)
            manager._download_recovery_store = store
            maintenance_root = os.path.normcase(os.path.abspath(save_root))
            DownloadManagerCore._STARTUP_MAINTENANCE_ROOTS.discard(maintenance_root)

            with patch("app.core.download_manager_core.cfg.get", return_value=str(save_root)):
                stats = manager._sweep_m3u8_orphan_workspaces_on_startup()

            self.assertFalse(inside_temp.exists())
            self.assertTrue(outside_temp.exists())
            self.assertEqual(stats["recovery_records_consumed"], 1)
            self.assertEqual(store.recovery_counts(), {"active": 0, "pending_cleanup": 1})
            self.assertEqual(store.directories(), [os.path.normcase(str(outside.resolve()))])

    def test_startup_sweep_does_not_consume_when_enumeration_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            save_root = base / "save"
            outside = base / "outside"
            save_root.mkdir()
            outside.mkdir()
            outside_temp = outside / "leave.mp4.downloading"
            outside_temp.write_bytes(b"outside")

            class BrokenStore:
                def recovery_batch(self):
                    raise OSError("unreadable ledger")

                def consume_recovery_records(self, *_args, **_kwargs):
                    raise AssertionError("enumeration failure must not consume")

            manager = DownloadManagerCore.__new__(DownloadManagerCore)
            manager._download_recovery_store = BrokenStore()
            maintenance_root = os.path.normcase(os.path.abspath(save_root))
            DownloadManagerCore._STARTUP_MAINTENANCE_ROOTS.discard(maintenance_root)

            with patch("app.core.download_manager_core.cfg.get", return_value=str(save_root)):
                stats = manager._sweep_m3u8_orphan_workspaces_on_startup()

            self.assertEqual(stats["recovery_records_consumed"], 0)
            self.assertTrue(outside_temp.exists())

    def test_startup_sweep_keeps_a_record_when_its_directory_scan_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            save_root = base / "save"
            good = save_root / "good"
            bad = save_root / "bad"
            good.mkdir(parents=True)
            bad.mkdir()
            store = DownloadRecoveryStore(db_path=base / "recovery.sqlite3")
            store.register_task(video_id="good", save_directory=good)
            store.handoff_failed_task("good")
            store.register_task(video_id="bad", save_directory=bad)
            store.handoff_failed_task("bad")
            store.mark_legacy_sweep_complete(save_root)
            manager = DownloadManagerCore.__new__(DownloadManagerCore)
            manager._download_recovery_store = store
            maintenance_root = os.path.normcase(os.path.abspath(save_root))
            DownloadManagerCore._STARTUP_MAINTENANCE_ROOTS.discard(maintenance_root)

            def scan(directory, *_args, **_kwargs):
                if "bad" in str(directory):
                    return SimpleNamespace(
                        removed_count=0,
                        error="denied",
                        children=[],
                        truncated=False,
                        scanned_entries=0,
                    )
                return SimpleNamespace(
                    removed_count=0,
                    error="",
                    children=[],
                    truncated=False,
                    scanned_entries=1,
                )

            with patch("app.core.download_manager_core.cfg.get", return_value=str(save_root)), patch(
                "app.services.file_service.MediaLibraryService.sweep_orphan_download_temp_directory",
                side_effect=scan,
            ):
                stats = manager._sweep_m3u8_orphan_workspaces_on_startup()

            self.assertEqual(stats["recovery_records_consumed"], 1)
            self.assertEqual(store.recovery_counts(), {"active": 0, "pending_cleanup": 1})
            self.assertEqual(store.directories(), [os.path.normcase(str(bad.resolve()))])

    def test_startup_sweep_rejects_a_drive_root(self):
        manager = DownloadManagerCore.__new__(DownloadManagerCore)
        drive_root = Path(tempfile.gettempdir()).anchor

        with patch("app.core.download_manager_core.cfg.get", return_value=drive_root), patch(
            "app.core.downloaders.m3u8.N_m3u8DL_RE_Downloader.sweep_orphaned_workspaces"
        ) as hls_sweep, patch(
            "app.services.file_service.MediaLibraryService.sweep_orphan_download_temp_directory"
        ) as directory_sweep, patch(
            "app.services.file_service.MediaLibraryService.sweep_orphan_download_temp_artifacts"
        ) as artifact_sweep:
            stats = manager._sweep_m3u8_orphan_workspaces_on_startup()

        self.assertTrue(stats["rejected_drive_root"])
        hls_sweep.assert_not_called()
        directory_sweep.assert_not_called()
        artifact_sweep.assert_not_called()

    def test_startup_listing_limit_does_not_consume_or_delete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            save_root = base / "save"
            save_root.mkdir()
            leftover = save_root / "clip.mp4.downloading"
            leftover.write_bytes(b"partial")
            (save_root / "other.bin").write_bytes(b"x")
            store = DownloadRecoveryStore(db_path=base / "recovery.sqlite3")
            store.register_task(video_id="partial", save_directory=save_root)
            store.handoff_failed_task("partial")
            store.mark_legacy_sweep_complete(save_root)
            manager = DownloadManagerCore.__new__(DownloadManagerCore)
            manager._download_recovery_store = store
            maintenance_root = os.path.normcase(os.path.abspath(save_root))
            DownloadManagerCore._STARTUP_MAINTENANCE_ROOTS.discard(maintenance_root)

            with patch("app.core.download_manager_core.cfg.get", return_value=str(save_root)), patch(
                "app.core.download_path_policy.STARTUP_DIRECTORY_ENTRY_LIMIT",
                1,
            ):
                stats = manager._sweep_m3u8_orphan_workspaces_on_startup()

            self.assertTrue(leftover.exists())
            self.assertEqual(stats["recovery_records_consumed"], 0)
            self.assertEqual(store.recovery_counts(), {"active": 0, "pending_cleanup": 1})

    def test_startup_hls_listing_failure_does_not_consume(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            save_root = base / "save"
            save_root.mkdir()
            store = DownloadRecoveryStore(db_path=base / "recovery.sqlite3")
            store.register_task(video_id="partial", save_directory=save_root)
            store.handoff_failed_task("partial")
            store.mark_legacy_sweep_complete(save_root)
            manager = DownloadManagerCore.__new__(DownloadManagerCore)
            manager._download_recovery_store = store
            maintenance_root = os.path.normcase(os.path.abspath(save_root))
            DownloadManagerCore._STARTUP_MAINTENANCE_ROOTS.discard(maintenance_root)

            with patch("app.core.download_manager_core.cfg.get", return_value=str(save_root)), patch(
                "app.core.downloaders.m3u8.N_m3u8DL_RE_Downloader.sweep_orphaned_workspaces",
                side_effect=OSError("hls listing failed"),
            ):
                stats = manager._sweep_m3u8_orphan_workspaces_on_startup()

            self.assertEqual(stats["recovery_records_consumed"], 0)
            self.assertEqual(store.recovery_counts(), {"active": 0, "pending_cleanup": 1})


if __name__ == "__main__":
    unittest.main()
