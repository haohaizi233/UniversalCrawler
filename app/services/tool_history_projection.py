"""Safe public and durable projections for tool runtime history."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.core.tools.builtin.link_parser import LINK_FORMAT_HINTS, normalize_link_url
from app.core.tools.contracts import ToolRunResult, ToolRunStatus


_MAX_LINK_CANDIDATES = 64
_MAX_LINK_SOURCE_ROWS = 512
_MAX_LINK_TEXT = 16_384
_MAX_LINK_STRING = 512
_MAX_PLATFORM_STRING = 64
_MAX_REASON_CODE = 80
MAX_HISTORY_MESSAGE_CHARS = 4096
MAX_HISTORY_WARNING_CHARS = 1024
MAX_HISTORY_WARNINGS = 32
MAX_HISTORY_WARNINGS_TOTAL_CHARS = 16 * 1024
_LINK_CANDIDATE_KEYS = (
    "candidate_id",
    "display_url",
    "platform",
    "resource_kind",
    "format_hint",
    "expanded",
    "admittable",
    "admission_reason_code",
)
_RESOURCE_KINDS = frozenset({"page", "media", "playlist"})


def project_history_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a recursively positive projection for public and durable history."""

    projected: dict[str, Any] = {}
    for key in (
        "run_id",
        "tool_id",
        "host_surface",
        "owner_id",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "progress",
        "message",
    ):
        value = record.get(key)
        if key in record and _is_scalar(value):
            projected[key] = (
                _project_bounded_scalar(value, MAX_HISTORY_MESSAGE_CHARS)
                if key == "message"
                else _project_scalar(value)
            )

    result = record.get("result")
    if isinstance(result, Mapping):
        projected_result: dict[str, Any] = {}
        for key in ("status", "message"):
            value = result.get(key)
            if key in result and _is_scalar(value):
                projected_result[key] = (
                    _project_bounded_scalar(value, MAX_HISTORY_MESSAGE_CHARS)
                    if key == "message"
                    else _project_scalar(value)
                )
        if "warnings" in result:
            warnings = _project_scalar_list(
                result.get("warnings"),
                max_items=MAX_HISTORY_WARNINGS,
                max_text=MAX_HISTORY_WARNING_CHARS,
                max_total_text=MAX_HISTORY_WARNINGS_TOTAL_CHARS,
            )
            if warnings is not None:
                projected_result["warnings"] = warnings
        if projected_result:
            projected["result"] = projected_result
    return projected


