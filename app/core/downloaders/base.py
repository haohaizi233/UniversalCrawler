"""下载器公共基类：统一校验、重试、断点续传与原子发布。"""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.parse
from typing import Any, Callable, cast

import requests

from app.config import DEFAULT_USER_AGENT, cfg, effective_download_retry_count
from app.debug_logger import debug_logger
from app.exceptions import DownloaderStoppedError, StreamDownloadError
from app.models import VideoItem
from app.utils.user_agents import resolve_user_agent
from shared.runtime_options import (
    PUBLIC_DOMAIN_POLICY,
    DomainPolicyEngine,
    DomainPolicyViolation,
)
from shared.network_proxy import requests_proxy_mapping

ProgressCallback = Callable[..., None]
StopCheck = Callable[[], bool]

_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_HEADER_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_MEDIA_BOX_SUFFIXES = frozenset({".mp4", ".m4v", ".mov", ".m4a"})

# DownloadWorker._detect_actual_file_type 仍只读 32 字节，并用
# 「前 12 字节里出现 ftyp」和「前 5 字节里出现 FLV」改名。那次改名不在
# 下载器里，需要另改 app/core/download_manager.py。
DOWNLOAD_MANAGER_SIGNATURE_RENAME_NOTE = (
    "download_manager.DownloadWorker 仍按 32 字节窗口做宽松 ftyp/FLV 改名，"
    "该逻辑需要另改 download_manager.py。"
)


class HlsPlaylistResponse(StreamDownloadError):
    """HTTP 响应体仍是 HLS 播放列表，不能当作普通文件发布。"""


def reject_control_characters(label: str, value: str) -> str:
    """拒绝标头、Referer、代理地址里的控制字符。"""
    text = str(value)
    if _CONTROL_CHARACTER.search(text):
        raise DomainPolicyViolation(f"{label} contains control characters")
    return text


def validate_header_token(name: str, value: str) -> tuple[str, str]:
    header_name = str(name).strip()
    header_value = str(value)
    if not _HEADER_TOKEN.fullmatch(header_name) or _CONTROL_CHARACTER.search(header_value):
        raise DomainPolicyViolation("outbound header contains control characters or is invalid")
    return header_name, header_value


def assert_same_save_directory(left: str, right: str) -> None:
    """签名改名只能留在原保存目录，避免路径穿越。"""
    left_dir = os.path.normcase(os.path.abspath(os.path.dirname(left)))
    right_dir = os.path.normcase(os.path.abspath(os.path.dirname(right)))
    if left_dir != right_dir:
        raise StreamDownloadError("媒体改名必须留在同一保存目录")


def looks_like_html_payload(header: bytes) -> bool:
    sample = header.lstrip().lower()
    return sample.startswith((b"<!doctype", b"<html", b"<head", b"<body", b"<script", b"<?xml"))


def assert_publishable_media(path: str, intended_path: str) -> None:
    """在发布前拒绝空文件、MZ、HTML、错位 ftyp 和非法 FLV。

    未知签名不能借 .mp4 扩展名成功结束。download_manager 的 32 字节改名
    仍需另人修改，本校验只保证下载器自己不会先把坏文件发布出去。
    """
    intended_suffix = os.path.splitext(intended_path)[1].lower()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            header = handle.read(64)
    except OSError as exc:
        raise StreamDownloadError(f"无法校验媒体签名: {exc}") from exc
    if size <= 0 or not header:
        raise StreamDownloadError("空文件不能作为下载成功")
    if header.startswith(b"MZ"):
        raise StreamDownloadError(f"拒绝 MZ 可执行文件。{DOWNLOAD_MANAGER_SIGNATURE_RENAME_NOTE}")
    if looks_like_html_payload(header):
        raise StreamDownloadError(f"拒绝 HTML 响应。{DOWNLOAD_MANAGER_SIGNATURE_RENAME_NOTE}")
    if intended_suffix == ".flv" and not header.startswith(b"FLV\x01"):
        raise StreamDownloadError(f"FLV 必须是 FLV\\x01。{DOWNLOAD_MANAGER_SIGNATURE_RENAME_NOTE}")
    if intended_suffix in _MEDIA_BOX_SUFFIXES and (len(header) < 8 or header[4:8] != b"ftyp"):
        raise StreamDownloadError(
            "未知签名不能保留 .mp4 成功结果，ftyp 必须对齐偏移 4。"
            + DOWNLOAD_MANAGER_SIGNATURE_RENAME_NOTE
        )


