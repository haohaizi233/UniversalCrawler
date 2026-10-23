"""Download retry preferences control the number of real transfer attempts."""

from unittest.mock import patch

import pytest
import requests

from app.core.downloaders.base import BaseDownloader
from app.core.downloaders.external import FFmpegExternalTool
from app.core.downloaders.m3u8 import N_m3u8DL_RE_Downloader
from app.exceptions import ExternalToolError, StreamDownloadError
from app.models import VideoItem


class _FailingResponse:
    status_code = 503
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        raise requests.HTTPError("503 service unavailable")


class _HttpProbeDownloader(BaseDownloader):
    source_id = "test"

    def download(self, video_item, save_path, progress_callback, check_stop_func):
        raise NotImplementedError


@pytest.mark.parametrize(
    ("auto_retry", "max_retries", "expected_attempts"),
    [(False, 3, 1), (True, 3, 4), (True, 0, 1)],
)
def test_http_download_attempt_count_follows_auto_retry(auto_retry, max_retries, expected_attempts, tmp_path):
    config = {
        ("download", "auto_retry"): auto_retry,
        ("download", "max_retries"): max_retries,
        ("download", "speed_limit_kb"): 0,
    }
    attempts = 0

    def fail_request(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return _FailingResponse()

    def config_get(section, key, default=None):
        return config.get((section, key), default)

    downloader = _HttpProbeDownloader()
    with (
        patch("app.core.downloaders.base.cfg.get", side_effect=config_get),
        patch("app.core.downloaders.base.requests.get", side_effect=fail_request),
        patch("app.core.downloaders.base.time.sleep"),
        pytest.raises(StreamDownloadError),
    ):
        downloader._download_http_file(
            url="https://cdn.example/video.mp4",
            save_path=str(tmp_path / "video.mp4"),
            headers={"User-Agent": "test"},
            check_stop_func=lambda: False,
            max_retries=max_retries,
        )

    assert attempts == expected_attempts


def test_m3u8_stops_after_backend_failure_when_auto_retry_is_disabled(tmp_path):
    config = {
        ("download", "auto_retry"): False,
        ("download", "max_retries"): 3,
    }
    downloader = N_m3u8DL_RE_Downloader()
    item = VideoItem(
        url="https://cdn.example/index.m3u8",
        title="demo",
        source="missav",
    )
    with (
        patch("app.core.downloaders.m3u8.cfg.get", side_effect=lambda section, key, default=None: config.get((section, key), default)),
        patch.object(N_m3u8DL_RE_Downloader, "_should_try_nm3u8_first", return_value=True),
        patch.object(N_m3u8DL_RE_Downloader, "_should_try_curl_cffi_first", return_value=True),
        patch("app.core.downloaders.m3u8.NM3U8DLREExternalTool.resolve_executable", return_value="nm3u8dl"),
        patch.object(downloader, "_download_with_nm3u8_external", side_effect=ExternalToolError("backend failed")),
        patch.object(downloader, "_download_with_curl_cffi_hls") as curl_fallback,
        patch.object(downloader, "_download_with_yt_dlp_fallback") as ytdlp_fallback,
        patch.object(downloader, "_cleanup_external_temp_files"),
        pytest.raises(ExternalToolError, match="backend failed"),
    ):
        downloader.download(item, str(tmp_path / "demo.mp4"), lambda *_args, **_kwargs: None, lambda: False)

    curl_fallback.assert_not_called()
    ytdlp_fallback.assert_not_called()


@pytest.mark.parametrize(
    ("auto_retry", "max_retries", "expect_reconnect"),
    [(False, 3, False), (True, 3, True), (True, 0, False)],
)
def test_ffmpeg_transport_reconnect_follows_effective_retry_budget(
    auto_retry,
    max_retries,
    expect_reconnect,
    tmp_path,
):
    config = {
        ("download", "auto_retry"): auto_retry,
        ("download", "max_retries"): max_retries,
    }
    with patch("app.core.downloaders.external.cfg.get", side_effect=lambda section, key, default=None: config.get((section, key), default)):
        command = FFmpegExternalTool.build_download_command(
            "ffmpeg",
            "https://cdn.example/index.m3u8",
            str(tmp_path / "demo.mp4"),
            {"User-Agent": "test"},
            timeout_seconds=10,
        )

    assert ("-reconnect" in command) is expect_reconnect
    assert ("-reconnect_at_eof" in command) is expect_reconnect
    assert ("-reconnect_streamed" in command) is expect_reconnect


def test_yt_dlp_does_not_retry_network_failure_when_auto_retry_is_disabled(tmp_path):
    from yt_dlp.utils import YoutubeDLError

    config = {
        ("download", "auto_retry"): False,
        ("download", "max_retries"): 3,
        ("download", "resume_enabled"): True,
        ("download", "speed_limit_kb"): 0,
    }
    downloader = N_m3u8DL_RE_Downloader()
    item = VideoItem(url="https://cdn.example/index.m3u8", title="demo", source="missav")

    def config_get(section, key, default=None):
        return config.get((section, key), default)

    with (
        patch("app.core.downloaders.m3u8_ytdlp.cfg.get", side_effect=config_get),
        patch.object(
            downloader,
            "_run_yt_dlp",
            side_effect=[YoutubeDLError("HTTP Error 503"), YoutubeDLError("HTTP Error 503")],
        ) as run_yt_dlp,
        pytest.raises(ExternalToolError),
    ):
        downloader._download_with_yt_dlp_fallback(
            item,
            str(tmp_path / "demo.mp4"),
            {"User-Agent": "test"},
            None,
            lambda *_args: None,
            lambda: False,
        )

    assert run_yt_dlp.call_count == 1


def test_yt_dlp_can_retry_capability_negotiation_without_impersonation_when_retries_disabled(tmp_path):
    from yt_dlp.utils import YoutubeDLError

    config = {
        ("download", "auto_retry"): False,
        ("download", "max_retries"): 0,
        ("download", "resume_enabled"): True,
        ("download", "speed_limit_kb"): 0,
    }
    downloader = N_m3u8DL_RE_Downloader()
    item = VideoItem(url="https://cdn.example/index.m3u8", title="demo", source="missav")

    def config_get(section, key, default=None):
        return config.get((section, key), default)

    with (
        patch("app.core.downloaders.m3u8_ytdlp.cfg.get", side_effect=config_get),
        patch.object(
            downloader,
            "_run_yt_dlp",
            side_effect=[
                YoutubeDLError("no impersonate target is available"),
                YoutubeDLError("HTTP Error 503"),
            ],
        ) as run_yt_dlp,
        pytest.raises(ExternalToolError),
    ):
        downloader._download_with_yt_dlp_fallback(
            item,
            str(tmp_path / "demo.mp4"),
            {"User-Agent": "test"},
            None,
            lambda *_args: None,
            lambda: False,
        )

    assert run_yt_dlp.call_count == 2
    assert "impersonate" in run_yt_dlp.call_args_list[0].args[1]
    assert "impersonate" not in run_yt_dlp.call_args_list[1].args[1]
