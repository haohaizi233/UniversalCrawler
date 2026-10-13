from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

import app.services.secure_updater as secure_updater_module
import app.services.updater_path_safety as path_safety_module
import app.services.updater_release_cache as release_cache_module
import shared.filesystem_directory_capability as directory_capability_module
from app.services.update_check_service import compare_versions
from app.services.secure_updater import (
    AssetSelector,
    DEFAULT_MAX_DOWNLOAD_BYTES,
    DownloadError,
    Downloader,
    GitHubReleaseClient,
    InstallerRunner,
    LocalUpdateState,
    ManifestError,
    MetadataNotFoundError,
    PackageVerifier,
    PendingInstall,
    ReleaseIdentity,
    SemVer,
    UpdateAsset,
    UpdateManifest,
    UpdateManifestVerifier,
    VerificationError,
    VersionPolicy,
    compare_semver,
    default_installer_types,
    log_update_event,
    open_trusted_url,
    record_pending_install,
    rollback_pending_install,
    record_skipped_update,
    record_startup_update_health,
    validate_asset_url,
)


class _BytesResponse:
    def __init__(
        self,
        data: bytes,
        *,
        headers: dict | None = None,
        status: int = 200,
        read_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self._data = data
        self._sent = False
        self.headers = headers or {"Content-Length": str(len(data))}
        self.status = status
        self.code = status
        self.read_error = read_error
        self.close_error = close_error
        self.close_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def read(self, _size: int = -1) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        if self._sent:
            return b""
        self._sent = True
        return self._data

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def _github_release_payload(*, as_list: bool = False) -> bytes:
    release = {
        "html_url": "https://github.com/owner/repo/releases/tag/v3.8.0",
        "tag_name": "v3.8.0",
        "name": "Release 3.8.0",
        "assets": [
            {
                "name": "latest.json",
                "browser_download_url": (
                    "https://github.com/owner/repo/releases/download/v3.8.0/latest.json"
                ),
            },
            {
                "name": "latest.json.sig",
                "browser_download_url": (
                    "https://github.com/owner/repo/releases/download/v3.8.0/latest.json.sig"
                ),
            },
        ],
    }
    return json.dumps([release] if as_list else release).encode("utf-8")


_RELEASE_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <link rel="alternate" href="https://github.com/owner/repo/releases/tag/v3.8.0" />
    <title>Release 3.8.0</title>
  </entry>
</feed>"""


def _invoke_github_response_path(
    client: GitHubReleaseClient,
    path: str,
    opener,
):
    if path == "latest":
        return client.fetch_manifest_locations(opener=opener, manual=True)
    if path == "candidates":
        return client.fetch_manifest_location_candidates(
            opener=opener,
            manual=True,
            per_page=1,
        )
    return client._fetch_manifest_locations_from_atom(  # noqa: SLF001
        opener=opener,
        per_page=1,
    )


def _signed_manifest(
    tmp_path: Path,
    *,
    overrides: dict | None = None,
    asset_overrides: dict | None = None,
    key=None,
    manifest_name: str = "latest.json",
) -> tuple[Path, Path, str]:
    key = key or ECC.generate(curve="Ed25519")
    public_pem = key.public_key().export_key(format="PEM")
    published_at = datetime.now(timezone.utc).replace(microsecond=0)
    payload = {
        "schema": 1,
        "appId": "ucrawl.universalcrawlerpro",
        "channel": "stable",
        "version": "3.7.0",
        "tag": "v3.7.0",
        "publishedAt": published_at.isoformat().replace("+00:00", "Z"),
        "expiresAt": (published_at + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
        "minClientVersion": "3.0.0",
        "mandatory": False,
        "notes": "release notes",
        "assets": {
            "windows-x64": {
                "name": "UniversalCrawlerPro_Setup_3.7.0.exe",
                "url": "https://github.com/owner/repo/releases/download/v3.7.0/installer.exe",
                "sha256": "a" * 64,
                "size": 1024,
                "installerType": "inno",
                "os": "windows",
                "arch": "x64",
            }
        },
    }
    if overrides:
        payload.update(overrides)
    if asset_overrides:
        payload["assets"]["windows-x64"].update(asset_overrides)
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifest_path = tmp_path / manifest_name
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    signer = eddsa.new(key, "rfc8032")
    signature = signer.sign(manifest_path.read_bytes())
    sig_path = manifest_path.with_name(f"{manifest_path.name}.sig")
    sig_path.write_bytes(signature)
    return manifest_path, sig_path, public_pem


def _signed_raw_manifest(tmp_path: Path, payload: bytes) -> tuple[Path, Path, str]:
    key = ECC.generate(curve="Ed25519")
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifest_path = tmp_path / "latest.json"
    signature_path = tmp_path / "latest.json.sig"
    manifest_path.write_bytes(payload)
    signature_path.write_bytes(eddsa.new(key, "rfc8032").sign(payload))
    return (
        manifest_path,
        signature_path,
        key.public_key().export_key(format="PEM"),
    )


def test_semver_compares_numeric_segments_and_prerelease():
    assert compare_semver("1.10.0", "1.9.9") > 0
    assert compare_semver("1.0.0", "1.0.0-rc.1") > 0


@pytest.mark.parametrize(
    ("lower", "higher"),
    [
        ("1.0.0-alpha", "1.0.0-alpha.1"),
        ("1.0.0-alpha.1", "1.0.0-alpha.beta"),
        ("1.0.0-alpha.beta", "1.0.0-beta"),
        ("1.0.0-beta", "1.0.0-beta.2"),
        ("1.0.0-beta.2", "1.0.0-beta.11"),
        ("1.0.0-beta.11", "1.0.0-rc.1"),
        ("1.0.0-rc.1", "1.0.0"),
    ],
)
def test_semver_follows_standard_prerelease_precedence_table(lower, higher):
    assert compare_semver(lower, higher) == -1
    assert compare_semver(higher, lower) == 1


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("1.0.0+20130313144700", "1.0.0+exp.sha.5114f85"),
        ("1.0.0-beta.2+build.7", "1.0.0-beta.2+build.99"),
    ],
)
def test_semver_build_metadata_does_not_affect_precedence(left, right):
    assert compare_semver(left, right) == 0


def test_semver_parse_accepts_optional_v_and_preserves_identifiers():
    assert SemVer.parse("v2.10.3-alpha.7+build.9") == SemVer(
        major=2,
        minor=10,
        patch=3,
        prerelease=("alpha", "7"),
        build="build.9",
    )
    assert compare_semver("V2.10.3", "2.10.3") == 0


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "1",
        "1.2",
        "1.2.3.4",
        "1.2.x",
        "1.2.3-",
        "1.2.3+",
    ],
)
def test_semver_parse_rejects_malformed_values(value):
    with pytest.raises(ValueError, match="invalid semver"):
        SemVer.parse(value)


@pytest.mark.parametrize(
    ("local", "latest", "expected"),
    [
        ("3.10", "3.9", 1),
        ("v3.6", "3.6.0", 0),
        ("release-2.7", "v2.10", -1),
    ],
)
def test_compare_versions_uses_numeric_fallback_for_non_semver_versions(
    local, latest, expected
):
    assert compare_versions(local, latest) == expected


def test_version_policy_rejects_stable_prerelease_and_uses_installed_version_as_floor():
    policy = VersionPolicy(channel="stable", state=LocalUpdateState(last_seen_version="3.8.0"))

    assert not policy.evaluate("3.7.0-rc.1", current_version="3.6.17").allowed
    result = policy.evaluate("3.7.0", current_version="3.6.17")
    assert result.allowed
    assert not policy.evaluate("3.6.16", current_version="3.6.17").allowed


def test_version_policy_respects_skipped_version_for_automatic_checks():
    policy = VersionPolicy(channel="stable", state=LocalUpdateState(skipped_version="3.7.0"))

    auto = policy.evaluate("3.7.0", current_version="3.6.17", manual=False)
    manual = policy.evaluate("3.7.0", current_version="3.6.17", manual=True)

    assert not auto.allowed
    assert manual.allowed


def test_manifest_signature_failure_is_rejected(tmp_path):
    manifest_path, sig_path, public_pem = _signed_manifest(tmp_path)
    sig_path.write_bytes(b"bad-signature")

    verifier = UpdateManifestVerifier(public_key_pem=public_pem)

    with pytest.raises(ManifestError):
        verifier.load_verified(manifest_path, sig_path)


def test_manifest_verifier_rejects_oversized_metadata_before_signature_work(tmp_path):
    manifest_path = tmp_path / "latest.json"
    signature_path = tmp_path / "latest.json.sig"
    manifest_path.write_bytes(b"x" * 2_000_001)
    signature_path.write_bytes(b"s" * 64)

    with pytest.raises(ManifestError, match="maximum size"):
        UpdateManifestVerifier(public_key_pem="unused").load_verified(
            manifest_path,
            signature_path,
        )

    signature_path.write_bytes(b"s" * 65)
    manifest_path.write_bytes(b"{}")
    with pytest.raises(ManifestError, match="signature.*size"):
        UpdateManifestVerifier(public_key_pem="unused").load_verified(
            manifest_path,
            signature_path,
        )


def test_manifest_verifier_rejects_symlinked_metadata_without_reading_external_file(tmp_path):
    source_dir = tmp_path / "source"
    manifest_path, signature_path, public_pem = _signed_manifest(source_dir)
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    manifest_link = metadata_dir / manifest_path.name
    signature_copy = metadata_dir / signature_path.name
    signature_copy.write_bytes(signature_path.read_bytes())
    try:
        manifest_link.symlink_to(manifest_path)
    except OSError as exc:
        pytest.fail(f"symlink creation unavailable: {exc}; enable Developer Mode or run elevated - symlink attack coverage must not be silently skipped")

    with pytest.raises(ManifestError, match="unsafe"):
        UpdateManifestVerifier(public_key_pem=public_pem).load_verified(
            manifest_link,
            signature_copy,
        )

    assert manifest_path.exists()
    assert manifest_link.is_symlink()


def test_manifest_rejects_app_id_mismatch_and_expiration(tmp_path):
    expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    manifest_path, sig_path, public_pem = _signed_manifest(
        tmp_path,
        overrides={"appId": "wrong.app", "expiresAt": expired},
    )
    verifier = UpdateManifestVerifier(public_key_pem=public_pem)

    with pytest.raises(ManifestError):
        verifier.load_verified(manifest_path, sig_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("publishedAt", "2026-07-30T12:00:00"),
        ("expiresAt", "2026-07-31T12:00:00"),
    ],
)
def test_manifest_rejects_datetime_without_timezone(tmp_path, field, value):
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    manifest_path, sig_path, public_pem = _signed_manifest(
        tmp_path,
        overrides={
            "publishedAt": "2026-07-30T12:00:00Z",
            "expiresAt": "2026-07-31T12:00:00Z",
            field: value,
        },
    )

    with pytest.raises(ManifestError, match="timezone"):
        UpdateManifestVerifier(public_key_pem=public_pem, now=lambda: now).load_verified(
            manifest_path,
            sig_path,
        )


@pytest.mark.parametrize(
    ("overrides", "asset_overrides"),
    [
        ({"schema": True}, None),
        ({"mandatory": "false"}, None),
        ({"trustedHosts": "github.com"}, None),
        (None, {"size": True}),
        (None, {"size": 1.9}),
        (None, {"size": "10"}),
    ],
)
def test_signed_manifest_rejects_ambiguous_json_native_types(
    tmp_path,
    overrides,
    asset_overrides,
):
    manifest_path, signature_path, public_pem = _signed_manifest(
        tmp_path,
        overrides=overrides,
        asset_overrides=asset_overrides,
    )

    with pytest.raises(ManifestError, match="must be"):
        UpdateManifestVerifier(public_key_pem=public_pem).load_verified(
            manifest_path,
            signature_path,
        )


@pytest.mark.parametrize("variant", ["invalid-utf8", "nan", "duplicate-key"])
def test_signed_manifest_requires_strict_json_even_with_a_valid_signature(
    tmp_path,
    variant,
):
    source_path, _signature_path, _public_pem = _signed_manifest(tmp_path / "source")
    payload = source_path.read_bytes()
    if variant == "invalid-utf8":
        payload += b"\xff"
    elif variant == "nan":
        payload = payload.replace(b'"schema": 1', b'"schema": NaN', 1)
    else:
        payload = payload.replace(
            b'"mandatory": false',
            b'"mandatory": false, "mandatory": true',
            1,
        )
    manifest_path, signature_path, public_pem = _signed_raw_manifest(
        tmp_path / variant,
        payload,
    )

    with pytest.raises(ManifestError, match="valid strict JSON"):
        UpdateManifestVerifier(public_key_pem=public_pem).load_verified(
            manifest_path,
            signature_path,
        )


def test_strict_manifest_json_normalizes_excessive_nesting():
    from app.services.updater_manifest_json import (
        StrictManifestJsonError,
        load_strict_manifest_json,
    )

    depth = sys.getrecursionlimit() * 4
    payload = b'{"nested":' + (b"[" * depth) + b"0" + (b"]" * depth) + b"}"

    with pytest.raises(StrictManifestJsonError, match="valid strict JSON"):
        load_strict_manifest_json(payload)


def test_manifest_rejects_invalid_asset_entries_instead_of_dropping_them(tmp_path):
    manifest_path, signature_path, public_pem = _signed_manifest(
        tmp_path,
        overrides={"assets": {"windows-x64": "not-an-object"}},
    )

    with pytest.raises(ManifestError, match="asset entry must be an object"):
        UpdateManifestVerifier(public_key_pem=public_pem).load_verified(
            manifest_path,
            signature_path,
        )


def test_manifest_from_dict_rejects_non_string_asset_keys_and_missing_fields(tmp_path):
    manifest_path, _signature_path, _public_pem = _signed_manifest(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    asset = payload["assets"].pop("windows-x64")
    payload["assets"][1] = asset

    with pytest.raises(ManifestError, match="asset keys must be strings"):
        UpdateManifest.from_dict(payload)

    payload["assets"] = {"windows-x64": asset}
    del payload["mandatory"]
    with pytest.raises(ManifestError, match="mandatory is required"):
        UpdateManifest.from_dict(payload)


@pytest.mark.parametrize(
    "published_at",
    ["2026-07-31T12:00:00Z", "2026-08-01T12:00:00Z"],
)
def test_manifest_requires_publication_before_expiration(tmp_path, published_at):
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    manifest_path, sig_path, public_pem = _signed_manifest(
        tmp_path,
        overrides={
            "publishedAt": published_at,
            "expiresAt": "2026-07-31T12:00:00Z",
        },
    )

    with pytest.raises(ManifestError, match="before.*expiresAt"):
        UpdateManifestVerifier(public_key_pem=public_pem, now=lambda: now).load_verified(
            manifest_path,
            sig_path,
        )


@pytest.mark.parametrize(
    ("offset", "accepted"),
    [
        (timedelta(minutes=5), True),
        (timedelta(minutes=5, microseconds=1), False),
    ],
)
def test_manifest_publication_future_skew_has_a_strict_boundary(tmp_path, offset, accepted):
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    published_at = (now + offset).isoformat().replace("+00:00", "Z")
    manifest_path, sig_path, public_pem = _signed_manifest(
        tmp_path,
        overrides={
            "publishedAt": published_at,
            "expiresAt": "2026-07-31T12:00:00Z",
        },
    )
    verifier = UpdateManifestVerifier(public_key_pem=public_pem, now=lambda: now)

    if accepted:
        assert verifier.load_verified(manifest_path, sig_path).published_at == published_at
    else:
        with pytest.raises(ManifestError, match="future"):
            verifier.load_verified(manifest_path, sig_path)


@pytest.mark.parametrize(
    ("validity", "accepted"),
    [
        (timedelta(days=30), True),
        (timedelta(days=30, microseconds=1), False),
    ],
)
def test_manifest_validity_window_has_a_strict_maximum(tmp_path, validity, accepted):
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    manifest_path, sig_path, public_pem = _signed_manifest(
        tmp_path,
        overrides={
            "publishedAt": now.isoformat().replace("+00:00", "Z"),
            "expiresAt": (now + validity).isoformat().replace("+00:00", "Z"),
        },
    )
    verifier = UpdateManifestVerifier(public_key_pem=public_pem, now=lambda: now)

    if accepted:
        verifier.load_verified(manifest_path, sig_path)
    else:
        with pytest.raises(ManifestError, match="validity window"):
            verifier.load_verified(manifest_path, sig_path)


def test_release_identity_bounds_web_round_trip_fields():
    ReleaseIdentity("1.2.3-" + "a" * 32, 1)

    with pytest.raises(ValueError, match="invalid semver"):
        ReleaseIdentity("1.2.3-" + "a" * 100, 1)
    with pytest.raises(ValueError, match="revision"):
        ReleaseIdentity("1.2.3", int("9" * 80))


def test_manifest_accepts_revision_when_tag_matches(tmp_path):
    manifest_path, sig_path, public_pem = _signed_manifest(
        tmp_path,
        overrides={"releaseRevision": 2, "tag": "v3.7.0-r2", "sourceCommit": "a" * 40},
    )

    manifest = UpdateManifestVerifier(public_key_pem=public_pem).load_verified(
        manifest_path,
        sig_path,
    )

    assert manifest.release_revision == 2
    assert manifest.identity.tag == "v3.7.0-r2"
    assert manifest.source_commit == "a" * 40


@pytest.mark.parametrize("revision", (-1, True, "2", 1.5, 2_147_483_648))
def test_manifest_rejects_invalid_release_revision(tmp_path, revision):
    manifest_path, sig_path, public_pem = _signed_manifest(
        tmp_path,
        overrides={"releaseRevision": revision, "tag": "v3.7.0-r2"},
    )

    with pytest.raises(ManifestError, match="revision"):
        UpdateManifestVerifier(public_key_pem=public_pem).load_verified(manifest_path, sig_path)


def test_manifest_and_state_share_the_exact_release_revision_boundary(tmp_path):
    from shared.release_identity import MAX_RELEASE_REVISION

    manifest_path, sig_path, public_pem = _signed_manifest(
        tmp_path,
        overrides={
            "releaseRevision": MAX_RELEASE_REVISION,
            "tag": f"v3.7.0-r{MAX_RELEASE_REVISION}",
        },
    )
    manifest = UpdateManifestVerifier(public_key_pem=public_pem).load_verified(
        manifest_path,
        sig_path,
    )
    state = LocalUpdateState(
        pending_install=PendingInstall(
            version=manifest.version,
            release_revision=manifest.release_revision,
        )
    )
    state_path = tmp_path / "state.json"

    state.save(state_path)

    assert LocalUpdateState.load(state_path).pending_install == state.pending_install


def test_manifest_rejects_tag_that_does_not_match_revision(tmp_path):
    manifest_path, sig_path, public_pem = _signed_manifest(
        tmp_path,
        overrides={"releaseRevision": 2, "tag": "v3.7.0-r1"},
    )

    with pytest.raises(ManifestError, match="tag"):
        UpdateManifestVerifier(public_key_pem=public_pem).load_verified(manifest_path, sig_path)


def test_legacy_manifest_without_revision_is_initial_release(tmp_path):
    manifest_path, sig_path, public_pem = _signed_manifest(tmp_path)

    manifest = UpdateManifestVerifier(public_key_pem=public_pem).load_verified(
        manifest_path,
        sig_path,
    )

    assert manifest.release_revision == 0


def test_asset_selector_requires_exact_platform_and_safe_https_url():
    manifest = UpdateManifest(
        schema=1,
        app_id="ucrawl.universalcrawlerpro",
        channel="stable",
        version="3.7.0",
        tag="v3.7.0",
        published_at="2026-07-09T00:00:00Z",
        expires_at="2026-07-10T00:00:00Z",
        min_client_version="3.0.0",
        mandatory=False,
        notes="",
        assets={
            "linux-x64": UpdateAsset(
                name="bad",
                url="file:///tmp/bad.AppImage",
                sha256="a" * 64,
                size=1,
                installer_type="AppImage",
                os="linux",
                arch="x64",
            )
        },
    )

    with pytest.raises(ManifestError):
        AssetSelector(os_name="windows", arch="x64").select(manifest)


def test_asset_selector_uses_explicit_installer_priority_not_json_order():
    assets = {
        "windows-msi": UpdateAsset(
            name="setup.msi",
            url="https://github.com/owner/repo/releases/download/v/setup.msi",
            sha256="a" * 64,
            size=1,
            installer_type="msi",
            os="windows",
            arch="x64",
        ),
        "windows-inno": UpdateAsset(
            name="setup.exe",
            url="https://github.com/owner/repo/releases/download/v/setup.exe",
            sha256="b" * 64,
            size=1,
            installer_type="inno",
            os="windows",
            arch="x64",
        ),
    }

    def select(values):
        manifest = UpdateManifest(
            schema=1,
            app_id="ucrawl.universalcrawlerpro",
            channel="stable",
            version="3.7.0",
            tag="v3.7.0",
            published_at="2026-07-09T00:00:00Z",
            expires_at="2026-07-10T00:00:00Z",
            min_client_version="3.0.0",
            mandatory=False,
            notes="",
            assets=dict(values),
        )
        return AssetSelector(os_name="windows", arch="x64").select(manifest).name

    assert select(assets.items()) == "setup.exe"
    assert select(reversed(tuple(assets.items()))) == "setup.exe"


def test_asset_selector_rejects_duplicate_same_priority_matches():
    duplicate = UpdateAsset(
        name="setup-a.exe",
        url="https://github.com/owner/repo/releases/download/v/setup-a.exe",
        sha256="a" * 64,
        size=1,
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    manifest = UpdateManifest(
        schema=1,
        app_id="ucrawl.universalcrawlerpro",
        channel="stable",
        version="3.7.0",
        tag="v3.7.0",
        published_at="2026-07-09T00:00:00Z",
        expires_at="2026-07-10T00:00:00Z",
        min_client_version="3.0.0",
        mandatory=False,
        notes="",
        assets={"first": duplicate, "second": replace(duplicate, name="setup-b.exe")},
    )

    with pytest.raises(ManifestError, match="multiple update assets"):
        AssetSelector(os_name="windows", arch="x64").select(manifest)


@pytest.mark.parametrize(
    ("os_name", "expected"),
    [
        ("windows", ("inno",)),
        ("macos", ()),
        ("linux", ()),
    ],
)
def test_default_installer_types_match_helper_capabilities(os_name, expected):
    assert default_installer_types(os_name) == expected
    for installer_type in expected:
        asset = UpdateAsset(
            name="installer.bin",
            url="https://github.com/owner/repo/releases/download/v/installer.bin",
            sha256="a" * 64,
            size=1,
            installer_type=installer_type,
            os=os_name,
            arch="x64",
        )
        InstallerRunner._install_argv(
            Path("installer.bin"),
            asset,
            Path("install.log"),
        )


@pytest.mark.parametrize(
    ("os_name", "installer_type", "asset_name"),
    [
        ("windows", "msi", "installer.msi"),
        ("windows", "nsis", "installer.exe"),
        ("macos", "pkg", "installer.pkg"),
    ],
)
def test_default_selector_rejects_installer_types_the_detached_helper_cannot_launch(
    os_name,
    installer_type,
    asset_name,
):
    asset = UpdateAsset(
        name=asset_name,
        url=f"https://github.com/owner/repo/releases/download/v/{asset_name}",
        sha256="a" * 64,
        size=1,
        installer_type=installer_type,
        os=os_name,
        arch="x64",
    )
    manifest = UpdateManifest(
        schema=1,
        app_id="ucrawl.universalcrawlerpro",
        channel="stable",
        version="3.7.0",
        tag="v3.7.0",
        published_at="2026-07-09T00:00:00Z",
        expires_at="2026-07-10T00:00:00Z",
        min_client_version="3.0.0",
        mandatory=False,
        notes="",
        assets={installer_type: asset},
    )

    with pytest.raises(ManifestError, match="no update asset"):
        AssetSelector(os_name=os_name, arch="x64").select(manifest)


def test_downloader_rejects_size_limit_and_hash_mismatch(tmp_path):
    data = b"not the expected payload"
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256="0" * 64,
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )

    downloader = Downloader(cache_dir=tmp_path, max_size_bytes=8)
    with pytest.raises(DownloadError):
        downloader.download(asset, opener=lambda _request, timeout: _BytesResponse(data))

    downloader = Downloader(cache_dir=tmp_path, max_size_bytes=1024)
    with pytest.raises(DownloadError):
        downloader.download(asset, opener=lambda _request, timeout: _BytesResponse(data))

    assert not (tmp_path / "installer.exe").exists()


def test_default_download_limit_accepts_large_production_installer_metadata():
    assert DEFAULT_MAX_DOWNLOAD_BYTES == 2 * 1024 * 1024 * 1024

    downloader = Downloader(cache_dir=Path("unused-update-cache"))
    assert downloader.max_size_bytes == DEFAULT_MAX_DOWNLOAD_BYTES
    assert downloader.max_size_bytes > 1_035_581_576


def test_downloader_honors_explicit_trusted_hosts(tmp_path):
    data = b"installer"
    digest = hashlib.sha256(data).hexdigest()
    asset = UpdateAsset(
        name="installer.exe",
        url="https://updates.example.com/downloads/installer.exe",
        sha256=digest,
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )

    with pytest.raises(ManifestError):
        Downloader(cache_dir=tmp_path).download(asset, opener=lambda _request, timeout: _BytesResponse(data))

    target = Downloader(cache_dir=tmp_path, allowed_hosts={"updates.example.com"}).download(
        asset,
        opener=lambda _request, timeout: _BytesResponse(data),
    )

    assert target.read_bytes() == data


def test_downloader_retries_transient_network_errors(tmp_path):
    data = b"installer"
    digest = hashlib.sha256(data).hexdigest()
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=digest,
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    calls = 0

    def opener(_request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise URLError("temporary outage")
        return _BytesResponse(data)

    target = Downloader(cache_dir=tmp_path, retries=1).download(asset, opener=opener)

    assert calls == 2
    assert target.read_bytes() == data


def test_downloader_rejects_a_partial_successor_published_as_the_final_target(
    tmp_path,
    monkeypatch,
):
    import app.services.updater_downloader as downloader_module

    verified = b"verified installer"
    successor = b"untrusted payload!"
    assert len(successor) == len(verified)
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(verified).hexdigest(),
        size=len(verified),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    original_partial: Path | None = None
    real_replace = downloader_module.replace_regular_file

    def replace_partial_then_publish(source, target, *, approved_root):
        nonlocal original_partial
        source = Path(source)
        original_partial = source.with_name(f"{source.name}.original")
        source.rename(original_partial)
        source.write_bytes(successor)
        real_replace(source, target, approved_root=approved_root)

    monkeypatch.setattr(
        downloader_module,
        "replace_regular_file",
        replace_partial_then_publish,
    )

    with pytest.raises(DownloadError, match="promoted|final target"):
        Downloader(cache_dir=tmp_path).download(
            asset,
            opener=lambda _request, timeout: _BytesResponse(verified),
        )

    assert original_partial is not None
    assert original_partial.read_bytes() == verified
    assert (tmp_path / asset.name).read_bytes() == successor


def test_default_downloader_retries_only_typed_transport_network_error(tmp_path, monkeypatch):
    from shared.network.pinned_transport import PinnedTransportNetworkError

    data = b"installer"
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    calls = 0

    class FakePinnedTransport:
        def __init__(self, **_kwargs):
            return None

        def request_to_sink(self, _method, url, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PinnedTransportNetworkError("connection reset")
            kwargs["response_validator"](200, {"Content-Length": str(len(data))})
            kwargs["sink"](data)
            return SimpleNamespace(status_code=200, url=url, headers={})

    monkeypatch.setattr("app.services.secure_updater.PinnedTransport", FakePinnedTransport)

    target = Downloader(cache_dir=tmp_path, retries=1).download(asset)

    assert calls == 2
    assert target.read_bytes() == data


def test_downloader_does_not_retry_short_disk_write(tmp_path, monkeypatch):
    data = b"installer"
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    from app.services import updater_downloader

    real_open = updater_downloader.open_regular_binary
    opener_calls = 0

    class ShortWriter:
        def __init__(self, stream):
            self._stream = stream

        def write(self, chunk):
            self._stream.write(chunk[:-1])
            return len(chunk) - 1

        def __getattr__(self, name):
            return getattr(self._stream, name)

    def patched_open(path, *, approved_root, mode):
        stream = real_open(path, approved_root=approved_root, mode=mode)
        if path.name.endswith(".partial") and "w" in mode:
            return ShortWriter(stream)
        return stream

    def opener(_request, *, timeout):
        nonlocal opener_calls
        opener_calls += 1
        return _BytesResponse(data)

    monkeypatch.setattr(updater_downloader, "open_regular_binary", patched_open)

    with pytest.raises(DownloadError, match="incomplete"):
        Downloader(cache_dir=tmp_path, retries=3).download(asset, opener=opener)

    assert opener_calls == 1
    assert (tmp_path / "installer.exe.partial").read_bytes() == data[:-1]


def test_downloader_normalizes_successful_handle_close_failure(tmp_path, monkeypatch):
    data = b"installer"
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    from app.services import updater_downloader

    real_open = updater_downloader.open_regular_binary
    opener_calls = 0

    class CloseFailWriter:
        def __init__(self, stream):
            self._stream = stream

        def close(self):
            self._stream.close()
            raise OSError("handle close failed")

        def __getattr__(self, name):
            return getattr(self._stream, name)

    def patched_open(path, *, approved_root, mode):
        stream = real_open(path, approved_root=approved_root, mode=mode)
        if path.name.endswith(".partial") and "w" in mode:
            return CloseFailWriter(stream)
        return stream

    def opener(_request, *, timeout):
        nonlocal opener_calls
        del timeout
        opener_calls += 1
        return _BytesResponse(data)

    monkeypatch.setattr(updater_downloader, "open_regular_binary", patched_open)

    with pytest.raises(DownloadError, match="handle cleanup failed") as exc_info:
        Downloader(cache_dir=tmp_path, retries=3).download(asset, opener=opener)

    assert isinstance(exc_info.value.__cause__, OSError)
    assert exc_info.value.retryable is False
    assert opener_calls == 1
    assert (tmp_path / "installer.exe.partial").read_bytes() == data


def test_downloader_retries_truncated_body_as_validated_range(tmp_path):
    data = b"0123456789"
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    ranges: list[str | None] = []

    def opener(request, *, timeout):
        del timeout
        requested_range = request.get_header("Range")
        ranges.append(requested_range)
        if requested_range is None:
            return _BytesResponse(
                data[:4],
                headers={"Content-Length": str(len(data))},
            )
        return _BytesResponse(
            data[4:],
            status=206,
            headers={
                "Content-Length": str(len(data) - 4),
                "Content-Range": f"bytes 4-{len(data) - 1}/{len(data)}",
            },
        )

    target = Downloader(cache_dir=tmp_path, retries=1).download(asset, opener=opener)

    assert ranges == [None, "bytes=4-"]
    assert target.read_bytes() == data


def test_concurrent_downloaders_serialize_the_same_target(tmp_path):
    data = b"concurrent installer"
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    first_started = threading.Event()
    release_first = threading.Event()
    calls = 0
    calls_guard = threading.Lock()

    def opener(_request, *, timeout):
        del timeout
        nonlocal calls
        with calls_guard:
            calls += 1
            call_number = calls
        if call_number == 1:
            first_started.set()
            assert release_first.wait(2)
        return _BytesResponse(data)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(Downloader(cache_dir=tmp_path).download, asset, opener=opener)
        assert first_started.wait(2)
        second = executor.submit(Downloader(cache_dir=tmp_path).download, asset, opener=opener)
        release_first.set()
        paths = (first.result(timeout=3), second.result(timeout=3))

    assert paths[0] == paths[1]
    assert paths[0].read_bytes() == data
    assert calls == 1
    assert not (tmp_path / "installer.exe.partial").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows path aliases are case-insensitive")
def test_download_lock_serializes_windows_case_aliases(tmp_path):
    from app.services.updater_download_locks import target_download_lock

    target = tmp_path / "Cache" / "Installer.exe"
    target.parent.mkdir()
    alias = Path(os.fspath(target).swapcase())
    first_started = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first_lock():
        with target_download_lock(
            target,
            cancel_event=None,
            timeout_seconds=2,
            error_factory=DownloadError,
        ):
            first_started.set()
            assert release_first.wait(2)

    def second_lock():
        with target_download_lock(
            alias,
            cancel_event=None,
            timeout_seconds=2,
            error_factory=DownloadError,
        ):
            second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_lock)
        assert first_started.wait(2)
        second = executor.submit(second_lock)
        assert not second_entered.wait(0.15)
        release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert second_entered.is_set()


def test_download_lock_serializes_competing_processes(tmp_path):
    target = tmp_path / "installer.exe"
    first_ready = tmp_path / "first-ready"
    first_started = tmp_path / "first-started"
    second_ready = tmp_path / "second-ready"
    second_started = tmp_path / "second-started"
    release_first = tmp_path / "release-first"
    script = r"""
import sys
import time
from pathlib import Path
from app.services.secure_updater import DownloadError
from app.services.updater_download_locks import target_download_lock

target, started, ready, gate = map(Path, sys.argv[1:])
started.write_text("started", encoding="utf-8")
with target_download_lock(
    target,
    cancel_event=None,
    timeout_seconds=5,
    error_factory=DownloadError,
):
    ready.write_text("entered", encoding="utf-8")
    if gate.name != "no-gate":
        while not gate.exists():
            time.sleep(0.01)
"""
    first = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(target),
            str(first_started),
            str(first_ready),
            str(release_first),
        ],
        cwd=Path.cwd(),
    )
    try:
        deadline = time.monotonic() + 3
        while not first_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert first_ready.exists()
        second = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(target),
                str(second_started),
                str(second_ready),
                str(tmp_path / "no-gate"),
            ],
            cwd=Path.cwd(),
        )
        try:
            deadline = time.monotonic() + 3
            while not second_started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert second_started.exists()
            time.sleep(0.1)
            assert not second_ready.exists()
            release_first.write_text("release", encoding="utf-8")
            assert first.wait(timeout=3) == 0
            assert second.wait(timeout=3) == 0
            assert second_ready.read_text(encoding="utf-8") == "entered"
        finally:
            if second.poll() is None:
                second.kill()
                second.wait(timeout=3)
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=3)


def test_download_lock_rejects_symlinked_lock_root_without_touching_external_directory(tmp_path):
    from app.services.updater_download_locks import target_download_lock

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    external = tmp_path / "external-locks"
    external.mkdir()
    try:
        (cache_dir / ".updater-locks").symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.fail(f"directory symlink creation unavailable: {exc}; enable Developer Mode or run elevated - symlink attack coverage must not be silently skipped")

    with pytest.raises(OSError, match="lock root"):
        with target_download_lock(
            cache_dir / "installer.exe",
            cancel_event=None,
            timeout_seconds=1,
            error_factory=DownloadError,
        ):
            pytest.fail("symlinked lock root must never be entered")

    assert list(external.iterdir()) == []


def test_file_lock_rejects_inode_replacement_between_lstat_and_open(tmp_path, monkeypatch):
    from app.services.updater_file_lock import exclusive_file_lock

    lock_path = tmp_path / "transaction.lock"
    replacement = tmp_path / "replacement.lock"
    lock_path.write_bytes(b"old")
    replacement.write_bytes(b"new")
    real_open_child = directory_capability_module.FilesystemDirectoryCapability.open_child
    replaced = False

    def replace_then_open(capability, name, flags, mode=0o600):
        nonlocal replaced
        if not replaced and capability.root == tmp_path and name == lock_path.name:
            replaced = True
            os.replace(replacement, lock_path)
        return real_open_child(capability, name, flags, mode)

    monkeypatch.setattr(
        directory_capability_module.FilesystemDirectoryCapability,
        "open_child",
        replace_then_open,
    )

    with pytest.raises(OSError, match="changed during open"):
        with exclusive_file_lock(lock_path, approved_root=tmp_path):
            pytest.fail("a replaced lock inode must never be acquired")


def test_file_lock_opens_safe_peer_generation_after_create_race(
    tmp_path,
    monkeypatch,
):
    from app.services.updater_file_lock import exclusive_file_lock

    lock_path = tmp_path / "transaction.lock"
    real_open_child = directory_capability_module.FilesystemDirectoryCapability.open_child
    injected = False

    def peer_creates_before_open(capability, name, flags, mode=0o600):
        nonlocal injected
        if not injected and name == lock_path.name and flags & os.O_EXCL:
            injected = True
            lock_path.write_bytes(b"\0")
            raise FileExistsError(lock_path)
        return real_open_child(capability, name, flags, mode)

    monkeypatch.setattr(
        directory_capability_module.FilesystemDirectoryCapability,
        "open_child",
        peer_creates_before_open,
    )

    with exclusive_file_lock(lock_path, approved_root=tmp_path):
        assert injected

    assert lock_path.read_bytes() == b"\0"


def test_file_lock_does_not_create_directories_through_symlinked_parent(tmp_path):
    from app.services.updater_file_lock import exclusive_file_lock

    external = tmp_path / "external"
    external.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.fail(f"directory symlink creation unavailable: {exc}; enable Developer Mode or run elevated - symlink attack coverage must not be silently skipped")
    lock_root = alias / "created-outside"

    with pytest.raises(OSError):
        with exclusive_file_lock(
            lock_root / "transaction.lock",
            approved_root=lock_root,
        ):
            pytest.fail("a lock root below a symlink must never be entered")

    assert not (external / "created-outside").exists()


def test_downloader_rejects_preexisting_target_symlink_without_touching_external_file(tmp_path):
    data = b"trusted installer"
    external = tmp_path / "external-installer.exe"
    external.write_bytes(data)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    target = cache_dir / "installer.exe"
    try:
        target.symlink_to(external)
    except OSError as exc:
        pytest.fail(f"symlink creation unavailable: {exc}; enable Developer Mode or run elevated - symlink attack coverage must not be silently skipped")
    asset = UpdateAsset(
        name=target.name,
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )

    with pytest.raises(DownloadError, match="staging preparation"):
        Downloader(cache_dir=cache_dir).download(
            asset,
            opener=lambda *_args, **_kwargs: pytest.fail("network must not be opened"),
        )

    assert external.read_bytes() == data
    assert target.is_symlink()


def test_downloader_does_not_create_staging_through_symlinked_ancestor(tmp_path):
    data = b"trusted installer"
    external = tmp_path / "external"
    external.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.fail(f"directory symlink creation unavailable: {exc}; enable Developer Mode or run elevated - symlink attack coverage must not be silently skipped")
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )

    with pytest.raises(DownloadError, match="staging directory is unsafe"):
        Downloader(cache_dir=alias / "staging").download(
            asset,
            opener=lambda *_args, **_kwargs: pytest.fail("network must not be opened"),
        )

    assert not (external / "staging").exists()


def test_downloader_rejects_preexisting_partial_symlink_without_touching_external_file(tmp_path):
    data = b"trusted installer"
    external = tmp_path / "external-partial.exe"
    external.write_bytes(data)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    partial = cache_dir / "installer.exe.partial"
    try:
        partial.symlink_to(external)
    except OSError as exc:
        pytest.fail(f"symlink creation unavailable: {exc}; enable Developer Mode or run elevated - symlink attack coverage must not be silently skipped")
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )

    with pytest.raises(DownloadError, match="staging preparation"):
        Downloader(cache_dir=cache_dir).download(
            asset,
            opener=lambda *_args, **_kwargs: pytest.fail("network must not be opened"),
        )

    assert external.read_bytes() == data
    assert partial.is_symlink()


def test_waiting_downloader_honors_cancellation_without_touching_active_partial(tmp_path):
    data = b"concurrent installer"
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    first_started = threading.Event()
    release_first = threading.Event()

    def opener(_request, *, timeout):
        del timeout
        first_started.set()
        assert release_first.wait(2)
        return _BytesResponse(data)

    cancelled = threading.Event()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(Downloader(cache_dir=tmp_path).download, asset, opener=opener)
        assert first_started.wait(2)
        second = executor.submit(
            Downloader(cache_dir=tmp_path).download,
            asset,
            opener=opener,
            cancel_event=cancelled,
        )
        threading.Event().wait(0.1)
        cancelled.set()
        with pytest.raises(DownloadError, match="cancelled"):
            second.result(timeout=2)
        release_first.set()
        target = first.result(timeout=3)

    assert target.read_bytes() == data
    assert not (tmp_path / "installer.exe.partial").exists()


def test_downloader_passes_timeout_as_keyword_for_urlopen_compatibility(tmp_path):
    data = b"installer"
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    seen_timeout: list[float] = []

    def opener(_request, *, timeout):
        seen_timeout.append(timeout)
        return _BytesResponse(data)

    target = Downloader(cache_dir=tmp_path, timeout_seconds=4.25).download(asset, opener=opener)

    assert seen_timeout == [4.25]
    assert target.read_bytes() == data


def test_default_downloader_streams_through_pinned_transport(tmp_path, monkeypatch):
    data = b"pinned installer payload"
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    captured: dict[str, object] = {}

    class FakePinnedTransport:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def request_to_sink(self, method, url, **kwargs):
            captured["request"] = {"method": method, "url": url, **kwargs}
            kwargs["response_validator"](200, {"Content-Length": str(len(data))})
            kwargs["sink"](data[:7])
            kwargs["sink"](data[7:])
            return SimpleNamespace(
                status_code=200,
                url=url,
                headers={"Content-Length": str(len(data))},
                bytes_written=len(data),
                total_response_bytes=len(data),
                redirect_chain=(url,),
            )

    monkeypatch.setattr("app.services.secure_updater.PinnedTransport", FakePinnedTransport)

    target = Downloader(cache_dir=tmp_path).download(asset)

    assert target.read_bytes() == data
    assert captured["request"]["accepted_statuses"] == {200}
    assert callable(captured["request"]["response_validator"])
    assert captured["request"]["max_total_bytes"] >= len(data)
    assert captured["init"]["max_response_bytes"] == captured["request"]["max_total_bytes"]


def test_open_trusted_url_uses_public_dns_pinned_transport(monkeypatch):
    from shared.network.pinned_transport import PinnedResponse

    captured: dict[str, object] = {}

    class FakePinnedTransport:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def request(self, method, url, **kwargs):
            captured["request"] = {"method": method, "url": url, **kwargs}
            return PinnedResponse(
                status_code=200,
                url="https://api.github.com:443/repos/owner/repo/releases/latest",
                headers={"Content-Type": "application/json"},
                body=b'{"ok":true}',
                redirect_chain=(url,),
            )

    monkeypatch.setattr("app.services.secure_updater.PinnedTransport", FakePinnedTransport)
    request = urllib.request.Request(
        "https://api.github.com/repos/owner/repo/releases/latest",
        headers={"Accept": "application/json"},
    )

    with open_trusted_url(
        request,
        timeout=3.0,
        allowed_hosts={"api.github.com"},
    ) as response:
        assert response.read() == b'{"ok":true}'
        assert response.geturl().startswith("https://api.github.com")

    assert captured["request"]["method"] == "GET"
    assert captured["init"]["timeout"] == 3.0
    assert captured["init"]["max_response_bytes"] > 0


def test_open_trusted_url_normalizes_ordinary_pinned_transport_failure(monkeypatch):
    primary = RuntimeError("curl session failed")

    class FailingPinnedTransport:
        def __init__(self, **_kwargs):
            return None

        def request(self, *_args, **_kwargs):
            raise primary

    monkeypatch.setattr(
        "app.services.secure_updater.PinnedTransport",
        FailingPinnedTransport,
    )
    request = urllib.request.Request(
        "https://api.github.com/repos/owner/repo/releases/latest"
    )

    with pytest.raises(URLError, match="trusted updater transport failed") as exc_info:
        open_trusted_url(
            request,
            timeout=3.0,
            allowed_hosts={"api.github.com"},
        )

    assert exc_info.value.__cause__ is primary


def test_open_trusted_url_preserves_transport_control_flow(monkeypatch):
    class TransportControl(BaseException):
        pass

    primary = TransportControl("stop")

    class FailingPinnedTransport:
        def __init__(self, **_kwargs):
            return None

        def request(self, *_args, **_kwargs):
            raise primary

    monkeypatch.setattr(
        "app.services.secure_updater.PinnedTransport",
        FailingPinnedTransport,
    )
    request = urllib.request.Request(
        "https://api.github.com/repos/owner/repo/releases/latest"
    )

    with pytest.raises(TransportControl) as exc_info:
        open_trusted_url(
            request,
            timeout=3.0,
            allowed_hosts={"api.github.com"},
        )

    assert exc_info.value is primary


def test_github_default_pinned_failure_is_download_error(tmp_path, monkeypatch):
    class FailingPinnedTransport:
        def __init__(self, **_kwargs):
            return None

        def request(self, *_args, **_kwargs):
            raise RuntimeError("curl request failed")

    monkeypatch.setattr(
        "app.services.secure_updater.PinnedTransport",
        FailingPinnedTransport,
    )
    client = GitHubReleaseClient(
        owner="owner",
        repo="repo",
        cache_path=tmp_path / "github.json",
    )

    with pytest.raises(DownloadError, match="GitHub release request failed"):
        client.fetch_manifest_locations(manual=True)


def test_downloader_uses_real_urlopen_against_loopback_http_server(tmp_path):
    data = b"real urlopen installer payload"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asset = UpdateAsset(
            name="installer.exe",
            url=f"http://127.0.0.1:{server.server_port}/installer.exe",
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            installer_type="inno",
            os="windows",
            arch="x64",
        )
        target = Downloader(
            cache_dir=tmp_path,
            timeout_seconds=2.0,
            development_mode=True,
        ).download(asset)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert target.read_bytes() == data


def test_development_mode_only_relaxes_asset_urls_for_local_sources():
    validate_asset_url("http://127.0.0.1:8765/installer.exe", frozenset(), development_mode=True)
    validate_asset_url("file:///C:/tmp/installer.exe", frozenset(), development_mode=True)

    with pytest.raises(ManifestError, match="not trusted"):
        validate_asset_url(
            "https://attacker.example/installer.exe",
            {"github.com"},
            development_mode=True,
        )
    with pytest.raises(ManifestError, match="must use https"):
        validate_asset_url(
            "http://github.com/owner/repo/installer.exe",
            {"github.com"},
            development_mode=True,
        )
    with pytest.raises(ManifestError, match="must use https"):
        validate_asset_url(
            "file://attacker.example/share/installer.exe",
            {"github.com"},
            development_mode=True,
        )
    with pytest.raises(ManifestError, match="port"):
        validate_asset_url(
            "https://github.com:444/owner/repo/installer.exe",
            {"github.com"},
        )
    with pytest.raises(ManifestError, match="userinfo"):
        validate_asset_url(
            "https://user:pass@github.com/owner/repo/installer.exe",
            {"github.com"},
        )
    with pytest.raises(ManifestError, match="fragment"):
        validate_asset_url(
            "https://github.com/owner/repo/installer.exe#ignored",
            {"github.com"},
        )
    for invalid_url in (
        " https://github.com/owner/repo/installer.exe",
        "https://github.com/owner/repo/installer.exe\n",
        "https://[::1/installer.exe",
    ):
        with pytest.raises(ManifestError, match="invalid"):
            validate_asset_url(invalid_url, {"github.com"})


def test_manifest_trusted_hosts_use_transport_idna2008_canonicalization(tmp_path):
    manifest_path, signature_path, public_pem = _signed_manifest(
        tmp_path,
        overrides={"trustedHosts": ["faß.de", "xn--fa-hia.de"]},
    )

    manifest = UpdateManifestVerifier(public_key_pem=public_pem).load_verified(
        manifest_path,
        signature_path,
    )

    assert manifest.trusted_hosts == ("xn--fa-hia.de",)


def test_manifest_rejects_invalid_idna_trusted_host(tmp_path):
    manifest_path, signature_path, public_pem = _signed_manifest(
        tmp_path,
        overrides={"trustedHosts": ["ab\u200dcd.example"]},
    )

    with pytest.raises(ManifestError, match="trustedHosts"):
        UpdateManifestVerifier(public_key_pem=public_pem).load_verified(
            manifest_path,
            signature_path,
        )


def test_update_event_filters_all_credential_field_names(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "app.services.secure_updater.debug_logger.log",
        lambda **kwargs: captured.update(kwargs),
    )

    log_update_event(
        "UPDATE_TEST",
        "safe message",
        password="password-value",
        api_key="api-key-value",
        credential="credential-value",
        authorization="bearer-value",
        cookie="cookie-value",
        release_version="3.6.17",
    )

    assert captured["details"] == {"release_version": "3.6.17"}


def test_update_event_is_best_effort_for_hostile_logger_and_detail_formatting(monkeypatch):
    class HostileDetail:
        def __repr__(self) -> str:
            raise SystemExit("detail formatting failed")

    def hostile_logger(**kwargs):
        repr(kwargs["details"])

    monkeypatch.setattr("app.services.secure_updater.debug_logger.log", hostile_logger)

    log_update_event("UPDATE_TEST", "safe message", detail=HostileDetail())


def test_signature_verification_failure_is_not_replaced_by_hostile_telemetry(monkeypatch):
    monkeypatch.setattr(
        "app.services.secure_updater.debug_logger.log",
        lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt("logger failed")),
    )
    verifier = UpdateManifestVerifier(public_key_pem="not-a-public-key")

    with pytest.raises(ManifestError, match="signature verification") as exc_info:
        verifier.verify_signature(b"{}", b"invalid")

    assert isinstance(exc_info.value.__cause__, Exception)


def test_download_completion_telemetry_failure_does_not_change_committed_result(
    tmp_path,
    monkeypatch,
):
    data = b"installer"
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    monkeypatch.setattr(
        "app.services.secure_updater.debug_logger.log",
        lambda **_kwargs: (_ for _ in ()).throw(SystemExit("logger failed")),
    )

    target = Downloader(cache_dir=tmp_path).download(
        asset,
        opener=lambda *_args, **_kwargs: _BytesResponse(data),
    )

    assert target.read_bytes() == data
    assert not target.with_name(f"{target.name}.partial").exists()


def test_pending_install_persists_when_post_commit_telemetry_is_hostile(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(
        "app.services.secure_updater.debug_logger.log",
        lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt("logger failed")),
    )

    result = record_pending_install(
        version="3.7.0",
        installer_path=tmp_path / "installer.exe",
        log_path=tmp_path / "install.log",
        path=state_path,
    )

    assert result.pending_install is not None
    assert LocalUpdateState.load(state_path).pending_install is not None


def test_startup_health_persists_before_best_effort_telemetry(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    LocalUpdateState(pending_install=PendingInstall(version="3.7.0")).save(state_path)
    monkeypatch.setattr(
        "app.services.secure_updater.debug_logger.log",
        lambda **_kwargs: (_ for _ in ()).throw(SystemExit("logger failed")),
    )

    result = record_startup_update_health(
        current_version="3.7.0",
        path=state_path,
    )

    assert result.pending_install is None
    assert LocalUpdateState.load(state_path).pending_install is None


def test_downloader_resumes_existing_partial_file_with_range(tmp_path):
    data = b"0123456789abcdef"
    digest = hashlib.sha256(data).hexdigest()
    partial = tmp_path / "installer.exe.partial"
    partial.write_bytes(data[:5])
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=digest,
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    seen_ranges: list[str | None] = []

    def opener(request, timeout):
        seen_ranges.append(request.get_header("Range"))
        return _BytesResponse(
            data[5:],
            headers={
                "Content-Length": str(len(data) - 5),
                "Content-Range": f"bytes 5-{len(data) - 1}/{len(data)}",
            },
            status=206,
        )

    target = Downloader(cache_dir=tmp_path).download(asset, opener=opener)

    assert seen_ranges == ["bytes=5-"]
    assert target.read_bytes() == data
    assert not partial.exists()


def test_default_downloader_validates_headers_before_first_sink_write(tmp_path, monkeypatch):
    data = b"installer"
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    sink_called = False

    class FakePinnedTransport:
        def __init__(self, **_kwargs):
            return None

        def request_to_sink(self, _method, _url, **kwargs):
            nonlocal sink_called
            kwargs["response_validator"](200, {"Content-Length": "999"})
            sink_called = True
            kwargs["sink"](data)
            raise AssertionError("invalid headers must abort before body")

    monkeypatch.setattr("app.services.secure_updater.PinnedTransport", FakePinnedTransport)

    with pytest.raises(DownloadError, match="Content-Length"):
        Downloader(cache_dir=tmp_path, retries=0).download(asset)

    assert not sink_called
    assert (tmp_path / "installer.exe.partial").read_bytes() == b""


def test_default_downloader_rejects_duplicate_content_length_before_sink(
    tmp_path,
    monkeypatch,
):
    data = b"installer"
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    sink_called = False

    class FakePinnedTransport:
        def __init__(self, **_kwargs):
            return None

        def request_to_sink(self, _method, _url, **kwargs):
            nonlocal sink_called
            kwargs["response_validator"](
                200,
                {
                    "Content-Length": str(len(data)),
                    "content-length": str(len(data)),
                },
            )
            sink_called = True
            kwargs["sink"](data)

    monkeypatch.setattr("app.services.secure_updater.PinnedTransport", FakePinnedTransport)

    with pytest.raises(DownloadError, match="Content-Length is duplicated"):
        Downloader(cache_dir=tmp_path, retries=0).download(asset)

    assert not sink_called


def test_default_downloader_rejects_duplicate_content_range_before_sink(
    tmp_path,
    monkeypatch,
):
    data = b"0123456789"
    partial = tmp_path / "installer.exe.partial"
    partial.write_bytes(data[:4])
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    sink_called = False

    class FakePinnedTransport:
        def __init__(self, **_kwargs):
            return None

        def request_to_sink(self, _method, _url, **kwargs):
            nonlocal sink_called
            value = f"bytes 4-{len(data) - 1}/{len(data)}"
            kwargs["response_validator"](
                206,
                {
                    "Content-Length": str(len(data) - 4),
                    "Content-Range": value,
                    "content-range": value,
                },
            )
            sink_called = True
            kwargs["sink"](data[4:])

    monkeypatch.setattr("app.services.secure_updater.PinnedTransport", FakePinnedTransport)

    with pytest.raises(DownloadError, match="Content-Range is duplicated"):
        Downloader(cache_dir=tmp_path, retries=0).download(asset)

    assert not sink_called


def test_downloader_range_200_restarts_clean_without_consuming_retry(tmp_path, monkeypatch):
    data = b"0123456789"
    partial = tmp_path / "installer.exe.partial"
    partial.write_bytes(data[:4])
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    calls: list[set[int]] = []

    class FakePinnedTransport:
        def __init__(self, **_kwargs):
            return None

        def request_to_sink(self, _method, url, **kwargs):
            calls.append(set(kwargs["accepted_statuses"]))
            if len(calls) == 1:
                kwargs["response_validator"](200, {"Content-Length": str(len(data))})
                raise AssertionError("full response must restart before its body")
            kwargs["response_validator"](200, {"Content-Length": str(len(data))})
            kwargs["sink"](data)
            return SimpleNamespace(status_code=200, url=url, headers={"Content-Length": str(len(data))})

    monkeypatch.setattr("app.services.secure_updater.PinnedTransport", FakePinnedTransport)

    target = Downloader(cache_dir=tmp_path, retries=0).download(asset)

    assert calls == [{200, 206}, {200}]
    assert target.read_bytes() == data


def test_downloader_rejects_invalid_content_range_before_sink(tmp_path, monkeypatch):
    data = b"0123456789"
    partial = tmp_path / "installer.exe.partial"
    partial.write_bytes(data[:4])
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )

    class FakePinnedTransport:
        def __init__(self, **_kwargs):
            return None

        def request_to_sink(self, _method, _url, **kwargs):
            kwargs["response_validator"](
                206,
                {
                    "Content-Length": str(len(data) - 4),
                    "Content-Range": f"bytes 4-{len(data) - 1}/{len(data)} trailing",
                },
            )
            raise AssertionError("invalid Content-Range must abort before body")

    monkeypatch.setattr("app.services.secure_updater.PinnedTransport", FakePinnedTransport)

    with pytest.raises(DownloadError, match="Content-Range"):
        Downloader(cache_dir=tmp_path, retries=0).download(asset)

    assert partial.read_bytes() == b"0123"


def test_downloader_preserves_primary_and_retains_ambiguous_partial(tmp_path, monkeypatch):
    data = b"installer"
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    primary = DownloadError("progress failed")

    def progress(_payload):
        raise primary

    original_unlink = os.unlink

    def hostile_unlink(path, *args, **kwargs):
        if Path(path).name.endswith(".partial"):
            raise KeyboardInterrupt("cleanup failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(path_safety_module.os, "unlink", hostile_unlink)
    with pytest.raises(DownloadError) as exc_info:
        Downloader(cache_dir=tmp_path, retries=0).download(
            asset,
            opener=lambda _request, *, timeout: _BytesResponse(data),
            progress_callback=progress,
        )

    assert exc_info.value is primary
    assert getattr(primary, "__notes__", []) == []
    assert (tmp_path / "installer.exe.partial").read_bytes() == data


def test_partial_cleanup_does_not_unlink_replaced_generation(tmp_path):
    from app.services.updater_downloader import _cleanup_partial

    partial = tmp_path / "installer.exe.partial"
    partial.write_bytes(b"original partial generation")
    partial.unlink()
    partial.write_bytes(b"later partial generation")
    primary = DownloadError("download failed")

    _cleanup_partial(partial, primary, approved_root=tmp_path)

    assert partial.read_bytes() == b"later partial generation"


def test_downloader_preserves_primary_when_response_close_lookup_is_hostile(tmp_path):
    data = b"installer"
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    primary = RuntimeError("progress failed")

    class HostileCloseResponse(_BytesResponse):
        def __getattribute__(self, name):
            if name == "close":
                raise KeyboardInterrupt("close lookup failed")
            return super().__getattribute__(name)

    with pytest.raises(RuntimeError) as exc_info:
        Downloader(cache_dir=tmp_path, retries=0).download(
            asset,
            opener=lambda _request, *, timeout: HostileCloseResponse(data),
            progress_callback=lambda _payload: (_ for _ in ()).throw(primary),
        )

    assert exc_info.value is primary
    assert primary.__notes__ == ["download response cleanup failed"]


def test_github_client_handles_304_and_rate_limit(tmp_path):
    cache = tmp_path / "github.json"
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=cache)

    with pytest.raises(DownloadError, match="without an active validator"):
        client.fetch_manifest_locations(
            opener=lambda request, timeout: (_ for _ in ()).throw(
                client.http_error(request.full_url, 304, "Not Modified")
            )
        )

    with pytest.raises(DownloadError):
        client.fetch_manifest_locations(
            opener=lambda request, timeout: (_ for _ in ()).throw(
                client.http_error(request.full_url, 429, "rate limited")
            )
        )


@pytest.mark.parametrize(
    "last_check_at",
    [
        (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "2026-07-30T12:00:00",
        "not-a-time",
    ],
)
def test_github_auto_check_throttle_fails_open_for_future_or_invalid_time(
    tmp_path,
    last_check_at,
):
    client = GitHubReleaseClient(
        owner="owner",
        repo="repo",
        cache_path=tmp_path / "github.json",
        auto_check_interval_seconds=3600,
    )

    assert not client._is_auto_check_throttled({"last_check_at": last_check_at})


@pytest.mark.parametrize("path", ["latest", "candidates", "atom"])
def test_github_response_cleanup_preserves_exact_primary(tmp_path, path):
    class PrimaryFailure(BaseException):
        pass

    primary = PrimaryFailure("primary response failure")
    response = _BytesResponse(
        b"unused",
        read_error=primary,
        close_error=KeyboardInterrupt("cleanup failure"),
    )
    client = GitHubReleaseClient(
        owner="owner",
        repo="repo",
        cache_path=tmp_path / "github.json",
    )

    with pytest.raises(PrimaryFailure) as exc_info:
        _invoke_github_response_path(
            client,
            path,
            lambda _request, *, timeout: response,
        )

    assert exc_info.value is primary
    assert response.close_calls == 1


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("latest", _github_release_payload()),
        ("candidates", _github_release_payload(as_list=True)),
        ("atom", _RELEASE_ATOM),
    ],
)
def test_github_response_cleanup_normalizes_ordinary_failure(
    tmp_path,
    path,
    payload,
):
    response = _BytesResponse(
        payload,
        close_error=OSError("response close failed"),
    )
    client = GitHubReleaseClient(
        owner="owner",
        repo="repo",
        cache_path=tmp_path / "github.json",
    )

    with pytest.raises(DownloadError, match="response cleanup failed") as exc_info:
        _invoke_github_response_path(
            client,
            path,
            lambda _request, *, timeout: response,
        )

    cause = exc_info.value.__cause__
    if cause is not None and cause.__cause__ is not None:
        cause = cause.__cause__
    assert isinstance(cause, OSError)
    assert response.close_calls == 1


def test_github_latest_release_without_signed_assets_reports_metadata_missing(tmp_path):
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=tmp_path / "github.json")

    def opener(_request, *, timeout):
        return _BytesResponse(
            json.dumps(
                {
                    "tag_name": "v3.8.0",
                    "html_url": "https://github.com/owner/repo/releases/tag/v3.8.0",
                    "assets": [{"name": "installer.exe", "browser_download_url": "https://github.com/file"}],
                }
            ).encode("utf-8")
        )

    with pytest.raises(MetadataNotFoundError):
        client.fetch_manifest_locations(opener=opener, manual=True)


def test_github_latest_release_normalizes_excessive_json_nesting(tmp_path):
    client = GitHubReleaseClient(
        owner="owner",
        repo="repo",
        cache_path=tmp_path / "github.json",
    )
    depth = sys.getrecursionlimit() * 4
    payload = b'{"nested":' + (b"[" * depth) + b"0" + (b"]" * depth) + b"}"

    with pytest.raises(DownloadError, match="request failed"):
        client.fetch_manifest_locations(
            opener=lambda _request, *, timeout: _BytesResponse(payload),
            manual=True,
        )


def test_github_client_rejects_non_array_release_assets(tmp_path):
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=tmp_path / "github.json")

    with pytest.raises(DownloadError, match="assets had an unexpected shape"):
        client.fetch_manifest_locations(
            opener=lambda _request, *, timeout: _BytesResponse(
                json.dumps(
                    {
                        "html_url": "https://github.com/owner/repo/releases/tag/v2",
                        "assets": {"name": "latest.json"},
                    }
                ).encode("utf-8")
            ),
            manual=True,
        )


def test_github_client_throttles_automatic_checks_but_not_manual(tmp_path):
    cache = tmp_path / "github.json"
    cache.write_text(
        json.dumps(
            {
                "schema": 1,
                "endpoint": "latest",
                "api_base": "https://api.github.com",
                "owner": "owner",
                "repo": "repo",
                "etag": "etag-1",
                "last_modified": "Wed, 09 Jul 2026 00:00:00 GMT",
                "last_check_at": datetime.now(timezone.utc).isoformat(),
                "release_url": "https://github.com/owner/repo/releases/tag/v1",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=cache)
    calls: list[str] = []

    def opener(request, timeout):
        calls.append(request.full_url)
        raise AssertionError("automatic check should be throttled")

    throttled = client.fetch_manifest_locations(opener=opener, manual=False)

    assert throttled.not_modified
    assert calls == []

    def manual_opener(request, timeout):
        calls.append(request.full_url)
        return _BytesResponse(
            json.dumps(
                {
                    "html_url": "https://github.com/owner/repo/releases/tag/v2",
                    "assets": [
                        {
                            "name": "latest.json",
                            "browser_download_url": "https://github.com/owner/repo/releases/download/v2/latest.json",
                        },
                        {
                            "name": "latest.json.sig",
                            "browser_download_url": "https://github.com/owner/repo/releases/download/v2/latest.json.sig",
                        },
                    ],
                }
            ).encode("utf-8"),
            headers={"ETag": "etag-2", "Last-Modified": "Wed, 09 Jul 2026 01:00:00 GMT"},
        )

    fresh = client.fetch_manifest_locations(opener=manual_opener, manual=True)

    assert fresh.release_url.endswith("/v2")
    assert len(calls) == 1


def test_github_client_ignores_cache_bound_to_another_repository(tmp_path):
    cache = tmp_path / "github.json"
    cache.write_text(
        json.dumps(
            {
                "schema": 1,
                "endpoint": "latest",
                "api_base": "https://api.github.com",
                "owner": "attacker",
                "repo": "repo",
                "etag": '"foreign"',
                "last_check_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=cache)
    seen_headers: list[dict[str, str]] = []

    def opener(request, *, timeout):
        del timeout
        seen_headers.append(dict(request.header_items()))
        return _BytesResponse(
            json.dumps(
                {
                    "html_url": "https://github.com/owner/repo/releases/tag/v2",
                    "assets": [
                        {
                            "name": "latest.json",
                            "browser_download_url": "https://github.com/owner/repo/releases/download/v2/latest.json",
                        },
                        {
                            "name": "latest.json.sig",
                            "browser_download_url": "https://github.com/owner/repo/releases/download/v2/latest.json.sig",
                        },
                    ],
                }
            ).encode("utf-8")
        )

    result = client.fetch_manifest_locations(opener=opener, manual=False)

    assert result.release_url.endswith("/v2")
    assert all(key.lower() != "if-none-match" for key in seen_headers[0])


def test_github_client_ignores_non_object_cache_json(tmp_path):
    cache = tmp_path / "github.json"
    cache.write_text("[]", encoding="utf-8")
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=cache)

    result = client.fetch_manifest_locations(
        opener=lambda _request, *, timeout: _BytesResponse(
            json.dumps(
                {
                    "html_url": "https://github.com/owner/repo/releases/tag/v2",
                    "assets": [
                        {
                            "name": "latest.json",
                            "browser_download_url": "https://github.com/owner/repo/releases/download/v2/latest.json",
                        },
                        {
                            "name": "latest.json.sig",
                            "browser_download_url": "https://github.com/owner/repo/releases/download/v2/latest.json.sig",
                        },
                    ],
                }
            ).encode("utf-8")
        ),
        manual=False,
    )

    assert result.release_url.endswith("/v2")


def test_github_client_ignores_linked_and_oversized_cache(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(
        json.dumps(
            {
                "schema": 1,
                "endpoint": "latest",
                "api_base": "https://api.github.com",
                "owner": "owner",
                "repo": "repo",
                "etag": '"attacker"',
            }
        ),
        encoding="utf-8",
    )
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    cache = cache_root / "github.json"
    try:
        cache.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.fail("symlinks are unavailable; enable Developer Mode or run elevated - symlink attack coverage must not be silently skipped")

    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=cache)
    assert client._load_cache() == {}
    assert outside.read_text(encoding="utf-8").endswith("}")

    cache.unlink()
    cache.write_bytes(b"{" + b" " * (64 * 1024))
    assert client._load_cache() == {}


def test_github_client_treats_cache_close_failure_as_a_cache_miss(
    tmp_path,
    monkeypatch,
):
    cache = tmp_path / "github.json"
    cache.write_text("{}", encoding="utf-8")

    class CloseFailingStream:
        def read(self, _size):
            return b'{"schema":1,"endpoint":"latest","api_base":"https://api.github.com",' \
                b'"owner":"owner","repo":"repo","etag":"trusted"}'

        def close(self):
            raise OSError("close failed")

    monkeypatch.setattr(
        release_cache_module,
        "open_regular_binary",
        lambda *_args, **_kwargs: CloseFailingStream(),
    )
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=cache)

    assert client._load_cache() == {}


def test_github_client_does_not_persist_empty_validator_cache(tmp_path):
    cache = tmp_path / "github.json"
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=cache)

    client.fetch_manifest_locations(
        opener=lambda _request, *, timeout: _BytesResponse(
            json.dumps(
                {
                    "html_url": "https://github.com/owner/repo/releases/tag/v2",
                    "assets": [
                        {
                            "name": "latest.json",
                            "browser_download_url": "https://github.com/owner/repo/releases/download/v2/latest.json",
                        },
                        {
                            "name": "latest.json.sig",
                            "browser_download_url": "https://github.com/owner/repo/releases/download/v2/latest.json.sig",
                        },
                    ],
                }
            ).encode("utf-8")
        ),
        manual=True,
    )

    assert not cache.exists()


def test_github_client_lists_multiple_signed_manifest_locations(tmp_path):
    cache = tmp_path / "github.json"
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=cache)
    seen_urls: list[str] = []

    def opener(request, timeout):
        seen_urls.append(request.full_url)
        return _BytesResponse(
            json.dumps(
                [
                    {
                        "tag_name": "v3.8.0",
                        "name": "Release 3.8.0",
                        "html_url": "https://github.com/owner/repo/releases/tag/v3.8.0",
                        "assets": [
                            {
                                "name": "latest.json",
                                "browser_download_url": "https://github.com/owner/repo/releases/download/v3.8.0/latest.json",
                            },
                            {
                                "name": "latest.json.sig",
                                "browser_download_url": "https://github.com/owner/repo/releases/download/v3.8.0/latest.json.sig",
                            },
                        ],
                    },
                    {
                        "tag_name": "v3.7.0",
                        "name": "Release 3.7.0",
                        "html_url": "https://github.com/owner/repo/releases/tag/v3.7.0",
                        "assets": [
                            {
                                "name": "latest.json",
                                "browser_download_url": "https://github.com/owner/repo/releases/download/v3.7.0/latest.json",
                            },
                            {
                                "name": "latest.json.sig",
                                "browser_download_url": "https://github.com/owner/repo/releases/download/v3.7.0/latest.json.sig",
                            },
                        ],
                    },
                ]
            ).encode("utf-8"),
            headers={"ETag": "etag-list", "Last-Modified": "Wed, 09 Jul 2026 02:00:00 GMT"},
        )

    locations = client.fetch_manifest_location_candidates(opener=opener, manual=True, per_page=5)

    assert seen_urls == ["https://api.github.com/repos/owner/repo/releases?per_page=5"]
    assert [item.tag_name for item in locations] == ["v3.8.0", "v3.7.0"]
    assert locations[0].manifest_url.endswith("/v3.8.0/latest.json")
    assert locations[1].signature_url.endswith("/v3.7.0/latest.json.sig")


def test_github_release_pages_normalize_excessive_json_nesting(tmp_path):
    client = GitHubReleaseClient(
        owner="owner",
        repo="repo",
        cache_path=tmp_path / "github.json",
    )
    depth = sys.getrecursionlimit() * 4
    payload = (b"[" * depth) + b"0" + (b"]" * depth)

    with pytest.raises(DownloadError, match="not valid JSON"):
        client.fetch_manifest_location_candidates(
            opener=lambda _request, *, timeout: _BytesResponse(payload),
            manual=True,
        )


def test_github_client_follows_next_link_until_it_finds_signed_metadata(tmp_path):
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=tmp_path / "github.json")
    seen_urls: list[str] = []
    next_url = "https://api.github.com/repos/owner/repo/releases?per_page=1&page=2"

    def opener(request, *, timeout):
        seen_urls.append(request.full_url)
        if len(seen_urls) == 1:
            return _BytesResponse(
                json.dumps([{"tag_name": "v3.8.0", "assets": []}]).encode("utf-8"),
                headers={"ETag": '"page-one"', "Link": f'<{next_url}>; rel="next"'},
            )
        return _BytesResponse(
            json.dumps(
                [
                    {
                        "tag_name": "v3.7.0",
                        "name": "Release 3.7.0",
                        "html_url": "https://github.com/owner/repo/releases/tag/v3.7.0",
                        "assets": [
                            {
                                "name": "latest.json",
                                "browser_download_url": "https://github.com/owner/repo/releases/download/v3.7.0/latest.json",
                            },
                            {
                                "name": "latest.json.sig",
                                "browser_download_url": "https://github.com/owner/repo/releases/download/v3.7.0/latest.json.sig",
                            },
                        ],
                    }
                ]
            ).encode("utf-8"),
            headers={"ETag": '"page-two"'},
        )

    locations = client.fetch_manifest_location_candidates(
        opener=opener,
        manual=True,
        per_page=1,
    )

    assert seen_urls == [
        "https://api.github.com/repos/owner/repo/releases?per_page=1",
        next_url,
    ]
    assert [location.tag_name for location in locations] == ["v3.7.0"]
    assert locations[0].etag == ""


def test_github_client_rejects_page_two_304_without_page_validator(tmp_path):
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=tmp_path / "github.json")
    next_url = "https://api.github.com/repos/owner/repo/releases?per_page=1&page=2"
    calls = 0

    def opener(request, *, timeout):
        del timeout
        nonlocal calls
        calls += 1
        if calls == 1:
            return _BytesResponse(
                b"[]",
                headers={"ETag": '"page-one"', "Link": f'<{next_url}>; rel="next"'},
            )
        raise client.http_error(request.full_url, 304, "Not Modified")

    with pytest.raises(DownloadError, match="page-specific validator"):
        client.fetch_manifest_location_candidates(opener=opener, manual=True, per_page=1)

    assert calls == 2


def test_github_candidate_discovery_never_reuses_single_page_validator(tmp_path):
    cache = tmp_path / "github.json"
    cache.write_text(
        json.dumps(
            {
                "schema": 1,
                "endpoint": "latest",
                "api_base": "https://api.github.com",
                "owner": "owner",
                "repo": "repo",
                "etag": '"latest-only"',
                "last_check_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=cache)
    seen_headers: list[dict[str, str]] = []

    def opener(request, *, timeout):
        del timeout
        seen_headers.append(dict(request.header_items()))
        return _BytesResponse(
            json.dumps(
                [
                    {
                        "tag_name": "v3.8.0",
                        "html_url": "https://github.com/owner/repo/releases/tag/v3.8.0",
                        "assets": [
                            {
                                "name": "latest.json",
                                "browser_download_url": "https://github.com/owner/repo/releases/download/v3.8.0/latest.json",
                            },
                            {
                                "name": "latest.json.sig",
                                "browser_download_url": "https://github.com/owner/repo/releases/download/v3.8.0/latest.json.sig",
                            },
                        ],
                    }
                ]
            ).encode("utf-8")
        )

    locations = client.fetch_manifest_location_candidates(opener=opener, manual=False, per_page=1)

    assert len(locations) == 1
    assert "If-none-match" not in seen_headers[0]
    assert locations[0].etag == ""


def test_github_client_stops_after_bounded_release_pages(tmp_path):
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=tmp_path / "github.json")
    seen_urls: list[str] = []

    def opener(request, *, timeout):
        del timeout
        seen_urls.append(request.full_url)
        page = len(seen_urls)
        next_url = (
            "https://api.github.com/repos/owner/repo/releases"
            f"?per_page=1&page={page + 1}"
        )
        return _BytesResponse(
            json.dumps([{"tag_name": f"v3.{page}.0", "assets": []}]).encode("utf-8"),
            headers={"Link": f'<{next_url}>; rel="next"'},
        )

    with pytest.raises(MetadataNotFoundError, match="signed update manifests"):
        client.fetch_manifest_location_candidates(opener=opener, manual=True, per_page=1)

    assert len(seen_urls) == 5
    assert seen_urls[-1].endswith("?per_page=1&page=5")


def test_github_client_stops_scanning_after_bounded_release_count(tmp_path):
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=tmp_path / "github.json")
    releases = [{"tag_name": f"v3.{index}.0", "assets": []} for index in range(500)]
    releases.append(
        {
            "tag_name": "v9.9.9",
            "html_url": "https://github.com/owner/repo/releases/tag/v9.9.9",
            "assets": [
                {
                    "name": "latest.json",
                    "browser_download_url": "https://github.com/owner/repo/releases/download/v9.9.9/latest.json",
                },
                {
                    "name": "latest.json.sig",
                    "browser_download_url": "https://github.com/owner/repo/releases/download/v9.9.9/latest.json.sig",
                },
            ],
        }
    )

    with pytest.raises(MetadataNotFoundError, match="signed update manifests"):
        client.fetch_manifest_location_candidates(
            opener=lambda _request, *, timeout: _BytesResponse(
                json.dumps(releases).encode("utf-8")
            ),
            manual=True,
            per_page=100,
        )


def test_github_client_rejects_next_link_outside_exact_repository(tmp_path):
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=tmp_path / "github.json")
    seen_urls: list[str] = []

    def opener(request, *, timeout):
        del timeout
        seen_urls.append(request.full_url)
        return _BytesResponse(
            b"[]",
            headers={
                "Link": (
                    '<https://api.github.com/repos/attacker/repo/releases?per_page=1&page=2>'
                    '; rel="next"'
                )
            },
        )

    with pytest.raises(DownloadError, match="pagination target is invalid"):
        client.fetch_manifest_location_candidates(opener=opener, manual=True, per_page=1)

    assert len(seen_urls) == 1


def test_github_client_rejects_next_link_on_non_https_port(tmp_path):
    client = GitHubReleaseClient(
        owner="owner",
        repo="repo",
        cache_path=tmp_path / "github.json",
    )

    with pytest.raises(DownloadError, match="pagination target is invalid"):
        client.fetch_manifest_location_candidates(
            opener=lambda _request, *, timeout: _BytesResponse(
                b"[]",
                headers={
                    "Link": (
                        "<https://api.github.com:444/repos/owner/repo/releases"
                        '?per_page=1&page=2>; rel="next"'
                    )
                },
            ),
            manual=True,
            per_page=1,
        )


def test_github_client_caps_valid_candidates_to_requested_count(tmp_path):
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=tmp_path / "github.json")

    def release(version: int) -> dict:
        return {
            "tag_name": f"v3.{version}.0",
            "html_url": f"https://github.com/owner/repo/releases/tag/v3.{version}.0",
            "assets": [
                {
                    "name": "latest.json",
                    "browser_download_url": f"https://github.com/owner/repo/releases/download/v3.{version}.0/latest.json",
                },
                {
                    "name": "latest.json.sig",
                    "browser_download_url": f"https://github.com/owner/repo/releases/download/v3.{version}.0/latest.json.sig",
                },
            ],
        }

    locations = client.fetch_manifest_location_candidates(
        opener=lambda _request, *, timeout: _BytesResponse(
            json.dumps([release(9), release(8), release(7)]).encode("utf-8")
        ),
        manual=True,
        per_page=2,
    )

    assert [location.tag_name for location in locations] == ["v3.9.0", "v3.8.0"]


def test_github_client_rejects_oversized_release_api_page(tmp_path):
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=tmp_path / "github.json")

    with pytest.raises(DownloadError, match="too large"):
        client.fetch_manifest_location_candidates(
            opener=lambda _request, *, timeout: _BytesResponse(b"x" * 2_000_001),
            manual=True,
        )


def test_github_client_passes_timeout_as_keyword_for_urlopen_compatibility(tmp_path):
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=tmp_path / "github.json", timeout=3.5)
    seen_timeout: list[float] = []

    def opener(request, *, timeout):
        seen_timeout.append(timeout)
        return _BytesResponse(
            json.dumps(
                [
                    {
                        "html_url": "https://github.com/owner/repo/releases/tag/v3.8.0",
                        "assets": [
                            {
                                "name": "latest.json",
                                "browser_download_url": "https://github.com/owner/repo/releases/download/v3.8.0/latest.json",
                            },
                            {
                                "name": "latest.json.sig",
                                "browser_download_url": "https://github.com/owner/repo/releases/download/v3.8.0/latest.json.sig",
                            },
                        ],
                    }
                ]
            ).encode("utf-8"),
        )

    client.fetch_manifest_location_candidates(opener=opener, manual=True)

    assert seen_timeout == [3.5]


def test_github_client_falls_back_to_release_atom_when_api_is_rate_limited(tmp_path):
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=tmp_path / "github.json")
    seen_urls: list[str] = []
    atom = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <link rel="alternate" type="text/html" href="https://github.com/owner/repo/releases/tag/v3.8.0" />
    <title>Release 3.8.0</title>
  </entry>
  <entry>
    <link rel="alternate" type="text/html" href="https://github.com/owner/repo/releases/tag/v3.7.0" />
    <title>Release 3.7.0</title>
  </entry>
</feed>"""

    def opener(request, *, timeout):
        seen_urls.append(request.full_url)
        if request.full_url.startswith("https://api.github.com/"):
            raise client.http_error(request.full_url, 429, "rate limited")
        return _BytesResponse(atom)

    locations = client.fetch_manifest_location_candidates(opener=opener, manual=True, per_page=2)

    assert seen_urls == [
        "https://api.github.com/repos/owner/repo/releases?per_page=2",
        "https://github.com/owner/repo/releases.atom",
    ]
    assert [item.tag_name for item in locations] == ["v3.8.0", "v3.7.0"]
    assert locations[0].manifest_url == "https://github.com/owner/repo/releases/download/v3.8.0/latest.json"
    assert locations[1].signature_url == "https://github.com/owner/repo/releases/download/v3.7.0/latest.json.sig"


def test_github_client_rejects_oversized_release_atom(tmp_path):
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=tmp_path / "github.json")

    def opener(request, *, timeout):
        if request.full_url.startswith("https://api.github.com/"):
            raise client.http_error(request.full_url, 429, "rate limited")
        return _BytesResponse(b"x" * 2_000_001)

    with pytest.raises(DownloadError, match="release feed is too large"):
        client.fetch_manifest_location_candidates(opener=opener, manual=True)


