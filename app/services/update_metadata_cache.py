"""Atomic index for the currently active signed update metadata generation."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Callable, Iterable

from app.services.updater_file_lock import exclusive_file_lock
from app.services.updater_launch_capability import (
    MAX_MANIFEST_BINDING_BYTES,
    MAX_SIGNATURE_BINDING_BYTES,
    file_sha256,
)
from app.services.updater_cleanup import run_typed_cleanup_actions
from app.services.updater_path_safety import (
    ensure_approved_directory,
    ensure_regular_or_missing,
    open_regular_binary,
    require_regular_file,
)

ACTIVE_GENERATION_NAME = "active-generation.json"
ACTIVE_GENERATION_SCHEMA = 2
IDENTITY_LEDGER_SCHEMA = 1
MAX_ACTIVE_GENERATION_BYTES = 256_000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GENERATION_LOCK = RLock()


class MetadataIdentityConflictError(ValueError):
    pass


def release_authority_scope(release_client: object) -> str:
    values = {
        "apiBase": str(getattr(release_client, "api_base", "") or "").rstrip("/"),
        "owner": str(getattr(release_client, "owner", "") or ""),
        "repo": str(getattr(release_client, "repo", "") or ""),
    }
    if not all(values.values()):
        return ""
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def release_discovery_scope(release_client: object, *, page_size: int) -> str:
    """Bind cached metadata to the exact discovery authority and query shape."""

    authority = release_authority_scope(release_client)
    if not authority:
        return ""
    payload = {
        "authority": json.loads(authority),
        "endpoint": "releases",
        "pageSize": int(page_size),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ActiveMetadataRecord:
    manifest_name: str
    signature_name: str
    manifest_sha256: str
    signature_sha256: str
    candidate_id: str = ""
    release_url: str = ""
    tag_name: str = ""
    release_name: str = ""


@dataclass(frozen=True, slots=True)
class ActiveMetadataGeneration:
    scope: str
    checked_at: str
    etag: str
    last_modified: str
    records: tuple[ActiveMetadataRecord, ...]


def metadata_record(
    manifest_path: Path,
    signature_path: Path,
    *,
    release_url: str = "",
    tag_name: str = "",
    release_name: str = "",
    candidate_id: str = "",
) -> ActiveMetadataRecord:
    return ActiveMetadataRecord(
        manifest_name=Path(manifest_path).name,
        signature_name=Path(signature_path).name,
        manifest_sha256=_sha256_path(
            manifest_path,
            approved_root=Path(manifest_path).parent,
            maximum=MAX_MANIFEST_BINDING_BYTES,
        ),
        signature_sha256=_sha256_path(
            signature_path,
            approved_root=Path(signature_path).parent,
            maximum=MAX_SIGNATURE_BINDING_BYTES,
        ),
        candidate_id=str(candidate_id or ""),
        release_url=str(release_url or ""),
        tag_name=str(tag_name or ""),
        release_name=str(release_name or ""),
    )


def write_active_generation(
    metadata_dir: Path,
    *,
    etag: str,
    last_modified: str,
    records: Iterable[ActiveMetadataRecord],
    atomic_write_text: Callable[[Path, str], None],
    scope: str = "",
    identity_scope: str = "",
    checked_at: str = "",
) -> None:
    normalized_records = tuple(records)
    if not normalized_records:
        raise ValueError("active update metadata generation cannot be empty")
    normalized_scope = str(scope or "")
    normalized_checked_at = str(checked_at or "") or datetime.now(timezone.utc).isoformat()
    record_payloads = [_record_payload(record) for record in normalized_records]
    payload = {
        "schema": ACTIVE_GENERATION_SCHEMA,
        "scope": normalized_scope,
        "checkedAt": normalized_checked_at,
        "etag": str(etag or ""),
        "lastModified": str(last_modified or ""),
        "candidates": record_payloads,
    }
    payload["candidateDigest"] = _candidate_set_digest(record_payloads)
    root = ensure_approved_directory(metadata_dir)
    with _GENERATION_LOCK, exclusive_file_lock(
        root / ".metadata.lock",
        approved_root=root,
    ):
        if identity_scope:
            _merge_identity_ledger(
                root,
                str(identity_scope),
                normalized_records,
                atomic_write_text=atomic_write_text,
            )
        atomic_write_text(
            root / ACTIVE_GENERATION_NAME,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        )


def load_active_generation(
    metadata_dir: Path,
    *,
    expected_etag: str = "",
    expected_last_modified: str = "",
    require_discovery_token: bool = False,
    expected_scope: str = "",
) -> ActiveMetadataGeneration | None:
    try:
        root = ensure_approved_directory(metadata_dir)
        index_path = ensure_regular_or_missing(
            root / ACTIVE_GENERATION_NAME,
            approved_root=root,
        )
        if not index_path.exists():
            return None
    except OSError:
        return None
    try:
        with _GENERATION_LOCK:
            raw = _read_bounded_text(
                index_path,
                approved_root=root,
                maximum=MAX_ACTIVE_GENERATION_BYTES,
            )
            payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != ACTIVE_GENERATION_SCHEMA:
        return None
    etag = payload.get("etag")
    last_modified = payload.get("lastModified")
    scope = payload.get("scope")
    checked_at = payload.get("checkedAt")
    candidates = payload.get("candidates")
    candidate_digest = payload.get("candidateDigest")
    if not all(
        type(value) is str
        for value in (scope, checked_at, etag, last_modified, candidate_digest)
    ):
        return None
    if expected_scope and scope != expected_scope:
        return None
    if require_discovery_token and not (expected_etag or expected_last_modified):
        return None
    if expected_etag and etag != expected_etag:
        return None
    if expected_last_modified and last_modified != expected_last_modified:
        return None
    if not isinstance(candidates, list) or not candidates:
        return None
    if candidate_digest != _candidate_set_digest(candidates):
        return None
    records: list[ActiveMetadataRecord] = []
    for candidate in candidates:
        record = _parse_record(candidate)
        if record is None or _record_paths(metadata_dir, record) is None:
            return None
        records.append(record)
    return ActiveMetadataGeneration(
        scope=scope,
        checked_at=checked_at,
        etag=etag,
        last_modified=last_modified,
        records=tuple(records),
    )


def verified_record_paths(
    metadata_dir: Path,
    record: ActiveMetadataRecord,
) -> tuple[Path, Path] | None:
    paths = _record_paths(metadata_dir, record)
    if paths is None:
        return None
    manifest_path, signature_path = paths
    try:
        root = ensure_approved_directory(metadata_dir)
        if (
            _sha256_path(
                manifest_path,
                approved_root=root,
                maximum=MAX_MANIFEST_BINDING_BYTES,
            )
            != record.manifest_sha256
            or _sha256_path(
                signature_path,
                approved_root=root,
                maximum=MAX_SIGNATURE_BINDING_BYTES,
            )
            != record.signature_sha256
        ):
            return None
    except OSError:
        return None
    return manifest_path, signature_path


def _parse_record(value: object) -> ActiveMetadataRecord | None:
    if not isinstance(value, dict):
        return None
    keys = (
        "manifestName",
        "signatureName",
        "manifestSha256",
        "signatureSha256",
        "candidateId",
        "releaseUrl",
        "tagName",
        "releaseName",
    )
    if not all(type(value.get(key, "")) is str for key in keys):
        return None
    manifest_digest = value.get("manifestSha256", "").lower()
    signature_digest = value.get("signatureSha256", "").lower()
    if _SHA256.fullmatch(manifest_digest) is None or _SHA256.fullmatch(signature_digest) is None:
        return None
    return ActiveMetadataRecord(
        manifest_name=value.get("manifestName", ""),
        signature_name=value.get("signatureName", ""),
        manifest_sha256=manifest_digest,
        signature_sha256=signature_digest,
        candidate_id=value.get("candidateId", ""),
        release_url=value.get("releaseUrl", ""),
        tag_name=value.get("tagName", ""),
        release_name=value.get("releaseName", ""),
    )


def _record_payload(record: ActiveMetadataRecord) -> dict[str, str]:
    return {
        "manifestName": record.manifest_name,
        "signatureName": record.signature_name,
        "manifestSha256": record.manifest_sha256,
        "signatureSha256": record.signature_sha256,
        "candidateId": record.candidate_id,
        "releaseUrl": record.release_url,
        "tagName": record.tag_name,
        "releaseName": record.release_name,
    }


def _candidate_set_digest(candidates: object) -> str:
    encoded = json.dumps(
        candidates,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_paths(
    metadata_dir: Path,
    record: ActiveMetadataRecord,
) -> tuple[Path, Path] | None:
    try:
        root = ensure_approved_directory(metadata_dir)
    except OSError:
        return None
    names = (record.manifest_name, record.signature_name)
    if any(not name or Path(name).name != name for name in names):
        return None
    try:
        paths = tuple(
            require_regular_file(root / name, approved_root=root)
            for name in names
        )
    except OSError:
        return None
    return paths[0], paths[1]


def _sha256_path(
    path: Path,
    *,
    approved_root: Path,
    maximum: int,
) -> str:
    return file_sha256(
        path,
        approved_root=approved_root,
        max_bytes=maximum,
    )


def generation_is_fresh(
    generation: ActiveMetadataGeneration,
    *,
    max_age_seconds: int,
    now: datetime | None = None,
) -> bool:
    if type(max_age_seconds) is not int or max_age_seconds <= 0:
        return False
    raw = generation.checked_at.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        checked_at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        return False
    age = (current.astimezone(timezone.utc) - checked_at.astimezone(timezone.utc)).total_seconds()
    return 0 <= age < max_age_seconds


def _merge_identity_ledger(
    metadata_dir: Path,
    scope: str,
    records: tuple[ActiveMetadataRecord, ...],
    *,
    atomic_write_text: Callable[[Path, str], None],
) -> None:
    ledger_path = metadata_dir / f"identity-ledger-{hashlib.sha256(scope.encode('utf-8')).hexdigest()[:24]}.json"
    identities = _load_identity_ledger(ledger_path, scope)
    for record in records:
        if not record.candidate_id:
            continue
        digest_pair = (record.manifest_sha256, record.signature_sha256)
        previous = identities.get(record.candidate_id)
        if previous is not None and previous != digest_pair:
            raise MetadataIdentityConflictError(
                f"conflicting signed manifests for release identity {record.candidate_id}"
            )
        identities[record.candidate_id] = digest_pair
    identity_payload = {
        candidate_id: [manifest_digest, signature_digest]
        for candidate_id, (manifest_digest, signature_digest) in sorted(identities.items())
    }
    payload = {
        "schema": IDENTITY_LEDGER_SCHEMA,
        "scope": scope,
        "identities": identity_payload,
        "identityDigest": _candidate_set_digest(identity_payload),
    }
    atomic_write_text(
        ledger_path,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
    )


def _load_identity_ledger(path: Path, scope: str) -> dict[str, tuple[str, str]]:
    try:
        root = ensure_approved_directory(path.parent)
        ledger = ensure_regular_or_missing(path, approved_root=root)
        if not ledger.exists():
            return {}
        payload = json.loads(
            _read_bounded_text(
                ledger,
                approved_root=root,
                maximum=MAX_ACTIVE_GENERATION_BYTES,
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MetadataIdentityConflictError("update identity ledger is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != IDENTITY_LEDGER_SCHEMA
        or payload.get("scope") != scope
        or not isinstance(payload.get("identities"), dict)
        or payload.get("identityDigest")
        != _candidate_set_digest(payload.get("identities"))
    ):
        raise MetadataIdentityConflictError("update identity ledger is invalid")
    identities: dict[str, tuple[str, str]] = {}
    for candidate_id, pair in payload["identities"].items():
        if (
            type(candidate_id) is not str
            or not candidate_id
            or type(pair) is not list
            or len(pair) != 2
            or any(type(value) is not str or _SHA256.fullmatch(value) is None for value in pair)
        ):
            raise MetadataIdentityConflictError("update identity ledger is invalid")
        identities[candidate_id] = (pair[0], pair[1])
    return identities


def _read_bounded_text(
    path: Path,
    *,
    approved_root: Path,
    maximum: int,
) -> str:
    stream = None
    primary_error: BaseException | None = None
    try:
        stream = open_regular_binary(path, approved_root=approved_root, mode="rb")
        if os.fstat(stream.fileno()).st_size > maximum:
            raise OSError("metadata cache file exceeds its size limit")
        raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise OSError("metadata cache file exceeds its size limit")
        return raw.decode("utf-8")
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if stream is not None:
            run_typed_cleanup_actions(
                primary_error,
                ((stream.close, "metadata cache handle cleanup failed"),),
                error_factory=OSError,
                failure_message="metadata cache handle cleanup failed",
            )
