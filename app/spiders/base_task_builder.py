"""平台任务装配公共工具，负责把解析结果整理成下载层可识别的 meta。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

_HLS_SEGMENT_SUFFIXES = (".ts", ".m4s", ".aac", ".m4a", ".vtt", ".mp2t")


def infer_media_download_strategy(stream_url: str) -> str:
    """播放列表和 HLS 分片都显式走 m3u8，其余渐进文件走 HTTP。

    只看 ``.m3u8`` 会把分片地址判成 HTTP，策略链会先把分片当单文件下载。
    """
    lowered = str(stream_url or "").strip().lower()
    path = urlsplit(lowered).path
    if ".m3u8" in lowered or path.endswith(".m3u8"):
        return "m3u8"
    if path.endswith(_HLS_SEGMENT_SUFFIXES):
        return "m3u8"
    if "/hls/" in path or path.endswith("/hls") or "mpegurl" in lowered:
        return "m3u8"
    return "http"


class BaseTaskBuilder:
    """平台任务装配公共基类。

    各平台仍可保留自己的高层装配方法，但底层下载 meta 统一走这里，
    这样 trace_id / referer / ua / proxy / strategy 等字段的结构会更一致。
    """

    def build_download_meta(
        self,
        *,
        trace_id: str,
        referer: str | None = None,
        user_agent: str | None = None,
        proxy: str | None = None,
        download_strategy: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """构建下载 meta。空的 download_strategy 保留为空字符串，表示自动选择。"""
        payload = {
            "trace_id": trace_id,
            "referer": referer,
            "ua": user_agent,
            "proxy": proxy,
            "download_strategy": download_strategy,
            **extra,
        }
        strategy = payload.get("download_strategy")
        cleaned = {
            key: value
            for key, value in payload.items()
            if key != "download_strategy" and value not in (None, "", [], {})
        }
        # 缺省策略必须留在 meta 里。丢掉之后下载器会按 URL 猜，HLS 可能被 HTTP 抢走。
        cleaned["download_strategy"] = "" if strategy is None else strategy
        return cleaned