def test_github_client_rejects_xml_entities_in_release_atom(tmp_path):
    client = GitHubReleaseClient(owner="owner", repo="repo", cache_path=tmp_path / "github.json")
    atom = b"""<?xml version="1.0"?>
<!DOCTYPE feed [<!ENTITY release "v3.8.0">]>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <link rel="alternate" href="https://github.com/owner/repo/releases/tag/v3.8.0" />
    <title>&release;</title>
  </entry>
</feed>"""

    def opener(request, *, timeout):
        if request.full_url.startswith("https://api.github.com/"):
            raise client.http_error(request.full_url, 429, "rate limited")
        return _BytesResponse(atom)

    with pytest.raises(DownloadError, match="unsafe XML"):
        client.fetch_manifest_location_candidates(opener=opener, manual=True)


def test_trusted_redirect_handler_rejects_target_before_following():
    from app.services.secure_updater import TrustedRedirectHandler

    handler = TrustedRedirectHandler({"github.com", "release-assets.githubusercontent.com"})
    request = urllib.request.Request("https://github.com/owner/repo/releases/download/v/app.exe")

    for target in (
        "http://github.com/owner/repo/app.exe",
        "https://127.0.0.1/app.exe",
        "https://attacker.example/app.exe",
    ):
        with pytest.raises(ManifestError):
            handler.redirect_request(request, None, 302, "Found", {}, target)


