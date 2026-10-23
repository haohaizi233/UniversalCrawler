import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.core.downloaders.base import BaseDownloader
from app.core.downloaders.strategy import DownloadStrategyChain
from app.exceptions import StreamDownloadError
from app.models import VideoItem

class _ProbeDownloader(BaseDownloader):
    def __init__(self):
        self.http_calls = []

    def download(self, video_item, save_path, progress_callback, check_stop_func):
        raise NotImplementedError

    def _download_http_file(self, **kwargs):
        self.http_calls.append(kwargs)

class _ExplodingStrategy:
    name = "explode"

    def execute(self, _downloader, _request):
        raise StreamDownloadError("boom")

class _UnavailableStrategy:
    name = "unavailable"

    def execute(self, _downloader, _request):
        return False

class _HttpProbeStrategy:
    name = "http"

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, _downloader, _request):
        self.calls += 1
        return True

class DownloadStrategyChainTests(unittest.TestCase):
    @patch("app.core.downloaders.m3u8.N_m3u8DL_RE_Downloader.is_available", return_value=True)
    @patch("app.core.downloaders.m3u8.N_m3u8DL_RE_Downloader.is_m3u8_url", return_value=True)
    @patch("app.core.downloaders.m3u8.N_m3u8DL_RE_Downloader.download")
    @patch("app.core.downloaders.chunked.ChunkedDownloader.download")
    @patch("app.core.downloaders.ffmpeg.FFmpegDownloader.download")
    def test_explicit_http_strategy_short_circuits_other_candidates(
        self,
        mocked_ffmpeg_download,
        mocked_chunked_download,
        mocked_m3u8_download,
        _mocked_is_m3u8_url,
        _mocked_is_available,
    ):
        item = VideoItem(url="https://example.com/video.mp4", title="demo", source="douyin")
        item.meta.update({"download_strategy": "http", "size_mb": 500, "duration": 3600})
        downloader = _ProbeDownloader()

        downloader._download_with_strategy_fallback(
            video_item=item,
            save_path="demo.mp4",
            headers={"User-Agent": "ua"},
            progress_callback=lambda _value: None,
            check_stop_func=lambda: False,
            max_retries=3,
            timeout=10,
            chunk_size=1024,
        )

        self.assertEqual(len(downloader.http_calls), 1)
        mocked_m3u8_download.assert_not_called()
        mocked_chunked_download.assert_not_called()
        mocked_ffmpeg_download.assert_not_called()

    @patch("app.core.downloaders.m3u8.N_m3u8DL_RE_Downloader.is_available", return_value=False)
    @patch("app.core.downloaders.m3u8.N_m3u8DL_RE_Downloader.is_m3u8_url", return_value=True)
    @patch("app.core.downloaders.chunked.ChunkedDownloader.should_use", return_value=False)
    @patch("app.core.downloaders.ffmpeg.FFmpegDownloader.should_use", return_value=False)
    def test_explicit_m3u8_strategy_does_not_save_playlist_via_http(
        self,
        _mocked_ffmpeg_should_use,
        _mocked_chunked_should_use,
        _mocked_is_m3u8_url,
        _mocked_is_available,
    ):
        item = VideoItem(url="https://example.com/live/index.m3u8", title="demo", source="douyin")
        item.meta["download_strategy"] = "m3u8"
        downloader = _ProbeDownloader()

        with self.assertRaises(StreamDownloadError):
            downloader._download_with_strategy_fallback(
                video_item=item,
                save_path="demo.mp4",
                headers={"User-Agent": "ua"},
                progress_callback=lambda _value: None,
                check_stop_func=lambda: False,
                max_retries=3,
                timeout=10,
                chunk_size=1024,
            )

        self.assertEqual(downloader.http_calls, [])

    @patch("app.core.downloaders.chunked.ChunkedDownloader.download", side_effect=StreamDownloadError("chunked failed"))
    @patch("app.core.downloaders.chunked.ChunkedDownloader.should_use", return_value=True)
    @patch("app.core.downloaders.ffmpeg.FFmpegDownloader.should_use", return_value=False)
    @patch("app.core.downloaders.m3u8.N_m3u8DL_RE_Downloader.is_m3u8_url", return_value=False)
    def test_chunked_failure_falls_back_to_http(
        self,
        _mocked_is_m3u8_url,
        _mocked_ffmpeg_should_use,
        _mocked_chunked_should_use,
        _mocked_chunked_download,
    ):
        item = VideoItem(url="https://example.com/video.mp4", title="demo", source="douyin")
        item.meta["size_mb"] = 500
        downloader = _ProbeDownloader()

        downloader._download_with_strategy_fallback(
            video_item=item,
            save_path="demo.mp4",
            headers={"User-Agent": "ua"},
            progress_callback=lambda _value: None,
            check_stop_func=lambda: False,
            max_retries=3,
            timeout=10,
            chunk_size=1024,
        )

        self.assertEqual(len(downloader.http_calls), 1)

    @patch("app.core.downloaders.chunked.ChunkedDownloader.download", side_effect=StreamDownloadError("chunked failed"))
    @patch("app.core.downloaders.chunked.ChunkedDownloader.should_use", return_value=True)
    @patch("app.core.downloaders.ffmpeg.FFmpegDownloader.should_use", return_value=False)
    @patch("app.core.downloaders.m3u8.N_m3u8DL_RE_Downloader.is_m3u8_url", return_value=False)
    def test_chunked_failure_does_not_start_http_after_auto_retry_is_disabled(
        self,
        _mocked_is_m3u8_url,
        _mocked_ffmpeg_should_use,
        _mocked_chunked_should_use,
        _mocked_chunked_download,
    ):
        item = VideoItem(url="https://example.com/video.mp4", title="demo", source="douyin")
        item.meta["size_mb"] = 500
        downloader = _ProbeDownloader()
        config = {
            ("download", "auto_retry"): False,
            ("download", "max_retries"): 3,
            ("download", "speed_limit_kb"): 0,
        }

        with patch("app.core.downloaders.base.cfg.get", side_effect=lambda section, key, default=None: config.get((section, key), default)):
            with self.assertRaises(StreamDownloadError):
                downloader._download_with_strategy_fallback(
                    video_item=item,
                    save_path="demo.mp4",
                    headers={"User-Agent": "ua"},
                    progress_callback=lambda _value: None,
                    check_stop_func=lambda: False,
                    max_retries=3,
                    timeout=10,
                    chunk_size=1024,
                )

        self.assertEqual(downloader.http_calls, [])
        _mocked_chunked_download.assert_called_once()

    def test_invalid_explicit_strategy_raises_clear_error(self):
        item = VideoItem(url="https://example.com/video.mp4", title="demo", source="douyin")
        item.meta["download_strategy"] = "nonexistent"
        downloader = _ProbeDownloader()

        with self.assertRaisesRegex(ValueError, "未知下载策略"):
            downloader._download_with_strategy_fallback(
                video_item=item,
                save_path="demo.mp4",
                headers={"User-Agent": "ua"},
                progress_callback=lambda _value: None,
                check_stop_func=lambda: False,
                max_retries=3,
                timeout=10,
                chunk_size=1024,
            )

    def test_backend_failure_stops_strategy_fallback_when_auto_retry_is_disabled(self):
        fallback = _HttpProbeStrategy()
        chain = DownloadStrategyChain([_ExplodingStrategy(), fallback])
        request = SimpleNamespace(
            video_item=VideoItem(url="https://example.com/video.mp4", title="demo", source="douyin"),
            headers={},
            explicit_strategy="",
            auto_retry=False,
            error_message="下载失败",
            context=SimpleNamespace(trace_id="trace-strategy"),
        )
        downloader = _ProbeDownloader()

        with self.assertRaises(StreamDownloadError):
            chain.execute(downloader, request)
        self.assertEqual(fallback.calls, 0)

    def test_unavailable_strategy_still_allows_capability_fallback_when_auto_retry_is_disabled(self):
        fallback = _HttpProbeStrategy()
        chain = DownloadStrategyChain([_UnavailableStrategy(), fallback])
        request = SimpleNamespace(
            video_item=VideoItem(url="https://example.com/video.mp4", title="demo", source="douyin"),
            headers={},
            explicit_strategy="",
            auto_retry=False,
            error_message="下载失败",
            context=SimpleNamespace(trace_id="trace-strategy"),
        )

        chain.execute(_ProbeDownloader(), request)

        self.assertEqual(fallback.calls, 1)

    def test_backend_failure_still_falls_back_when_auto_retry_is_enabled(self):
        fallback = _HttpProbeStrategy()
        chain = DownloadStrategyChain([_ExplodingStrategy(), fallback])
        request = SimpleNamespace(
            video_item=VideoItem(url="https://example.com/video.mp4", title="demo", source="douyin"),
            headers={},
            explicit_strategy="",
            auto_retry=True,
            error_message="下载失败",
            context=SimpleNamespace(trace_id="trace-strategy"),
        )
        downloader = _ProbeDownloader()

        chain.execute(downloader, request)

        self.assertEqual(fallback.calls, 1)

if __name__ == "__main__":
    unittest.main()
