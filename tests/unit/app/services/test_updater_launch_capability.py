from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import app.services.updater_launch_capability as launch_capability
from app.services.updater_launch_capability import (
    LaunchBinding,
    RestartHandoffBinding,
    UpdaterCapabilityError,
    consume_launch_capability,
    consume_restart_handoff,
    issue_launch_capability,
    issue_restart_handoff,
    load_installed_release_identity,
)
from shared.release_identity import ReleaseIdentity


def _write_release_identity(root: Path, identity: ReleaseIdentity) -> None:
    (root / "release_identity.json").write_text(
        json.dumps(
            {
                "version": identity.version,
                "revision": identity.revision,
                "tag": identity.tag,
                "sourceCommit": "a" * 40,
            }
        ),
        encoding="utf-8",
    )


def _launch_fixture(tmp_path: Path) -> tuple[LaunchBinding, Path, Path]:
    staging = tmp_path / "staging"
    metadata = tmp_path / "metadata"
    install = tmp_path / "installed"
    staging.mkdir(parents=True)
    metadata.mkdir(parents=True)
    install.mkdir(parents=True)
    installer = staging / "update.exe"
    manifest = metadata / "latest.json"
    signature = metadata / "latest.json.sig"
    for path, payload in (
        (installer, b"trusted installer"),
        (manifest, b"signed manifest"),
        (signature, b"s" * 64),
    ):
        path.write_bytes(payload)
    current = ReleaseIdentity("3.6.21", 1)
    _write_release_identity(install, current)
    binding = LaunchBinding(
        installer_path=installer,
        manifest_path=manifest,
        signature_path=signature,
        log_path=staging / "install.log",
        install_root=install,
        target_identity=ReleaseIdentity("3.6.21", 2),
        current_identity=current,
        restart_argv=(str(install / "UniversalCrawlerPro.exe"), "--updated"),
        parent_pid=4242,
        restart_handoff_path=staging / ".ucrawl-restart-test.json",
    )
    return binding, staging, metadata


def test_launch_capability_is_bound_and_exactly_once(tmp_path: Path) -> None:
    binding, staging, metadata = _launch_fixture(tmp_path)
    capability_path, token = issue_launch_capability(
        binding,
        staging_root=staging,
        metadata_root=metadata,
        now=100,
    )

    consume_launch_capability(
        capability_path,
        token,
        expected=binding,
        staging_root=staging,
        metadata_root=metadata,
        now=101,
    )

    assert not capability_path.exists()
    with pytest.raises(UpdaterCapabilityError, match="missing or invalid"):
        consume_launch_capability(
            capability_path,
            token,
            expected=binding,
            staging_root=staging,
            metadata_root=metadata,
            now=101,
        )


def test_claim_cleanup_does_not_unlink_replaced_claimed_capability(
    tmp_path: Path,
    monkeypatch,
) -> None:
    binding, staging, metadata = _launch_fixture(tmp_path)
    capability_path, token = issue_launch_capability(
        binding,
        staging_root=staging,
        metadata_root=metadata,
        now=100,
    )
    real_read = launch_capability._read_bounded
    claimed_paths: list[Path] = []

    def read_then_replace(path: Path, approved_root: Path, maximum: int) -> bytes:
        payload = real_read(path, approved_root, maximum)
        if ".claimed-" in path.name:
            path.unlink()
            path.write_bytes(b"later claimed generation")
            claimed_paths.append(path)
        return payload

    monkeypatch.setattr(launch_capability, "_read_bounded", read_then_replace)

    consume_launch_capability(
        capability_path,
        token,
        expected=binding,
        staging_root=staging,
        metadata_root=metadata,
        now=101,
    )

    assert len(claimed_paths) == 1
    assert claimed_paths[0].read_bytes() == b"later claimed generation"