def test_installer_runner_uses_argv_and_records_nonzero_exit(tmp_path):
    installer = tmp_path / "installer.msi"
    installer.write_bytes(b"installer")
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    asset = UpdateAsset(
        name="installer.msi",
        url="https://github.com/owner/repo/releases/download/v/installer.msi",
        sha256=digest,
        size=installer.stat().st_size,
        installer_type="msi",
        os="windows",
        arch="x64",
    )
    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, "kwargs": kwargs})
        return subprocess.CompletedProcess(argv, 1603)

    runner = InstallerRunner(
        package_verifier=PackageVerifier(os_name="windows", trusted_publishers=["CN=Test"], verify_func=lambda _p, _a: None),
        run_func=fake_run,
    )

    result = runner.run_verified_installer(
        installer,
        asset,
        version="3.7.0",
        log_path=tmp_path / "install.log",
        approved_root=tmp_path,
    )

    assert result.exit_code == 1603
    assert not result.succeeded
    assert calls[0]["kwargs"]["shell"] is False
    assert calls[0]["argv"][:2] == ["msiexec.exe", "/i"]


def test_installer_runner_detaches_inno_self_update_with_post_install_handoff(tmp_path):
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"installer")
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=digest,
        size=installer.stat().st_size,
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    restart_handoff = tmp_path / "restart.json"
    calls: list[dict] = []
    process = SimpleNamespace(pid=4321)

    def fake_popen(argv, **kwargs):
        calls.append({"argv": argv, "kwargs": kwargs})
        return process

    runner = InstallerRunner(
        package_verifier=PackageVerifier(
            os_name="windows",
            trusted_publishers=["CN=Test"],
            verify_func=lambda _p, _a: None,
        ),
        popen_func=fake_popen,
    )

    launched = runner.launch_verified_installer(
        installer,
        asset,
        version="3.7.0",
        log_path=tmp_path / "install.log",
        install_dir=install_dir,
        restart_handoff_path=restart_handoff,
        restart_handoff_token="one-time-restart-token",
        approved_root=tmp_path,
    )

    assert launched is process
    assert calls[0]["kwargs"]["shell"] is False
    assert calls[0]["kwargs"]["close_fds"] is True
    assert f"/DIR={install_dir}" in calls[0]["argv"]
    assert f"/UCrawlRestartHandoff={restart_handoff}" in calls[0]["argv"]
    assert "one-time-restart-token" not in " ".join(calls[0]["argv"])
    assert calls[0]["kwargs"]["env"]["UCRAWL_RESTART_HANDOFF_TOKEN"] == (
        "one-time-restart-token"
    )
    assert calls[0]["kwargs"]["startupinfo"].lpAttributeList["handle_list"]


