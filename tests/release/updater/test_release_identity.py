from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import shared.release_identity as release_identity_module

from shared.release_identity import (
    MAX_RELEASE_REVISION,
    ReleaseIdentity,
    SemVer,
    format_release_tag,
    load_runtime_release_identity,
    parse_release_tag,
)


def test_release_identity_orders_revision_after_semver() -> None:
    assert ReleaseIdentity("3.6.21", 2) > ReleaseIdentity("3.6.21", 1)
    assert ReleaseIdentity("3.7.0", 0) > ReleaseIdentity("3.6.21", 99)


@pytest.mark.parametrize("value", (-1, True, 1.5, "1"))
def test_release_identity_rejects_non_integer_revision(value: object) -> None:
    with pytest.raises(ValueError, match="revision"):
        ReleaseIdentity("3.6.21", value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("tag", "expected"),
    (
        ("v3.6.21", ReleaseIdentity("3.6.21", 0)),
        ("v3.6.21-r1", ReleaseIdentity("3.6.21", 1)),
        ("v3.7.0-rc.1", ReleaseIdentity("3.7.0-rc.1", 0)),
    ),
)
def test_release_tag_round_trip(tag: str, expected: ReleaseIdentity) -> None:
    identity = parse_release_tag(tag)

    assert identity == expected
    assert format_release_tag(identity.version, identity.revision) == tag


@pytest.mark.parametrize("tag", ("3.6.21", "v3.6.21-r0", "v3.6.21-r01"))
def test_release_tag_rejects_noncanonical_values(tag: str) -> None:
    with pytest.raises(ValueError, match="release tag"):
        parse_release_tag(tag)


@pytest.mark.parametrize(
    "identity",
    (
        ReleaseIdentity("1.2.3", 0),
        ReleaseIdentity("1.2.3", 2),
        ReleaseIdentity("1.2.3-rx", 0),
        ReleaseIdentity("1.2.3-r-1", 0),
        ReleaseIdentity("1.2.3-rx", 2),
    ),
)
def test_release_identity_tag_always_round_trips(identity: ReleaseIdentity) -> None:
    assert parse_release_tag(identity.tag) == identity


@pytest.mark.parametrize("version", ("1.2.3-r0", "1.2.3-r01", "1.2.3-r2"))
def test_release_identity_reserves_numeric_revision_suffix(version: str) -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        ReleaseIdentity(version)


@pytest.mark.parametrize("version", ("1.2.3-r2", "1.2.3+build.7"))
def test_format_release_tag_rejects_non_round_trippable_versions(version: str) -> None:
    with pytest.raises(ValueError):
        format_release_tag(version, 0)


@pytest.mark.parametrize("version", ("１.2.3", "١.2.3"))
def test_semver_rejects_non_ascii_core_digits(version: str) -> None:
    with pytest.raises(ValueError, match="semver"):
        SemVer.parse(version)


def test_release_revision_has_one_shared_exact_boundary() -> None:
    assert ReleaseIdentity("3.7.0", MAX_RELEASE_REVISION).revision == MAX_RELEASE_REVISION
    with pytest.raises(ValueError, match="revision"):
        ReleaseIdentity("3.7.0", MAX_RELEASE_REVISION + 1)


def test_runtime_identity_missing_file_defaults_to_initial_release(tmp_path) -> None:
    assert load_runtime_release_identity(tmp_path, fallback_version="3.6.21") == ReleaseIdentity(
        "3.6.21",
        0,
    )


def test_runtime_identity_reads_packaged_marker(tmp_path) -> None:
    (tmp_path / "release_identity.json").write_text(
        json.dumps(
            {
                "version": "3.6.21",
                "revision": 3,
                "tag": "v3.6.21-r3",
                "sourceCommit": "a" * 40,
            }
        ),
        encoding="utf-8",
    )

    identity = load_runtime_release_identity(tmp_path, fallback_version="0.0.0")

    assert identity == ReleaseIdentity("3.6.21", 3)


@pytest.mark.parametrize(
    "payload",
    (
        {"version": "3.6.21", "revision": -1, "tag": "v3.6.21"},
        {"version": "3.6.21", "revision": 2, "tag": "v3.6.21-r1"},
        {"version": "3.6.21", "revision": True, "tag": "v3.6.21-r1"},
    ),
)
def test_runtime_identity_rejects_invalid_marker_without_trusting_partial_data(
    tmp_path,
    payload,
) -> None:
    (tmp_path / "release_identity.json").write_text(json.dumps(payload), encoding="utf-8")

    assert load_runtime_release_identity(tmp_path, fallback_version="3.6.20") == ReleaseIdentity(
        "3.6.20",
        0,
    )


def test_runtime_identity_rejects_oversized_or_invalid_utf8_marker(tmp_path) -> None:
    marker = tmp_path / "release_identity.json"
    marker.write_bytes(b" " * (16 * 1024 + 1))
    assert load_runtime_release_identity(
        tmp_path,
        fallback_version="3.6.20",
    ) == ReleaseIdentity("3.6.20", 0)

    marker.write_bytes(b"\xff\xfe")
    assert load_runtime_release_identity(
        tmp_path,
        fallback_version="3.6.20",
    ) == ReleaseIdentity("3.6.20", 0)


def test_runtime_identity_rejects_duplicate_json_fields(tmp_path) -> None:
    (tmp_path / "release_identity.json").write_text(
        '{"version":"3.6.21","version":"9.9.9","revision":0,'
        '"tag":"v9.9.9"}',
        encoding="utf-8",
    )

    assert load_runtime_release_identity(
        tmp_path,
        fallback_version="3.6.20",
    ) == ReleaseIdentity("3.6.20", 0)


def test_runtime_identity_rejects_link_marker(tmp_path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps({"version": "9.9.9", "revision": 0, "tag": "v9.9.9"}),
        encoding="utf-8",
    )
    marker = tmp_path / "release_identity.json"
    try:
        marker.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    assert load_runtime_release_identity(
        tmp_path,
        fallback_version="3.6.20",
    ) == ReleaseIdentity("3.6.20", 0)


def test_runtime_identity_rejects_install_root_replaced_after_validation(
    tmp_path,
    monkeypatch,
) -> None:
    install_root = tmp_path / "install"
    install_root.mkdir()
    moved_root = tmp_path / "install-before-swap"
    outside = tmp_path / "outside"
    outside.mkdir()
    (install_root / "release_identity.json").write_text(
        json.dumps({"version": "3.6.21", "revision": 1, "tag": "v3.6.21-r1"}),
        encoding="utf-8",
    )
    (outside / "release_identity.json").write_text(
        json.dumps({"version": "9.9.9", "revision": 9, "tag": "v9.9.9-r9"}),
        encoding="utf-8",
    )
    probe = tmp_path / "directory-link-probe"
    try:
        probe.symlink_to(outside, target_is_directory=True)
        probe.unlink()
    except OSError as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")
    real_validate = release_identity_module._require_regular_directory_ancestry
    install_text = os.path.normcase(os.path.abspath(os.fspath(install_root)))
    swapped = False

    def validate_then_swap(path: Path) -> None:
        nonlocal swapped
        real_validate(path)
        candidate_text = os.path.normcase(os.path.abspath(os.fspath(path)))
        if not swapped and candidate_text == install_text:
            swapped = True
            install_root.rename(moved_root)
            install_root.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(
        release_identity_module,
        "_require_regular_directory_ancestry",
        validate_then_swap,
    )

    identity = load_runtime_release_identity(
        install_root,
        fallback_version="3.6.20",
    )

    assert identity == ReleaseIdentity("3.6.20", 0)
