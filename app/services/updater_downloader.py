"""Bounded, resumable installer downloader for the secure updater."""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import Event
from typing import Any, Callable, Mapping

from app.services.updater_cleanup import (
    run_cleanup_actions,
    run_typed_cleanup_actions,
)
from app.services.updater_download_locks import target_download_lock
from app.services.updater_download_protocol import (
    RestartFullDownload,
    response_status,
    validate_download_response,
)
from app.services.updater_path_safety import (
    ensure_approved_directory,
    ensure_regular_or_missing,
    open_regular_binary,
    replace_regular_file,
)
from app.services.updater_transport import build_updater_transport
from shared.network.pinned_transport import PinnedTransportNetworkError
from shared.runtime_options import DomainPolicyViolation

DEFAULT_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_UPDATER_REDIRECT_BYTES = 1_000_000


class DownloadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        keep_partial: bool = False,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.keep_partial = bool(keep_partial)
        self.retryable = bool(retryable)


class Downloader:
    """Download one signed asset through a bounded, DNS-pinned stream."""

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        max_size_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        retries: int = 2,
        allowed_hosts: set[str] | frozenset[str] | None = None,
        development_mode: bool = False,
    ) -> None:
        updater = _updater_module()
        self.cache_dir = cache_dir or updater.default_update_staging_dir()
        self.max_size_bytes = int(max_size_bytes)
        self.timeout_seconds = float(timeout_seconds)
        self.retries = max(0, int(retries))
        self.allowed_hosts = frozenset(
            host.lower() for host in (allowed_hosts or updater.DEFAULT_ALLOWED_HOSTS)
        )
        self.development_mode = bool(development_mode)

    def download(
        self,
        asset: Any,
        *,
        opener: Callable[..., Any] | None = None,
        cancel_event: Event | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path:
        updater = _updater_module()
        updater.validate_asset_url(
            asset.url,
            self.allowed_hosts,
            development_mode=self.development_mode,
        )
        if asset.size <= 0:
            raise DownloadError("asset size must be positive")
        if asset.size > self.max_size_bytes:
            raise DownloadError("asset size exceeds configured maximum")
        try:
            cache_dir = ensure_approved_directory(self.cache_dir, create=True)
        except OSError as exc:
            raise DownloadError("download staging directory is unsafe") from exc
        target = cache_dir / updater.sanitize_filename(asset.name)
        partial = target.with_name(target.name + ".partial")
        effective_opener = opener or (
            self._trusted_opener() if self.development_mode else None
        )
        with target_download_lock(
            target,
            cancel_event=cancel_event,
            timeout_seconds=self.timeout_seconds,
            error_factory=DownloadError,
        ):
            return self._run_download_transaction(
                asset,
                target,
                partial,
                effective_opener,
                cancel_event,
                progress_callback,
            )

    def _run_download_transaction(
        self,
        asset: Any,
        target: Path,
        partial: Path,
        opener: Callable[..., Any] | None,
        cancel_event: Event | None,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> Path:
        updater = _updater_module()
        attempt = 0
        while True:
            try:
                return self._download_once(
                    asset,
                    target,
                    partial,
                    opener,
                    cancel_event,
                    progress_callback,
                )
            except RestartFullDownload:
                try:
                    _truncate_partial(partial, approved_root=target.parent)
                except (DownloadError, OSError) as exc:
                    raise DownloadError(
                        "partial download could not be reset after range fallback"
                    ) from exc
                continue
            except updater.ManifestError as exc:
                failure = DownloadError(f"download redirect was rejected: {exc}")
                _cleanup_partial(partial, failure, approved_root=target.parent)
                raise failure from exc
            except DownloadError as exc:
                if not exc.keep_partial:
                    _cleanup_partial(partial, exc, approved_root=target.parent)
                if cancel_event is not None and cancel_event.is_set():
                    raise
                if not exc.retryable or attempt >= self.retries:
                    raise
                attempt += 1
            except BaseException as exc:
                _cleanup_partial(partial, exc, approved_root=target.parent)
                raise

    def _trusted_opener(self) -> Callable[..., Any]:
        updater = _updater_module()

        def opener(request: urllib.request.Request, *, timeout: float) -> Any:
            return updater.open_trusted_url(
                request,
                timeout=timeout,
                allowed_hosts=self.allowed_hosts,
                development_mode=self.development_mode,
            )

        return opener

    def _download_once(
        self,
        asset: Any,
        target: Path,
        partial: Path,
        opener: Callable[..., Any] | None,
        cancel_event: Event | None,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> Path:
        updater = _updater_module()
        try:
            target = ensure_regular_or_missing(target, approved_root=target.parent)
            partial = ensure_regular_or_missing(partial, approved_root=target.parent)
            if target.exists():
                try:
                    updater._verify_file_hash(target, asset)
                    return target
                except updater.VerificationError:
                    pass
            resume_from = partial.stat().st_size if partial.exists() else 0
            if resume_from >= asset.size:
                if resume_from == asset.size:
                    try:
                        updater._verify_file_hash(partial, asset)
                        _promote_verified_partial(
                            partial,
                            target,
                            asset=asset,
                            updater=updater,
                        )
                        return target
                    except updater.VerificationError:
                        pass
                _truncate_partial(partial, approved_root=target.parent)
                resume_from = 0
        except OSError as exc:
            raise DownloadError("download staging preparation failed") from exc

        downloaded = resume_from
        attempt_downloaded = 0
        started = time.monotonic()
        headers = {"User-Agent": "UniversalCrawlerPro/updater"}
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"

        def validate_response(status: int, response_headers: Mapping[str, str]) -> None:
            validate_download_response(
                status,
                response_headers,
                asset_size=asset.size,
                resume_from=resume_from,
                error_factory=DownloadError,
            )

        handle: Any | None = None
        primary_error: BaseException | None = None
        updater._log_update_event(
            "update.download.started",
            "download started",
            asset=asset.name,
            size=asset.size,
        )
        try:
            handle = open_regular_binary(
                partial,
                approved_root=target.parent,
                mode="ab" if resume_from else "wb",
            )

            def consume(chunk: bytes) -> None:
                nonlocal downloaded, attempt_downloaded
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadError("download was cancelled", keep_partial=True)
                if type(chunk) is not bytes:
                    raise DownloadError("download response body is invalid")
                if downloaded + len(chunk) > min(self.max_size_bytes, asset.size):
                    raise DownloadError("download exceeded expected size")
                try:
                    written = handle.write(chunk)
                except OSError as exc:
                    raise DownloadError("partial download write failed") from exc
                if type(written) is not int or written != len(chunk):
                    raise DownloadError("partial download write was incomplete")
                downloaded += len(chunk)
                attempt_downloaded += len(chunk)
                if progress_callback:
                    elapsed = max(0.001, time.monotonic() - started)
                    progress: dict[str, Any] = {
                        "bytesDownloaded": downloaded,
                        "totalBytes": asset.size,
                        "percent": round(downloaded / asset.size * 100, 2),
                        "speed": attempt_downloaded / elapsed,
                        "state": "Downloading",
                    }
                    updater._log_update_event(
                        "update.download.progress",
                        "download progress",
                        **progress,
                    )
                    progress_callback(progress)

            if opener is None:
                self._download_pinned(
                    asset,
                    headers,
                    consume,
                    validate_response,
                    resume_from=resume_from,
                )
            else:
                self._download_injected(
                    asset,
                    headers,
                    consume,
                    validate_response,
                    opener=opener,
                    partial=partial,
                )
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except OSError as exc:
                raise DownloadError("partial download sync failed") from exc
        except RestartFullDownload as exc:
            primary_error = exc
            raise
        except DownloadError as exc:
            primary_error = exc
            raise
        except PinnedTransportNetworkError as exc:
            failure = DownloadError(
                "download network request failed",
                keep_partial=_safe_regular_file_exists(
                    partial,
                    approved_root=target.parent,
                ),
                retryable=True,
            )
            primary_error = failure
            raise failure from exc
        except DomainPolicyViolation as exc:
            failure = DownloadError("download network policy rejected the request")
            primary_error = failure
            raise failure from exc
        except OSError as exc:
            failure = DownloadError("download staging I/O failed")
            primary_error = failure
            raise failure from exc
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if handle is not None:
                run_typed_cleanup_actions(
                    primary_error,
                    ((lambda: handle.close(), "partial download handle cleanup failed"),),
                    error_factory=DownloadError,
                    failure_message="partial download handle cleanup failed",
                )
        if downloaded != asset.size:
            raise DownloadError(
                "downloaded size does not match manifest",
                keep_partial=True,
                retryable=True,
            )
        try:
            updater._verify_file_hash(partial, asset)
        except updater.VerificationError as exc:
            updater._log_update_event(
                "update.verify.failed",
                "download hash mismatch",
                level="SECURITY",
                asset=asset.name,
            )
            raise DownloadError("download sha256 does not match manifest") from exc
        except OSError as exc:
            raise DownloadError("downloaded file could not be verified") from exc
        _promote_verified_partial(
            partial,
            target,
            asset=asset,
            updater=updater,
        )
        updater._log_update_event(
            "update.download.completed",
            "download completed",
            asset=asset.name,
        )
        return target

    def _download_pinned(
        self,
        asset: Any,
        headers: Mapping[str, str],
        consume: Callable[[bytes], None],
        validate_response: Callable[[int, Mapping[str, str]], None],
        *,
        resume_from: int,
    ) -> None:
        updater = _updater_module()
        network_budget = asset.size - resume_from + DEFAULT_MAX_UPDATER_REDIRECT_BYTES
        transport = build_updater_transport(
            timeout=self.timeout_seconds,
            max_response_bytes=network_budget,
            allowed_hosts=self.allowed_hosts,
            validate_url=updater.validate_asset_url,
            transport_factory=updater.PinnedTransport,
        )
        expected_statuses = {200, 206} if resume_from else {200}
        result = transport.request_to_sink(
            "GET",
            asset.url,
            headers=headers,
            sink=consume,
            response_validator=validate_response,
            accepted_statuses=expected_statuses,
            max_redirects=5,
            max_total_bytes=network_budget,
        )
        if result.status_code not in expected_statuses:
            raise DownloadError(f"download failed with HTTP {result.status_code}")

    def _download_injected(
        self,
        asset: Any,
        headers: Mapping[str, str],
        consume: Callable[[bytes], None],
        validate_response: Callable[[int, Mapping[str, str]], None],
        *,
        opener: Callable[..., Any],
        partial: Path,
    ) -> None:
        request = urllib.request.Request(asset.url, headers=dict(headers))
        response: Any | None = None
        response_error: BaseException | None = None
        try:
            try:
                response = opener(request, timeout=self.timeout_seconds)
            except urllib.error.HTTPError as exc:
                raise DownloadError(f"download failed with HTTP {exc.code}") from exc
            except (OSError, TimeoutError, urllib.error.URLError) as exc:
                raise DownloadError(
                    "download network request failed",
                    keep_partial=_safe_regular_file_exists(
                        partial,
                        approved_root=partial.parent,
                    ),
                    retryable=True,
                ) from exc
            validate_response(
                response_status(response, error_factory=DownloadError),
                getattr(response, "headers", {}),
            )
            while True:
                try:
                    chunk = response.read(1024 * 256)
                except (OSError, TimeoutError, urllib.error.URLError) as exc:
                    raise DownloadError(
                        "download network stream failed",
                        keep_partial=_safe_regular_file_exists(
                            partial,
                            approved_root=partial.parent,
                        ),
                        retryable=True,
                    ) from exc
                if not chunk:
                    break
                consume(chunk)
        except BaseException as exc:
            response_error = exc
            raise
        finally:
            if response is not None:
                run_cleanup_actions(
                    response_error,
                    ((_response_close_action(response), "download response cleanup failed"),),
                )


def _promote_verified_partial(
    partial: Path,
    target: Path,
    *,
    asset: Any,
    updater: Any,
) -> None:
    """Publish a path generation, then verify the authoritative target."""

    try:
        replace_regular_file(partial, target, approved_root=target.parent)
    except OSError as exc:
        raise DownloadError("verified download could not be promoted") from exc
    try:
        updater._verify_file_hash(target, asset)
    except updater.VerificationError as exc:
        raise DownloadError(
            "promoted download final target does not match manifest"
        ) from exc
    except OSError as exc:
        raise DownloadError(
            "promoted download final target could not be verified"
        ) from exc


def _cleanup_partial(
    partial: Path,
    primary_error: BaseException,
    *,
    approved_root: Path,
) -> None:
    """Retain an ambiguous partial generation for age-bound recovery.

    A pathname cannot prove that the object currently at that name is the
    generation opened by the failed request.  Deleting by name here could
    therefore remove a later download's file.
    """

    del partial, primary_error, approved_root


def _truncate_partial(partial: Path, *, approved_root: Path) -> None:
    """Reset one verified partial generation through its opened descriptor."""

    handle = open_regular_binary(
        partial,
        approved_root=approved_root,
        mode="wb",
    )
    run_typed_cleanup_actions(
        None,
        ((handle.close, "partial download reset handle cleanup failed"),),
        error_factory=DownloadError,
        failure_message="partial download reset handle cleanup failed",
    )


def _safe_regular_file_exists(path: Path, *, approved_root: Path) -> bool:
    try:
        candidate = ensure_regular_or_missing(path, approved_root=approved_root)
        return candidate.exists()
    except OSError:
        return False


def _response_close_action(response: Any) -> Callable[[], object]:
    def close() -> object:
        candidate = getattr(response, "close", None)
        if callable(candidate):
            return candidate()
        return None

    return close


def _updater_module() -> Any:
    from app.services import secure_updater

    return secure_updater
