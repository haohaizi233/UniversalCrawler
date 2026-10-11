from __future__ import annotations

import os

import pytest

import app.services.updater_installer_lease as lease_module


@pytest.mark.skipif(os.name != "nt", reason="verified file leases require Windows")
def test_successful_file_lease_ignores_descriptor_cleanup_failure(
    tmp_path,
    monkeypatch,
):
    class HostileCleanup(BaseException):
        pass

    target = tmp_path / "updater_helper.exe"
    target.write_bytes(b"helper")
    cleanup_errors: list[BaseException] = []
    real_close = os.close

    def close(descriptor: int) -> None:
        if descriptor == 771:
            raise HostileCleanup("file lease cleanup failed")
        real_close(descriptor)

    monkeypatch.setattr(
        lease_module,
        "_open_windows_read_lease",
        lambda *_args, **_kwargs: 771,
    )
    monkeypatch.setattr(lease_module.os, "close", close)

    with lease_module.verified_file_lease(
        target,
        approved_root=tmp_path,
        error_factory=RuntimeError,
        cleanup_error_callback=cleanup_errors.append,
    ) as leased:
        assert leased == target

    assert len(cleanup_errors) == 1
    assert isinstance(cleanup_errors[0], HostileCleanup)


@pytest.mark.skipif(os.name != "nt", reason="verified file leases require Windows")
def test_failed_file_lease_preserves_body_error_over_cleanup_failure(
    tmp_path,
    monkeypatch,
):
    class HostileBody(BaseException):
        pass

    class HostileCleanup(BaseException):
        pass

    target = tmp_path / "updater_helper.exe"
    target.write_bytes(b"helper")
    primary = HostileBody("process creation failed")
    real_close = os.close

    def close(descriptor: int) -> None:
        if descriptor == 772:
            raise HostileCleanup("file lease cleanup failed")
        real_close(descriptor)

    monkeypatch.setattr(
        lease_module,
        "_open_windows_read_lease",
        lambda *_args, **_kwargs: 772,
    )
    monkeypatch.setattr(lease_module.os, "close", close)

    with pytest.raises(HostileBody) as exc_info:
        with lease_module.verified_file_lease(
            target,
            approved_root=tmp_path,
            error_factory=RuntimeError,
        ):
            raise primary

    assert exc_info.value is primary
    if hasattr(BaseException, "add_note"):
        assert getattr(primary, "__notes__", []) == [
            "verified executable lease cleanup failed"
        ]
