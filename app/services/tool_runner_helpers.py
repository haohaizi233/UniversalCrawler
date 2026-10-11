"""Tool-runner validation, history merge, and shutdown helpers."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.tools.contracts import (
    ToolGrant,
    ToolRunResult,
    ToolRunStatus,
    ToolValidationResult,
)
from app.debug_logger import debug_logger
from app.services.tool_history_projection import project_history_record
from app.services.tool_history_storage import (
    MAX_HISTORY_BYTES,
    atomic_write_json as _storage_atomic_write_json,
)
from app.utils.runtime_paths import user_cache_root
from shared.execution_profile import (
    ExecutionProfile,
    execution_profile_identity_error,
)

if TYPE_CHECKING:
    from app.services.tool_runner_service import _RunRecord


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
    from app.services.tool_runner_service import _RunRecord

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


def _public_snapshot_matches(raw: Any, projected: Any) -> bool:
    """True when a loaded row is already the public projection."""

    if isinstance(projected, Mapping):
        if not isinstance(raw, Mapping) or set(map(str, raw)) != set(map(str, projected)):
            return False
        return all(
            _public_snapshot_matches(raw[key], value) for key, value in projected.items()
        )
    if isinstance(projected, (list, tuple)):
        if not isinstance(raw, (list, tuple)) or len(raw) != len(projected):
            return False
        return all(
            _public_snapshot_matches(left, right)
            for left, right in zip(raw, projected, strict=True)
        )
    return raw == projected


def _merge_history_rows(
    disk: Any,
    rows: Sequence[Mapping[str, Any]],
    removed_ids: set[str],
) -> list[dict[str, Any]]:
    """Replace this process's rows and keep every other owner's durable rows."""

    removed = {str(run_id) for run_id in removed_ids if str(run_id).strip()}
    replacements: dict[str, dict[str, Any]] = {}
    for row in rows:
        run_id = str(row.get("run_id") or "").strip()
        if run_id and run_id not in removed:
            replacements[run_id] = dict(row)
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(disk, list):
        for item in disk:
            if not isinstance(item, Mapping):
                continue
            run_id = str(item.get("run_id") or "").strip()
            if not run_id or run_id in removed or run_id in replacements or run_id in seen:
                continue
            merged.append(dict(item))
            seen.add(run_id)
    merged.extend(replacements.values())
    merged.sort(
        key=lambda row: (
            float(row.get("created_at") or 0.0),
            str(row.get("run_id") or ""),
        )
    )
    return merged


def _atomic_write_json(path: Path, value: Any, *, cache_root: Path | None = None) -> None:
    root = Path(cache_root) if cache_root is not None else Path(user_cache_root())
    _storage_atomic_write_json(Path(path), value, cache_root=root, max_bytes=MAX_HISTORY_BYTES)