def project_runtime_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Add bounded link-parser candidates to an owner-scoped runtime record."""

    projected = project_history_record(record)
    if record.get("tool_id") != "link_parser" or record.get("status") != "succeeded":
        return projected

    result = record.get("result")
    projected_result = projected.get("result")
    if (
        not isinstance(result, Mapping)
        or result.get("status") != "succeeded"
        or not isinstance(projected_result, dict)
    ):
        return projected
    data = result.get("data")
    if not isinstance(data, Mapping):
        return projected
    links = data.get("links")
    if not _is_row_sequence(links):
        return projected

    projected_result["data"] = {"links": project_link_parser_runtime_links(data)}
    return projected


def project_link_parser_runtime_links(value: Any) -> list[dict[str, Any]]:
    """Return bounded, positive-projection link-parser candidate rows."""

    links = value.get("links") if isinstance(value, Mapping) else value
    if not _is_row_sequence(links):
        return []
    projected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    remaining_text = _MAX_LINK_TEXT
    for index, value in enumerate(links):
        if index >= _MAX_LINK_SOURCE_ROWS or len(projected) >= _MAX_LINK_CANDIDATES:
            break
        row = _project_link_candidate(value)
        if row is None or row["candidate_id"] in seen_ids:
            continue
        text_size = sum(len(item) for item in row.values() if isinstance(item, str))
        if text_size > remaining_text:
            continue
        seen_ids.add(row["candidate_id"])
        projected.append(row)
        remaining_text -= text_size
    return projected


def _project_link_candidate(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    row = {key: value.get(key) for key in _LINK_CANDIDATE_KEYS}
    candidate_id = row["candidate_id"]
    display_url = row["display_url"]
    platform = row["platform"]
    resource_kind = row["resource_kind"]
    format_hint = row["format_hint"]
    expanded = row["expanded"]
    admittable = row["admittable"]
    reason_code = row["admission_reason_code"]
    if (
        not _is_candidate_id(candidate_id)
        or not _is_redacted_display_url(display_url)
        or not _is_bounded_text(platform, _MAX_PLATFORM_STRING)
        or resource_kind not in _RESOURCE_KINDS
        or not isinstance(format_hint, str)
        or format_hint not in LINK_FORMAT_HINTS
        or type(expanded) is not bool
        or type(admittable) is not bool
        or not _is_reason_code(reason_code, required=not admittable)
    ):
        return None
    return row


def _is_candidate_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_redacted_display_url(value: Any) -> bool:
    if (
        not _is_bounded_text(value, _MAX_LINK_STRING)
        or any(character.isspace() for character in value)
        or "?" in value
        or "#" in value
    ):
        return False
    try:
        parts = urlsplit(value)
        canonical_url = normalize_link_url(value)
    except (TypeError, ValueError):
        return False
    return (
        canonical_url == value
        and parts.query == ""
        and parts.fragment == ""
        and parts.path in {"/", "/[redacted]"}
    )


def _is_bounded_text(value: Any, limit: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and not any(
            unicodedata.category(character).startswith("C") for character in value
        )
    )


def _is_reason_code(value: Any, *, required: bool) -> bool:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_REASON_CODE
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        return False
    return bool(value) is required


def _is_row_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _project_scalar_list(
    value: Any,
    *,
    max_items: int,
    max_text: int,
    max_total_text: int,
) -> list[Any] | None:
    if not isinstance(value, (list, tuple)):
        return None
    projected: list[Any] = []
    remaining_text = max_total_text
    for item in value:
        if len(projected) >= max_items:
            break
        if _is_scalar(item):
            if isinstance(item, str):
                if remaining_text <= 0:
                    break
                item = _project_bounded_scalar(item, min(max_text, remaining_text))
                remaining_text -= len(item)
            projected.append(item)
    return projected


def _project_scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if not any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        return value
    return "".join(
        "\ufffd" if 0xD800 <= ord(character) <= 0xDFFF else character
        for character in value
    )


def _project_bounded_scalar(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return _project_scalar(value[:limit])
    return value


def json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def redact_parameters(
    parameters: Mapping[str, Any],
    *,
    sensitive_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    sensitive = (
        "token",
        "secret",
        "password",
        "cookie",
        "authorization",
        "proxy_auth",
    )
    explicit = {str(key).casefold() for key in sensitive_fields}
    result: dict[str, Any] = {}
    for key, value in parameters.items():
        normalized = str(key).casefold()
        result[str(key)] = (
            "[redacted]"
            if normalized in explicit or any(marker in normalized for marker in sensitive)
            else json_value(value)
        )
    return result


def public_validation_payload(
    payload: Mapping[str, Any],
    *,
    tool_id: str,
) -> dict[str, Any]:
    public_payload = dict(payload)
    parameters = payload.get("parameters")
    if isinstance(parameters, Mapping):
        public_payload["parameters"] = redact_parameters(
            parameters,
            sensitive_fields=("text",) if tool_id == "link_parser" else (),
        )
    return public_payload


def normalize_tool_result(value: Any) -> ToolRunResult:
    if isinstance(value, ToolRunResult):
        return value
    if value is None:
        return ToolRunResult.success("tool run completed")
    if isinstance(value, Mapping):
        status = str(value.get("status") or "succeeded").strip().lower()
        if status in {"error", "failed", ToolRunStatus.FAILED.value}:
            return ToolRunResult.failure(
                str(value.get("message") or "tool run failed"),
                data=dict(value.get("data") or {}),
            )
        if status in {"cancelled", "canceled"}:
            return ToolRunResult.cancelled(
                str(value.get("message") or "tool run cancelled")
            )
        return ToolRunResult.success(
            str(value.get("message") or "tool run completed"),
            data=dict(value.get("data") or value),
            output_paths=tuple(
                str(path) for path in value.get("output_paths", ()) or ()
            ),
            warnings=tuple(str(item) for item in value.get("warnings", ()) or ()),
        )
    return ToolRunResult.success(
        "tool run completed",
        data={"value": json_value(value)},
    )


__all__ = [
    "MAX_HISTORY_MESSAGE_CHARS",
    "MAX_HISTORY_WARNINGS",
    "MAX_HISTORY_WARNING_CHARS",
    "MAX_HISTORY_WARNINGS_TOTAL_CHARS",
    "json_value",
    "normalize_tool_result",
    "project_history_record",
    "project_link_parser_runtime_links",
    "project_runtime_record",
    "public_validation_payload",
    "redact_parameters",
]