@pytest.mark.skipif(os.name != "nt", reason="installer lease inheritance requires Windows")
def test_detached_installer_keeps_delete_lock_until_process_exits(tmp_path):
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"installer")
    asset = _windows_asset_for_file(installer)
    install_dir = tmp_path / "installed"
    install_dir.mkdir()

    def popen(argv, **kwargs):
        del argv
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            shell=False,
            close_fds=True,
            startupinfo=kwargs["startupinfo"],
            creationflags=kwargs.get("creationflags", 0),
        )

    runner = InstallerRunner(
        package_verifier=PackageVerifier(
            os_name="windows",
            trusted_publishers=["CN=Test"],
            verify_func=lambda _p, _a: None,
        ),
        popen_func=popen,
    )
    process = runner.launch_verified_installer(
        installer,
        asset,
        version="3.7.0",
        log_path=tmp_path / "install.log",
        install_dir=install_dir,
        restart_handoff_path=None,
        approved_root=tmp_path,
    )
    try:
        with pytest.raises(OSError):
            installer.unlink()
    finally:
        process.kill()
        process.wait(timeout=10)

    installer.unlink()


@pytest.mark.skipif(os.name != "nt", reason="verified installer leases require Windows")
def test_run_verified_installer_holds_delete_lock_until_process_returns(tmp_path):
    installer = tmp_path / "installer.msi"
    installer.write_bytes(b"installer")
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    asset = UpdateAsset(
        name="installer.msi",
        url="https://github.com/owner/repo/releases/download/v/installer.msi",
        sha256=digest,
        size=installer.stat().st_size,
        installer_type="msi",
        os="windows",
        arch="x64",
    )

    def fake_run(argv, **kwargs):
        del kwargs
        with pytest.raises(OSError):
            installer.unlink()
        return subprocess.CompletedProcess(argv, 0)

    runner = InstallerRunner(
        package_verifier=PackageVerifier(
            os_name="windows",
            trusted_publishers=["CN=Test"],
            verify_func=lambda _p, _a: None,
        ),
        run_func=fake_run,
    )
    runner.run_verified_installer(
        installer,
        asset,
        version="3.7.0",
        log_path=tmp_path / "install.log",
        approved_root=tmp_path,
    )

    installer.unlink()


