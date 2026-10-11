"""Fail-closed supervision for download-manager shutdown."""

from __future__ import annotations

import queue
import time
from typing import Any, Callable

from app.debug_logger import debug_logger

_REAL_SLEEP = time.sleep
_MIN_RETRY_SECONDS = 0.01
_MAX_RETRY_SECONDS = 0.25


def stop_download_manager(manager: Any) -> bool:
    """Run every stop step and retain the exact first operational failure."""

    first_failure: BaseException | None = None
    logged_failures: set[str] = set()

    def remember(action: str, exc: BaseException) -> None:
        nonlocal first_failure
        if first_failure is None:
            first_failure = exc
        if action in logged_failures:
            return
        logged_failures.add(action)
        try:
            debug_logger.log_exception("DownloadManager", action, exc)
        except BaseException:
            pass

    def attempt(action: str, callback: Callable[[], Any]) -> tuple[bool, Any]:
        try:
            return True, callback()
        except BaseException as exc:
            remember(action, exc)
            return False, None

    def log_safely(**kwargs: Any) -> None:
        try:
            debug_logger.log(**kwargs)
        except BaseException:
            pass

    def pause(seconds: float) -> None:
        try:
            time.sleep(seconds)
        except BaseException as exc:
            if "stop_all.retry_wait" not in logged_failures:
                logged_failures.add("stop_all.retry_wait")
                try:
                    debug_logger.log_exception("DownloadManager", "stop_all.retry_wait", exc)
                except BaseException:
                    pass
            _REAL_SLEEP(seconds)

    def log_discarded_items(items: Any) -> None:
        if not isinstance(items, (list, tuple)) or not items:
            return
        video_ids: list[Any] = []
        for item in items:
            try:
                video_ids.append(getattr(item[0], "id", None))
            except Exception:
                video_ids.append(None)
        log_safely(
            component="DownloadManager",
            action="stop_all_discard_unstarted",
            level="WARN",
            message="Discarded queued downloads that were not started",
            status_code="DL_STOP_DISCARD_UNSTARTED",
            details={"count": len(items), "video_ids": video_ids},
        )

    def drain_fallback() -> None:
        discarded: list[Any] = []
        while True:
            try:
                discarded.append(manager.queue.get_nowait())
            except queue.Empty:
                log_discarded_items(discarded)
                return
            except BaseException as exc:
                remember("stop_all.queue_fallback", exc)
                log_discarded_items(discarded)
                return

    def drain_queue(action: str) -> None:
        found, drain = attempt(
            f"{action}_lookup",
            lambda: getattr(manager.queue, "drain", None),
        )
        if found and callable(drain):
            drained, items = attempt(action, drain)
            if drained:
                log_discarded_items(items)
                return
            drain_fallback()
            return
        drain_fallback()

    def log_worker_timeout(worker: Any) -> None:
        video_id = trace_id = None
        try:
            video = worker.video
            video_id = video.id
            trace_id = video.meta.get("trace_id")
        except BaseException:
            pass
        log_safely(
            component="DownloadManager",
            action="stop_all_worker_timeout",
            level="WARN",
            message="Worker is still stopping; continuing supervised wait",
            status_code="DL_STOP_TIMEOUT",
            details={"video_id": video_id, "timeout_ms": manager.WORKER_STOP_TIMEOUT_MS},
            trace_id=trace_id,
        )

    with manager._start_stop_guard():
        manager.is_running = False
        found, startup_done = attempt(
            "stop_all.startup_gate_lookup",
            lambda: getattr(manager, "_startup_maintenance_done", None),
        )
        if found and startup_done is not None:
            attempt("stop_all.startup_gate", startup_done.set)
        attempt("stop_all.dispatch_gate", lambda: manager._get_dispatch_slot_gate().set())
        workers_ok, workers_snapshot = attempt(
            "stop_all.worker_snapshot",
            lambda: _snapshot_workers(manager),
        )
        workers_snapshot = workers_snapshot if workers_ok else []
        log_safely(
            component="DownloadManager",
            action="stop_all",
            level="WARN",
            message="Download manager stopping, draining queue and workers",
            status_code="DL_STOP_ALL",
            details={"active_workers": len(workers_snapshot)},
        )
        # 排空之后禁止再入队。调度线程里已取出但未启动的任务不能再 queue.put。
        manager._downloads_queue_closed = True
        drain_queue("stop_all.queue_drain")

    for worker in workers_snapshot:
        attempt("stop_all.worker_stop", lambda worker=worker: worker.stop())
    for worker in workers_snapshot:
        delay = _MIN_RETRY_SECONDS
        timeout_logged = False
        while True:
            waited, stopped = attempt(
                "stop_all.worker_wait",
                lambda worker=worker: worker.wait(manager.WORKER_STOP_TIMEOUT_MS),
            )
            if waited and stopped:
                break
            if waited and not timeout_logged:
                log_worker_timeout(worker)
                timeout_logged = True
            checked, finished = attempt(
                "stop_all.worker_liveness",
                lambda worker=worker: manager._worker_has_finished(worker),
            )
            if checked and finished:
                break
            pause(delay)
            delay = min(_MAX_RETRY_SECONDS, delay * 2.0)

    dispatcher_delay = _MIN_RETRY_SECONDS
    dispatcher_timeout_logged = False
    while True:
        joined, _ = attempt(
            "stop_all.dispatcher_join",
            lambda: manager.dispatcher_thread.join(timeout=2),
        )
        checked, alive = attempt(
            "stop_all.dispatcher_is_alive",
            manager.dispatcher_thread.is_alive,
        )
        if checked and not alive:
            break
        if not dispatcher_timeout_logged:
            log_safely(
                component="DownloadManager",
                action="stop_all_dispatcher_timeout",
                level="WARN",
                message="Dispatcher is still stopping; continuing supervised wait",
                status_code="DL_DISPATCHER_STOP_TIMEOUT",
            )
            dispatcher_timeout_logged = True
        pause(dispatcher_delay)
        dispatcher_delay = min(_MAX_RETRY_SECONDS, dispatcher_delay * 2.0)

    # 调度线程退出后可能还有它在 join 前塞回的任务，再排空一次。
    drain_queue("stop_all.queue_drain_after_join")

    if first_failure is not None:
        raise first_failure
    return True


def _snapshot_workers(manager: Any) -> list[Any]:
    with manager._workers_lock:
        return list(manager.workers)


__all__ = ["stop_download_manager"]
