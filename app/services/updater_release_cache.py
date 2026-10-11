"""Bounded, no-follow persistence for GitHub release discovery validators."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.services.updater_atomic_io import atomic_write_text
from app.services.updater_manifest_json import (
    StrictManifestJsonError,
    load_strict_manifest_json,
)
from app.services.updater_path_safety import (
    ensure_approved_directory,
    open_regular_binary,
)

MAX_RELEASE_CACHE_BYTES = 64 * 1024


def load_release_cache(
    path: Path,
    *,
    expected_scope: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a trusted validator cache, or an empty cache on any read defect."""

    stream = None
    primary_error: BaseException | None = None
    read_failed = False
    cleanup_failed = False
    payload: dict[str, Any] = {}
    try:
        root = ensure_approved_directory(Path(path).parent)
        stream = open_regular_binary(path, approved_root=root, mode="rb")
        raw = stream.read(MAX_RELEASE_CACHE_BYTES + 1)
        if len(raw) > MAX_RELEASE_CACHE_BYTES:
            read_failed = True
        else:
            payload = dict(load_strict_manifest_json(raw))
    except (OSError, StrictManifestJsonError):
        read_failed = True
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if stream is not None:
            try:
                stream.close()
            except OSError:
                if primary_error is None:
                    cleanup_failed = True

    if read_failed or cleanup_failed:
        return {}

    if any(payload.get(key) != value for key, value in expected_scope.items()):
        return {}
    for key in ("etag", "last_modified", "last_check_at", "release_url"):
        if key in payload and type(payload[key]) is not str:
            return {}
    if not (payload.get("etag") or payload.get("last_modified")):
        return {}
    return payload


def save_release_cache(
    path: Path,
    *,
    scope: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> None:
    scoped = {**dict(scope), **dict(payload)}
    atomic_write_text(
        path,
        json.dumps(scoped, ensure_ascii=False, indent=2),
    )