def _windows_asset_for_file(path: Path) -> UpdateAsset:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return UpdateAsset(
        name=path.name,
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256=digest,
        size=path.stat().st_size,
        installer_type="inno",
        os="windows",
        arch="x64",
    )


def _authenticode_run(
    *,
    status: str = "Valid",
    subject: str = "CN=Trusted Publisher",
    sha1: str = "AA BB CC",
    sha256: str = "11:22:33",
):
    def fake_run(argv, **kwargs):
        payload = {
            "Status": status,
            "Subject": subject,
            "Thumbprint": sha1,
            "SHA256Fingerprint": sha256,
        }
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    return fake_run


def test_package_verifier_explicitly_skips_os_signature_after_hash_check(tmp_path):
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"installer")
    asset = _windows_asset_for_file(installer)

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("Authenticode command must not run when explicitly disabled")

    def unexpected_verify(*_args, **_kwargs):
        raise AssertionError("OS verification hook must not run when explicitly disabled")

    PackageVerifier(
        os_name="windows",
        require_os_signature=False,
        verify_func=unexpected_verify,
        run_func=unexpected_run,
    ).verify(installer, asset)


def test_package_verifier_cannot_skip_hash_when_os_signature_is_disabled(tmp_path):
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"tampered")
    asset = replace(_windows_asset_for_file(installer), sha256="0" * 64)

    with pytest.raises(VerificationError, match="hash"):
        PackageVerifier(os_name="windows", require_os_signature=False).verify(installer, asset)


def test_package_verifier_stops_after_manifest_size_when_file_grows(
    tmp_path,
    monkeypatch,
):
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"installer")
    asset = _windows_asset_for_file(installer)
    real_open = secure_updater_module.open_regular_binary
    read_sizes: list[int] = []

    class GrowingStream:
        def __init__(self, stream):
            self._stream = stream

        def fileno(self):
            return self._stream.fileno()

        def read(self, size=-1):
            read_sizes.append(size)
            if len(read_sizes) == 1:
                return self._stream.read(size)
            return b"x"

        def close(self):
            self._stream.close()

    monkeypatch.setattr(
        secure_updater_module,
        "open_regular_binary",
        lambda path, **kwargs: GrowingStream(real_open(path, **kwargs)),
    )

    with pytest.raises(VerificationError, match="size"):
        PackageVerifier(os_name="windows", require_os_signature=False).verify(
            installer,
            asset,
        )

    assert read_sizes == [asset.size, 1]


def test_windows_verifier_rejects_publisher_only_allowlist(tmp_path):
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"installer")
    asset = _windows_asset_for_file(installer)
    verifier = PackageVerifier(
        os_name="windows",
        trusted_publishers=["CN=Trusted Publisher"],
        run_func=_authenticode_run(),
    )

    with pytest.raises(VerificationError, match="thumbprint"):
        verifier.verify(installer, asset)


def test_windows_verifier_rejects_publisher_match_when_thumbprint_mismatches(tmp_path):
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"installer")
    asset = _windows_asset_for_file(installer)
    verifier = PackageVerifier(
        os_name="windows",
        trusted_publishers=["CN=Trusted Publisher"],
        trusted_thumbprints=["FFFF"],
        run_func=_authenticode_run(),
    )

    with pytest.raises(VerificationError, match="thumbprint"):
        verifier.verify(installer, asset)


def test_windows_verifier_rejects_publisher_mismatch_when_thumbprint_matches(tmp_path):
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"installer")
    asset = _windows_asset_for_file(installer)
    verifier = PackageVerifier(
        os_name="windows",
        trusted_publishers=["CN=Expected Publisher"],
        trusted_thumbprints=["AABBCC"],
        run_func=_authenticode_run(
            subject="CN=Unexpected Publisher",
            sha1="AA BB CC",
        ),
    )

    with pytest.raises(VerificationError, match="publisher"):
        verifier.verify(installer, asset)


@pytest.mark.skipif(os.name != "nt", reason="Authenticode verification requires Windows")
def test_windows_verifier_passes_package_path_as_independent_literalpath(tmp_path):
    installer = tmp_path / "a b; $(calc).exe"
    installer.write_bytes(b"unsigned")
    asset = _windows_asset_for_file(installer)
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        script_path = Path(argv[argv.index("-File") + 1])
        captured["argv"] = list(argv)
        captured["script"] = script_path.read_text(encoding="utf-8-sig")
        # 本测试守护 argv/脚本形态（防 -Command 注入、路径独立传参）。
        # 真实 PowerShell 对非 PE 载荷的 Get-AuthenticodeSignature 行为随
        # runner 版本漂移（NotSigned 或 terminating error），签名解析
        # 语义已由 _authenticode_run 系列 stub 测试覆盖。
        payload = {
            "Status": "NotSigned",
            "Subject": "",
            "Thumbprint": "",
            "SHA256Fingerprint": "",
        }
        return subprocess.CompletedProcess(
            list(argv), 0, stdout=json.dumps(payload), stderr=""
        )

    with pytest.raises(VerificationError, match="not valid"):
        PackageVerifier(
            os_name="windows",
            trusted_thumbprints=["FFFF"],
            run_func=fake_run,
        ).verify(installer, asset)

    argv = captured["argv"]
    script = str(captured["script"])
    assert isinstance(argv, list)
    assert "-Command" not in argv
    assert argv[argv.index("-LiteralPath") + 1] == str(installer)
    assert "Get-AuthenticodeSignature -LiteralPath $LiteralPath" in script
    assert str(installer) not in script


def test_windows_verifier_accepts_normalized_sha1_thumbprint(tmp_path):
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"installer")
    asset = _windows_asset_for_file(installer)
    verifier = PackageVerifier(
        os_name="windows",
        trusted_publishers=["CN=Trusted Publisher"],
        trusted_thumbprints=["aa:bb cc"],
        run_func=_authenticode_run(sha1="AA BB CC"),
    )

    verifier.verify(installer, asset)


@pytest.mark.parametrize("os_name", ["linux", "freebsd"])
def test_package_verifier_fails_closed_when_required_signature_is_unsupported(
    tmp_path,
    os_name,
):
    installer = tmp_path / "installer.AppImage"
    installer.write_bytes(b"installer")
    asset = replace(
        _windows_asset_for_file(installer),
        installer_type="AppImage",
        os=os_name,
    )
    verifier = PackageVerifier(
        os_name=os_name,
        require_os_signature=True,
    )

    with pytest.raises(VerificationError, match="not supported"):
        verifier.verify(installer, asset)


def test_windows_verifier_accepts_sha256_fingerprint(tmp_path):
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"installer")
    asset = _windows_asset_for_file(installer)
    verifier = PackageVerifier(
        os_name="windows",
        trusted_thumbprints=["112233"],
        run_func=_authenticode_run(sha1="AABBCC", sha256="11:22:33"),
    )

    verifier.verify(installer, asset)


def test_windows_verifier_rejects_invalid_authenticode_status(tmp_path):
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"installer")
    asset = _windows_asset_for_file(installer)
    verifier = PackageVerifier(
        os_name="windows",
        trusted_thumbprints=["AABBCC"],
        run_func=_authenticode_run(status="NotSigned"),
    )

    with pytest.raises(VerificationError, match="not valid"):
        verifier.verify(installer, asset)


def test_macos_verifier_requires_trusted_publisher_allowlist(tmp_path):
    installer = tmp_path / "installer.pkg"
    installer.write_bytes(b"installer")
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    asset = UpdateAsset(
        name="installer.pkg",
        url="https://github.com/owner/repo/releases/download/v/installer.pkg",
        sha256=digest,
        size=installer.stat().st_size,
        installer_type="pkg",
        os="macos",
        arch="arm64",
    )

    with pytest.raises(VerificationError):
        PackageVerifier(os_name="macos", run_func=lambda *a, **k: subprocess.CompletedProcess([], 0)).verify(installer, asset)

    calls: list[dict] = []

    def fake_macos_run(argv, **kwargs):
        calls.append({"argv": argv, "kwargs": kwargs})
        return subprocess.CompletedProcess(argv, 0, stdout="Developer ID Installer: Test", stderr="")

    verifier = PackageVerifier(
        os_name="macos",
        trusted_publishers=["Developer ID Installer: Test"],
        run_func=fake_macos_run,
    )

    verifier.verify(installer, asset)

    assert calls[0]["argv"][:2] == ["pkgutil", "--check-signature"]
    assert calls[1]["argv"][:5] == ["spctl", "-a", "-vv", "-t", "install"]
    assert all(call["kwargs"]["shell"] is False for call in calls)


def test_pending_install_clears_after_new_version_starts(tmp_path):
    state = LocalUpdateState(pending_install=PendingInstall(version="3.7.0", attempts=1))
    state.record_startup_health(current_version="3.7.0", staging_dir=tmp_path)

    assert state.pending_install is None
    assert state.last_install_error == ""


def test_pending_install_requires_matching_revision_before_it_is_healthy(tmp_path):
    state = LocalUpdateState(
        pending_install=PendingInstall(version="3.7.0", release_revision=2, attempts=0)
    )

    state.record_startup_health(
        current_version="3.7.0",
        current_revision=1,
        staging_dir=tmp_path,
    )

    assert state.pending_install is not None
    assert state.pending_install.attempts == 1
    assert "v3.7.0-r2" in state.last_install_error


