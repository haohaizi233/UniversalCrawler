from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("url", "platform", "resource_kind", "expected_admittable", "expected_reason"),
    (
        (
            "https://cdn.example.com/video.mp4",
            "generic",
            "media",
            False,
            "candidate_downloader_unavailable",
        ),
        (
            "https://www.douyin.com/media/master.m3u8",
            "douyin",
            "playlist",
            False,
            "candidate_downloader_unavailable",
        ),
        (
            "https://missav.ai/media/master.m3u8",
            "missav",
            "playlist",
            True,
            "",
        ),
        (
            "https://www.bilibili.com/media/video.mp4",
            "bilibili",
            "media",
            True,
            "",
        ),
    ),
)
def test_candidate_admission_capability_is_the_single_server_policy(
    url: str,
    platform: str,
    resource_kind: str,
    expected_admittable: bool,
    expected_reason: str,
) -> None:
    from app.core.tools.link_candidate_admission_policy import (
        link_candidate_admission_capability,
    )

    capability = link_candidate_admission_capability(
        url,
        platform=platform,
        resource_kind=resource_kind,
    )

    assert capability.admittable is expected_admittable
    assert capability.reason_code == expected_reason

