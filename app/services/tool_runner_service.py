"""Bounded asynchronous runtime for built-in and hot-loaded tools."""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.tools.contracts import (
    CancellationToken,
    ToolCancelledError,
    ToolContext,
    ToolDescriptor,
    ToolGrant,
    ToolGrantEvaluator,
    ToolRequirements,
    ToolRunResult,
    ToolRunStatus,
    ToolValidationResult,
    freeze_private_data,
)
from app.core.tools.registry import ToolRegistry
from app.debug_logger import debug_logger
from app.services.tool_history_projection import (
    normalize_tool_result as _normalize_result,
    project_history_record,
    project_runtime_record,
    public_validation_payload as _public_validation_payload,
    redact_parameters as _redact_parameters,
)
from app.services.tool_history_storage import (
    MAX_HISTORY_BYTES,
    atomic_write_json as _storage_atomic_write_json,
    bounded_history_snapshot,
    persist_retry_delay,
    read_history_json,
)
from app.utils.runtime_paths import user_cache_root, user_data_root
from shared.execution_profile import (
    ExecutionProfile,
    execution_profile_identity_error,
)

ToolEventCallback = Callable[[str, dict[str, Any]], None]

_SHUTDOWN_PERSIST_GRACE_SECONDS = 0.25
_TOOL_REQUIREMENTS_ERROR = "tool requirements evaluation failed"
_TOOL_VALIDATION_ERROR = "tool validation failed"
_TOOL_RUN_ERROR = "tool run failed"
_PERSIST_RETRY_DELAYS = (0.01, 0.05, 0.1, 0.25)


@dataclass(frozen=True, slots=True)
class PrivateToolResult:
    """Minimal owner-scoped handle for trusted local result opening."""

    run_id: str
    tool_id: str
    output_paths: tuple[Path, ...]
    structured_data: Mapping[str, Any] = field(default_factory=dict, repr=False)
    private_data: Mapping[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "output_paths",
            tuple(Path(str(path)) for path in self.output_paths),
        )
        object.__setattr__(
            self,
            "structured_data",
            freeze_private_data(self.structured_data),
        )
        object.__setattr__(
            self,
            "private_data",
            freeze_private_data(self.private_data),
        )


@dataclass(slots=True)
class _RunRecord:
    run_id: str
    tool_id: str
    status: ToolRunStatus
    parameters: dict[str, Any]
    host_surface: str = ""
    owner_id: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress: int = 0
    message: str = ""
    progress_details: dict[str, Any] = field(default_factory=dict)
    result: ToolRunResult | None = None
    runtime_private_result: bool = field(default=False, repr=False)
    claimed_candidate_ids: set[str] = field(default_factory=set, repr=False)

    def to_dict(self, *, include_runtime_data: bool = False) -> dict[str, Any]:
        payload = {
            "run_id": self.run_id,
            "tool_id": self.tool_id,
            "host_surface": self.host_surface,
            "owner_id": self.owner_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": self.progress,
            "message": self.message,
        }
        if self.result is not None:
            result_status = self.result.status
            projected_result = {
                "status": (
                    result_status.value
                    if isinstance(result_status, ToolRunStatus)
                    else result_status
                ),
                "message": self.result.message,
                "warnings": self.result.warnings,
            }
            if include_runtime_data:
                projected_result["data"] = self.result.data
            payload["result"] = projected_result
        return payload

    def to_public_dict(self) -> dict[str, Any]:
        return project_history_record(self.to_dict())

    def to_runtime_public_dict(self) -> dict[str, Any]:
        if not self.runtime_private_result:
            return self.to_public_dict()
        return project_runtime_record(self.to_dict(include_runtime_data=True))