def test_pending_install_state_persists_and_stops_retry_loop(tmp_path):
    state_path = tmp_path / "state.json"

    record_pending_install(
        version="3.7.0",
        installer_path=tmp_path / "installer.exe",
        log_path=tmp_path / "install.log",
        path=state_path,
    )
    first_start = record_startup_update_health(current_version="3.6.17", path=state_path)
    second_start = record_startup_update_health(current_version="3.6.17", path=state_path)

    assert first_start.pending_install is not None
    assert first_start.pending_install.attempts == 1
    assert second_start.pending_install is None
    assert "installed version did not change" in second_start.last_install_error


def test_pending_install_rollback_restores_prior_pending_by_launch_token(tmp_path):
    state_path = tmp_path / "state.json"
    prior = PendingInstall(version="3.6.18", launch_token="prior")
    LocalUpdateState(pending_install=prior, skipped_version="3.6.17").save(state_path)
    previous: list[PendingInstall | None] = []

    record_pending_install(
        version="3.7.0",
        installer_path=tmp_path / "new.exe",
        log_path=tmp_path / "new.log",
        launch_token="launch-b",
        previous_pending_out=previous,
        path=state_path,
    )
    rolled_back = rollback_pending_install(
        "launch-b",
        restore_pending=previous[0],
        path=state_path,
    )

    assert rolled_back.pending_install == prior
    assert rolled_back.skipped_version == "3.6.17"


def test_pending_install_rollback_preserves_a_concurrent_newer_launch(tmp_path):
    state_path = tmp_path / "state.json"
    previous: list[PendingInstall | None] = []
    record_pending_install(
        version="3.7.0",
        installer_path=tmp_path / "b.exe",
        log_path=tmp_path / "b.log",
        launch_token="launch-b",
        previous_pending_out=previous,
        path=state_path,
    )
    record_pending_install(
        version="3.8.0",
        installer_path=tmp_path / "c.exe",
        log_path=tmp_path / "c.log",
        launch_token="launch-c",
        path=state_path,
    )

    rolled_back = rollback_pending_install(
        "launch-b",
        restore_pending=previous[0],
        path=state_path,
    )

    assert rolled_back.pending_install is not None
    assert rolled_back.pending_install.version == "3.8.0"
    assert rolled_back.pending_install.launch_token == "launch-c"


def test_pending_install_retry_limit_does_not_delete_shared_staging_directory(tmp_path):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (staging_dir / "installer.exe").write_bytes(b"stale installer")
    state = LocalUpdateState(
        pending_install=PendingInstall(version="3.7.0", attempts=1),
        install_attempt_limit=2,
    )

    state.record_startup_health(
        current_version="3.6.17",
        staging_dir=staging_dir,
    )

    assert state.pending_install is None
    assert staging_dir.is_dir()
    assert (staging_dir / "installer.exe").is_file()


def test_record_skipped_update_persists_version(tmp_path):
    state = record_skipped_update("v3.7.0", path=tmp_path / "state.json")

    assert state.skipped_version == "3.7.0"


def test_update_state_persists_revision_fields(tmp_path):
    state_path = tmp_path / "state.json"

    record_pending_install(
        version="3.7.0",
        release_revision=2,
        installer_path=tmp_path / "installer.exe",
        log_path=tmp_path / "install.log",
        path=state_path,
    )
    state = record_skipped_update("3.7.0", release_revision=1, path=state_path)

    assert state.skipped_revision == 1
    assert state.pending_install is not None
    assert state.pending_install.release_revision == 2
    assert LocalUpdateState.load(state_path).pending_install.release_revision == 2


def test_version_policy_orders_same_version_by_revision_and_never_forces_skipped_update():
    state = LocalUpdateState(
        last_seen_version="3.7.0",
        last_seen_revision=1,
        skipped_version="3.7.0",
        skipped_revision=2,
    )
    policy = VersionPolicy(channel="stable", state=state)

    assert not policy.evaluate(
        "3.7.0",
        update_revision=1,
        current_version="3.7.0",
        current_revision=1,
    ).allowed
    skipped = policy.evaluate(
        "3.7.0",
        update_revision=2,
        current_version="3.7.0",
        current_revision=1,
        mandatory=True,
    )
    manual = policy.evaluate(
        "3.7.0",
        update_revision=2,
        current_version="3.7.0",
        current_revision=1,
        manual=True,
        mandatory=True,
    )

    assert not skipped.allowed
    assert skipped.mandatory is False
    assert manual.allowed
    assert manual.mandatory is False


def test_update_state_load_tolerates_unknown_fields_and_invalid_numbers(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "last_seen_version": "3.8.0",
                "pending_install": {
                    "version": "3.9.0",
                    "attempts": "not-a-number",
                    "installer_path": "installer.exe",
                    "future_field": True,
                },
                "install_attempt_limit": "invalid",
                "future_root_field": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )

    state = LocalUpdateState.load(state_path)

    assert state.last_seen_version == "3.8.0"
    assert state.install_attempt_limit == 2
    assert state.pending_install == PendingInstall(
        version="3.9.0",
        attempts=0,
        installer_path="installer.exe",
        log_path="",
    )


def test_update_state_load_discards_invalid_semver_fields(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "last_seen_version": "broken",
                "skipped_version": "also-broken",
                "pending_install": {"version": "not-semver", "attempts": -4},
                "install_attempt_limit": 0,
            }
        ),
        encoding="utf-8",
    )

    state = LocalUpdateState.load(state_path)

    assert state.last_seen_version == ""
    assert state.skipped_version == ""
    assert state.pending_install is None
    assert state.install_attempt_limit == 2


def test_update_state_load_fails_safe_for_oversized_cache_file(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_bytes(b'{"last_seen_version":"3.8.0","padding":"' + b"x" * 70_000)

    state = LocalUpdateState.load(state_path)

    assert state == LocalUpdateState()


def test_update_state_load_bounds_strings_and_integer_fields(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "last_seen_version": "3.8.0",
                "last_seen_revision": 10**30,
                "skipped_revision": -1,
                "last_install_error": "e" * 5000,
                "install_attempt_limit": 10**30,
                "pending_install": {
                    "version": "3.9.0",
                    "attempts": 10**30,
                    "installer_path": "p" * 5000,
                    "log_path": "l" * 5000,
                    "release_revision": 10**30,
                    "launch_token": "t" * 500,
                },
            }
        ),
        encoding="utf-8",
    )

    state = LocalUpdateState.load(state_path)

    assert state.last_seen_version == "3.8.0"
    assert state.last_seen_revision == 0
    assert state.skipped_revision == 0
    assert state.last_install_error == ""
    assert state.install_attempt_limit == 2
    assert state.pending_install == PendingInstall(version="3.9.0")


def test_update_state_save_accepts_exact_field_limit_and_rejects_overflow_atomically(tmp_path):
    from app.services.updater_state_model import MAX_STATE_PATH_LENGTH

    state_path = tmp_path / "state.json"
    accepted = LocalUpdateState(
        pending_install=PendingInstall(
            version="3.9.0",
            installer_path="p" * MAX_STATE_PATH_LENGTH,
        )
    )
    accepted.save(state_path)
    accepted_content = state_path.read_bytes()
    assert LocalUpdateState.load(state_path).pending_install == accepted.pending_install

    rejected = LocalUpdateState(
        pending_install=PendingInstall(
            version="3.10.0",
            installer_path="p" * (MAX_STATE_PATH_LENGTH + 1),
        )
    )
    with pytest.raises(ValueError, match="supported length"):
        rejected.save(state_path)

    assert state_path.read_bytes() == accepted_content


