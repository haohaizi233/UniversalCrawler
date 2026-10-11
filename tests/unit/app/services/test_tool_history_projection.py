from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from app.core.tools.contracts import (
    ToolManifest,
    ToolRequirements,
    ToolRunResult,
    ToolValidationResult,
)
from app.core.tools.registry import ToolRegistry
from app.services import tool_history_projection as projection_module
from app.services.tool_runner_service import ToolRunnerService
from shared.execution_profile import local_execution_profile


_CANDIDATE_KEYS = {
    "candidate_id",
    "display_url",
    "platform",
    "resource_kind",
    "format_hint",
    "expanded",
    "admittable",
    "admission_reason_code",
}


class _LinkParserProjectionTool:
    manifest = ToolManifest(
        id="link_parser",
        title="Link parser projection",
        summary="Return controlled candidate rows",
    )

    def __init__(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        failed: bool = False,
        message: str = "done",
    ) -> None:
        self._rows = list(rows)
        self._failed = failed
        self._message = message

    @staticmethod
    def requirements_for(parameters: Mapping[str, Any]) -> ToolRequirements:
        del parameters
        return ToolRequirements()

    @staticmethod
    def validate(context: Any) -> list[str]:
        del context
        return []

    def run(self, context: Any) -> ToolRunResult:
        del context
        data = {
            "links": self._rows,
            "path": "D:/private/result.json",
            "token": "top-level-secret",
        }
        if self._failed:
            return ToolRunResult.failure(self._message, data=data)
        return ToolRunResult.success(
            self._message,
            data=data,
            private_data={"token": "private-result-secret"},
        )


class _HostileStringError(RuntimeError):
    def __str__(self) -> str:
        raise KeyboardInterrupt("hostile exception string")


class _NoFullScanString(str):
    def __iter__(self):
        raise AssertionError("oversized source text must be sliced before scanning")


class _HostileValidationParameters(Mapping[str, Any]):
    def __getitem__(self, key: str) -> Any:
        raise KeyError(key)

    def __iter__(self):
        return iter(("private",))

    def __len__(self) -> int:
        return 1

    def items(self):
        raise SystemExit("private validation projection secret")


class _BoundaryTool:
    manifest = ToolManifest(
        id="boundary_tool",
        title="Boundary tool",
        summary="Exercise runner exception boundaries",
    )

    def __init__(self) -> None:
        self.settings_seen: list[dict[str, Any]] = []

    @staticmethod
    def requirements_for(parameters: Mapping[str, Any]) -> ToolRequirements:
        stage = parameters.get("fatal_stage")
        if stage == "requirements":
            raise _boundary_failure(str(parameters.get("fatal_kind") or "exception"))
        if stage == "requirements_normalized" and parameters.get("normalized") is True:
            raise _boundary_failure(str(parameters.get("fatal_kind") or "exception"))
        return ToolRequirements()

    @staticmethod
    def validate(context: Any) -> ToolValidationResult:
        stage = context.parameters.get("fatal_stage")
        if stage == "validate":
            raise _boundary_failure(
                str(context.parameters.get("fatal_kind") or "exception")
            )
        if stage == "validate_projection":
            return ToolValidationResult(
                True,
                (),
                (),
                _HostileValidationParameters(),
            )
        parameters = dict(context.parameters)
        if stage == "requirements_normalized":
            parameters["normalized"] = True
        return ToolValidationResult.ok(parameters=parameters)

    def run(self, context: Any) -> ToolRunResult:
        self.settings_seen.append(dict(context.settings))
        fatal_kind = context.parameters.get("worker_fatal")
        if fatal_kind:
            raise _boundary_failure(str(fatal_kind))
        return ToolRunResult.success("boundary run completed")


class _LargeHistoryTool:
    manifest = ToolManifest(
        id="large_history",
        title="Large history",
        summary="Return deliberately oversized public history fields",
    )

    def __init__(self, max_history_bytes: int) -> None:
        self.message = "m" * (max_history_bytes + 4096)
        self.warnings = tuple("w" * 2048 for _index in range(48))

    @staticmethod
    def requirements_for(parameters: Mapping[str, Any]) -> ToolRequirements:
        del parameters
        return ToolRequirements()

    @staticmethod
    def validate(context: Any) -> list[str]:
        del context
        return []

    def run(self, context: Any) -> ToolRunResult:
        del context
        return ToolRunResult.success(
            self.message,
            warnings=self.warnings,
        )


