"""N_m3u8DL-RE 下载器的 yt-dlp 兜底与请求头整理。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.config import DEFAULT_USER_AGENT, cfg
from app.debug_logger import debug_logger
from app.exceptions import DownloaderStoppedError, ExternalToolError, ExternalToolNotFoundError
from app.models import VideoItem

from . import hls_proxy as hls_proxy_utils
from .base import BaseDownloader, ProgressCallback, StopCheck
from .hls_proxy import _LocalHlsProxy


class Nm3u8YtDlpSupport:
    """由 N_m3u8DL_RE_Downloader 继承的 yt-dlp 兜底、请求头和失败清理。"""

    def _download_with_yt_dlp_fallback(
        self,
        video_item: VideoItem,
        save_path: str,
        headers: dict[str, str],
        proxy: str | None,
        progress_callback: ProgressCallback,
        check_stop_func: StopCheck,
    ) -> None:
        try:
            from yt_dlp.utils import YoutubeDLError
        except ImportError as exc:
            raise ExternalToolNotFoundError("yt-dlp is unavailable; cannot run MissAV m3u8 fallback") from exc

        target = Path(save_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        ydl_headers = self._headers_for_yt_dlp(headers)

        def progress_hook(status: dict[str, Any]) -> None:
            if check_stop_func():
                raise DownloaderStoppedError("Download stopped by user")
            if status.get("status") == "downloading":
                downloaded = float(status.get("downloaded_bytes") or 0)
                total = float(status.get("total_bytes") or status.get("total_bytes_estimate") or 0)
                if total > 0:
                    value = min(95, max(10, int(downloaded * 90 / total)))
                    self._emit_progress(progress_callback, value)
            elif status.get("status") == "finished":
                self._emit_progress(progress_callback, 95)

        local_proxy: _LocalHlsProxy | None = None
        source_url = video_item.url
        yt_dlp_proxy = proxy
        if self._should_use_local_hls_proxy(video_item):
            local_proxy = self._start_local_hls_proxy(video_item, headers, proxy)
            source_url = local_proxy.url
            yt_dlp_proxy = None
            ydl_headers = {"User-Agent": str(headers.get("User-Agent") or DEFAULT_USER_AGENT)}

        params = self._yt_dlp_params(str(target), ydl_headers, yt_dlp_proxy, progress_hook)
        with hls_proxy_utils._hls_proxy_cleanup_scope(
            local_proxy.stop if local_proxy is not None else None
        ):
            try:
                self._run_yt_dlp(source_url, dict(params))
            except YoutubeDLError as exc:
                params.pop("impersonate", None)
                try:
                    self._run_yt_dlp(source_url, dict(params))
                except YoutubeDLError as retry_exc:
                    raise ExternalToolError(f"yt-dlp fallback failed: {retry_exc}") from retry_exc
                else:
                    debug_logger.log(
                        component="N_m3u8DL_RE_Downloader",
                        action="yt_dlp_impersonation_retry",
                        message="yt-dlp fallback succeeded without impersonation",
                        status_code="M3U8_YTDLP_RETRY_OK",
                        details={"initial_error": str(exc)},
                        trace_id=video_item.meta.get("trace_id"),
                    )
            if not target.exists() or target.stat().st_size <= 0:
                raise ExternalToolError("yt-dlp fallback did not create a valid output file")

    @staticmethod
    def _headers_for_yt_dlp(headers: dict[str, str]) -> dict[str, str]:
        blocked = {
            "host",
            "content-length",
            "connection",
            "transfer-encoding",
        }
        result: dict[str, str] = {}
        for key, value in (headers or {}).items():
            key_text = str(key or "").strip()
            value_text = str(value or "").strip()
            if not key_text or not value_text:
                continue
            lowered = key_text.lower()
            if lowered in blocked or lowered.startswith(":"):
                continue
            result[key_text] = value_text
        return result

    @staticmethod
    def _yt_dlp_params(
        save_path: str,
        headers: dict[str, str],
        proxy: str | None,
        progress_hook,
    ) -> dict[str, Any]:
        retry_count = BaseDownloader._coerce_retry_count(cfg.get("download", "max_retries", 3))
        resume_enabled = BaseDownloader._coerce_bool_setting(cfg.get("download", "resume_enabled", True))
        try:
            speed_limit_kb = max(0, int(cfg.get("download", "speed_limit_kb", 0) or 0))
        except (TypeError, ValueError):
            speed_limit_kb = 0
        params = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "outtmpl": {"default": save_path},
            "paths": {"home": str(Path(save_path).parent)},
            "http_headers": headers,
            "proxy": proxy or None,
            "retries": retry_count,
            "fragment_retries": retry_count,
            "continuedl": resume_enabled,
            "nopart": not resume_enabled,
            "merge_output_format": "mp4",
            "progress_hooks": [progress_hook],
            "impersonate": "",
        }
        if speed_limit_kb > 0:
            # yt-dlp 的 ratelimit 单位是 bytes/s，和配置页的 KB/s 做一次显式换算。
            params["ratelimit"] = speed_limit_kb * 1024
        return params

    @staticmethod
    def _run_yt_dlp(url: str, params: dict[str, Any]) -> None:
        from yt_dlp import YoutubeDL

        with YoutubeDL(params) as ydl:
            ydl.download([url])

    @staticmethod
    def _headers_from_meta(video_item: VideoItem, user_agent: str, referer: str) -> dict[str, str]:
        headers: dict[str, str] = {}
        meta_headers = video_item.meta.get("headers")
        if isinstance(meta_headers, dict):
            headers.update({str(key): str(value) for key, value in meta_headers.items() if value})
        headers.setdefault("User-Agent", str(user_agent or DEFAULT_USER_AGENT))
        if referer:
            headers.setdefault("Referer", str(referer))
        if (
            str(video_item.source or "").lower() == "missav"
            and not video_item.meta.get("missav_use_browser_headers")
            and not video_item.meta.get("missav_include_cookies")
        ):
            return {
                key: headers[key]
                for key in ("User-Agent", "Referer")
                if headers.get(key)
            }
        cookie_dict = video_item.meta.get("cookies")
        if isinstance(cookie_dict, dict) and cookie_dict:
            headers.setdefault("Cookie", "; ".join(f"{key}={value}" for key, value in cookie_dict.items()))
        elif isinstance(video_item.meta.get("cookie"), str) and video_item.meta.get("cookie"):
            headers.setdefault("Cookie", str(video_item.meta["cookie"]))
        return headers

    @staticmethod
    def _cleanup_external_temp_files(save_path: str) -> None:
        """删掉本次 yt-dlp 留在保存目录的 .part 等临时文件。

        只匹配「主干.」前缀，因此「标题.mp4」「标题.ts」和「标题-其他.part」会留下。
        工作目录仍由 _cleanup_nm3u8_temp_workspace 按确切路径删除。
        """
        target = Path(save_path)
        directory = target.parent
        stem = target.stem
        if not stem:
            return
        prefix = f"{stem}."
        temp_markers = (".part", ".ytdl", ".temp", ".tmp", ".aria2", ".download")
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            name = entry.name
            if not name.startswith(prefix):
                continue
            lowered = name.lower()
            # 必须是后缀命中：子串匹配会把「标题.temporary.mkv」这类正常文件误删。
            if not lowered.endswith(temp_markers):
                continue
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                os.remove(entry.path)
            except OSError:
                continue
