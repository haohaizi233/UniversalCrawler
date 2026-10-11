"""Strict response-start validation for resumable updater downloads."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

_CONTENT_RANGE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)\Z")


class RestartFullDownload(Exception):
    """A range request received a full 200 response and must restart cleanly."""


def response_status(
    response: Any,
    *,
    error_factory: Callable[[str], BaseException],
) -> int:
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "code", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else None
    if type(status) is not int or status < 100 or status > 599:
        raise error_factory("download response did not expose a valid HTTP status")
    return status


def validate_download_response(
    status: int,
    headers: Mapping[str, str] | Any,
    *,
    asset_size: int,
    resume_from: int,
    error_factory: Callable[[str], BaseException],
) -> None:
    if resume_from and status == 200:
        raise RestartFullDownload()
    expected_status = 206 if resume_from else 200
    if status != expected_status:
        raise error_factory(f"download failed with HTTP {status}")
    content_length = _required_decimal_header(
        headers,
        "Content-Length",
        error_factory=error_factory,
    )
    expected_length = asset_size - resume_from
    if content_length != expected_length:
        raise error_factory("Content-Length does not match manifest size")
    if not resume_from:
        return
    value = _header_value(
        headers,
        "Content-Range",
        error_factory=error_factory,
    )
    match = _CONTENT_RANGE.fullmatch(value)
    if match is None:
        raise error_factory("Content-Range is invalid")
    start, end, total = (int(part) for part in match.groups())
    if (
        start != resume_from
        or end != asset_size - 1
        or total != asset_size
        or end < start
    ):
        raise error_factory("Content-Range does not match manifest size")


def _required_decimal_header(
    headers: Mapping[str, str] | Any,
    name: str,
    *,
    error_factory: Callable[[str], BaseException],
) -> int:
    value = _header_value(headers, name, error_factory=error_factory)
    if not value.isdigit():
        raise error_factory(f"{name} is missing or invalid")
    return int(value)


def _header_value(
    headers: Mapping[str, str] | Any,
    name: str,
    *,
    error_factory: Callable[[str], BaseException],
) -> str:
    if isinstance(headers, Mapping):
        matches: list[str] = []
        for key, value in headers.items():
            if type(key) is str and key.lower() == name.lower():
                if type(value) is not str:
                    raise error_factory(f"{name} is invalid")
                matches.append(value.strip())
        if len(matches) > 1:
            raise error_factory(f"{name} is duplicated")
        return matches[0] if matches else ""
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        values = get_all(name)
        if values is not None:
            if type(values) is not list or len(values) != 1 or type(values[0]) is not str:
                raise error_factory(f"{name} is duplicated or invalid")
            return values[0].strip()
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return ""
    value = getter(name)
    if value is None:
        return ""
    if type(value) is not str:
        raise error_factory(f"{name} is invalid")
    return value.strip()