def test_update_state_atomic_save_preserves_previous_file_on_replace_failure(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    original = LocalUpdateState(last_seen_version="3.6.17")
    original.save(state_path)
    original_content = state_path.read_text(encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("replace interrupted")

    monkeypatch.setattr("app.services.updater_path_safety.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace interrupted"):
        LocalUpdateState(last_seen_version="3.7.0").save(state_path)

    assert state_path.read_text(encoding="utf-8") == original_content
    retained = list(tmp_path.glob(".state.json.*.tmp"))
    assert len(retained) == 1
    if os.name != "nt":
        assert retained[0].stat().st_mode & 0o777 == 0o600


def test_atomic_write_does_not_create_parent_through_symlinked_ancestor(tmp_path):
    from app.services.secure_updater import _atomic_write_text

    external = tmp_path / "external"
    external.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.fail(f"directory symlink creation unavailable: {exc}; enable Developer Mode or run elevated - symlink attack coverage must not be silently skipped")

    with pytest.raises(OSError, match="ancestry"):
        _atomic_write_text(alias / "metadata" / "state.json", "{}")

    assert not (external / "metadata").exists()


def _approved_root_swap_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Callable[[], None]]:
    approved = tmp_path / "approved"
    approved.mkdir()
    original = tmp_path / "approved-before-swap"
    outside = tmp_path / "outside"
    outside.mkdir()
    probe = tmp_path / "directory-link-probe"
    try:
        probe.symlink_to(outside, target_is_directory=True)
        probe.unlink()
    except OSError as exc:
        pytest.fail(f"directory symlink creation unavailable: {exc}; enable Developer Mode or run elevated - symlink attack coverage must not be silently skipped")

    swapped = False

    def swap() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        approved.rename(original)
        approved.symlink_to(outside, target_is_directory=True)

    return approved, original, outside, swap


def _swap_after_root_validation(
    monkeypatch,
    *,
    approved: Path,
    swap,
) -> None:
    real_validate = path_safety_module._require_regular_directory
    approved_text = os.path.normcase(os.path.abspath(os.fspath(approved)))

    def validate_then_swap(path: Path) -> None:
        real_validate(path)
        candidate_text = os.path.normcase(os.path.abspath(os.fspath(path)))
        if candidate_text == approved_text:
            swap()

    monkeypatch.setattr(
        path_safety_module,
        "_require_regular_directory",
        validate_then_swap,
    )


def test_bounded_read_rejects_approved_root_replaced_after_validation(
    tmp_path,
    monkeypatch,
):
    approved, _original, outside, swap = _approved_root_swap_fixture(tmp_path)
    candidate = approved / "payload.bin"
    candidate.write_bytes(b"approved")
    outside_payload = outside / candidate.name
    outside_payload.write_bytes(b"outside")
    _swap_after_root_validation(monkeypatch, approved=approved, swap=swap)

    with pytest.raises(OSError):
        path_safety_module.read_bounded_regular_bytes(
            candidate,
            approved_root=approved,
            maximum=32,
        )

    assert outside_payload.read_bytes() == b"outside"


def test_binary_write_rejects_approved_root_replaced_after_validation(
    tmp_path,
    monkeypatch,
):
    approved, _original, outside, swap = _approved_root_swap_fixture(tmp_path)
    candidate = approved / "payload.bin"
    candidate.write_bytes(b"approved")
    outside_payload = outside / candidate.name
    outside_payload.write_bytes(b"outside")
    _swap_after_root_validation(monkeypatch, approved=approved, swap=swap)

    with pytest.raises(OSError):
        with path_safety_module.open_regular_binary(
            candidate,
            approved_root=approved,
            mode="wb",
        ) as stream:
            stream.write(b"mutated")

    assert outside_payload.read_bytes() == b"outside"


def test_cross_root_replace_cannot_follow_target_root_replacement(
    tmp_path,
    monkeypatch,
):
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "payload.bin"
    source.write_bytes(b"approved")
    target_root, _original, outside, swap = _approved_root_swap_fixture(tmp_path)
    target = target_root / "payload.bin"
    outside_target = outside / target.name
    outside_target.write_bytes(b"outside")
    real_replace = path_safety_module.os.replace

    def swap_then_replace(source_path, target_path, *args, **kwargs):
        swap()
        return real_replace(source_path, target_path, *args, **kwargs)

    monkeypatch.setattr(path_safety_module.os, "replace", swap_then_replace)

    with pytest.raises(OSError):
        path_safety_module.replace_between_approved_roots(
            source,
            target,
            source_root=source_root,
            target_root=target_root,
        )

    assert outside_target.read_bytes() == b"outside"


def test_replace_regular_file_linearizes_at_the_os_source_name(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source.bin"
    original = tmp_path / "source-original.bin"
    target = tmp_path / "target.bin"
    source.write_bytes(b"verified")
    target.write_bytes(b"previous")
    real_replace = path_safety_module.os.replace
    swapped = False

    def replace_source_then_publish(source_path, target_path, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            source.rename(original)
            source.write_bytes(b"later generation")
        return real_replace(source_path, target_path, *args, **kwargs)

    monkeypatch.setattr(
        path_safety_module.os,
        "replace",
        replace_source_then_publish,
    )

    path_safety_module.replace_regular_file(
        source,
        target,
        approved_root=tmp_path,
    )

    assert original.read_bytes() == b"verified"
    assert not source.exists()
    assert target.read_bytes() == b"later generation"


def test_atomic_write_cannot_create_temporary_file_after_root_replacement(
    tmp_path,
    monkeypatch,
):
    from app.services.updater_atomic_io import atomic_write_bytes

    approved, _original, outside, swap = _approved_root_swap_fixture(tmp_path)
    _swap_after_root_validation(monkeypatch, approved=approved, swap=swap)

    with pytest.raises(OSError):
        atomic_write_bytes(approved / "capability.json", b"private capability")

    assert list(outside.iterdir()) == []


def test_atomic_write_holds_root_after_target_validation(tmp_path, monkeypatch):
    import app.services.updater_atomic_io as atomic_io_module

    approved, _original, outside, swap = _approved_root_swap_fixture(tmp_path)
    real_ensure = atomic_io_module.ensure_regular_or_missing

    def ensure_then_swap(path: Path, *, approved_root: Path) -> Path:
        result = real_ensure(path, approved_root=approved_root)
        swap()
        return result

    monkeypatch.setattr(
        atomic_io_module,
        "ensure_regular_or_missing",
        ensure_then_swap,
    )

    with pytest.raises(OSError):
        atomic_io_module.atomic_write_bytes(
            approved / "capability.json",
            b"private capability",
        )

    assert list(outside.iterdir()) == []


def test_atomic_write_rejects_a_source_successor_published_at_the_target(
    tmp_path,
    monkeypatch,
):
    import app.services.updater_atomic_io as atomic_io_module

    target = tmp_path / "state.json"
    target.write_bytes(b"previous")
    original_temp: Path | None = None
    real_replace = path_safety_module.os.replace
    swapped = False

    def replace_source_then_publish(source_path, target_path, *args, **kwargs):
        nonlocal original_temp, swapped
        source = Path(source_path)
        if not swapped and Path(target_path) == target:
            swapped = True
            original_temp = source.with_name(f"{source.name}.original")
            source.rename(original_temp)
            source.write_bytes(b"later generation")
        return real_replace(source_path, target_path, *args, **kwargs)

    monkeypatch.setattr(
        path_safety_module.os,
        "replace",
        replace_source_then_publish,
    )

    with pytest.raises(OSError, match="target changed"):
        atomic_io_module.atomic_write_bytes(target, b"verified payload")

    assert original_temp is not None
    assert original_temp.read_bytes() == b"verified payload"
    assert target.read_bytes() == b"later generation"


def test_atomic_write_cleanup_does_not_unlink_replaced_temp_file(
    tmp_path,
    monkeypatch,
):
    import app.services.updater_atomic_io as atomic_io_module

    class AmbiguousFailure(BaseException):
        pass

    primary = AmbiguousFailure("replace outcome is ambiguous")
    replacement = b"later file generation"
    temporary_paths: list[Path] = []

    def replace_temp_then_fail(
        source: Path,
        _target: Path,
        *,
        approved_root: Path,
    ) -> None:
        assert approved_root == tmp_path
        source = Path(source)
        source.unlink()
        source.write_bytes(replacement)
        temporary_paths.append(source)
        raise primary

    monkeypatch.setattr(
        atomic_io_module,
        "replace_regular_file",
        replace_temp_then_fail,
    )

    with pytest.raises(AmbiguousFailure) as exc_info:
        atomic_io_module.atomic_write_bytes(tmp_path / "state.json", b"payload")

    assert exc_info.value is primary
    assert len(temporary_paths) == 1
    assert temporary_paths[0].read_bytes() == replacement


def test_temporary_directory_cleanup_does_not_rmdir_replaced_directory(
    tmp_path,
    monkeypatch,
):
    original: Path | None = None
    replacement: Path | None = None
    real_finish = directory_capability_module._finish_capability

    def finish_then_replace(capability, primary_error) -> None:
        nonlocal original, replacement
        real_finish(capability, primary_error)
        candidate = capability.root
        if candidate.parent != tmp_path or not candidate.name.startswith("candidate-"):
            return
        original = candidate.with_name(f"{candidate.name}-original")
        candidate.rename(original)
        candidate.mkdir()
        replacement = candidate

    monkeypatch.setattr(
        directory_capability_module,
        "_finish_capability",
        finish_then_replace,
    )

    with path_safety_module.temporary_approved_directory(
        tmp_path,
        prefix="candidate-",
    ):
        pass

    assert original is not None and original.is_dir()
    assert replacement is not None and replacement.is_dir()


def test_temporary_directory_cleanup_cannot_rmdir_successor_after_final_identity_check(
    tmp_path,
    monkeypatch,
):
    original: Path | None = None
    replacement: Path | None = None
    candidate_path: Path | None = None
    post_lease_stat_calls = 0
    armed = False
    real_stat_child = directory_capability_module.FilesystemDirectoryCapability.stat_child

    def stat_then_replace(capability, name):
        nonlocal original, replacement, post_lease_stat_calls
        value = real_stat_child(capability, name)
        candidate = capability.root / name
        if (
            armed
            and replacement is None
            and candidate.parent == tmp_path
            and candidate.name.startswith("candidate-")
        ):
            post_lease_stat_calls += 1
            original = candidate.with_name(f"{candidate.name}-original")
            candidate.rename(original)
            candidate.mkdir()
            replacement = candidate
        return value

    monkeypatch.setattr(
        directory_capability_module.FilesystemDirectoryCapability,
        "stat_child",
        stat_then_replace,
    )

    with path_safety_module.temporary_approved_directory(
        tmp_path,
        prefix="candidate-",
    ) as candidate:
        candidate_path = candidate
        armed = True

    assert post_lease_stat_calls == 0
    assert candidate_path is not None and candidate_path.is_dir()
    assert original is None
    assert replacement is None


def test_file_lock_cannot_open_lock_after_root_replacement(tmp_path, monkeypatch):
    import app.services.updater_file_lock as file_lock_module

    approved, _original, outside, swap = _approved_root_swap_fixture(tmp_path)
    real_validate = file_lock_module._require_regular_directory
    approved_text = os.path.normcase(os.path.abspath(os.fspath(approved)))

    def validate_then_swap(path: Path) -> None:
        real_validate(path)
        candidate_text = os.path.normcase(os.path.abspath(os.fspath(path)))
        if candidate_text == approved_text:
            swap()

    monkeypatch.setattr(
        file_lock_module,
        "_require_regular_directory",
        validate_then_swap,
    )

    with pytest.raises(OSError):
        with file_lock_module.exclusive_file_lock(
            approved / "metadata.lock",
            approved_root=approved,
        ):
            pytest.fail("a replaced approved lock root must never be entered")

    assert list(outside.iterdir()) == []


def test_file_lock_holds_directory_authority_for_entire_lock_lifetime(tmp_path):
    import app.services.updater_file_lock as file_lock_module

    approved, original, outside, swap = _approved_root_swap_fixture(tmp_path)

    with pytest.raises(OSError):
        with file_lock_module.exclusive_file_lock(
            approved / "metadata.lock",
            approved_root=approved,
        ):
            swap()

    assert list(outside.iterdir()) == []
    assert (approved / "metadata.lock").is_file() or (
        original / "metadata.lock"
    ).is_file()


def test_installer_lease_cannot_follow_approved_root_replacement(
    tmp_path,
    monkeypatch,
):
    import app.services.updater_installer_lease as installer_lease_module

    approved, _original, outside, swap = _approved_root_swap_fixture(tmp_path)
    installer = approved / "installer.exe"
    installer.write_bytes(b"approved")
    outside_installer = outside / installer.name
    outside_installer.write_bytes(b"outside")
    real_require = installer_lease_module.require_regular_file

    def require_then_swap(path: Path, *, approved_root: Path) -> Path:
        result = real_require(path, approved_root=approved_root)
        swap()
        return result

    monkeypatch.setattr(
        installer_lease_module,
        "require_regular_file",
        require_then_swap,
    )

    with pytest.raises(RuntimeError):
        with installer_lease_module.verified_installer_lease(
            installer,
            approved_root=approved,
            error_factory=RuntimeError,
        ):
            pytest.fail("a replaced installer root must never be leased")

    assert outside_installer.read_bytes() == b"outside"


def test_directory_creation_cannot_follow_parent_replacement(tmp_path, monkeypatch):
    approved, _original, outside, swap = _approved_root_swap_fixture(tmp_path)
    target = approved / "metadata"
    real_mkdir = path_safety_module.os.mkdir

    def swap_then_mkdir(path, *args, **kwargs):
        if Path(path) == target:
            swap()
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(path_safety_module.os, "mkdir", swap_then_mkdir)

    with pytest.raises(OSError):
        path_safety_module.ensure_approved_directory(target, create=True)

    assert not (outside / target.name).exists()


def test_candidate_temporary_directory_cannot_follow_metadata_root_replacement(
    tmp_path,
    monkeypatch,
):
    import app.services.update_check_service as update_service

    approved, _original, outside, swap = _approved_root_swap_fixture(tmp_path)
    real_ensure = update_service.ensure_approved_directory
    approved_text = os.path.normcase(os.path.abspath(os.fspath(approved)))
    download_targets: list[Path] = []

    def ensure_then_swap(path: Path, *args, **kwargs):
        result = real_ensure(path, *args, **kwargs)
        candidate_text = os.path.normcase(os.path.abspath(os.fspath(path)))
        if candidate_text == approved_text:
            swap()
        return result

    def reject_external_download(_url, target, **_kwargs):
        download_targets.append(Path(target))
        raise AssertionError("metadata download reached a replaced cache root")

    class ReleaseClient:
        api_base = "https://api.github.com"
        owner = "owner"
        repo = "repo"
        auto_check_interval_seconds = 0

        @staticmethod
        def fetch_manifest_location_candidates(**_kwargs):
            return (
                update_service.ManifestLocations(
                    manifest_url=(
                        "https://github.com/owner/repo/releases/download/"
                        "v3.8.0/latest.json"
                    ),
                    signature_url=(
                        "https://github.com/owner/repo/releases/download/"
                        "v3.8.0/latest.json.sig"
                    ),
                    tag_name="v3.8.0",
                ),
            )

    monkeypatch.setattr(update_service, "default_update_metadata_dir", lambda: approved)
    monkeypatch.setattr(update_service, "ensure_approved_directory", ensure_then_swap)
    monkeypatch.setattr(update_service, "download_metadata_file", reject_external_download)

    with pytest.raises((OSError, update_service.UpdateCheckError)):
        update_service.check_secure_update(
            "3.7.0",
            public_key_pem="configured",
            release_client=ReleaseClient(),
            state=LocalUpdateState(),
        )

    assert download_targets == []
    assert list(outside.iterdir()) == []


def test_bounded_read_rejects_ancestor_replaced_after_chain_validation(
    tmp_path,
    monkeypatch,
):
    ancestor = tmp_path / "ancestor"
    approved = ancestor / "approved"
    approved.mkdir(parents=True)
    candidate = approved / "payload.bin"
    candidate.write_bytes(b"approved")
    moved_ancestor = tmp_path / "ancestor-before-swap"
    outside_ancestor = tmp_path / "outside-ancestor"
    outside_approved = outside_ancestor / approved.name
    outside_approved.mkdir(parents=True)
    outside_payload = outside_approved / candidate.name
    outside_payload.write_bytes(b"outside")
    probe = tmp_path / "directory-link-probe"
    try:
        probe.symlink_to(outside_ancestor, target_is_directory=True)
        probe.unlink()
    except OSError as exc:
        pytest.fail(f"directory symlink creation unavailable: {exc}; enable Developer Mode or run elevated - symlink attack coverage must not be silently skipped")
    real_validate = directory_capability_module._require_regular_directory_ancestry
    approved_text = os.path.normcase(os.path.abspath(os.fspath(approved)))
    swapped = False

    def validate_then_swap(path: Path) -> None:
        nonlocal swapped
        real_validate(path)
        candidate_text = os.path.normcase(os.path.abspath(os.fspath(path)))
        if not swapped and candidate_text == approved_text:
            swapped = True
            ancestor.rename(moved_ancestor)
            ancestor.symlink_to(outside_ancestor, target_is_directory=True)

    monkeypatch.setattr(
        directory_capability_module,
        "_require_regular_directory_ancestry",
        validate_then_swap,
    )

    with pytest.raises(OSError):
        path_safety_module.read_bounded_regular_bytes(
            candidate,
            approved_root=approved,
            maximum=32,
        )

    assert outside_payload.read_bytes() == b"outside"


def test_atomic_write_preserves_primary_without_ambiguous_name_cleanup(tmp_path, monkeypatch):
    from app.services.secure_updater import _atomic_write_text

    class HostileCleanup(BaseException):
        def __str__(self) -> str:
            raise SystemExit("cleanup stringification must not run")

    primary = KeyboardInterrupt("replace interrupted")
    cleanup = HostileCleanup()

    def fail_replace(_source, _target):
        raise primary

    def fail_unlink(_path, *args, **kwargs):
        del args, kwargs
        raise cleanup

    monkeypatch.setattr("app.services.updater_path_safety.os.replace", fail_replace)
    monkeypatch.setattr("app.services.updater_path_safety.os.unlink", fail_unlink)

    with pytest.raises(KeyboardInterrupt) as exc_info:
        _atomic_write_text(tmp_path / "state.json", "{}")

    assert exc_info.value is primary
    if hasattr(BaseException, "add_note"):
        assert getattr(primary, "__notes__", []) == []
    assert len(list(tmp_path.glob(".state.json.*.tmp"))) == 1


def test_atomic_write_ignores_hostile_add_note_while_preserving_primary(tmp_path, monkeypatch):
    from app.services.secure_updater import _atomic_write_text

    class HostilePrimary(BaseException):
        @property
        def add_note(self):
            raise SystemExit("add_note lookup failed")

    primary = HostilePrimary("write failed")

    monkeypatch.setattr(
        "app.services.updater_path_safety.os.replace",
        lambda _source, _target: (_ for _ in ()).throw(primary),
    )
    monkeypatch.setattr(
        "app.services.updater_path_safety.os.unlink",
        lambda _path, *args, **kwargs: (_ for _ in ()).throw(
            KeyboardInterrupt("cleanup failed")
        ),
    )

    with pytest.raises(HostilePrimary) as exc_info:
        _atomic_write_text(tmp_path / "state.json", "{}")

    assert exc_info.value is primary


def test_revision_authorization_preserves_atomic_writer_baseexception(tmp_path, monkeypatch):
    from app.services.update_check_service import _write_revision_authorization
    from shared.release_identity import ReleaseIdentity

    manifest = tmp_path / "latest.json"
    manifest.write_text("{}", encoding="utf-8")
    primary = SystemExit("atomic authorization write failed")

    monkeypatch.setattr(
        "app.services.update_check_service.atomic_write_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(primary),
    )

    with pytest.raises(SystemExit) as exc_info:
        _write_revision_authorization(
            manifest_path=manifest,
            identity=ReleaseIdentity("3.7.0", 1),
            approved_metadata_root=tmp_path,
        )

    assert exc_info.value is primary


def test_updater_helper_entry_uses_installer_runner_and_no_shell_strings():
    helper = Path("entry/updater_helper.py")

    assert helper.exists()
    source = helper.read_text(encoding="utf-8")
    assert "InstallerRunner" in source
    assert "UpdateManifestVerifier" in source
    assert '"--manifest"' in source
    assert '"--signature"' in source
    assert "restart-argv-json" in source
    assert "shell=True" not in source
    assert "os.system" not in source


def test_update_trust_config_contains_only_public_trust_anchors():
    source = Path("app/config/update_trust.py").read_text(encoding="utf-8")

    assert "PRIVATE KEY" not in source
    assert "token" not in source.lower()
    assert "secret" not in source.lower()
    assert "UPDATE_PUBLIC_KEY_PEM" in source
    assert "UPDATE_REQUIRE_OS_SIGNATURE" in source
    assert "UPDATE_TRUSTED_PUBLISHERS" in source
    assert "UPDATE_TRUSTED_THUMBPRINTS" in source


def test_update_trust_config_explicitly_disables_os_signature_for_personal_releases():
    from app.config.update_trust import UPDATE_REQUIRE_OS_SIGNATURE

    assert UPDATE_REQUIRE_OS_SIGNATURE is False


def _call_name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _policy_name(expr: ast.AST | None) -> str:
    if isinstance(expr, ast.Name) and expr.id == "UPDATE_REQUIRE_OS_SIGNATURE":
        return expr.id
    raise AssertionError("PackageVerifier policy is not UPDATE_REQUIRE_OS_SIGNATURE")


def _policy_names_passed_toward_package_verifier(source: str) -> list[str]:
    """解析真实调用。注释和字符串里的同名关键字不会被当成传参。"""
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        keywords = {item.arg: item.value for item in node.keywords if item.arg}
        func_name = _call_name(node.func)
        if func_name == "PackageVerifier":
            names.append(_policy_name(keywords.get("require_os_signature")))
        elif func_name == "prepare_verified_update":
            if _call_name(keywords.get("package_verifier_cls")) != "PackageVerifier":
                raise AssertionError("update entry does not pass PackageVerifier")
            names.append(_policy_name(keywords.get("require_os_signature")))
    return names


def _assert_prepare_verified_update_forwards_policy(source: str) -> None:
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "prepare_verified_update"
    )
    forwarded = False
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or _call_name(node.func) != "package_verifier_cls":
            continue
        keywords = {item.arg: item.value for item in node.keywords if item.arg}
        expr = keywords.get("require_os_signature")
        if not isinstance(expr, ast.Name) or expr.id != "require_os_signature":
            raise AssertionError("prepare_verified_update does not forward the policy parameter")
        forwarded = True
    if not forwarded:
        raise AssertionError("prepare_verified_update never constructs its package verifier")


def test_update_entrypoints_pass_shared_os_signature_policy():
    from app.config.update_trust import UPDATE_REQUIRE_OS_SIGNATURE
    import app.services.update_check_service as update_check_service
    import entry.updater_helper as updater_helper

    comment_only = "\n".join(
        (
            "def build():",
            "    # require_os_signature=UPDATE_REQUIRE_OS_SIGNATURE",
            "    return None",
        )
    )
    commented_wrong_call = "\n".join(
        (
            "def build():",
            "    # require_os_signature=UPDATE_REQUIRE_OS_SIGNATURE",
            "    return PackageVerifier(require_os_signature=True)",
        )
    )
    assert _policy_names_passed_toward_package_verifier(comment_only) == []
    with pytest.raises(AssertionError):
        _policy_names_passed_toward_package_verifier(commented_wrong_call)

    desktop = Path("app/ui/main_window.py").read_text(encoding="utf-8")
    helper = Path("entry/updater_helper.py").read_text(encoding="utf-8")
    service = Path("app/services/update_check_service.py").read_text(encoding="utf-8")

    assert _policy_names_passed_toward_package_verifier(desktop) == [
        "UPDATE_REQUIRE_OS_SIGNATURE"
    ]
    assert _policy_names_passed_toward_package_verifier(helper) == [
        "UPDATE_REQUIRE_OS_SIGNATURE"
    ]
    _assert_prepare_verified_update_forwards_policy(service)
    assert (
        updater_helper.UPDATE_REQUIRE_OS_SIGNATURE
        is update_check_service.UPDATE_REQUIRE_OS_SIGNATURE
        is UPDATE_REQUIRE_OS_SIGNATURE
    )

    delivered = PackageVerifier(
        require_os_signature=UPDATE_REQUIRE_OS_SIGNATURE,
        verify_func=lambda _path, _asset: None,
    )
    assert delivered.require_os_signature is UPDATE_REQUIRE_OS_SIGNATURE


def test_updater_helper_loads_asset_from_signed_manifest(tmp_path, monkeypatch):
    from entry import updater_helper

    manifest_path, signature_path, public_pem = _signed_manifest(tmp_path)
    monkeypatch.setattr(updater_helper, "UPDATE_PUBLIC_KEY_PEM", public_pem, raising=False)
    loader = getattr(updater_helper, "_load_verified_asset", lambda **_kwargs: None)

    asset = loader(
        manifest_path=manifest_path,
        signature_path=signature_path,
        expected_version="3.7.0",
        os_name="windows",
        arch="x64",
    )

    assert isinstance(asset, UpdateAsset)
    assert asset.name == "UniversalCrawlerPro_Setup_3.7.0.exe"


def test_updater_helper_rejects_manifest_for_another_revision(tmp_path, monkeypatch):
    from entry import updater_helper

    manifest_path, signature_path, public_pem = _signed_manifest(
        tmp_path,
        overrides={"releaseRevision": 2, "tag": "v3.7.0-r2"},
    )
    monkeypatch.setattr(updater_helper, "UPDATE_PUBLIC_KEY_PEM", public_pem)

    with pytest.raises(VerificationError, match="release identity"):
        updater_helper._load_verified_asset(
            manifest_path=manifest_path,
            signature_path=signature_path,
            expected_version="3.7.0",
            expected_revision=1,
            os_name="windows",
            arch="x64",
        )


def test_revision_change_authorization_is_manifest_bound_and_single_use(tmp_path):
    from app.services.update_check_service import _write_revision_authorization
    from entry import updater_helper
    from shared.release_identity import ReleaseIdentity

    manifest = tmp_path / "latest.json"
    manifest.write_bytes(b'{"signed":"manifest"}')
    identity = ReleaseIdentity("3.7.0", 2)
    authorization_path, token = _write_revision_authorization(
        manifest_path=manifest,
        identity=identity,
        approved_metadata_root=tmp_path,
    )

    updater_helper._consume_revision_authorization(
        authorization_path=authorization_path,
        token=token,
        manifest_path=manifest,
        target_identity=identity,
        approved_metadata_root=tmp_path,
    )

    assert not authorization_path.exists()
    with pytest.raises(VerificationError, match="authorization"):
        updater_helper._consume_revision_authorization(
            authorization_path=authorization_path,
            token=token,
            manifest_path=manifest,
            target_identity=identity,
            approved_metadata_root=tmp_path,
        )


def test_revision_claim_rejects_an_authorization_successor_published_at_claim(
    tmp_path,
    monkeypatch,
):
    from app.services.update_check_service import _write_revision_authorization
    from entry import updater_helper
    from shared.release_identity import ReleaseIdentity

    manifest = tmp_path / "latest.json"
    manifest.write_bytes(b'{"signed":"manifest"}')
    identity = ReleaseIdentity("3.7.0", 2)
    authorization_path, token = _write_revision_authorization(
        manifest_path=manifest,
        identity=identity,
        approved_metadata_root=tmp_path,
    )
    real_replace = updater_helper.replace_regular_file
    claimed_paths: list[Path] = []

    def replace_authorization_then_claim(source, target, *, approved_root):
        source = Path(source)
        target = Path(target)
        source.rename(source.with_name(f"{source.name}.verified-original"))
        source.write_bytes(b"later authorization generation")
        claimed_paths.append(target)
        real_replace(source, target, approved_root=approved_root)

    monkeypatch.setattr(
        updater_helper,
        "replace_regular_file",
        replace_authorization_then_claim,
    )

    with pytest.raises(VerificationError, match="changed while it was claimed"):
        updater_helper._consume_revision_authorization(
            authorization_path=authorization_path,
            token=token,
            manifest_path=manifest,
            target_identity=identity,
            approved_metadata_root=tmp_path,
        )

    assert len(claimed_paths) == 1
    assert claimed_paths[0].read_bytes() == b"later authorization generation"


def test_revision_claim_cleanup_does_not_unlink_replaced_claimed_file(
    tmp_path,
    monkeypatch,
):
    from app.services.update_check_service import _write_revision_authorization
    from entry import updater_helper
    from shared.release_identity import ReleaseIdentity

    manifest = tmp_path / "latest.json"
    manifest.write_bytes(b'{"signed":"manifest"}')
    identity = ReleaseIdentity("3.7.0", 2)
    authorization_path, token = _write_revision_authorization(
        manifest_path=manifest,
        identity=identity,
        approved_metadata_root=tmp_path,
    )
    real_read = updater_helper._read_revision_authorization
    claimed_paths: list[Path] = []

    def read_then_replace(path: Path, *, root: Path) -> bytes:
        payload = real_read(path, root=root)
        if ".claimed-" in path.name:
            path.unlink()
            path.write_bytes(b"later authorization generation")
            claimed_paths.append(path)
        return payload

    monkeypatch.setattr(
        updater_helper,
        "_read_revision_authorization",
        read_then_replace,
    )

    updater_helper._consume_revision_authorization(
        authorization_path=authorization_path,
        token=token,
        manifest_path=manifest,
        target_identity=identity,
        approved_metadata_root=tmp_path,
    )

    assert len(claimed_paths) == 1
    assert claimed_paths[0].read_bytes() == b"later authorization generation"


def test_forward_transition_does_not_unlink_supplied_authorization_path(tmp_path):
    from entry import updater_helper
    from shared.release_identity import ReleaseIdentity

    authorization = tmp_path / ".ucrawl-revision-auth-later.json"
    authorization.write_bytes(b"later authorization generation")

    with pytest.raises(VerificationError, match="not valid for an upgrade"):
        updater_helper._authorize_release_transition(
            current_identity=ReleaseIdentity("3.7.0", 1),
            target_identity=ReleaseIdentity("3.7.0", 2),
            manifest_path=tmp_path / "latest.json",
            authorization_path=authorization,
            token="unreturned-bearer",
            approved_metadata_root=tmp_path,
        )

    assert authorization.read_bytes() == b"later authorization generation"


def test_revision_change_authorization_rejects_tampered_manifest(tmp_path):
    from app.services.update_check_service import _write_revision_authorization
    from entry import updater_helper
    from shared.release_identity import ReleaseIdentity

    manifest = tmp_path / "latest.json"
    manifest.write_bytes(b'{"signed":"manifest"}')
    identity = ReleaseIdentity("3.7.0", 2)
    authorization_path, token = _write_revision_authorization(
        manifest_path=manifest,
        identity=identity,
        approved_metadata_root=tmp_path,
    )
    manifest.write_bytes(b'{"signed":"different"}')

    with pytest.raises(VerificationError, match="manifest"):
        updater_helper._consume_revision_authorization(
            authorization_path=authorization_path,
            token=token,
            manifest_path=manifest,
            target_identity=identity,
            approved_metadata_root=tmp_path,
        )

    assert authorization_path.is_file()
    manifest.write_bytes(b'{"signed":"manifest"}')
    updater_helper._consume_revision_authorization(
        authorization_path=authorization_path,
        token=token,
        manifest_path=manifest,
        target_identity=identity,
        approved_metadata_root=tmp_path,
    )
    assert not authorization_path.exists()


def test_wrong_revision_token_does_not_consume_legitimate_authorization(tmp_path):
    from app.services.update_check_service import _write_revision_authorization
    from entry import updater_helper

    manifest = tmp_path / "latest.json"
    manifest.write_bytes(b'{"signed":"manifest"}')
    identity = ReleaseIdentity("3.7.0", 2)
    authorization_path, token = _write_revision_authorization(
        manifest_path=manifest,
        identity=identity,
        approved_metadata_root=tmp_path,
    )

    with pytest.raises(VerificationError, match="token"):
        updater_helper._consume_revision_authorization(
            authorization_path=authorization_path,
            token="wrong-token",
            manifest_path=manifest,
            target_identity=identity,
            approved_metadata_root=tmp_path,
        )

    assert authorization_path.is_file()
    updater_helper._consume_revision_authorization(
        authorization_path=authorization_path,
        token=token,
        manifest_path=manifest,
        target_identity=identity,
        approved_metadata_root=tmp_path,
    )
    assert not authorization_path.exists()


def test_revision_authorization_has_exactly_one_concurrent_consumer(tmp_path):
    from app.services.update_check_service import _write_revision_authorization
    from entry import updater_helper

    manifest = tmp_path / "latest.json"
    manifest.write_bytes(b'{"signed":"manifest"}')
    identity = ReleaseIdentity("3.7.0", 2)
    authorization_path, token = _write_revision_authorization(
        manifest_path=manifest,
        identity=identity,
        approved_metadata_root=tmp_path,
    )
    barrier = threading.Barrier(2)

    def consume() -> str:
        barrier.wait(timeout=2)
        try:
            updater_helper._consume_revision_authorization(
                authorization_path=authorization_path,
                token=token,
                manifest_path=manifest,
                target_identity=identity,
                approved_metadata_root=tmp_path,
            )
        except VerificationError as exc:
            assert "revision authorization" in str(exc)
            return "rejected"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(consume), executor.submit(consume))
        results = sorted(future.result(timeout=3) for future in futures)

    assert results == ["accepted", "rejected"]
    assert not authorization_path.exists()


def test_revision_authorization_expires_at_the_exact_ttl_boundary(tmp_path):
    from app.services.update_check_service import _write_revision_authorization
    from entry import updater_helper

    manifest = tmp_path / "latest.json"
    manifest.write_bytes(b'{"signed":"manifest"}')
    identity = ReleaseIdentity("3.7.0", 2)

    authorization_path, token = _write_revision_authorization(
        manifest_path=manifest,
        identity=identity,
        now=100,
        approved_metadata_root=tmp_path,
    )
    updater_helper._consume_revision_authorization(
        authorization_path=authorization_path,
        token=token,
        manifest_path=manifest,
        target_identity=identity,
        now=100 + updater_helper.REVISION_AUTH_TTL_SECONDS - 1,
        approved_metadata_root=tmp_path,
    )

    authorization_path, token = _write_revision_authorization(
        manifest_path=manifest,
        identity=identity,
        now=100,
        approved_metadata_root=tmp_path,
    )
    with pytest.raises(VerificationError, match="expired"):
        updater_helper._consume_revision_authorization(
            authorization_path=authorization_path,
            token=token,
            manifest_path=manifest,
            target_identity=identity,
            now=100 + updater_helper.REVISION_AUTH_TTL_SECONDS,
            approved_metadata_root=tmp_path,
        )


def test_updater_helper_requires_authorization_only_for_non_forward_transition(tmp_path):
    from entry import updater_helper
    from shared.release_identity import ReleaseIdentity

    manifest = tmp_path / "latest.json"
    manifest.write_bytes(b"signed manifest")

    updater_helper._authorize_release_transition(
        current_identity=ReleaseIdentity("3.7.0", 1),
        target_identity=ReleaseIdentity("3.7.0", 2),
        manifest_path=manifest,
        authorization_path=None,
        token="",
        approved_metadata_root=tmp_path,
    )

    with pytest.raises(VerificationError, match="one-time local authorization"):
        updater_helper._authorize_release_transition(
            current_identity=ReleaseIdentity("3.7.0", 2),
            target_identity=ReleaseIdentity("3.7.0", 2),
            manifest_path=manifest,
            authorization_path=None,
            token="",
            approved_metadata_root=tmp_path,
        )


def test_updater_helper_rejects_signed_manifest_for_another_version(tmp_path, monkeypatch):
    from entry import updater_helper

    manifest_path, signature_path, public_pem = _signed_manifest(tmp_path)
    monkeypatch.setattr(updater_helper, "UPDATE_PUBLIC_KEY_PEM", public_pem)

    with pytest.raises(VerificationError, match="does not match requested version"):
        updater_helper._load_verified_asset(
            manifest_path=manifest_path,
            signature_path=signature_path,
            expected_version="3.8.0",
            os_name="windows",
            arch="x64",
        )


def _write_test_release_identity(path: Path, identity: ReleaseIdentity) -> None:
    (path / "release_identity.json").write_text(
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


def _helper_test_roots(tmp_path, monkeypatch, *, installed_identity):
    from entry import updater_helper

    staging = tmp_path / "staging"
    metadata = tmp_path / "metadata"
    install_dir = tmp_path / "installed"
    staging.mkdir()
    metadata.mkdir()
    install_dir.mkdir()
    helper_exe = install_dir / "updater_helper.exe"
    helper_exe.write_bytes(b"helper")
    _write_test_release_identity(install_dir, installed_identity)
    monkeypatch.setattr(updater_helper, "default_update_staging_dir", lambda: staging)
    monkeypatch.setattr(updater_helper, "default_update_metadata_dir", lambda: metadata)
    monkeypatch.setattr(updater_helper.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater_helper.sys, "executable", str(helper_exe))
    return updater_helper, staging, metadata, install_dir


def _issue_helper_launch_capability(
    monkeypatch,
    *,
    staging,
    metadata,
    installer,
    manifest,
    signature,
    log_path,
    install_dir,
    current_identity,
    target_identity,
    restart_argv=(),
    wait_pid=4242,
    restart_handoff_path=None,
):
    from app.services.updater_launch_capability import (
        LAUNCH_CAPABILITY_TOKEN_ENV,
        LaunchBinding,
        issue_launch_capability,
    )

    capability_path, token = issue_launch_capability(
        LaunchBinding(
            installer_path=installer,
            manifest_path=manifest,
            signature_path=signature,
            log_path=log_path,
            install_root=install_dir,
            target_identity=target_identity,
            current_identity=current_identity,
            restart_argv=tuple(restart_argv),
            parent_pid=wait_pid,
            restart_handoff_path=restart_handoff_path,
        ),
        staging_root=staging,
        metadata_root=metadata,
    )
    monkeypatch.setenv(LAUNCH_CAPABILITY_TOKEN_ENV, token)
    return capability_path


def test_updater_helper_detaches_installer_and_writes_restart_handoff(
    tmp_path,
    monkeypatch,
):
    current = ReleaseIdentity("3.6.21", 1)
    target = ReleaseIdentity("3.7.0", 0)
    updater_helper, staging, metadata, install_dir = _helper_test_roots(
        tmp_path,
        monkeypatch,
        installed_identity=current,
    )
    installer = staging / "installer.exe"
    manifest_path = metadata / "latest.json"
    signature_path = metadata / "latest.json.sig"
    log_path = staging / "install.log"
    for path, payload in (
        (installer, b"x"),
        (manifest_path, b"manifest"),
        (signature_path, b"s" * 64),
    ):
        path.write_bytes(payload)
    restart_exe = install_dir / "UniversalCrawlerPro.exe"
    restart_exe.write_bytes(b"exe")
    restart_argv = (str(restart_exe), "--updated")
    handoff_output = staging / ".ucrawl-restart-test.json"
    capability = _issue_helper_launch_capability(
        monkeypatch,
        staging=staging,
        metadata=metadata,
        installer=installer,
        manifest=manifest_path,
        signature=signature_path,
        log_path=log_path,
        install_dir=install_dir,
        current_identity=current,
        target_identity=target,
        restart_argv=restart_argv,
        restart_handoff_path=handoff_output,
    )
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256="a" * 64,
        size=1,
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    calls: list[dict] = []

    class FakeRunner:
        def __init__(self, **_kwargs):
            pass

        def launch_verified_installer(self, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return SimpleNamespace(pid=4242)

    monkeypatch.setattr(updater_helper, "InstallerRunner", FakeRunner)
    monkeypatch.setattr(updater_helper, "_load_verified_asset", lambda **_kwargs: asset)
    monkeypatch.setattr(updater_helper, "_wait_for_process_exit", lambda _pid: None)

    exit_code = updater_helper.main(
        [
            "--installer",
            str(installer),
            "--manifest",
            str(manifest_path),
            "--signature",
            str(signature_path),
            "--version",
            target.version,
            "--log-path",
            str(log_path),
            "--install-dir",
            str(install_dir),
            "--restart-argv-json",
            json.dumps(list(restart_argv)),
            "--restart-handoff-output",
            str(handoff_output),
            "--wait-pid",
            "4242",
            "--launch-capability",
            str(capability),
        ]
    )

    assert exit_code == 0
    assert len(calls) == 1
    assert calls[0]["args"][:2] == (installer, asset)
    assert calls[0]["kwargs"]["install_dir"] == install_dir.resolve()
    handoff_path = calls[0]["kwargs"]["restart_handoff_path"]
    handoff_token = calls[0]["kwargs"]["restart_handoff_token"]
    handoff_text = handoff_path.read_text(encoding="utf-8")
    assert json.loads(handoff_text)["binding"]["restartArgv"] == list(restart_argv)
    assert handoff_token not in handoff_text


def test_updater_helper_launch_failure_does_not_unlink_replaced_restart_handoff(
    tmp_path,
    monkeypatch,
):
    current = ReleaseIdentity("3.6.21", 1)
    target = ReleaseIdentity("3.7.0", 0)
    updater_helper, staging, metadata, install_dir = _helper_test_roots(
        tmp_path,
        monkeypatch,
        installed_identity=current,
    )
    installer = staging / "installer.exe"
    manifest_path = metadata / "latest.json"
    signature_path = metadata / "latest.json.sig"
    log_path = staging / "install.log"
    for path, payload in (
        (installer, b"x"),
        (manifest_path, b"manifest"),
        (signature_path, b"s" * 64),
    ):
        path.write_bytes(payload)
    restart_exe = install_dir / "UniversalCrawlerPro.exe"
    restart_exe.write_bytes(b"exe")
    restart_argv = (str(restart_exe), "--updated")
    handoff_output = staging / ".ucrawl-restart-test.json"
    capability = _issue_helper_launch_capability(
        monkeypatch,
        staging=staging,
        metadata=metadata,
        installer=installer,
        manifest=manifest_path,
        signature=signature_path,
        log_path=log_path,
        install_dir=install_dir,
        current_identity=current,
        target_identity=target,
        restart_argv=restart_argv,
        restart_handoff_path=handoff_output,
    )
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256="a" * 64,
        size=1,
        installer_type="inno",
        os="windows",
        arch="x64",
    )

    class FailingRunner:
        def __init__(self, **_kwargs):
            pass

        def launch_verified_installer(self, *_args, **kwargs):
            handoff = Path(kwargs["restart_handoff_path"])
            handoff.unlink()
            handoff.write_bytes(b"later restart handoff generation")
            raise RuntimeError("installer launch failed")

    monkeypatch.setattr(updater_helper, "InstallerRunner", FailingRunner)
    monkeypatch.setattr(updater_helper, "_load_verified_asset", lambda **_kwargs: asset)
    monkeypatch.setattr(updater_helper, "_wait_for_process_exit", lambda _pid: None)

    exit_code = updater_helper.main(
        [
            "--installer",
            str(installer),
            "--manifest",
            str(manifest_path),
            "--signature",
            str(signature_path),
            "--version",
            target.version,
            "--log-path",
            str(log_path),
            "--install-dir",
            str(install_dir),
            "--restart-argv-json",
            json.dumps(list(restart_argv)),
            "--restart-handoff-output",
            str(handoff_output),
            "--wait-pid",
            "4242",
            "--launch-capability",
            str(capability),
        ]
    )

    assert exit_code == 1
    assert handoff_output.read_bytes() == b"later restart handoff generation"


def _issue_post_install_handoff(tmp_path, monkeypatch, *, with_argument=True):
    from app.services.updater_launch_capability import (
        RESTART_HANDOFF_TOKEN_ENV,
        RestartHandoffBinding,
        issue_restart_handoff,
    )

    target = ReleaseIdentity("3.7.0", 0)
    updater_helper, staging, _metadata, install_dir = _helper_test_roots(
        tmp_path,
        monkeypatch,
        installed_identity=target,
    )
    restart_exe = install_dir / "UniversalCrawlerPro.exe"
    restart_exe.write_bytes(b"app")
    restart_argv = (str(restart_exe), "--updated") if with_argument else (str(restart_exe),)
    handoff_path = staging / ".ucrawl-restart-test.json"
    token = issue_restart_handoff(
        handoff_path,
        RestartHandoffBinding(
            restart_argv=restart_argv,
            install_root=install_dir,
            target_identity=target,
            log_path=staging / "install.log",
        ),
        staging_root=staging,
    )
    monkeypatch.setenv(RESTART_HANDOFF_TOKEN_ENV, token)
    return updater_helper, install_dir, restart_argv, handoff_path


def test_updater_helper_post_install_restarts_validated_installed_app(
    tmp_path,
    monkeypatch,
):
    updater_helper, install_dir, restart_argv, handoff_path = (
        _issue_post_install_handoff(tmp_path, monkeypatch)
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        updater_helper.subprocess,
        "Popen",
        lambda argv, **kwargs: calls.append({"argv": argv, "kwargs": kwargs}),
    )

    exit_code = updater_helper.main(
        ["--complete-install", "--restart-handoff", str(handoff_path)]
    )

    assert exit_code == 0
    assert not handoff_path.exists()
    assert calls[0]["argv"] == list(restart_argv)
    assert calls[0]["kwargs"]["shell"] is False
    assert calls[0]["kwargs"]["cwd"] == str(install_dir.resolve())


def test_updater_helper_post_install_restart_survives_telemetry_baseexception(
    tmp_path,
    monkeypatch,
):
    updater_helper, _install_dir, restart_argv, handoff_path = (
        _issue_post_install_handoff(tmp_path, monkeypatch, with_argument=False)
    )
    launches: list[list[str]] = []
    monkeypatch.setattr(
        updater_helper,
        "log_update_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("hostile")),
    )
    monkeypatch.setattr(
        updater_helper.subprocess,
        "Popen",
        lambda argv, **_kwargs: launches.append(argv),
    )

    exit_code = updater_helper.main(
        ["--complete-install", "--restart-handoff", str(handoff_path)]
    )

    assert exit_code == 0
    assert launches == [list(restart_argv)]
    assert not handoff_path.exists()


def test_updater_helper_persists_windowed_failure_when_telemetry_is_unavailable(
    tmp_path,
    monkeypatch,
):
    from entry import updater_helper

    log_path = tmp_path / "updater-helper.log"
    monkeypatch.setattr(updater_helper, "default_update_staging_dir", lambda: tmp_path)
    monkeypatch.setattr(
        updater_helper,
        "log_update_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("hostile")),
    )

    exit_code = updater_helper.main(["--log-path", str(log_path)])

    assert exit_code == 1
    assert "updater helper failed" in log_path.read_text(encoding="utf-8")
    assert "missing updater helper arguments" in log_path.read_text(encoding="utf-8")


def test_updater_helper_failure_log_rejects_outside_path(tmp_path, monkeypatch):
    from entry import updater_helper

    staging = tmp_path / "staging"
    staging.mkdir()
    victim = tmp_path / "victim.log"
    victim.write_bytes(b"unchanged")
    monkeypatch.setattr(updater_helper, "default_update_staging_dir", lambda: staging)

    assert updater_helper.main(["--log-path", str(victim)]) == 1
    assert victim.read_bytes() == b"unchanged"
    assert (staging / "updater-helper.log").is_file()


def test_updater_helper_failure_log_cannot_append_arbitrary_staging_file(
    tmp_path,
    monkeypatch,
):
    from entry import updater_helper

    staging = tmp_path / "staging"
    staging.mkdir()
    victim = staging / "installer.exe"
    victim.write_bytes(b"trusted-installer")
    monkeypatch.setattr(updater_helper, "default_update_staging_dir", lambda: staging)

    assert updater_helper.main(["--log-path", str(victim)]) == 1
    assert victim.read_bytes() == b"trusted-installer"
    assert (staging / "updater-helper.log").is_file()


def test_updater_helper_waits_for_parent_before_running_installer(
    tmp_path,
    monkeypatch,
):
    current = ReleaseIdentity("3.6.21", 1)
    target = ReleaseIdentity("3.7.0", 0)
    updater_helper, staging, metadata, install_dir = _helper_test_roots(
        tmp_path,
        monkeypatch,
        installed_identity=current,
    )
    installer = staging / "installer.exe"
    manifest_path = metadata / "latest.json"
    signature_path = metadata / "latest.json.sig"
    log_path = staging / "install.log"
    for path, payload in (
        (installer, b"x"),
        (manifest_path, b"manifest"),
        (signature_path, b"s" * 64),
    ):
        path.write_bytes(payload)
    capability = _issue_helper_launch_capability(
        monkeypatch,
        staging=staging,
        metadata=metadata,
        installer=installer,
        manifest=manifest_path,
        signature=signature_path,
        log_path=log_path,
        install_dir=install_dir,
        current_identity=current,
        target_identity=target,
    )
    asset = UpdateAsset(
        name="installer.exe",
        url="https://github.com/owner/repo/releases/download/v/installer.exe",
        sha256="a" * 64,
        size=1,
        installer_type="inno",
        os="windows",
        arch="x64",
    )
    events: list[tuple[str, int | None]] = []

    class FakeRunner:
        def __init__(self, **_kwargs):
            pass

        def launch_verified_installer(self, *_args, **_kwargs):
            events.append(("install", None))
            return SimpleNamespace(pid=4243)

    monkeypatch.setattr(updater_helper, "InstallerRunner", FakeRunner)
    monkeypatch.setattr(updater_helper, "_load_verified_asset", lambda **_kwargs: asset)
    monkeypatch.setattr(
        updater_helper,
        "_wait_for_process_exit",
        lambda pid: events.append(("wait", pid)),
    )

    exit_code = updater_helper.main(
        [
            "--installer",
            str(installer),
            "--manifest",
            str(manifest_path),
            "--signature",
            str(signature_path),
            "--version",
            target.version,
            "--log-path",
            str(log_path),
            "--install-dir",
            str(install_dir),
            "--wait-pid",
            "4242",
            "--launch-capability",
            str(capability),
        ]
    )

    assert exit_code == 0
    assert events == [("wait", 4242), ("install", None)]
