"""以 N_m3u8DL-RE 为主、yt-dlp 为 MissAV 兜底的 HLS 下载器。"""

from __future__ import annotations

import base64
import io
import os
import re
import stat
import subprocess
import tempfile
import time
import urllib.parse
import threading
from pathlib import Path
from typing import Any, Callable

from app.config import DEFAULT_USER_AGENT, cfg, effective_download_retry_count
from app.core.anti_detection import build_browser_anti_detection
from app.debug_logger import debug_logger
from shared import playwright_network_guard
from app.exceptions import DownloaderStoppedError, ExternalToolError, ExternalToolNotFoundError
from app.models import VideoItem
from shared.network.pinned_transport import PinnedTransport, canonicalize_request_target
from shared.runtime_options import PUBLIC_DOMAIN_POLICY, DomainPolicyEngine, DomainPolicyViolation
from shared.subprocess_env import isolated_media_subprocess_env

from .base import (
    BaseDownloader,
    ProgressCallback,
    StopCheck,
    TransferRateLimiter,
    assert_publishable_media,
)
from . import hls_proxy as hls_proxy_utils
from .hls_proxy import _LocalHlsProxy
from .nm3u8_progress import _Nm3u8OutputProgress
from .external import (
    ExternalToolRunner,
    FFmpegExternalTool,
    NM3U8DLREExternalTool,
    build_hidden_startupinfo,
    build_no_window_flags,
)
from .m3u8_ytdlp import Nm3u8YtDlpSupport

try:
    from playwright.sync_api import Error as PlaywrightError
except ImportError:  # pragma: no cover - 可选浏览器回退路径
    PlaywrightError = RuntimeError