def test_claim_rejects_a_requested_successor_published_at_the_claimed_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    binding, staging, metadata = _launch_fixture(tmp_path)
    capability_path, token = issue_launch_capability(
        binding,
        staging_root=staging,
        metadata_root=metadata,
        now=100,
    )
    real_replace = launch_capability.replace_regular_file
    claimed_paths: list[Path] = []

    def replace_requested_then_claim(source, target, *, approved_root):
        source = Path(source)
        target = Path(target)
        source.rename(source.with_name(f"{source.name}.verified-original"))
        source.write_bytes(b"later capability generation")
        claimed_paths.append(target)
        real_replace(source, target, approved_root=approved_root)

    monkeypatch.setattr(
        launch_capability,
        "replace_regular_file",
        replace_requested_then_claim,
    )

    with pytest.raises(UpdaterCapabilityError, match="changed while it was claimed"):
        consume_launch_capability(
            capability_path,
            token,
            expected=binding,
            staging_root=staging,
            metadata_root=metadata,
            now=101,
        )

    assert len(claimed_paths) == 1
    assert claimed_paths[0].read_bytes() == b"later capability generation"


def test_launch_capability_does_not_unlink_replaced_ambiguous_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class HostileFailure(BaseException):
        pass

    binding, staging, metadata = _launch_fixture(tmp_path)
    primary = HostileFailure("atomic writer failed after replace")
    written_targets: list[Path] = []

    def commit_then_fail(path: Path, payload: bytes) -> None:
        path.write_bytes(payload)
        path.write_bytes(b"later capability generation")
        written_targets.append(path)
        raise primary

    monkeypatch.setattr(launch_capability, "atomic_write_bytes", commit_then_fail)

    with pytest.raises(HostileFailure) as exc_info:
        issue_launch_capability(
            binding,
            staging_root=staging,
            metadata_root=metadata,
        )

    assert exc_info.value is primary
    assert len(written_targets) == 1
    assert written_targets[0].read_bytes() == b"later capability generation"


def test_restart_handoff_does_not_unlink_replaced_ambiguous_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class HostileFailure(BaseException):
        pass

    binding, staging, _metadata = _launch_fixture(tmp_path)
    target = staging / ".ucrawl-restart-test.json"
    restart_binding = RestartHandoffBinding(
        restart_argv=binding.restart_argv,
        install_root=binding.install_root,
        target_identity=binding.target_identity,
        log_path=binding.log_path,
    )
    primary = HostileFailure("restart writer failed after replace")

    def commit_then_fail(path: Path, payload: bytes) -> None:
        path.write_bytes(payload)
        path.write_bytes(b"later restart generation")
        raise primary

    monkeypatch.setattr(launch_capability, "atomic_write_bytes", commit_then_fail)

    with pytest.raises(HostileFailure) as exc_info:
        issue_restart_handoff(
            target,
            restart_binding,
            staging_root=staging,
        )

    assert exc_info.value is primary
    assert target.read_bytes() == b"later restart generation"


def test_launch_capability_rejects_changed_artifact_and_binding(tmp_path: Path) -> None:
    binding, staging, metadata = _launch_fixture(tmp_path)
    capability_path, token = issue_launch_capability(
        binding,
        staging_root=staging,
        metadata_root=metadata,
        now=100,
    )
    binding.installer_path.write_bytes(b"evil replacement")

    with pytest.raises(UpdaterCapabilityError, match="does not match"):
        consume_launch_capability(
            capability_path,
            token,
            expected=binding,
            staging_root=staging,
            metadata_root=metadata,
            now=101,
        )

    second_binding, staging, metadata = _launch_fixture(tmp_path / "second")
    capability_path, token = issue_launch_capability(
        second_binding,
        staging_root=staging,
        metadata_root=metadata,
        now=100,
    )
    changed = LaunchBinding(
        **{
            **second_binding.__dict__,
            "parent_pid": second_binding.parent_pid + 1,
        }
    )
    with pytest.raises(UpdaterCapabilityError, match="does not match"):
        consume_launch_capability(
            capability_path,
            token,
            expected=changed,
            staging_root=staging,
            metadata_root=metadata,
            now=101,
        )


