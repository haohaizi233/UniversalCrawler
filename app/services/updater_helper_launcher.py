"""Transactional process creation for an already leased updater helper."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from pathlib import Path
from typing import Any, Callable

from app.services.updater_cleanup import run_cleanup_actions
from app.services.updater_launch_capability import (
    LAUNCH_CAPABILITY_TOKEN_ENV,
    RESTART_HANDOFF_TOKEN_ENV,
    LaunchBinding,
    UpdaterCapabilityError,
    issue_launch_capability,
)

REVISION_AUTH_TOKEN_ENV = "UCRAWL_REVISION_AUTH_TOKEN"


def launch_leased_updater_helper(
    helper_path: Path,
    binding: LaunchBinding,
    *,
    staging_root: Path,
    metadata_root: Path,
    revision_authorization_token: str,
    popen: Callable[..., Any],
    record_pending: Callable[..., object],
    rollback_pending: Callable[..., object],
    error_factory: Callable[[str], BaseException],
) -> Any:
    """Create the helper process and roll back all pre-launch state on failure."""

    try:
        capability_path, capability_token = issue_launch_capability(
            binding,
            staging_root=staging_root,
            metadata_root=metadata_root,
        )
    except (OSError, UpdaterCapabilityError) as exc:
        raise error_factory("could not create updater launch capability") from exc

    previous_pending: list[Any] = []
    launch_token = ""
    record_attempted = False
    try:
        argv = _helper_argv(helper_path, binding, capability_path)
        process_kwargs = _helper_process_kwargs(
            capability_token=capability_token,
            revision_authorization_path=binding.revision_authorization_path,
            revision_authorization_token=revision_authorization_token,
        )
        launch_token = secrets.token_urlsafe(24)
        record_attempted = True
        record_pending(
            version=binding.target_identity.version,
            release_revision=binding.target_identity.revision,
            installer_path=os.fspath(binding.installer_path),
            log_path=os.fspath(binding.log_path),
            launch_token=launch_token,
            previous_pending_out=previous_pending,
        )
        return popen(argv, **process_kwargs)
    except BaseException as exc:
        cleanup_actions = []
        if record_attempted:
            cleanup_actions.append(
                (
                    lambda: rollback_pending(
                        launch_token,
                        restore_pending=(
                            previous_pending[0] if previous_pending else None
                        ),
                    ),
                    "pending update state rollback failed",
                )
            )
        run_cleanup_actions(exc, cleanup_actions)
        raise


def _helper_argv(
    helper_path: Path,
    binding: LaunchBinding,
    capability_path: Path,
) -> list[str]:
    argv = [
        os.fspath(helper_path),
        "--installer",
        os.fspath(binding.installer_path),
        "--manifest",
        os.fspath(binding.manifest_path),
        "--signature",
        os.fspath(binding.signature_path),
        "--version",
        binding.target_identity.version,
        "--release-revision",
        str(binding.target_identity.revision),
        "--log-path",
        os.fspath(binding.log_path),
        "--install-dir",
        os.fspath(binding.install_root),
        "--restart-argv-json",
        json.dumps(list(binding.restart_argv)),
        "--wait-pid",
        str(binding.parent_pid),
        "--launch-capability",
        os.fspath(capability_path),
    ]
    if binding.restart_handoff_path is not None:
        argv.extend(
            [
                "--restart-handoff-output",
                os.fspath(binding.restart_handoff_path),
            ]
        )
    if binding.revision_authorization_path is not None:
        argv.extend(
            [
                "--revision-authorization",
                os.fspath(binding.revision_authorization_path),
            ]
        )
    return argv


def _helper_process_kwargs(
    *,
    capability_token: str,
    revision_authorization_path: Path | None,
    revision_authorization_token: str,
) -> dict[str, Any]:
    process_kwargs: dict[str, Any] = {"shell": False, "close_fds": True}
    if os.name == "nt":
        process_kwargs["creationflags"] = getattr(
            subprocess,
            "CREATE_NO_WINDOW",
            0,
        )
    environment = os.environ.copy()
    for inherited_token in (
        LAUNCH_CAPABILITY_TOKEN_ENV,
        REVISION_AUTH_TOKEN_ENV,
        RESTART_HANDOFF_TOKEN_ENV,
    ):
        environment.pop(inherited_token, None)
    environment[LAUNCH_CAPABILITY_TOKEN_ENV] = capability_token
    if revision_authorization_path is not None:
        environment[REVISION_AUTH_TOKEN_ENV] = revision_authorization_token
    process_kwargs["env"] = environment
    return process_kwargs


__all__ = ["REVISION_AUTH_TOKEN_ENV", "launch_leased_updater_helper"]
