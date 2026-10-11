"""Immutable update workflow records used by the desktop main window."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.update_check_service import UpdateCheckResult


@dataclass(frozen=True)
class _UpdateCheckRequest:
    sequence: int
    local_version: str
    local_revision: int = 0


@dataclass(frozen=True)
class _UpdateCheckOutcome:
    sequence: int
    result: UpdateCheckResult | None = None
    error: str = ""


@dataclass(frozen=True)
class _PreparedUpdate:
    installer_path: str
    manifest_path: str
    signature_path: str
    version: str
    log_path: str
    release_revision: int = 0
    revision_authorization_path: str = ""
    revision_authorization_token: str = field(default="", repr=False)


__all__ = ["_PreparedUpdate", "_UpdateCheckOutcome", "_UpdateCheckRequest"]