def test_launch_capability_hashing_is_bounded_by_artifact_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, staging, metadata = _launch_fixture(tmp_path)
    binding.signature_path.write_bytes(b"s" * 65)

    with pytest.raises(UpdaterCapabilityError, match="size limit"):
        issue_launch_capability(
            binding,
            staging_root=staging,
            metadata_root=metadata,
        )

    binding.signature_path.write_bytes(b"s" * 64)
    monkeypatch.setattr(launch_capability, "MAX_INSTALLER_BINDING_BYTES", 4)
    with pytest.raises(UpdaterCapabilityError, match="size limit"):
        issue_launch_capability(
            binding,
            staging_root=staging,
            metadata_root=metadata,
        )

def test_launch_capability_expires_at_exact_boundary(tmp_path: Path) -> None:
    binding, staging, metadata = _launch_fixture(tmp_path)
    capability_path, token = issue_launch_capability(
        binding,
        staging_root=staging,
        metadata_root=metadata,
        now=100,
    )

    with pytest.raises(UpdaterCapabilityError, match="expired"):
        consume_launch_capability(
            capability_path,
            token,
            expected=binding,
            staging_root=staging,
            metadata_root=metadata,
            now=700,
        )


def test_wrong_launch_token_does_not_consume_legitimate_capability(tmp_path: Path) -> None:
    binding, staging, metadata = _launch_fixture(tmp_path)
    capability_path, token = issue_launch_capability(
        binding,
        staging_root=staging,
        metadata_root=metadata,
        now=100,
    )

    with pytest.raises(UpdaterCapabilityError, match="token"):
        consume_launch_capability(
            capability_path,
            "wrong-token",
            expected=binding,
            staging_root=staging,
            metadata_root=metadata,
            now=101,
        )

    assert capability_path.is_file()
    consume_launch_capability(
        capability_path,
        token,
        expected=binding,
        staging_root=staging,
        metadata_root=metadata,
        now=101,
    )
    assert not capability_path.exists()


def test_launch_capability_has_exactly_one_concurrent_consumer(tmp_path: Path) -> None:
    binding, staging, metadata = _launch_fixture(tmp_path)
    capability_path, token = issue_launch_capability(
        binding,
        staging_root=staging,
        metadata_root=metadata,
        now=100,
    )
    barrier = threading.Barrier(2)

    def consume() -> str:
        barrier.wait(timeout=2)
        try:
            consume_launch_capability(
                capability_path,
                token,
                expected=binding,
                staging_root=staging,
                metadata_root=metadata,
                now=101,
            )
        except UpdaterCapabilityError:
            return "rejected"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(
            future.result(timeout=3)
            for future in (executor.submit(consume), executor.submit(consume))
        )

    assert outcomes == ["accepted", "rejected"]


def test_restart_handoff_is_token_bound_and_exactly_once(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    install = tmp_path / "installed"
    staging.mkdir()
    install.mkdir()
    binding = RestartHandoffBinding(
        restart_argv=(str(install / "UniversalCrawlerPro.exe"), "--updated"),
        install_root=install,
        target_identity=ReleaseIdentity("3.6.21", 2),
        log_path=staging / "install.log",
    )
    handoff_path = staging / ".ucrawl-restart-test.json"
    token = issue_restart_handoff(
        handoff_path,
        binding,
        staging_root=staging,
        now=100,
    )

    consumed = consume_restart_handoff(
        handoff_path,
        token,
        expected_install_root=install,
        staging_root=staging,
        now=101,
    )

    assert consumed == binding
    assert not handoff_path.exists()
    with pytest.raises(UpdaterCapabilityError, match="missing or invalid"):
        consume_restart_handoff(
            handoff_path,
            token,
            expected_install_root=install,
            staging_root=staging,
            now=101,
        )


def test_installed_release_identity_requires_safe_exact_marker(tmp_path: Path) -> None:
    install = tmp_path / "installed"
    install.mkdir()
    expected = ReleaseIdentity("3.6.21", 3)
    _write_release_identity(install, expected)

    assert load_installed_release_identity(install) == expected

    (install / "release_identity.json").write_text(
        '{"version":"3.6.21","revision":3,"tag":"v3.6.21-r2"}',
        encoding="utf-8",
    )
    with pytest.raises(UpdaterCapabilityError, match="identity marker"):
        load_installed_release_identity(install)
