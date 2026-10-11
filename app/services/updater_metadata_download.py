"""Bounded persistence for signed updater metadata assets."""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.services.secure_updater import (
    DownloadError,
    ManifestError,
    MetadataNotFoundError,
    open_trusted_url,
    validate_asset_url,
)
from app.services.github_release_pages import response_final_url
from app.services.updater_atomic_io import atomic_write_bytes

_ALLOWED_METADATA_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


def download_metadata_file(
    url: str,
    target: Path,
    *,
    timeout: float = 8.0,
    max_bytes: int = 2_000_000,
    open_url: Callable[..., Any] = open_trusted_url,
    validate_url: Callable[..., None] = validate_asset_url,
    write_bytes: Callable[[Path, bytes], object] = atomic_write_bytes,
    final_url: Callable[[Any, str], str] = response_final_url,
) -> Path:
    """Download one trusted metadata asset and atomically persist a bounded body."""

    try:
        validate_url(url, _ALLOWED_METADATA_HOSTS)
    except ManifestError as exc:
        raise DownloadError(f"metadata URL must use trusted HTTPS: {exc}") from exc
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "UniversalCrawlerPro/update-check"},
    )
    try:
        with open_url(
            request,
            timeout=timeout,
            allowed_hosts=_ALLOWED_METADATA_HOSTS,
            max_response_bytes=max_bytes + 1,
        ) as response:
            validate_url(final_url(response, url), _ALLOWED_METADATA_HOSTS)
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise MetadataNotFoundError(
                "metadata download failed with HTTP 404"
            ) from exc
        raise DownloadError(f"metadata download failed with HTTP {exc.code}") from exc
    except ManifestError as exc:
        raise DownloadError(f"metadata URL must use trusted HTTPS: {exc}") from exc
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise DownloadError(f"metadata download failed: {exc}") from exc
    if len(data) > max_bytes:
        raise DownloadError("metadata file exceeds size limit")
    try:
        write_bytes(target, data)
    except OSError as exc:
        raise DownloadError("metadata file could not be persisted safely") from exc
    return target