class _TerminalProjectionTool:
    manifest = ToolManifest(
        id="terminal_projection",
        title="Terminal projection",
        summary="Pause the first run at its terminal boundary",
    )

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    @staticmethod
    def requirements_for(parameters: Mapping[str, Any]) -> ToolRequirements:
        del parameters
        return ToolRequirements()

    @staticmethod
    def validate(context: Any) -> list[str]:
        del context
        return []

    def run(self, context: Any) -> ToolRunResult:
        del context
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            assert self.release.wait(2.0)
        return ToolRunResult.success("terminal success")


def _boundary_failure(kind: str) -> BaseException:
    if kind == "keyboard_interrupt":
        return KeyboardInterrupt("private keyboard interrupt secret")
    if kind == "system_exit":
        return SystemExit("private system exit secret")
    if kind == "hostile_string":
        return _HostileStringError("private hostile string secret")
    return RuntimeError("private exception secret")


def _registry(tool: Any) -> ToolRegistry:
    registry = ToolRegistry(
        tools=[],
        include_builtins=False,
        include_entry_points=False,
    )
    registry._register(tool, provenance="builtin")
    return registry


def _profile(tmp_path: Path, owner_id: str):
    return local_execution_profile(
        host_surface="test",
        owner_id=owner_id,
        approved_roots=(tmp_path,),
        tool_permissions=(),
        allow_external_plugins=False,
    )


def _candidate(candidate_id: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "candidate_id": candidate_id,
        "display_url": "https://www.bilibili.com/[redacted]",
        "platform": "bilibili",
        "resource_kind": "media",
        "format_hint": "MP4",
        "expanded": False,
        "admittable": True,
        "admission_reason_code": "",
    }
    row.update(overrides)
    return row


def test_link_parser_runtime_link_projector_is_reusable_for_data_or_rows() -> None:
    candidate = _candidate(
        "a" * 64,
        token="private-token",
        path="D:/private/video.mp4",
    )
    expected = {key: candidate[key] for key in _CANDIDATE_KEYS}
    future_candidate = _candidate(
        "b" * 64,
        platform="future-platform",
        admittable=False,
        admission_reason_code="future-reason.v2",
    )
    future_expected = {key: future_candidate[key] for key in _CANDIDATE_KEYS}
    projector = getattr(
        projection_module,
        "project_link_parser_runtime_links",
        None,
    )

    assert callable(projector)
    assert projector({"links": [candidate], "token": "private-token"}) == [expected]
    assert projector([candidate]) == [expected]
    assert projector([future_candidate]) == [future_expected]


def test_history_projection_slices_before_scanning_and_caps_warning_budget() -> None:
    huge_message = _NoFullScanString("m" * (2 * 1024 * 1024 + 4096))
    huge_warning = _NoFullScanString("w" * 2048)

    projected = projection_module.project_history_record(
        {
            "message": huge_message,
            "result": {
                "status": "succeeded",
                "message": huge_message,
                "warnings": [huge_warning] * 64,
            },
        }
    )

    warning_budget = getattr(
        projection_module,
        "MAX_HISTORY_WARNINGS_TOTAL_CHARS",
        None,
    )
    assert isinstance(warning_budget, int)
    assert len(projected["message"]) <= projection_module.MAX_HISTORY_MESSAGE_CHARS
    assert len(projected["result"]["message"]) <= (
        projection_module.MAX_HISTORY_MESSAGE_CHARS
    )
    warnings = projected["result"]["warnings"]
    assert len(warnings) <= projection_module.MAX_HISTORY_WARNINGS
    assert all(
        len(warning) <= projection_module.MAX_HISTORY_WARNING_CHARS
        for warning in warnings
    )
    assert sum(len(warning) for warning in warnings) <= warning_budget


def test_get_and_wait_expose_only_owner_scoped_runtime_candidate_projection(
    tmp_path: Path,
) -> None:
    history_path = tmp_path / "tool-history.json"
    candidate_id = "a" * 64
    source = _candidate(
        candidate_id,
        private_url="https://www.bilibili.com/private/video.mp4?token=row-secret",
        path="D:/private/video.mp4",
        query="token=row-secret",
        token="row-secret",
    )
    owner = _profile(tmp_path, "owner:one")
    other_owner = _profile(tmp_path, "owner:two")
    service = ToolRunnerService(
        registry=_registry(_LinkParserProjectionTool([source])),
        history_path=history_path,
        max_workers=1,
    )

    queued = service.run("link_parser", {}, execution_profile=owner)
    waited = service.wait_for_run(
        queued["run_id"], execution_profile=owner, timeout=2.0
    )
    fetched = service.get_run(queued["run_id"], execution_profile=owner)

    expected = {key: source[key] for key in _CANDIDATE_KEYS}
    assert waited["result"]["data"] == {"links": [expected]}
    assert fetched is not None
    assert fetched["result"]["data"] == {"links": [expected]}
    assert service.get_run(queued["run_id"], execution_profile=other_owner) is None
    owner_mismatch = service.wait_for_run(
        queued["run_id"], execution_profile=other_owner, timeout=0
    )
    assert owner_mismatch == {
        "status": "forbidden",
        "code": "tool_owner_mismatch",
        "message": "tool run belongs to another owner",
        "run_id": queued["run_id"],
    }

    history = service.history(execution_profile=owner)
    assert "data" not in history[0]["result"]
    assert service.shutdown(wait=True)

    persisted = json.loads(history_path.read_text(encoding="utf-8"))
    assert "data" not in persisted[0]["result"]
    public_text = json.dumps(
        {
            "waited": waited,
            "fetched": fetched,
            "history": history,
            "persisted": persisted,
        }
    )
    assert "row-secret" not in public_text
    assert "private-result-secret" not in public_text
    assert "D:/private" not in public_text