def durations_consistent(actual: float, expected: float) -> bool:
    if expected <= 0:
        return actual > 0
    return abs(actual - expected) <= max(1.0, expected * 0.02)


def sizes_consistent(actual: int, expected: int) -> bool:
    if expected <= 0:
        return actual > 0
    return abs(actual - expected) <= max(4096, int(expected * 0.02))


def single_file_http_forbidden(url: str) -> bool:
    """`.ts`、`.m4s` 和 `.m3u8` 不能按单个 HTTP 文件成功保存。"""
    try:
        path = urllib.parse.urlsplit(str(url or "")).path.lower()
    except ValueError:
        return False
    return path.endswith((".ts", ".m4s", ".m3u8"))


def http_payload_is_hls_playlist(content_type: str, path: str) -> bool:
    """用响应类型和正文判断播放列表，不看 URL 里是否出现 m3u8 字符串。"""
    lowered = str(content_type or "").lower()
    if "mpegurl" in lowered or "m3u8" in lowered:
        return True
    try:
        with open(path, "rb") as handle:
            prefix = handle.read(4096)
    except OSError:
        return False
    return b"#EXTM3U" in prefix


class TransferRateLimiter:
    """按所有调用线程的累计字节数限制单个下载任务的平均传输速度。"""

    def __init__(self, speed_limit_kb: object) -> None:
        try:
            normalized = max(0, int(cast(Any, speed_limit_kb or 0)))
        except (TypeError, ValueError):
            normalized = 0
        self.bytes_per_second = normalized * 1024
        self._started_at = time.monotonic()
        self._scheduled_bytes = 0
        self._lock = threading.Lock()

    def throttle(self, byte_count: int, check_stop_func: StopCheck | None = None) -> None:
        """等待到累计字节对应的时间点；共享实例可限制多分片的合计速度。"""
        if self.bytes_per_second <= 0 or byte_count <= 0:
            return
        with self._lock:
            self._scheduled_bytes += int(byte_count)
            deadline = self._started_at + (self._scheduled_bytes / self.bytes_per_second)

        while True:
            if check_stop_func is not None and check_stop_func():
                raise DownloaderStoppedError("用户停止下载")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.1))


