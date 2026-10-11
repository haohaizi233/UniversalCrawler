"""Bilibili API 响应解析：把视频信息和播放流整理成平台无关结构。"""

from __future__ import annotations

import re
from typing import Any

from app.exceptions import SpiderParseError
from app.spiders.parser_cache import cached_parser_result

class BilibiliParser:
    """只做结构化解析，不访问网络，便于 spider 和测试复用。"""

    def parse_video_info_response(self, data: dict[str, Any]) -> dict[str, Any]:
        """解析视频详情；合集按 section 展平成连续 P 序，普通视频按 pages 保留原页码。"""
        return cached_parser_result(
            "bilibili.video_info",
            data,
            lambda: self._parse_video_info_response_uncached(data),
        )

    def _parse_video_info_response_uncached(self, data: dict[str, Any]) -> dict[str, Any]:
        try:
            info = {
                "bvid": data["bvid"],
                "title": data["title"],
                "owner": data["owner"]["name"],
                "is_season": False,
                "season_id": None,
                "season_title": "",
                "episodes": [],
            }
            if data.get("ugc_season"):
                info["is_season"] = True
                info["season_id"] = data["ugc_season"]["id"]
                info["season_title"] = data["ugc_season"]["title"]
                ep_counter = 1
                # UGC season 的 section/page 层级不稳定，下载文件名只需要稳定顺序。
                for section in data["ugc_season"]["sections"]:
                    for episode in section["episodes"]:
                        info["episodes"].append(
                            {
                                "title": episode["title"],
                                "bvid": episode["bvid"],
                                "cid": episode["cid"],
                                "page_num": ep_counter,
                            }
                        )
                        ep_counter += 1
                return info

            info["season_title"] = data["title"]
            for page in data.get("pages", []):
                info["episodes"].append(
                    {
                        "title": page["part"],
                        "bvid": data["bvid"],
                        "cid": page["cid"],
                        "page_num": page["page"],
                    }
                )
            return info
        except (KeyError, TypeError, IndexError) as exc:
            raise SpiderParseError("Bilibili 视频信息结构不完整") from exc

    def parse_play_url_response(self, resp: dict[str, Any]) -> tuple[str | None, str | None, int]:
        """解析 DASH 播放流；下载层后续会把首个 video/audio 流交给 Bilibili 下载器合并。"""
        return cached_parser_result(
            "bilibili.play_url.v2",
            resp,
            lambda: self._parse_play_url_response_uncached(resp),
        )

    def classify_play_url_response(self, resp: dict[str, Any]) -> tuple[str | None, str | None, int, str]:
        """区分可合并 DASH、含音画的单段 durl，以及无法下载的响应。"""
        if not isinstance(resp, dict) or resp.get("code") != 0 or not isinstance(resp.get("data"), dict):
            return None, None, 0, "missing"
        data = resp["data"]
        dash = data.get("dash")
        if isinstance(dash, dict):
            video_url, quality_id = self._dash_stream_url(dash.get("video"))
            audio_url, _audio_quality = self._dash_stream_url(self._dash_audio_streams(dash))
            if video_url and audio_url:
                return video_url, audio_url, quality_id, "dash"
        durl_url, durl_quality = self._muxed_durl_url(data)
        if durl_url:
            return durl_url, None, durl_quality, "durl"
        if isinstance(dash, dict) or data.get("durl"):
            return None, None, 0, "incomplete"
        return None, None, 0, "missing"

    def _parse_play_url_response_uncached(self, resp: dict[str, Any]) -> tuple[str | None, str | None, int]:
        video_url, audio_url, quality_id, kind = self.classify_play_url_response(resp)
        if kind == "dash":
            return video_url, audio_url, quality_id
        if kind == "durl":
            return video_url, None, quality_id
        if kind == "incomplete":
            raise SpiderParseError("Bilibili 播放流缺少可合并的视频和音频地址")
        return None, None, 0

    @staticmethod
    def _dash_audio_streams(dash: dict[str, Any]) -> list[Any]:
        streams: list[Any] = []
        audio = dash.get("audio")
        if isinstance(audio, list):
            streams.extend(audio)
        for key in ("flac", "dolby"):
            extra = dash.get(key)
            if not isinstance(extra, dict):
                continue
            nested = extra.get("audio")
            if isinstance(nested, list):
                streams.extend(nested)
            elif isinstance(nested, dict):
                streams.append(nested)
        return streams

    @staticmethod
    def _dash_stream_url(streams: Any) -> tuple[str | None, int]:
        if not isinstance(streams, list):
            return None, 0
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            url = stream.get("baseUrl") or stream.get("base_url")
            if not isinstance(url, str) or not url.strip():
                continue
            try:
                quality_id = int(stream.get("id") or 0)
            except (TypeError, ValueError):
                quality_id = 0
            return url.strip(), quality_id
        return None, 0

    @staticmethod
    def _muxed_durl_url(data: dict[str, Any]) -> tuple[str | None, int]:
        """只接受单段 durl；多段 FLV 无法用一个地址表示完整音画。"""
        durl = data.get("durl")
        if not isinstance(durl, list) or len(durl) != 1 or not isinstance(durl[0], dict):
            return None, 0
        entry = durl[0]
        url = entry.get("url") or entry.get("baseUrl")
        if not isinstance(url, str) or not url.strip():
            return None, 0
        try:
            quality_id = int(data.get("quality") or entry.get("id") or 0)
        except (TypeError, ValueError):
            quality_id = 0
        return url.strip(), quality_id

    @staticmethod
    def clean_name(name: str) -> str:
        """清理 Windows 文件名禁用字符，保持平台 task builder 命名一致。"""
        return re.sub(r'[\\/:*?"<>|]', "_", str(name)).strip()
