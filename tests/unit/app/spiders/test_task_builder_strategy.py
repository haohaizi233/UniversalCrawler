"""任务构建器把下载策略交给下载层，而不是猜错传输方式。"""

from __future__ import annotations

from app.spiders.base_task_builder import BaseTaskBuilder, infer_media_download_strategy
from app.spiders.kuaishou.task_builder import KuaishouTaskBuilder
from app.spiders.xiaohongshu.task_builder import XiaohongshuTaskBuilder


def test_missing_download_strategy_stays_blank_for_auto_selection() -> None:
    meta = BaseTaskBuilder().build_download_meta(trace_id="trace-auto", referer="https://example.com")

    assert "download_strategy" in meta
    assert meta["download_strategy"] == ""


def test_xiaohongshu_video_does_not_force_http() -> None:
    items = XiaohongshuTaskBuilder().build_items(
        {
            "note_id": "note-hls",
            "type": "video",
            "title": "视频笔记",
            "user": {"nickname": "作者"},
            "video_candidates": ["https://cdn.example.com/master.m3u8"],
        },
        trace_id_factory=lambda suffix: f"{suffix}-trace",
        referer="https://www.xiaohongshu.com/explore/note-hls",
        user_agent="ua-demo",
        cookie_str="a1=demo",
    )

    assert items[0].meta["download_strategy"] == ""


def test_kuaishou_segment_is_not_classified_as_http() -> None:
    assert infer_media_download_strategy("https://cdn.example.com/live.m3u8") == "m3u8"
    assert infer_media_download_strategy("https://cdn.example.com/seg-1.ts?token=1") == "m3u8"
    assert infer_media_download_strategy("https://cdn.example.com/video.mp4") == "http"

    meta = KuaishouTaskBuilder().build_download_meta(
        "trace-seg",
        "https://www.kuaishou.com/",
        "https://cdn.example.com/hls/segment-1.m4s",
    )

    assert meta["download_strategy"] == "m3u8"