class ToolRunnerService:
    """Runs tools outside frontend threads with cancellation and durable history."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        *,
        history_path: str | os.PathLike[str] | None = None,
        external_tool_dir: str | os.PathLike[str] | None = None,
        max_workers: int = 2,
        max_pending: int = 32,
        max_pending_per_owner: int = 8,
        history_limit: int = 200,
        event_callback: ToolEventCallback | None = None,
        settings_provider: Callable[[], Mapping[str, Any]] | None = None,
        services: Mapping[str, Any] | None = None,
    ) -> None:
        default_external_dir = Path(user_data_root()) / "tools"
        self.registry = registry or ToolRegistry(
            external_dir=external_tool_dir or default_external_dir,
        )
        self.history_path = Path(
            history_path or (Path(user_cache_root()) / "tool_history.json")
        )
        self._history_cache_root = self.history_path.parent
        self.history_limit = max(10, int(history_limit))
        self.max_pending = max(1, int(max_pending))
        self.max_pending_per_owner = max(1, int(max_pending_per_owner))
        self._event_callback = event_callback
        self._settings_provider = settings_provider
        self._services = dict(services or {})
        self._lock = threading.RLock()
        self._history_write_lock = threading.Lock()
        self._records: dict[str, _RunRecord] = {}
        self._order: list[str] = []
        self._tokens: dict[str, CancellationToken] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._worker_state_lock = threading.Lock()
        self._active_workers = 0
        self._workers_idle = threading.Event()
        self._workers_idle.set()
        self._closed = False
        self._persist_generation = 0
        self._persisted_generation = -1
        self._persist_running = False
        self._persist_condition = threading.Condition(self._lock)
        self._shutdown_persist_thread: threading.Thread | None = None
        self._shutdown_persist_result: bool | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="tool-runner",
        )
        self._io_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tool-history")
        self._history_loaded = threading.Event()
        self._history_load_future = self._io_executor.submit(self._load_history)

    # Public SDK/CLI contract.

    def list(self) -> list[dict[str, Any]]:
        return self.registry.manifests()

    def describe(self, tool_id: str) -> dict[str, Any]:
        tool = self.registry.get(tool_id)
        if tool is None:
            return {"status": "error", "message": "unknown tool", "tool_id": str(tool_id or "")}
        return tool.manifest.to_dict()

    def validate(
        self,
        tool_id: str,
        params: Mapping[str, Any] | None,
        *,
        execution_profile: ExecutionProfile,
    ) -> dict[str, Any]:
        identity_error = _profile_identity_error(execution_profile)
        if identity_error is not None:
            return identity_error
        descriptor = self.registry.descriptor(tool_id)
        if descriptor is None:
            return {"status": "error", "valid": False, "errors": ["unknown tool"], "tool_id": tool_id}
        try:
            parameters = dict(params or {})
        except BaseException as exc:
            _log_exception_best_effort("validate.parameters", exc)
            return _preflight_error_payload(
                descriptor.tool.manifest.id,
                code="tool_validation_failed",
                message=_TOOL_VALIDATION_ERROR,
            )
        return _public_validation_payload(
            self._validate_descriptor(
                descriptor,
                parameters,
                execution_profile,
            ),
            tool_id=descriptor.tool.manifest.id,
        )

    def _validate_descriptor(
        self,
        descriptor: ToolDescriptor,
        parameters: dict[str, Any],
        execution_profile: ExecutionProfile,
    ) -> dict[str, Any]:
        tool = descriptor.tool
        try:
            grant = self._evaluate_grant(descriptor, parameters, execution_profile)
        except BaseException as exc:
            _log_exception_best_effort("requirements", exc)
            return _preflight_error_payload(
                tool.manifest.id,
                code="tool_requirements_failed",
                message=_TOOL_REQUIREMENTS_ERROR,
            )
        if not grant.allowed:
            return _forbidden_payload(tool.manifest.id, grant)
        context = self._make_context(
            tool_id=tool.manifest.id,
            run_id="",
            parameters=parameters,
            execution_profile=execution_profile,
            provenance=descriptor.provenance,
            cancellation=CancellationToken(),
        )
        try:
            validation = _normalize_validation(tool.validate(context), context.parameters)
            payload = validation.to_dict()
        except BaseException as exc:
            _log_exception_best_effort("validate", exc)
            return _preflight_error_payload(
                tool.manifest.id,
                code="tool_validation_failed",
                message=_TOOL_VALIDATION_ERROR,
            )
        payload["tool_id"] = tool.manifest.id
        return payload

    def run(
        self,
        tool_id: str,
        params: Mapping[str, Any] | None,
        *,
        execution_profile: ExecutionProfile,
    ) -> dict[str, Any]:
        identity_error = _profile_identity_error(execution_profile)
        if identity_error is not None:
            return identity_error
        self._history_loaded.wait()
        with self._lock:
            if self._closed:
                return {"status": "error", "message": "tool runner is shut down"}
        descriptor = self.registry.descriptor(tool_id)
        if descriptor is None:
            return {"status": "error", "message": "unknown tool", "tool_id": str(tool_id or "")}
        tool = descriptor.tool
        try:
            parameters = dict(params or {})
        except BaseException as exc:
            _log_exception_best_effort("run.parameters", exc)
            return _preflight_error_payload(
                tool.manifest.id,
                code="tool_validation_failed",
                message=_TOOL_VALIDATION_ERROR,
            )
        validation = self._validate_descriptor(
            descriptor,
            parameters,
            execution_profile,
        )
        if validation.get("status") != "ok":
            return _public_validation_payload(
                validation,
                tool_id=tool.manifest.id,
            )
        try:
            normalized_parameters = dict(validation.get("parameters") or parameters)
        except BaseException as exc:
            _log_exception_best_effort("run.normalized_parameters", exc)
            return _preflight_error_payload(
                tool.manifest.id,
                code="tool_validation_failed",
                message=_TOOL_VALIDATION_ERROR,
            )
        try:
            grant = self._evaluate_grant(
                descriptor,
                normalized_parameters,
                execution_profile,
            )
        except BaseException as exc:
            _log_exception_best_effort("requirements.normalized", exc)
            return _preflight_error_payload(
                tool.manifest.id,
                code="tool_requirements_failed",
                message=_TOOL_REQUIREMENTS_ERROR,
            )
        if not grant.allowed:
            return _forbidden_payload(tool.manifest.id, grant)
        run_id = f"tool_{uuid.uuid4().hex}"
        token = CancellationToken()
        record = _RunRecord(
            run_id,
            tool.manifest.id,
            ToolRunStatus.QUEUED,
            _redact_parameters(
                normalized_parameters,
                sensitive_fields=("text",) if tool.manifest.id == "link_parser" else (),
            ),
            host_surface=execution_profile.host_surface,
            owner_id=execution_profile.owner_id,
        )
        with self._lock:
            if self._closed:
                return {"status": "error", "message": "tool runner is shut down"}
            active_run_ids = tuple(self._futures)
            owner_pending = sum(
                1
                for active_run_id in active_run_ids
                if self._records.get(active_run_id) is not None
                and self._records[active_run_id].owner_id == execution_profile.owner_id
            )
            if len(active_run_ids) >= self.max_pending or owner_pending >= self.max_pending_per_owner:
                return {
                    "status": "busy",
                    "code": "tool_capacity_reached",
                    "message": "tool runner capacity reached",
                }
            self._records[run_id] = record
            self._order.append(run_id)
            self._trim_history_locked()
            self._tokens[run_id] = token
            queued_payload = record.to_public_dict()
            future = self._executor.submit(
                self._execute_guarded,
                run_id,
                tool,
                normalized_parameters,
                execution_profile,
                descriptor.provenance,
                token,
            )
            self._futures[run_id] = future
        self._emit("tools.queued", queued_payload)
        self._schedule_persist()
        return queued_payload

    @staticmethod
    def _evaluate_grant(
        descriptor: ToolDescriptor,
        parameters: Mapping[str, Any],
        execution_profile: ExecutionProfile,
    ) -> ToolGrant:
        declared_permissions = frozenset(descriptor.tool.manifest.permissions)
        preliminary = ToolGrantEvaluator.evaluate(
            requirements=ToolRequirements(),
            declared_permissions=declared_permissions,
            provenance=descriptor.provenance,
            execution_profile=execution_profile,
        )
        if not preliminary.allowed:
            return preliminary
        requirements = descriptor.tool.requirements_for(dict(parameters))
        if not isinstance(requirements, ToolRequirements):
            raise TypeError("requirements_for() must return ToolRequirements")
        return ToolGrantEvaluator.evaluate(
            requirements=requirements,
            declared_permissions=declared_permissions,
            provenance=descriptor.provenance,
            execution_profile=execution_profile,
        )

    def cancel(
        self,
        run_id: str,
        *,
        execution_profile: ExecutionProfile,
    ) -> dict[str, Any]:
        identity_error = _profile_identity_error(execution_profile)
        if identity_error is not None:
            return identity_error
        self._history_loaded.wait()
        normalized = str(run_id or "").strip()
        with self._lock:
            record = self._records.get(normalized)
            if record is None:
                return {"status": "error", "message": "unknown tool run", "run_id": normalized}
            if not self._is_owned_by(record, execution_profile):
                return _owner_mismatch_payload(normalized)
            payload, event_topic = self._cancel_locked(normalized)
        if event_topic is not None:
            self._emit(event_topic, payload)
            self._schedule_persist(mark_dirty=False)
        return payload

    def history(
        self,
        *,
        execution_profile: ExecutionProfile,
        tool_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if _profile_identity_error(execution_profile) is not None:
            return []
        self._history_loaded.wait()
        normalized_status = str(status or "").strip().lower()
        normalized_tool = str(tool_id or "").strip().lower()
        row_limit = max(1, min(self.history_limit, int(limit or self.history_limit)))
        with self._lock:
            rows = [
                self._records[run_id].to_public_dict()
                for run_id in reversed(self._order)
                if run_id in self._records
                and self._is_owned_by(self._records[run_id], execution_profile)
            ]
        if normalized_tool:
            rows = [row for row in rows if row.get("tool_id") == normalized_tool]
        if normalized_status:
            rows = [row for row in rows if row.get("status") == normalized_status]
        return rows[:row_limit]

    # Additional application-runtime operations.

    def reload(
        self,
        *,
        force: bool = False,
        execution_profile: ExecutionProfile,
    ) -> dict[str, Any]:
        identity_error = _profile_identity_error(execution_profile)
        if identity_error is not None:
            return identity_error
        if not execution_profile.allow_tool_execution:
            return {
                "status": "forbidden",
                "code": "tool_run_disabled",
                "message": "tool execution is disabled for this host",
            }
        if not execution_profile.allow_external_plugins:
            return {
                "status": "forbidden",
                "code": "external_plugins_disabled",
                "message": "external tools are disabled for this host",
            }
        result = self.registry.reload_external(force=force).to_dict()
        self._emit("tools.reloaded", result)
        return result

    def get_run(
        self,
        run_id: str,
        *,
        execution_profile: ExecutionProfile,
    ) -> dict[str, Any] | None:
        if _profile_identity_error(execution_profile) is not None:
            return None
        self._history_loaded.wait()
        with self._lock:
            record = self._records.get(str(run_id or ""))
            if record is None or not self._is_owned_by(record, execution_profile):
                return None
            return record.to_runtime_public_dict()

    def lookup_private_result(
        self,
        run_id: str,
        *,
        execution_profile: ExecutionProfile,
    ) -> PrivateToolResult | None:
        """Return an immutable local-only result handle without exposing run secrets."""

        normalized = str(run_id or "").strip()
        if not normalized or _profile_identity_error(execution_profile) is not None:
            return None
        self._history_loaded.wait()
        with self._lock:
            record = self._records.get(normalized)
            if not self._record_has_private_result(record, execution_profile):
                return None
            assert record is not None and record.result is not None
            return PrivateToolResult(
                run_id=record.run_id,
                tool_id=record.tool_id,
                output_paths=tuple(
                    Path(str(path))
                    for path in record.result.output_paths
                    if str(path).strip()
                ),
                structured_data=record.result.data,
                private_data=record.result.private_data,
            )

    def _claim_private_candidates(
        self,
        run_id: str,
        candidate_ids: tuple[str, ...],
        *,
        execution_profile: ExecutionProfile,
    ) -> bool:
        """Atomically reserve current-process private candidates for one owner."""

        normalized_run_id = str(run_id or "").strip()
        normalized_ids = tuple(str(candidate_id or "").strip() for candidate_id in candidate_ids)
        if (
            not normalized_run_id
            or not normalized_ids
            or len(set(normalized_ids)) != len(normalized_ids)
            or any(not candidate_id for candidate_id in normalized_ids)
            or _profile_identity_error(execution_profile) is not None
        ):
            return False
        self._history_loaded.wait()
        with self._lock:
            record = self._records.get(normalized_run_id)
            if not self._record_has_private_result(record, execution_profile):
                return False
            assert record is not None
            if record.claimed_candidate_ids.intersection(normalized_ids):
                return False
            record.claimed_candidate_ids.update(normalized_ids)
            return True

    def _release_private_candidates(
        self,
        run_id: str,
        candidate_ids: tuple[str, ...],
        *,
        execution_profile: ExecutionProfile,
    ) -> bool:
        """Release a reservation only while the same private result is still live."""

        normalized_run_id = str(run_id or "").strip()
        normalized_ids = tuple(str(candidate_id or "").strip() for candidate_id in candidate_ids)
        if (
            not normalized_run_id
            or not normalized_ids
            or len(set(normalized_ids)) != len(normalized_ids)
            or any(not candidate_id for candidate_id in normalized_ids)
            or _profile_identity_error(execution_profile) is not None
        ):
            return False
        self._history_loaded.wait()
        with self._lock:
            record = self._records.get(normalized_run_id)
            if not self._record_has_private_result(record, execution_profile):
                return False
            assert record is not None
            if not set(normalized_ids).issubset(record.claimed_candidate_ids):
                return False
            record.claimed_candidate_ids.difference_update(normalized_ids)
            return True

    @staticmethod
    def _record_has_private_result(
        record: _RunRecord | None,
        execution_profile: ExecutionProfile,
    ) -> bool:
        return bool(
            record is not None
            and ToolRunnerService._is_owned_by(record, execution_profile)
            and record.status is ToolRunStatus.SUCCEEDED
            and record.result is not None
            and _coerce_run_status(record.result.status) is ToolRunStatus.SUCCEEDED
            and record.runtime_private_result
        )

    def snapshot(
        self,
        *,
        execution_profile: ExecutionProfile,
        history_limit: int = 20,
    ) -> dict[str, Any]:
        return {
            "toolbox_items": self.list(),
            "toolbox_recent_items": self.history(
                execution_profile=execution_profile,
                limit=history_limit,
            ),
        }

    def clear_history(
        self,
        *,
        execution_profile: ExecutionProfile,
    ) -> dict[str, Any]:
        identity_error = _profile_identity_error(execution_profile)
        if identity_error is not None:
            return identity_error
        self._history_loaded.wait()
        with self._lock:
            active = set(self._futures)
            removed = [
                run_id
                for run_id in self._order
                if run_id not in active
                and run_id in self._records
                and self._is_owned_by(self._records[run_id], execution_profile)
            ]
            for run_id in removed:
                self._records.pop(run_id, None)
            removed_ids = set(removed)
            self._order = [run_id for run_id in self._order if run_id not in removed_ids]
        self._schedule_persist()
        payload = {"status": "ok", "removed": len(removed)}
        self._emit("tools.history_cleared", payload)
        return payload

    def wait_for_run(
        self,
        run_id: str,
        *,
        execution_profile: ExecutionProfile,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Wait for one owned run without exposing another host's state."""

        normalized = str(run_id or "").strip()
        identity_error = _profile_identity_error(execution_profile)
        if identity_error is not None:
            return {**identity_error, "run_id": normalized}
        deadline = _deadline(timeout)
        if not self._history_loaded.wait(_remaining_seconds(deadline)):
            return {"status": "timeout", "run_id": normalized}
        with self._lock:
            record = self._records.get(normalized)
            if record is None:
                return {"status": "error", "message": "unknown tool run", "run_id": normalized}
            if not self._is_owned_by(record, execution_profile):
                return _owner_mismatch_payload(normalized)
            if _is_terminal_status(record.status):
                return record.to_runtime_public_dict()
            future = self._futures.get(normalized)
        if future is None:
            return {"status": "timeout", "run_id": normalized}
        try:
            future.result(timeout=_remaining_seconds(deadline))
        except FutureTimeoutError:
            return {"status": "timeout", "run_id": normalized}
        except Exception:
            # Tool exceptions are normally normalized in _execute; cancellation may
            # still surface through Future while the record already has a terminal state.
            pass
        except BaseException as exc:
            background_exc = None
            if future.done() and not future.cancelled():
                background_exc = future.exception(timeout=0)
            if background_exc is not exc:
                raise
        with self._lock:
            record = self._records.get(normalized)
            if record is None:
                return {"status": "error", "message": "unknown tool run", "run_id": normalized}
            if not self._is_owned_by(record, execution_profile):
                return _owner_mismatch_payload(normalized)
            return record.to_runtime_public_dict()

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._lock:
                futures = tuple(self._futures.values())
            if not futures:
                return self._workers_idle.wait(
                    None if deadline is None else max(0.0, deadline - time.monotonic())
                )
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            _, pending = wait(futures, timeout=remaining)
            if pending and deadline is not None and time.monotonic() >= deadline:
                return False

    def shutdown(
        self,
        *,
        wait: bool = True,
        timeout: float | None = None,
    ) -> bool:
        """Cancel active runs, wait within the caller budget, and durably flush history."""

        deadline = _deadline(timeout)
        history_wait_timeout = 0.0 if not wait else _remaining_seconds(deadline)
        if not self._history_loaded.wait(history_wait_timeout):
            with self._lock:
                self._closed = True
                self._persist_condition.notify_all()
            self._executor.shutdown(wait=False, cancel_futures=True)
            # The queued loader owns the history barrier's finally block. Cancelling
            # it before start would leave every repeated shutdown stuck forever.
            self._io_executor.shutdown(wait=False, cancel_futures=False)
            self._log_shutdown_timeout((), timeout, phase="history_load")
            return False
        with self._lock:
            first_shutdown = not self._closed
            self._closed = True
            self._persist_condition.notify_all()
            active_run_ids = tuple(self._futures)
            cancellation_events = (
                [self._cancel_locked(run_id) for run_id in active_run_ids]
                if first_shutdown
                else []
            )
        for payload, event_topic in cancellation_events:
            if event_topic is not None:
                self._emit(event_topic, payload)

        with self._lock:
            drained = not self._futures and self._workers_idle.is_set()
        if wait:
            drained = self.wait_for_idle(timeout=_remaining_seconds(deadline))
        self._executor.shutdown(wait=False, cancel_futures=True)
        workers_joined = not wait or _join_executor_threads(self._executor, deadline)

        # With a deadline, never let history executor teardown extend the caller budget.
        # Generation was advanced above, so an in-flight loop will refresh its snapshot.
        self._io_executor.shutdown(wait=False, cancel_futures=False)
        persist_thread = self._ensure_shutdown_persist_thread()
        persist_timeout = _remaining_seconds(deadline)
        if not wait:
            persist_timeout = _SHUTDOWN_PERSIST_GRACE_SECONDS
        elif persist_timeout is not None:
            # A zero worker-drain budget must not silently discard the terminal
            # cancellation ledger. Keep this durability grace short and bounded.
            persist_timeout = max(persist_timeout, _SHUTDOWN_PERSIST_GRACE_SECONDS)
        persist_thread.join(persist_timeout)
        with self._lock:
            persisted = (
                not persist_thread.is_alive()
                and self._shutdown_persist_result is True
            )
        io_joined = not wait or _join_executor_threads(self._io_executor, deadline)
        drained = drained and persisted and workers_joined and io_joined
        if not drained:
            self._log_shutdown_timeout(active_run_ids, timeout, phase="drain")
        return drained

    def _execute_guarded(self, *args: Any) -> None:
        with self._worker_state_lock:
            self._active_workers += 1
            self._workers_idle.clear()
        try:
            try:
                self._execute(*args)
            except BaseException as exc:
                run_id = str(args[0]) if args else ""
                _log_exception_best_effort("run.worker_boundary", exc)
                self._terminalize_worker_failure(run_id)
        finally:
            with self._worker_state_lock:
                self._active_workers = max(0, self._active_workers - 1)
                if self._active_workers == 0:
                    self._workers_idle.set()

    def _execute(
        self,
        run_id: str,
        tool: Any,
        parameters: dict[str, Any],
        execution_profile: ExecutionProfile,
        provenance: str,
        token: CancellationToken,
    ) -> None:
        cancelled_before_run = False
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                return
            if token.is_cancelled():
                cancelled_payload, terminal_transitioned = self._mark_cancelled_locked(
                    run_id
                )
                persist_synchronously = self._closed
                cancelled_before_run = True
            else:
                record.status = ToolRunStatus.RUNNING
                record.started_at = time.time()
                record.message = "tool run started"
                running_payload = record.to_public_dict()
        if cancelled_before_run:
            if terminal_transitioned:
                self._emit("tools.cancelled", cancelled_payload)
                if persist_synchronously:
                    self._persist_history_now()
                else:
                    self._schedule_persist(mark_dirty=False)
            return
        self._emit("tools.running", running_payload)
        context = self._make_context(
            tool_id=tool.manifest.id,
            run_id=run_id,
            parameters=parameters,
            execution_profile=execution_profile,
            provenance=provenance,
            cancellation=token,
        )
        try:
            token.raise_if_cancelled()
            result = _normalize_result(tool.run(context))
            token.raise_if_cancelled()
        except ToolCancelledError:
            result = ToolRunResult.cancelled("tool run cancelled")
        except BaseException as exc:
            _log_exception_best_effort(f"run.{tool.manifest.id}", exc)
            result = ToolRunResult.failure(_TOOL_RUN_ERROR)
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                return
            result_status = _coerce_run_status(result.status)
            cancellation_won = (
                record.status == ToolRunStatus.CANCELLING
                or token.is_cancelled()
                or result_status == ToolRunStatus.CANCELLED
            )
            if cancellation_won:
                result_status = ToolRunStatus.CANCELLED
                completed_payload, terminal_transitioned = self._mark_cancelled_locked(
                    run_id,
                    message=result.message,
                )
            else:
                record.status = result_status
                record.result = result
                record.runtime_private_result = True
                record.message = result.message
                record.progress = (
                    100 if result_status == ToolRunStatus.SUCCEEDED else record.progress
                )
                record.finished_at = time.time()
                self._mark_persist_dirty_locked()
                try:
                    completed_payload = record.to_public_dict()
                finally:
                    self._finish_run_locked(run_id)
                terminal_transitioned = True
            persist_synchronously = self._closed
        if not terminal_transitioned:
            return
        terminal_topic = (
            "tools.cancelled"
            if result_status == ToolRunStatus.CANCELLED
            else "tools.finished"
        )
        self._emit(terminal_topic, completed_payload)
        if persist_synchronously:
            self._persist_history_now()
        else:
            self._schedule_persist(mark_dirty=False)

    def _terminalize_worker_failure(self, run_id: str) -> None:
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                return
            token = self._tokens.get(run_id)
            cancelled = record.status == ToolRunStatus.CANCELLING or (
                token is not None and token.is_cancelled()
            )
            if cancelled:
                result = ToolRunResult.cancelled("tool run cancelled")
                record.status = ToolRunStatus.CANCELLED
                topic = "tools.cancelled"
            else:
                result = ToolRunResult.failure(_TOOL_RUN_ERROR)
                record.status = ToolRunStatus.FAILED
                topic = "tools.finished"
            record.result = result
            record.runtime_private_result = False
            record.message = result.message
            record.finished_at = time.time()
            self._mark_persist_dirty_locked()
            try:
                payload = record.to_public_dict()
            except BaseException as exc:
                _log_exception_best_effort("run.terminal_projection", exc)
                payload = project_history_record(record.to_dict())
            finally:
                self._finish_run_locked(run_id)
            persist_synchronously = self._closed
        self._emit(topic, payload)
        if persist_synchronously:
            self._persist_history_now()
        else:
            self._schedule_persist(mark_dirty=False)

    def _make_context(
        self,
        *,
        tool_id: str,
        run_id: str,
        parameters: dict[str, Any],
        execution_profile: ExecutionProfile,
        provenance: str,
        cancellation: CancellationToken,
    ) -> ToolContext:
        settings: Mapping[str, Any] = {}
        if self._settings_provider is not None:
            try:
                settings = dict(self._settings_provider() or {})
            except BaseException as exc:
                _log_exception_best_effort("settings_provider", exc)
        return ToolContext(
            parameters=dict(parameters),
            run_id=run_id,
            execution_profile=execution_profile,
            provenance=provenance,
            settings=settings,
            services=self._services,
            cancellation=cancellation,
            progress_callback=lambda percent, message, details: self._on_progress(
                run_id,
                percent,
                message,
                details,
            ),
        )

    def _on_progress(
        self,
        run_id: str,
        percent: int,
        message: str,
        details: Mapping[str, Any],
    ) -> None:
        with self._lock:
            record = self._records.get(run_id)
            if record is None or record.status not in {ToolRunStatus.RUNNING, ToolRunStatus.CANCELLING}:
                return
            record.progress = max(record.progress, max(0, min(100, int(percent))))
            if message:
                record.message = str(message)
            record.progress_details = dict(details or {})
            payload = record.to_public_dict()
        self._emit("tools.progress", payload)

    def _finish_run_locked(self, run_id: str) -> None:
        self._tokens.pop(run_id, None)
        self._futures.pop(run_id, None)
        self._trim_history_locked()

    def _mark_persist_dirty_locked(self) -> None:
        self._persist_generation += 1
        self._persist_condition.notify_all()

    def _mark_cancelled_locked(
        self,
        run_id: str,
        *,
        message: str = "tool run cancelled",
    ) -> tuple[dict[str, Any], bool]:
        record = self._records[run_id]
        if _is_terminal_status(record.status):
            return record.to_public_dict(), False
        result = ToolRunResult.cancelled(message)
        record.status = ToolRunStatus.CANCELLED
        record.result = result
        record.runtime_private_result = False
        record.message = result.message
        record.finished_at = time.time()
        self._mark_persist_dirty_locked()
        try:
            payload = record.to_public_dict()
        except BaseException as exc:
            _log_exception_best_effort("cancel.terminal_projection", exc)
            payload = project_history_record(record.to_dict())
        finally:
            self._finish_run_locked(run_id)
        return payload, True

    def _cancel_locked(self, run_id: str) -> tuple[dict[str, Any], str | None]:
        record = self._records[run_id]
        if _is_terminal_status(record.status):
            return record.to_public_dict(), None
        if record.status == ToolRunStatus.CANCELLING:
            return record.to_public_dict(), None
        token = self._tokens.get(run_id)
        future = self._futures.get(run_id)
        if token is not None:
            token.cancel()
        cancelled_before_start = bool(future and future.cancel())
        if cancelled_before_start:
            payload, _ = self._mark_cancelled_locked(run_id)
            return payload, "tools.cancelled"
        record.status = ToolRunStatus.CANCELLING
        record.message = "cancellation requested"
        self._mark_persist_dirty_locked()
        return record.to_public_dict(), "tools.cancelling"

    @staticmethod
    def _is_owned_by(record: _RunRecord, execution_profile: ExecutionProfile) -> bool:
        return (
            record.host_surface == execution_profile.host_surface
            and record.owner_id == execution_profile.owner_id
        )

    def _trim_history_locked(self) -> None:
        while len(self._order) > self.history_limit:
            candidate = next(
                (
                    run_id
                    for run_id in self._order
                    if run_id not in self._futures
                    and run_id in self._records
                    and _is_terminal_status(self._records[run_id].status)
                ),
                None,
            )
            if candidate is None:
                break
            self._order.remove(candidate)
            self._records.pop(candidate, None)

    def _emit(self, topic: str, payload: Mapping[str, Any]) -> None:
        del payload
        callback = self._event_callback
        if callback is None:
            return
        try:
            # Events only invalidate the toolbox section. Owner-scoped state is fetched
            # through history/get_run so one host cannot observe another host's payload.
            callback(str(topic), {})
        except BaseException as exc:
            _log_exception_best_effort("event_callback", exc)

    def _load_history(self) -> None:
        try:
            raw = read_history_json(
                self.history_path,
                cache_root=self._history_cache_root,
                max_bytes=MAX_HISTORY_BYTES,
            )
            if raw is None:
                return
            if not isinstance(raw, list):
                return
            loaded: dict[str, _RunRecord] = {}
            for item in raw:
                record = _record_from_dict(item)
                if record is None:
                    continue
                existing = loaded.get(record.run_id)
                if existing is None or record.created_at >= existing.created_at:
                    loaded[record.run_id] = record
            with self._lock:
                for run_id, record in loaded.items():
                    if run_id not in self._records:
                        self._records[run_id] = record
                self._order = sorted(
                    set(self._order).union(loaded),
                    key=lambda run_id: (
                        self._records[run_id].created_at,
                        run_id,
                    ),
                )
                self._trim_history_locked()
        except BaseException as exc:
            _log_exception_best_effort("load_history", exc)
        finally:
            self._history_loaded.set()

    def _schedule_persist(self, *, mark_dirty: bool = True) -> None:
        with self._lock:
            if mark_dirty:
                self._mark_persist_dirty_locked()
            if (
                self._closed
                or self._persisted_generation >= self._persist_generation
                or self._persist_running
            ):
                return
            self._persist_running = True
        try:
            self._io_executor.submit(self._persist_loop)
        except BaseException as exc:
            _log_exception_best_effort("persist_history.schedule", exc)
            with self._lock:
                self._persist_running = False

    def _persist_loop(self) -> None:
        generation: int | None = None
        attempt_generation: int | None = None
        failures = 0
        try:
            while True:
                with self._lock:
                    if (
                        self._closed
                        or self._persisted_generation >= self._persist_generation
                    ):
                        return
                    generation = self._persist_generation
                if generation != attempt_generation:
                    attempt_generation = generation
                    failures = 0
                try:
                    with self._history_write_lock:
                        with self._lock:
                            snapshot = [
                                self._records[run_id].to_public_dict()
                                for run_id in self._order
                                if run_id in self._records
                            ]
                        snapshot = bounded_history_snapshot(
                            snapshot,
                            max_bytes=MAX_HISTORY_BYTES,
                        )
                        _atomic_write_json(
                            self.history_path,
                            snapshot,
                            cache_root=self._history_cache_root,
                        )
                        with self._lock:
                            self._persisted_generation = max(
                                self._persisted_generation,
                                generation,
                            )
                except OSError as exc:
                    failures += 1
                    _log_exception_best_effort("persist_history", exc)
                    with self._persist_condition:
                        if self._closed or self._persisted_generation >= generation:
                            return
                        if generation != self._persist_generation:
                            attempt_generation = None
                            continue
                        self._persist_condition.wait(
                            timeout=persist_retry_delay(
                                failures,
                                _PERSIST_RETRY_DELAYS,
                            )
                        )
                    continue
                except BaseException as exc:
                    _log_exception_best_effort("persist_history", exc)
                    return
                with self._lock:
                    if (
                        self._closed
                        or self._persisted_generation >= self._persist_generation
                    ):
                        return
                attempt_generation = None
        finally:
            with self._lock:
                retry = (
                    not self._closed
                    and generation is not None
                    and self._persist_generation > generation
                )
                self._persist_running = False
            if retry:
                self._schedule_persist(mark_dirty=False)

    def _persist_history_now(self, *, timeout: float | None = None) -> bool:
        acquired = (
            self._history_write_lock.acquire()
            if timeout is None
            else self._history_write_lock.acquire(timeout=max(0.0, float(timeout)))
        )
        if not acquired:
            return False
        try:
            with self._lock:
                generation = self._persist_generation
                if self._persisted_generation >= generation:
                    return True
                snapshot = [
                    self._records[run_id].to_public_dict()
                    for run_id in self._order
                    if run_id in self._records
                ]
            snapshot = bounded_history_snapshot(
                snapshot,
                max_bytes=MAX_HISTORY_BYTES,
            )
            _atomic_write_json(
                self.history_path,
                snapshot,
                cache_root=self._history_cache_root,
            )
            with self._lock:
                self._persisted_generation = max(
                    self._persisted_generation,
                    generation,
                )
                return self._persisted_generation >= self._persist_generation
        except BaseException as exc:
            _log_exception_best_effort("persist_history.shutdown", exc)
            return False
        finally:
            self._history_write_lock.release()

    def _ensure_shutdown_persist_thread(self) -> threading.Thread:
        with self._lock:
            current = self._shutdown_persist_thread
            if current is not None and current.is_alive():
                return current
            self._shutdown_persist_result = None
            thread = threading.Thread(
                target=self._persist_shutdown_snapshot,
                name="tool-history-shutdown",
                # A timed-out owner may return and let the interpreter exit. Keep
                # already-accepted terminal history alive until this final snapshot
                # is durable instead of abandoning it with a daemon worker.
                daemon=False,
            )
            self._shutdown_persist_thread = thread
            thread.start()
            return thread

    def _persist_shutdown_snapshot(self) -> None:
        try:
            result = self._persist_history_now()
        except BaseException as exc:
            _log_exception_best_effort("persist_history.shutdown_worker", exc)
            result = False
        with self._lock:
            self._shutdown_persist_result = result

    @staticmethod
    def _log_shutdown_timeout(
        active_run_ids: tuple[str, ...],
        timeout: float | None,
        *,
        phase: str,
    ) -> None:
        try:
            debug_logger.log(
                component="ToolRunnerService",
                action="shutdown_timeout",
                level="WARN",
                message="Tool runtime exceeded the bounded shutdown window",
                status_code="TOOL_SHUTDOWN_TIMEOUT",
                details={
                    "active_run_ids": list(active_run_ids),
                    "timeout": timeout,
                    "phase": phase,
                },
            )
        except BaseException:
            pass


