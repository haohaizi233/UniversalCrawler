"""One-time local capabilities for updater launch and post-install restart."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.services.updater_atomic_io import atomic_write_bytes
from app.services.updater_cleanup import run_typed_cleanup_actions
from app.services.updater_file_lock import exclusive_file_lock
from app.services.updater_manifest_json import (
    StrictManifestJsonError,
    load_strict_manifest_json,
)
from app.services.updater_path_safety import (
    ensure_approved_directory,
    ensure_regular_or_missing,
    open_regular_binary,
    replace_regular_file,
    require_regular_file,
)
from shared.release_identity import RELEASE_IDENTITY_FILENAME, ReleaseIdentity

LAUNCH_CAPABILITY_TOKEN_ENV = "UCRAWL_UPDATE_LAUNCH_TOKEN"
RESTART_HANDOFF_TOKEN_ENV = "UCRAWL_RESTART_HANDOFF_TOKEN"
LAUNCH_CAPABILITY_TTL_SECONDS = 10 * 60
RESTART_HANDOFF_TTL_SECONDS = 6 * 60 * 60
MAX_CAPABILITY_BYTES = 64 * 1024
MAX_PATH_TEXT = 4096
MAX_RESTART_ARGUMENTS = 64
MAX_INSTALLER_BINDING_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_BINDING_BYTES = 2_000_000
MAX_SIGNATURE_BINDING_BYTES = 64
MAX_REVISION_AUTHORIZATION_BYTES = 16 * 1024


class UpdaterCapabilityError(RuntimeError):
    """A local updater capability is missing, stale, unsafe, or mismatched."""


@dataclass(frozen=True)
class LaunchBinding:
    installer_path: Path
    manifest_path: Path
    signature_path: Path
    log_path: Path
    install_root: Path
    target_identity: ReleaseIdentity
    current_identity: ReleaseIdentity
    restart_argv: tuple[str, ...]
    parent_pid: int
    restart_handoff_path: Path | None = None
    revision_authorization_path: Path | None = None


@dataclass(frozen=True)
class RestartHandoffBinding:
    restart_argv: tuple[str, ...]
    install_root: Path
    target_identity: ReleaseIdentity
    log_path: Path


def issue_launch_capability(
    binding: LaunchBinding,
    *,
    staging_root: Path,
    metadata_root: Path,
    now: float | None = None,
) -> tuple[Path, str]:
    """Persist a short-lived capability while keeping its bearer token in memory."""

    staging = ensure_approved_directory(staging_root)
    metadata = ensure_approved_directory(metadata_root)
    issued_at = int(time.time() if now is None else now)
    token = secrets.token_urlsafe(32)
    target = metadata / f".ucrawl-launch-capability-{secrets.token_hex(16)}.json"
    payload = _one_time_payload(
        purpose="launch",
        token=token,
        issued_at=issued_at,
        expires_at=issued_at + LAUNCH_CAPABILITY_TTL_SECONDS,
        binding=_launch_payload(
            binding,
            staging_root=staging,
            metadata_root=metadata,
        ),
    )
    atomic_write_bytes(target, _encode_payload(payload))
    return target, token


def consume_launch_capability(
    path: Path,
    token: str,
    *,
    expected: LaunchBinding,
    staging_root: Path,
    metadata_root: Path,
    now: float | None = None,
) -> None:
    """Atomically consume and compare every launch-authority input."""

    staging = ensure_approved_directory(staging_root)
    metadata = ensure_approved_directory(metadata_root)
    payload = _claim_one_time_payload(
        path,
        token,
        purpose="launch",
        filename_prefix=".ucrawl-launch-capability-",
        approved_root=metadata,
        maximum_lifetime=LAUNCH_CAPABILITY_TTL_SECONDS,
        now=now,
    )
    actual = payload.get("binding")
    expected_payload = _launch_payload(
        expected,
        staging_root=staging,
        metadata_root=metadata,
    )
    if not _json_exact_equal(actual, expected_payload):
        raise UpdaterCapabilityError("updater launch capability does not match request")


def issue_restart_handoff(
    path: Path,
    binding: RestartHandoffBinding,
    *,
    staging_root: Path,
    now: float | None = None,
) -> str:
    """Write the exact restart handoff authorized by the consumed launch grant."""

    staging = ensure_approved_directory(staging_root)
    target = ensure_regular_or_missing(path, approved_root=staging)
    issued_at = int(time.time() if now is None else now)
    token = secrets.token_urlsafe(32)
    payload = _one_time_payload(
        purpose="restart",
        token=token,
        issued_at=issued_at,
        expires_at=issued_at + RESTART_HANDOFF_TTL_SECONDS,
        binding=_restart_payload(binding, staging_root=staging),
    )
    atomic_write_bytes(target, _encode_payload(payload))
    return token


def consume_restart_handoff(
    path: Path,
    token: str,
    *,
    expected_install_root: Path,
    staging_root: Path,
    now: float | None = None,
) -> RestartHandoffBinding:
    """Atomically claim a token-bound post-install restart handoff."""

    staging = ensure_approved_directory(staging_root)
    expected_root = ensure_approved_directory(expected_install_root)
    payload = _claim_one_time_payload(
        path,
        token,
        purpose="restart",
        filename_prefix=".ucrawl-restart-",
        approved_root=staging,
        maximum_lifetime=RESTART_HANDOFF_TTL_SECONDS,
        now=now,
    )
    binding = _restart_binding_from_payload(payload.get("binding"), staging_root=staging)
    if _path_text(binding.install_root) != _path_text(expected_root):
        raise UpdaterCapabilityError("restart handoff install root does not match helper")
    return binding


def load_installed_release_identity(install_root: Path) -> ReleaseIdentity:
    """Read the packaged identity marker without fallback or link traversal."""

    root = ensure_approved_directory(install_root)
    marker = require_regular_file(root / RELEASE_IDENTITY_FILENAME, approved_root=root)
    try:
        payload = load_strict_manifest_json(_read_bounded(marker, root, 16 * 1024))
        version = payload.get("version")
        revision = payload.get("revision", payload.get("releaseRevision", 0))
        tag = payload.get("tag")
        if type(version) is not str or type(tag) is not str:
            raise ValueError("identity fields must be strings")
        identity = ReleaseIdentity(version, revision)
        if tag != identity.tag:
            raise ValueError("identity tag does not match")
        return identity
    except (OSError, StrictManifestJsonError, TypeError, ValueError) as exc:
        raise UpdaterCapabilityError("installed release identity marker is invalid") from exc


def file_sha256(
    path: Path,
    *,
    approved_root: Path,
    max_bytes: int | None = None,
) -> str:
    digest = hashlib.sha256()
    total = 0
    stream = None
    primary_error: BaseException | None = None
    try:
        stream = open_regular_binary(path, approved_root=approved_root, mode="rb")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise UpdaterCapabilityError("updater file exceeds size limit")
            digest.update(chunk)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if stream is not None:
            run_typed_cleanup_actions(
                primary_error,
                ((stream.close, "updater capability file cleanup failed"),),
                error_factory=UpdaterCapabilityError,
                failure_message="updater capability file cleanup failed",
            )
    return digest.hexdigest()


def _launch_payload(
    binding: LaunchBinding,
    *,
    staging_root: Path,
    metadata_root: Path,
) -> dict[str, Any]:
    if not isinstance(binding, LaunchBinding):
        raise UpdaterCapabilityError("updater launch binding is invalid")
    installer = require_regular_file(
        binding.installer_path,
        approved_root=staging_root,
    )
    manifest = require_regular_file(
        binding.manifest_path,
        approved_root=metadata_root,
    )
    signature = require_regular_file(
        binding.signature_path,
        approved_root=metadata_root,
    )
    log_path = ensure_regular_or_missing(binding.log_path, approved_root=staging_root)
    install_root = ensure_approved_directory(binding.install_root)
    handoff = None
    if binding.restart_handoff_path is not None:
        handoff = ensure_regular_or_missing(
            binding.restart_handoff_path,
            approved_root=staging_root,
        )
    revision_authorization = None
    if binding.revision_authorization_path is not None:
        revision_authorization = require_regular_file(
            binding.revision_authorization_path,
            approved_root=metadata_root,
        )
    restart_argv = _validated_argv(binding.restart_argv)
    if type(binding.parent_pid) is not int or binding.parent_pid <= 0:
        raise UpdaterCapabilityError("updater launch parent pid is invalid")
    return {
        "installer": _file_binding(
            installer,
            staging_root,
            max_bytes=MAX_INSTALLER_BINDING_BYTES,
        ),
        "manifest": _file_binding(
            manifest,
            metadata_root,
            max_bytes=MAX_MANIFEST_BINDING_BYTES,
        ),
        "signature": _file_binding(
            signature,
            metadata_root,
            max_bytes=MAX_SIGNATURE_BINDING_BYTES,
        ),
        "revisionAuthorization": (
            _file_binding(
                revision_authorization,
                metadata_root,
                max_bytes=MAX_REVISION_AUTHORIZATION_BYTES,
            )
            if revision_authorization is not None
            else None
        ),
        "logPath": _path_text(log_path),
        "installRoot": _path_text(install_root),
        "target": _identity_payload(binding.target_identity),
        "current": _identity_payload(binding.current_identity),
        "restartArgv": list(restart_argv),
        "parentPid": binding.parent_pid,
        "restartHandoffPath": _path_text(handoff) if handoff is not None else None,
    }


def _restart_payload(
    binding: RestartHandoffBinding,
    *,
    staging_root: Path,
) -> dict[str, Any]:
    if not isinstance(binding, RestartHandoffBinding):
        raise UpdaterCapabilityError("restart handoff binding is invalid")
    install_root = ensure_approved_directory(binding.install_root)
    log_path = ensure_regular_or_missing(binding.log_path, approved_root=staging_root)
    return {
        "restartArgv": list(_validated_argv(binding.restart_argv, required=True)),
        "installRoot": _path_text(install_root),
        "target": _identity_payload(binding.target_identity),
        "logPath": _path_text(log_path),
    }


def _restart_binding_from_payload(
    value: object,
    *,
    staging_root: Path,
) -> RestartHandoffBinding:
    if not isinstance(value, Mapping) or set(value) != {
        "restartArgv",
        "installRoot",
        "target",
        "logPath",
    }:
        raise UpdaterCapabilityError("restart handoff binding is invalid")
    restart_argv = _validated_argv(value.get("restartArgv"), required=True)
    install_text = _bounded_text(value.get("installRoot"), "install root")
    log_text = _bounded_text(value.get("logPath"), "log path")
    install_root = ensure_approved_directory(Path(install_text))
    log_path = ensure_regular_or_missing(Path(log_text), approved_root=staging_root)
    target = _identity_from_payload(value.get("target"))
    binding = RestartHandoffBinding(
        restart_argv=restart_argv,
        install_root=install_root,
        target_identity=target,
        log_path=log_path,
    )
    expected = _restart_payload(binding, staging_root=staging_root)
    if not _json_exact_equal(value, expected):
        raise UpdaterCapabilityError("restart handoff binding is invalid")
    return binding


def _claim_one_time_payload(
    path: Path,
    token: str,
    *,
    purpose: str,
    filename_prefix: str,
    approved_root: Path,
    maximum_lifetime: int,
    now: float | None,
) -> Mapping[str, Any]:
    try:
        requested = require_regular_file(path, approved_root=approved_root)
        if (
            not requested.name.startswith(filename_prefix)
            or requested.suffix != ".json"
        ):
            raise OSError("updater capability filename is invalid")
        lock_path = approved_root / f".{requested.name}.claim.lock"
        with exclusive_file_lock(
            lock_path,
            approved_root=approved_root,
            error_factory=UpdaterCapabilityError,
        ):
            requested = require_regular_file(requested, approved_root=approved_root)
            raw = _read_bounded(requested, approved_root, MAX_CAPABILITY_BYTES)
            payload = load_strict_manifest_json(raw)
            _validate_envelope(
                payload,
                token,
                purpose=purpose,
                maximum_lifetime=maximum_lifetime,
                now=now,
            )
            claimed = requested.with_name(
                f".{requested.stem}.claimed-{os.getpid()}-"
                f"{secrets.token_hex(8)}.json"
            )
            replace_regular_file(requested, claimed, approved_root=approved_root)
            claimed_raw = _read_bounded(
                claimed,
                approved_root,
                MAX_CAPABILITY_BYTES,
            )
            if not hmac.compare_digest(raw, claimed_raw):
                raise UpdaterCapabilityError(
                    "updater capability changed while it was claimed"
                )
    except OSError as exc:
        raise UpdaterCapabilityError(
            "updater capability is missing or invalid"
        ) from exc
    except (StrictManifestJsonError, TypeError, ValueError) as exc:
        raise UpdaterCapabilityError(
            "updater capability is missing or invalid"
        ) from exc
    return payload


def _validate_envelope(
    payload: Mapping[str, Any],
    token: str,
    *,
    purpose: str,
    maximum_lifetime: int,
    now: float | None,
) -> None:
    if set(payload) != {
        "schema",
        "purpose",
        "tokenSha256",
        "issuedAt",
        "expiresAt",
        "binding",
    } or payload.get("schema") != 1 or payload.get("purpose") != purpose:
        raise UpdaterCapabilityError("updater capability payload is invalid")
    issued_at = payload.get("issuedAt")
    expires_at = payload.get("expiresAt")
    if (
        type(issued_at) is not int
        or type(expires_at) is not int
        or expires_at <= issued_at
        or expires_at - issued_at > maximum_lifetime
    ):
        raise UpdaterCapabilityError("updater capability lifetime is invalid")
    current_time = int(time.time() if now is None else now)
    if issued_at > current_time + 60 or expires_at <= current_time:
        raise UpdaterCapabilityError("updater capability has expired")
    token_digest = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
    stored_digest = payload.get("tokenSha256")
    if type(stored_digest) is not str or not token or not hmac.compare_digest(
        token_digest,
        stored_digest,
    ):
        raise UpdaterCapabilityError("updater capability token does not match")


def _one_time_payload(
    *,
    purpose: str,
    token: str,
    issued_at: int,
    expires_at: int,
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": 1,
        "purpose": purpose,
        "tokenSha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "issuedAt": issued_at,
        "expiresAt": expires_at,
        "binding": dict(binding),
    }


def _read_bounded(path: Path, approved_root: Path, maximum: int) -> bytes:
    stream = None
    primary_error: BaseException | None = None
    try:
        stream = open_regular_binary(path, approved_root=approved_root, mode="rb")
        payload = stream.read(maximum + 1)
        if len(payload) > maximum:
            raise UpdaterCapabilityError("updater capability is too large")
        return payload
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if stream is not None:
            run_typed_cleanup_actions(
                primary_error,
                ((stream.close, "updater capability read cleanup failed"),),
                error_factory=UpdaterCapabilityError,
                failure_message="updater capability read cleanup failed",
            )


def _file_binding(
    path: Path,
    approved_root: Path,
    *,
    max_bytes: int,
) -> dict[str, str]:
    return {
        "path": _path_text(path),
        "sha256": file_sha256(
            path,
            approved_root=approved_root,
            max_bytes=max_bytes,
        ),
    }


def _identity_payload(identity: ReleaseIdentity) -> dict[str, Any]:
    if not isinstance(identity, ReleaseIdentity):
        raise UpdaterCapabilityError("updater release identity is invalid")
    return {
        "version": identity.version,
        "revision": identity.revision,
        "tag": identity.tag,
    }


def _identity_from_payload(value: object) -> ReleaseIdentity:
    if not isinstance(value, Mapping) or set(value) != {"version", "revision", "tag"}:
        raise UpdaterCapabilityError("updater release identity is invalid")
    version = value.get("version")
    revision = value.get("revision")
    tag = value.get("tag")
    if type(version) is not str or type(tag) is not str:
        raise UpdaterCapabilityError("updater release identity is invalid")
    try:
        identity = ReleaseIdentity(version, revision)
    except (TypeError, ValueError) as exc:
        raise UpdaterCapabilityError("updater release identity is invalid") from exc
    if tag != identity.tag:
        raise UpdaterCapabilityError("updater release identity is invalid")
    return identity


def _validated_argv(value: object, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise UpdaterCapabilityError("updater restart argv is invalid")
    if required and not value:
        raise UpdaterCapabilityError("updater restart argv is invalid")
    if len(value) > MAX_RESTART_ARGUMENTS:
        raise UpdaterCapabilityError("updater restart argv is invalid")
    result: list[str] = []
    for item in value:
        if type(item) is not str or not item or len(item) > MAX_PATH_TEXT:
            raise UpdaterCapabilityError("updater restart argv is invalid")
        result.append(item)
    return tuple(result)


def _bounded_text(value: object, label: str) -> str:
    if type(value) is not str or not value or len(value) > MAX_PATH_TEXT:
        raise UpdaterCapabilityError(f"updater {label} is invalid")
    return value


def _path_text(path: Path) -> str:
    value = os.fspath(Path(os.path.abspath(os.fspath(path))))
    if not value or len(value) > MAX_PATH_TEXT:
        raise UpdaterCapabilityError("updater path is invalid")
    return value


def _encode_payload(payload: Mapping[str, Any]) -> bytes:
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_CAPABILITY_BYTES:
        raise UpdaterCapabilityError("updater capability is too large")
    return encoded


def _json_exact_equal(left: object, right: object) -> bool:
    try:
        return json.dumps(
            left,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ) == json.dumps(
            right,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return False


__all__ = [
    "LAUNCH_CAPABILITY_TOKEN_ENV",
    "LAUNCH_CAPABILITY_TTL_SECONDS",
    "LaunchBinding",
    "RESTART_HANDOFF_TTL_SECONDS",
    "RESTART_HANDOFF_TOKEN_ENV",
    "RestartHandoffBinding",
    "UpdaterCapabilityError",
    "consume_launch_capability",
    "consume_restart_handoff",
    "file_sha256",
    "issue_launch_capability",
    "issue_restart_handoff",
    "load_installed_release_identity",
]