class N_m3u8DL_RE_Downloader(Nm3u8YtDlpSupport, BaseDownloader):
    """使用 N_m3u8DL-RE 下载 HLS；遇到限制更严的 MissAV CDN 时启用兜底链路。"""

    NM3U8_TEMP_ROOT_NAME = ".ucp-nm3u8-tmp"
    NM3U8_TEMP_DIR_PREFIX = "ucp-"
    NM3U8_SWEEP_ENTRY_LIMIT = 4096
    _OWNED_MARKER_NAME = ".ucp-owned"
    _OWNED_MARKER_TEXT = "universal-crawler-pro\n"

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        """lstat 识别符号链接、重解析点和目录联接，不跟随目标。"""
        try:
            st = os.lstat(path)
        except OSError:
            return True
        if stat.S_ISLNK(st.st_mode):
            return True
        attributes = int(getattr(st, "st_file_attributes", 0) or 0)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if attributes & reparse_flag:
            return True
        isjunction = getattr(os.path, "isjunction", None)
        if callable(isjunction):
            try:
                if isjunction(path):
                    return True
            except OSError:
                return True
        return False

    @classmethod
    def _remove_owned_tree(cls, path: Path) -> None:
        """删除普通目录树。遇到重解析点或目录联接时拒绝，避免 shutil.rmtree 跟进去。"""
        if cls._is_reparse_point(path):
            raise OSError(f"refuse reparse point: {path}")
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            os.remove(path)
            return
        with os.scandir(path) as entries:
            children = [Path(entry.path) for entry in entries]
        for child in children:
            if cls._is_reparse_point(child):
                raise OSError(f"refuse reparse point: {child}")
            cls._remove_owned_tree(child)
        os.rmdir(path)

    @classmethod
    def _mark_owned_workspace(cls, path: Path) -> None:
        marker = path / cls._OWNED_MARKER_NAME
        marker.write_text(cls._OWNED_MARKER_TEXT, encoding="ascii")

    @classmethod
    def _has_owned_marker(cls, path: Path) -> bool:
        marker = path / cls._OWNED_MARKER_NAME
        try:
            if cls._is_reparse_point(marker):
                return False
            st = os.lstat(marker)
        except OSError:
            return False
        if not stat.S_ISREG(st.st_mode):
            return False
        try:
            return marker.read_text(encoding="ascii") == cls._OWNED_MARKER_TEXT
        except OSError:
            return False

    @classmethod
    def is_available(cls) -> bool:
        return NM3U8DLREExternalTool.is_available() or cls._python_hls_fallback_available()

    @staticmethod
    def _python_hls_fallback_available() -> bool:
        try:
            import m3u8  # noqa: F401
            from curl_cffi import requests as curl_requests  # noqa: F401
        except ImportError:
            return False
        return True

    @classmethod
    def is_m3u8_url(cls, url: str, content_type: str | None = None) -> bool:
        return NM3U8DLREExternalTool.is_m3u8_url(url, content_type)

    def download(
        self,
        video_item: VideoItem,
        save_path: str,
        progress_callback: ProgressCallback,
        check_stop_func: StopCheck,
    ) -> None:
        url = video_item.url
        domain_policy = self._domain_policy_for_item(video_item)
        if domain_policy is not None:
            domain_policy.require_public_url(url)
        trace_id = video_item.meta.get("trace_id")
        user_agent_source = video_item.source or "douyin"
        ua = self._resolve_runtime_user_agent(
            video_item,
            source=user_agent_source,
            configured_user_agent=cfg.get(user_agent_source, "user_agent", DEFAULT_USER_AGENT),
        )
        referer = video_item.meta.get("referer", "https://www.douyin.com/")
        proxy = video_item.meta.get("proxy")
        headers = self._headers_from_meta(video_item, ua, referer)
        thread_count = video_item.meta.get("m3u8_thread_count")
        if str(video_item.source or "").lower() == "missav" and not thread_count:
            thread_count = 16
        auto_retry = effective_download_retry_count(
            cfg.get("download", "max_retries", 3),
            cfg.get("download", "auto_retry", True),
        ) > 0

        self._emit_progress(progress_callback, 0)
        download_succeeded = False
        browser_fallback_error: Exception | None = None

        # MissAV 的 surrit CDN 对请求头/浏览器指纹敏感，优先尝试更像浏览器的下载路径。
        if self._should_try_nm3u8_first(video_item):
            executable = NM3U8DLREExternalTool.resolve_executable()
            if executable:
                try:
                    external_headers = self._headers_for_nm3u8_external(video_item, headers, ua, referer)
                    self._download_with_nm3u8_external(
                        video_item,
                        save_path,
                        executable,
                        ua,
                        referer,
                        proxy,
                        external_headers,
                        thread_count,
                        progress_callback,
                        check_stop_func,
                    )
                    download_succeeded = True
                    self._emit_progress(progress_callback, 100, bytes_downloaded=self._downloaded_file_size_hint(save_path))
                    return
                except DownloaderStoppedError:
                    raise
                except (OSError, RuntimeError, ValueError, ExternalToolError, ExternalToolNotFoundError) as external_exc:
                    debug_logger.log_exception(
                        "N_m3u8DL_RE_Downloader",
                        "missav_nm3u8_first_error",
                        external_exc,
                        context={"title": video_item.title, "save_path": save_path, "source_url": url},
                        trace_id=trace_id,
                    )
                    if not auto_retry:
                        self._cleanup_external_temp_files(save_path)
                        raise ExternalToolError(f"N_m3u8DL-RE failed: {external_exc}") from external_exc

        if self._should_try_curl_cffi_first(video_item):
            try:
                debug_logger.log(
                    component="N_m3u8DL_RE_Downloader",
                    action="curl_cffi_hls_start",
                    message="Trying curl_cffi browser-impersonated HLS download for MissAV",
                    status_code="M3U8_CURL_CFFI_START",
                    details={"title": video_item.title, "save_path": save_path, "source_url": url},
                    trace_id=trace_id,
                )
                self._download_with_curl_cffi_hls(
                    video_item,
                    save_path,
                    headers,
                    proxy,
                    progress_callback,
                    check_stop_func,
                )
                download_succeeded = True
                self._emit_progress(progress_callback, 100)
                return
            except DownloaderStoppedError:
                raise
            except (OSError, RuntimeError, ValueError, ExternalToolError, ExternalToolNotFoundError) as curl_exc:
                debug_logger.log_exception(
                    "N_m3u8DL_RE_Downloader",
                    "curl_cffi_hls_error",
                    curl_exc,
                    context={"title": video_item.title, "save_path": save_path, "source_url": url},
                    trace_id=trace_id,
                )
                if not auto_retry:
                    self._cleanup_external_temp_files(save_path)
                    raise ExternalToolError(f"curl_cffi HLS failed: {curl_exc}") from curl_exc
                try:
                    debug_logger.log(
                        component="N_m3u8DL_RE_Downloader",
                        action="playwright_hls_start",
                        message="Trying Playwright browser-context HLS download for MissAV",
                        status_code="M3U8_PLAYWRIGHT_START",
                        details={"title": video_item.title, "save_path": save_path, "source_url": url},
                        trace_id=trace_id,
                    )
                    self._download_with_playwright_hls(
                        video_item,
                        save_path,
                        headers,
                        proxy,
                        progress_callback,
                        check_stop_func,
                    )
                    download_succeeded = True
                    self._emit_progress(progress_callback, 100)
                    return
                except DownloaderStoppedError:
                    raise
                except (OSError, RuntimeError, ValueError, ExternalToolError, ExternalToolNotFoundError) as browser_exc:
                    browser_fallback_error = browser_exc
                    debug_logger.log_exception(
                        "N_m3u8DL_RE_Downloader",
                        "playwright_hls_error",
                        browser_exc,
                        context={"title": video_item.title, "save_path": save_path, "source_url": url},
                        trace_id=trace_id,
                    )

        if self._should_try_curl_cffi_first(video_item) and browser_fallback_error is not None:
            if self._should_skip_network_playlist_fallback(video_item):
                debug_logger.log(
                    component="N_m3u8DL_RE_Downloader",
                    action="skip_network_playlist_fallback",
                    level="WARN",
                    message="MissAV browser HLS failed after cached playlist; skipping network playlist fallback that would re-hit 403",
                    status_code="M3U8_SKIP_NETWORK_PLAYLIST_FALLBACK",
                    details={
                        "title": video_item.title,
                        "save_path": save_path,
                        "source_url": url,
                        "playlist_cache_keys": sorted(self._playlist_cache_from_meta(video_item)),
                        "browser_error": str(browser_fallback_error),
                    },
                    trace_id=trace_id,
                )
                raise ExternalToolError(f"MissAV browser HLS failed after cached playlist: {browser_fallback_error}") from browser_fallback_error
            try:
                debug_logger.log(
                    component="N_m3u8DL_RE_Downloader",
                    action="yt_dlp_fallback_start",
                    message="MissAV browser HLS failed; trying yt-dlp before skipping N_m3u8DL-RE",
                    status_code="M3U8_YTDLP_AFTER_BROWSER",
                    details={"title": video_item.title, "save_path": save_path, "source_url": url},
                    trace_id=trace_id,
                )
                self._download_with_yt_dlp_fallback(
                    video_item,
                    save_path,
                    headers,
                    proxy,
                    progress_callback,
                    check_stop_func,
                )
                download_succeeded = True
                self._emit_progress(progress_callback, 100)
                return
            except DownloaderStoppedError:
                raise
            except (OSError, RuntimeError, ValueError, ExternalToolError, ExternalToolNotFoundError) as fallback_exc:
                debug_logger.log_exception(
                    "N_m3u8DL_RE_Downloader",
                    "yt_dlp_after_browser_error",
                    fallback_exc,
                    context={"title": video_item.title, "save_path": save_path, "source_url": url},
                    trace_id=trace_id,
                )
                raise ExternalToolError(
                    f"MissAV browser HLS failed: {browser_fallback_error}; yt-dlp fallback failed: {fallback_exc}"
                ) from fallback_exc

        executable = NM3U8DLREExternalTool.resolve_executable()
        if not executable:
            if self._should_try_yt_dlp_fallback(video_item):
                self._download_with_yt_dlp_fallback(
                    video_item,
                    save_path,
                    headers,
                    proxy,
                    progress_callback,
                    check_stop_func,
                )
                download_succeeded = True
                self._emit_progress(progress_callback, 100)
                return
            raise ExternalToolNotFoundError("N_m3u8DL-RE executable not found")

        try:
            self._download_with_nm3u8_external(
                video_item,
                save_path,
                executable,
                ua,
                referer,
                proxy,
                headers,
                thread_count,
                progress_callback,
                check_stop_func,
            )
            download_succeeded = True
            try:
                self._emit_progress(progress_callback, 100)
            except Exception as exc:
                debug_logger.log_exception(
                    "N_m3u8DL_RE_Downloader",
                    "progress_callback_error",
                    exc,
                    context={"title": video_item.title, "save_path": save_path},
                    trace_id=trace_id,
                )
            debug_logger.log(
                component="N_m3u8DL_RE_Downloader",
                action="download_finished",
                message="N_m3u8DL-RE download finished",
                status_code="M3U8_OK",
                details={"title": video_item.title, "save_path": save_path},
                trace_id=trace_id,
            )
        except DownloaderStoppedError:
            raise
        except (OSError, RuntimeError, ValueError, ExternalToolError) as exc:
            debug_logger.log_exception(
                "N_m3u8DL_RE_Downloader",
                "download_error",
                exc,
                context={"title": video_item.title, "save_path": save_path, "source_url": url},
                trace_id=trace_id,
            )
            if auto_retry and self._should_try_yt_dlp_fallback(video_item):
                try:
                    debug_logger.log(
                        component="N_m3u8DL_RE_Downloader",
                        action="yt_dlp_fallback_start",
                        message="N_m3u8DL-RE failed; trying yt-dlp impersonation fallback",
                        status_code="M3U8_YTDLP_FALLBACK",
                        details={"title": video_item.title, "save_path": save_path, "source_url": url},
                        trace_id=trace_id,
                    )
                    self._download_with_yt_dlp_fallback(
                        video_item,
                        save_path,
                        headers,
                        proxy,
                        progress_callback,
                        check_stop_func,
                    )
                    download_succeeded = True
                    try:
                        self._emit_progress(progress_callback, 100)
                    except Exception as callback_exc:
                        debug_logger.log_exception(
                            "N_m3u8DL_RE_Downloader",
                            "yt_dlp_progress_callback_error",
                            callback_exc,
                            context={"title": video_item.title, "save_path": save_path},
                            trace_id=trace_id,
                        )
                    return
                except DownloaderStoppedError:
                    raise
                except (OSError, RuntimeError, ValueError, ExternalToolError) as fallback_exc:
                    debug_logger.log_exception(
                        "N_m3u8DL_RE_Downloader",
                        "yt_dlp_fallback_error",
                        fallback_exc,
                        context={"title": video_item.title, "save_path": save_path, "source_url": url},
                        trace_id=trace_id,
                    )
                    raise ExternalToolError(
                        f"N_m3u8DL-RE failed: {exc}; yt-dlp fallback failed: {fallback_exc}"
                    ) from fallback_exc
            raise ExternalToolError(f"N_m3u8DL-RE failed: {exc}") from exc
        finally:
            if not download_succeeded:
                # 外部工具可能直接写目标目录旁的 .part/.tmp，失败时做一次目标名前缀清理。
                self._cleanup_external_temp_files(save_path)

    @staticmethod
    def _should_try_yt_dlp_fallback(video_item: VideoItem) -> bool:
        return str(video_item.source or "").lower() == "missav"

    @classmethod
    def _should_try_curl_cffi_first(cls, video_item: VideoItem) -> bool:
        if str(video_item.source or "").lower() != "missav":
            return False
        try:
            host = urllib.parse.urlparse(str(video_item.url or "")).netloc.lower()
        except (TypeError, ValueError):
            host = ""
        return host == "surrit.com" or host.endswith(".surrit.com")

    @classmethod
    def _should_try_nm3u8_first(cls, video_item: VideoItem) -> bool:
        return cls._should_try_curl_cffi_first(video_item) and not bool(video_item.meta.get("force_python_hls"))

    @classmethod
    def _headers_for_nm3u8_external(
        cls,
        video_item: VideoItem,
        headers: dict[str, str],
        user_agent: str,
        referer: str,
    ) -> dict[str, str]:
        if not cls._should_try_curl_cffi_first(video_item):
            return dict(headers)
        clean = {
            "User-Agent": str(headers.get("User-Agent") or user_agent or DEFAULT_USER_AGENT),
        }
        referer_text = str(referer or headers.get("Referer") or video_item.meta.get("referer") or "").strip()
        if referer_text:
            clean["Referer"] = referer_text
        cookie = str(headers.get("Cookie") or video_item.meta.get("cookie") or "").strip()
        if cookie:
            clean["Cookie"] = cookie
        return clean

    @classmethod
    def _create_nm3u8_temp_workspace(cls, save_path: str) -> Path:
        target = Path(save_path).expanduser()
        parent = target.parent if str(target.parent) else Path.cwd()
        root = parent / cls.NM3U8_TEMP_ROOT_NAME
        root.mkdir(parents=True, exist_ok=True)
        if cls._is_reparse_point(root):
            raise OSError(f"refuse reparse point: {root}")
        # 工作目录名带目标 stem，便于人工排查；真正的删除判断仍依赖父目录和前缀白名单。
        safe_stem = re.sub(r"[^0-9A-Za-z_.-]+", "_", target.stem).strip("._-")[:36] or "download"
        created = Path(tempfile.mkdtemp(prefix=f"{cls.NM3U8_TEMP_DIR_PREFIX}{safe_stem}-", dir=str(root)))
        cls._mark_owned_workspace(created)
        return created

    @classmethod
    def _is_owned_nm3u8_temp_workspace(cls, temp_dir: str | os.PathLike[str] | None) -> bool:
        if not temp_dir:
            return False
        try:
            path = Path(temp_dir)
            if cls._is_reparse_point(path) or cls._is_reparse_point(path.parent):
                return False
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return path.parent.name == cls.NM3U8_TEMP_ROOT_NAME and path.name.startswith(cls.NM3U8_TEMP_DIR_PREFIX)

    @classmethod
    def _cleanup_nm3u8_temp_workspace(
        cls,
        temp_dir: str | os.PathLike[str] | None,
        *,
        trace_id: str | None = None,
    ) -> None:
        if not temp_dir:
            return
        if not cls._is_owned_nm3u8_temp_workspace(temp_dir):
            # 这是防误删边界：只删除本下载器创建的 .ucp-nm3u8-tmp/ucp-* 工作目录。
            debug_logger.log(
                component="N_m3u8DL_RE_Downloader",
                action="skip_unowned_tmp_cleanup",
                level="WARN",
                message="Skip N_m3u8DL-RE temp cleanup because the directory is outside the owned workspace",
                status_code="M3U8_TMP_CLEANUP_SKIPPED",
                details={"tmp_dir": str(temp_dir)},
                trace_id=trace_id,
            )
            return

        path = Path(temp_dir)
        last_error: OSError | None = None
        for attempt in range(3):
            try:
                if cls._is_reparse_point(path):
                    raise OSError(f"refuse reparse point: {path}")
                try:
                    os.lstat(path)
                except FileNotFoundError:
                    last_error = None
                    break
                cls._remove_owned_tree(path)
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                time.sleep(0.1 * (attempt + 1))
        if last_error is not None:
            debug_logger.log_exception(
                "N_m3u8DL_RE_Downloader",
                "tmp_workspace_cleanup_error",
                last_error,
                details={"tmp_dir": str(temp_dir)},
                trace_id=trace_id,
            )
            return

        try:
            path.parent.rmdir()
        except OSError:
            pass

    @classmethod
    def _scandir_temp_root(cls, path: Path) -> list[Path]:
        """列举临时根。超过条目上限或列举失败时必须让调用方看到失败。"""
        children: list[Path] = []
        limit = cls.NM3U8_SWEEP_ENTRY_LIMIT
        with os.scandir(path) as entries:
            for entry in entries:
                if len(children) >= limit:
                    raise OSError(f"HLS temp root has more than {limit} entries: {path}")
                children.append(Path(entry.path))
        return children

    @classmethod
    def sweep_orphaned_workspaces(cls, download_dirs: list[str | os.PathLike[str]]) -> int:
        """应用启动后、任务运行前清理一次遗留的 HLS 工作目录。"""
        cleaned_count = 0
        errors: list[dict[str, str]] = []

        for raw_dir in download_dirs:
            try:
                base_dir = Path(raw_dir).expanduser()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                errors.append({"path": str(raw_dir), "error": str(exc)})
                debug_logger.log_exception(
                    "N_m3u8DL_RE_Downloader",
                    "orphan_workspace_sweep_path_error",
                    exc,
                    details={"path": str(raw_dir)},
                )
                continue

            try:
                if cls._is_reparse_point(base_dir):
                    continue
                try:
                    base_stat = os.lstat(base_dir)
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(base_stat.st_mode):
                    continue
                # 只扫本程序的受控根目录。不以 _curl_cffi_hls/_playwright_hls 结尾作为删除条件。
                candidates = [base_dir / cls.NM3U8_TEMP_ROOT_NAME]
            except OSError as exc:
                errors.append({"path": str(base_dir), "error": str(exc)})
                debug_logger.log_exception(
                    "N_m3u8DL_RE_Downloader",
                    "orphan_workspace_sweep_scan_error",
                    exc,
                    details={"path": str(base_dir)},
                )
                continue

            for path in candidates:
                try:
                    try:
                        root_stat = os.lstat(path)
                    except FileNotFoundError:
                        continue
                    # 首次启动或上次清理后，受控根目录不存在是正常情况。
                    # _is_reparse_point 对 stat 失败保守拒绝，不能用它判断目录存在性。
                    if cls._is_reparse_point(path):
                        errors.append({"path": str(path), "error": "refuse reparse point"})
                        continue
                    if not stat.S_ISDIR(root_stat.st_mode):
                        continue
                except OSError as exc:
                    errors.append({"path": str(path), "error": str(exc)})
                    debug_logger.log_exception(
                        "N_m3u8DL_RE_Downloader",
                        "orphan_workspace_sweep_stat_error",
                        exc,
                        details={"path": str(path)},
                    )
                    continue
                children = cls._scandir_temp_root(path)
                for child in children:
                    try:
                        if cls._is_reparse_point(child):
                            errors.append({"path": str(child), "error": "refuse reparse point"})
                            continue
                        try:
                            child_stat = os.lstat(child)
                        except OSError as exc:
                            errors.append({"path": str(child), "error": str(exc)})
                            continue
                        if not stat.S_ISDIR(child_stat.st_mode):
                            continue
                        if not child.name.startswith(cls.NM3U8_TEMP_DIR_PREFIX) or not cls._has_owned_marker(child):
                            continue
                        cls._remove_owned_tree(child)
                        cleaned_count += 1
                    except OSError as exc:
                        errors.append({"path": str(child), "error": str(exc)})
                        debug_logger.log_exception(
                            "N_m3u8DL_RE_Downloader",
                            "orphan_workspace_sweep_remove_error",
                            exc,
                            details={"path": str(child)},
                        )
                remaining = cls._scandir_temp_root(path)
                if not remaining and not cls._is_reparse_point(path):
                    try:
                        os.rmdir(path)
                    except OSError:
                        pass

        debug_logger.log(
            component="N_m3u8DL_RE_Downloader",
            action="orphan_workspace_sweep",
            message="Swept stale HLS workspaces at application startup",
            status_code="M3U8_TMP_SWEEP",
            details={"cleaned_count": cleaned_count, "errors": errors},
        )
        return cleaned_count

    def _download_with_nm3u8_external(
        self,
        video_item: VideoItem,
        save_path: str,
        executable: str,
        user_agent: str,
        referer: str,
        proxy: str | None,
        headers: dict[str, str],
        thread_count: str | int | None,
        progress_callback: ProgressCallback,
        check_stop_func: StopCheck,
    ) -> None:
        # 没有任务级公网策略时也不能把原始 URL 交给 N_m3u8DL-RE，它会自己做 DNS。
        del referer
        local_proxy = self._start_local_hls_proxy(video_item, headers, proxy)
        with hls_proxy_utils._hls_proxy_cleanup_scope(local_proxy.stop):
            local_headers = {"User-Agent": str(user_agent or DEFAULT_USER_AGENT)}
            self._run_nm3u8_external_command(
                video_item,
                save_path,
                executable,
                local_proxy.url,
                user_agent,
                "",
                None,
                local_headers,
                hls_proxy_utils._local_hls_proxy_thread_count(thread_count),
                progress_callback,
                check_stop_func,
                local_proxy=True,
                progress_provider=local_proxy.progress_snapshot,
            )

    def _run_nm3u8_external_command(
        self,
        video_item: VideoItem,
        save_path: str,
        executable: str,
        source_url: str,
        user_agent: str,
        referer: str,
        proxy: str | None,
        headers: dict[str, str],
        thread_count: str | int | None,
        progress_callback: ProgressCallback,
        check_stop_func: StopCheck,
        *,
        local_proxy: bool,
        progress_provider: Callable[[], tuple[int, int]] | None = None,
    ) -> None:
        trace_id = video_item.meta.get("trace_id")
        temp_workspace: Path | None = None
        process: subprocess.Popen | None = None
        output_reader: threading.Thread | None = None
        try:
            temp_workspace = self._create_nm3u8_temp_workspace(save_path)
            fragment_dir = temp_workspace / "fragments"
            fragment_dir.mkdir()
            suffix = Path(save_path).suffix or ".mp4"
            staged_output = temp_workspace / f"ucp-media{suffix}"
            cmd = NM3U8DLREExternalTool.build_download_command(
                executable,
                source_url,
                str(staged_output),
                user_agent,
                referer,
                proxy=proxy,
                extra_headers=headers,
                thread_count=thread_count,
                tmp_dir=str(fragment_dir),
            )
            debug_logger.log_command(
                component="N_m3u8DL_RE_Downloader",
                tool_name="N_m3u8DL-RE",
                command_args=[arg.replace(source_url, "<local-hls-capability-url>") if local_proxy and isinstance(arg, str) else arg for arg in cmd],
                message="Preparing N_m3u8DL-RE HLS download",
                context={
                    "title": video_item.title,
                    "save_path": save_path,
                    "source_url": video_item.url,
                    "local_hls_proxy": local_proxy,
                    "thread_count": thread_count,
                    "tmp_dir": str(temp_workspace),
                },
                trace_id=trace_id,
            )
            output_progress = _Nm3u8OutputProgress(default_progress=0)
            process = self._popen_nm3u8_process(cmd, build_no_window_flags())
            output_reader = self._start_nm3u8_output_reader(process, output_progress, trace_id)
            # 本地代理能统计真实转发字节，N_m3u8DL-RE 输出能提供阶段进度，两者取最大值减少回退感。
            combined_provider = self._combine_progress_providers(progress_provider, output_progress.snapshot)
            self._wait_external_process_with_file_progress(
                process,
                str(staged_output),
                check_stop_func,
                progress_callback,
                0,
                progress_provider=combined_provider,
                temp_paths=[fragment_dir],
            )
            if process.returncode != 0:
                self._join_nm3u8_output_reader(output_reader)
                output_reader = None
                output_progress.log_process_failure(executable, process.returncode, trace_id)
                raise ExternalToolError(f"N_m3u8DL-RE exited abnormally (Code: {process.returncode})")
            produced = self._locate_staged_media(staged_output)
            self._assert_nm3u8_output(produced, video_item)
            assert_publishable_media(str(produced), save_path)
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            os.replace(produced, save_path)
        finally:
            self._join_nm3u8_output_reader(output_reader)
            if process is not None and process.poll() is None:
                ExternalToolRunner.terminate_process(process)
            self._cleanup_nm3u8_temp_workspace(temp_workspace, trace_id=trace_id)

    @classmethod
    def _locate_staged_media(cls, staged: Path) -> Path:
        if not cls._is_reparse_point(staged):
            try:
                st = os.lstat(staged)
            except FileNotFoundError:
                st = None
            if st is not None and stat.S_ISREG(st.st_mode) and st.st_size > 0:
                return staged
        matches: list[Path] = []
        try:
            children = list(staged.parent.iterdir())
        except OSError as exc:
            raise ExternalToolError("N_m3u8DL-RE 退出码为 0，但没有非空输出") from exc
        for child in children:
            if cls._is_reparse_point(child) or not child.name.startswith(staged.stem):
                continue
            if child.suffix.lower() != ".mp4":
                continue
            try:
                child_stat = os.lstat(child)
            except OSError:
                continue
            if stat.S_ISREG(child_stat.st_mode) and child_stat.st_size > 0:
                matches.append(child)
        if not matches:
            raise ExternalToolError("N_m3u8DL-RE 退出码为 0，但输出不存在或为空文件")
        return max(matches, key=lambda item: os.lstat(item).st_size)

    @staticmethod
    def _assert_nm3u8_output(produced: Path, video_item: VideoItem) -> None:
        try:
            size = os.lstat(produced).st_size
        except OSError as exc:
            raise ExternalToolError("N_m3u8DL-RE 输出无法读取") from exc
        if size <= 0:
            raise ExternalToolError("N_m3u8DL-RE 退出码为 0，但输出为空文件")
        available, probed = FFmpegExternalTool.probe_duration_seconds(str(produced))
        if available and (probed is None or probed <= 0):
            raise ExternalToolError("ffprobe 未能确认 N_m3u8DL-RE 输出时长")
        expected = video_item.meta.get("duration") if isinstance(video_item.meta, dict) else None
        try:
            FFmpegExternalTool.assert_duration_if_expected(
                str(produced),
                float(expected) if isinstance(expected, (int, float)) and not isinstance(expected, bool) else None,
            )
        except DomainPolicyViolation as exc:
            raise ExternalToolError(f"N_m3u8DL-RE 输出时长与预期不一致: {exc}") from exc

    @staticmethod
    def _combine_progress_providers(
        first: Callable[[], tuple[int, int]] | None,
        second: Callable[[], tuple[int, int]] | None,
    ) -> Callable[[], tuple[int, int]] | None:
        providers = [provider for provider in (first, second) if provider is not None]
        if not providers:
            return None

        def combined() -> tuple[int, int]:
            progress_values: list[int] = []
            byte_values: list[int] = []
            for provider in providers:
                try:
                    progress, byte_count = provider()
                except Exception as exc:
                    debug_logger.log_exception(
                        "N_m3u8DL_RE_Downloader",
                        "combined_progress_provider_error",
                        exc,
                    )
                    continue
                progress_values.append(int(progress or 0))
                byte_values.append(int(byte_count or 0))
            return max(progress_values or [0]), max(byte_values or [0])

        return combined

    @staticmethod
    def _popen_nm3u8_process(cmd: list[str], creation_flags: int) -> subprocess.Popen:
        return subprocess.Popen(
            cmd,
            creationflags=creation_flags,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            startupinfo=build_hidden_startupinfo(),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=isolated_media_subprocess_env(),
        )

    @staticmethod
    def _start_nm3u8_output_reader(
        process: subprocess.Popen,
        progress: _Nm3u8OutputProgress,
        trace_id: str | None = None,
    ) -> threading.Thread | None:
        stream = getattr(process, "stdout", None)
        if not isinstance(stream, io.IOBase):
            return None

        def read_output() -> None:
            buffer: list[str] = []
            try:
                while True:
                    chunk = stream.read(1)
                    if not chunk:
                        break
                    if isinstance(chunk, bytes):
                        chunk = chunk.decode("utf-8", errors="replace")
                    if chunk in ("\r", "\n"):
                        if buffer:
                            progress.feed("".join(buffer))
                            buffer.clear()
                        continue
                    buffer.append(str(chunk))
                    if len(buffer) >= 4096:
                        progress.feed("".join(buffer))
                        buffer.clear()
                if buffer:
                    progress.feed("".join(buffer))
            except (OSError, RuntimeError, ValueError) as exc:
                debug_logger.log_exception(
                    "N_m3u8DL_RE_Downloader",
                    "nm3u8_output_reader_error",
                    exc,
                    trace_id=trace_id,
                )

        reader = threading.Thread(target=read_output, name="ucp-nm3u8-progress", daemon=True)
        reader.start()
        return reader

    @staticmethod
    def _join_nm3u8_output_reader(reader: threading.Thread | None) -> None:
        if reader is not None:
            reader.join(timeout=1.0)

    @classmethod
    def _should_use_local_hls_proxy(cls, video_item: VideoItem) -> bool:
        if cls._domain_policy_for_item(video_item) is not None:
        # 这是网络安全边界：任务元数据不得把公网任务重新切回外部工具直连，
        # 否则会绕过逐跳重定向与目标地址校验。
            return True
        return cls._should_try_curl_cffi_first(video_item) and not bool(video_item.meta.get("disable_local_hls_proxy"))

    def _start_local_hls_proxy(
        self,
        video_item: VideoItem,
        headers: dict[str, str],
        upstream_proxy: str | None,
    ) -> _LocalHlsProxy:
        proxy_headers = self._headers_for_hls_proxy(video_item, headers)
        # 外部工具只看到 loopback 地址；所有真实上游请求仍由代理逐跳执行公网校验。
        task_policy = self._domain_policy_for_item(video_item)
        domain_policy = task_policy or PUBLIC_DOMAIN_POLICY
        local_proxy = _LocalHlsProxy(
            self,
            str(video_item.url),
            proxy_headers,
            upstream_proxy,
            domain_policy=domain_policy,
            allow_upstream_proxy=task_policy is None,
        ).start()
        debug_logger.log(
            component="N_m3u8DL_RE_Downloader",
            action="local_hls_proxy_start",
            message="Started local HLS proxy for protected MissAV stream",
            status_code="M3U8_LOCAL_PROXY_START",
            details={"task_id": local_proxy.capability.task_id, "base_url": local_proxy.base_url},
            trace_id=video_item.meta.get("trace_id"),
        )
        return local_proxy

    @classmethod
    def _headers_for_hls_proxy(cls, video_item: VideoItem, headers: dict[str, str]) -> dict[str, str]:
        proxy_headers = {
            key: value
            for key, value in cls._headers_for_yt_dlp(headers).items()
            if str(key).lower() not in {"host", "content-length", "connection", "transfer-encoding"}
        }
        referer = str(video_item.meta.get("referer") or headers.get("Referer") or "").strip()
        if referer:
            proxy_headers["Referer"] = referer
        proxy_headers.setdefault("User-Agent", str(video_item.meta.get("ua") or headers.get("User-Agent") or DEFAULT_USER_AGENT))
        proxy_headers.setdefault("Accept", "*/*")
        proxy_headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9,en-CN;q=0.8,en;q=0.7")
        return proxy_headers

    @classmethod
    def _headers_for_hls_proxy_upstream(cls, upstream_url: str, headers: dict[str, str]) -> dict[str, str]:
        return hls_proxy_utils.build_hls_proxy_upstream_headers(upstream_url, headers)

    def _hls_proxy_fetch_upstream(
        self,
        upstream_url: str,
        headers: dict[str, str],
        upstream_proxy: str | None,
        *,
        domain_policy: DomainPolicyEngine | None = None,
    ) -> tuple[int, str, bytes, str, tuple[str, ...]]:
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as exc:
            raise ExternalToolNotFoundError("curl_cffi is required for local MissAV HLS proxy") from exc
        first_error: Exception | None = None
        for request_headers in self._hls_proxy_header_attempts(headers):
            try:
                response = self._hls_proxy_request_response(
                    curl_requests,
                    upstream_url,
                    request_headers,
                    upstream_proxy,
                    domain_policy=domain_policy,
                )
            except Exception as exc:
                first_error = exc
                continue
            status = int(getattr(response, "status_code", 0) or 0)
            if status in (200, 206):
                try:
                    content_type = str(getattr(response, "headers", {}).get("Content-Type", "") or "")
                    resolved_url = canonicalize_request_target(str(getattr(response, "url", "") or upstream_url)).url
                    chain = tuple(getattr(response, "redirect_chain", ()) or (resolved_url,))
                    return status, content_type, bytes(getattr(response, "content", b"") or b""), resolved_url, chain
                finally:
                    self._close_hls_proxy_response(response, upstream_url)
            self._close_hls_proxy_response(response, upstream_url)
            first_error = ExternalToolError(f"local HLS proxy upstream request failed ({status}) for {upstream_url}")
            if status != 403:
                break
        if first_error is not None:
            raise first_error
        raise ExternalToolError(f"local HLS proxy upstream request failed for {upstream_url}")

    def _hls_proxy_open_upstream(
        self,
        upstream_url: str,
        headers: dict[str, str],
        upstream_proxy: str | None,
        *,
        domain_policy: DomainPolicyEngine | None = None,
    ):
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as exc:
            raise ExternalToolNotFoundError("curl_cffi is required for local MissAV HLS proxy") from exc
        first_error: Exception | None = None
        for request_headers in self._hls_proxy_header_attempts(headers):
            response = None
            try:
                response = self._hls_proxy_request_response(
                    curl_requests,
                    upstream_url,
                    request_headers,
                    upstream_proxy,
                    domain_policy=domain_policy,
                )
            except Exception as exc:
                first_error = exc
                continue
            status = int(getattr(response, "status_code", 0) or 0)
            if status in (200, 206, 416):
                return response
            self._close_hls_proxy_response(response, upstream_url)
            first_error = ExternalToolError(f"local HLS proxy upstream request failed ({status}) for {upstream_url}")
            if status != 403:
                break
        if first_error is not None:
            raise first_error
        raise ExternalToolError(f"local HLS proxy upstream request failed for {upstream_url}")

    def _hls_proxy_request_response(
        self,
        curl_requests,
        upstream_url: str,
        headers: dict[str, str],
        upstream_proxy: str | None,
        *,
        domain_policy: DomainPolicyEngine | None,
        max_redirects: int = 5,
    ):
        """读取单个 HLS 资源，并在跟随前逐跳校验每次重定向。"""
        current_url = canonicalize_request_target(str(upstream_url)).url
        redirect_chain = [current_url]
        request_headers = hls_proxy_utils.filter_hls_outbound_headers(dict(headers), cross_origin=False)
        for redirect_count in range(max(0, int(max_redirects)) + 1):
            request_url, curl_options = hls_proxy_utils.prepare_hls_curl_request(
                current_url, domain_policy, upstream_proxy
            )
            response_body = hls_proxy_utils._BoundedHlsResponseBody(request_url)
            try:
                response = self._curl_cffi_get_response(
                    curl_requests,
                    request_url,
                    request_headers,
                    upstream_proxy,
                    allow_redirects=False,
                    content_callback=response_body.collect,
                    curl_options=curl_options,
                    header_callback=response_body.collect_header,
                )
            except Exception as exc:
                response_body.raise_if_oversized(exc)
                raise
            response_body.finalize(
                response,
                lambda: self._close_hls_proxy_response(response, request_url),
            )
            status = int(getattr(response, "status_code", 0) or 0)
            response_headers = getattr(response, "headers", {}) or {}
            location = response_headers.get("Location") or response_headers.get("location")
            if status not in DomainPolicyEngine.REDIRECT_STATUS_CODES or not location:
                response.redirect_chain = tuple(redirect_chain)
                return response
            try:
                target_url = canonicalize_request_target(urllib.parse.urljoin(request_url, str(location))).url
                if redirect_count >= max_redirects:
                    raise ExternalToolError("local HLS proxy redirect limit exceeded")
            except Exception:
                self._close_hls_proxy_response(response, current_url)
                raise

            if self._url_origin(request_url) != self._url_origin(target_url):
                request_headers = hls_proxy_utils.filter_hls_outbound_headers(
                    request_headers,
                    cross_origin=True,
                )
            self._close_hls_proxy_response(response, request_url)
            current_url = target_url
            redirect_chain.append(target_url)
        raise ExternalToolError("local HLS proxy redirect limit exceeded")

    @staticmethod
    def _url_origin(url: str) -> tuple[str, str, int | None]:
        parsed = urllib.parse.urlsplit(str(url or ""))
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port is None:
            port = 443 if parsed.scheme.lower() == "https" else 80 if parsed.scheme.lower() == "http" else None
        return parsed.scheme.lower(), (parsed.hostname or "").lower(), port

    @staticmethod
    def _close_hls_proxy_response(response, _url: str) -> None:
        close = getattr(response, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception as exc:
            hls_proxy_utils._log_diagnostic_best_effort("M3U8Downloader", "close_redirect_response", exc)

    @staticmethod
    def _hls_proxy_header_attempts(headers: dict[str, str]) -> list[dict[str, str]]:
        primary = dict(headers)
        attempts = [primary]
        if any(str(key).lower() == "cookie" for key in primary):
            without_cookie = {key: value for key, value in primary.items() if str(key).lower() != "cookie"}
            attempts.append(without_cookie)
        return attempts

    @staticmethod
    def _curl_cffi_get_response(
        curl_requests,
        url: str,
        headers: dict[str, str],
        proxy: str | None,
        *,
        stream: bool = False,
        allow_redirects: bool = True,
        curl_options: dict[Any, Any] | None = None,
        content_callback: Callable[[bytes], int] | None = None,
        header_callback: Callable[[bytes], int] | None = None,
    ):
        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": 60,
            "impersonate": "chrome",
            "allow_redirects": bool(allow_redirects),
        }
        if proxy:
            kwargs["proxy"] = proxy
        if curl_options:
            kwargs["curl_options"] = curl_options
        if content_callback is not None:
            kwargs["content_callback"] = content_callback
        if stream:
            kwargs["stream"] = True
        return hls_proxy_utils.perform_hls_curl_get(curl_requests, url, kwargs, header_callback)

    @staticmethod
    def _looks_like_hls_playlist(url: str, content_type: str, body: bytes) -> bool:
        return hls_proxy_utils.looks_like_hls_playlist(url, content_type, body)

    @staticmethod
    def _looks_like_hls_playlist_url(url: str, content_type: str) -> bool:
        return hls_proxy_utils.looks_like_hls_playlist_url(url, content_type)

    @staticmethod
    def _response_content_bytes(response) -> bytes:
        return hls_proxy_utils.response_content_bytes(response)

    @staticmethod
    def _response_iter_bytes(response, chunk_size: int = 256 * 1024):
        yield from hls_proxy_utils.response_iter_bytes(response, chunk_size=chunk_size)

    @staticmethod
    def _looks_like_hls_media_resource(url: str) -> bool:
        return hls_proxy_utils.looks_like_hls_media_resource(url)

    @staticmethod
    def _count_hls_media_entries(playlist_text: str) -> int:
        return hls_proxy_utils.count_hls_media_entries(playlist_text)

    @classmethod
    def _rewrite_hls_playlist_for_proxy(cls, playlist_text: str, playlist_url: str, local_url_for) -> str:
        return hls_proxy_utils.rewrite_hls_playlist_for_proxy(playlist_text, playlist_url, local_url_for)

    @staticmethod
    def _rewrite_hls_attribute_uris(line: str, base_url: str, local_url_for) -> str:
        return hls_proxy_utils.rewrite_hls_attribute_uris(line, base_url, local_url_for)


    def _wait_external_process_with_file_progress(
        self,
        process: subprocess.Popen,
        save_path: str,
        check_stop_func: StopCheck,
        progress_callback: ProgressCallback | None,
        progress_value: int,
        *,
        progress_provider: Callable[[], tuple[int, int]] | None = None,
        temp_paths: list[str | os.PathLike[str]] | None = None,
    ) -> None:
        started_at = time.time()
        last_emit_at = 0.0
        while process.poll() is None:
            if check_stop_func():
                ExternalToolRunner.terminate_process(process)
                raise DownloaderStoppedError("Download stopped by user")
            time.sleep(0.5)
            now = time.time()
            if now - last_emit_at >= 1.0:
                last_emit_at = now
                provider_progress = progress_value
                provider_bytes = 0
                if progress_provider is not None:
                    try:
                        provider_progress, provider_bytes = progress_provider()
                    except Exception as exc:
                        debug_logger.log_exception(
                            "N_m3u8DL_RE_Downloader",
                            "external_progress_provider_error",
                            exc,
                        )
                        provider_progress, provider_bytes = progress_value, 0
                bytes_downloaded = max(
                    int(provider_bytes or 0),
                    self._downloaded_file_size_hint(save_path, since=started_at, extra_paths=temp_paths),
                )
                self._emit_progress(
                    progress_callback,
                    provider_progress,
                    bytes_downloaded=bytes_downloaded,
                )

    @staticmethod
    def _downloaded_file_size_hint(
        save_path: str,
        *,
        since: float | None = None,
        extra_paths: list[str | os.PathLike[str]] | None = None,
    ) -> int:
        target = Path(save_path)
        total = 0
        candidates: list[Path] = []
        try:
            if target.exists():
                candidates.append(target)
            for path in extra_paths or []:
                candidates.append(Path(path))
            candidates.extend(path for path in target.parent.glob(f"{target.stem}*") if path != target)
            if since is not None:
                for path in target.parent.iterdir():
                    if path in candidates:
                        continue
                    if path.name == N_m3u8DL_RE_Downloader.NM3U8_TEMP_ROOT_NAME:
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    if stat.st_mtime >= since - 2 and (
                        path.is_dir() or path.suffix.lower() in {".ts", ".m4s", ".mp4", ".tmp", ".part"}
                    ):
                        candidates.append(path)
        except OSError:
            return 0
        seen: set[Path] = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=False)
            except OSError:
                resolved = candidate
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                if candidate.is_file():
                    total += candidate.stat().st_size
                elif candidate.is_dir():
                    for child in candidate.rglob("*"):
                        if child.is_file():
                            total += child.stat().st_size
            except OSError:
                continue
        return int(total)

    @staticmethod
    def _playlist_cache_from_meta(video_item: VideoItem) -> dict[str, str]:
        cache = video_item.meta.get("playlist_cache")
        if not isinstance(cache, dict):
            return {}
        result: dict[str, str] = {}
        for key, value in cache.items():
            key_text = str(key or "").strip()
            value_text = str(value or "")
            if key_text and value_text:
                result[key_text] = value_text
        return result

    @staticmethod
    def _playlist_text_from_cache(cache: dict[str, str], url: str) -> str | None:
        if not cache:
            return None
        url_text = str(url or "")
        candidates = [url_text, url_text.split("#", 1)[0]]
        for candidate in candidates:
            if candidate in cache:
                return cache[candidate]
        parsed_target = urllib.parse.urlparse(url_text)
        for cached_url, text in cache.items():
            parsed_cached = urllib.parse.urlparse(str(cached_url or ""))
            if (
                parsed_cached.scheme == parsed_target.scheme
                and parsed_cached.netloc == parsed_target.netloc
                and parsed_cached.path == parsed_target.path
                and parsed_cached.query == parsed_target.query
            ):
                return text
        return None

    @classmethod
    def _should_skip_network_playlist_fallback(cls, video_item: VideoItem) -> bool:
        return cls._should_try_curl_cffi_first(video_item) and bool(cls._playlist_cache_from_meta(video_item))

    @classmethod
    def _playwright_launch_kwargs(cls, video_item: VideoItem, proxy: str | None) -> dict[str, Any]:
        show_browser_window = bool(cfg.get("common", "show_browser_window", True))
        kwargs: dict[str, Any] = {
            "headless": not (show_browser_window and cls._should_try_curl_cffi_first(video_item)),
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if proxy:
            kwargs["proxy"] = {"server": proxy}
        return kwargs

    def _download_with_curl_cffi_hls(
        self,
        video_item: VideoItem,
        save_path: str,
        headers: dict[str, str],
        proxy: str | None,
        progress_callback: ProgressCallback,
        check_stop_func: StopCheck,
    ) -> None:
        try:
            import m3u8
            from curl_cffi import requests as curl_requests
        except ImportError as exc:
            raise ExternalToolNotFoundError("curl_cffi and m3u8 are required for MissAV browser HLS fallback") from exc

        target = Path(save_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_dir = self._create_nm3u8_temp_workspace(save_path)
        # Python 回退路径（fallback）先合并成裸 TS，再根据目标后缀决定是否 remux，失败时可整目录清理。
        raw_path = temp_dir / "ucp-media.ts"
        domain_policy = self._domain_policy_for_item(video_item)
        session = self._make_curl_cffi_session(
            curl_requests,
            headers,
            None if domain_policy is not None else proxy,
        )
        playlist_cache = self._playlist_cache_from_meta(video_item)
        try:
            playlist_url = str(video_item.url)
            playlist_text = self._playlist_text_from_cache(playlist_cache, playlist_url)
            if playlist_text is None:
                playlist_text = self._curl_cffi_get_text(
                    session,
                    playlist_url,
                    headers,
                    domain_policy=domain_policy,
                )
            playlist = m3u8.loads(playlist_text, uri=playlist_url)
            if playlist.is_variant:
                variant = self._choose_m3u8_variant(playlist)
                playlist_url = variant.absolute_uri
                playlist_text = self._playlist_text_from_cache(playlist_cache, playlist_url)
                if playlist_text is None:
                    playlist_text = self._curl_cffi_get_text(
                        session,
                        playlist_url,
                        headers,
                        domain_policy=domain_policy,
                    )
                playlist = m3u8.loads(playlist_text, uri=playlist_url)
            if not playlist.segments:
                raise ExternalToolError("curl_cffi HLS fallback found no media segments")
            if self._has_unsupported_hls_encryption(playlist):
                raise ExternalToolError("curl_cffi HLS fallback found unsupported encrypted HLS segments")

            self._write_hls_segments(
                playlist,
                raw_path,
                lambda fetch_url: self._curl_cffi_get_bytes(
                    session,
                    fetch_url,
                    headers,
                    domain_policy=domain_policy,
                ),
                progress_callback,
                check_stop_func,
            )

            if raw_path.stat().st_size <= 0:
                raise ExternalToolError("curl_cffi HLS fallback produced an empty media file")
            self._finalize_curl_cffi_hls_output(raw_path, target, check_stop_func)
        finally:
            try:
                session.close()
            except (OSError, RuntimeError, AttributeError) as exc:
                debug_logger.log_exception("M3U8Downloader", "close_curl_cffi_session", exc)
            self._cleanup_nm3u8_temp_workspace(temp_dir)

    def _download_with_playwright_hls(
        self,
        video_item: VideoItem,
        save_path: str,
        headers: dict[str, str],
        proxy: str | None,
        progress_callback: ProgressCallback,
        check_stop_func: StopCheck,
    ) -> None:
        try:
            import m3u8
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ExternalToolNotFoundError("playwright and m3u8 are required for MissAV browser HLS fallback") from exc

        target = Path(save_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_dir = self._create_nm3u8_temp_workspace(save_path)
        # Playwright 回退路径（fallback）也使用独立目录，避免浏览器上下文失败时污染最终输出路径。
        raw_path = temp_dir / "ucp-media.ts"
        storage_state = video_item.meta.get("browser_storage_state")
        referer = str(video_item.meta.get("referer") or headers.get("Referer") or "")
        user_agent = str(video_item.meta.get("ua") or headers.get("User-Agent") or DEFAULT_USER_AGENT)
        domain_policy = self._domain_policy_for_item(video_item)
        if domain_policy is not None:
            domain_policy.require_public_url(str(video_item.url))
            if referer:
                domain_policy.require_public_url(referer)
        playlist_cache = self._playlist_cache_from_meta(video_item)

        with sync_playwright() as playwright:
            launch_kwargs = self._playwright_launch_kwargs(video_item, proxy)
            browser = playwright.chromium.launch(**launch_kwargs)
            try:
                anti_context = build_browser_anti_detection(
                    "missav",
                    {"ua": user_agent},
                    referer=referer or "https://missav.ai/",
                    default_user_agent=user_agent or DEFAULT_USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                )
                context_kwargs: dict[str, Any] = anti_context.browser_context_kwargs()
                if isinstance(storage_state, dict) and storage_state:
                    context_kwargs["storage_state"] = storage_state
                if domain_policy is not None:
                    context_kwargs["service_workers"] = "block"
                context = browser.new_context(**context_kwargs)
                if domain_policy is not None:
                    playwright_network_guard.install_public_network_guard(context, domain_policy)
                anti_context.apply_to_context(context)
                self._add_cookie_header_to_context(context, headers.get("Cookie"), video_item.url, referer)
                page = context.new_page()
                captured_playlist_cache: dict[str, str] = {}

                playlist_url = str(video_item.url)
                playlist_text = self._playlist_text_from_cache(playlist_cache, playlist_url)
                if playlist_text is None and referer:
                    captured_playlist_cache = self._playwright_capture_playlist_from_referer(page, referer, playlist_url)
                    playlist_text = self._playlist_text_from_cache(captured_playlist_cache, playlist_url)
                elif referer:
                    try:
                        page.goto(referer, wait_until="domcontentloaded", timeout=60000)
                    except PlaywrightError:
                        pass
                if playlist_text is None:
                    if domain_policy is not None:
                        domain_policy.require_public_url(playlist_url)
                    playlist_bytes = self._playwright_fetch_or_goto_bytes(page, playlist_url)
                    playlist_text = playlist_bytes.decode("utf-8", errors="replace")
                playlist = m3u8.loads(playlist_text, uri=playlist_url)
                if playlist.is_variant:
                    variant = self._choose_m3u8_variant(playlist)
                    playlist_url = variant.absolute_uri
                    playlist_text = self._playlist_text_from_cache(playlist_cache, playlist_url)
                    if playlist_text is None:
                        playlist_text = self._playlist_text_from_cache(captured_playlist_cache, playlist_url)
                    if playlist_text is None:
                        if domain_policy is not None:
                            domain_policy.require_public_url(playlist_url)
                        playlist_bytes = self._playwright_fetch_or_goto_bytes(page, playlist_url)
                        playlist_text = playlist_bytes.decode("utf-8", errors="replace")
                    playlist = m3u8.loads(playlist_text, uri=playlist_url)
                if not playlist.segments:
                    raise ExternalToolError("Playwright HLS fallback found no media segments")
                if self._has_unsupported_hls_encryption(playlist):
                    raise ExternalToolError("Playwright HLS fallback found unsupported encrypted HLS segments")

                self._write_hls_segments(
                    playlist,
                    raw_path,
                    lambda fetch_url: self._playwright_fetch_with_policy(
                        page,
                        fetch_url,
                        domain_policy=domain_policy,
                    ),
                    progress_callback,
                    check_stop_func,
                )
                if raw_path.stat().st_size <= 0:
                    raise ExternalToolError("Playwright HLS fallback produced an empty media file")
                self._finalize_curl_cffi_hls_output(raw_path, target, check_stop_func)
            finally:
                try:
                    browser.close()
                finally:
                    self._cleanup_nm3u8_temp_workspace(temp_dir)

    @classmethod
    def _playwright_capture_playlist_from_referer(cls, page, referer: str, playlist_url: str) -> dict[str, str]:
        captured: dict[str, str] = {}
        try:
            with page.expect_response(
                lambda response: cls._playwright_response_matches_url(response, playlist_url)
                and int(getattr(response, "status", 0) or 0) in (200, 206),
                timeout=30000,
            ) as response_info:
                page.goto(referer, wait_until="domcontentloaded", timeout=60000)
                cls._playwright_trigger_media_playback(page)
            response = response_info.value
            body = response.body()
        except Exception:
            return captured
        try:
            text = bytes(body or b"").decode("utf-8", errors="replace")
        except (TypeError, ValueError):
            text = ""
        if "#EXTM3U" in text:
            captured[str(getattr(response, "url", "") or playlist_url)] = text
        return captured

    @staticmethod
    def _playwright_trigger_media_playback(page) -> None:
        try:
            page.evaluate(
                """
                async () => {
                    const video = document.querySelector('video');
                    const buttons = [
                        document.querySelector('button[data-plyr="play"]'),
                        document.querySelector('.plyr__control[data-plyr="play"]'),
                        document.querySelector('.plyr'),
                    ].filter(Boolean);
                    for (const button of buttons) {
                        try { button.click(); } catch (_) {}
                    }
                    if (video) {
                        video.muted = true;
                        video.playsInline = true;
                        try { await video.play(); } catch (_) {}
                        try { video.load(); } catch (_) {}
                    }
                }
                """
            )
        except (PlaywrightError, RuntimeError, AttributeError) as exc:
            debug_logger.log_exception("M3U8Downloader", "trigger_media_playback_eval", exc)
        try:
            page.mouse.click(400, 300)
        except (PlaywrightError, RuntimeError, AttributeError) as exc:
            debug_logger.log_exception("M3U8Downloader", "trigger_media_playback_click", exc)

    @classmethod
    def _playwright_fetch_or_goto_bytes(cls, page, url: str) -> bytes:
        try:
            return cls._playwright_fetch_bytes(page, url)
        except ExternalToolError:
            pass
        try:
            return cls._playwright_same_origin_media_request_bytes(page, url)
        except ExternalToolError:
            pass
        try:
            return cls._playwright_same_origin_fetch_bytes(page, url)
        except ExternalToolError:
            return cls._playwright_goto_bytes(page, url)

    @classmethod
    def _playwright_same_origin_media_request_bytes(cls, page, url: str) -> bytes:
        landing_url = cls._playwright_same_origin_landing_url(url)
        try:
            page.goto(landing_url, wait_until="commit", timeout=60000)
        except (PlaywrightError, RuntimeError, AttributeError) as exc:
            debug_logger.log_exception("M3U8Downloader", "same_origin_media_landing_goto", exc, details={"url": landing_url})
        try:
            with page.expect_response(
                lambda response: cls._playwright_response_matches_url(response, url)
                and int(getattr(response, "status", 0) or 0) in (200, 206),
                timeout=60000,
            ) as response_info:
                page.evaluate(
                    """
                    async (url) => {
                        const previous = document.getElementById('__ucp_hls_probe');
                        if (previous) { previous.remove(); }
                        const video = document.createElement('video');
                        video.id = '__ucp_hls_probe';
                        video.preload = 'auto';
                        video.muted = true;
                        video.playsInline = true;
                        video.style.cssText = 'position:fixed;left:-99999px;top:-99999px;width:1px;height:1px;';
                        document.body.appendChild(video);
                        video.src = url;
                        video.load();
                        await new Promise(resolve => setTimeout(resolve, 250));
                    }
                    """,
                    url,
                )
            response = response_info.value
            body = response.body()
        except Exception as exc:
            raise ExternalToolError(f"Playwright media request failed for {url}: {exc}") from exc
        if not body:
            raise ExternalToolError(f"Playwright media request returned empty body for {url}")
        return bytes(body)

    @classmethod
    def _playwright_same_origin_fetch_bytes(cls, page, url: str) -> bytes:
        landing_url = cls._playwright_same_origin_landing_url(url)
        try:
            page.goto(landing_url, wait_until="commit", timeout=60000)
        except (PlaywrightError, RuntimeError, AttributeError) as exc:
            debug_logger.log_exception("M3U8Downloader", "same_origin_fetch_landing_goto", exc, details={"url": landing_url})
        return cls._playwright_fetch_bytes(page, url)

    @staticmethod
    def _playwright_response_matches_url(response, url: str) -> bool:
        response_url = str(getattr(response, "url", "") or "").split("#", 1)[0]
        target_url = str(url or "").split("#", 1)[0]
        return response_url == target_url

    @staticmethod
    def _playwright_same_origin_landing_url(url: str) -> str:
        parsed = urllib.parse.urlparse(str(url or ""))
        if not parsed.scheme or not parsed.netloc:
            return str(url or "")
        path = parsed.path or "/"
        if path.endswith("/"):
            directory = path
        else:
            directory = path.rsplit("/", 1)[0] + "/"
        return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, directory or "/", "", "", ""))

    @staticmethod
    def _playwright_fetch_bytes(page, url: str) -> bytes:
        try:
            result = page.evaluate(
                """
                async (url) => {
                    const response = await fetch(url, { credentials: 'include', cache: 'no-store' });
                    if (!response.ok && response.status !== 206) {
                        throw new Error(`HTTP ${response.status}`);
                    }
                    const buffer = await response.arrayBuffer();
                    const bytes = new Uint8Array(buffer);
                    let binary = '';
                    const chunkSize = 0x8000;
                    for (let i = 0; i < bytes.length; i += chunkSize) {
                        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
                    }
                    return { status: response.status, body: btoa(binary) };
                }
                """,
                url,
            )
        except Exception as exc:
            raise ExternalToolError(f"Playwright fetch failed for {url}: {exc}") from exc
        if not isinstance(result, dict) or not result.get("body"):
            raise ExternalToolError(f"Playwright fetch returned no body for {url}")
        try:
            return base64.b64decode(str(result["body"]))
        except (ValueError, TypeError) as exc:
            raise ExternalToolError(f"Playwright fetch returned invalid body for {url}") from exc

    @staticmethod
    def _playwright_goto_bytes(page, url: str) -> bytes:
        response = page.goto(url, wait_until="commit", timeout=60000)
        if response is None:
            raise ExternalToolError(f"Playwright navigation returned no response for {url}")
        status = int(response.status or 0)
        if status not in (200, 206):
            raise ExternalToolError(f"Playwright HLS request failed ({status}) for {url}")
        body = response.body()
        if not body:
            raise ExternalToolError(f"Playwright navigation returned empty body for {url}")
        return bytes(body)

    @classmethod
    def _add_cookie_header_to_context(cls, context, cookie_header: str | None, stream_url: str, referer: str) -> None:
        cookies = cls._cookies_from_header(cookie_header, stream_url, referer)
        if not cookies:
            return
        try:
            context.add_cookies(cookies)
        except Exception:
            return

    @staticmethod
    def _cookies_from_header(cookie_header: str | None, stream_url: str, referer: str) -> list[dict[str, Any]]:
        if not cookie_header:
            return []
        hosts: list[str] = []
        for url in (stream_url, referer):
            try:
                host = urllib.parse.urlparse(str(url or "")).hostname
            except (TypeError, ValueError):
                host = None
            if host and host not in hosts:
                hosts.append(host)
        cookies: list[dict[str, Any]] = []
        for pair in str(cookie_header).split(";"):
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not name:
                continue
            for host in hosts:
                cookies.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": host,
                        "path": "/",
                        "secure": True,
                    }
                )
        return cookies

    def _write_hls_segments(
        self,
        playlist,
        raw_path: Path,
        fetch_bytes,
        progress_callback: ProgressCallback,
        check_stop_func: StopCheck,
    ) -> None:
        written_maps: set[str] = set()
        key_cache: dict[str, bytes] = {}
        total = len(playlist.segments)
        bytes_written = 0
        rate_limiter = TransferRateLimiter(cfg.get("download", "speed_limit_kb", 0))
        with raw_path.open("wb") as output:
            for index, segment in enumerate(playlist.segments, start=1):
                if check_stop_func():
                    raise DownloaderStoppedError("Download stopped by user")
                init_section = getattr(segment, "init_section", None)
                init_uri = getattr(init_section, "absolute_uri", None) if init_section else None
                if init_uri and init_uri not in written_maps:
                    # fMP4 HLS 的 MAP 初始化段只写一次，否则合并后的流会被播放器识别为损坏。
                    init_bytes = fetch_bytes(init_uri)
                    output.write(init_bytes)
                    rate_limiter.throttle(len(init_bytes), check_stop_func)
                    bytes_written += len(init_bytes)
                    written_maps.add(init_uri)
                segment_bytes = fetch_bytes(segment.absolute_uri)
                decoded_segment = self._decrypt_hls_segment(segment, segment_bytes, fetch_bytes, key_cache)
                output.write(decoded_segment)
                # Python/浏览器回退路径（fallback）已经拿到整段数据，只能在段之间施加背压；
                # 外部工具与 yt-dlp 路径则使用各自的原生限速参数。
                rate_limiter.throttle(len(decoded_segment), check_stop_func)
                bytes_written += len(decoded_segment)
                self._emit_progress(
                    progress_callback,
                    min(95, 10 + int(index * 85 / total)),
                    bytes_downloaded=bytes_written,
                )

    def _decrypt_hls_segment(
        self,
        segment,
        data: bytes,
        fetch_bytes,
        key_cache: dict[str, bytes],
    ) -> bytes:
        key = getattr(segment, "key", None)
        method = str(getattr(key, "method", "") or "").upper()
        if not key or not method or method == "NONE":
            return data
        if method != "AES-128":
            raise ExternalToolError(f"Unsupported HLS encryption method: {method}")
        key_uri = getattr(key, "absolute_uri", None) or getattr(key, "uri", None)
        if not key_uri:
            raise ExternalToolError("AES-128 HLS key URI is missing")
        if key_uri not in key_cache:
            key_bytes = fetch_bytes(key_uri)
            if len(key_bytes) != 16:
                raise ExternalToolError(f"AES-128 HLS key must be 16 bytes, got {len(key_bytes)}")
            key_cache[key_uri] = key_bytes
        sequence = int(getattr(segment, "media_sequence", 0) or 0)
        iv = self._hls_aes_iv(getattr(key, "iv", None), sequence)
        return self._aes_128_cbc_decrypt(data, key_cache[key_uri], iv)

    @staticmethod
    def _hls_aes_iv(iv_text: str | None, media_sequence: int) -> bytes:
        if iv_text:
            text = str(iv_text).strip()
            if text.lower().startswith("0x"):
                text = text[2:]
            text = text.zfill(32)[-32:]
            try:
                return bytes.fromhex(text)
            except ValueError as exc:
                raise ExternalToolError(f"Invalid HLS AES IV: {iv_text}") from exc
        return int(media_sequence).to_bytes(16, byteorder="big", signed=False)

    @staticmethod
    def _aes_128_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
        try:
            # ``Crypto`` 实际由持续维护的 PyCryptodome 提供；B413 只识别到了兼容命名空间。
            from Crypto.Cipher import AES  # nosec B413
            from Crypto.Util.Padding import unpad  # nosec B413
        except ImportError:
            try:
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                from cryptography.hazmat.backends import default_backend
            except ImportError as cryptography_exc:
                raise ExternalToolNotFoundError("pycryptodome or cryptography is required for AES-128 HLS") from cryptography_exc
            decryptor = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend()).decryptor()
            decrypted = decryptor.update(data) + decryptor.finalize()
            return N_m3u8DL_RE_Downloader._pkcs7_unpad(decrypted)
        try:
            return unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(data), 16)
        except ValueError:
            # 部分 HLS 生产端已按 MPEG-TS 包边界对齐，数据本身不含 PKCS7 填充。
            return AES.new(key, AES.MODE_CBC, iv).decrypt(data)

    @staticmethod
    def _pkcs7_unpad(data: bytes) -> bytes:
        if not data:
            return data
        pad = data[-1]
        if pad < 1 or pad > 16 or data[-pad:] != bytes([pad]) * pad:
            return data
        return data[:-pad]

    @staticmethod
    def _make_curl_cffi_session(curl_requests, headers: dict[str, str], proxy: str | None):
        kwargs: dict[str, Any] = {"impersonate": "chrome"}
        if proxy:
            kwargs["proxy"] = proxy
        else:
            from curl_cffi.const import CurlOpt

            kwargs["curl_options"] = {CurlOpt.PROXY: ""}
        try:
            return curl_requests.Session(**kwargs)
        except TypeError:
            kwargs.pop("impersonate", None)
            return curl_requests.Session(**kwargs)

    @classmethod
    def _curl_cffi_get_text(cls, session, url: str, headers: dict[str, str], *, domain_policy=None) -> str:
        response = cls._curl_cffi_session_response(
            session,
            url,
            headers,
            timeout=30,
            domain_policy=domain_policy,
            max_response_bytes=8 * 1024 * 1024,
        )
        try:
            if response.status_code not in (200, 206):
                raise ExternalToolError(f"curl_cffi HLS request failed ({response.status_code}) for {url}")
            return response.text
        finally:
            cls._close_hls_proxy_response(response, url)

    @classmethod
    def _curl_cffi_get_bytes(cls, session, url: str, headers: dict[str, str], *, domain_policy=None) -> bytes:
        response = cls._curl_cffi_session_response(
            session,
            url,
            headers,
            timeout=60,
            domain_policy=domain_policy,
            max_response_bytes=256 * 1024 * 1024,
        )
        try:
            if response.status_code not in (200, 206):
                raise ExternalToolError(f"curl_cffi HLS request failed ({response.status_code}) for {url}")
            return bytes(response.content or b"")
        finally:
            cls._close_hls_proxy_response(response, url)

    @classmethod
    def _curl_cffi_session_response(
        cls,
        session,
        url: str,
        headers: dict[str, str],
        *,
        timeout: float,
        domain_policy: DomainPolicyEngine | None,
        max_redirects: int = 5,
        max_response_bytes: int = 64 * 1024 * 1024,
    ):
        """使用 curl_cffi 发起请求，但仍由本项目掌控并逐跳校验重定向。"""
        policy = domain_policy or PUBLIC_DOMAIN_POLICY
        if domain_policy is not None:
            return PinnedTransport(
                policy=policy,
                timeout=timeout,
                max_response_bytes=max_response_bytes,
            ).request("GET", url, headers=headers, max_redirects=max_redirects)
        current_url = str(url)
        request_headers = hls_proxy_utils.filter_hls_outbound_headers(dict(headers), cross_origin=False)
        for redirect_count in range(max(0, int(max_redirects)) + 1):
            request_url, curl_options = hls_proxy_utils.prepare_hls_curl_request(current_url, policy, None)
            request_kwargs: dict[str, Any] = {
                "headers": request_headers,
                "timeout": timeout,
                "allow_redirects": False,
            }
            if curl_options:
                request_kwargs["curl_options"] = curl_options
            response = session.get(request_url, **request_kwargs)
            status = int(getattr(response, "status_code", 0) or 0)
            response_headers = getattr(response, "headers", {}) or {}
            location = response_headers.get("Location") or response_headers.get("location")
            if status not in DomainPolicyEngine.REDIRECT_STATUS_CODES or not location:
                return response

            target_url = urllib.parse.urljoin(request_url, str(location))
            try:
                if redirect_count >= max_redirects:
                    raise ExternalToolError("curl_cffi HLS redirect limit exceeded")
            except Exception:
                cls._close_hls_proxy_response(response, current_url)
                raise

            if cls._url_origin(current_url) != cls._url_origin(target_url):
                request_headers = hls_proxy_utils.filter_hls_outbound_headers(
                    request_headers,
                    cross_origin=True,
                )
            cls._close_hls_proxy_response(response, current_url)
            current_url = target_url
        raise ExternalToolError("curl_cffi HLS redirect limit exceeded")

    @classmethod
    def _playwright_fetch_with_policy(cls, page, url: str, *, domain_policy=None) -> bytes:
        if domain_policy is not None:
            domain_policy.require_public_url(url)
        return cls._playwright_fetch_or_goto_bytes(page, url)

    @staticmethod
    def _has_unsupported_hls_encryption(playlist) -> bool:
        for segment in playlist.segments:
            key = getattr(segment, "key", None)
            method = str(getattr(key, "method", "") or "").upper()
            if method and method not in ("NONE", "AES-128"):
                return True
        return False

    @staticmethod
    def _choose_m3u8_variant(playlist):
        variants = list(playlist.playlists or [])
        if not variants:
            raise ExternalToolError("curl_cffi HLS fallback found no variant streams")

        def score(item) -> tuple[int, int]:
            stream_info = getattr(item, "stream_info", None)
            bandwidth = int(getattr(stream_info, "bandwidth", 0) or 0)
            resolution = getattr(stream_info, "resolution", None) or (0, 0)
            try:
                pixels = int(resolution[0] or 0) * int(resolution[1] or 0)
            except (TypeError, ValueError, IndexError):
                pixels = 0
            return bandwidth, pixels

        return max(variants, key=score)

    def _finalize_curl_cffi_hls_output(self, raw_path: Path, target: Path, check_stop_func: StopCheck) -> None:
        if target.suffix.lower() != ".mp4":
            assert_publishable_media(str(raw_path), str(target))
            os.replace(raw_path, target)
            return
        ffmpeg = FFmpegExternalTool.resolve_executable()
        if not ffmpeg:
            raise ExternalToolNotFoundError("ffmpeg executable not found for MissAV HLS remux")
        staging = raw_path.with_name(f"ucp-remux{target.suffix}")
        command = [
            ffmpeg,
            "-y",
            "-i",
            str(raw_path),
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            str(staging),
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=build_hidden_startupinfo(),
            creationflags=0 if os.name != "nt" else getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=isolated_media_subprocess_env(),
        )
        ExternalToolRunner.wait_process(process, check_stop_func, None, None, poll_interval=0.2)
        if process.returncode != 0:
            raise ExternalToolError(f"ffmpeg remux failed (Code: {process.returncode})")
        try:
            staging_size = os.lstat(staging).st_size
        except OSError:
            staging_size = 0
        if staging_size <= 0:
            raise ExternalToolError("ffmpeg remux did not create a valid output file")
        assert_publishable_media(str(staging), str(target))
        os.replace(staging, target)
