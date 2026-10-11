"""MissAV 详情页嗅探到的 m3u8 选择。"""

from __future__ import annotations

import re
from dataclasses import dataclass


_STREAM_INF_RE = re.compile(r"(?im)^[ \t]*#EXT-X-STREAM-INF:")
_BANDWIDTH_RE = re.compile(r"(?i)\b(?:AVERAGE-)?BANDWIDTH=(\d+)\b")
_RESOLUTION_RE = re.compile(r"(?i)\bRESOLUTION=(\d+)x(\d+)\b")
_URL_PIXELS_RE = re.compile(r"(?<!\d)(\d{3,5})x(\d{3,5})(?!\d)")
_URL_HEIGHT_RE = re.compile(r"(?i)(?<!\d)(\d{3,4})p(?!\d)")


@dataclass(frozen=True)
class ConfirmedPlaylist:
    """一次 200/206 且正文可播放的播放列表。"""

    url: str
    status: int
    headers: dict[str, str]
    body: str
    is_master: bool
    bandwidth: int
    pixels: int


def _is_master_playlist(text: str) -> bool:
    return _STREAM_INF_RE.search(text) is not None


def _rank(text: str, url: str) -> tuple[int, int]:
    bandwidths = [int(value) for value in _BANDWIDTH_RE.findall(text)]
    pixels = [int(width) * int(height) for width, height in _RESOLUTION_RE.findall(text)]
    if not pixels:
        pixels = [int(width) * int(height) for width, height in _URL_PIXELS_RE.findall(url)]
    if not pixels:
        heights = [int(value) for value in _URL_HEIGHT_RE.findall(url)]
        pixels = [height * height for height in heights]
    return max(bandwidths or [0]), max(pixels or [0])


def _outranks(candidate: ConfirmedPlaylist, current: ConfirmedPlaylist) -> bool:
    if candidate.is_master != current.is_master:
        return candidate.is_master
    candidate_rank = (candidate.bandwidth, candidate.pixels)
    current_rank = (current.bandwidth, current.pixels)
    if candidate_rank != current_rank:
        return candidate_rank > current_rank
    return candidate.url == current.url


class PlaylistCapture:
    """只保留主列表；没有主列表时保留清晰度最高的媒体列表。"""

    def __init__(self) -> None:
        self.selected: ConfirmedPlaylist | None = None

    def observe(
        self,
        url: str,
        status: int,
        headers: dict[str, str] | None,
        body: str | None,
    ) -> None:
        if status not in (200, 206):
            return
        text = str(body or "")
        if "#EXTM3U" not in text:
            return
        bandwidth, pixels = _rank(text, url)
        candidate = ConfirmedPlaylist(
            url=str(url),
            status=int(status),
            headers=dict(headers or {}),
            body=text,
            is_master=_is_master_playlist(text),
            bandwidth=bandwidth,
            pixels=pixels,
        )
        current = self.selected
        if current is None or _outranks(candidate, current):
            self.selected = candidate

    @property
    def url(self) -> str | None:
        return None if self.selected is None else self.selected.url

    @property
    def cache(self) -> dict[str, str]:
        if self.selected is None:
            return {}
        return {self.selected.url: self.selected.body}

    def submission(self) -> tuple[str, dict[str, str]] | None:
        """提交地址必须就是缓存中那份成功正文。"""
        selected = self.selected
        if selected is None:
            return None
        cached = self.cache.get(selected.url)
        if cached != selected.body:
            return None
        return selected.url, {selected.url: selected.body}
