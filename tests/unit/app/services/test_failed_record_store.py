from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
import sqlite3
import threading
import time
from contextlib import closing

import pytest

from app.services.failed_record_store import FailedRecordStore


def _wait_for_worker_idle(store: FailedRecordStore, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with store._lock:
            if (
                not store._writing
                and not store._refreshing
                and not store._pruning
                and store._refresh_requested is None
                and store._prune_retention_days is None
            ):
                return
        time.sleep(0.01)
    raise AssertionError("failed-record worker did not become idle")


def test_failed_record_store_persists_queued_records(tmp_path):
    store = FailedRecordStore(db_path=tmp_path / "failed.sqlite3")
    try:
        store.queue_upsert(
            [
                {
                    "id": "video-1",
                    "title": "broken",
                    "reason": "network",
                    "failed_at": "2026-07-06 10:00:00",
                    "status": "Failed",
                    "platform": "Bilibili",
                    "trace_id": "trace-1",
                }
            ]
        )

        assert store.flush(timeout=2)
        rows = store.query(limit=10)
    finally:
        store.shutdown()

    assert len(rows) == 1
    assert rows[0]["id"] == "video-1"
    assert rows[0]["title"] == "broken"
    assert rows[0]["reason"] == "network"
    assert rows[0]["trace_id"] == "trace-1"


def test_failed_record_store_uses_wal_and_full_synchronous_durability(tmp_path):
    db_path = tmp_path / "failed.sqlite3"
    store = FailedRecordStore(db_path=db_path)
    try:
        store.queue_upsert([{"id": "video-durable", "reason": "network"}])
        assert store.flush(timeout=2)
    finally:
        store.shutdown()

    with closing(sqlite3.connect(db_path)) as conn:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert int(conn.execute("PRAGMA synchronous").fetchone()[0]) == 2


def test_failed_record_store_refreshes_memory_snapshot_in_worker(tmp_path):
    # records_snapshot() 暴露给 UI 热路径，必须返回副本；外部改动不能污染
    # worker 线程维护的内存快照。
    refreshed_counts: list[int] = []
    store = FailedRecordStore(
        db_path=tmp_path / "failed.sqlite3",
        on_refresh=refreshed_counts.append,
    )
    try:
        store.queue_upsert(
            [
                {
                    "id": "video-2",
                    "title": "snapshot row",
                    "reason": "timeout",
                    "failed_at": "2026-07-06 11:00:00",
                    "status": "Failed",
                    "platform": "Bilibili",
                    "trace_id": "trace-2",
                }
            ]
        )

        assert store.flush(timeout=2)
        snapshot = store.records_snapshot()
        snapshot[0]["title"] = "mutated outside"
        snapshot_after_mutation = store.records_snapshot()
    finally:
        store.shutdown()

    assert refreshed_counts[-1] == 1
    assert snapshot[0]["id"] == "video-2"
    assert snapshot_after_mutation[0]["title"] == "snapshot row"


def test_failed_record_store_query_records_uses_sql_filters_and_counts(tmp_path):
    store = FailedRecordStore(db_path=tmp_path / "failed.sqlite3")
    try:
        store.queue_upsert(
            [
                {
                    "id": "bili-old",
                    "title": "old timeout",
                    "reason": "network timeout",
                    "failed_at": "2026-07-06 09:00:00",
                    "status": "Failed",
                    "platform": "Bilibili",
                    "trace_id": "trace-old",
                },
                {
                    "id": "bili-new",
                    "title": "new timeout",
                    "reason": "network timeout",
                    "failed_at": "2026-07-06 10:00:00",
                    "status": "Failed",
                    "platform": "Bilibili",
                    "trace_id": "trace-new",
                },
                {
                    "id": "douyin-row",
                    "title": "unrelated",
                    "reason": "auth",
                    "failed_at": "2026-07-06 11:00:00",
                    "status": "Failed",
                    "platform": "Douyin",
                    "trace_id": "trace-dy",
                },
            ]
        )

        assert store.flush(timeout=2)
        page = store.query_records(
            limit=1,
            offset=0,
            platform="Bilibili",
            keyword="timeout",
            failed_from="2026-07-06 09:30:00",
        )
    finally:
        store.shutdown()

    assert page.total_count == 1
    assert page.limit == 1
    assert page.offset == 0
    assert [row["id"] for row in page.records] == ["bili-new"]


def test_failed_record_store_worker_refresh_accepts_structured_query(tmp_path):
    # request_refresh 走后台 worker 查询 SQLite；这里覆盖平台和 trace 条件能
    # 防止 UI 重新退回同步 query_records。
    store = FailedRecordStore(db_path=tmp_path / "failed.sqlite3")
    try:
        store.queue_upsert(
            [
                {
                    "id": "trace-a",
                    "title": "first",
                    "reason": "network",
                    "failed_at": "2026-07-06 08:00:00",
                    "status": "Failed",
                    "platform": "Bilibili",
                    "trace_id": "bili-trace-a",
                },
                {
                    "id": "trace-b",
                    "title": "second",
                    "reason": "network",
                    "failed_at": "2026-07-06 08:01:00",
                    "status": "Failed",
                    "platform": "Bilibili",
                    "trace_id": "bili-trace-b",
                },
                {
                    "id": "trace-c",
                    "title": "third",
                    "reason": "network",
                    "failed_at": "2026-07-06 08:02:00",
                    "status": "Failed",
                    "platform": "MissAV",
                    "trace_id": "missav-trace-c",
                },
            ]
        )

        assert store.flush(timeout=2)
        store.request_refresh(limit=10, platform="Bilibili", trace_query="trace")
        assert store.flush(timeout=2)
        snapshot = store.records_snapshot()
        total_count = store.snapshot_total_count
    finally:
        store.shutdown()

    assert total_count == 2
    assert [row["id"] for row in snapshot] == ["trace-b", "trace-a"]


def test_failed_record_store_retries_transient_write_before_reporting_drained(tmp_path):
    db_path = tmp_path / "failed.sqlite3"
    store = FailedRecordStore(db_path=db_path)
    original_write_batch = store._write_batch
    retry_started = threading.Event()
    allow_commit = threading.Event()
    attempts = 0

    def flaky_write_batch(records: list[dict]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is temporarily locked")
        retry_started.set()
        assert allow_commit.wait(timeout=2)
        original_write_batch(records)

    try:
        store._write_retry_base_seconds = 0.01
        store._write_retry_max_seconds = 0.02
        store._write_batch = flaky_write_batch  # type: ignore[method-assign]
        store.queue_upsert(
            [
                {
                    "id": "video-retried",
                    "title": "accepted row",
                    "reason": "transient storage failure",
                    "failed_at": "2026-07-06 12:00:00",
                    "status": "Failed",
                    "platform": "Bilibili",
                    "trace_id": "trace-retried",
                }
            ]
        )

        assert retry_started.wait(timeout=2)
        assert store.shutdown(timeout=0.01) is False
        allow_commit.set()
        assert store.shutdown(timeout=2.0) is True
    finally:
        allow_commit.set()
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        rows = reopened.query(limit=10)
    finally:
        reopened.shutdown()

    assert attempts == 2
    assert [(row["id"], row["title"]) for row in rows] == [("video-retried", "accepted row")]


def test_failed_record_store_deletes_single_record_and_refreshes_snapshot(tmp_path):
    refreshed_counts: list[int] = []
    store = FailedRecordStore(db_path=tmp_path / "failed.sqlite3", on_refresh=refreshed_counts.append)
    try:
        store.queue_upsert(
            [
                {
                    "id": "delete-me",
                    "title": "delete row",
                    "reason": "network",
                    "failed_at": "2026-07-06 12:00:00",
                    "status": "Failed",
                    "platform": "Bilibili",
                    "trace_id": "trace-delete",
                },
                {
                    "id": "keep-me",
                    "title": "keep row",
                    "reason": "network",
                    "failed_at": "2026-07-06 12:01:00",
                    "status": "Failed",
                    "platform": "Bilibili",
                    "trace_id": "trace-keep",
                },
            ]
        )

        assert store.flush(timeout=2)
        assert store.delete_record("delete-me") is True
        immediate_snapshot = store.records_snapshot()
        immediate_total = store.snapshot_total_count
        immediate_callback_count = refreshed_counts[-1]
        assert store.flush(timeout=2)
        rows = store.query(limit=10)
        snapshot = store.records_snapshot()
    finally:
        store.shutdown()

    assert [row["id"] for row in immediate_snapshot] == ["keep-me"]
    assert immediate_total == 1
    assert immediate_callback_count == 1
    assert [row["id"] for row in rows] == ["keep-me"]
    assert [row["id"] for row in snapshot] == ["keep-me"]


def test_failed_record_store_clear_records_removes_all_and_refreshes_snapshot(tmp_path):
    refreshed_counts: list[int] = []
    store = FailedRecordStore(db_path=tmp_path / "failed.sqlite3", on_refresh=refreshed_counts.append)
    try:
        store.queue_upsert(
            [
                {"id": "clear-a", "title": "A", "reason": "network", "failed_at": "2026-07-06 12:00:00"},
                {"id": "clear-b", "title": "B", "reason": "network", "failed_at": "2026-07-06 12:01:00"},
            ]
        )

        assert store.flush(timeout=2)
        assert store.clear_records() == 2
        immediate_snapshot = store.records_snapshot()
        immediate_total = store.snapshot_total_count
        immediate_callback_count = refreshed_counts[-1]
        assert store.flush(timeout=2)
        rows = store.query(limit=10)
        snapshot = store.records_snapshot()
    finally:
        store.shutdown()

    assert immediate_snapshot == []
    assert immediate_total == 0
    assert immediate_callback_count == 0
    assert rows == []
    assert snapshot == []


def test_deleted_record_can_be_recorded_again_after_a_later_failure(tmp_path):
    store = FailedRecordStore(db_path=tmp_path / "failed.sqlite3")
    try:
        store.queue_upsert([{"id": "retry-failed", "reason": "first failure"}])
        assert store.flush(timeout=2)
        assert store.delete_record("retry-failed") is True
        assert store.flush(timeout=2)

        store.queue_upsert([{"id": "retry-failed", "reason": "later failure"}])
        assert store.flush(timeout=2)
        rows = store.query(limit=10)
    finally:
        store.shutdown()

    assert [(row["id"], row["reason"]) for row in rows] == [("retry-failed", "later failure")]


def test_cleared_record_can_be_recorded_again_after_a_later_failure(tmp_path):
    store = FailedRecordStore(db_path=tmp_path / "failed.sqlite3")
    try:
        store.queue_upsert([{"id": "clear-then-fail", "reason": "first failure"}])
        assert store.flush(timeout=2)
        assert store.clear_records() == 1
        assert store.flush(timeout=2)

        store.queue_upsert([{"id": "clear-then-fail", "reason": "later failure"}])
        assert store.flush(timeout=2)
        rows = store.query(limit=10)
    finally:
        store.shutdown()

    assert [(row["id"], row["reason"]) for row in rows] == [("clear-then-fail", "later failure")]


def test_delete_record_cancels_an_upsert_that_has_not_reached_sqlite(tmp_path):
    store = FailedRecordStore(db_path=tmp_path / "failed.sqlite3")
    original_ensure_worker = store._ensure_worker_locked
    try:
        # Hold the worker at the queue boundary to make the delete/upsert race deterministic.
        store._ensure_worker_locked = lambda: None  # type: ignore[method-assign]
        store.queue_upsert([{"id": "pending-delete", "title": "pending"}])

        assert store.delete_record("pending-delete") is True

        store._ensure_worker_locked = original_ensure_worker  # type: ignore[method-assign]
        store.request_refresh()
        assert store.flush(timeout=2)
        assert store.query(limit=10) == []
    finally:
        store._ensure_worker_locked = original_ensure_worker  # type: ignore[method-assign]
        store.shutdown()


def test_delete_record_cancels_an_upsert_already_dispatched_to_the_worker(tmp_path):
    store = FailedRecordStore(db_path=tmp_path / "failed.sqlite3")
    original_write_batch = store._write_batch
    started = threading.Event()
    release = threading.Event()

    def delayed_write(records: list[dict]) -> None:
        started.set()
        assert release.wait(timeout=2)
        original_write_batch(records)

    try:
        store._write_batch = delayed_write  # type: ignore[method-assign]
        store.queue_upsert([{"id": "inflight-delete", "title": "in flight"}])
        assert started.wait(timeout=2)

        assert store.delete_record("inflight-delete") is True

        release.set()
        assert store.flush(timeout=2)
        assert store.query(limit=10) == []
    finally:
        release.set()
        store.shutdown()


def test_failed_record_store_prune_expired_records_removes_old_failed_at(tmp_path):
    old_failed_at = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
    fresh_failed_at = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    store = FailedRecordStore(db_path=tmp_path / "failed.sqlite3")
    try:
        store.queue_upsert(
            [
                {"id": "expired", "title": "old", "reason": "network", "failed_at": old_failed_at},
                {"id": "fresh", "title": "new", "reason": "network", "failed_at": fresh_failed_at},
            ]
        )

        assert store.flush(timeout=2)
        assert store.prune_expired(7) == 1
        assert store.flush(timeout=2)
        rows = store.query(limit=10)
    finally:
        store.shutdown()

    assert [row["id"] for row in rows] == ["fresh"]


def test_shutdown_drains_an_accepted_upsert_before_the_worker_starts(tmp_path):
    db_path = tmp_path / "failed.sqlite3"
    store = FailedRecordStore(db_path=db_path)
    original_ensure_worker = store._ensure_worker_locked
    try:
        # Keep accepted work in the admission slot until shutdown owns the drain.
        store._ensure_worker_locked = lambda: None  # type: ignore[method-assign]
        store.queue_upsert([{"id": "shutdown-pending", "reason": "network"}])
        store._ensure_worker_locked = original_ensure_worker  # type: ignore[method-assign]

        assert store.shutdown(timeout=2.0) is True
    finally:
        store._ensure_worker_locked = original_ensure_worker  # type: ignore[method-assign]
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        assert [row["id"] for row in reopened.query(limit=10)] == ["shutdown-pending"]
    finally:
        reopened.shutdown()


def test_shutdown_timeout_is_observable_while_accepted_work_keeps_draining(tmp_path):
    db_path = tmp_path / "failed.sqlite3"
    store = FailedRecordStore(db_path=db_path)
    original_write_batch = store._write_batch
    started = threading.Event()
    release = threading.Event()

    def delayed_write(records: list[dict]) -> None:
        started.set()
        assert release.wait(timeout=2)
        original_write_batch(records)

    try:
        store._write_batch = delayed_write  # type: ignore[method-assign]
        store.queue_upsert([{"id": "shutdown-inflight", "reason": "network"}])
        assert started.wait(timeout=2)

        assert store.shutdown(timeout=0.01) is False
        release.set()
        assert store.shutdown(timeout=2.0) is True
    finally:
        release.set()
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        assert [row["id"] for row in reopened.query(limit=10)] == ["shutdown-inflight"]
    finally:
        reopened.shutdown()


def test_shutdown_drains_an_accepted_prune_request(tmp_path):
    db_path = tmp_path / "failed.sqlite3"
    old_failed_at = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
    seed = FailedRecordStore(db_path=db_path)
    try:
        seed.queue_upsert([{"id": "expired-before-shutdown", "failed_at": old_failed_at}])
        assert seed.flush(timeout=2)
    finally:
        seed.shutdown()

    store = FailedRecordStore(db_path=db_path)
    original_ensure_worker = store._ensure_worker_locked
    try:
        store._ensure_worker_locked = lambda: None  # type: ignore[method-assign]
        store.request_prune(7)
        store._ensure_worker_locked = original_ensure_worker  # type: ignore[method-assign]

        assert store.shutdown(timeout=2.0) is True
    finally:
        store._ensure_worker_locked = original_ensure_worker  # type: ignore[method-assign]
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        assert reopened.query(limit=10) == []
    finally:
        reopened.shutdown()


def test_failed_batch_does_not_overwrite_newer_pending_value_for_same_id(tmp_path, monkeypatch):
    db_path = tmp_path / "failed.sqlite3"
    store = FailedRecordStore(db_path=db_path)
    original_write_batch = store._write_batch
    failure_log_started = threading.Event()
    release_failure_log = threading.Event()
    attempts = 0

    def flaky_write_batch(records: list[dict]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("transient write failure")
        original_write_batch(records)

    def blocking_log(*_args, **_kwargs) -> None:
        failure_log_started.set()
        assert release_failure_log.wait(timeout=2)

    monkeypatch.setattr("app.services.failed_record_store.debug_logger.log_exception", blocking_log)
    try:
        store._write_retry_base_seconds = 0.01
        store._write_retry_max_seconds = 0.02
        store._write_batch = flaky_write_batch  # type: ignore[method-assign]
        store.queue_upsert([{"id": "same-id", "title": "old", "reason": "first"}])
        assert failure_log_started.wait(timeout=2)

        store.queue_upsert([{"id": "same-id", "title": "new", "reason": "second"}])
        release_failure_log.set()
        assert store.shutdown(timeout=2.0) is True
    finally:
        release_failure_log.set()
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        rows = reopened.query(limit=10)
    finally:
        reopened.shutdown()

    assert [(row["id"], row["title"], row["reason"]) for row in rows] == [("same-id", "new", "second")]


def test_retry_commit_serializes_newer_same_id_admission_with_monotonic_sequence(tmp_path):
    retry_commit_entered = threading.Event()
    allow_retry_commit = threading.Event()
    newer_admission_finished = threading.Event()
    clock_calls = 0

    def blocking_clock() -> float:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 1:
            retry_commit_entered.set()
            assert allow_retry_commit.wait(timeout=2)
        return float(clock_calls)

    store = FailedRecordStore(db_path=tmp_path / "failed.sqlite3", clock=blocking_clock)
    original_write_batch = store._write_batch
    write_sequences: list[tuple[str, int]] = []
    attempts = 0

    def flaky_write_batch(records: list[dict]) -> None:
        nonlocal attempts
        attempts += 1
        write_sequences.extend(
            (str(record["title"]), int(record["_store_record_generation"])) for record in records
        )
        if attempts == 1:
            raise sqlite3.OperationalError("force old row through retry path")
        original_write_batch(records)

    def admit_newer_record() -> None:
        store.queue_upsert([{"id": "same-id", "title": "new"}])
        newer_admission_finished.set()

    admission_thread = threading.Thread(target=admit_newer_record)
    try:
        store._write_retry_base_seconds = 0.01
        store._write_retry_max_seconds = 0.02
        store._write_batch = flaky_write_batch  # type: ignore[method-assign]
        store.queue_upsert([{"id": "same-id", "title": "old"}])
        assert retry_commit_entered.wait(timeout=2)

        admission_thread.start()
        assert newer_admission_finished.wait(timeout=0.05) is False
        allow_retry_commit.set()
        assert newer_admission_finished.wait(timeout=2)
        admission_thread.join(timeout=2)
        assert admission_thread.is_alive() is False
        assert store.shutdown(timeout=2.0) is True
    finally:
        allow_retry_commit.set()
        if admission_thread.is_alive():
            admission_thread.join(timeout=2)
        store.shutdown()

    reopened = FailedRecordStore(db_path=tmp_path / "failed.sqlite3")
    try:
        row = reopened.record_by_id("same-id")
    finally:
        reopened.shutdown()

    assert row is not None
    assert row["title"] == "new"
    assert write_sequences[0] == write_sequences[1]
    assert write_sequences[-1][0] == "new"
    assert write_sequences[-1][1] > write_sequences[0][1]


def test_delete_wins_over_failed_inflight_batch_retry(tmp_path, monkeypatch):
    db_path = tmp_path / "failed.sqlite3"
    store = FailedRecordStore(db_path=db_path)
    failure_log_started = threading.Event()
    release_failure_log = threading.Event()

    def failed_write(_records: list[dict]) -> None:
        raise sqlite3.OperationalError("transient write failure")

    def blocking_log(*_args, **_kwargs) -> None:
        failure_log_started.set()
        assert release_failure_log.wait(timeout=2)

    monkeypatch.setattr("app.services.failed_record_store.debug_logger.log_exception", blocking_log)
    try:
        store._write_batch = failed_write  # type: ignore[method-assign]
        store.queue_upsert([{"id": "delete-during-retry", "title": "stale"}])
        assert failure_log_started.wait(timeout=2)

        assert store.delete_record("delete-during-retry") is True
        release_failure_log.set()
        assert store.shutdown(timeout=2.0) is True
    finally:
        release_failure_log.set()
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        assert reopened.query(limit=10) == []
    finally:
        reopened.shutdown()


def test_clear_wins_over_failed_inflight_batch_retry(tmp_path, monkeypatch):
    db_path = tmp_path / "failed.sqlite3"
    store = FailedRecordStore(db_path=db_path)
    failure_log_started = threading.Event()
    release_failure_log = threading.Event()

    def failed_write(_records: list[dict]) -> None:
        raise OSError("transient filesystem failure")

    def blocking_log(*_args, **_kwargs) -> None:
        failure_log_started.set()
        assert release_failure_log.wait(timeout=2)

    monkeypatch.setattr("app.services.failed_record_store.debug_logger.log_exception", blocking_log)
    try:
        store._write_batch = failed_write  # type: ignore[method-assign]
        store.queue_upsert([{"id": "clear-during-retry", "title": "stale"}])
        assert failure_log_started.wait(timeout=2)

        assert store.clear_records() == 0
        release_failure_log.set()
        assert store.shutdown(timeout=2.0) is True
    finally:
        release_failure_log.set()
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        assert reopened.query(limit=10) == []
    finally:
        reopened.shutdown()


class _UnserializablePayload:
    def __deepcopy__(self, _memo):
        raise TypeError("must not deepcopy untrusted payload")

    def __str__(self) -> str:
        raise RuntimeError("must not stringify untrusted payload")


class _LateIdentityMapping(Mapping[str, object]):
    def __init__(self) -> None:
        self.items_yielded = 0

    def __getitem__(self, key: str) -> object:
        if key == "id":
            return "late-identity"
        if key == "title":
            return "identity survives bounded sampling"
        raise KeyError(key)

    def __iter__(self):
        for index in range(10_000):
            yield f"extra-{index}"
        yield "id"
        yield "title"

    def __len__(self) -> int:
        return 10_002

    def items(self):
        for index in range(10_000):
            self.items_yielded += 1
            if self.items_yielded > 129:
                raise AssertionError("sanitizer consumed the mapping beyond its item budget")
            yield f"extra-{index}", index
        yield "id", "late-identity"
        yield "title", "identity survives bounded sampling"


class _OversizedSet(set[str]):
    def __len__(self) -> int:
        return 10_000

    def __iter__(self):
        raise AssertionError("oversized set must not be materialized")


class _InfiniteHostileList(list[int]):
    def __init__(self) -> None:
        super().__init__([0])
        self.items_yielded = 0

    def __getitem__(self, key):
        if isinstance(key, slice):
            return self._finite_slice_values()
        return super().__getitem__(key)

    def _finite_slice_values(self):
        for _ in range(10_000):
            self.items_yielded += 1
            yield self.items_yielded

    def __iter__(self):
        while True:
            self.items_yielded += 1
            yield self.items_yielded


class _CountingInfiniteIterable:
    def __init__(self) -> None:
        self.items_yielded = 0

    def __iter__(self):
        while True:
            self.items_yielded += 1
            yield self.items_yielded


class _InfiniteRecordList(list[Mapping[str, object]]):
    def __init__(self, stop: threading.Event) -> None:
        super().__init__()
        self._stop = stop
        self.records_yielded = 0

    def __iter__(self):
        while not self._stop.is_set():
            self.records_yielded += 1
            yield {
                "id": f"top-level-{self.records_yielded}",
                "title": f"record {self.records_yielded}",
            }
            time.sleep(0.001)


def test_queue_normalizes_unserializable_payload_to_bounded_json_before_admission(tmp_path):
    db_path = tmp_path / "failed.sqlite3"
    store = FailedRecordStore(db_path=db_path)
    try:
        store.queue_upsert(
            [
                {
                    "id": "safe-payload",
                    "title": "accepted",
                    "opaque": _UnserializablePayload(),
                    "unordered": {"second", "first"},
                    "oversized": "x" * 100_000,
                }
            ]
        )
        assert store.shutdown(timeout=2.0) is True
    finally:
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        row = reopened.record_by_id("safe-payload")
    finally:
        reopened.shutdown()

    assert row is not None
    assert row["opaque"] == "[unsupported]"
    assert row["unordered"] == ["first", "second"]
    assert len(row["oversized"]) <= 4096


def test_queue_preserves_late_identity_without_fully_iterating_mapping_items(tmp_path):
    db_path = tmp_path / "failed.sqlite3"
    record = _LateIdentityMapping()
    store = FailedRecordStore(db_path=db_path)
    try:
        store.queue_upsert([record])
        assert store.shutdown(timeout=2.0) is True
    finally:
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        row = reopened.record_by_id("late-identity")
    finally:
        reopened.shutdown()

    assert row is not None
    assert row["title"] == "identity survives bounded sampling"
    assert record.items_yielded <= 128


def test_queue_does_not_materialize_oversized_set_payload(tmp_path):
    db_path = tmp_path / "failed.sqlite3"
    store = FailedRecordStore(db_path=db_path)
    try:
        store.queue_upsert(
            [
                {
                    "id": "bounded-set",
                    "title": "accepted",
                    "huge": _OversizedSet({"seed"}),
                }
            ]
        )
        assert store.shutdown(timeout=2.0) is True
    finally:
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        row = reopened.record_by_id("bounded-set")
    finally:
        reopened.shutdown()

    assert row is not None
    assert row["huge"] == "[truncated]"


def test_queue_lazily_bounds_hostile_list_to_limit_plus_one_items(tmp_path):
    db_path = tmp_path / "failed.sqlite3"
    hostile_list = _InfiniteHostileList()
    store = FailedRecordStore(db_path=db_path)
    try:
        store.queue_upsert([{"id": "bounded-hostile-list", "items": hostile_list}])
        assert store.shutdown(timeout=2.0) is True
    finally:
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        row = reopened.record_by_id("bounded-hostile-list")
    finally:
        reopened.shutdown()

    assert row is not None
    assert hostile_list.items_yielded == 129
    assert row["items"][:3] == [1, 2, 3]
    assert row["items"][-1] == "[truncated]"


def test_queue_lazily_bounds_an_infinite_generator_to_limit_plus_one_items(tmp_path):
    db_path = tmp_path / "failed.sqlite3"
    yielded = 0

    def infinite_values():
        nonlocal yielded
        value = 0
        while True:
            value += 1
            yielded += 1
            yield value

    store = FailedRecordStore(db_path=db_path)
    try:
        store.queue_upsert([{"id": "bounded-generator", "items": infinite_values()}])
        assert store.shutdown(timeout=2.0) is True
    finally:
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        row = reopened.record_by_id("bounded-generator")
    finally:
        reopened.shutdown()

    assert row is not None
    assert yielded == 129
    assert row["items"][:3] == [1, 2, 3]
    assert row["items"][-1] == "[truncated]"


def test_queue_lazily_truncates_an_infinite_top_level_list_after_batch_limit_plus_one(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "failed.sqlite3"
    stop = threading.Event()
    records = _InfiniteRecordList(stop)
    admission_finished = threading.Event()
    admission_errors: list[BaseException] = []
    store = FailedRecordStore(db_path=db_path)

    def admit_records() -> None:
        try:
            store.queue_upsert(records)
        except BaseException as exc:
            admission_errors.append(exc)
        finally:
            admission_finished.set()

    monkeypatch.setattr(
        "app.services.failed_record_store._MAX_UPSERT_BATCH_RECORDS",
        3,
        raising=False,
    )
    admission_thread = threading.Thread(target=admit_records, daemon=True)
    try:
        admission_thread.start()
        assert admission_finished.wait(timeout=0.5)
        admission_thread.join(timeout=2)
        assert admission_thread.is_alive() is False
        assert admission_errors == []
        assert records.records_yielded == 4
        assert store.shutdown(timeout=2.0) is True
    finally:
        if not admission_finished.is_set():
            store.shutdown(timeout=0.1)
        stop.set()
        admission_thread.join(timeout=2)
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        rows = reopened.query(limit=10)
    finally:
        reopened.shutdown()

    assert sorted(row["id"] for row in rows) == ["top-level-1", "top-level-2", "top-level-3"]


def test_iterable_projection_stops_consuming_immediately_when_node_budget_is_exhausted():
    streams = [_CountingInfiniteIterable() for _ in range(5)]

    normalized = FailedRecordStore._normalize_record({"id": "node-budget", "items": streams})

    assert normalized["video_id"] == "node-budget"
    assert streams[0].items_yielded == 129
    assert streams[1].items_yielded == 129
    assert streams[2].items_yielded == 129
    assert 0 < streams[3].items_yielded < 129
    assert streams[4].items_yielded == 0
    assert sum(stream.items_yielded for stream in streams) <= 512


def test_queue_bounds_oversized_integer_without_losing_valid_identity(tmp_path):
    db_path = tmp_path / "failed.sqlite3"
    store = FailedRecordStore(db_path=db_path)
    try:
        store.queue_upsert(
            [
                {
                    "id": "bounded-integer",
                    "title": "accepted",
                    "huge": 10**5_000,
                }
            ]
        )
        assert store.shutdown(timeout=2.0) is True
    finally:
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        row = reopened.record_by_id("bounded-integer")
    finally:
        reopened.shutdown()

    assert row is not None
    assert row["title"] == "accepted"
    assert row["huge"] == "[integer-out-of-range]"


def test_final_json_encoding_failure_preserves_known_core_fields(tmp_path, monkeypatch):
    db_path = tmp_path / "failed.sqlite3"
    store = FailedRecordStore(db_path=db_path)

    def failed_json_encoding(*_args, **_kwargs):
        raise ValueError("simulated encoder failure")

    monkeypatch.setattr("app.services.failed_record_store.json.dumps", failed_json_encoding)
    try:
        store.queue_upsert(
            [
                {
                    "id": "encoder-failure",
                    "title": "core fields survive",
                    "reason": "transient encoder failure",
                }
            ]
        )
        assert store.shutdown(timeout=2.0) is True
    finally:
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        row = reopened.record_by_id("encoder-failure")
    finally:
        reopened.shutdown()

    assert row is not None
    assert row["title"] == "core fields survive"
    assert row["reason"] == "transient encoder failure"


def test_video_id_uses_reserved_budget_before_large_known_fields(tmp_path, monkeypatch):
    db_path = tmp_path / "failed.sqlite3"
    large_text = "x" * 4_096
    monkeypatch.setattr(
        "app.services.failed_record_store._MAX_PAYLOAD_TEXT_CHARS",
        4 * 4_096,
    )
    store = FailedRecordStore(db_path=db_path)
    try:
        store.queue_upsert(
            [
                {
                    "video_id": "fallback-essential-id",
                    "failed_at": large_text,
                    "platform": large_text,
                    "reason": large_text,
                    "status": large_text,
                    "title": large_text,
                    "trace_id": large_text,
                }
            ]
        )
        assert store.shutdown(timeout=2.0) is True
    finally:
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        row = reopened.record_by_id("fallback-essential-id")
    finally:
        reopened.shutdown()

    assert row is not None
    assert row["id"] == "fallback-essential-id"


def test_invalid_id_marker_yields_to_valid_video_id() -> None:
    normalized = FailedRecordStore._normalize_record(
        {
            "id": 10**5_000,
            "video_id": "valid-fallback",
        }
    )

    assert normalized["video_id"] == "valid-fallback"
    assert normalized["payload"]["id"] == "[integer-out-of-range]"


def test_invalid_id_markers_do_not_collide_as_database_primary_keys(tmp_path):
    db_path = tmp_path / "failed.sqlite3"
    store = FailedRecordStore(db_path=db_path)
    try:
        store.queue_upsert(
            [
                {"id": 10**5_000, "video_id": "fallback-a", "title": "first"},
                {"id": -(10**5_001), "video_id": "fallback-b", "title": "second"},
            ]
        )
        assert store.shutdown(timeout=2.0) is True
    finally:
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        rows = reopened.query(limit=10)
    finally:
        reopened.shutdown()

    assert sorted((row["id"], row["title"]) for row in rows) == [
        ("fallback-a", "first"),
        ("fallback-b", "second"),
    ]


def test_overlong_same_prefix_ids_are_rejected_in_favor_of_distinct_video_ids(tmp_path):
    db_path = tmp_path / "failed.sqlite3"
    shared_prefix = "x" * 4_096
    store = FailedRecordStore(db_path=db_path)
    try:
        store.queue_upsert(
            [
                {
                    "id": shared_prefix + "-first-tail",
                    "video_id": "fallback-first",
                    "title": "first",
                },
                {
                    "id": shared_prefix + "-second-tail",
                    "video_id": "fallback-second",
                    "title": "second",
                },
            ]
        )
        assert store.shutdown(timeout=2.0) is True
    finally:
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        rows = reopened.query(limit=10)
    finally:
        reopened.shutdown()

    assert sorted((row["id"], row["title"]) for row in rows) == [
        ("fallback-first", "first"),
        ("fallback-second", "second"),
    ]
    assert {row["id"] for row in rows} == {"fallback-first", "fallback-second"}


def test_lone_surrogates_are_persisted_safely_without_blocking_later_records(tmp_path):
    db_path = tmp_path / "failed.sqlite3"
    store = FailedRecordStore(db_path=db_path)
    store._write_retry_base_seconds = 0.01
    store._write_retry_max_seconds = 0.02
    try:
        store.queue_upsert(
            [
                {
                    "id": "bad\ud800identity",
                    "video_id": "surrogate-fallback",
                    "title": "title\ud800text",
                    "reason": "reason\udffftext",
                    "status": "status\ud800text",
                    "platform": "platform\udffftext",
                    "trace_id": "trace\ud800text",
                    "nested": {"value": "payload\udffftext"},
                }
            ]
        )
        store.queue_upsert([{"id": "after-surrogate", "title": "later record"}])
        assert store.shutdown(timeout=2.0) is True
    finally:
        if not store.shutdown(timeout=0.1):
            store.clear_records()
            store.shutdown(timeout=2.0)

    reopened = FailedRecordStore(db_path=db_path)
    try:
        surrogate_row = reopened.record_by_id("surrogate-fallback")
        later_row = reopened.record_by_id("after-surrogate")
    finally:
        reopened.shutdown()

    assert surrogate_row is not None
    assert later_row is not None
    assert surrogate_row["title"] == "title\ufffdtext"
    assert surrogate_row["reason"] == "reason\ufffdtext"
    assert surrogate_row["nested"]["value"] == "payload\ufffdtext"
    assert surrogate_row["id"] == "surrogate-fallback"
    assert surrogate_row["video_id"] == "surrogate-fallback"
    for value in surrogate_row.values():
        if isinstance(value, str):
            value.encode("utf-8")


def test_prune_success_logger_base_exception_does_not_kill_worker(tmp_path, monkeypatch):
    db_path = tmp_path / "failed.sqlite3"
    old_failed_at = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
    seed = FailedRecordStore(db_path=db_path)
    try:
        seed.queue_upsert([{"id": "expired", "failed_at": old_failed_at}])
        assert seed.shutdown(timeout=2.0) is True
    finally:
        seed.shutdown()

    logger_entered = threading.Event()
    release_logger = threading.Event()

    def hostile_log(*_args, **_kwargs) -> None:
        logger_entered.set()
        assert release_logger.wait(timeout=2)
        raise KeyboardInterrupt("hostile prune success logger")

    monkeypatch.setattr("app.services.failed_record_store.debug_logger.log", hostile_log)
    store = FailedRecordStore(db_path=db_path)
    try:
        store.request_prune(7)
        assert logger_entered.wait(timeout=2)
        worker = store._thread
        assert worker is not None
        release_logger.set()
        _wait_for_worker_idle(store)

        assert worker.is_alive()
        assert store.shutdown(timeout=2.0) is True
    finally:
        release_logger.set()
        store.shutdown()


def test_prune_failure_logger_base_exception_does_not_kill_worker(tmp_path, monkeypatch):
    logger_entered = threading.Event()
    release_logger = threading.Event()
    store = FailedRecordStore(db_path=tmp_path / "failed.sqlite3")

    def failed_prune(_retention_days: int) -> int:
        raise OSError("prune failed")

    def hostile_log(*_args, **_kwargs) -> None:
        logger_entered.set()
        assert release_logger.wait(timeout=2)
        raise KeyboardInterrupt("hostile prune failure logger")

    monkeypatch.setattr("app.services.failed_record_store.debug_logger.log_exception", hostile_log)
    store.prune_expired = failed_prune  # type: ignore[method-assign]
    try:
        store.request_prune(7)
        assert logger_entered.wait(timeout=2)
        worker = store._thread
        assert worker is not None
        release_logger.set()
        _wait_for_worker_idle(store)

        assert worker.is_alive()
        assert store.shutdown(timeout=2.0) is True
    finally:
        release_logger.set()
        store.shutdown()


def test_worker_refresh_catch_logger_base_exception_does_not_kill_worker(tmp_path, monkeypatch):
    logger_entered = threading.Event()
    release_logger = threading.Event()
    store = FailedRecordStore(db_path=tmp_path / "failed.sqlite3")

    class HostileStringError(RuntimeError):
        def __str__(self) -> str:
            raise KeyboardInterrupt("hostile exception string")

    def failed_refresh(_request) -> None:
        raise HostileStringError()

    def hostile_log(_component, _action, exc, **_kwargs) -> None:
        logger_entered.set()
        assert release_logger.wait(timeout=2)
        str(exc)

    monkeypatch.setattr("app.services.failed_record_store.debug_logger.log_exception", hostile_log)
    store._refresh_snapshot = failed_refresh  # type: ignore[method-assign]
    try:
        store.request_refresh()
        assert logger_entered.wait(timeout=2)
        worker = store._thread
        assert worker is not None
        release_logger.set()
        _wait_for_worker_idle(store)

        assert worker.is_alive()
        assert store.shutdown(timeout=2.0) is True
    finally:
        release_logger.set()
        store.shutdown()


def test_log_detail_string_base_exception_does_not_kill_worker(tmp_path):
    prune_called = threading.Event()
    store = FailedRecordStore(db_path=tmp_path / "failed.sqlite3")

    class HostileLogPath:
        def __str__(self) -> str:
            raise KeyboardInterrupt("hostile log detail path")

    def failed_prune(_retention_days: int) -> int:
        prune_called.set()
        raise OSError("prune failed")

    store._db_path = HostileLogPath()  # type: ignore[assignment]
    store.prune_expired = failed_prune  # type: ignore[method-assign]
    try:
        store.request_prune(7)
        assert prune_called.wait(timeout=2)
        worker = store._thread
        assert worker is not None
        _wait_for_worker_idle(store)

        assert worker.is_alive()
        assert store.shutdown(timeout=2.0) is True
    finally:
        store.shutdown()


def test_snapshot_query_failure_logger_base_exception_does_not_kill_worker(tmp_path, monkeypatch):
    logger_entered = threading.Event()
    release_logger = threading.Event()
    store = FailedRecordStore(db_path=tmp_path / "failed.sqlite3")

    def failed_query(_request):
        raise sqlite3.OperationalError("snapshot query failed")

    def hostile_log(*_args, **_kwargs) -> None:
        logger_entered.set()
        assert release_logger.wait(timeout=2)
        raise KeyboardInterrupt("hostile snapshot query logger")

    monkeypatch.setattr("app.services.failed_record_store.debug_logger.log_exception", hostile_log)
    store._query_rows = failed_query  # type: ignore[method-assign]
    try:
        store.request_refresh()
        assert logger_entered.wait(timeout=2)
        worker = store._thread
        assert worker is not None
        release_logger.set()
        _wait_for_worker_idle(store)

        assert worker.is_alive()
        assert store.shutdown(timeout=2.0) is True
    finally:
        release_logger.set()
        store.shutdown()


def test_refresh_callback_logger_base_exception_does_not_kill_worker(tmp_path, monkeypatch):
    logger_entered = threading.Event()
    release_logger = threading.Event()

    def failed_callback(_count: int) -> None:
        raise RuntimeError("refresh callback failed")

    def hostile_log(*_args, **_kwargs) -> None:
        logger_entered.set()
        assert release_logger.wait(timeout=2)
        raise KeyboardInterrupt("hostile callback logger")

    monkeypatch.setattr("app.services.failed_record_store.debug_logger.log_exception", hostile_log)
    store = FailedRecordStore(
        db_path=tmp_path / "failed.sqlite3",
        on_refresh=failed_callback,
    )
    try:
        store.queue_upsert([{"id": "callback-row", "reason": "network"}])
        assert logger_entered.wait(timeout=2)
        worker = store._thread
        assert worker is not None
        release_logger.set()
        _wait_for_worker_idle(store)

        assert worker.is_alive()
        assert store.shutdown(timeout=2.0) is True
    finally:
        release_logger.set()
        store.shutdown()


def test_persistent_write_failure_stays_undrained_with_bounded_retry_and_logging(tmp_path, monkeypatch):
    db_path = tmp_path / "failed.sqlite3"
    store = FailedRecordStore(db_path=db_path)
    original_write_batch = store._write_batch
    attempt_times: list[float] = []
    logged_failures: list[int] = []
    third_attempt = threading.Event()

    def failed_write(_records: list[dict]) -> None:
        attempt_times.append(time.monotonic())
        if len(attempt_times) >= 3:
            third_attempt.set()
        raise sqlite3.OperationalError("persistent write failure")

    def record_log(*_args, **_kwargs) -> None:
        logged_failures.append(1)

    monkeypatch.setattr("app.services.failed_record_store.debug_logger.log_exception", record_log)
    try:
        store._write_retry_base_seconds = 0.01
        store._write_retry_max_seconds = 0.04
        store._write_error_log_interval_seconds = 60.0
        store._write_batch = failed_write  # type: ignore[method-assign]
        store.queue_upsert([{"id": "persistent", "reason": "disk unavailable"}])

        assert third_attempt.wait(timeout=2)
        assert store.flush(timeout=0.01) is False
        assert len(logged_failures) == 1
        assert attempt_times[1] - attempt_times[0] >= 0.008
        assert attempt_times[2] - attempt_times[1] >= 0.016

        store._write_batch = original_write_batch  # type: ignore[method-assign]
        store._event.set()
        assert store.shutdown(timeout=2.0) is True
    finally:
        store._write_batch = original_write_batch  # type: ignore[method-assign]
        store._event.set()
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        assert [row["id"] for row in reopened.query(limit=10)] == ["persistent"]
    finally:
        reopened.shutdown()


@pytest.mark.parametrize("logger_error_type", [RuntimeError, KeyboardInterrupt])
def test_hostile_failure_logger_cannot_kill_worker_or_drop_retried_batch(
    tmp_path,
    monkeypatch,
    logger_error_type,
):
    db_path = tmp_path / f"failed-{logger_error_type.__name__}.sqlite3"
    store = FailedRecordStore(db_path=db_path)
    original_write_batch = store._write_batch
    retry_started = threading.Event()
    allow_commit = threading.Event()
    attempts = 0

    def flaky_write_batch(records: list[dict]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("transient write failure")
        retry_started.set()
        assert allow_commit.wait(timeout=2)
        original_write_batch(records)

    def hostile_log(*_args, **_kwargs) -> None:
        raise logger_error_type("hostile logger")

    monkeypatch.setattr("app.services.failed_record_store.debug_logger.log_exception", hostile_log)
    try:
        store._write_retry_base_seconds = 0.01
        store._write_retry_max_seconds = 0.02
        store._write_batch = flaky_write_batch  # type: ignore[method-assign]
        store.queue_upsert([{"id": "hostile-logger", "reason": "network"}])

        assert retry_started.wait(timeout=2)
        with store._lock:
            assert store._thread is not None
            assert store._thread.is_alive()
        allow_commit.set()
        assert store.shutdown(timeout=2.0) is True
    finally:
        allow_commit.set()
        store.shutdown()

    reopened = FailedRecordStore(db_path=db_path)
    try:
        assert [row["id"] for row in reopened.query(limit=10)] == ["hostile-logger"]
    finally:
        reopened.shutdown()
