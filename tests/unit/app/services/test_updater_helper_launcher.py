from __future__ import annotations

from unittest.mock import Mock

import pytest

import app.services.updater_helper_launcher as launcher_module
from app.services.updater_launch_capability import LaunchBinding
from shared.release_identity import ReleaseIdentity


def test_prelaunch_failure_does_not_unlink_replaced_capability(
    tmp_path,
    monkeypatch,
):
    class HostileFailure(BaseException):
        pass

    primary = HostileFailure("environment snapshot failed")
    metadata_root = tmp_path / "metadata"
    staging_root = tmp_path / "staging"
    metadata_root.mkdir()
    staging_root.mkdir()
    capability_path = metadata_root / ".launch-capability.json"
    capability_path.write_bytes(b"one-time capability")
    binding = LaunchBinding(
        installer_path=staging_root / "installer.exe",
        manifest_path=metadata_root / "latest.json",
        signature_path=metadata_root / "latest.json.sig",
        log_path=staging_root / "install.log",
        install_root=tmp_path / "install",
        target_identity=ReleaseIdentity("3.7.0", 0),
        current_identity=ReleaseIdentity("3.6.21", 0),
        restart_argv=(),
        parent_pid=123,
    )
    record_pending = Mock()
    rollback_pending = Mock()
    popen = Mock()
    monkeypatch.setattr(
        launcher_module,
        "issue_launch_capability",
        lambda *_args, **_kwargs: (capability_path, "launch-token"),
    )
    def replace_capability_then_fail(**_kwargs):
        capability_path.write_bytes(b"later capability generation")
        raise primary

    monkeypatch.setattr(
        launcher_module,
        "_helper_process_kwargs",
        replace_capability_then_fail,
    )

    with pytest.raises(HostileFailure) as exc_info:
        launcher_module.launch_leased_updater_helper(
            tmp_path / "updater_helper.exe",
            binding,
            staging_root=staging_root,
            metadata_root=metadata_root,
            revision_authorization_token="",
            popen=popen,
            record_pending=record_pending,
            rollback_pending=rollback_pending,
            error_factory=RuntimeError,
        )

    assert exc_info.value is primary
    assert capability_path.read_bytes() == b"later capability generation"
    record_pending.assert_not_called()
    rollback_pending.assert_not_called()
    popen.assert_not_called()


def test_ambiguous_pending_commit_is_rolled_back_by_launch_token(
    tmp_path,
    monkeypatch,
):
    class HostileFailure(BaseException):
        pass

    primary = HostileFailure("pending state close failed after commit")
    previous_pending = object()
    metadata_root = tmp_path / "metadata"
    staging_root = tmp_path / "staging"
    metadata_root.mkdir()
    staging_root.mkdir()
    capability_path = metadata_root / ".launch-capability.json"
    capability_path.write_bytes(b"one-time capability")
    binding = LaunchBinding(
        installer_path=staging_root / "installer.exe",
        manifest_path=metadata_root / "latest.json",
        signature_path=metadata_root / "latest.json.sig",
        log_path=staging_root / "install.log",
        install_root=tmp_path / "install",
        target_identity=ReleaseIdentity("3.7.0", 0),
        current_identity=ReleaseIdentity("3.6.21", 0),
        restart_argv=(),
        parent_pid=123,
    )

    def commit_then_fail(*, previous_pending_out, **_kwargs):
        previous_pending_out.append(previous_pending)
        raise primary

    rollback_pending = Mock()
    popen = Mock()
    monkeypatch.setattr(
        launcher_module,
        "issue_launch_capability",
        lambda *_args, **_kwargs: (capability_path, "capability-token"),
    )
    monkeypatch.setattr(
        launcher_module.secrets,
        "token_urlsafe",
        lambda _length: "stable-launch-token",
    )

    with pytest.raises(HostileFailure) as exc_info:
        launcher_module.launch_leased_updater_helper(
            tmp_path / "updater_helper.exe",
            binding,
            staging_root=staging_root,
            metadata_root=metadata_root,
            revision_authorization_token="",
            popen=popen,
            record_pending=commit_then_fail,
            rollback_pending=rollback_pending,
            error_factory=RuntimeError,
        )

    assert exc_info.value is primary
    assert capability_path.read_bytes() == b"one-time capability"
    rollback_pending.assert_called_once_with(
        "stable-launch-token",
        restore_pending=previous_pending,
    )
    popen.assert_not_called()
