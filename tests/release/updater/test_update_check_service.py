import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from Crypto.PublicKey import ECC

from app.services.update_check_service import (
    UPDATE_STATUS_AVAILABLE,
    UPDATE_STATUS_CURRENT,
    UPDATE_STATUS_LOCAL_NEWER,
    PreparedUpdate,
    UpdateCandidate,
    UpdateCheckResult,
    UpdateCheckError,
    check_for_update,
    check_secure_update,
    compare_versions,
    fetch_latest_release_page_payload,
    fetch_latest_release_payload,
    launch_prepared_update,
    normalize_version,
    prepare_verified_update,
)


class _FakeResponse:
    def __init__(self, *, url: str, body: str = "") -> None:
        self._url = url
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def geturl(self) -> str:
        return self._url

    def read(self, *_args):
        return self._body


class _FakeBytesResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, *_args):
        return self._body


def _pyqt6_available() -> bool:
    try:
        import PyQt6  # noqa: F401
    except ImportError:
        return False
    return True


def _qt_app():
    if not _pyqt6_available():
        return None
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


class UpdateCheckServiceTests(unittest.TestCase):
    def test_secure_update_does_not_create_metadata_through_symlinked_cache_ancestor(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            external = root / "external"
            external.mkdir()
            alias = root / "alias"
            try:
                alias.symlink_to(external, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink creation unavailable: {exc}")
            with patch(
                "app.services.update_check_service.user_cache_root",
                return_value=alias,
            ):
                with self.assertRaisesRegex(UpdateCheckError, "metadata directory is unsafe"):
                    check_secure_update("3.6.17", public_key_pem="configured")

            self.assertFalse((external / "updates").exists())

    def test_normalize_version_accepts_display_and_tag_values(self):
        self.assertEqual(normalize_version("v3.6.17"), "3.6.17")
        self.assertEqual(normalize_version("UniversalCrawlerPro 3.7.0"), "3.7.0")

    def test_compare_versions_uses_numeric_segments(self):
        self.assertLess(compare_versions("v3.6.17", "v3.6.18"), 0)
        self.assertEqual(compare_versions("3.6.17", "v3.6.17"), 0)
        self.assertGreater(compare_versions("3.7.0", "3.6.99"), 0)

    def test_check_for_update_reports_current_release(self):
        result = check_for_update(
            "v3.6.17",
            fetcher=lambda: {"tag_name": "v3.6.17", "html_url": "https://example.test/release"},
        )
        self.assertEqual(result.status, UPDATE_STATUS_CURRENT)
        self.assertEqual(result.local_version, "3.6.17")
        self.assertEqual(result.latest_version, "3.6.17")

    def test_check_for_update_reports_available_release(self):
        result = check_for_update(
            "v3.6.17",
            fetcher=lambda: {"tag_name": "v3.6.18", "html_url": "https://example.test/release"},
        )
        self.assertEqual(result.status, UPDATE_STATUS_AVAILABLE)

    def test_check_for_update_reports_local_newer_release(self):
        result = check_for_update(
            "v3.7.0",
            fetcher=lambda: {"tag_name": "v3.6.18"},
        )
        self.assertEqual(result.status, UPDATE_STATUS_LOCAL_NEWER)

    def test_check_for_update_rejects_missing_release_version(self):
        with self.assertRaises(UpdateCheckError):
            check_for_update("v3.6.17", fetcher=lambda: {"html_url": "https://example.test/release"})

    def test_fetch_latest_release_page_payload_reads_redirect_tag(self):
        response = _FakeResponse(url="https://github.com/haohaizi554/UniversalCrawler/releases/tag/v3.6.18")
        with patch("app.services.update_check_service.open_trusted_url", return_value=response):
            payload = fetch_latest_release_page_payload()

        self.assertEqual(payload["tag_name"], "v3.6.18")
        self.assertEqual(payload["html_url"], "https://github.com/haohaizi554/UniversalCrawler/releases/tag/v3.6.18")

    def test_fetch_latest_release_payload_uses_trusted_url_opener(self):
        response = _FakeResponse(
            url="https://api.github.com/repos/haohaizi554/UniversalCrawler/releases/latest",
            body='{"tag_name":"v3.6.18"}',
        )
        with patch("app.services.update_check_service.open_trusted_url", return_value=response) as opener:
            payload = fetch_latest_release_payload()

        self.assertEqual(payload["tag_name"], "v3.6.18")
        self.assertEqual(opener.call_args.kwargs["allowed_hosts"], {"api.github.com"})
        self.assertEqual(opener.call_args.kwargs["max_response_bytes"], 2_000_001)

    def test_fetch_latest_release_payload_rejects_oversized_body(self):
        response = _FakeBytesResponse(b"x" * 2_000_001)
        with patch("app.services.update_check_service.open_trusted_url", return_value=response):
            with self.assertRaisesRegex(UpdateCheckError, "too large"):
                fetch_latest_release_payload()

    def test_fetch_latest_release_payload_normalizes_excessive_json_nesting(self):
        depth = sys.getrecursionlimit() * 4
        body = '{"nested":' + ("[" * depth) + "0" + ("]" * depth) + "}"
        response = _FakeResponse(
            url="https://api.github.com/repos/haohaizi554/UniversalCrawler/releases/latest",
            body=body,
        )

        with patch(
            "app.services.update_check_service.open_trusted_url",
            return_value=response,
        ):
            with self.assertRaisesRegex(UpdateCheckError, "not valid JSON"):
                fetch_latest_release_payload()

    def test_fetch_latest_release_page_payload_rejects_oversized_body(self):
        response = _FakeBytesResponse(b"x" * 256_001)
        response.geturl = lambda: "https://github.com/haohaizi554/UniversalCrawler/releases/latest"
        with patch("app.services.update_check_service.open_trusted_url", return_value=response):
            with self.assertRaisesRegex(UpdateCheckError, "too large"):
                fetch_latest_release_page_payload()

    def test_fetch_latest_release_payload_rejects_non_https_source_before_open(self):
        with patch("app.services.update_check_service.open_trusted_url") as urlopen:
            with self.assertRaisesRegex(UpdateCheckError, "trusted HTTPS"):
                fetch_latest_release_payload(api_url="file:///tmp/latest.json")

        urlopen.assert_not_called()

    def test_fetch_latest_release_page_payload_rejects_untrusted_redirect_host(self):
        response = _FakeResponse(url="https://attacker.example/releases/tag/v9.9.9")
        with patch("app.services.update_check_service.open_trusted_url", return_value=response):
            with self.assertRaisesRegex(UpdateCheckError, "trusted HTTPS"):
                fetch_latest_release_page_payload()

    def test_fetch_latest_release_payload_falls_back_when_api_is_forbidden(self):
        forbidden = HTTPError(
            url="https://api.github.com/repos/haohaizi554/UniversalCrawler/releases/latest",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=None,
        )
        response = _FakeResponse(url="https://github.com/haohaizi554/UniversalCrawler/releases/tag/v3.6.19")
        with patch("app.services.update_check_service.open_trusted_url", side_effect=[forbidden, response]):
            payload = fetch_latest_release_payload()

        self.assertEqual(payload["tag_name"], "v3.6.19")

    def test_secure_update_requires_configured_public_key(self):
        with self.assertRaises(UpdateCheckError):
            check_secure_update("v3.6.17", public_key_pem="")

    def test_secure_update_reports_available_signed_manifest(self):
        from tempfile import TemporaryDirectory

        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path, sig_path, public_pem = _signed_manifest(root)

            with patch(
                "app.services.update_check_service.default_update_metadata_dir",
                return_value=root,
            ):
                result = check_secure_update(
                    "v3.6.17",
                    public_key_pem=public_pem,
                    manifest_path=manifest_path,
                    signature_path=sig_path,
                    os_name="windows",
                    arch="x64",
                    release_url="https://github.com/owner/repo/releases/tag/v3.7.0",
                )

        self.assertEqual(result.status, UPDATE_STATUS_AVAILABLE)
        self.assertEqual(result.latest_version, "3.7.0")
        self.assertEqual(result.asset_name, "UniversalCrawlerPro_Setup_3.7.0.exe")
        self.assertEqual(result.installer_type, "inno")

    def test_secure_update_returns_verified_candidates_and_honors_selected_version(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import LocalUpdateState, ManifestLocations
        from tests.release.updater.test_secure_updater import _signed_manifest

        def overrides(version: str) -> dict:
            return {
                "version": version,
                "tag": f"v{version}",
                "notes": f"release {version}",
                "assets": {
                    "windows-x64": {
                        "name": f"UniversalCrawlerPro_Setup_{version}.exe",
                        "url": f"https://github.com/owner/repo/releases/download/v{version}/installer.exe",
                        "sha256": "b" * 64,
                        "size": 2048,
                        "installerType": "inno",
                        "os": "windows",
                        "arch": "x64",
                    }
                },
            }

        key = ECC.generate(curve="Ed25519")
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest38, sig38, public_pem = _signed_manifest(
                root / "v3.8.0",
                key=key,
                manifest_name="latest.json",
                overrides=overrides("3.8.0"),
            )
            manifest37, sig37, _public_pem = _signed_manifest(
                root / "v3.7.0",
                key=key,
                manifest_name="latest.json",
                overrides=overrides("3.7.0"),
            )
            blobs = {
                "https://github.com/owner/repo/releases/download/v3.8.0/latest.json": manifest38.read_bytes(),
                "https://github.com/owner/repo/releases/download/v3.8.0/latest.json.sig": sig38.read_bytes(),
                "https://github.com/owner/repo/releases/download/v3.7.0/latest.json": manifest37.read_bytes(),
                "https://github.com/owner/repo/releases/download/v3.7.0/latest.json.sig": sig37.read_bytes(),
            }

            class FakeReleaseClient:
                def fetch_manifest_location_candidates(self, **_kwargs):
                    return (
                        ManifestLocations(
                            manifest_url="https://github.com/owner/repo/releases/download/v3.8.0/latest.json",
                            signature_url="https://github.com/owner/repo/releases/download/v3.8.0/latest.json.sig",
                            release_url="https://github.com/owner/repo/releases/tag/v3.8.0",
                            tag_name="v3.8.0",
                            release_name="Release 3.8.0",
                        ),
                        ManifestLocations(
                            manifest_url="https://github.com/owner/repo/releases/download/v3.7.0/latest.json",
                            signature_url="https://github.com/owner/repo/releases/download/v3.7.0/latest.json.sig",
                            release_url="https://github.com/owner/repo/releases/tag/v3.7.0",
                            tag_name="v3.7.0",
                            release_name="Release 3.7.0",
                        ),
                    )

            def fake_urlopen(request, timeout, **_kwargs):
                return _FakeBytesResponse(blobs[request.full_url])

            with (
                patch("app.services.update_check_service.open_trusted_url", side_effect=fake_urlopen),
                patch("app.services.update_check_service.user_cache_root", return_value=root / "cache"),
            ):
                result = check_secure_update(
                    "v3.6.17",
                    public_key_pem=public_pem,
                    release_client=FakeReleaseClient(),
                    os_name="windows",
                    arch="x64",
                    state=LocalUpdateState(),
                )

        self.assertEqual(result.latest_version, "3.8.0")
        self.assertEqual([candidate.version for candidate in result.candidates], ["3.8.0", "3.7.0"])
        selected = result.for_version("3.7.0")
        self.assertEqual(selected.latest_version, "3.7.0")
        self.assertEqual(selected.asset_name, "UniversalCrawlerPro_Setup_3.7.0.exe")
        self.assertIn("3.7.0", selected.manifest_path)

    def test_verified_metadata_is_revalidated_at_the_authoritative_cache_target(self):
        from tempfile import TemporaryDirectory

        import app.services.update_check_service as update_service
        from app.services.secure_updater import LocalUpdateState, ManifestLocations
        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, signature, public_pem = _signed_manifest(root / "source")
            manifest_url = "https://github.com/owner/repo/releases/download/v3.7.0/latest.json"
            signature_url = f"{manifest_url}.sig"
            blobs = {
                manifest_url: manifest.read_bytes(),
                signature_url: signature.read_bytes(),
            }

            class FakeReleaseClient:
                def fetch_manifest_location_candidates(self, **_kwargs):
                    return (
                        ManifestLocations(
                            manifest_url=manifest_url,
                            signature_url=signature_url,
                            release_url="https://github.com/owner/repo/releases/tag/v3.7.0",
                            tag_name="v3.7.0",
                            release_name="Release 3.7.0",
                        ),
                    )

            def fake_urlopen(request, timeout, **_kwargs):
                del timeout
                return _FakeBytesResponse(blobs[request.full_url])

            real_replace = update_service.replace_between_approved_roots
            authoritative_targets: list[Path] = []
            swapped = False

            def replace_source_then_publish(
                source,
                target,
                *,
                source_root,
                target_root,
            ):
                nonlocal swapped
                source = Path(source)
                target = Path(target)
                if not swapped and source.name == "latest.json":
                    swapped = True
                    source.rename(source.with_name("verified-original.json"))
                    source.write_bytes(b'{"untrusted":true}')
                    authoritative_targets.append(target)
                real_replace(
                    source,
                    target,
                    source_root=source_root,
                    target_root=target_root,
                )

            with (
                patch(
                    "app.services.update_check_service.open_trusted_url",
                    side_effect=fake_urlopen,
                ),
                patch(
                    "app.services.update_check_service.user_cache_root",
                    return_value=root / "cache",
                ),
                patch(
                    "app.services.update_check_service.replace_between_approved_roots",
                    side_effect=replace_source_then_publish,
                ),
            ):
                with self.assertRaises(UpdateCheckError):
                    check_secure_update(
                        "v3.6.17",
                        public_key_pem=public_pem,
                        release_client=FakeReleaseClient(),
                        os_name="windows",
                        arch="x64",
                        state=LocalUpdateState(),
                    )

            self.assertEqual(len(authoritative_targets), 1)
            self.assertEqual(
                authoritative_targets[0].read_bytes(),
                b'{"untrusted":true}',
            )

    def test_active_metadata_cache_treats_excessive_json_nesting_as_a_miss(self):
        from tempfile import TemporaryDirectory

        from app.services.update_metadata_cache import (
            ACTIVE_GENERATION_NAME,
            load_active_generation,
        )

        with TemporaryDirectory() as temp_dir:
            metadata_dir = Path(temp_dir)
            depth = sys.getrecursionlimit() * 4
            payload = '{"nested":' + ("[" * depth) + "0" + ("]" * depth) + "}"
            (metadata_dir / ACTIVE_GENERATION_NAME).write_text(
                payload,
                encoding="utf-8",
            )

            self.assertIsNone(load_active_generation(metadata_dir))

    def test_identity_metadata_cache_normalizes_excessive_json_nesting(self):
        from tempfile import TemporaryDirectory

        from app.services.update_metadata_cache import (
            MetadataIdentityConflictError,
            _load_identity_ledger,
        )

        with TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "identity-ledger.json"
            depth = sys.getrecursionlimit() * 4
            payload = '{"nested":' + ("[" * depth) + "0" + ("]" * depth) + "}"
            ledger.write_text(payload, encoding="utf-8")

            with self.assertRaisesRegex(
                MetadataIdentityConflictError,
                "ledger is invalid",
            ):
                _load_identity_ledger(ledger, "scope")

    def test_stable_manual_check_hides_prerelease_and_rejects_explicit_selection(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import LocalUpdateState

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stable_manifest = root / "stable.json"
            stable_signature = root / "stable.sig"
            prerelease_manifest = root / "prerelease.json"
            prerelease_signature = root / "prerelease.sig"
            stable_manifest.write_bytes(b"stable-manifest")
            stable_signature.write_bytes(b"stable-signature")
            prerelease_manifest.write_bytes(b"prerelease-manifest")
            prerelease_signature.write_bytes(b"prerelease-signature")
            stable = UpdateCandidate(
                version="3.8.0",
                tag_name="v3.8.0",
                release_name="Stable",
                html_url="https://github.com/owner/repo/releases/tag/v3.8.0",
                manifest_path=str(stable_manifest),
                signature_path=str(stable_signature),
            )
            prerelease = UpdateCandidate(
                version="4.0.0-rc.1",
                tag_name="v4.0.0-rc.1",
                release_name="Release candidate",
                html_url="https://github.com/owner/repo/releases/tag/v4.0.0-rc.1",
                manifest_path=str(prerelease_manifest),
                signature_path=str(prerelease_signature),
            )

            with (
                patch(
                    "app.services.update_check_service._fetch_verified_update_candidates",
                    return_value=(prerelease, stable),
                ),
                patch(
                    "app.services.update_check_service.default_update_metadata_dir",
                    return_value=root,
                ),
            ):
                result = check_secure_update(
                    "3.7.0",
                    public_key_pem="configured-test-key",
                    channel="stable",
                    manual=True,
                    state=LocalUpdateState(),
                )
                with self.assertRaisesRegex(UpdateCheckError, "not available"):
                    check_secure_update(
                        "3.7.0",
                        public_key_pem="configured-test-key",
                        channel="stable",
                        manual=True,
                        state=LocalUpdateState(),
                        selected_candidate_id=prerelease.candidate_id,
                    )

        self.assertEqual(result.selected_identity, stable.identity)
        self.assertEqual(tuple(candidate.identity for candidate in result.candidates), (stable.identity,))

    def test_secure_update_orders_same_version_revisions_and_selects_by_candidate_id(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import LocalUpdateState, ManifestLocations
        from tests.release.updater.test_secure_updater import _signed_manifest

        key = ECC.generate(curve="Ed25519")
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            blobs = {}
            locations = []
            public_pem = ""
            for revision in (1, 2):
                tag = f"v3.7.0-r{revision}"
                manifest, signature, public_pem = _signed_manifest(
                    root / tag,
                    key=key,
                    overrides={"releaseRevision": revision, "tag": tag},
                )
                base_url = f"https://github.com/owner/repo/releases/download/{tag}/latest.json"
                blobs[base_url] = manifest.read_bytes()
                blobs[f"{base_url}.sig"] = signature.read_bytes()
                locations.append(
                    ManifestLocations(
                        manifest_url=base_url,
                        signature_url=f"{base_url}.sig",
                        release_url=f"https://github.com/owner/repo/releases/tag/{tag}",
                        tag_name=tag,
                        release_name=tag,
                    )
                )

            class FakeReleaseClient:
                def fetch_manifest_location_candidates(self, **_kwargs):
                    return tuple(locations)

            def fake_urlopen(request, timeout, **_kwargs):
                return _FakeBytesResponse(blobs[request.full_url])

            with (
                patch("app.services.update_check_service.open_trusted_url", side_effect=fake_urlopen),
                patch("app.services.update_check_service.user_cache_root", return_value=root / "cache"),
            ):
                result = check_secure_update(
                    "3.7.0",
                    local_revision=0,
                    public_key_pem=public_pem,
                    release_client=FakeReleaseClient(),
                    os_name="windows",
                    arch="x64",
                    state=LocalUpdateState(),
                )

        self.assertEqual([item.candidate_id for item in result.candidates], ["v3.7.0-r2", "v3.7.0-r1"])
        self.assertEqual(result.latest_revision, 2)
        selected = result.for_candidate("v3.7.0-r1")
        self.assertEqual(selected.latest_revision, 1)
        self.assertEqual(selected.tag_name, "v3.7.0-r1")

    def test_update_result_rejects_ambiguous_version_only_selection(self):
        candidates = (
            UpdateCandidate(version="3.7.0", release_revision=2, tag_name="v3.7.0-r2", release_name="", html_url=""),
            UpdateCandidate(version="3.7.0", release_revision=1, tag_name="v3.7.0-r1", release_name="", html_url=""),
        )
        result = UpdateCheckResult(
            status=UPDATE_STATUS_AVAILABLE,
            local_version="3.6.21",
            latest_version="3.7.0",
            tag_name="v3.7.0-r2",
            release_name="",
            html_url="",
            latest_revision=2,
            candidates=candidates,
        )

        with self.assertRaisesRegex(ValueError, "ambiguous"):
            result.for_version("3.7.0")

    def test_verified_candidate_rejects_release_tag_manifest_mismatch(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import AssetSelector, UpdateManifestVerifier
        from app.services.update_check_service import _candidate_from_verified_metadata
        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            manifest, signature, public_pem = _signed_manifest(
                Path(temp_dir),
                overrides={"releaseRevision": 2, "tag": "v3.7.0-r2"},
            )
            with self.assertRaisesRegex(UpdateCheckError, "Release tag"):
                _candidate_from_verified_metadata(
                    verifier=UpdateManifestVerifier(public_key_pem=public_pem),
                    selector=AssetSelector(os_name="windows", arch="x64"),
                    local_version="3.6.21",
                    manifest_path=manifest,
                    signature_path=signature,
                    tag_name="v3.7.0-r1",
                )

    def test_secure_update_fails_closed_for_legacy_cache_when_release_list_is_not_modified(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import LocalUpdateState, ManifestLocations
        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_metadata = root / "cache" / "updates" / "metadata"
            manifest_path, sig_path, public_pem = _signed_manifest(root / "source")
            cache_metadata.mkdir(parents=True)
            cached_manifest = cache_metadata / "3.7.0.latest.json"
            cached_signature = cache_metadata / "3.7.0.latest.json.sig"
            cached_manifest.write_bytes(manifest_path.read_bytes())
            cached_signature.write_bytes(sig_path.read_bytes())

            class FakeReleaseClient:
                def fetch_manifest_location_candidates(self, **_kwargs):
                    return (ManifestLocations(not_modified=True),)

            with patch("app.services.update_check_service.user_cache_root", return_value=root / "cache"):
                with self.assertRaisesRegex(UpdateCheckError, "no cached manifest"):
                    check_secure_update(
                        "v3.6.17",
                        public_key_pem=public_pem,
                        release_client=FakeReleaseClient(),
                        os_name="windows",
                        arch="x64",
                        state=LocalUpdateState(),
                    )

    def test_not_modified_loads_only_the_atomically_active_cache_generation(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import LocalUpdateState, ManifestLocations
        from tests.release.updater.test_secure_updater import _signed_manifest

        key = ECC.generate(curve="Ed25519")
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            live_manifest, live_signature, public_pem = _signed_manifest(
                root / "live",
                key=key,
                overrides={"version": "3.7.0", "tag": "v3.7.0", "notes": "live"},
            )
            withdrawn_manifest, withdrawn_signature, _ = _signed_manifest(
                root / "withdrawn",
                key=key,
                overrides={"version": "3.8.0", "tag": "v3.8.0", "notes": "withdrawn"},
            )
            manifest_url = "https://github.com/owner/repo/releases/download/v3.7.0/latest.json"
            signature_url = f"{manifest_url}.sig"
            blobs = {
                manifest_url: live_manifest.read_bytes(),
                signature_url: live_signature.read_bytes(),
            }

            class LiveReleaseClient:
                api_base = "https://api.github.com"
                owner = "owner"
                repo = "repo"
                auto_check_interval_seconds = 0

                def fetch_manifest_location_candidates(self, **_kwargs):
                    return (
                        ManifestLocations(
                            manifest_url=manifest_url,
                            signature_url=signature_url,
                            etag='"generation-live"',
                            last_modified="Thu, 30 Jul 2026 00:00:00 GMT",
                            release_url="https://github.com/owner/repo/releases/tag/v3.7.0",
                            tag_name="v3.7.0",
                            release_name="v3.7.0",
                        ),
                    )

            class NotModifiedReleaseClient:
                api_base = "https://api.github.com"
                owner = "owner"
                repo = "repo"
                auto_check_interval_seconds = 0

                def fetch_manifest_location_candidates(self, **_kwargs):
                    return (
                        ManifestLocations(
                            not_modified=True,
                            etag='"generation-live"',
                            last_modified="Thu, 30 Jul 2026 00:00:00 GMT",
                        ),
                    )

            def fake_urlopen(request, timeout, **_kwargs):
                return _FakeBytesResponse(blobs[request.full_url])

            with (
                patch("app.services.update_check_service.user_cache_root", return_value=root / "cache"),
                patch("app.services.update_check_service.open_trusted_url", side_effect=fake_urlopen),
            ):
                first = check_secure_update(
                    "3.6.17",
                    public_key_pem=public_pem,
                    release_client=LiveReleaseClient(),
                    os_name="windows",
                    arch="x64",
                    state=LocalUpdateState(),
                )
                metadata_dir = root / "cache" / "updates" / "metadata"
                (metadata_dir / "v3.8.0-withdrawn.latest.json").write_bytes(
                    withdrawn_manifest.read_bytes()
                )
                (metadata_dir / "v3.8.0-withdrawn.latest.json.sig").write_bytes(
                    withdrawn_signature.read_bytes()
                )
                cached = check_secure_update(
                    "3.6.17",
                    public_key_pem=public_pem,
                    release_client=NotModifiedReleaseClient(),
                    os_name="windows",
                    arch="x64",
                    state=LocalUpdateState(),
                )

        self.assertEqual(first.latest_version, "3.7.0")
        self.assertEqual(cached.latest_version, "3.7.0")
        self.assertEqual(cached.notes, "live")

    def test_verified_cache_is_scoped_to_the_exact_release_discovery(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import DownloadError, LocalUpdateState, ManifestLocations
        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, signature, public_pem = _signed_manifest(root / "source")
            manifest_url = "https://github.com/owner-a/repo-a/releases/download/v3.7.0/latest.json"
            blobs = {
                manifest_url: manifest.read_bytes(),
                f"{manifest_url}.sig": signature.read_bytes(),
            }

            class ReleaseClient:
                api_base = "https://api.github.com"
                auto_check_interval_seconds = 0

                def __init__(self, owner, repo, *, fail=False):
                    self.owner = owner
                    self.repo = repo
                    self.fail = fail

                def fetch_manifest_location_candidates(self, **_kwargs):
                    if self.fail:
                        raise DownloadError("discovery unavailable")
                    return (
                        ManifestLocations(
                            manifest_url=manifest_url,
                            signature_url=f"{manifest_url}.sig",
                            etag='"repo-a"',
                            tag_name="v3.7.0",
                        ),
                    )

            with (
                patch("app.services.update_check_service.user_cache_root", return_value=root / "cache"),
                patch(
                    "app.services.update_check_service.open_trusted_url",
                    side_effect=lambda request, **_kwargs: _FakeBytesResponse(blobs[request.full_url]),
                ),
            ):
                check_secure_update(
                    "3.6.17",
                    public_key_pem=public_pem,
                    release_client=ReleaseClient("owner-a", "repo-a"),
                    os_name="windows",
                    arch="x64",
                    state=LocalUpdateState(),
                )
                with self.assertRaisesRegex(UpdateCheckError, "discovery unavailable"):
                    check_secure_update(
                        "3.6.17",
                        public_key_pem=public_pem,
                        release_client=ReleaseClient("owner-b", "repo-b", fail=True),
                        os_name="windows",
                        arch="x64",
                        state=LocalUpdateState(),
                    )

    def test_automatic_signed_discovery_is_throttled_by_scoped_active_generation(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import LocalUpdateState, ManifestLocations
        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, signature, public_pem = _signed_manifest(root / "source")
            manifest_url = "https://github.com/owner/repo/releases/download/v3.7.0/latest.json"
            blobs = {
                manifest_url: manifest.read_bytes(),
                f"{manifest_url}.sig": signature.read_bytes(),
            }

            class ReleaseClient:
                api_base = "https://api.github.com"
                owner = "owner"
                repo = "repo"
                auto_check_interval_seconds = 3600

                def __init__(self):
                    self.calls = 0

                def fetch_manifest_location_candidates(self, **_kwargs):
                    self.calls += 1
                    return (
                        ManifestLocations(
                            manifest_url=manifest_url,
                            signature_url=f"{manifest_url}.sig",
                            etag='"stable"',
                            tag_name="v3.7.0",
                        ),
                    )

            client = ReleaseClient()
            with (
                patch("app.services.update_check_service.user_cache_root", return_value=root / "cache"),
                patch(
                    "app.services.update_check_service.open_trusted_url",
                    side_effect=lambda request, **_kwargs: _FakeBytesResponse(blobs[request.full_url]),
                ),
            ):
                first = check_secure_update(
                    "3.6.17",
                    public_key_pem=public_pem,
                    release_client=client,
                    os_name="windows",
                    arch="x64",
                    manual=False,
                    state=LocalUpdateState(),
                )
                second = check_secure_update(
                    "3.6.17",
                    public_key_pem=public_pem,
                    release_client=client,
                    os_name="windows",
                    arch="x64",
                    manual=False,
                    state=LocalUpdateState(),
                )
                check_secure_update(
                    "3.6.17",
                    public_key_pem=public_pem,
                    release_client=client,
                    os_name="windows",
                    arch="x64",
                    manual=True,
                    state=LocalUpdateState(),
                )

        self.assertEqual(client.calls, 2)
        self.assertEqual(first.selected_identity, second.selected_identity)

    def test_release_identity_cannot_be_reused_for_different_signed_metadata(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import LocalUpdateState, ManifestLocations
        from tests.release.updater.test_secure_updater import _signed_manifest

        key = ECC.generate(curve="Ed25519")
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_manifest, first_signature, public_pem = _signed_manifest(
                root / "first",
                key=key,
                overrides={"notes": "immutable A"},
            )
            second_manifest, second_signature, _ = _signed_manifest(
                root / "second",
                key=key,
                overrides={"notes": "replacement B"},
            )
            manifest_url = "https://github.com/owner/repo/releases/download/v3.7.0/latest.json"
            blobs = {
                manifest_url: first_manifest.read_bytes(),
                f"{manifest_url}.sig": first_signature.read_bytes(),
            }

            class ReleaseClient:
                api_base = "https://api.github.com"
                owner = "owner"
                repo = "repo"
                auto_check_interval_seconds = 0

                def fetch_manifest_location_candidates(self, **_kwargs):
                    return (
                        ManifestLocations(
                            manifest_url=manifest_url,
                            signature_url=f"{manifest_url}.sig",
                            etag='"identity"',
                            tag_name="v3.7.0",
                        ),
                    )

            client = ReleaseClient()
            with (
                patch("app.services.update_check_service.user_cache_root", return_value=root / "cache"),
                patch(
                    "app.services.update_check_service.open_trusted_url",
                    side_effect=lambda request, **_kwargs: _FakeBytesResponse(blobs[request.full_url]),
                ),
            ):
                first = check_secure_update(
                    "3.6.17",
                    public_key_pem=public_pem,
                    release_client=client,
                    os_name="windows",
                    arch="x64",
                    state=LocalUpdateState(),
                )
                blobs[manifest_url] = second_manifest.read_bytes()
                blobs[f"{manifest_url}.sig"] = second_signature.read_bytes()
                with self.assertRaisesRegex(UpdateCheckError, "conflicting signed manifests"):
                    check_secure_update(
                        "3.6.17",
                        public_key_pem=public_pem,
                        release_client=client,
                        os_name="windows",
                        arch="x64",
                        max_candidates=1,
                        state=LocalUpdateState(),
                    )

        self.assertEqual(first.notes, "immutable A")

    def test_identity_ledger_serializes_competing_processes(self):
        from tempfile import TemporaryDirectory

        from tests.release.updater.test_secure_updater import _signed_manifest

        script = r"""
import sys
import time
from pathlib import Path
from app.services.secure_updater import _atomic_write_text
from app.services.update_metadata_cache import (
    MetadataIdentityConflictError,
    metadata_record,
    write_active_generation,
)
metadata_dir, manifest, signature, ready, gate = map(Path, sys.argv[1:])
record = metadata_record(manifest, signature, candidate_id="v3.7.0")
ready.write_text("ready", encoding="utf-8")
while not gate.exists():
    time.sleep(0.01)
try:
    write_active_generation(
        metadata_dir,
        etag="",
        last_modified="",
        records=(record,),
        atomic_write_text=_atomic_write_text,
        scope='{"pageSize":10}',
        identity_scope='{"owner":"owner","repo":"repo"}',
    )
except MetadataIdentityConflictError:
    print("conflict")
    raise SystemExit(23)
print("ok")
"""
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            metadata_dir = root / "metadata"
            metadata_dir.mkdir()
            first_manifest, first_signature, _ = _signed_manifest(
                root / "first",
                overrides={"notes": "A"},
            )
            second_manifest, second_signature, _ = _signed_manifest(
                root / "second",
                overrides={"notes": "B"},
            )
            gate = root / "gate"
            processes = []
            for index, (manifest, signature) in enumerate(
                (
                    (first_manifest, first_signature),
                    (second_manifest, second_signature),
                )
            ):
                ready = root / f"ready-{index}"
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(metadata_dir),
                        str(manifest),
                        str(signature),
                        str(ready),
                        str(gate),
                    ],
                    cwd=Path(__file__).resolve().parents[3],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                processes.append((process, ready))
            for _ in range(200):
                if all(ready.exists() for _process, ready in processes):
                    break
                __import__("time").sleep(0.01)
            self.assertTrue(all(ready.exists() for _process, ready in processes))
            gate.write_text("go", encoding="utf-8")
            results = [
                (*process.communicate(timeout=10), process.returncode)
                for process, _ready in processes
            ]

        self.assertEqual(sorted(result[2] for result in results), [0, 23], results)
        self.assertEqual(sorted(result[0].strip() for result in results), ["conflict", "ok"])

    def test_fresh_invalid_signature_is_terminal_and_does_not_use_cached_metadata(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import LocalUpdateState, ManifestLocations
        from tests.release.updater.test_secure_updater import _signed_manifest

        key = ECC.generate(curve="Ed25519")
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cached_manifest, cached_signature, public_pem = _signed_manifest(
                root / "cached",
                key=key,
                overrides={"notes": "last known good"},
            )
            fresh_manifest, _fresh_signature, _ = _signed_manifest(
                root / "fresh",
                key=key,
                overrides={"version": "3.8.0", "tag": "v3.8.0"},
            )
            manifest_url = "https://github.com/owner/repo/releases/download/current/latest.json"
            blobs = {
                manifest_url: cached_manifest.read_bytes(),
                f"{manifest_url}.sig": cached_signature.read_bytes(),
            }

            class ReleaseClient:
                api_base = "https://api.github.com"
                owner = "owner"
                repo = "repo"
                auto_check_interval_seconds = 0

                def fetch_manifest_location_candidates(self, **_kwargs):
                    return (
                        ManifestLocations(
                            manifest_url=manifest_url,
                            signature_url=f"{manifest_url}.sig",
                            etag='"current"',
                        ),
                    )

            client = ReleaseClient()
            with (
                patch("app.services.update_check_service.user_cache_root", return_value=root / "cache"),
                patch(
                    "app.services.update_check_service.open_trusted_url",
                    side_effect=lambda request, **_kwargs: _FakeBytesResponse(blobs[request.full_url]),
                ),
            ):
                first = check_secure_update(
                    "3.6.17",
                    public_key_pem=public_pem,
                    release_client=client,
                    os_name="windows",
                    arch="x64",
                    state=LocalUpdateState(),
                )
                blobs[manifest_url] = fresh_manifest.read_bytes()
                blobs[f"{manifest_url}.sig"] = b"invalid signature"
                with self.assertRaisesRegex(UpdateCheckError, "signature"):
                    check_secure_update(
                        "3.6.17",
                        public_key_pem=public_pem,
                        release_client=client,
                        os_name="windows",
                        arch="x64",
                        state=LocalUpdateState(),
                    )

        self.assertEqual(first.notes, "last known good")

    def test_slow_check_merges_last_seen_without_losing_concurrent_state_writes(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import (
            LocalUpdateState,
            record_pending_install,
            record_skipped_update,
        )

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_path = root / "state.json"
            manifest = root / "latest.json"
            signature = root / "latest.json.sig"
            manifest.write_bytes(b"manifest")
            signature.write_bytes(b"signature")
            candidate = UpdateCandidate(
                version="3.7.0",
                tag_name="v3.7.0",
                release_name="v3.7.0",
                html_url="",
                manifest_path=str(manifest),
                signature_path=str(signature),
            )
            discovery_started = threading.Event()
            release_discovery = threading.Event()

            def slow_fetch(**_kwargs):
                discovery_started.set()
                self.assertTrue(release_discovery.wait(3))
                return (candidate,)

            with (
                patch(
                    "app.services.update_check_service._fetch_verified_update_candidates",
                    side_effect=slow_fetch,
                ),
                patch(
                    "app.services.secure_updater.default_update_state_path",
                    return_value=state_path,
                ),
                patch(
                    "app.services.update_check_service.default_update_metadata_dir",
                    return_value=root,
                ),
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    check = executor.submit(
                        check_secure_update,
                        "3.6.17",
                        public_key_pem="configured-test-key",
                        os_name="windows",
                        arch="x64",
                    )
                    self.assertTrue(discovery_started.wait(3))
                    record_skipped_update("3.6.18", path=state_path)
                    record_pending_install(
                        version="3.6.19",
                        installer_path=root / "pending.exe",
                        log_path=root / "pending.log",
                        launch_token="concurrent",
                        path=state_path,
                    )
                    release_discovery.set()
                    check.result(timeout=3)

            state = LocalUpdateState.load(state_path)

        self.assertEqual(state.last_seen_version, "3.7.0")
        self.assertEqual(state.skipped_version, "3.6.18")
        self.assertIsNotNone(state.pending_install)
        self.assertEqual(state.pending_install.version, "3.6.19")

    def test_secure_update_fails_closed_for_legacy_cache_when_release_discovery_fails(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import DownloadError, LocalUpdateState
        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_metadata = root / "cache" / "updates" / "metadata"
            manifest_path, sig_path, public_pem = _signed_manifest(root / "source")
            cache_metadata.mkdir(parents=True)
            (cache_metadata / "3.7.0.latest.json").write_bytes(manifest_path.read_bytes())
            (cache_metadata / "3.7.0.latest.json.sig").write_bytes(sig_path.read_bytes())

            class FailingReleaseClient:
                def fetch_manifest_location_candidates(self, **_kwargs):
                    raise DownloadError("GitHub rate limit reached")

            with patch("app.services.update_check_service.user_cache_root", return_value=root / "cache"):
                with self.assertRaisesRegex(UpdateCheckError, "rate limit"):
                    check_secure_update(
                        "v3.6.17",
                        public_key_pem=public_pem,
                        release_client=FailingReleaseClient(),
                        os_name="windows",
                        arch="x64",
                        state=LocalUpdateState(),
                    )

    def test_secure_update_does_not_reconstruct_active_generation_from_legacy_files(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import LocalUpdateState, ManifestLocations
        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_metadata = root / "cache" / "updates" / "metadata"
            cache_metadata.mkdir(parents=True)
            key = ECC.generate(curve="Ed25519")
            old_manifest, old_signature, public_pem = _signed_manifest(
                root / "old",
                key=key,
                overrides={
                    "publishedAt": "2026-07-09T00:00:00Z",
                    "notes": "old signed generation",
                },
            )
            new_manifest, new_signature, _ = _signed_manifest(
                root / "new",
                key=key,
                overrides={
                    "publishedAt": "2026-07-10T00:00:00Z",
                    "notes": "new signed generation",
                },
            )
            (cache_metadata / "3.7.0-old.latest.json").write_bytes(old_manifest.read_bytes())
            (cache_metadata / "3.7.0-old.latest.json.sig").write_bytes(old_signature.read_bytes())
            (cache_metadata / "3.7.0-new.latest.json").write_bytes(new_manifest.read_bytes())
            (cache_metadata / "3.7.0-new.latest.json.sig").write_bytes(new_signature.read_bytes())

            class FakeReleaseClient:
                def fetch_manifest_location_candidates(self, **_kwargs):
                    return (ManifestLocations(not_modified=True),)

            with patch("app.services.update_check_service.user_cache_root", return_value=root / "cache"):
                with self.assertRaisesRegex(UpdateCheckError, "no cached manifest"):
                    check_secure_update(
                        "v3.6.17",
                        public_key_pem=public_pem,
                        release_client=FakeReleaseClient(),
                        os_name="windows",
                        arch="x64",
                        state=LocalUpdateState(),
                    )

    def test_secure_update_ignores_conflicting_legacy_cache_without_active_index(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import LocalUpdateState, ManifestLocations
        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_metadata = root / "cache" / "updates" / "metadata"
            cache_metadata.mkdir(parents=True)
            key = ECC.generate(curve="Ed25519")
            first_manifest, first_signature, public_pem = _signed_manifest(
                root / "first",
                key=key,
                overrides={"notes": "first signed generation"},
            )
            second_manifest, second_signature, _ = _signed_manifest(
                root / "second",
                key=key,
                overrides={"notes": "second signed generation"},
            )
            (cache_metadata / "3.7.0-first.latest.json").write_bytes(first_manifest.read_bytes())
            (cache_metadata / "3.7.0-first.latest.json.sig").write_bytes(first_signature.read_bytes())
            (cache_metadata / "3.7.0-second.latest.json").write_bytes(second_manifest.read_bytes())
            (cache_metadata / "3.7.0-second.latest.json.sig").write_bytes(second_signature.read_bytes())

            class FakeReleaseClient:
                def fetch_manifest_location_candidates(self, **_kwargs):
                    return (ManifestLocations(not_modified=True),)

            with patch("app.services.update_check_service.user_cache_root", return_value=root / "cache"):
                with self.assertRaisesRegex(UpdateCheckError, "no cached manifest"):
                    check_secure_update(
                        "v3.6.17",
                        public_key_pem=public_pem,
                        release_client=FakeReleaseClient(),
                        os_name="windows",
                        arch="x64",
                        state=LocalUpdateState(),
                    )

    def test_secure_update_preserves_last_good_cache_when_fresh_signature_download_fails(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import LocalUpdateState, ManifestLocations, _atomic_write_text
        from app.services.update_metadata_cache import metadata_record, write_active_generation
        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_metadata = root / "cache" / "updates" / "metadata"
            manifest_path, sig_path, public_pem = _signed_manifest(root / "source")
            cache_metadata.mkdir(parents=True)
            cached_manifest = cache_metadata / "3.7.0.latest.json"
            cached_signature = cache_metadata / "3.7.0.latest.json.sig"
            original_manifest = manifest_path.read_bytes()
            original_signature = sig_path.read_bytes()
            cached_manifest.write_bytes(original_manifest)
            cached_signature.write_bytes(original_signature)
            write_active_generation(
                cache_metadata,
                etag='"last-good"',
                last_modified="Thu, 30 Jul 2026 00:00:00 GMT",
                records=(metadata_record(cached_manifest, cached_signature),),
                atomic_write_text=_atomic_write_text,
                scope='{"authority":{"apiBase":"https://api.github.com","owner":"owner","repo":"repo"},"endpoint":"releases","pageSize":10}',
            )

            manifest_url = "https://github.com/owner/repo/releases/download/v3.7.0/latest.json"
            signature_url = f"{manifest_url}.sig"

            class FreshReleaseClient:
                api_base = "https://api.github.com"
                owner = "owner"
                repo = "repo"
                auto_check_interval_seconds = 0

                def fetch_manifest_location_candidates(self, **_kwargs):
                    return (
                        ManifestLocations(
                            manifest_url=manifest_url,
                            signature_url=signature_url,
                            tag_name="v3.7.0",
                        ),
                    )

            def failing_urlopen(request, timeout, **_kwargs):
                if request.full_url == signature_url:
                    raise HTTPError(signature_url, 503, "unavailable", None, None)
                return _FakeBytesResponse(b'{"incomplete": true}')

            with (
                patch("app.services.update_check_service.user_cache_root", return_value=root / "cache"),
                patch("app.services.update_check_service.open_trusted_url", side_effect=failing_urlopen),
            ):
                result = check_secure_update(
                    "v3.6.17",
                    public_key_pem=public_pem,
                    release_client=FreshReleaseClient(),
                    os_name="windows",
                    arch="x64",
                    state=LocalUpdateState(),
                )

            self.assertEqual(cached_manifest.read_bytes(), original_manifest)
            self.assertEqual(cached_signature.read_bytes(), original_signature)

        self.assertEqual(result.status, UPDATE_STATUS_AVAILABLE)
        self.assertEqual(result.latest_version, "3.7.0")

    def test_secure_update_uses_readonly_release_status_when_signed_metadata_is_missing_and_local_is_newer(self):
        from app.services.secure_updater import LocalUpdateState, ManifestLocations

        manifest_url = "https://github.com/owner/repo/releases/download/v3.6.14/latest.json"

        class ReleaseClient:
            def fetch_manifest_location_candidates(self, **_kwargs):
                return (
                    ManifestLocations(
                        manifest_url=manifest_url,
                        signature_url=f"{manifest_url}.sig",
                        release_url="https://github.com/owner/repo/releases/tag/v3.6.14",
                        tag_name="v3.6.14",
                    ),
                )

        with (
            patch(
                "app.services.update_check_service.open_trusted_url",
                side_effect=HTTPError(manifest_url, 404, "missing", None, None),
            ),
            patch(
                "app.services.update_check_service.fetch_latest_release_payload",
                return_value={
                    "tag_name": "v3.6.14",
                    "name": "v3.6.14",
                    "html_url": "https://github.com/owner/repo/releases/tag/v3.6.14",
                },
            ),
        ):
            result = check_secure_update(
                "3.6.17",
                public_key_pem="test-public-key",
                release_client=ReleaseClient(),
                os_name="windows",
                arch="x64",
                state=LocalUpdateState(),
            )

        self.assertEqual(result.status, UPDATE_STATUS_LOCAL_NEWER)
        self.assertEqual(result.local_version, "3.6.17")
        self.assertEqual(result.latest_version, "3.6.14")

    def test_secure_update_reports_untrusted_state_when_newer_release_lacks_signed_metadata(self):
        import app.services.update_check_service as update_service
        from app.services.secure_updater import LocalUpdateState, ManifestLocations

        manifest_url = "https://github.com/owner/repo/releases/download/v3.8.0/latest.json"

        class ReleaseClient:
            def fetch_manifest_location_candidates(self, **_kwargs):
                return (
                    ManifestLocations(
                        manifest_url=manifest_url,
                        signature_url=f"{manifest_url}.sig",
                        release_url="https://github.com/owner/repo/releases/tag/v3.8.0",
                        tag_name="v3.8.0",
                    ),
                )

        with (
            patch(
                "app.services.update_check_service.open_trusted_url",
                side_effect=HTTPError(manifest_url, 404, "missing", None, None),
            ),
            patch(
                "app.services.update_check_service.fetch_latest_release_payload",
                return_value={
                    "tag_name": "v3.8.0",
                    "name": "v3.8.0",
                    "html_url": "https://github.com/owner/repo/releases/tag/v3.8.0",
                },
            ),
        ):
            result = check_secure_update(
                "3.6.17",
                public_key_pem="test-public-key",
                release_client=ReleaseClient(),
                os_name="windows",
                arch="x64",
                state=LocalUpdateState(),
            )

        self.assertEqual(result.status, getattr(update_service, "UPDATE_STATUS_UNTRUSTED", "untrusted"))
        self.assertEqual(result.local_version, "3.6.17")
        self.assertEqual(result.latest_version, "3.8.0")
        self.assertEqual(result.manifest_path, "")
        self.assertEqual(result.signature_path, "")

    def test_secure_update_never_downgrades_invalid_signature_to_readonly_release_check(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import LocalUpdateState, ManifestLocations
        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path, signature_path, public_pem = _signed_manifest(root / "source")
            signature_path.write_bytes(b"invalid-signature")
            manifest_url = "https://github.com/owner/repo/releases/download/v3.8.0/latest.json"
            signature_url = f"{manifest_url}.sig"
            blobs = {
                manifest_url: manifest_path.read_bytes(),
                signature_url: signature_path.read_bytes(),
            }

            class ReleaseClient:
                def fetch_manifest_location_candidates(self, **_kwargs):
                    return (
                        ManifestLocations(
                            manifest_url=manifest_url,
                            signature_url=signature_url,
                            tag_name="v3.8.0",
                        ),
                    )

            def fake_urlopen(request, timeout, **_kwargs):
                return _FakeBytesResponse(blobs[request.full_url])

            with (
                patch("app.services.update_check_service.user_cache_root", return_value=root / "cache"),
                patch("app.services.update_check_service.open_trusted_url", side_effect=fake_urlopen),
                patch("app.services.update_check_service.fetch_latest_release_payload") as readonly_fetch,
            ):
                with self.assertRaisesRegex(UpdateCheckError, "signature"):
                    check_secure_update(
                        "3.6.17",
                        public_key_pem=public_pem,
                        release_client=ReleaseClient(),
                        os_name="windows",
                        arch="x64",
                        state=LocalUpdateState(),
                    )

            readonly_fetch.assert_not_called()

    def test_secure_update_accepts_verified_release_newer_than_installed_version_when_last_seen_is_stale(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import LocalUpdateState
        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path, sig_path, public_pem = _signed_manifest(root)
            state = LocalUpdateState(last_seen_version="3.8.0")

            with patch(
                "app.services.update_check_service.default_update_metadata_dir",
                return_value=root,
            ):
                result = check_secure_update(
                    "v3.6.17",
                    public_key_pem=public_pem,
                    manifest_path=manifest_path,
                    signature_path=sig_path,
                    os_name="windows",
                    arch="x64",
                    state=state,
                )

        self.assertEqual(result.status, UPDATE_STATUS_AVAILABLE)
        self.assertEqual(result.latest_version, "3.7.0")
        self.assertEqual(state.last_seen_version, "3.8.0")

    def test_secure_update_refreshes_persisted_last_seen_version_from_verified_release_head(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import LocalUpdateState
        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path, sig_path, public_pem = _signed_manifest(root)
            state = LocalUpdateState(last_seen_version="3.8.0")

            def record_seen(version, *, release_revision):
                state.last_seen_version = version
                state.last_seen_revision = release_revision
                return state

            with (
                patch(
                    "app.services.update_check_service.default_update_metadata_dir",
                    return_value=root,
                ),
                patch("app.services.update_check_service.load_local_update_state", return_value=state),
                patch(
                    "app.services.update_check_service.record_last_seen_update",
                    side_effect=record_seen,
                ) as record_seen_update,
            ):
                result = check_secure_update(
                    "v3.6.17",
                    public_key_pem=public_pem,
                    manifest_path=manifest_path,
                    signature_path=sig_path,
                    os_name="windows",
                    arch="x64",
                )

        self.assertEqual(result.status, UPDATE_STATUS_AVAILABLE)
        self.assertEqual(state.last_seen_version, "3.7.0")
        record_seen_update.assert_called_once_with("3.7.0", release_revision=0)

    def test_secure_update_rejects_manifest_requiring_newer_client(self):
        from tempfile import TemporaryDirectory

        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            manifest_path, sig_path, public_pem = _signed_manifest(
                Path(temp_dir),
                overrides={"minClientVersion": "3.6.18"},
            )

            with self.assertRaises(UpdateCheckError):
                check_secure_update(
                    "v3.6.17",
                    public_key_pem=public_pem,
                    manifest_path=manifest_path,
                    signature_path=sig_path,
                    os_name="windows",
                    arch="x64",
                )

    def test_main_window_update_flow_uses_secure_manifest_path(self):
        source = Path("app/ui/main_window.py").read_text(encoding="utf-8")
        service_source = Path("app/services/update_check_service.py").read_text(encoding="utf-8")
        launcher_source = Path("app/services/updater_helper_launcher.py").read_text(
            encoding="utf-8"
        )
        combined = source + service_source + launcher_source

        self.assertIn("check_secure_update", source)
        self.assertIn("_download_verified_update", source)
        self.assertIn("_cancel_update_download", source)
        self.assertIn("launch_prepared_update", source)
        self.assertIn('"updater_helper.exe"', service_source)
        self.assertIn('"--wait-pid"', launcher_source)
        self.assertIn('"--install-dir"', launcher_source)
        self.assertIn('"--manifest"', launcher_source)
        self.assertIn('"--signature"', launcher_source)
        self.assertNotIn('"--asset-json"', combined)
        self.assertIn('"shell": False', launcher_source)
        self.assertIn("record_skipped_update", source)
        self.assertIn("跳过此修订", source)
        self.assertNotIn("下载和安装稍后接入", source)
        self.assertNotIn("自动下载和安装流程尚未接入", source)

    def test_launch_prepared_update_uses_verified_server_side_paths(self):
        from tempfile import TemporaryDirectory

        from app.services.update_check_service import REVISION_AUTH_TOKEN_ENV
        from app.services.updater_launch_capability import (
            LAUNCH_CAPABILITY_TOKEN_ENV,
            RESTART_HANDOFF_TOKEN_ENV,
        )
        from shared.release_identity import ReleaseIdentity

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            installer = root / "update.exe"
            manifest = root / "latest.json"
            signature = root / "latest.json.sig"
            for path in (installer, manifest, signature):
                path.write_bytes(b"verified")
            app_exe = root / "CrawlerWebPortal.exe"
            helper_exe = root / "updater_helper.exe"
            app_exe.write_bytes(b"app")
            helper_exe.write_bytes(b"helper")
            resolved_helper_exe = helper_exe.resolve()
            prepared = PreparedUpdate(
                installer_path=os.fspath(installer),
                manifest_path=os.fspath(manifest),
                signature_path=os.fspath(signature),
                version="3.6.18",
                log_path=os.fspath(root / "install.log"),
                release_revision=2,
            )
            popen = Mock()
            with (
                patch("app.services.update_check_service.record_pending_install") as record_pending,
                patch(
                    "app.services.update_check_service.default_update_staging_dir",
                    return_value=root,
                ),
                patch(
                    "app.services.update_check_service.default_update_metadata_dir",
                    return_value=root,
                ),
                patch(
                    "app.services.update_check_service.load_installed_release_identity",
                    return_value=ReleaseIdentity("3.6.17", 1),
                ),
                patch.dict(
                    os.environ,
                    {
                        LAUNCH_CAPABILITY_TOKEN_ENV: "stale-launch-token",
                        REVISION_AUTH_TOKEN_ENV: "stale-revision-token",
                        RESTART_HANDOFF_TOKEN_ENV: "stale-restart-token",
                    },
                ),
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(app_exe)),
            ):
                launch_prepared_update(
                    prepared,
                    restart_argv=[str(app_exe), "--port", "8000"],
                    popen=popen,
                )

        argv = popen.call_args.args[0]
        self.assertEqual(Path(argv[0]), resolved_helper_exe)
        self.assertIn("--manifest", argv)
        self.assertIn(os.fspath(manifest), argv)
        self.assertIn("--signature", argv)
        self.assertIn(os.fspath(signature), argv)
        self.assertIn("--install-dir", argv)
        self.assertIn("--release-revision", argv)
        revision_index = argv.index("--release-revision")
        self.assertEqual(argv[revision_index + 1], "2")
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertTrue(popen.call_args.kwargs["close_fds"])
        self.assertEqual(
            popen.call_args.kwargs["creationflags"],
            getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        child_environment = popen.call_args.kwargs["env"]
        self.assertNotEqual(
            child_environment[LAUNCH_CAPABILITY_TOKEN_ENV],
            "stale-launch-token",
        )
        self.assertNotIn(REVISION_AUTH_TOKEN_ENV, child_environment)
        self.assertNotIn(RESTART_HANDOFF_TOKEN_ENV, child_environment)
        self.assertEqual(record_pending.call_args.kwargs["release_revision"], 2)

    def test_launch_prepared_update_restores_previous_state_when_popen_fails(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import PendingInstall
        from shared.release_identity import ReleaseIdentity

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            installer = root / "update.exe"
            manifest = root / "latest.json"
            signature = root / "latest.json.sig"
            for path in (installer, manifest, signature):
                path.write_bytes(b"verified")
            app_exe = root / "CrawlerWebPortal.exe"
            helper_exe = root / "updater_helper.exe"
            app_exe.write_bytes(b"app")
            helper_exe.write_bytes(b"helper")
            prepared = PreparedUpdate(
                installer_path=os.fspath(installer),
                manifest_path=os.fspath(manifest),
                signature_path=os.fspath(signature),
                version="3.6.18",
                log_path=os.fspath(root / "install.log"),
            )
            previous_pending = PendingInstall(version="3.6.17")
            primary = RuntimeError("process creation failed")

            def record_pending(**kwargs):
                kwargs["previous_pending_out"].append(previous_pending)

            with (
                patch(
                    "app.services.update_check_service.record_pending_install",
                    side_effect=record_pending,
                ),
                patch(
                    "app.services.update_check_service.rollback_pending_install"
                ) as rollback_pending,
                patch(
                    "app.services.update_check_service.default_update_staging_dir",
                    return_value=root,
                ),
                patch(
                    "app.services.update_check_service.default_update_metadata_dir",
                    return_value=root,
                ),
                patch(
                    "app.services.update_check_service.load_installed_release_identity",
                    return_value=ReleaseIdentity("3.6.17", 0),
                ),
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(app_exe)),
            ):
                with self.assertRaises(RuntimeError) as raised:
                    launch_prepared_update(
                        prepared,
                        restart_argv=[str(app_exe)],
                        popen=Mock(side_effect=primary),
                    )

        self.assertIs(raised.exception, primary)
        rollback_pending.assert_called_once()
        self.assertIs(
            rollback_pending.call_args.kwargs["restore_pending"],
            previous_pending,
        )

    def test_launch_prepared_update_rejects_install_root_replacement(self):
        from tempfile import TemporaryDirectory

        import app.services.updater_installer_lease as installer_lease_module
        from shared.release_identity import ReleaseIdentity

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            staging = root / "staging"
            metadata = root / "metadata"
            install_root = root / "install"
            outside_root = root / "outside-install"
            for directory in (staging, metadata, install_root, outside_root):
                directory.mkdir()
            installer = staging / "update.exe"
            manifest = metadata / "latest.json"
            signature = metadata / "latest.json.sig"
            for path in (installer, manifest, signature):
                path.write_bytes(b"verified")
            app_exe = install_root / "CrawlerWebPortal.exe"
            helper_exe = install_root / "updater_helper.exe"
            app_exe.write_bytes(b"app")
            helper_exe.write_bytes(b"approved helper")
            (outside_root / helper_exe.name).write_bytes(b"outside helper")
            probe = root / "directory-link-probe"
            try:
                probe.symlink_to(outside_root, target_is_directory=True)
                probe.unlink()
            except OSError as exc:
                self.skipTest(f"directory symlink creation unavailable: {exc}")
            moved_root = root / "install-before-swap"
            prepared = PreparedUpdate(
                installer_path=os.fspath(installer),
                manifest_path=os.fspath(manifest),
                signature_path=os.fspath(signature),
                version="3.6.18",
                log_path=os.fspath(staging / "install.log"),
            )
            real_require = installer_lease_module.require_regular_file

            def require_then_swap(path: Path, *, approved_root: Path) -> Path:
                result = real_require(path, approved_root=approved_root)
                install_root.rename(moved_root)
                install_root.symlink_to(outside_root, target_is_directory=True)
                return result

            popen = Mock(side_effect=AssertionError("outside helper must not launch"))
            with (
                patch.object(
                    installer_lease_module,
                    "require_regular_file",
                    side_effect=require_then_swap,
                ),
                patch(
                    "app.services.update_check_service.default_update_staging_dir",
                    return_value=staging,
                ),
                patch(
                    "app.services.update_check_service.default_update_metadata_dir",
                    return_value=metadata,
                ),
                patch(
                    "app.services.update_check_service.load_installed_release_identity",
                    return_value=ReleaseIdentity("3.6.17", 0),
                ),
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(app_exe)),
            ):
                with self.assertRaises(UpdateCheckError):
                    launch_prepared_update(
                        prepared,
                        restart_argv=[],
                        popen=popen,
                    )

            popen.assert_not_called()
            self.assertEqual(
                (outside_root / helper_exe.name).read_bytes(),
                b"outside helper",
            )

    def test_launch_prepared_update_rejects_helper_file_replacement(self):
        from tempfile import TemporaryDirectory

        import shared.filesystem_directory_capability as capability_module
        from shared.release_identity import ReleaseIdentity

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            staging = root / "staging"
            metadata = root / "metadata"
            install_root = root / "install"
            for directory in (staging, metadata, install_root):
                directory.mkdir()
            installer = staging / "update.exe"
            manifest = metadata / "latest.json"
            signature = metadata / "latest.json.sig"
            for path in (installer, manifest, signature):
                path.write_bytes(b"verified")
            app_exe = install_root / "CrawlerWebPortal.exe"
            helper_exe = install_root / "updater_helper.exe"
            replacement = install_root / "replacement-helper.exe"
            original = install_root / "approved-helper.exe"
            app_exe.write_bytes(b"app")
            helper_exe.write_bytes(b"approved helper")
            replacement.write_bytes(b"outside helper")
            prepared = PreparedUpdate(
                installer_path=os.fspath(installer),
                manifest_path=os.fspath(manifest),
                signature_path=os.fspath(signature),
                version="3.6.18",
                log_path=os.fspath(staging / "install.log"),
            )
            real_stat_child = capability_module.FilesystemDirectoryCapability.stat_child
            swapped = False

            def stat_then_replace(capability, name: str):
                nonlocal swapped
                result = real_stat_child(capability, name)
                if not swapped and name == helper_exe.name:
                    swapped = True
                    helper_exe.replace(original)
                    replacement.replace(helper_exe)
                return result

            launch_capability = metadata / ".launch-test.json"
            launch_capability.write_bytes(b"capability")

            def reject_outside_helper(argv, **_kwargs):
                self.assertEqual(Path(argv[0]).read_bytes(), b"outside helper")
                raise AssertionError("replaced helper must not launch")

            with (
                patch.object(
                    capability_module.FilesystemDirectoryCapability,
                    "stat_child",
                    new=stat_then_replace,
                ),
                patch(
                    "app.services.update_check_service.default_update_staging_dir",
                    return_value=staging,
                ),
                patch(
                    "app.services.update_check_service.default_update_metadata_dir",
                    return_value=metadata,
                ),
                patch(
                    "app.services.update_check_service.load_installed_release_identity",
                    return_value=ReleaseIdentity("3.6.17", 0),
                ),
                patch(
                    "app.services.updater_helper_launcher.issue_launch_capability",
                    return_value=(launch_capability, "launch-token"),
                ),
                patch("app.services.update_check_service.record_pending_install"),
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(app_exe)),
            ):
                with self.assertRaises(UpdateCheckError):
                    launch_prepared_update(
                        prepared,
                        restart_argv=[],
                        popen=reject_outside_helper,
                    )

            self.assertEqual(helper_exe.read_bytes(), b"outside helper")

    def test_launch_success_survives_helper_directory_lease_cleanup_failure(self):
        from tempfile import TemporaryDirectory

        import shared.filesystem_directory_capability as capability_module
        from shared.release_identity import ReleaseIdentity

        class HostileCleanup(BaseException):
            pass

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            staging = root / "staging"
            metadata = root / "metadata"
            install_root = root / "install"
            for directory in (staging, metadata, install_root):
                directory.mkdir()
            installer = staging / "update.exe"
            manifest = metadata / "latest.json"
            signature = metadata / "latest.json.sig"
            for path in (installer, manifest, signature):
                path.write_bytes(b"verified")
            app_exe = install_root / "CrawlerWebPortal.exe"
            helper_exe = install_root / "updater_helper.exe"
            app_exe.write_bytes(b"app")
            helper_exe.write_bytes(b"approved helper")
            prepared = PreparedUpdate(
                installer_path=os.fspath(installer),
                manifest_path=os.fspath(manifest),
                signature_path=os.fspath(signature),
                version="3.6.18",
                log_path=os.fspath(staging / "install.log"),
            )
            launch_capability = metadata / ".launch-test.json"
            launch_capability.write_bytes(b"capability")
            process = object()
            rollback_pending = Mock()
            cleanup_events = Mock()
            real_close = capability_module.FilesystemDirectoryCapability.close
            cleanup_armed = False

            def close_then_fail(capability) -> None:
                nonlocal cleanup_armed
                real_close(capability)
                if cleanup_armed:
                    cleanup_armed = False
                    raise HostileCleanup("directory lease cleanup failed")

            def start_process(*_args, **_kwargs):
                nonlocal cleanup_armed
                cleanup_armed = True
                return process

            with (
                patch.object(
                    capability_module.FilesystemDirectoryCapability,
                    "close",
                    new=close_then_fail,
                ),
                patch(
                    "app.services.update_check_service.default_update_staging_dir",
                    return_value=staging,
                ),
                patch(
                    "app.services.update_check_service.default_update_metadata_dir",
                    return_value=metadata,
                ),
                patch(
                    "app.services.update_check_service.load_installed_release_identity",
                    return_value=ReleaseIdentity("3.6.17", 0),
                ),
                patch(
                    "app.services.updater_helper_launcher.issue_launch_capability",
                    return_value=(launch_capability, "launch-token"),
                ),
                patch("app.services.update_check_service.record_pending_install"),
                patch(
                    "app.services.update_check_service.rollback_pending_install",
                    rollback_pending,
                ),
                patch(
                    "app.services.update_check_service.log_update_event",
                    cleanup_events,
                ),
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(app_exe)),
            ):
                result = launch_prepared_update(
                    prepared,
                    restart_argv=[],
                    popen=start_process,
                )

            self.assertIs(result, process)
            self.assertTrue(launch_capability.exists())
            rollback_pending.assert_not_called()
            cleanup_events.assert_called_once()

    def test_prepare_does_not_unlink_replaced_package_after_verification_failure(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import VerificationError
        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path, signature_path, public_pem = _signed_manifest(root)
            installer = root / "installer.exe"
            installer.write_bytes(b"hash-verified-only")
            primary = VerificationError("OS package signature failed")

            class FakeDownloader:
                def __init__(self, **_kwargs):
                    return None

                def download(self, *_args, **_kwargs):
                    return installer

            class FailingPackageVerifier:
                def __init__(self, **_kwargs):
                    return None

                def verify(self, path, *_args, **_kwargs):
                    Path(path).write_bytes(b"later installer generation")
                    raise primary

            result = UpdateCheckResult(
                status=UPDATE_STATUS_AVAILABLE,
                local_version="3.6.17",
                latest_version="3.7.0",
                tag_name="v3.7.0",
                release_name="v3.7.0",
                html_url="https://github.com/owner/repo/releases/tag/v3.7.0",
                manifest_path=os.fspath(manifest_path),
                signature_path=os.fspath(signature_path),
            )

            with patch(
                "app.services.update_check_service.default_update_staging_dir",
                return_value=root,
            ):
                with self.assertRaises(VerificationError) as raised:
                    prepare_verified_update(
                        result,
                        public_key_pem=public_pem,
                        require_os_signature=True,
                        downloader_cls=FakeDownloader,
                        package_verifier_cls=FailingPackageVerifier,
                    )

            self.assertIs(raised.exception, primary)
            self.assertEqual(installer.read_bytes(), b"later installer generation")

    def test_launch_revision_change_keeps_one_time_token_out_of_helper_argv(self):
        from tempfile import TemporaryDirectory

        from app.services.update_check_service import REVISION_AUTH_TOKEN_ENV
        from shared.release_identity import ReleaseIdentity

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            installer = root / "update.exe"
            manifest = root / "latest.json"
            signature = root / "latest.json.sig"
            authorization = root / ".ucrawl-revision-auth-test.json"
            for path in (installer, manifest, signature, authorization):
                path.write_bytes(b"verified")
            app_exe = root / "CrawlerWebPortal.exe"
            helper_exe = root / "updater_helper.exe"
            app_exe.write_bytes(b"app")
            helper_exe.write_bytes(b"helper")
            token = "one-time-local-token"
            prepared = PreparedUpdate(
                installer_path=os.fspath(installer),
                manifest_path=os.fspath(manifest),
                signature_path=os.fspath(signature),
                version="3.6.18",
                log_path=os.fspath(root / "install.log"),
                release_revision=1,
                revision_authorization_path=os.fspath(authorization),
                revision_authorization_token=token,
            )
            popen = Mock()
            with (
                patch("app.services.update_check_service.record_pending_install"),
                patch(
                    "app.services.update_check_service.default_update_staging_dir",
                    return_value=root,
                ),
                patch(
                    "app.services.update_check_service.default_update_metadata_dir",
                    return_value=root,
                ),
                patch(
                    "app.services.update_check_service.load_installed_release_identity",
                    return_value=ReleaseIdentity("3.6.17", 0),
                ),
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(app_exe)),
            ):
                launch_prepared_update(
                    prepared,
                    restart_argv=[str(app_exe)],
                    popen=popen,
                )

        argv = popen.call_args.args[0]
        self.assertIn("--revision-authorization", argv)
        self.assertIn(os.fspath(authorization), argv)
        self.assertNotIn(token, argv)
        self.assertEqual(popen.call_args.kwargs["env"][REVISION_AUTH_TOKEN_ENV], token)

    def test_update_ui_describes_only_mandatory_verification_layers(self):
        dialog_source = Path("app/ui/dialogs/update_check.py").read_text(encoding="utf-8")
        main_source = Path("app/ui/main_window.py").read_text(encoding="utf-8")
        combined = dialog_source + main_source

        self.assertIn("更新清单签名、大小和 SHA-256 校验", combined)
        self.assertNotIn("系统签名校验", combined)

    def test_update_download_rejects_manifest_version_changed_after_selection(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import VerificationError
        from app.services.update_check_service import UpdateCheckResult
        from app.ui.main_window import MainWindow
        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            manifest_path, signature_path, public_pem = _signed_manifest(Path(temp_dir))
            result = UpdateCheckResult(
                status=UPDATE_STATUS_AVAILABLE,
                local_version="3.6.17",
                latest_version="3.8.0",
                tag_name="v3.8.0",
                release_name="v3.8.0",
                html_url="https://github.com/owner/repo/releases/tag/v3.8.0",
                manifest_path=str(manifest_path),
                signature_path=str(signature_path),
            )

            with (
                patch("app.ui.main_window.UPDATE_PUBLIC_KEY_PEM", public_pem),
                patch(
                    "app.ui.main_window.Downloader",
                    side_effect=AssertionError("download must not start for a changed manifest"),
                ),
            ):
                with self.assertRaisesRegex(VerificationError, "selected update version"):
                    MainWindow._download_verified_update(result)

    def test_update_download_rechecks_minimum_client_version_before_network(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import VerificationError
        from app.services.update_check_service import UpdateCheckResult
        from app.ui.main_window import MainWindow
        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            manifest_path, signature_path, public_pem = _signed_manifest(
                Path(temp_dir),
                overrides={"minClientVersion": "3.8.0"},
            )
            result = UpdateCheckResult(
                status=UPDATE_STATUS_AVAILABLE,
                local_version="3.6.17",
                latest_version="3.7.0",
                tag_name="v3.7.0",
                release_name="v3.7.0",
                html_url="https://github.com/owner/repo/releases/tag/v3.7.0",
                manifest_path=str(manifest_path),
                signature_path=str(signature_path),
            )

            with (
                patch("app.ui.main_window.UPDATE_PUBLIC_KEY_PEM", public_pem),
                patch(
                    "app.ui.main_window.Downloader",
                    side_effect=AssertionError("download must not start for an incompatible client"),
                ),
            ):
                with self.assertRaisesRegex(VerificationError, "minimum client version"):
                    MainWindow._download_verified_update(result)

    def test_prepare_reinstall_requires_confirmation_and_creates_one_time_authorization(self):
        from tempfile import TemporaryDirectory

        from app.services.secure_updater import VerificationError
        from app.services.update_check_service import prepare_verified_update
        from tests.release.updater.test_secure_updater import _signed_manifest

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_path, signature_path, public_pem = _signed_manifest(
                root,
                overrides={"releaseRevision": 2, "tag": "v3.7.0-r2"},
            )
            installer = root / "installer.exe"
            result = UpdateCheckResult(
                status=UPDATE_STATUS_AVAILABLE,
                local_version="3.7.0",
                local_revision=2,
                latest_version="3.7.0",
                latest_revision=2,
                tag_name="v3.7.0-r2",
                release_name="v3.7.0-r2",
                html_url="https://github.com/owner/repo/releases/tag/v3.7.0-r2",
                manifest_path=str(manifest_path),
                signature_path=str(signature_path),
            )

            class FakeDownloader:
                def __init__(self, **_kwargs):
                    pass

                def download(self, *_args, **_kwargs):
                    installer.write_bytes(b"verified installer")
                    return installer

            class FakePackageVerifier:
                def __init__(self, **_kwargs):
                    pass

                def verify(self, *_args, **_kwargs):
                    return None

            with (
                patch(
                    "app.services.update_check_service.default_update_staging_dir",
                    return_value=root,
                ),
                patch(
                    "app.services.update_check_service.default_update_metadata_dir",
                    return_value=root,
                ),
            ):
                with self.assertRaisesRegex(
                    VerificationError,
                    "explicit local authorization",
                ):
                    prepare_verified_update(
                        result,
                        public_key_pem=public_pem,
                        downloader_cls=FakeDownloader,
                        package_verifier_cls=FakePackageVerifier,
                    )

                prepared = prepare_verified_update(
                    result,
                    public_key_pem=public_pem,
                    downloader_cls=FakeDownloader,
                    package_verifier_cls=FakePackageVerifier,
                    allow_downgrade=True,
                )

            authorization_path = Path(prepared.revision_authorization_path)
            self.assertTrue(authorization_path.is_file())
            self.assertTrue(prepared.revision_authorization_token)
            self.assertNotIn(
                prepared.revision_authorization_token,
                authorization_path.read_text(encoding="utf-8"),
            )
            authorization_path.unlink()

    def test_main_window_startup_health_passes_update_staging_directory(self):
        from types import SimpleNamespace

        from app.ui.main_window import MainWindow

        staging_dir = Path("C:/ucrawl-test/update-staging")
        host = SimpleNamespace(
            _current_status_version=lambda: "v3.7.0",
            _display_version=lambda version: version,
        )
        with (
            patch("app.ui.main_window.default_update_staging_dir", return_value=staging_dir, create=True),
            patch("app.ui.main_window.record_startup_update_health") as record_health,
        ):
            MainWindow._record_update_startup_health(host)

        record_health.assert_called_once_with(
            current_version="3.7.0",
            current_revision=0,
            staging_dir=staging_dir,
        )

    def test_update_download_dialog_exposes_cancel_error_retry_and_install_controls(self):
        source = Path("app/ui/dialogs/update_check.py").read_text(encoding="utf-8")

        self.assertIn("class UpdateDownloadDialog", source)
        for text in ("取消下载", "正在取消", "重试", "查看日志", "安装并重启"):
            self.assertIn(text, source)
        self.assertIn("def set_cancelling", source)

    def test_cancel_update_download_enters_non_retryable_cancelling_state(self):
        from app.ui.main_window import MainWindow

        events: list[str] = []

        class CancelEvent:
            def set(self):
                events.append("cancel-event")

        class Dialog:
            def set_cancelling(self):
                events.append("cancelling")

            def set_cancelled(self):
                events.append("cancelled")

        host = type("Host", (), {"append_log": lambda self, _message: None})()
        host.__dict__["_update_download_cancel_event"] = CancelEvent()
        host.__dict__["_update_download_dialog"] = Dialog()

        MainWindow._cancel_update_download(host)

        self.assertEqual(events, ["cancel-event", "cancelling"])

    def test_retry_update_download_waits_until_previous_worker_has_stopped(self):
        from app.services.update_check_service import UpdateCheckResult
        from app.ui.main_window import MainWindow

        events: list[str] = []

        class RunningThread:
            @staticmethod
            def is_alive():
                return True

        host = type(
            "Host",
            (),
            {
                "append_log": lambda self, _message: events.append("log"),
                "_begin_update_download": lambda self, _result: events.append("begin"),
                "_show_update_check_error": lambda self, _message: events.append("error"),
            },
        )()
        host.__dict__["_last_update_result"] = UpdateCheckResult(
            status=UPDATE_STATUS_AVAILABLE,
            local_version="3.6.17",
            latest_version="3.7.0",
            tag_name="v3.7.0",
            release_name="3.7.0",
            html_url="https://example.test/release",
        )
        host.__dict__["_update_download_thread"] = RunningThread()

        MainWindow._retry_update_download(host)

        self.assertEqual(events, ["log"])

    def test_untrusted_update_state_renders_known_versions_without_download_action(self):
        from app.services.update_check_service import UpdateCheckResult
        from app.ui.main_window import MainWindow

        calls: list[tuple] = []
        host = type(
            "Host",
            (),
            {
                "_display_version": staticmethod(lambda version: f"v{str(version).lstrip('vV')}"),
                "_tr": lambda self, text: text,
                "_show_basic_message": lambda self, *args, **kwargs: calls.append(("basic", args, kwargs)),
                "_show_update_available_message": lambda self, *_args: calls.append(("download", (), {})),
                "_show_update_check_error": lambda self, message: calls.append(("generic-error", (message,), {})),
            },
        )()
        result = UpdateCheckResult(
            status="untrusted",
            local_version="3.6.17",
            latest_version="3.8.0",
            tag_name="v3.8.0",
            release_name="Release 3.8.0",
            html_url="https://github.com/owner/repo/releases/tag/v3.8.0",
        )

        MainWindow._show_update_check_result(host, result)

        self.assertEqual(len(calls), 1)
        kind, _args, kwargs = calls[0]
        self.assertEqual(kind, "basic")
        self.assertEqual(kwargs["status"], "error")
        self.assertEqual(kwargs["local_version"], "v3.6.17")
        self.assertEqual(kwargs["latest_version"], "v3.8.0")

    def test_main_window_dialog_defaults_to_the_service_selected_candidate(self):
        from app.ui.dialogs.update_check import UpdateCheckDialog as RealUpdateCheckDialog
        from app.ui.main_window import MainWindow

        qt_app = _qt_app()
        captured: dict[str, object] = {}

        class CapturingDialog(RealUpdateCheckDialog):
            SKIP_CODE = RealUpdateCheckDialog.SKIP_CODE

            def __init__(self, _parent, **kwargs) -> None:
                super().__init__(None, **kwargs)
                captured["selected_candidate_id"] = self.selected_update_candidate_id()

            def exec(self) -> int:
                self.deleteLater()
                return 0

        host = type(
            "Host",
            (),
            {
                "_tr": lambda self, text: text,
                "_current_ui_language": lambda self: "en-US",
            },
        )()
        result = UpdateCheckResult(
            status=UPDATE_STATUS_AVAILABLE,
            local_version="3.6.17",
            latest_version="3.7.0",
            latest_revision=1,
            tag_name="v3.7.0-r1",
            release_name="Release 3.7.0 revision 1",
            html_url="https://example.test/releases/v3.7.0-r1",
            candidates=(
                UpdateCandidate(
                    version="3.7.0",
                    release_revision=2,
                    tag_name="v3.7.0-r2",
                    release_name="Release 3.7.0 revision 2",
                    html_url="https://example.test/releases/v3.7.0-r2",
                ),
                UpdateCandidate(
                    version="3.7.0",
                    release_revision=1,
                    tag_name="v3.7.0-r1",
                    release_name="Release 3.7.0 revision 1",
                    html_url="https://example.test/releases/v3.7.0-r1",
                ),
            ),
        )

        with patch("app.ui.main_window.UpdateCheckDialog", CapturingDialog):
            MainWindow._show_update_available_message(
                host,
                result.local_identity.tag,
                result.selected_identity.tag,
                result,
            )

        self.assertEqual(captured["selected_candidate_id"], "v3.7.0-r1")
        self.assertIsNotNone(qt_app)

    def test_generic_update_error_preserves_known_local_version(self):
        from app.ui.main_window import MainWindow

        calls: list[dict] = []
        host = type(
            "Host",
            (),
            {
                "_display_version": staticmethod(lambda version: f"v{str(version).lstrip('vV')}"),
                "_show_basic_message": lambda self, *_args, **kwargs: calls.append(kwargs),
            },
        )()
        host.__dict__["_last_update_check_local_version"] = "3.6.17"

        MainWindow._show_update_check_error(host, "network unavailable")

        self.assertEqual(calls[0]["local_version"], "v3.6.17")
        self.assertEqual(calls[0]["latest_version"], "")


@unittest.skipUnless(_pyqt6_available(), "PyQt6 is not installed")
class StatusBarUpdateCheckInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _qt_app()

    def test_version_button_emits_current_version_text(self):
        from app.ui.layout.status_bar import StatusBarWidget

        widget = StatusBarWidget()
        self.addCleanup(widget.deleteLater)
        observed: list[str] = []
        widget.update_check_requested.connect(observed.append)

        widget.render({"version": "v9.9.9"})
        widget.lbl_version.click()

        self.assertEqual(observed, ["v9.9.9"])

    def test_version_button_is_disabled_while_checking(self):
        from app.ui.layout.status_bar import StatusBarWidget

        widget = StatusBarWidget()
        self.addCleanup(widget.deleteLater)

        widget.set_update_checking(True)
        self.assertFalse(widget.lbl_version.isEnabled())
        self.assertTrue(widget.lbl_version.toolTip())
        widget.set_language("en-US")
        self.assertEqual(widget.lbl_version.toolTip(), "Checking for updates...")

        widget.set_update_checking(False)
        self.assertTrue(widget.lbl_version.isEnabled())
        self.assertEqual(widget.lbl_version.toolTip(), "Check for updates")

    def test_update_check_dialog_uses_scoped_theme_styles(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QLabel, QPushButton, QFrame, QTextBrowser

        from app.ui.dialogs.chromed_dialog import ChromedDialog
        from app.ui.dialogs.update_check import UpdateCheckDialog, UpdateStatusIcon
        from app.ui.layout.window_chrome_controller import FramelessWindowChromeController
        from app.ui.styles import theme_colors

        dialog = UpdateCheckDialog(
            None,
            title="检查更新",
            message="当前版本已经是最新版本。",
            details="当前版本：v3.6.17",
            status=UPDATE_STATUS_CURRENT,
            local_version="v3.6.17",
            latest_version="v3.6.17",
            release_url="https://example.test/release",
        )
        self.addCleanup(dialog.deleteLater)

        colors = theme_colors(dialog._is_dark)
        self.assertIsInstance(dialog, ChromedDialog)
        self.assertIs(dialog.window_title_bar, dialog.chrome_frame.title_bar)
        self.assertIsInstance(dialog._window_chrome_controller, FramelessWindowChromeController)
        self.assertFalse(dialog.window_title_bar.btn_minimize.isHidden())
        self.assertFalse(dialog.window_title_bar.btn_maximize.isHidden())
        self.assertFalse(dialog.window_title_bar.btn_close.isHidden())
        self.assertTrue(dialog._window_chrome_controller.minimizable)
        self.assertTrue(dialog._window_chrome_controller.maximizable)
        self.assertTrue(bool(dialog.windowFlags() & Qt.WindowType.FramelessWindowHint))
        self.assertIn(colors["bg"], dialog.styleSheet())
        self.assertIn(colors["text"], dialog.styleSheet())
        self.assertIsNotNone(dialog.findChild(QLabel, "DialogTitle"))
        self.assertIsNotNone(dialog.findChild(QLabel, "DialogBody"))
        self.assertIsNotNone(dialog.findChild(QTextBrowser, "UpdateMarkdownView"))
        self.assertIsNotNone(dialog.findChild(UpdateStatusIcon, "UpdateStatusIcon"))
        self.assertIsNotNone(dialog.findChild(QLabel, "UpdateStatusBadge"))
        self.assertIsNotNone(dialog.findChild(QFrame, "UpdateVersionPanel"))
        self.assertIsNotNone(dialog.findChild(QLabel, "UpdateReleaseLink"))
        self.assertIsNotNone(dialog.findChild(QPushButton, "DialogPrimaryButton"))

    def test_update_check_dialog_renders_release_notes_as_markdown(self):
        from PyQt6.QtWidgets import QTextBrowser

        from app.ui.dialogs.update_check import UpdateCheckDialog

        dialog = UpdateCheckDialog(
            None,
            title="Update available",
            message="Install the selected version?",
            details=(
                "# Release notes\n\n"
                "**Important fix**\n\n"
                "| Item | Status |\n"
                "| --- | --- |\n"
                "| Hot update | Complete |"
            ),
            status=UPDATE_STATUS_AVAILABLE,
            local_version="v3.6.18",
            latest_version="v3.6.21",
        )
        self.addCleanup(dialog.deleteLater)

        browser = dialog.findChild(QTextBrowser, "UpdateMarkdownView")
        self.assertIsNotNone(browser)
        self.assertNotIn("**Important fix**", browser.toPlainText())
        self.assertIn("Important fix", browser.toPlainText())
        self.assertIn("<h1", browser.toHtml().lower())
        self.assertIn("<table", browser.toHtml().lower())

    def test_update_markdown_links_are_activated_only_through_the_project_https_boundary(self):
        from tempfile import TemporaryDirectory

        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QImage, QTextDocument
        from PyQt6.QtWidgets import QTextBrowser

        from app.ui.dialogs.update_check import UpdateCheckDialog

        dialog = UpdateCheckDialog(
            None,
            title="Update available",
            message="Review the release notes",
            details="[Release notes](https://github.com/haohaizi554/UniversalCrawler/issues)",
            status=UPDATE_STATUS_AVAILABLE,
            local_version="v3.6.18",
            latest_version="v3.6.21",
            release_url="https://github.com/haohaizi554/UniversalCrawler/releases/tag/v3.6.21",
        )
        self.addCleanup(dialog.deleteLater)

        browser = dialog.findChild(QTextBrowser, "UpdateMarkdownView")
        self.assertIsNotNone(browser)
        self.assertFalse(browser.openExternalLinks())
        self.assertFalse(browser.openLinks())
        self.assertIsNotNone(dialog.release_link)
        self.assertFalse(dialog.release_link.openExternalLinks())

        with TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "local.png"
            image = QImage(1, 1, QImage.Format.Format_ARGB32)
            self.assertTrue(image.save(str(image_path)))
            resource = browser.loadResource(
                QTextDocument.ResourceType.ImageResource,
                QUrl.fromLocalFile(str(image_path)),
            )
        self.assertFalse(getattr(resource, "isValid", lambda: True)())

        denied_urls = (
            "file:///C:/Windows/win.ini",
            "qrc:/internal/resource",
            "http://github.com/haohaizi554/UniversalCrawler/issues",
            "https://user@github.com/haohaizi554/UniversalCrawler/issues",
            "https://@github.com/haohaizi554/UniversalCrawler/issues",
            "https://:@github.com/haohaizi554/UniversalCrawler/issues",
            "https://github.com:444/haohaizi554/UniversalCrawler/issues",
            "https://attacker.example/phish",
            "https://github.com/haohaizi554/UniversalCrawler.evil/issues",
            "https://github.com/%2568aohaizi554/UniversalCrawler/issues",
            "https://github.com/haohaizi554/%2575niversalCrawler/issues",
            "https://github.com/%252568aohaizi554/UniversalCrawler/issues",
            "https://github.com/haohaizi554/UniversalCrawler/../evil",
            "https://github.com/haohaizi554/UniversalCrawler/./evil",
            "https://github.com/haohaizi554/UniversalCrawler/%2e%2e/evil",
            "https://github.com/haohaizi554/UniversalCrawler/%252e%252e/evil",
            "https://github.com/haohaizi554/UniversalCrawler\\evil",
            "https://github.com/haohaizi554/UniversalCrawler/%5c/evil",
            "https://github.com/haohaizi554/UniversalCrawler/%255c/evil",
            "https://github.com/haohaizi554/UniversalCrawler/%2f/evil",
            "https://github.com/haohaizi554/UniversalCrawler/%252f/evil",
            "https://github.com/haohaizi554/UniversalCrawler/%3f/evil",
            "https://github.com/haohaizi554/UniversalCrawler/%23/evil",
            "https://github.com/haohaizi554/UniversalCrawler/%00evil",
            "https://github.com/haohaizi554/UniversalCrawler/%250Aevil",
            "https://github.com/haohaizi554/UniversalCrawler/%E2%80%8B",
            "https://github.com/haohaizi554/UniversalCrawler/%C0%AF",
            "https://github.com/haohaizi554/Univer\u017falCrawler/issues",
        )
        allowed_urls = (
            "https://github.com/haohaizi554/UniversalCrawler/issues",
            "https://GITHUB.COM:443/HAOHAIZI554/UNIVERSALCRAWLER/releases/tag/v3.6.21",
        )

        with (
            patch("app.ui.dialogs.update_check.QDesktopServices.openUrl") as open_url,
            patch(
                "app.ui.dialogs.update_check._ReleaseNotesBrowser.scrollToAnchor"
            ) as scroll_to_anchor,
        ):
            for raw_url in denied_urls:
                with self.subTest(denied_url=raw_url):
                    browser.anchorClicked.emit(QUrl(raw_url))
            browser.anchorClicked.emit(QUrl("#security-fixes"))
            for raw_url in allowed_urls:
                with self.subTest(allowed_url=raw_url):
                    browser.anchorClicked.emit(QUrl(raw_url))

        scroll_to_anchor.assert_called_once_with("security-fixes")
        self.assertEqual(
            [call.args[0].toString() for call in open_url.call_args_list],
            [QUrl(raw_url).toString() for raw_url in allowed_urls],
        )

    def test_update_check_dialog_exposes_indeterminate_loading_state(self):
        from PyQt6.QtWidgets import QFrame, QProgressBar, QSizePolicy

        from app.ui.dialogs.update_check import UpdateCheckDialog

        dialog = UpdateCheckDialog(
            None,
            title="检查更新",
            message="正在检查更新，请稍候。",
            primary_text="正在检查更新...",
            status="checking",
            local_version="v3.6.17",
        )
        self.addCleanup(dialog.deleteLater)

        progress = dialog.findChild(QProgressBar, "UpdateCheckBusyBar")
        self.assertIsNotNone(progress)
        self.assertEqual(progress.minimum(), 0)
        self.assertEqual(progress.maximum(), 0)
        self.assertFalse(dialog.primary_button.isEnabled())
        self.assertLessEqual(dialog.minimumHeight(), 420)
        self.assertLessEqual(dialog.height(), 480)
        self.assertEqual(dialog.remote_version_value.text(), "检测中…")
        version_panel = dialog.findChild(QFrame, "UpdateVersionPanel")
        self.assertIsNotNone(version_panel)
        self.assertEqual(version_panel.sizePolicy().verticalPolicy(), QSizePolicy.Policy.Fixed)
        hero = dialog.findChild(QFrame, "UpdateHero")
        self.assertIsNotNone(hero)
        self.assertEqual(hero.sizePolicy().verticalPolicy(), QSizePolicy.Policy.Fixed)
        self.assertNotEqual(dialog.remote_version_value.text(), "-")

    def test_update_check_dialog_uses_current_language(self):
        from PyQt6.QtWidgets import QLabel, QPushButton, QTextBrowser

        from app.ui.dialogs.update_check import UpdateCheckDialog

        dialog = UpdateCheckDialog(
            None,
            title="检查更新失败",
            message="暂时无法检查最新版本。",
            details="本地版本与 GitHub 最新 Release 一致，无需更新。",
            primary_text="确定",
            status="error",
            local_version="v3.6.17",
            latest_version="v3.6.17",
            language="en-US",
        )
        self.addCleanup(dialog.deleteLater)

        details_browser = dialog.findChild(QTextBrowser, "UpdateMarkdownView")
        self.assertIsNotNone(details_browser)
        labels = "\n".join(label.text() for label in dialog.findChildren(QLabel)) + "\n" + details_browser.toPlainText()
        buttons = "\n".join(button.text() for button in dialog.findChildren(QPushButton))

        self.assertEqual(dialog.windowTitle(), "Update check failed")
        self.assertIn("Update check failed", labels)
        self.assertIn("Could not check the latest version right now.", labels)
        self.assertIn("Current version", labels)
        self.assertIn("Release version", labels)
        self.assertIn("Check failed", labels)
        self.assertIn("Error details", labels)
        self.assertIn("The local version matches the latest GitHub Release. No update is needed.", labels)
        self.assertIn("OK", buttons)
        for unexpected in ("检查更新失败", "暂时无法", "当前版本", "错误详情", "确定"):
            self.assertNotIn(unexpected, labels + buttons)

    def test_update_check_dialog_translates_local_newer_detail_with_prefix(self):
        from PyQt6.QtWidgets import QTextBrowser

        from app.ui.dialogs.update_check import UpdateCheckDialog

        dialog = UpdateCheckDialog(
            None,
            title="\u68c0\u67e5\u66f4\u65b0",
            message="\u5f53\u524d\u7248\u672c v3.6.17 \u9ad8\u4e8e\u6700\u65b0 Release v3.6.14\u3002",
            details="\u8fd9\u901a\u5e38\u8868\u793a\u4f60\u6b63\u5728\u4f7f\u7528\u672c\u5730\u6784\u5efa\u6216\u9884\u53d1\u5e03\u6784\u5efa\uff0c\u65e0\u9700\u66f4\u65b0\u3002",
            status=UPDATE_STATUS_LOCAL_NEWER,
            local_version="v3.6.17",
            latest_version="v3.6.14",
            language="en-US",
        )
        self.addCleanup(dialog.deleteLater)

        details_browser = dialog.findChild(QTextBrowser, "UpdateMarkdownView")
        self.assertIsNotNone(details_browser)

        self.assertIn(
            "This usually means you are using a local or pre-release build, so no update is needed.",
            details_browser.toPlainText(),
        )
        self.assertNotIn("\u8fd9\u901a\u5e38\u8868\u793a", details_browser.toPlainText())

    def test_update_check_dialog_exposes_selector_for_multiple_update_candidates(self):
        from app.ui.components.combo_popup import ThemedComboBox

        from app.services.update_check_service import UpdateCandidate
        from app.ui.dialogs.update_check import UpdateCheckDialog

        dialog = UpdateCheckDialog(
            None,
            title="\u68c0\u6d4b\u5230\u65b0\u7248\u672c",
            message="\u68c0\u6d4b\u5230\u591a\u4e2a\u53ef\u66f4\u65b0\u7248\u672c\u3002",
            details="\u8bf7\u9009\u62e9\u8981\u5b89\u88c5\u7684\u7248\u672c\u3002",
            status=UPDATE_STATUS_AVAILABLE,
            local_version="v3.6.17",
            latest_version="v3.8.0",
            release_url="https://example.test/releases/v3.8.0",
            language="en-US",
            candidates=(
                UpdateCandidate(
                    version="3.8.0",
                    release_revision=2,
                    tag_name="v3.8.0-r2",
                    release_name="Release 3.8.0 revision 2",
                    html_url="https://example.test/releases/v3.8.0-r2",
                    notes="notes 3.8.0 r2",
                    asset_name="setup-3.8.0-r2.exe",
                ),
                UpdateCandidate(
                    version="3.8.0",
                    release_revision=1,
                    tag_name="v3.8.0-r1",
                    release_name="Release 3.8.0 revision 1",
                    html_url="https://example.test/releases/v3.8.0-r1",
                    notes="notes 3.8.0 r1",
                    asset_name="setup-3.8.0-r1.exe",
                ),
            ),
        )
        self.addCleanup(dialog.deleteLater)

        combo = dialog.findChild(ThemedComboBox, "UpdateVersionCombo")
        self.assertIsNotNone(combo)
        self.assertEqual(combo.count(), 2)

        combo.setCurrentIndex(1)
        details_text = dialog.details_browser.toPlainText()

        self.assertEqual(dialog.selected_update_version(), "3.8.0")
        self.assertEqual(dialog.selected_update_candidate_id(), "v3.8.0-r1")
        self.assertIn("Revision 1", combo.currentText())
        self.assertIn("notes 3.8.0 r1", details_text)

    def test_update_check_dialog_default_follows_service_selected_identity(self):
        from app.services.update_check_service import UpdateCandidate
        from app.ui.dialogs.update_check import UpdateCheckDialog

        selected_url = "https://github.com/haohaizi554/UniversalCrawler/releases/tag/v3.7.0"
        dialog = UpdateCheckDialog(
            None,
            title="Update available",
            message="Select an update",
            status=UPDATE_STATUS_AVAILABLE,
            local_version="v3.6.17",
            latest_version="v3.7.0-r1",
            release_url=selected_url,
            language="en-US",
            candidates=(
                UpdateCandidate(
                    version="3.7.0",
                    release_revision=2,
                    tag_name="v3.7.0-r2",
                    release_name="Release 3.7.0 revision 2",
                    html_url=selected_url,
                ),
                UpdateCandidate(
                    version="3.7.0",
                    release_revision=1,
                    tag_name="v3.7.0-r1",
                    release_name="Release 3.7.0 revision 1",
                    html_url=selected_url,
                ),
            ),
        )
        self.addCleanup(dialog.deleteLater)

        self.assertEqual(dialog.selected_update_candidate_id(), "v3.7.0-r1")
        self.assertEqual(dialog.selected_update_version(), "3.7.0")
