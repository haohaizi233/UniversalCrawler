"""Persistent updater state value objects, independent from network discovery."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from app.services.updater_atomic_io import atomic_write_text
from app.services.updater_cleanup import run_typed_cleanup_actions
from app.services.updater_path_safety import open_regular_binary
from shared.release_identity import MAX_RELEASE_REVISION, ReleaseIdentity, SemVer

DEFAULT_INSTALL_ATTEMPT_LIMIT = 2
MAX_UPDATE_STATE_BYTES = 64 * 1024
MAX_STATE_PATH_LENGTH = 4096
MAX_STATE_ERROR_LENGTH = 4096
MAX_LAUNCH_TOKEN_LENGTH = 256
MAX_INSTALL_ATTEMPT_LIMIT = 10
MAX_RECORDED_INSTALL_ATTEMPTS = 100


@dataclass
class PendingInstall:
    version: str
    attempts: int = 0
    installer_path: str = ""
    log_path: str = ""
    release_revision: int = 0
    launch_token: str = field(default="", repr=False)

    @property
    def identity(self) -> ReleaseIdentity:
        return ReleaseIdentity(self.version, self.release_revision)


@dataclass
class LocalUpdateState:
    last_seen_version: str = ""
    last_seen_revision: int = 0
    skipped_version: str = ""
    skipped_revision: int = 0
    pending_install: PendingInstall | None = None
    last_install_error: str = ""
    install_attempt_limit: int = DEFAULT_INSTALL_ATTEMPT_LIMIT

    def record_startup_health(
        self,
        *,
        current_version: str,
        current_revision: int = 0,
        staging_dir: Path | None = None,
    ) -> None:
        del staging_dir
        pending = self.pending_install
        if pending is None:
            return
        current_identity = ReleaseIdentity(current_version, current_revision)
        if current_identity == pending.identity:
            self.pending_install = None
            self.last_install_error = ""
            return
        pending.attempts += 1
        self.last_install_error = f"installed version did not change to {pending.identity.tag}"
        if pending.attempts >= self.install_attempt_limit:
            self.pending_install = None

    @classmethod
    def load(cls, path: Path) -> "LocalUpdateState":
        try:
            raw = _read_bounded_state(Path(path))
            if raw is None:
                return cls()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return cls()
        if not isinstance(payload, Mapping):
            return cls()
        pending_payload = payload.get("pending_install")
        pending = None
        if isinstance(pending_payload, Mapping):
            pending_version = _state_semver(pending_payload.get("version"))
            if pending_version:
                pending = PendingInstall(
                    version=pending_version,
                    attempts=_state_int(
                        pending_payload.get("attempts"),
                        default=0,
                        maximum=MAX_RECORDED_INSTALL_ATTEMPTS,
                    ),
                    installer_path=_state_string(
                        pending_payload.get("installer_path"),
                        maximum=MAX_STATE_PATH_LENGTH,
                    ),
                    log_path=_state_string(
                        pending_payload.get("log_path"),
                        maximum=MAX_STATE_PATH_LENGTH,
                    ),
                    release_revision=_state_int(
                        pending_payload.get("release_revision"),
                        default=0,
                        maximum=MAX_RELEASE_REVISION,
                    ),
                    launch_token=_state_string(
                        pending_payload.get("launch_token"),
                        maximum=MAX_LAUNCH_TOKEN_LENGTH,
                    ),
                )
        return cls(
            last_seen_version=_state_semver(payload.get("last_seen_version")),
            last_seen_revision=_state_int(
                payload.get("last_seen_revision"),
                default=0,
                maximum=MAX_RELEASE_REVISION,
            ),
            skipped_version=_state_semver(payload.get("skipped_version")),
            skipped_revision=_state_int(
                payload.get("skipped_revision"),
                default=0,
                maximum=MAX_RELEASE_REVISION,
            ),
            pending_install=pending,
            last_install_error=_state_string(
                payload.get("last_install_error"),
                maximum=MAX_STATE_ERROR_LENGTH,
            ),
            install_attempt_limit=_state_int(
                payload.get("install_attempt_limit"),
                default=DEFAULT_INSTALL_ATTEMPT_LIMIT,
                minimum=1,
                maximum=MAX_INSTALL_ATTEMPT_LIMIT,
            ),
        )

    def save(self, path: Path) -> None:
        _validate_state_for_save(self)
        content = json.dumps(asdict(self), ensure_ascii=False, indent=2)
        if len(content.encode("utf-8")) > MAX_UPDATE_STATE_BYTES:
            raise ValueError("update state exceeds the maximum encoded size")
        atomic_write_text(
            Path(path),
            content,
        )


def _state_semver(value: Any) -> str:
    raw = _state_string(value, maximum=80).strip()
    if not raw:
        return ""
    try:
        return str(SemVer.parse(raw))
    except ValueError:
        return ""


def _state_int(
    value: Any,
    *,
    default: int,
    minimum: int = 0,
    maximum: int,
) -> int:
    if type(value) is not int:
        return default
    return value if minimum <= value <= maximum else default


def _state_string(value: Any, *, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum:
        return ""
    return value


def _validate_state_for_save(state: LocalUpdateState) -> None:
    _validate_optional_semver(state.last_seen_version, "last_seen_version")
    _validate_optional_semver(state.skipped_version, "skipped_version")
    _require_bounded_int(
        state.last_seen_revision,
        "last_seen_revision",
        minimum=0,
        maximum=MAX_RELEASE_REVISION,
    )
    _require_bounded_int(
        state.skipped_revision,
        "skipped_revision",
        minimum=0,
        maximum=MAX_RELEASE_REVISION,
    )
    _require_bounded_string(
        state.last_install_error,
        "last_install_error",
        maximum=MAX_STATE_ERROR_LENGTH,
    )
    _require_bounded_int(
        state.install_attempt_limit,
        "install_attempt_limit",
        minimum=1,
        maximum=MAX_INSTALL_ATTEMPT_LIMIT,
    )
    pending = state.pending_install
    if pending is None:
        return
    _validate_optional_semver(pending.version, "pending_install.version", required=True)
    _require_bounded_int(
        pending.attempts,
        "pending_install.attempts",
        minimum=0,
        maximum=MAX_RECORDED_INSTALL_ATTEMPTS,
    )
    _require_bounded_int(
        pending.release_revision,
        "pending_install.release_revision",
        minimum=0,
        maximum=MAX_RELEASE_REVISION,
    )
    _require_bounded_string(
        pending.installer_path,
        "pending_install.installer_path",
        maximum=MAX_STATE_PATH_LENGTH,
    )
    _require_bounded_string(
        pending.log_path,
        "pending_install.log_path",
        maximum=MAX_STATE_PATH_LENGTH,
    )
    _require_bounded_string(
        pending.launch_token,
        "pending_install.launch_token",
        maximum=MAX_LAUNCH_TOKEN_LENGTH,
    )


def _validate_optional_semver(value: Any, field: str, *, required: bool = False) -> None:
    if type(value) is not str or (required and not value):
        raise ValueError(f"{field} is invalid")
    if not value:
        return
    try:
        SemVer.parse(value)
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc


def _require_bounded_int(
    value: Any,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field} is outside the supported range")


def _require_bounded_string(value: Any, field: str, *, maximum: int) -> None:
    if type(value) is not str or len(value) > maximum:
        raise ValueError(f"{field} exceeds the supported length")


def _read_bounded_state(path: Path) -> bytes | None:
    handle = None
    primary_error: BaseException | None = None
    try:
        handle = open_regular_binary(path, approved_root=path.parent, mode="rb")
        if os.fstat(handle.fileno()).st_size > MAX_UPDATE_STATE_BYTES:
            return None
        raw = handle.read(MAX_UPDATE_STATE_BYTES + 1)
        return raw if len(raw) <= MAX_UPDATE_STATE_BYTES else None
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if handle is not None:
            run_typed_cleanup_actions(
                primary_error,
                ((handle.close, "update state handle cleanup failed"),),
                error_factory=OSError,
                failure_message="update state handle cleanup failed",
            )
