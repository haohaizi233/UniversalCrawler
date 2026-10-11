"""Strict JSON decoding for cryptographically signed update manifests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


class StrictManifestJsonError(ValueError):
    pass


def load_strict_manifest_json(payload: bytes) -> Mapping[str, Any]:
    """Reject non-UTF-8, non-finite constants, duplicate keys, and non-objects."""

    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        StrictManifestJsonError,
    ) as exc:
        raise StrictManifestJsonError("manifest is not valid strict JSON") from exc
    if not isinstance(value, Mapping):
        raise StrictManifestJsonError("manifest root must be an object")
    return value


def _reject_constant(value: str) -> object:
    raise StrictManifestJsonError(f"non-finite JSON constant is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictManifestJsonError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result
