"""用于选择媒体传输方案的显式下载策略链。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app.config import cfg
from app.debug_logger import debug_logger
from app.exceptions import DownloaderStoppedError, StreamDownloadError
from app.models import VideoItem
from app.models.download_context import DownloadContext

from .base import HlsPlaylistResponse, single_file_http_forbidden

@dataclass(slots=True)
class DownloadRequest:
    """下载策略链共享的请求上下文，避免每个策略重复解析 VideoItem/meta。"""

    video_item: VideoItem
    save_path: str
    headers: dict[str, str]
    progress_callback: Callable[..., None]
    check_stop_func: Callable[[], bool]
    max_retries: int
    timeout: int
    chunk_size: int
    auto_retry: bool = True
    support_resume: bool = False
    error_message: str = "下载失败"

    @property
    def context(self) -> DownloadContext:
        return self.video_item.build_download_context()

    @property
    def explicit_strategy(self) -> str:
        return self.context.explicit_strategy

class StrategyCapableDownloader(Protocol):
    """策略链只依赖 BaseDownloader 的最小接口，便于平台下载器复用。"""

    def _apply_runtime_headers(self, video_item: VideoItem, headers: dict[str, str]) -> None:
        ...

    def _domain_policy_for_item(self, video_item: VideoItem):
        ...

    def _download_http_file(
        self,
        *,
        url: str,
        save_path: str,
        headers: dict[str, str],
        check_stop_func,
        progress_callback=None,
        max_retries: int = 3,
        timeout: int = 60,
        chunk_size: int = 8192,
        support_resume: bool = False,
        error_message: str = "下载失败",
        proxy: str | None = None,
        trace_id: str | None = None,
        domain_policy=None,
        expected_size_bytes: int | None = None,
        expected_duration: float | None = None,
    ) -> None:
        ...

class DownloadStrategy(Protocol):
    """单个下载策略；返回 False 表示当前策略不适用或允许后续策略接管。"""

    name: str

    def execute(self, downloader: StrategyCapableDownloader, request: DownloadRequest) -> bool:
        ...

class M3U8DownloadStrategy:
    name = "m3u8"

    def execute(self, downloader: StrategyCapableDownloader, request: DownloadRequest) -> bool:
        from .m3u8 import N_m3u8DL_RE_Downloader

        content_type = str(request.video_item.meta.get("content_type") or "")
        if request.explicit_strategy != self.name and not N_m3u8DL_RE_Downloader.is_m3u8_url(
            request.video_item.url,
            content_type,
        ):
            return False
        if not N_m3u8DL_RE_Downloader.is_available():
            return False
        N_m3u8DL_RE_Downloader().download(
            request.video_item,
            request.save_path,
            request.progress_callback,
            request.check_stop_func,
        )
        return True

class ChunkedDownloadStrategy:
    name = "chunked"

    def execute(self, downloader: StrategyCapableDownloader, request: DownloadRequest) -> bool:
        from .chunked import ChunkedDownloader

        if request.explicit_strategy != self.name and not ChunkedDownloader.should_use(request.video_item):
            return False
        try:
            ChunkedDownloader().download(
                request.video_item,
                request.save_path,
                request.progress_callback,
                request.check_stop_func,
            )
            return True
        except StreamDownloadError as exc:
            if not request.auto_retry:
                raise
            debug_logger.log(
                component=type(downloader).__name__,
                action="chunked_fallback",
                level="WARN",
                message="分块下载不可用，回退到后续下载策略",
                status_code="DL_CHUNKED_FALLBACK",
                details={
                    "title": request.video_item.title,
                    "reason": str(exc),
                    "url": request.video_item.url,
                    "requested_strategy": request.explicit_strategy or "auto",
                },
                trace_id=request.context.trace_id,
            )
            return False

class FFmpegDownloadStrategy:
    name = "ffmpeg"

    def execute(self, downloader: StrategyCapableDownloader, request: DownloadRequest) -> bool:
        from .ffmpeg import FFmpegDownloader

        try:
            if int(cfg.get("download", "speed_limit_kb", 0) or 0) > 0:
                return False
        except (TypeError, ValueError):
            pass
        if request.explicit_strategy != self.name and not FFmpegDownloader.should_use(request.video_item):
            return False
        if not FFmpegDownloader.is_available():
            return False
        FFmpegDownloader().download(
            request.video_item,
            request.save_path,
            request.progress_callback,
            request.check_stop_func,
        )
        return True

class HttpDownloadStrategy:
    name = "http"

    def execute(self, downloader: StrategyCapableDownloader, request: DownloadRequest) -> bool:
        if single_file_http_forbidden(request.video_item.url):
            raise StreamDownloadError("分片或播放列表不能按单文件 HTTP 成功保存")
        meta = request.video_item.meta if isinstance(request.video_item.meta, dict) else {}
        raw_size = meta.get("size_bytes")
        expected_size = raw_size if type(raw_size) is int and raw_size > 0 else None
        raw_duration = meta.get("duration")
        expected_duration = None
        if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool) and raw_duration > 0:
            expected_duration = float(raw_duration)
        try:
            downloader._download_http_file(
                url=request.video_item.url,
                save_path=request.save_path,
                headers=request.headers,
                check_stop_func=request.check_stop_func,
                progress_callback=request.progress_callback,
                max_retries=request.max_retries,
                timeout=request.timeout,
                chunk_size=request.chunk_size,
                support_resume=request.support_resume,
                error_message=request.error_message,
                proxy=request.context.proxy,
                trace_id=request.context.trace_id,
                domain_policy=downloader._domain_policy_for_item(request.video_item),
                expected_size_bytes=expected_size,
                expected_duration=expected_duration,
            )
        except HlsPlaylistResponse:
            # 显式 http 也不能把播放列表正文当成成功结束。无论策略顺序如何，
            # 响应体仍是 m3u8 时都继续走 HLS，而不是发布该正文。
            request.video_item.meta["content_type"] = (
                str(request.video_item.meta.get("content_type") or "")
                or "application/vnd.apple.mpegurl"
            )
            from .m3u8 import N_m3u8DL_RE_Downloader

            if not N_m3u8DL_RE_Downloader.is_available():
                return False
            N_m3u8DL_RE_Downloader().download(
                request.video_item,
                request.save_path,
                request.progress_callback,
                request.check_stop_func,
            )
            return True
        return True

class DownloadStrategyChain:
    """按固定顺序尝试下载策略，并支持任务级显式策略优先。"""

    def __init__(self, strategies: list[DownloadStrategy]) -> None:
        self._strategies = list(strategies)

    def execute(self, downloader: StrategyCapableDownloader, request: DownloadRequest) -> None:
        downloader._apply_runtime_headers(request.video_item, request.headers)
        last_error: Exception | None = None
        strategies = self._ordered_strategies(request.explicit_strategy)
        if (
            request.explicit_strategy
            and request.explicit_strategy != "http"
            and single_file_http_forbidden(request.video_item.url)
        ):
            # 显式策略失败后，不要再回退到普通 HTTP 把分片或播放列表存成成功文件。
            strategies = [strategy for strategy in strategies if strategy.name != "http"]
        for strategy in strategies:
            try:
                if strategy.execute(downloader, request):
                    return
            except DownloaderStoppedError:
                # 用户停止不能被当成策略失败回退，否则会出现“取消后又被下一个策略继续下载”。
                raise
            except Exception as exc:
                last_error = exc
                if not getattr(request, "auto_retry", True):
                    raise StreamDownloadError(f"{request.error_message}: {exc}") from exc
                debug_logger.log(
                    component=type(downloader).__name__,
                    action="strategy_fallback",
                    level="WARN",
                    message="下载策略执行失败，回退到后续策略",
                    status_code="DL_STRATEGY_FALLBACK",
                    details={
                        "strategy": strategy.name,
                        "reason": str(exc),
                        "requested_strategy": request.explicit_strategy or "auto",
                        "url": request.video_item.url,
                    },
                    trace_id=request.context.trace_id,
                )
        if last_error is not None:
            raise StreamDownloadError(f"{request.error_message}: {last_error}") from last_error
        raise StreamDownloadError(request.error_message)

    def _ordered_strategies(self, explicit_strategy: str) -> list[DownloadStrategy]:
        if not explicit_strategy:
            return list(self._strategies)
        # 显式策略只调整优先级，不关闭兜底；外部工具不可用时仍可回退到 HTTP。
        preferred = [strategy for strategy in self._strategies if strategy.name == explicit_strategy]
        if not preferred:
            available = [strategy.name for strategy in self._strategies]
            raise ValueError(f"未知下载策略: '{explicit_strategy}'，可用策略: {available}")
        fallback = [strategy for strategy in self._strategies if strategy.name != explicit_strategy]
        return preferred + fallback

DEFAULT_DOWNLOAD_STRATEGY_CHAIN = DownloadStrategyChain(
    [
        M3U8DownloadStrategy(),
        ChunkedDownloadStrategy(),
        FFmpegDownloadStrategy(),
        HttpDownloadStrategy(),
    ]
)