class BaseDownloader:
    """为平台下载器提供公共校验、进度回调、重试与原子落盘能力。"""

    source_id: str | None = None

    @staticmethod
    def _domain_policy_for_item(video_item: VideoItem) -> DomainPolicyEngine | None:
        meta = video_item.meta if isinstance(getattr(video_item, "meta", None), dict) else {}
        return PUBLIC_DOMAIN_POLICY if meta.get("_network_policy") == "public" else None

    @staticmethod
    def _domain_policy_request_kwargs(
        domain_policy: DomainPolicyEngine | None,
        url: str,
    ) -> dict[str, object]:
        if domain_policy is None:
            return {}
        domain_policy.require_public_url(url)
        return {"hooks": {"response": domain_policy.validate_redirect_response}}

    @classmethod
    def can_handle(cls, video_item: VideoItem) -> bool:
        """仅接收来源与当前下载器 `source_id` 匹配的任务。"""
        return bool(cls.source_id and video_item.source == cls.source_id)

    @staticmethod
    def _emit_progress(
        progress_callback: ProgressCallback | None,
        progress: int,
        *,
        bytes_downloaded: int | None = None,
        bytes_total: int | None = None,
        phase: str | None = None,
        phase_message: str | None = None,
        write_status: str | None = None,
        merge_status: str | None = None,
    ) -> None:
        if progress_callback is None:
            return
        kwargs = {
            "bytes_downloaded": bytes_downloaded,
            "bytes_total": bytes_total,
            "phase": phase,
            "phase_message": phase_message,
            "write_status": write_status,
            "merge_status": merge_status,
        }
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        try:
            if not kwargs:
                progress_callback(progress)
                return
            progress_callback(progress, **kwargs)
        except TypeError as exc:
            # 旧调用方只接受百分比；这里降级调用，避免新增 telemetry 字段破坏兼容适配器。
            if not kwargs:
                debug_logger.log_exception(
                    "BaseDownloader",
                    "progress_callback_error",
                    exc,
                    details={"progress": progress},
                )
                return
            try:
                progress_callback(progress)
            except Exception as fallback_exc:
                debug_logger.log_exception(
                    "BaseDownloader",
                    "progress_callback_fallback_error",
                    fallback_exc,
                    details={"progress": progress},
                )
        except Exception as exc:
            debug_logger.log_exception(
                "BaseDownloader",
                "progress_callback_error",
                exc,
                details={"progress": progress},
            )

    def _apply_runtime_headers(self, video_item: VideoItem, headers: dict[str, str]) -> None:
        """让策略下载器复用上层已经确定好的认证头信息。"""
        if headers.get("User-Agent"):
            video_item.meta["ua"] = headers["User-Agent"]
        if headers.get("Referer"):
            video_item.meta["referer"] = headers["Referer"]

    def _resolve_runtime_user_agent(
        self,
        video_item: VideoItem,
        *,
        source: str,
        configured_user_agent: str,
        default_user_agent: str = DEFAULT_USER_AGENT,
    ) -> str:
        user_agent = resolve_user_agent(
            source,
            video_item.meta,
            configured_user_agent=configured_user_agent,
            default_user_agent=default_user_agent,
        )
        if not video_item.meta.get("ua"):
            video_item.meta["ua"] = user_agent
        return user_agent

    # 智能下载调度器：把选择策略交给 strategy.py，便于平台下载器复用同一套回退顺序。
    def _download_with_strategy_fallback(
        self,
        *,
        video_item: VideoItem,
        save_path: str,
        headers: dict[str, str],
        progress_callback: ProgressCallback,
        check_stop_func: StopCheck,
        max_retries: int,
        timeout: int,
        chunk_size: int,
        support_resume: bool = False,
        error_message: str = "下载失败",
    ) -> None:
        """按流类型/体积选择外部工具、分块下载或普通 HTTP 下载。"""
        from .strategy import DEFAULT_DOWNLOAD_STRATEGY_CHAIN, DownloadRequest

        request = DownloadRequest(
            video_item=video_item,
            save_path=save_path,
            headers=headers,
            progress_callback=progress_callback,
            check_stop_func=check_stop_func,
            max_retries=max_retries,
            auto_retry=effective_download_retry_count(
                max_retries,
                cfg.get("download", "auto_retry", True),
            ) > 0,
            timeout=timeout,
            chunk_size=chunk_size,
            support_resume=support_resume,
            error_message=error_message,
        )
        DEFAULT_DOWNLOAD_STRATEGY_CHAIN.execute(self, request)

    def _should_resume_download(self, temp_path: str) -> bool:
        """仅在临时文件仍存在时尝试断点续传。"""
        return os.path.exists(temp_path)

    def _get_existing_size(self, temp_path: str) -> int:
        """读取续传偏移；文件在检查后消失时按零字节重新下载。"""
        try:
            return os.path.getsize(temp_path)
        except OSError:
            return 0

    def _cleanup_temp_file(self, temp_path: str) -> None:
        """尽力清理失败下载的临时文件，不覆盖原始异常。"""
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

    def _assert_expected_http_media(
        self,
        media_path: str,
        expected_size_bytes: int | None,
        expected_duration: float | None,
    ) -> None:
        """meta 里有期望大小或时长时，不满足就失败。ffprobe 不可用也不能放过时长。"""
        if type(expected_size_bytes) is int and expected_size_bytes > 0:
            try:
                actual_size = os.path.getsize(media_path)
            except OSError as exc:
                raise StreamDownloadError(f"HTTP 下载结果无法核对大小: {exc}") from exc
            if not sizes_consistent(actual_size, expected_size_bytes):
                raise StreamDownloadError(
                    f"HTTP 下载大小与预期不一致: expected {expected_size_bytes}, got {actual_size}"
                )
        if (
            isinstance(expected_duration, (int, float))
            and not isinstance(expected_duration, bool)
            and expected_duration > 0
        ):
            from .external import FFmpegExternalTool

            try:
                FFmpegExternalTool.assert_duration_if_expected(media_path, float(expected_duration))
            except DomainPolicyViolation as exc:
                raise StreamDownloadError(f"HTTP 下载时长不符合预期: {exc}") from exc

    def _finalize_download(self, temp_path: str, save_path: str) -> None:
        """校验签名后原子替换目标文件，失败时保留旧文件。"""
        assert_same_save_directory(temp_path, save_path)
        assert_publishable_media(temp_path, save_path)
        os.replace(temp_path, save_path)

    @staticmethod
    def _coerce_retry_count(value: object, default: int = 3) -> int:
        try:
            retry_count = int(cast(Any, value))
        except (TypeError, ValueError):
            retry_count = default
        return max(0, min(retry_count, 10))

    @staticmethod
    def _coerce_bool_setting(value: object, default: bool = True) -> bool:
        if value is None:
            return default
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)

    # 单线程 HTTP 下载实现，作为所有专用策略不可用时的最终兜底。
    def _download_http_file(
        self,
        *,
        url: str,
        save_path: str,
        headers: dict[str, str],
        check_stop_func: StopCheck,
        progress_callback: ProgressCallback | None = None,
        max_retries: int = 3,
        timeout: int = 60,
        chunk_size: int = 8192,
        support_resume: bool = False,
        error_message: str = "下载失败",
        proxy: str | None = None,
        trace_id: str | None = None,
        domain_policy: DomainPolicyEngine | None = None,
        expected_size_bytes: int | None = None,
        expected_duration: float | None = None,
    ) -> None:
        """按配置执行普通 HTTP 下载，支持基于 `.downloading` 临时文件的续传重试。"""
        if single_file_http_forbidden(url):
            raise StreamDownloadError("分片或播放列表不能按单文件 HTTP 成功保存")
        # 只在最后 rename 到 save_path，避免失败或中断时把半成品暴露成可播放文件。
        temp_path = save_path + ".downloading"
        success = False
        response_content_type = ""
        content_length_known = False
        proxies = requests_proxy_mapping(proxy)
        retry_count = effective_download_retry_count(
            max_retries,
            cfg.get("download", "auto_retry", True),
        )
        rate_limiter = TransferRateLimiter(cfg.get("download", "speed_limit_kb", 0))

        # retry_count 表示失败后的重试次数，因此总尝试次数是 retry_count + 1。
        for attempt in range(retry_count + 1):
            if check_stop_func():
                raise DownloaderStoppedError("用户停止下载")
            existing_size = 0
            downloaded = 0
            discard_partial_on_error = False
            try:
                request_kwargs = self._domain_policy_request_kwargs(domain_policy, url)
                # 续传依赖上次失败保留下来的临时文件；主动停止会清理它，避免用户取消后误续传。
                existing_size = self._get_existing_size(temp_path) if support_resume and self._should_resume_download(temp_path) else 0
                request_headers = headers.copy()
                if support_resume and existing_size > 0:
                    request_headers["Range"] = f"bytes={existing_size}-"
                    debug_logger.log(
                        component=self.__class__.__name__,
                        action="http_resume",
                        message="HTTP 断点续传请求已建立",
                        status_code="DL_HTTP_RESUME",
                        details={
                            "url": url,
                            "save_path": save_path,
                            "attempt": attempt + 1,
                            "resume_offset": existing_size,
                        },
                        trace_id=trace_id,
                    )

                with requests.get(
                    url,
                    headers=request_headers,
                    stream=True,
                    timeout=timeout,
                    proxies=proxies,
                    **request_kwargs,
                ) as response:
                    response.raise_for_status()
                    response_content_type = str(response.headers.get("content-type") or "")
                    raw_content_length = response.headers.get("content-length")
                    content_length_known = raw_content_length is not None
                    total_size = int(raw_content_length or 0) if content_length_known else 0
                    if response.status_code == 206:
                        content_range = response.headers.get("content-range") or response.headers.get("Content-Range")
                        parsed_range = self._parse_content_range_header(content_range)
                        if parsed_range is None or parsed_range[0] != existing_size:
                            discard_partial_on_error = True
                            raise StreamDownloadError(
                                f"断点续传响应范围不匹配: expected {existing_size}, got {content_range!r}"
                            )
                        total_size = parsed_range[2] or (total_size + existing_size)
                    elif support_resume and existing_size > 0 and response.status_code == 200:
                        # 服务端忽略 Range 时必须从头覆盖写入，不能继续 append 已有半截文件。
                        existing_size = 0

                    mode = "ab" if support_resume and existing_size > 0 and response.status_code == 206 else "wb"
                    downloaded = existing_size
                    with open(temp_path, mode) as fp:
                        for chunk in response.iter_content(chunk_size=chunk_size):
                            if check_stop_func():
                                raise DownloaderStoppedError("用户停止下载")
                            if chunk:
                                fp.write(chunk)
                                # iter_content 下一轮读取前等待，才能对网络读取形成背压；
                                # 只限制本任务，不会让一个慢任务阻塞其他下载 worker。
                                rate_limiter.throttle(len(chunk), check_stop_func)
                                downloaded += len(chunk)
                                if progress_callback and total_size > 0:
                                    self._emit_progress(progress_callback, int(downloaded / total_size * 100), bytes_downloaded=downloaded, bytes_total=total_size)
                    if total_size > 0 and downloaded < total_size:
                        raise StreamDownloadError(
                            f"HTTP response ended early: received {downloaded} of {total_size} bytes"
                        )
                success = True
                break
            except DownloaderStoppedError:
                # 用户主动停止语义是“放弃本次未完成文件”，不同于网络失败后的可续传缓存。
                self._cleanup_temp_file(temp_path)
                raise
            except DomainPolicyViolation as exc:
                self._cleanup_temp_file(temp_path)
                raise StreamDownloadError(f"下载地址违反公网访问策略: {exc}") from exc
            except requests.RequestException:
                if attempt == retry_count:
                    break
                debug_logger.log(
                    component=self.__class__.__name__,
                    action="http_retry",
                    level="WARN",
                    message=f"HTTP 下载失败，准备重试 ({attempt + 1}/{retry_count})",
                    status_code="DL_HTTP_RETRY",
                    details={
                        "url": url,
                        "save_path": save_path,
                        "attempt": attempt + 1,
                        "max_retries": retry_count,
                        "resume_enabled": support_resume,
                        "resume_offset": existing_size,
                    },
                    trace_id=trace_id,
                )
                time.sleep(1 if retry_count <= 3 else 3)
            except StreamDownloadError as exc:
                if discard_partial_on_error or not support_resume or attempt == retry_count:
                    self._cleanup_temp_file(temp_path)
                if attempt == retry_count:
                    raise
                debug_logger.log(
                    component=self.__class__.__name__,
                    action="http_retry",
                    level="WARN",
                    message=f"HTTP 下载内容不完整，准备重试 ({attempt + 1}/{retry_count})",
                    status_code="DL_HTTP_RETRY",
                    details={
                        "url": url,
                        "save_path": save_path,
                        "attempt": attempt + 1,
                        "max_retries": retry_count,
                        "resume_enabled": support_resume,
                        "resume_offset": 0 if discard_partial_on_error else downloaded,
                        "error": str(exc),
                    },
                    trace_id=trace_id,
                )
                time.sleep(1 if retry_count <= 3 else 3)
            except OSError as exc:
                self._cleanup_temp_file(temp_path)
                raise StreamDownloadError(f"{error_message}: {exc}") from exc
            except (ValueError, TypeError, RuntimeError) as exc:
                if attempt == retry_count:
                    raise StreamDownloadError(f"{error_message}: {exc}") from exc
                debug_logger.log(
                    component=self.__class__.__name__,
                    action="http_retry",
                    level="WARN",
                    message=f"HTTP 下载异常，准备重试 ({attempt + 1}/{retry_count})",
                    status_code="DL_HTTP_RETRY",
                    details={
                        "url": url,
                        "save_path": save_path,
                        "attempt": attempt + 1,
                        "max_retries": retry_count,
                        "resume_enabled": support_resume,
                        "resume_offset": existing_size,
                        "error": str(exc),
                    },
                    trace_id=trace_id,
                )
                time.sleep(1 if retry_count <= 3 else 3)

        if not success:
            # 网络失败且重试耗尽后清理兜底临时文件，避免失败列表删除时再遗留 `.downloading`。
            self._cleanup_temp_file(temp_path)
            raise StreamDownloadError(error_message)
        if not content_length_known and downloaded == 0:
            self._cleanup_temp_file(temp_path)
            raise StreamDownloadError("HTTP 响应没有 Content-Length 且正文为空")
        if http_payload_is_hls_playlist(response_content_type, temp_path):
            self._cleanup_temp_file(temp_path)
            raise HlsPlaylistResponse("HTTP 响应仍是 HLS 播放列表")

        try:
            self._assert_expected_http_media(temp_path, expected_size_bytes, expected_duration)
            self._finalize_download(temp_path, save_path)
        except StreamDownloadError:
            self._cleanup_temp_file(temp_path)
            raise
        if progress_callback:
            self._emit_progress(progress_callback, 100, bytes_downloaded=total_size or downloaded, bytes_total=total_size or downloaded)

    def download(
        self,
        video_item: VideoItem,
        save_path: str,
        progress_callback: ProgressCallback,
        check_stop_func: StopCheck,
    ) -> str | None:
        
        raise NotImplementedError
    @staticmethod
    def _parse_content_range_header(value: object) -> tuple[int, int, int | None] | None:
        match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", str(value or "").strip(), re.IGNORECASE)
        if match is None:
            return None
        start = int(match.group(1))
        end = int(match.group(2))
        total = None if match.group(3) == "*" else int(match.group(3))
        if end < start or (total is not None and end >= total):
            return None
        return start, end, total