def _normalize_validation(value: Any, parameters: Mapping[str, Any]) -> ToolValidationResult:
    if isinstance(value, ToolValidationResult):
        if value.parameters:
            return value
        return ToolValidationResult(value.valid, value.errors, value.warnings, dict(parameters))
    if isinstance(value, ToolRunResult):
        valid = _coerce_run_status(value.status) != ToolRunStatus.FAILED
        return ToolValidationResult(valid, () if valid else (value.message,), value.warnings, dict(parameters))
    if value is None or value is True:
        return ToolValidationResult.ok(parameters=parameters)
    if value is False:
        return ToolValidationResult.rejected("tool input is invalid")
    if isinstance(value, str):
        return ToolValidationResult.rejected(value)
    if isinstance(value, Mapping):
        errors = tuple(str(item) for item in value.get("errors", ()) if str(item).strip())
        warnings = tuple(str(item) for item in value.get("warnings", ()) if str(item).strip())
        normalized = value.get("parameters")
        return ToolValidationResult(
            not errors and bool(value.get("valid", True)),
            errors,
            warnings,
            dict(normalized) if isinstance(normalized, Mapping) else dict(parameters),
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        errors = tuple(str(item) for item in value if str(item).strip())
        return ToolValidationResult(not errors, errors, (), dict(parameters))
    return ToolValidationResult.rejected("unsupported validation result")


def _is_terminal_status(status: ToolRunStatus) -> bool:
    return status in {
        ToolRunStatus.SUCCEEDED,
        ToolRunStatus.FAILED,
        ToolRunStatus.CANCELLED,
    }


def _deadline(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    return time.monotonic() + max(0.0, float(timeout))


def _remaining_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _join_executor_threads(
    executor: ThreadPoolExecutor,
    deadline: float | None,
) -> bool:
    """Join CPython 3.10-3.13 executor workers within the shutdown budget.

    ThreadPoolExecutor exposes no public bounded join. Service-owned futures prove
    work completion but not physical worker exit, so the supported CPython matrix's
    stable ``_threads`` set is isolated here instead of weakening timeout semantics.
    """

    current_ident = threading.get_ident()
    threads = tuple(getattr(executor, "_threads", ()))
    for thread in threads:
        if thread.ident == current_ident:
            return False
        thread.join(_remaining_seconds(deadline))
    return all(not thread.is_alive() for thread in threads)


def _owner_mismatch_payload(run_id: str) -> dict[str, Any]:
    return {
        "status": "forbidden",
        "code": "tool_owner_mismatch",
        "message": "tool run belongs to another owner",
        "run_id": run_id,
    }


def _preflight_error_payload(
    tool_id: str,
    *,
    code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "status": "error",
        "valid": False,
        "code": code,
        "message": message,
        "errors": [message],
        "tool_id": tool_id,
    }


def _log_exception_best_effort(action: str, exc: BaseException) -> None:
    try:
        debug_logger.log_exception("ToolRunnerService", action, exc)
    except BaseException:
        pass


def _record_from_dict(value: Any) -> _RunRecord | None:
    if not isinstance(value, Mapping):
        return None
    try:
        value = project_history_record(value)
        run_id = str(value["run_id"]).strip()
        tool_id = str(value["tool_id"]).strip()
        host_surface = value.get("host_surface")
        owner_id = value.get("owner_id")
        if (
            not run_id
            or not tool_id
            or execution_profile_identity_error(
                host_surface=host_surface,
                owner_id=owner_id,
            )
            is not None
        ):
            return None
        status = _coerce_run_status(value.get("status"))
        if status in {ToolRunStatus.QUEUED, ToolRunStatus.RUNNING, ToolRunStatus.CANCELLING}:
            status = ToolRunStatus.FAILED
        record = _RunRecord(
            run_id=run_id,
            tool_id=tool_id,
            status=status,
            parameters=dict(value.get("parameters") or {}),
            host_surface=host_surface,
            owner_id=owner_id,
            created_at=(
                float(value["created_at"])
                if value.get("created_at") is not None
                else time.time()
            ),
            started_at=float(value["started_at"]) if value.get("started_at") is not None else None,
            finished_at=float(value["finished_at"]) if value.get("finished_at") is not None else None,
            progress=int(value.get("progress") or 0),
            message=str(value.get("message") or ""),
            progress_details=dict(value.get("progress_details") or {}),
        )
        result = value.get("result")
        if isinstance(result, Mapping):
            result_status = _coerce_run_status(result.get("status") or status.value)
            record.result = ToolRunResult(
                result_status,
                str(result.get("message") or ""),
                {},
                (),
                tuple(str(item) for item in result.get("warnings", ()) or ()),
            )
        return record
    except (KeyError, TypeError, ValueError):
        return None


def _forbidden_payload(tool_id: str, grant: ToolGrant) -> dict[str, Any]:
    return {
        "status": "forbidden",
        "code": grant.code,
        "message": grant.message,
        "tool_id": str(tool_id),
    }


def _profile_identity_error(
    execution_profile: object,
) -> dict[str, Any] | None:
    if type(execution_profile) is ExecutionProfile:
        identity_error = execution_profile_identity_error(
            host_surface=execution_profile.host_surface,
            owner_id=execution_profile.owner_id,
        )
        if identity_error is None:
            return None
    return {
        "status": "forbidden",
        "code": "tool_profile_identity_required",
        "message": "tool execution profile identity is required",
    }


def _coerce_run_status(value: Any) -> ToolRunStatus:
    normalized = str(value or "").strip().lower()
    aliases = {
        "ok": ToolRunStatus.SUCCEEDED,
        "success": ToolRunStatus.SUCCEEDED,
        "error": ToolRunStatus.FAILED,
        "canceled": ToolRunStatus.CANCELLED,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return ToolRunStatus(normalized)
    except ValueError:
        return ToolRunStatus.FAILED


def _atomic_write_json(path: Path, value: Any, *, cache_root: Path | None = None) -> None:
    root = Path(cache_root) if cache_root is not None else Path(user_cache_root())
    _storage_atomic_write_json(Path(path), value, cache_root=root, max_bytes=MAX_HISTORY_BYTES)


__all__ = ["ToolRunnerService"]