def test_loaded_history_cannot_restore_link_parser_runtime_result_data(
    tmp_path: Path,
) -> None:
    history_path = tmp_path / "loaded-private-history.json"
    owner = _profile(tmp_path, "owner:loaded")
    run_id = "loaded-link-parser-run"
    history_path.write_text(
        json.dumps(
            [
                {
                    "run_id": run_id,
                    "tool_id": "link_parser",
                    "host_surface": owner.host_surface,
                    "owner_id": owner.owner_id,
                    "status": "succeeded",
                    "created_at": 1.0,
                    "started_at": 1.0,
                    "finished_at": 2.0,
                    "progress": 100,
                    "message": "loaded",
                    "result": {
                        "status": "succeeded",
                        "message": "loaded",
                        "warnings": [],
                        "data": {
                            "links": [_candidate("a" * 64)],
                            "token": "loaded-private-token",
                        },
                        "output_paths": ["D:/loaded/private/result.json"],
                        "private_data": {"token": "loaded-private-token"},
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    service = ToolRunnerService(
        registry=_registry(_LinkParserProjectionTool([])),
        history_path=history_path,
    )

    fetched = service.get_run(run_id, execution_profile=owner)
    waited = service.wait_for_run(run_id, execution_profile=owner, timeout=0)

    assert fetched is not None
    assert "data" not in fetched["result"]
    assert "data" not in waited["result"]
    assert "data" not in service.history(execution_profile=owner)[0]["result"]
    assert service.lookup_private_result(run_id, execution_profile=owner) is None
    with service._lock:
        loaded = service._records[run_id]
        assert loaded.runtime_private_result is False
        assert loaded.result is not None
        assert loaded.result.data == {}
        assert loaded.result.output_paths == ()
        assert loaded.result.private_data == {}
    assert service.shutdown(wait=True, timeout=2.0)
    persisted_text = history_path.read_text(encoding="utf-8")
    assert "data" not in json.loads(persisted_text)[0]["result"]
    assert "loaded-private-token" not in persisted_text
    assert "D:/loaded/private" not in persisted_text


def test_service_generated_history_is_bounded_and_drops_oldest_snapshot_rows(
    tmp_path: Path,
) -> None:
    from app.services import tool_runner_service as runner_module

    max_history_bytes = runner_module.MAX_HISTORY_BYTES
    history_path = tmp_path / "bounded-service-history.json"
    owner = _profile(tmp_path, "owner:large-history")
    tool = _LargeHistoryTool(max_history_bytes)
    service = ToolRunnerService(
        registry=_registry(tool),
        history_path=history_path,
        history_limit=80,
        max_workers=1,
    )
    run_ids: list[str] = []
    try:
        for _index in range(100):
            queued = service.run("large_history", {}, execution_profile=owner)
            run_ids.append(queued["run_id"])
            terminal = service.wait_for_run(
                queued["run_id"], execution_profile=owner, timeout=2.0
            )
            assert terminal["status"] == "succeeded"
            assert len(terminal["message"]) <= 4096
            assert len(terminal["result"]["message"]) <= 4096
            assert len(terminal["result"]["warnings"]) <= 32
            assert all(
                len(warning) <= 1024
                for warning in terminal["result"]["warnings"]
            )
            assert sum(
                len(warning) for warning in terminal["result"]["warnings"]
            ) <= projection_module.MAX_HISTORY_WARNINGS_TOTAL_CHARS

        assert service.shutdown(wait=True, timeout=5.0)
        persisted = json.loads(history_path.read_text(encoding="utf-8"))
        persisted_ids = [row["run_id"] for row in persisted]
        assert 0 < len(persisted_ids) < len(run_ids)
        assert persisted_ids == run_ids[-len(persisted_ids) :]
        assert history_path.stat().st_size <= max_history_bytes
        assert service._persisted_generation == service._persist_generation
        assert service._persist_running is False
    finally:
        service.shutdown(wait=True, timeout=5.0)


def test_terminal_projection_failure_cleans_run_and_persists_fixed_terminal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.services import tool_runner_service as runner_module

    history_path = tmp_path / "terminal-projection-history.json"
    owner = _profile(tmp_path, "owner:terminal-projection")
    tool = _TerminalProjectionTool()
    service = ToolRunnerService(
        registry=_registry(tool),
        history_path=history_path,
        max_workers=1,
    )
    original_projection = runner_module._RunRecord.to_public_dict
    projection_failures = 0

    def fail_first_terminal_projection(record):
        nonlocal projection_failures
        if (
            record.tool_id == "terminal_projection"
            and record.finished_at is not None
            and projection_failures == 0
        ):
            projection_failures += 1
            raise SystemExit("private terminal projection secret")
        return original_projection(record)

    try:
        first = service.run("terminal_projection", {}, execution_profile=owner)
        assert tool.started.wait(2.0)
        recursive: dict[str, Any] = {}
        recursive["self"] = recursive
        with service._lock:
            record = service._records[first["run_id"]]
            record.parameters = {"discarded": recursive}
            record.progress_details = {"discarded": recursive}
        monkeypatch.setattr(
            runner_module._RunRecord,
            "to_public_dict",
            fail_first_terminal_projection,
        )
        tool.release.set()

        first_terminal = service.wait_for_run(
            first["run_id"], execution_profile=owner, timeout=2.0
        )
        assert first_terminal["status"] == "failed"
        assert first_terminal["message"] == "tool run failed"
        assert first_terminal["result"] == {
            "status": "failed",
            "message": "tool run failed",
            "warnings": [],
        }
        assert first["run_id"] not in service._futures
        assert first["run_id"] not in service._tokens

        second = service.run("terminal_projection", {}, execution_profile=owner)
        second_terminal = service.wait_for_run(
            second["run_id"], execution_profile=owner, timeout=2.0
        )
        assert second_terminal["status"] == "succeeded"
        assert service.wait_for_idle(timeout=2.0)
        assert service.shutdown(wait=True, timeout=2.0)

        persisted = json.loads(history_path.read_text(encoding="utf-8"))
        assert [row["status"] for row in persisted] == ["failed", "succeeded"]
        assert "private terminal projection secret" not in json.dumps(persisted)
    finally:
        tool.release.set()
        service.shutdown(wait=True, timeout=5.0)


def test_runtime_candidate_projection_drops_malicious_rows_and_bounds_valid_rows(
    tmp_path: Path,
) -> None:
    invalid_rows = [
        _candidate("1" * 64, display_url="https://example.com/private/video.mp4"),
        _candidate("2" * 64, display_url="https://example.com/[redacted]?token=secret"),
        _candidate("3" * 64, platform="douyin\nsecret"),
        _candidate("4" * 64, platform="x" * 65),
        _candidate("7" * 64, platform="bilibili\u202esecret"),
        _candidate("9" * 64, platform="bilibili\ud800secret"),
        _candidate("8" * 64, format_hint="VIDEO"),
        _candidate(
            "5" * 64,
            admittable=False,
            admission_reason_code="candidate_downloader_unavailable\rsecret",
        ),
        _candidate("not-a-candidate-id"),
        _candidate("6" * 64, admittable=False, admission_reason_code=""),
    ]
    valid_rows = [_candidate(f"{index:064x}") for index in range(1, 81)]
    duplicate = _candidate(valid_rows[0]["candidate_id"], platform="douyin")
    owner = _profile(tmp_path, "owner:bounded")
    service = ToolRunnerService(
        registry=_registry(
            _LinkParserProjectionTool([*invalid_rows, *valid_rows, duplicate])
        ),
        history_path=tmp_path / "bounded-history.json",
        max_workers=1,
    )

    queued = service.run("link_parser", {}, execution_profile=owner)
    terminal = service.wait_for_run(
        queued["run_id"], execution_profile=owner, timeout=2.0
    )
    links = terminal["result"]["data"]["links"]

    assert len(links) == 64
    assert [row["candidate_id"] for row in links] == [
        row["candidate_id"] for row in valid_rows[:64]
    ]
    assert all(set(row) == _CANDIDATE_KEYS for row in links)
    serialized = json.dumps(terminal)
    assert "private/video.mp4" not in serialized
    assert "token=secret" not in serialized
    assert "secret" not in serialized
    assert service.shutdown(wait=True)


def test_runtime_candidate_projection_is_absent_for_failed_link_parser(
    tmp_path: Path,
) -> None:
    owner = _profile(tmp_path, "owner:failed")
    service = ToolRunnerService(
        registry=_registry(
            _LinkParserProjectionTool([_candidate("a" * 64)], failed=True)
        ),
        history_path=tmp_path / "failed-history.json",
        max_workers=1,
    )

    queued = service.run("link_parser", {}, execution_profile=owner)
    terminal = service.wait_for_run(
        queued["run_id"], execution_profile=owner, timeout=2.0
    )

    assert terminal["status"] == "failed"
    assert "data" not in terminal["result"]
    assert service.shutdown(wait=True)


def test_surrogate_projection_and_encoding_failure_keep_dirty_generation_retryable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.services import tool_runner_service as runner_module

    history_path = tmp_path / "surrogate-history.json"
    owner = _profile(tmp_path, "owner:surrogate")
    first_write_started = threading.Event()
    release_first_write = threading.Event()
    original_atomic_write = runner_module._atomic_write_json
    attempts = 0

    def fail_first_write(
        path: Path,
        value: Any,
        *,
        cache_root: Path | None = None,
    ) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_write_started.set()
            assert release_first_write.wait(2.0)
            raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogate")
        original_atomic_write(path, value, cache_root=cache_root)

    monkeypatch.setattr(runner_module, "_atomic_write_json", fail_first_write)
    service = ToolRunnerService(
        registry=_registry(
            _LinkParserProjectionTool(
                [_candidate("a" * 64)],
                message="unsafe-\ud800-message",
            )
        ),
        history_path=history_path,
        max_workers=1,
    )
    try:
        first = service.run("link_parser", {}, execution_profile=owner)
        first_terminal = service.wait_for_run(
            first["run_id"], execution_profile=owner, timeout=2.0
        )
        assert first_write_started.wait(2.0)

        second = service.run("link_parser", {}, execution_profile=owner)
        second_terminal = service.wait_for_run(
            second["run_id"], execution_profile=owner, timeout=2.0
        )
        release_first_write.set()

        deadline = time.monotonic() + 2.0
        persisted: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            if history_path.is_file():
                persisted = json.loads(history_path.read_text(encoding="utf-8"))
                if len(persisted) == 2 and not service._persist_running:
                    break
            time.sleep(0.01)

        assert first_terminal["message"] == "unsafe-\ufffd-message"
        assert first_terminal["result"]["message"] == "unsafe-\ufffd-message"
        assert second_terminal["result"]["message"] == "unsafe-\ufffd-message"
        assert len(persisted) == 2
        assert service._persist_running is False
        persisted_text = json.dumps(persisted, ensure_ascii=False)
        assert "\ud800" not in persisted_text
        assert "unsafe-\ufffd-message" in persisted_text
        assert attempts >= 2
    finally:
        release_first_write.set()
        monkeypatch.setattr(
            runner_module,
            "_atomic_write_json",
            original_atomic_write,
        )
        assert service.shutdown(wait=True, timeout=2.0)


def test_same_generation_persist_retries_past_three_failures_without_a_new_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.services import tool_runner_service as runner_module

    history_path = tmp_path / "same-generation-history.json"
    service = ToolRunnerService(
        registry=_registry(_BoundaryTool()),
        history_path=history_path,
    )
    assert service._history_loaded.wait(2.0)
    original_atomic_write = runner_module._atomic_write_json
    attempts = 0
    logged_actions: list[str] = []

    def fail_five_times(
        path: Path,
        value: Any,
        *,
        cache_root: Path | None = None,
    ) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 5:
            raise OSError("transient write failure")
        original_atomic_write(path, value, cache_root=cache_root)

    def capture_log(_component: str, action: str, _exc: BaseException) -> None:
        logged_actions.append(action)

    monkeypatch.setattr(runner_module, "_atomic_write_json", fail_five_times)
    monkeypatch.setattr(runner_module.debug_logger, "log_exception", capture_log)
    generation_before = service._persist_generation
    try:
        service._schedule_persist()
        generation = generation_before + 1
        deadline = time.monotonic() + 2.0
        while service._persist_running and time.monotonic() < deadline:
            time.sleep(0.01)

        assert service._persist_generation == generation
        assert service._persisted_generation == generation
        assert attempts == 6
        assert logged_actions == ["persist_history"] * 5
        assert json.loads(history_path.read_text(encoding="utf-8")) == []
        assert service._persist_running is False
        service._schedule_persist(mark_dirty=False)
        time.sleep(0.05)
        assert attempts == 6
    finally:
        monkeypatch.setattr(
            runner_module,
            "_atomic_write_json",
            original_atomic_write,
        )
        assert service.shutdown(wait=True, timeout=2.0)


def test_serialization_failure_stops_worker_but_keeps_same_generation_dirty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.services import tool_runner_service as runner_module

    service = ToolRunnerService(
        registry=_registry(_BoundaryTool()),
        history_path=tmp_path / "permanent-history.json",
    )
    assert service._history_loaded.wait(2.0)
    original_atomic_write = runner_module._atomic_write_json
    attempts = 0
    failure_seen = threading.Event()

    def fail_write(
        _path: Path,
        _value: Any,
        *,
        cache_root: Path | None = None,
    ) -> None:
        del cache_root
        nonlocal attempts
        attempts += 1
        failure_seen.set()
        raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogate")

    monkeypatch.setattr(runner_module, "_atomic_write_json", fail_write)
    monkeypatch.setattr(
        runner_module.debug_logger,
        "log_exception",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(runner_module.debug_logger, "log", lambda **_kwargs: None)

    service._schedule_persist()
    generation = service._persist_generation
    assert failure_seen.wait(2.0)
    deadline = time.monotonic() + 2.0
    while service._persist_running and time.monotonic() < deadline:
        time.sleep(0.01)

    assert attempts == 1
    assert service._persisted_generation < generation
    assert service._persist_running is False

    monkeypatch.setattr(runner_module, "_atomic_write_json", original_atomic_write)
    service._schedule_persist(mark_dirty=False)
    deadline = time.monotonic() + 2.0
    while service._persist_running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert json.loads(service.history_path.read_text(encoding="utf-8")) == []
    assert service._persisted_generation == generation
    assert service.shutdown(wait=True, timeout=2.0)
    assert service._persisted_generation == service._persist_generation


def test_shutdown_wakes_retry_backoff_and_serially_takes_over_persistence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.services import tool_runner_service as runner_module

    history_path = tmp_path / "takeover-history.json"
    service = ToolRunnerService(
        registry=_registry(_BoundaryTool()),
        history_path=history_path,
    )
    assert service._history_loaded.wait(2.0)
    original_atomic_write = runner_module._atomic_write_json
    first_failure = threading.Event()
    counter_lock = threading.Lock()
    attempts = 0
    active_writes = 0
    max_active_writes = 0

    def fail_then_succeed(
        path: Path,
        value: Any,
        *,
        cache_root: Path | None = None,
    ) -> None:
        nonlocal attempts, active_writes, max_active_writes
        with counter_lock:
            attempts += 1
            attempt = attempts
            active_writes += 1
            max_active_writes = max(max_active_writes, active_writes)
        try:
            if attempt == 1:
                first_failure.set()
                raise OSError("transient write failure")
            original_atomic_write(path, value, cache_root=cache_root)
        finally:
            with counter_lock:
                active_writes -= 1

    monkeypatch.setattr(runner_module, "_atomic_write_json", fail_then_succeed)
    monkeypatch.setattr(
        runner_module,
        "_PERSIST_RETRY_DELAYS",
        (10.0, 10.0),
        raising=False,
    )
    monkeypatch.setattr(
        runner_module.debug_logger,
        "log_exception",
        lambda *_args, **_kwargs: None,
    )

    service._schedule_persist()
    assert first_failure.wait(2.0)
    started_at = time.monotonic()
    assert service.shutdown(wait=True, timeout=1.0)

    assert time.monotonic() - started_at < 0.5
    assert attempts == 2
    assert max_active_writes == 1
    assert service._persist_running is False
    assert service._persisted_generation == service._persist_generation
    assert json.loads(history_path.read_text(encoding="utf-8")) == []


def test_shutdown_rewrites_recovered_nonterminal_history_without_a_new_event(
    tmp_path: Path,
) -> None:
    history_path = tmp_path / "recovered-history.json"
    history_path.write_text(
        json.dumps(
            [
                {
                    "run_id": "recovered-run",
                    "tool_id": "boundary_tool",
                    "host_surface": "test",
                    "owner_id": "owner:recovered",
                    "status": "running",
                    "parameters": {},
                    "created_at": 1.0,
                    "started_at": 1.0,
                    "progress": 50,
                    "message": "interrupted",
                }
            ]
        ),
        encoding="utf-8",
    )
    owner = _profile(tmp_path, "owner:recovered")
    service = ToolRunnerService(
        registry=_registry(_BoundaryTool()),
        history_path=history_path,
    )

    assert service.history(execution_profile=owner)[0]["status"] == "failed"
    assert service.shutdown(wait=True, timeout=2.0)
    persisted = json.loads(history_path.read_text(encoding="utf-8"))
    assert persisted[0]["status"] == "failed"
    assert service._persisted_generation == service._persist_generation


def test_hostile_logger_is_best_effort_for_emit_load_and_persist_boundaries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.services import tool_runner_service as runner_module

    def hostile_log_exception(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt("hostile log_exception")

    def hostile_log(*_args: Any, **_kwargs: Any) -> None:
        raise SystemExit("hostile log")

    monkeypatch.setattr(
        runner_module.debug_logger,
        "log_exception",
        hostile_log_exception,
    )
    monkeypatch.setattr(runner_module.debug_logger, "log", hostile_log)

    emit_service = ToolRunnerService(
        registry=_registry(_LinkParserProjectionTool([])),
        history_path=tmp_path / "emit-history.json",
        event_callback=lambda _topic, _payload: (_ for _ in ()).throw(
            ValueError("event callback failed")
        ),
    )
    try:
        emit_service._emit("tools.test", {})
    finally:
        assert emit_service.shutdown(wait=True, timeout=2.0)

    malformed_path = tmp_path / "malformed-history.json"
    malformed_path.write_text("{", encoding="utf-8")
    load_service = ToolRunnerService(
        registry=_registry(_LinkParserProjectionTool([])),
        history_path=malformed_path,
    )
    assert load_service._history_loaded.wait(2.0)
    assert load_service._history_load_future.exception(timeout=1.0) is None
    assert load_service.shutdown(wait=True, timeout=2.0)

    persist_service = ToolRunnerService(
        registry=_registry(_LinkParserProjectionTool([])),
        history_path=tmp_path / "persist-history.json",
    )
    original_atomic_write = runner_module._atomic_write_json

    def failed_write(
        _path: Path,
        _value: Any,
        *,
        cache_root: Path | None = None,
    ) -> None:
        del cache_root
        raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogate")

    monkeypatch.setattr(runner_module, "_atomic_write_json", failed_write)
    persist_service._schedule_persist()
    deadline = time.monotonic() + 2.0
    while persist_service._persist_running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert persist_service._persist_running is False
    assert persist_service._persist_history_now(timeout=1.0) is False
    assert persist_service.shutdown(wait=True, timeout=1.0) is False
    monkeypatch.setattr(
        runner_module,
        "_atomic_write_json",
        original_atomic_write,
    )


@pytest.mark.parametrize("fatal_kind", ["keyboard_interrupt", "system_exit"])
def test_worker_control_flow_and_event_failures_become_safe_durable_terminals(
    tmp_path: Path,
    monkeypatch,
    fatal_kind: str,
) -> None:
    from app.services import tool_runner_service as runner_module

    history_path = tmp_path / f"worker-{fatal_kind}.json"
    owner = _profile(tmp_path, f"owner:{fatal_kind}")
    tool = _BoundaryTool()
    queued_calls = 0
    running_calls = 0
    terminal_calls = 0

    def hostile_events(topic: str, _payload: Mapping[str, Any]) -> None:
        nonlocal queued_calls, running_calls, terminal_calls
        if topic == "tools.queued":
            queued_calls += 1
            if queued_calls == 1:
                raise SystemExit("private queued callback secret")
        if topic == "tools.running":
            running_calls += 1
            if running_calls == 1:
                raise KeyboardInterrupt("private running callback secret")
        if topic in {"tools.finished", "tools.cancelled"}:
            terminal_calls += 1
            if terminal_calls == 1:
                raise ValueError("private terminal callback secret")
            if terminal_calls == 2:
                raise SystemExit("private terminal callback secret")

    def hostile_logger(*_args: Any, **_kwargs: Any) -> None:
        raise SystemExit("private logger secret")

    monkeypatch.setattr(runner_module.debug_logger, "log_exception", hostile_logger)
    service = ToolRunnerService(
        registry=_registry(tool),
        history_path=history_path,
        max_workers=1,
        event_callback=hostile_events,
    )

    first = service.run(
        "boundary_tool",
        {"worker_fatal": fatal_kind},
        execution_profile=owner,
    )
    first_terminal = service.wait_for_run(
        first["run_id"], execution_profile=owner, timeout=2.0
    )
    assert first_terminal["status"] == "failed"
    assert first_terminal["message"] == "tool run failed"
    assert first_terminal["result"] == {
        "status": "failed",
        "message": "tool run failed",
        "warnings": [],
    }
    assert first["run_id"] not in service._futures

    second = service.run("boundary_tool", {}, execution_profile=owner)
    second_terminal = service.wait_for_run(
        second["run_id"], execution_profile=owner, timeout=2.0
    )
    assert second_terminal["status"] == "succeeded"
    assert second["run_id"] not in service._futures
    assert service.wait_for_idle(timeout=2.0)
    assert service.shutdown(wait=True, timeout=2.0)

    persisted = json.loads(history_path.read_text(encoding="utf-8"))
    assert [row["status"] for row in persisted] == ["failed", "succeeded"]
    serialized = json.dumps(
        {"first": first_terminal, "second": second_terminal, "persisted": persisted}
    )
    assert "private" not in serialized


@pytest.mark.parametrize(
    ("fatal_stage", "fatal_kind", "code", "message"),
    [
        (
            "requirements",
            "exception",
            "tool_requirements_failed",
            "tool requirements evaluation failed",
        ),
        (
            "validate",
            "keyboard_interrupt",
            "tool_validation_failed",
            "tool validation failed",
        ),
        (
            "requirements_normalized",
            "system_exit",
            "tool_requirements_failed",
            "tool requirements evaluation failed",
        ),
        (
            "validate",
            "hostile_string",
            "tool_validation_failed",
            "tool validation failed",
        ),
        (
            "validate_projection",
            "system_exit",
            "tool_validation_failed",
            "tool validation failed",
        ),
    ],
)
def test_preflight_failures_are_fixed_redacted_and_do_not_poison_later_runs(
    tmp_path: Path,
    monkeypatch,
    fatal_stage: str,
    fatal_kind: str,
    code: str,
    message: str,
) -> None:
    from app.services import tool_runner_service as runner_module

    history_path = tmp_path / f"preflight-{fatal_stage}-{fatal_kind}.json"
    owner = _profile(tmp_path, f"owner:{fatal_stage}:{fatal_kind}")

    def hostile_logger(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt("private logger secret")

    monkeypatch.setattr(runner_module.debug_logger, "log_exception", hostile_logger)
    service = ToolRunnerService(
        registry=_registry(_BoundaryTool()),
        history_path=history_path,
        max_workers=1,
    )
    parameters = {"fatal_stage": fatal_stage, "fatal_kind": fatal_kind}

    validation = service.validate(
        "boundary_tool", parameters, execution_profile=owner
    )
    if fatal_stage == "requirements_normalized":
        assert validation["status"] == "ok"
    else:
        assert validation["status"] == "error"
        assert validation["code"] == code
        assert validation["message"] == message
        assert validation["errors"] == [message]

    rejected = service.run("boundary_tool", parameters, execution_profile=owner)
    assert rejected["status"] == "error"
    assert rejected["code"] == code
    assert rejected["message"] == message
    if "errors" in rejected:
        assert rejected["errors"] == [message]
    assert "run_id" not in rejected
    assert "private" not in json.dumps(rejected)

    queued = service.run("boundary_tool", {}, execution_profile=owner)
    terminal = service.wait_for_run(
        queued["run_id"], execution_profile=owner, timeout=2.0
    )
    assert terminal["status"] == "succeeded"
    assert service._futures == {}
    assert service.shutdown(wait=True, timeout=2.0)
    persisted = json.loads(history_path.read_text(encoding="utf-8"))
    assert [row["status"] for row in persisted] == ["succeeded"]


@pytest.mark.parametrize(
    "fatal_kind",
    ["exception", "keyboard_interrupt", "system_exit", "hostile_string"],
)
def test_settings_provider_failures_degrade_to_empty_settings(
    tmp_path: Path,
    monkeypatch,
    fatal_kind: str,
) -> None:
    from app.services import tool_runner_service as runner_module

    history_path = tmp_path / f"settings-{fatal_kind}.json"
    owner = _profile(tmp_path, f"owner:settings:{fatal_kind}")
    tool = _BoundaryTool()

    def failed_settings() -> Mapping[str, Any]:
        raise _boundary_failure(fatal_kind)

    def hostile_logger(*_args: Any, **_kwargs: Any) -> None:
        raise SystemExit("private logger secret")

    monkeypatch.setattr(runner_module.debug_logger, "log_exception", hostile_logger)
    service = ToolRunnerService(
        registry=_registry(tool),
        history_path=history_path,
        max_workers=1,
        settings_provider=failed_settings,
    )

    first = service.run("boundary_tool", {}, execution_profile=owner)
    first_terminal = service.wait_for_run(
        first["run_id"], execution_profile=owner, timeout=2.0
    )
    second = service.run("boundary_tool", {}, execution_profile=owner)
    second_terminal = service.wait_for_run(
        second["run_id"], execution_profile=owner, timeout=2.0
    )

    assert first_terminal["status"] == "succeeded"
    assert second_terminal["status"] == "succeeded"
    assert tool.settings_seen == [{}, {}]
    assert service._futures == {}
    assert service.shutdown(wait=True, timeout=2.0)
    persisted = json.loads(history_path.read_text(encoding="utf-8"))
    assert [row["status"] for row in persisted] == ["succeeded", "succeeded"]
