"""桌面端安全更新基础能力。

更新发现、安装包验证和安装器启动均与 GUI 隔离。模块不保存 GitHub 凭据；
下载产物只有通过 Ed25519 签名 manifest 约束及配置的平台签名策略后才可执行。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from typing import Any, Callable, Mapping

# ``Crypto`` 来自仍在维护的 PyCryptodome，而不是已停止维护的 PyCrypto。
from Crypto.PublicKey import ECC  # nosec B413
from Crypto.Signature import eddsa  # nosec B413
from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

from app.debug_logger import debug_logger
from app.services.github_release_pages import (
    GitHubReleasePageError,
    collect_release_candidates,
)
from app.services.updater_atomic_io import atomic_write_text
from app.services.updater_cleanup import run_typed_cleanup_actions
from app.services.updater_downloader import (
    DEFAULT_DOWNLOAD_TIMEOUT_SECONDS as _DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    DEFAULT_MAX_DOWNLOAD_BYTES as _DEFAULT_MAX_DOWNLOAD_BYTES,
    DEFAULT_MAX_UPDATER_REDIRECT_BYTES as _DEFAULT_MAX_UPDATER_REDIRECT_BYTES,
    DownloadError as _DownloadError,
    Downloader as _Downloader,
)
from app.services.updater_manifest_json import (
    StrictManifestJsonError,
    load_strict_manifest_json,
)
from app.services.updater_installer_runner import (
    InstallerRunnerCore,
    build_installer_argv,
)
from app.services.updater_package_verification import PlatformPackageVerifier
from app.services.updater_path_safety import (
    open_regular_binary,
    read_bounded_regular_bytes,
)
from app.services.updater_release_cache import load_release_cache, save_release_cache
from app.services.updater_state_model import LocalUpdateState, PendingInstall
from app.services.updater_state_transactions import mutate_update_state
from app.services.updater_transport import open_pinned_updater_url
from app.utils.runtime_paths import user_cache_root, user_logs_root
from shared.network.pinned_transport import PinnedTransport, canonicalize_host
from shared.runtime_options import DomainPolicyViolation
from shared.release_identity import (  # noqa: F401 - 兼容导出
    MAX_RELEASE_REVISION,
    ReleaseIdentity,
    SemVer,
    compare_semver,
)

APP_ID = "ucrawl.universalcrawlerpro"
DEFAULT_CHANNEL = "stable"
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = _DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
DEFAULT_MAX_DOWNLOAD_BYTES = _DEFAULT_MAX_DOWNLOAD_BYTES
DEFAULT_MAX_UPDATER_REDIRECT_BYTES = _DEFAULT_MAX_UPDATER_REDIRECT_BYTES
DEFAULT_MANIFEST_NAME = "latest.json"
DEFAULT_SIGNATURE_NAME = "latest.json.sig"
DEFAULT_ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
DEFAULT_MAX_RELEASE_FEED_BYTES = 2_000_000
DEFAULT_MAX_RELEASE_API_BYTES = 2_000_000
DEFAULT_INSTALL_ATTEMPT_LIMIT = 2
MAX_MANIFEST_FUTURE_SKEW = timedelta(minutes=5)
MAX_MANIFEST_VALIDITY = timedelta(days=30)
MAX_SIGNED_MANIFEST_BYTES = 2_000_000
ED25519_SIGNATURE_BYTES = 64
UrlOpener = Callable[..., Any]


class TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """在 urllib 跟随 30x 之前验证目标，阻断更新链路的重定向 SSRF。"""

    def __init__(
        self,
        allowed_hosts: set[str] | frozenset[str],
        *,
        development_mode: bool = False,
    ) -> None:
        super().__init__()
        self.allowed_hosts = frozenset(str(host).lower() for host in allowed_hosts)
        self.development_mode = bool(development_mode)

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        validate_asset_url(newurl, self.allowed_hosts, development_mode=self.development_mode)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_trusted_url(
    request: urllib.request.Request,
    *,
    timeout: float,
    allowed_hosts: set[str] | frozenset[str],
    development_mode: bool = False,
    max_response_bytes: int = DEFAULT_MAX_RELEASE_API_BYTES,
) -> Any:
    """打开受信 URL，并把同一策略应用到每一次 HTTP 重定向。"""

    validate_asset_url(request.full_url, allowed_hosts, development_mode=development_mode)
    if development_mode:
        opener = urllib.request.build_opener(
            TrustedRedirectHandler(allowed_hosts, development_mode=True)
        )
        return opener.open(request, timeout=timeout)
    return open_pinned_updater_url(
        request,
        timeout=timeout,
        max_response_bytes=max_response_bytes,
        allowed_hosts=allowed_hosts,
        validate_url=validate_asset_url,
        transport_factory=PinnedTransport,
    )


def _response_final_url(response: Any, fallback: str) -> str:
    getter = getattr(response, "geturl", None)
    return str(getter() if callable(getter) else fallback)


def _response_close_action(response: Any) -> Callable[[], object]:
    def close() -> object:
        candidate = getattr(response, "close", None)
        if callable(candidate):
            return candidate()
        return None

    return close


def _atomic_write_text(path: Path, content: str) -> None:
    """在目标目录写入并 fsync 临时文件，再原子替换，避免暴露半写 JSON。"""
    atomic_write_text(path, content)
DEFAULT_UPDATE_STATE_NAME = "state.json"
DEFAULT_AUTO_CHECK_INTERVAL_SECONDS = 6 * 60 * 60


class ManifestError(RuntimeError):
    """签名 manifest 缺失、无效或违反安全约束。"""


DownloadError = _DownloadError


class MetadataNotFoundError(DownloadError):
    """Release 未发布 latest.json 或对应签名。"""


class VerificationError(RuntimeError):
    """安装包完整性或平台签名验证失败。"""


class InstallError(RuntimeError):
    """已验证安装包无法按当前平台策略启动。"""


def _manifest_field(
    data: Mapping[str, Any],
    name: str,
    *,
    legacy_name: str = "",
    required: bool = True,
    default: Any = None,
) -> Any:
    names = (name, legacy_name) if legacy_name else (name,)
    present = [candidate for candidate in names if candidate in data]
    if len(present) > 1:
        raise ManifestError(f"manifest {name} has duplicate aliases")
    if not present:
        if required:
            raise ManifestError(f"manifest {name} is required")
        return default
    return data[present[0]]


def _manifest_string(
    data: Mapping[str, Any],
    name: str,
    *,
    legacy_name: str = "",
    required: bool = True,
    default: str = "",
) -> str:
    value = _manifest_field(
        data,
        name,
        legacy_name=legacy_name,
        required=required,
        default=default,
    )
    if type(value) is not str:
        raise ManifestError(f"manifest {name} must be a string")
    return value


def _asset_string(
    data: Mapping[str, Any],
    name: str,
    *,
    legacy_name: str = "",
) -> str:
    value = _manifest_field(data, name, legacy_name=legacy_name)
    if type(value) is not str:
        raise ManifestError(f"manifest asset {name} must be a string")
    return value


@dataclass(frozen=True)
class UpdateAsset:
    name: str
    url: str
    sha256: str
    size: int
    installer_type: str
    os: str
    arch: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UpdateAsset":
        if not isinstance(data, Mapping):
            raise ManifestError("manifest asset entry must be an object")
        size = _manifest_field(data, "size")
        if type(size) is not int:
            raise ManifestError("manifest asset size must be an integer")
        return cls(
            name=_asset_string(data, "name"),
            url=_asset_string(data, "url"),
            sha256=_asset_string(data, "sha256").lower(),
            size=size,
            installer_type=_asset_string(
                data,
                "installerType",
                legacy_name="installer_type",
            ),
            os=_asset_string(data, "os").lower(),
            arch=normalize_arch(_asset_string(data, "arch")),
        )


@dataclass(frozen=True)
class UpdateManifest:
    schema: int
    app_id: str
    channel: str
    version: str
    tag: str
    published_at: str
    expires_at: str
    min_client_version: str
    mandatory: bool
    notes: str
    assets: dict[str, UpdateAsset]
    trusted_hosts: tuple[str, ...] = ()
    release_revision: int = 0
    source_commit: str = ""

    @property
    def identity(self) -> ReleaseIdentity:
        return ReleaseIdentity(self.version, self.release_revision)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UpdateManifest":
        if not isinstance(data, Mapping):
            raise ManifestError("manifest root must be an object")
        assets_raw = _manifest_field(data, "assets")
        if not isinstance(assets_raw, Mapping):
            raise ManifestError("manifest assets must be a map")
        assets: dict[str, UpdateAsset] = {}
        for key, value in assets_raw.items():
            if type(key) is not str:
                raise ManifestError("manifest asset keys must be strings")
            if not isinstance(value, Mapping):
                raise ManifestError("manifest asset entry must be an object")
            assets[key] = UpdateAsset.from_dict(value)
        trusted_hosts_raw = _manifest_field(
            data,
            "trustedHosts",
            required=False,
            default=[],
        )
        if type(trusted_hosts_raw) is not list or any(
            type(host) is not str or not host for host in trusted_hosts_raw
        ):
            raise ManifestError("manifest trustedHosts must be an array of strings")
        trusted_hosts = _canonicalize_manifest_hosts(trusted_hosts_raw)
        revision = _manifest_field(
            data,
            "releaseRevision",
            required=False,
            default=0,
        )
        if type(revision) is not int or not 0 <= revision <= MAX_RELEASE_REVISION:
            raise ManifestError("manifest release revision is outside the supported range")
        schema = _manifest_field(data, "schema")
        if type(schema) is not int:
            raise ManifestError("manifest schema must be an integer")
        mandatory = _manifest_field(data, "mandatory")
        if type(mandatory) is not bool:
            raise ManifestError("manifest mandatory must be a boolean")
        return cls(
            schema=schema,
            app_id=_manifest_string(data, "appId", legacy_name="app_id"),
            channel=_manifest_string(data, "channel"),
            version=_manifest_string(data, "version"),
            tag=_manifest_string(data, "tag"),
            published_at=_manifest_string(
                data,
                "publishedAt",
                legacy_name="published_at",
            ),
            expires_at=_manifest_string(
                data,
                "expiresAt",
                legacy_name="expires_at",
            ),
            min_client_version=_manifest_string(
                data,
                "minClientVersion",
                legacy_name="min_client_version",
            ),
            mandatory=mandatory,
            notes=_manifest_string(data, "notes", required=False),
            assets=assets,
            trusted_hosts=trusted_hosts,
            release_revision=revision,
            source_commit=_manifest_string(
                data,
                "sourceCommit",
                legacy_name="source_commit",
                required=False,
            ).strip().lower(),
        )


class UpdateManifestVerifier:
    """先验 Ed25519 原文签名，再约束 manifest 的 schema、应用、通道和有效期。"""

    def __init__(
        self,
        *,
        public_key_pem: str,
        app_id: str = APP_ID,
        channel: str = DEFAULT_CHANNEL,
        schema: int = MANIFEST_SCHEMA_VERSION,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.public_key_pem = public_key_pem
        self.app_id = app_id
        self.channel = channel
        self.schema = schema
        self._now = now or (lambda: datetime.now(timezone.utc))

    def load_verified(self, manifest_path: Path, signature_path: Path) -> UpdateManifest:
        manifest_file = Path(os.path.abspath(os.fspath(manifest_path)))
        signature_file = Path(os.path.abspath(os.fspath(signature_path)))
        if os.path.normcase(os.fspath(manifest_file.parent)) != os.path.normcase(
            os.fspath(signature_file.parent)
        ):
            raise ManifestError("manifest and signature must share one approved directory")
        try:
            manifest_bytes = read_bounded_regular_bytes(
                manifest_file,
                approved_root=manifest_file.parent,
                maximum=MAX_SIGNED_MANIFEST_BYTES,
            )
            signature = read_bounded_regular_bytes(
                signature_file,
                approved_root=manifest_file.parent,
                maximum=ED25519_SIGNATURE_BYTES,
            )
        except OSError as exc:
            raise ManifestError("manifest metadata path is unsafe or unreadable") from exc
        if len(manifest_bytes) > MAX_SIGNED_MANIFEST_BYTES:
            raise ManifestError("manifest exceeds the maximum size")
        if len(signature) != ED25519_SIGNATURE_BYTES:
            raise ManifestError("manifest signature has an invalid size")
        # 必须对原始 bytes 验签后再解析 JSON，解析或重编码结果不能替代签名原文。
        self.verify_signature(manifest_bytes, signature)
        try:
            payload = load_strict_manifest_json(manifest_bytes)
        except StrictManifestJsonError as exc:
            raise ManifestError("manifest is not valid strict JSON") from exc
        manifest = UpdateManifest.from_dict(payload)
        self.validate(manifest)
        return manifest

    def verify_signature(self, manifest_bytes: bytes, signature: bytes) -> None:
        try:
            key = ECC.import_key(self.public_key_pem)
            verifier = eddsa.new(key, "rfc8032")
            verifier.verify(manifest_bytes, signature)
        except Exception as exc:
            _log_update_event("update.verify.failed", "manifest signature verification failed", level="SECURITY")
            raise ManifestError("manifest signature verification failed") from exc

    def validate(self, manifest: UpdateManifest) -> None:
        if manifest.schema != self.schema:
            raise ManifestError("manifest schema is not supported")
        if manifest.app_id != self.app_id:
            raise ManifestError("manifest appId does not match this application")
        if manifest.channel != self.channel:
            raise ManifestError("manifest channel does not match this client")
        try:
            SemVer.parse(manifest.version)
            SemVer.parse(manifest.min_client_version)
            identity = manifest.identity
        except ValueError as exc:
            raise ManifestError(str(exc)) from exc
        if manifest.tag != identity.tag:
            raise ManifestError("manifest tag does not match version and release revision")
        if manifest.source_commit and not re.fullmatch(r"[0-9a-f]{40}", manifest.source_commit):
            raise ManifestError("manifest sourceCommit must be a full 40-character Git SHA")
        published_at = _parse_rfc3339(manifest.published_at)
        expires_at = _parse_rfc3339(manifest.expires_at)
        now = self._now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ManifestError("manifest verification clock must be timezone-aware")
        now = now.astimezone(timezone.utc)
        if published_at >= expires_at:
            raise ManifestError("manifest publishedAt must be before expiresAt")
        if expires_at - published_at > MAX_MANIFEST_VALIDITY:
            raise ManifestError("manifest validity window exceeds the maximum")
        if published_at > now + MAX_MANIFEST_FUTURE_SKEW:
            raise ManifestError("manifest publishedAt is materially in the future")
        if expires_at <= now:
            raise ManifestError("manifest is expired")
        if not manifest.assets:
            raise ManifestError("manifest has no assets")
        for asset in manifest.assets.values():
            _validate_asset_shape(asset)


def default_update_state_path() -> Path:
    """返回 GUI 与 updater helper 交接时共用的持久化状态路径。"""
    return user_cache_root() / "updates" / DEFAULT_UPDATE_STATE_NAME


def default_update_staging_dir() -> Path:
    """返回断点文件和已验证更新包共用的 staging 缓存目录。"""
    return user_cache_root() / "updates" / "staging"


def load_local_update_state(path: Path | None = None) -> LocalUpdateState:
    """加载回滚防护、跳过版本和待安装状态，并隐藏默认路径约定。"""
    return LocalUpdateState.load(path or default_update_state_path())


def save_local_update_state(state: LocalUpdateState, path: Path | None = None) -> None:
    state.save(path or default_update_state_path())


def record_pending_install(
    *,
    version: str,
    release_revision: int = 0,
    installer_path: str | Path,
    log_path: str | Path,
    path: Path | None = None,
    launch_token: str = "",
    previous_pending_out: list[PendingInstall | None] | None = None,
) -> LocalUpdateState:
    """只持久化一个待安装版本，使启动健康检查能限制失败重试次数。"""
    state_path = path or default_update_state_path()
    identity = ReleaseIdentity(version, release_revision)

    def mutate(state: LocalUpdateState) -> None:
        if previous_pending_out is not None:
            previous_pending_out.append(state.pending_install)
        state.pending_install = PendingInstall(
            version=identity.version,
            attempts=0,
            installer_path=str(installer_path),
            log_path=str(log_path),
            release_revision=identity.revision,
            launch_token=str(launch_token or ""),
        )

    state = mutate_update_state(
        state_path,
        load=LocalUpdateState.load,
        save=lambda current, target: current.save(target),
        mutate=mutate,
    )
    _log_update_event(
        "update.install.pending",
        "pending update recorded",
        version=version,
        revision=release_revision,
    )
    return state


def record_skipped_update(
    version: str,
    path: Path | None = None,
    *,
    release_revision: int = 0,
) -> LocalUpdateState:
    """记录用户跳过的发布身份；手动检查仍可再次选择。"""
    state_path = path or default_update_state_path()
    identity = ReleaseIdentity(version, release_revision)

    def mutate(state: LocalUpdateState) -> None:
        state.skipped_version = identity.version
        state.skipped_revision = identity.revision

    state = mutate_update_state(
        state_path,
        load=LocalUpdateState.load,
        save=lambda current, target: current.save(target),
        mutate=mutate,
    )
    _log_update_event(
        "update.check.skipped",
        "update version skipped",
        version=state.skipped_version,
        revision=state.skipped_revision,
    )
    return state


def record_last_seen_update(
    version: str,
    *,
    release_revision: int = 0,
    path: Path | None = None,
) -> LocalUpdateState:
    state_path = path or default_update_state_path()
    identity = ReleaseIdentity(version, release_revision)

    def mutate(state: LocalUpdateState) -> None:
        state.last_seen_version = identity.version
        state.last_seen_revision = identity.revision

    return mutate_update_state(
        state_path,
        load=LocalUpdateState.load,
        save=lambda current, target: current.save(target),
        mutate=mutate,
    )


def record_startup_update_health(
    *,
    current_version: str,
    current_revision: int = 0,
    path: Path | None = None,
    staging_dir: Path | None = None,
) -> LocalUpdateState:
    """启动后以实际运行版本确认安装结果；未切换版本会累计尝试并最终停止重试。"""
    state_path = path or default_update_state_path()
    telemetry: list[tuple[str, str, str, dict[str, Any]]] = []

    def mutate(state: LocalUpdateState) -> None:
        pending = state.pending_install
        state.record_startup_health(
            current_version=current_version,
            current_revision=current_revision,
            staging_dir=staging_dir,
        )
        if pending is None:
            return
        if state.pending_install is None and not state.last_install_error:
            telemetry.append(
                (
                    "update.install.succeeded",
                    "pending update completed",
                    "INFO",
                    {"version": current_version, "revision": current_revision},
                )
            )
        else:
            telemetry.append(
                (
                    "update.install.failed",
                    state.last_install_error,
                    "ERROR",
                    {},
                )
            )

    state = mutate_update_state(
        state_path,
        load=LocalUpdateState.load,
        save=lambda current, target: current.save(target),
        mutate=mutate,
    )
    for event, message, level, details in telemetry:
        _log_update_event(event, message, level=level, **details)
    return state


def rollback_pending_install(
    launch_token: str,
    *,
    path: Path | None = None,
    restore_pending: PendingInstall | None = None,
) -> LocalUpdateState:
    state_path = path or default_update_state_path()
    token = str(launch_token or "")

    def mutate(state: LocalUpdateState) -> None:
        pending = state.pending_install
        if pending is not None and token and pending.launch_token == token:
            state.pending_install = restore_pending

    return mutate_update_state(
        state_path,
        load=LocalUpdateState.load,
        save=lambda current, target: current.save(target),
        mutate=mutate,
    )


@dataclass(frozen=True)
class UpdatePolicy:
    allowed: bool
    reason: str = ""
    mandatory: bool = False


class VersionPolicy:
    """统一处理渠道、已安装版本和用户跳过记录的更新准入。"""

    def __init__(
        self,
        *,
        channel: str = DEFAULT_CHANNEL,
        state: LocalUpdateState | None = None,
        allow_downgrade: bool = False,
    ) -> None:
        self.channel = channel
        self.state = state or LocalUpdateState()
        self.allow_downgrade = allow_downgrade

    def evaluate(
        self,
        update_version: str,
        *,
        update_revision: int = 0,
        current_version: str,
        current_revision: int = 0,
        manual: bool = False,
        mandatory: bool = False,
    ) -> UpdatePolicy:
        try:
            candidate = SemVer.parse(update_version)
            candidate_identity = ReleaseIdentity(update_version, update_revision)
            current_identity = ReleaseIdentity(current_version, current_revision)
        except ValueError as exc:
            return UpdatePolicy(False, str(exc), mandatory=False)
        if self.channel == "stable" and candidate.prerelease:
            return UpdatePolicy(False, "stable channel excludes prerelease updates", mandatory=False)
        if candidate_identity <= current_identity and not self.allow_downgrade:
            return UpdatePolicy(False, "candidate release is not newer than current release", mandatory=False)
        if self.state.skipped_version:
            skipped = ReleaseIdentity(self.state.skipped_version, self.state.skipped_revision)
            if candidate_identity == skipped and not manual:
                return UpdatePolicy(False, "candidate release was skipped", mandatory=False)
        self.state.last_seen_version = candidate_identity.version
        self.state.last_seen_revision = candidate_identity.revision
        # mandatory 仅为旧清单兼容字段；更新必须始终由用户确认。
        return UpdatePolicy(True, mandatory=False)


@dataclass(frozen=True)
class ManifestLocations:
    manifest_url: str = ""
    signature_url: str = ""
    etag: str = ""
    last_modified: str = ""
    not_modified: bool = False
    release_url: str = ""
    tag_name: str = ""
    release_name: str = ""


class GitHubReleaseClient:
    """无凭据读取 GitHub release 元数据，并用 ETag/Last-Modified 控制缓存请求。"""

    def __init__(
        self,
        *,
        owner: str,
        repo: str,
        cache_path: Path | None = None,
        api_base: str = "https://api.github.com",
        user_agent: str = "UniversalCrawlerPro/update-check",
        timeout: float = 8.0,
        auto_check_interval_seconds: int = DEFAULT_AUTO_CHECK_INTERVAL_SECONDS,
    ) -> None:
        self.owner = owner
        self.repo = repo
        self.cache_path = cache_path or user_cache_root() / "updates" / "github_release_cache.json"
        self.api_base = api_base.rstrip("/")
        self.user_agent = user_agent
        self.timeout = timeout
        self.auto_check_interval_seconds = int(auto_check_interval_seconds)

    def http_error(self, url: str, code: int, message: str) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(url, code, message, hdrs=Message(), fp=None)

    def fetch_manifest_locations(
        self,
        *,
        opener: UrlOpener | None = None,
        manual: bool = False,
    ) -> ManifestLocations:
        effective_opener = opener or self._trusted_opener()
        cache = self._load_cache()
        if not manual and self._is_auto_check_throttled(cache):
            _log_update_event("update.check.throttled", "automatic update check throttled")
            return ManifestLocations(
                etag=str(cache.get("etag") or ""),
                last_modified=str(cache.get("last_modified") or ""),
                not_modified=True,
                release_url=str(cache.get("release_url") or ""),
            )
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": self.user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if cache.get("etag"):
            headers["If-None-Match"] = str(cache["etag"])
        if cache.get("last_modified"):
            headers["If-Modified-Since"] = str(cache["last_modified"])
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/releases/latest"
        request = urllib.request.Request(url, headers=headers)
        _log_update_event("update.check.started", "checking GitHub release", manual=manual)
        response: Any | None = None
        response_error: BaseException | None = None
        try:
            try:
                response = effective_opener(request, timeout=self.timeout)
                validate_asset_url(_response_final_url(response, url), {"api.github.com"})
                payload_bytes = response.read(DEFAULT_MAX_RELEASE_API_BYTES + 1)
                if len(payload_bytes) > DEFAULT_MAX_RELEASE_API_BYTES:
                    raise DownloadError("GitHub release response is too large")
                payload = payload_bytes.decode("utf-8")
                data = json.loads(payload)
                etag = str(getattr(response, "headers", {}).get("ETag") or "")
                last_modified = str(getattr(response, "headers", {}).get("Last-Modified") or "")
            except BaseException as exc:
                response_error = exc
                raise
            finally:
                if response is not None:
                    run_typed_cleanup_actions(
                        response_error,
                        ((_response_close_action(response), "GitHub release response cleanup failed"),),
                        error_factory=DownloadError,
                        failure_message="GitHub release response cleanup failed",
                    )
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                if not (cache.get("etag") or cache.get("last_modified")):
                    raise DownloadError(
                        "GitHub release returned 304 without an active validator"
                    ) from exc
                return ManifestLocations(not_modified=True, etag=str(cache.get("etag") or ""), last_modified=str(cache.get("last_modified") or ""))
            if exc.code in {403, 429}:
                _log_update_event(
                    "update.check.api_rate_limited",
                    "GitHub API rate limited; falling back to the public release feed",
                    level="WARN",
                )
                return self._fetch_manifest_locations_from_atom(opener=effective_opener, per_page=1)[0]
            if exc.code == 404:
                raise DownloadError("GitHub release was not found") from exc
            if exc.code >= 500:
                raise DownloadError("GitHub release service is temporarily unavailable") from exc
            raise DownloadError(f"GitHub release request failed with HTTP {exc.code}") from exc
        except (
            OSError,
            TimeoutError,
            UnicodeError,
            urllib.error.URLError,
            json.JSONDecodeError,
            RecursionError,
            ManifestError,
        ) as exc:
            raise DownloadError(f"GitHub release request failed: {exc}") from exc
        if not isinstance(data, Mapping):
            raise DownloadError("GitHub release response had an unexpected shape")
        locations = self._locations_from_release(data, etag=etag, last_modified=last_modified)
        if locations.etag or locations.last_modified:
            self._save_cache(
                {
                    "etag": locations.etag,
                    "last_modified": locations.last_modified,
                    "last_check_at": datetime.now(timezone.utc).isoformat(),
                    "release_url": locations.release_url,
                }
            )
        return locations

    def fetch_manifest_location_candidates(
        self,
        *,
        opener: UrlOpener | None = None,
        manual: bool = False,
        per_page: int = 10,
    ) -> tuple[ManifestLocations, ...]:
        """按新到旧返回近期 Release 中的签名 manifest 地址。"""

        effective_opener = opener or self._trusted_opener()
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": self.user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        page_size = max(1, min(100, int(per_page)))
        url = f"{self.api_base}/repos/{self.owner}/{self.repo}/releases?per_page={page_size}"
        _log_update_event("update.check.started", "checking GitHub release candidates", manual=manual)
        def candidate_factory(
            release: Mapping[str, Any],
            etag: str,
            last_modified: str,
        ) -> ManifestLocations | None:
            try:
                return self._locations_from_release(
                    release,
                    etag=etag,
                    last_modified=last_modified,
                )
            except DownloadError:
                return None

        try:
            locations = collect_release_candidates(
                opener=effective_opener,
                first_url=url,
                headers=headers,
                timeout=self.timeout,
                owner=self.owner,
                repo=self.repo,
                page_size=page_size,
                validate_url=validate_asset_url,
                candidate_factory=candidate_factory,
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                raise DownloadError(
                    "GitHub releases returned 304 without a page-specific validator"
                ) from exc
            if exc.code in {403, 429}:
                _log_update_event(
                    "update.check.api_rate_limited",
                    "GitHub API rate limited; falling back to the public release feed",
                    level="WARN",
                )
                return self._fetch_manifest_locations_from_atom(
                    opener=effective_opener,
                    per_page=page_size,
                )
            if exc.code == 404:
                raise DownloadError("GitHub releases were not found") from exc
            if exc.code >= 500:
                raise DownloadError("GitHub release service is temporarily unavailable") from exc
            raise DownloadError(f"GitHub release request failed with HTTP {exc.code}") from exc
        except (OSError, TimeoutError, urllib.error.URLError, GitHubReleasePageError) as exc:
            raise DownloadError(f"GitHub release request failed: {exc}") from exc
        if not locations:
            raise MetadataNotFoundError("GitHub releases do not contain signed update manifests")
        return tuple(locations)

    @staticmethod
    def _trusted_opener() -> UrlOpener:
        allowed_hosts = frozenset({"api.github.com", "github.com"})

        def opener(request: urllib.request.Request, *, timeout: float) -> Any:
            return open_trusted_url(request, timeout=timeout, allowed_hosts=allowed_hosts)

        return opener

    def _fetch_manifest_locations_from_atom(
        self,
        *,
        opener: Callable[..., Any],
        per_page: int,
    ) -> tuple[ManifestLocations, ...]:
        """通过 Atom 发现 release tag，避免消耗 GitHub REST API 限额。

        Atom 只提供 release 身份；下载后的 manifest 仍走正常 Ed25519 验证，
        因而该降级路径不会扩大安装信任边界。
        """

        feed_url = f"https://github.com/{self.owner}/{self.repo}/releases.atom"
        request = urllib.request.Request(feed_url, headers={"User-Agent": self.user_agent})
        response: Any | None = None
        response_error: BaseException | None = None
        try:
            try:
                response = opener(request, timeout=self.timeout)
                validate_asset_url(_response_final_url(response, feed_url), {"github.com"})
                payload = response.read(DEFAULT_MAX_RELEASE_FEED_BYTES + 1)
            except BaseException as exc:
                response_error = exc
                raise
            finally:
                if response is not None:
                    run_typed_cleanup_actions(
                        response_error,
                        ((_response_close_action(response), "GitHub release feed response cleanup failed"),),
                        error_factory=DownloadError,
                        failure_message="GitHub release feed response cleanup failed",
                    )
        except urllib.error.HTTPError as exc:
            raise DownloadError(f"GitHub release feed failed with HTTP {exc.code}") from exc
        except (OSError, TimeoutError, urllib.error.URLError, ManifestError) as exc:
            raise DownloadError(f"GitHub release feed failed: {exc}") from exc
        if len(payload) > DEFAULT_MAX_RELEASE_FEED_BYTES:
            raise DownloadError("GitHub release feed is too large")
        try:
            # Atom 只承载 release tag，不需要 DTD/实体；拒绝这些特性可避免实体扩展攻击。
            root = DefusedET.fromstring(payload)
        except DefusedXmlException as exc:
            raise DownloadError(f"GitHub release feed contains unsafe XML: {exc}") from exc
        except ET.ParseError as exc:
            raise DownloadError(f"GitHub release feed failed: {exc}") from exc

        namespace = {"atom": "http://www.w3.org/2005/Atom"}
        expected_prefix = f"/{self.owner}/{self.repo}/releases/tag/"
        locations: list[ManifestLocations] = []
        for entry in root.findall("atom:entry", namespace):
            release_url = ""
            for link in entry.findall("atom:link", namespace):
                if str(link.attrib.get("rel") or "alternate") == "alternate":
                    release_url = str(link.attrib.get("href") or "")
                    break
            parsed = urllib.parse.urlparse(release_url)
            if parsed.scheme != "https" or parsed.hostname != "github.com" or not parsed.path.startswith(expected_prefix):
                continue
            tag_name = urllib.parse.unquote(parsed.path[len(expected_prefix) :]).strip("/")
            if not tag_name:
                continue
            encoded_tag = urllib.parse.quote(tag_name, safe="")
            download_base = f"https://github.com/{self.owner}/{self.repo}/releases/download/{encoded_tag}"
            locations.append(
                ManifestLocations(
                    manifest_url=f"{download_base}/{DEFAULT_MANIFEST_NAME}",
                    signature_url=f"{download_base}/{DEFAULT_SIGNATURE_NAME}",
                    release_url=release_url,
                    tag_name=tag_name,
                    release_name=str(entry.findtext("atom:title", default="", namespaces=namespace) or ""),
                )
            )
            if len(locations) >= per_page:
                break
        if not locations:
            raise DownloadError("GitHub release feed did not contain release tags")
        return tuple(locations)

    def _locations_from_release(self, data: Mapping[str, Any], *, etag: str, last_modified: str) -> ManifestLocations:
        manifest_url = ""
        sig_url = ""
        assets = data.get("assets")
        if type(assets) is not list:
            raise DownloadError("GitHub release assets had an unexpected shape")
        for asset in assets:
            if not isinstance(asset, Mapping):
                raise DownloadError("GitHub release assets had an unexpected shape")
            name = asset.get("name")
            browser_url = asset.get("browser_download_url")
            if type(name) is not str or type(browser_url) is not str:
                raise DownloadError("GitHub release assets had an unexpected shape")
            if name == DEFAULT_MANIFEST_NAME:
                manifest_url = browser_url
            elif name == DEFAULT_SIGNATURE_NAME:
                sig_url = browser_url
        if not manifest_url or not sig_url:
            raise MetadataNotFoundError("GitHub release does not contain latest.json and latest.json.sig")
        try:
            validate_asset_url(manifest_url, DEFAULT_ALLOWED_HOSTS)
            validate_asset_url(sig_url, DEFAULT_ALLOWED_HOSTS)
        except ManifestError as exc:
            raise DownloadError("GitHub release metadata URL is not trusted") from exc
        release_fields = {
            key: data.get(key) or ""
            for key in ("html_url", "tag_name", "name")
        }
        if any(type(value) is not str for value in release_fields.values()):
            raise DownloadError("GitHub release identity had an unexpected shape")
        return ManifestLocations(
            manifest_url=manifest_url,
            signature_url=sig_url,
            etag=etag,
            last_modified=last_modified,
            release_url=release_fields["html_url"],
            tag_name=release_fields["tag_name"],
            release_name=release_fields["name"],
        )

    def _load_cache(self) -> dict[str, Any]:
        return load_release_cache(
            self.cache_path,
            expected_scope={
            "schema": 1,
            "endpoint": "latest",
            "api_base": self.api_base,
            "owner": self.owner,
            "repo": self.repo,
            },
        )

    def _save_cache(self, payload: Mapping[str, Any]) -> None:
        save_release_cache(
            self.cache_path,
            scope={
                "schema": 1,
                "endpoint": "latest",
                "api_base": self.api_base,
                "owner": self.owner,
                "repo": self.repo,
            },
            payload=payload,
        )

    def _is_auto_check_throttled(self, cache: Mapping[str, Any]) -> bool:
        if self.auto_check_interval_seconds <= 0:
            return False
        last_check_at = str(cache.get("last_check_at") or "")
        if not last_check_at:
            return False
        try:
            checked_at = datetime.fromisoformat(last_check_at.replace("Z", "+00:00"))
            if checked_at.tzinfo is None or checked_at.utcoffset() is None:
                return False
            checked_at = checked_at.astimezone(timezone.utc)
        except (OverflowError, ValueError):
            return False
        age = (datetime.now(timezone.utc) - checked_at).total_seconds()
        return 0 <= age < self.auto_check_interval_seconds


class AssetSelector:
    """只选择与当前 OS、CPU 架构和安装器类型完全匹配的 manifest 资产。"""

    def __init__(
        self,
        *,
        os_name: str | None = None,
        arch: str | None = None,
        installer_types: tuple[str, ...] | None = None,
        allowed_hosts: set[str] | frozenset[str] | None = None,
        development_mode: bool = False,
    ) -> None:
        self.os_name = normalize_os(os_name or sys.platform)
        self.arch = normalize_arch(arch or platform.machine())
        configured_types = (
            installer_types
            if installer_types is not None
            else default_installer_types(self.os_name)
        )
        self.installer_types = tuple(str(value).lower() for value in configured_types)
        self.allowed_hosts = _canonicalize_allowed_hosts(
            allowed_hosts or DEFAULT_ALLOWED_HOSTS,
        )
        self.development_mode = development_mode

    def select(self, manifest: UpdateManifest) -> UpdateAsset:
        hosts = self.allowed_hosts | frozenset(manifest.trusted_hosts)
        candidates = [
            asset
            for asset in manifest.assets.values()
            if normalize_os(asset.os) == self.os_name
            and normalize_arch(asset.arch) in {self.arch, "universal"}
            and asset.installer_type.lower() in self.installer_types
        ]
        if not candidates:
            raise ManifestError(f"no update asset for {self.os_name}-{self.arch}")
        for installer_type in self.installer_types:
            matches = [
                asset
                for asset in candidates
                if asset.installer_type.lower() == installer_type
            ]
            if len(matches) > 1:
                raise ManifestError(
                    f"multiple update assets match {self.os_name}-{self.arch}/{installer_type}"
                )
            if matches:
                validate_asset_url(
                    matches[0].url,
                    hosts,
                    development_mode=self.development_mode,
                )
                return matches[0]
        raise ManifestError(f"no update asset for {self.os_name}-{self.arch}")


Downloader = _Downloader



class PackageVerifier(PlatformPackageVerifier):
    """校验安装包完整性，并按发布策略追加操作系统签名校验。"""

    def __init__(
        self,
        *,
        os_name: str | None = None,
        trusted_publishers: list[str] | tuple[str, ...] = (),
        trusted_thumbprints: list[str] | tuple[str, ...] = (),
        require_os_signature: bool = True,
        verify_func: Callable[[Path, UpdateAsset], None] | None = None,
        run_func: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        super().__init__(
            os_name=os_name or sys.platform,
            trusted_publishers=tuple(trusted_publishers),
            trusted_thumbprints=tuple(trusted_thumbprints),
            require_os_signature=require_os_signature,
            verify_func=verify_func,
            run_func=run_func,
            normalize_os=normalize_os,
            normalize_fingerprint=_normalize_certificate_fingerprint,
            verify_file_hash=_verify_file_hash,
            error_factory=VerificationError,
        )

class InstallerRunner(InstallerRunnerCore):
    """updater helper 侧的安装器入口；每次启动前都重新执行 PackageVerifier。"""

    def __init__(
        self,
        *,
        package_verifier: PackageVerifier,
        run_func: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        popen_func: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        super().__init__(
            package_verifier=package_verifier,
            run_func=run_func,
            popen_func=popen_func,
            default_staging_dir=default_update_staging_dir,
            logs_root=user_logs_root,
            sanitize_filename=sanitize_filename,
            log_event=_log_update_event,
            verification_error=VerificationError,
            install_error=InstallError,
        )

    def launch_verified_installer(
        self,
        path: Path,
        asset: UpdateAsset,
        *,
        version: str,
        log_path: Path,
        install_dir: Path,
        restart_handoff_path: Path | None,
        restart_handoff_token: str = "",
        approved_root: Path | None = None,
    ) -> subprocess.Popen:
        """验证后异步启动会覆盖当前安装目录的 Inno 安装器。

        updater helper 本身也位于安装目录。若在 helper 内同步等待安装器，
        Windows Restart Manager 会要求 helper 退出，而 helper 又在等待安装器，
        最终形成死锁。这里仅完成最后一次校验与进程交接，让 helper 随即退出；
        安装成功后的重启由 Inno 调用新版本 helper 完成。
        """

        return super().launch_verified_installer(
            path,
            asset,
            version=version,
            log_path=log_path,
            install_dir=install_dir,
            restart_handoff_path=restart_handoff_path,
            restart_handoff_token=restart_handoff_token,
            approved_root=approved_root,
        )

    @staticmethod
    def _install_argv(
        path: Path,
        asset: UpdateAsset,
        log_path: Path,
        *,
        install_dir: Path | None = None,
        restart_handoff_path: Path | None = None,
        restart_handoff_token: str = "",
    ) -> list[str]:
        return build_installer_argv(
            path,
            asset,
            log_path,
            install_error=InstallError,
            install_dir=install_dir,
            restart_handoff_path=restart_handoff_path,
            restart_handoff_token=restart_handoff_token,
        )


def write_update_asset_descriptor(installer_path: Path, asset: UpdateAsset) -> Path:
    """原子保存 GUI 交给 updater helper 的已验证资产描述。"""

    descriptor_path = Path(installer_path).with_name(f"{Path(installer_path).name}.asset.json")
    _atomic_write_text(
        descriptor_path,
        json.dumps(
            {
                "name": asset.name,
                "url": asset.url,
                "sha256": asset.sha256,
                "size": asset.size,
                "installerType": asset.installer_type,
                "os": asset.os,
                "arch": asset.arch,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    return descriptor_path


def _parse_rfc3339(value: str) -> datetime:
    raw = str(value)
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ManifestError(f"datetime must include a timezone: {value!r}")
        return parsed.astimezone(timezone.utc)
    except ManifestError:
        raise
    except (OverflowError, ValueError) as exc:
        raise ManifestError(f"invalid datetime: {value!r}") from exc


def _validate_asset_shape(asset: UpdateAsset) -> None:
    if not asset.name or Path(asset.name).name != asset.name:
        raise ManifestError("asset name is missing or unsafe")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", asset.sha256 or ""):
        raise ManifestError("asset sha256 must be a 64-character hex digest")
    if asset.size <= 0:
        raise ManifestError("asset size must be positive")
    if normalize_os(asset.os) not in {"windows", "macos", "linux"}:
        raise ManifestError("asset os is not supported")
    if normalize_arch(asset.arch) not in {"x64", "arm64", "universal"}:
        raise ManifestError("asset arch is not supported")
    if not asset.installer_type:
        raise ManifestError("asset installerType is missing")


def _normalize_semver_text(value: str) -> str:
    version = SemVer.parse(value)
    text = f"{version.major}.{version.minor}.{version.patch}"
    if version.prerelease:
        text += "-" + ".".join(version.prerelease)
    if version.build:
        text += "+" + version.build
    return text


def normalize_os(value: Any) -> str:
    raw = str(value or "").lower()
    if raw.startswith("win") or raw == "windows":
        return "windows"
    if raw in {"darwin", "mac", "macos", "osx"}:
        return "macos"
    if raw.startswith("linux"):
        return "linux"
    return raw


def normalize_arch(value: Any) -> str:
    raw = str(value or "").lower().replace("amd64", "x64").replace("x86_64", "x64")
    if raw in {"aarch64", "arm64"}:
        return "arm64"
    if raw in {"universal", "universal2"}:
        return "universal"
    return raw


def _normalize_certificate_fingerprint(value: Any) -> str:
    """去掉 Windows 工具输出中的分隔符，统一 SHA1/SHA256 证书指纹格式。"""
    return re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).upper()


def default_installer_types(os_name: str) -> tuple[str, ...]:
    if os_name == "windows":
        return ("inno",)
    return ()


def _is_local_development_asset_url(parsed: urllib.parse.ParseResult) -> bool:
    """开发模式只放行 file 或 loopback 资源，不能借该开关绕过 SSRF 边界。"""
    if parsed.scheme == "file":
        return (parsed.netloc or "").lower() in {"", "localhost"} and bool(parsed.path)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_asset_url(url: str, allowed_hosts: set[str] | frozenset[str], *, development_mode: bool = False) -> None:
    """生产 URL 必须使用 HTTPS 且命中 host 白名单；裸 IP 与 localhost 一律拒绝。"""
    if (
        type(url) is not str
        or not url
        or url != url.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in url)
    ):
        raise ManifestError("asset URL is invalid")
    raw_url = url
    try:
        parsed = urllib.parse.urlparse(raw_url)
    except ValueError as exc:
        raise ManifestError("asset URL is invalid") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ManifestError("asset URL must not contain userinfo")
    if "#" in raw_url:
        raise ManifestError("asset URL must not contain a fragment")
    if development_mode and _is_local_development_asset_url(parsed):
        return
    if parsed.scheme != "https":
        raise ManifestError("asset URL must use https")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ManifestError("asset URL port is invalid") from exc
    if port not in {None, 443}:
        raise ManifestError("asset URL must use the default https port")
    raw_host = parsed.hostname or ""
    if not raw_host:
        raise ManifestError("asset URL host is missing")
    try:
        host = canonicalize_host(raw_host)
    except DomainPolicyViolation as exc:
        raise ManifestError("asset URL host is invalid") from exc
    if host in {"localhost", "127.0.0.1", "::1"}:
        raise ManifestError("asset URL must not point to localhost")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ManifestError("asset URL must not use a bare IP address")
    if host not in _canonicalize_allowed_hosts(allowed_hosts):
        raise ManifestError(f"asset URL host is not trusted: {host}")


def _canonicalize_manifest_hosts(hosts: list[str]) -> tuple[str, ...]:
    try:
        return tuple(dict.fromkeys(canonicalize_host(host) for host in hosts))
    except DomainPolicyViolation as exc:
        raise ManifestError("manifest trustedHosts contains an invalid host") from exc


def _canonicalize_allowed_hosts(hosts: object) -> frozenset[str]:
    try:
        return frozenset(canonicalize_host(host) for host in hosts)  # type: ignore[union-attr]
    except (DomainPolicyViolation, TypeError) as exc:
        raise ManifestError("allowed update host is invalid") from exc


def sanitize_filename(name: str) -> str:
    value = Path(str(name or "")).name
    value = re.sub(r"[^0-9A-Za-z._() -]+", "_", value).strip(" .")
    if not value:
        raise DownloadError("asset filename is empty")
    return value


def _verify_file_hash(path: Path, asset: UpdateAsset) -> None:
    file_path = Path(path)
    handle: Any | None = None
    primary_error: BaseException | None = None
    hasher = hashlib.sha256()
    try:
        handle = open_regular_binary(
            file_path,
            approved_root=file_path.parent,
            mode="rb",
        )
        if os.fstat(handle.fileno()).st_size != asset.size:
            raise VerificationError("installer size does not match manifest")
        remaining = asset.size
        while remaining:
            chunk = handle.read(min(1024 * 512, remaining))
            if not chunk:
                raise VerificationError("installer size does not match manifest")
            hasher.update(chunk)
            remaining -= len(chunk)
        if handle.read(1):
            raise VerificationError("installer size does not match manifest")
    except VerificationError as exc:
        primary_error = exc
        raise
    except OSError as exc:
        failure = VerificationError("installer file could not be read")
        primary_error = failure
        raise failure from exc
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if handle is not None:
            run_typed_cleanup_actions(
                primary_error,
                ((handle.close, "installer verification handle cleanup failed"),),
                error_factory=VerificationError,
                failure_message="installer verification handle cleanup failed",
            )
    if hasher.hexdigest().lower() != asset.sha256.lower():
        raise VerificationError("installer hash does not match manifest")


_SENSITIVE_UPDATE_DETAIL_MARKERS = (
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "sessionkey",
    "token",
)


def _is_sensitive_update_detail_key(key: Any) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", str(key or "").lower())
    return any(marker in compact for marker in _SENSITIVE_UPDATE_DETAIL_MARKERS)


def _log_update_event(event: str, message: str, *, level: str = "INFO", **details: Any) -> None:
    try:
        safe_details = {
            key: value
            for key, value in details.items()
            if not _is_sensitive_update_detail_key(key)
        }
        debug_logger.log(
            component="Updater",
            action=event,
            message=message,
            level=level,
            status_code=event,
            details=safe_details,
        )
    except BaseException:
        return


def log_update_event(event: str, message: str, *, level: str = "INFO", **details: Any) -> None:
    """复用更新日志脱敏规则记录结构化事件，避免凭据字段进入调试日志。"""
    _log_update_event(event, message, level=level, **details)
