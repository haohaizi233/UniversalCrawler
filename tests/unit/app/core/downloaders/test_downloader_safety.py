"""下载器安全边界：协议、播放列表、清理、签名和 B 站降级。"""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app.core.downloaders.base import (
    BaseDownloader,
    HlsPlaylistResponse,
    assert_publishable_media,
    http_payload_is_hls_playlist,
)
from app.core.downloaders.bilibili import BilibiliDownloader
from app.core.downloaders.external import FFmpegExternalTool, NM3U8DLREExternalTool
from app.core.downloaders.ffmpeg import FFmpegDownloader
from app.core.download_manager import DownloadWorker
from app.core.downloaders.hls_proxy import (
    looks_like_hls_playlist,
    looks_like_hls_playlist_url,
    prepare_hls_curl_request,
)
from app.core.downloaders.base import single_file_http_forbidden
from app.core.downloaders.m3u8 import N_m3u8DL_RE_Downloader
from app.core.downloaders.strategy import HttpDownloadStrategy
from app.exceptions import ExternalToolError, StreamDownloadError
from app.models import VideoItem
from shared.runtime_options import DomainPolicyEngine, DomainPolicyViolation

_FTYP = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00"


class _BytesResponse:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body = body
        self.headers = headers
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 8192):
        del chunk_size
        if self._body:
            yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> bool:
        return False


class _ProbeDownloader(BaseDownloader):
    source_id = "douyin"

    def download(self, video_item, save_path, progress_callback, check_stop_func):
        raise NotImplementedError


def test_ffmpeg_command_accepts_only_http_inputs_and_clean_headers(tmp_path: Path) -> None:
    command = FFmpegExternalTool.build_download_command(
        "ffmpeg",
        "https://cdn.example/video.mp4",
        str(tmp_path / "video.mp4"),
        {"User-Agent": "ua-demo", "Referer": "https://www.douyin.com/"},
    )
    assert command[command.index("-i") + 1] == "https://cdn.example/video.mp4"

    for blocked in ("file:///tmp/secret", "concat:list.txt", "subfile:clip", "data:text/plain,hi"):
        with pytest.raises(DomainPolicyViolation):
            FFmpegExternalTool.build_download_command(
                "ffmpeg",
                blocked,
                str(tmp_path / "video.mp4"),
                {"User-Agent": "ua-demo"},
            )
    with pytest.raises(DomainPolicyViolation):
        FFmpegExternalTool.build_download_command(
            "ffmpeg",
            "https://cdn.example/video.mp4",
            str(tmp_path / "video.mp4"),
            {"User-Agent": "bad\r\nX: 1", "Referer": "https://www.douyin.com/"},
        )
    with pytest.raises(DomainPolicyViolation):
        FFmpegExternalTool.build_download_command(
            "ffmpeg",
            "https://cdn.example/video.mp4",
            str(tmp_path / "video.mp4"),
            {"User-Agent": "ua", "Referer": "https://evil.example/\nCookie: a=b"},
            proxy="http://127.0.0.1:8080\x00",
        )


def test_ffmpeg_without_public_policy_receives_loopback_url(tmp_path: Path) -> None:
    save_path = tmp_path / "demo.mp4"

    class _Proxy:
        url = "http://127.0.0.1:9/hls?u=pinned"

        def stop(self) -> None:
            return None

    def fake_popen(command, **_kwargs):
        assert command[command.index("-i") + 1] == _Proxy.url
        assert "file:" not in command
        Path(command[-1]).write_bytes(_FTYP)
        process = Mock()
        process.stderr.readline.side_effect = [b"progress=end\n", b""]
        process.poll.return_value = 0
        process.returncode = 0
        process.wait.return_value = 0
        return process

    item = VideoItem(url="https://cdn.example/video.mp4", title="demo", source="douyin")
    with (
        patch("app.core.downloaders.ffmpeg.FFmpegExternalTool.resolve_executable", return_value="ffmpeg"),
        patch("app.core.downloaders.ffmpeg.subprocess.Popen", side_effect=fake_popen),
        patch.object(FFmpegDownloader, "_start_validated_media_proxy", return_value=_Proxy()) as started,
    ):
        FFmpegDownloader().download(item, str(save_path), lambda *_args, **_kwargs: None, lambda: False)

    started.assert_called_once()
    assert save_path.read_bytes() == _FTYP


def test_ffmpeg_expected_duration_must_match(tmp_path: Path) -> None:
    save_path = tmp_path / "demo.mp4"

    class _Proxy:
        url = "http://127.0.0.1:9/hls?u=pinned"

        def stop(self) -> None:
            return None

    def fake_popen(command, **_kwargs):
        Path(command[-1]).write_bytes(_FTYP)
        process = Mock()
        process.stderr.readline.side_effect = [b"out_time_ms=1000\n", b"progress=end\n", b""]
        process.poll.return_value = 0
        process.returncode = 0
        process.wait.return_value = 0
        return process

    item = VideoItem(url="https://cdn.example/video.mp4", title="demo", source="douyin")
    item.meta["duration"] = 100

    def fake_cfg(section, key, default=None):
        del section
        if key == "max_retries":
            return 0
        return default

    with (
        patch("app.core.downloaders.ffmpeg.FFmpegExternalTool.resolve_executable", return_value="ffmpeg"),
        patch("app.core.downloaders.ffmpeg.FFmpegExternalTool.probe_duration_seconds", return_value=(False, None)),
        patch("app.core.downloaders.ffmpeg.cfg.get", side_effect=fake_cfg),
        patch("app.core.downloaders.ffmpeg.subprocess.Popen", side_effect=fake_popen),
        patch.object(FFmpegDownloader, "_start_validated_media_proxy", return_value=_Proxy()),
        pytest.raises(ExternalToolError, match="时长"),
    ):
        FFmpegDownloader().download(item, str(save_path), lambda *_args, **_kwargs: None, lambda: False)

    assert not save_path.exists()


def test_hls_url_detection_uses_suffix_and_content_type() -> None:
    assert NM3U8DLREExternalTool.is_m3u8_url("https://cdn.example/live/index.m3u8")
    assert NM3U8DLREExternalTool.is_m3u8_url("https://cdn.example/live/index.m3u8?token=1")
    assert not NM3U8DLREExternalTool.is_m3u8_url("https://cdn.example/m3u8/video.mp4")
    assert not NM3U8DLREExternalTool.is_m3u8_url("https://cdn.example/not-m3u8-playlist.mp4")
    assert NM3U8DLREExternalTool.is_m3u8_url(
        "https://cdn.example/watch/stream",
        "application/vnd.apple.mpegurl",
    )


def test_explicit_http_playlist_body_continues_to_hls(tmp_path: Path) -> None:
    item = VideoItem(url="https://cdn.example/watch/stream", title="demo", source="douyin")
    item.meta["download_strategy"] = "http"
    save_path = tmp_path / "demo.mp4"
    response = _BytesResponse(b"#EXTM3U\n#EXTINF:1,\nseg.ts\n", {"content-type": "application/vnd.apple.mpegurl"})
    with (
        patch("app.core.downloaders.base.requests.get", return_value=response),
        patch("app.core.downloaders.base.cfg.get", return_value=0),
        patch("app.core.downloaders.m3u8.N_m3u8DL_RE_Downloader.is_available", return_value=True),
        patch("app.core.downloaders.m3u8.N_m3u8DL_RE_Downloader.download") as hls_download,
    ):
        _ProbeDownloader()._download_with_strategy_fallback(
            video_item=item,
            save_path=str(save_path),
            headers={"User-Agent": "ua"},
            progress_callback=lambda *_args, **_kwargs: None,
            check_stop_func=lambda: False,
            max_retries=0,
            timeout=5,
            chunk_size=1024,
        )

    hls_download.assert_called_once()
    assert not save_path.exists()
    assert item.meta["content_type"] == "application/vnd.apple.mpegurl"


def test_substring_m3u8_url_with_media_body_stays_http(tmp_path: Path) -> None:
    item = VideoItem(url="https://cdn.example/m3u8/video.mp4", title="demo", source="douyin")
    save_path = tmp_path / "demo.mp4"
    response = _BytesResponse(_FTYP, {"content-type": "video/mp4", "content-length": str(len(_FTYP))})
    with (
        patch("app.core.downloaders.base.requests.get", return_value=response),
        patch("app.core.downloaders.base.cfg.get", return_value=0),
        patch("app.core.downloaders.m3u8.N_m3u8DL_RE_Downloader.download") as hls_download,
    ):
        _ProbeDownloader()._download_with_strategy_fallback(
            video_item=item,
            save_path=str(save_path),
            headers={"User-Agent": "ua"},
            progress_callback=lambda *_args, **_kwargs: None,
            check_stop_func=lambda: False,
            max_retries=0,
            timeout=5,
            chunk_size=1024,
        )

    hls_download.assert_not_called()
    assert save_path.read_bytes() == _FTYP


def test_http_without_content_length_rejects_empty_body(tmp_path: Path) -> None:
    save_path = tmp_path / "demo.mp4"
    response = _BytesResponse(b"", {})
    with (
        patch("app.core.downloaders.base.requests.get", return_value=response),
        patch("app.core.downloaders.base.cfg.get", return_value=0),
        pytest.raises(StreamDownloadError, match="Content-Length"),
    ):
        _ProbeDownloader()._download_http_file(
            url="https://cdn.example/video.mp4",
            save_path=str(save_path),
            headers={"User-Agent": "ua"},
            check_stop_func=lambda: False,
            max_retries=0,
        )
    assert not save_path.exists()


def test_media_signature_rules(tmp_path: Path) -> None:
    mp4 = tmp_path / "ok.mp4"
    mp4.write_bytes(_FTYP)
    assert_publishable_media(str(mp4), str(mp4))

    shifted = tmp_path / "shifted.mp4"
    shifted.write_bytes(b"XXXXX" + b"ftyp" + b"rest")
    with pytest.raises(StreamDownloadError, match="ftyp"):
        assert_publishable_media(str(shifted), str(shifted))

    html = tmp_path / "page.mp4"
    html.write_bytes(b"<!DOCTYPE html><html></html>")
    with pytest.raises(StreamDownloadError, match="HTML"):
        assert_publishable_media(str(html), str(html))

    executable = tmp_path / "app.mp4"
    executable.write_bytes(b"MZ" + b"\x00" * 16)
    with pytest.raises(StreamDownloadError, match="MZ"):
        assert_publishable_media(str(executable), str(executable))

    bad_flv = tmp_path / "clip.flv"
    bad_flv.write_bytes(b"FLV\x00rest")
    with pytest.raises(StreamDownloadError, match="FLV"):
        assert_publishable_media(str(bad_flv), str(bad_flv))

    good_flv = tmp_path / "clip-ok.flv"
    good_flv.write_bytes(b"FLV\x01" + b"\x00" * 8)
    assert_publishable_media(str(good_flv), str(good_flv))
    with pytest.raises(StreamDownloadError, match="download_manager.py"):
        assert_publishable_media(str(html), str(html))


def test_failure_cleanup_keeps_same_stem_user_files_and_foreign_hls_dirs(tmp_path: Path) -> None:
    user_ts = tmp_path / "标题.ts"
    user_ts.write_bytes(b"user-media")
    foreign = tmp_path / "标题_curl_cffi_hls"
    foreign.mkdir()
    (foreign / "secret.txt").write_text("keep", encoding="utf-8")
    playwright_dir = tmp_path / "other_playwright_hls"
    playwright_dir.mkdir()
    owned_root = tmp_path / N_m3u8DL_RE_Downloader.NM3U8_TEMP_ROOT_NAME
    owned = owned_root / "ucp-task"
    owned.mkdir(parents=True)
    (owned / N_m3u8DL_RE_Downloader._OWNED_MARKER_NAME).write_text(
        N_m3u8DL_RE_Downloader._OWNED_MARKER_TEXT,
        encoding="ascii",
    )
    unmarked = owned_root / "ucp-not-ours"
    unmarked.mkdir()
    (unmarked / "keep.txt").write_text("stay", encoding="utf-8")

    cleaned = N_m3u8DL_RE_Downloader.sweep_orphaned_workspaces([tmp_path])

    assert cleaned == 1
    assert not owned.exists()
    assert unmarked.exists()
    assert user_ts.read_bytes() == b"user-media"
    assert foreign.exists()
    assert playwright_dir.exists()
    N_m3u8DL_RE_Downloader._cleanup_external_temp_files(str(tmp_path / "标题.mp4"))
    assert user_ts.read_bytes() == b"user-media"


def test_owned_tree_removal_refuses_reparse_points(tmp_path: Path) -> None:
    workspace = tmp_path / "ucp-demo"
    workspace.mkdir()
    (workspace / "file.txt").write_text("data", encoding="utf-8")

    with patch.object(N_m3u8DL_RE_Downloader, "_is_reparse_point", return_value=True):
        with pytest.raises(OSError, match="reparse"):
            N_m3u8DL_RE_Downloader._remove_owned_tree(workspace)

    assert (workspace / "file.txt").read_text(encoding="utf-8") == "data"


def test_nm3u8_zero_exit_without_payload_is_not_success(tmp_path: Path) -> None:
    missing = tmp_path / "ucp-media.mp4"
    with pytest.raises(ExternalToolError, match="空|没有"):
        N_m3u8DL_RE_Downloader._locate_staged_media(missing)
    missing.write_bytes(b"")
    with pytest.raises(ExternalToolError, match="空|没有"):
        N_m3u8DL_RE_Downloader._locate_staged_media(missing)


def test_nm3u8_header_rejects_control_characters() -> None:
    with pytest.raises(DomainPolicyViolation, match="header"):
        NM3U8DLREExternalTool.build_download_command(
            "N_m3u8DL-RE",
            "https://cdn.example/index.m3u8",
            "out.mp4",
            "ua",
            "https://example.com/",
            extra_headers={"X-Token": "secret\r\nCookie: a=b"},
        )


def test_proxy_hop_rejects_non_public_target() -> None:
    def resolver(host: str, port: int | None, **_kwargs):
        del host
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 443))]

    policy = DomainPolicyEngine(resolver=resolver)
    with pytest.raises(DomainPolicyViolation):
        prepare_hls_curl_request(
            "https://cdn.example/index.m3u8",
            policy,
            "http://proxy.example:8080",
        )


def test_bilibili_cdn_headers_drop_session_cookies_and_missing_audio_is_degraded() -> None:
    headers = BilibiliDownloader._cdn_request_headers(
        {
            "User-Agent": "ua",
            "Referer": "https://www.bilibili.com",
            "Cookie": "SESSDATA=secret; bili_jct=token",
        }
    )
    assert "Cookie" not in headers
    assert headers["User-Agent"] == "ua"
    with pytest.raises(StreamDownloadError, match="缺少音频"):
        BilibiliDownloader._completion_labels(audio_present=False)
    complete = BilibiliDownloader._completion_labels(audio_present=True)
    assert complete["merge_status"] == "合并完成"


def test_playlist_helper_ignores_m3u8_substring_in_unrelated_bytes(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(_FTYP)
    assert not http_payload_is_hls_playlist("video/mp4", str(path))
    assert http_payload_is_hls_playlist("application/vnd.apple.mpegurl", str(path))
    assert HttpDownloadStrategy.name == "http"
    with pytest.raises(HlsPlaylistResponse):
        raise HlsPlaylistResponse("playlist")


def test_hls_playlist_detection_requires_path_suffix_or_body() -> None:
    nested = "https://cdn.example/m3u8/video.mp4"
    assert not looks_like_hls_playlist_url(nested, "")
    assert not looks_like_hls_playlist(nested, "video/mp4", b"ftyp")
    assert looks_like_hls_playlist_url("https://cdn.example/live/index.m3u8?token=1", "")
    assert looks_like_hls_playlist(nested, "video/mp4", b"#EXTM3U\n#EXTINF:1,\nseg.ts\n")


def test_hostname_upstream_proxy_is_refused_and_literal_ip_stays_pinned() -> None:
    def resolver(host: str, port: int | None, **_kwargs):
        del host
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]

    policy = DomainPolicyEngine(resolver=resolver)
    with pytest.raises(DomainPolicyViolation, match="DNS"):
        prepare_hls_curl_request(
            "https://cdn.example/index.m3u8",
            policy,
            "http://proxy.example:8080",
        )

    pinned_url, _options = prepare_hls_curl_request(
        "https://93.184.216.34/video.mp4",
        DomainPolicyEngine(),
        "http://proxy.example:8080",
    )
    assert "93.184.216.34" in pinned_url
    assert "cdn.example" not in pinned_url


def test_cleanup_removes_ytdlp_parts_but_not_same_stem_media(tmp_path: Path) -> None:
    media = tmp_path / "标题.mp4"
    media.write_bytes(b"media")
    user_ts = tmp_path / "标题.ts"
    user_ts.write_bytes(b"user-media")
    part = tmp_path / "标题.mp4.part"
    part.write_bytes(b"partial")
    ytdl = tmp_path / "标题.ytdl"
    ytdl.write_text("meta", encoding="utf-8")
    other = tmp_path / "标题-其他.part"
    other.write_bytes(b"keep")

    N_m3u8DL_RE_Downloader._cleanup_external_temp_files(str(media))

    assert media.read_bytes() == b"media"
    assert user_ts.read_bytes() == b"user-media"
    assert other.read_bytes() == b"keep"
    assert not part.exists()
    assert not ytdl.exists()


def test_temp_root_listing_limit_and_oserror_fail(tmp_path: Path) -> None:
    root = tmp_path / N_m3u8DL_RE_Downloader.NM3U8_TEMP_ROOT_NAME
    root.mkdir()
    for name in ("ucp-a", "ucp-b"):
        child = root / name
        child.mkdir()
        (child / N_m3u8DL_RE_Downloader._OWNED_MARKER_NAME).write_text(
            N_m3u8DL_RE_Downloader._OWNED_MARKER_TEXT,
            encoding="ascii",
        )

    with patch.object(N_m3u8DL_RE_Downloader, "NM3U8_SWEEP_ENTRY_LIMIT", 1):
        with pytest.raises(OSError, match="entries"):
            N_m3u8DL_RE_Downloader.sweep_orphaned_workspaces([tmp_path])
    assert (root / "ucp-a").exists()
    assert (root / "ucp-b").exists()

    with patch("app.core.downloaders.m3u8.os.scandir", side_effect=OSError("listing denied")):
        with pytest.raises(OSError, match="listing denied"):
            N_m3u8DL_RE_Downloader.sweep_orphaned_workspaces([tmp_path])


def test_nm3u8_without_public_policy_does_not_receive_original_url() -> None:
    item = VideoItem(url="https://cdn.example/live/index.m3u8", title="live", source="douyin")

    class _Proxy:
        url = "http://127.0.0.1:9/hls?u=pinned"

        def progress_snapshot(self) -> tuple[int, int]:
            return 0, 0

        def stop(self) -> None:
            return None

    seen: dict[str, object] = {}

    def capture(*args, **kwargs):
        seen["url"] = args[3]
        seen["proxy"] = args[6]
        seen["local"] = kwargs.get("local_proxy")

    with (
        patch.object(N_m3u8DL_RE_Downloader, "_start_local_hls_proxy", return_value=_Proxy()),
        patch.object(N_m3u8DL_RE_Downloader, "_run_nm3u8_external_command", side_effect=capture),
    ):
        N_m3u8DL_RE_Downloader()._download_with_nm3u8_external(
            item,
            "demo.mp4",
            "N_m3u8DL-RE",
            "ua",
            "https://example.com/",
            "http://proxy.example:8080",
            {"User-Agent": "ua"},
            4,
            lambda *_args, **_kwargs: None,
            lambda: False,
        )

    assert seen["url"] == _Proxy.url
    assert seen["proxy"] is None
    assert seen["local"] is True
    assert item.url != seen["url"]


def test_http_rejects_segments_and_unmet_meta(tmp_path: Path) -> None:
    downloader = _ProbeDownloader()
    save_path = tmp_path / "demo.mp4"
    for url in (
        "https://cdn.example/seg.ts",
        "https://cdn.example/seg.m4s",
        "https://cdn.example/index.m3u8?token=1",
    ):
        assert single_file_http_forbidden(url)
        with pytest.raises(StreamDownloadError, match="单文件"):
            downloader._download_http_file(
                url=url,
                save_path=str(save_path),
                headers={"User-Agent": "ua"},
                check_stop_func=lambda: False,
                max_retries=0,
            )
    assert not save_path.exists()

    response = _BytesResponse(_FTYP, {"content-type": "video/mp4", "content-length": str(len(_FTYP))})
    with (
        patch("app.core.downloaders.base.requests.get", return_value=response),
        patch("app.core.downloaders.base.cfg.get", return_value=0),
        pytest.raises(StreamDownloadError, match="大小"),
    ):
        downloader._download_http_file(
            url="https://cdn.example/video.mp4",
            save_path=str(save_path),
            headers={"User-Agent": "ua"},
            check_stop_func=lambda: False,
            max_retries=0,
            expected_size_bytes=10_000_000,
        )
    assert not save_path.exists()

    with (
        patch("app.core.downloaders.base.requests.get", return_value=response),
        patch("app.core.downloaders.base.cfg.get", return_value=0),
        patch(
            "app.core.downloaders.external.FFmpegExternalTool.probe_duration_seconds",
            return_value=(False, None),
        ),
        pytest.raises(StreamDownloadError, match="时长"),
    ):
        downloader._download_http_file(
            url="https://cdn.example/video.mp4",
            save_path=str(save_path),
            headers={"User-Agent": "ua"},
            check_stop_func=lambda: False,
            max_retries=0,
            expected_duration=120.0,
        )
    assert not save_path.exists()


def test_unreadable_download_is_not_completed_as_mp4(tmp_path: Path) -> None:
    item = VideoItem(url="https://cdn.example/video.mp4", title="demo", source="douyin")
    worker = DownloadWorker(item, str(tmp_path))
    finished: list[str] = []
    errors: list[str] = []
    worker.sig_finished.connect(lambda video_id: finished.append(video_id))
    worker.sig_error.connect(lambda video_id, message: errors.append(message))

    class _Writer:
        def download(self, video_item, save_path, progress_callback, check_stop_func):
            del video_item, progress_callback, check_stop_func
            Path(save_path).write_bytes(_FTYP)
            return save_path

    with (
        patch.object(worker, "_select_downloader", return_value=_Writer()),
        patch.object(worker, "_detect_actual_file_type", return_value=None),
    ):
        worker.run()

    assert finished == []
    assert errors
    assert "无法读取" in errors[0]


class _StreamResponse:
    status_code = 200
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 8192):
        del chunk_size
        yield b"video-bytes"

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> bool:
        return False


def _bili_cfg(section: str, key: str, default=None):
    del section
    if key == "max_retries":
        return 0
    if key == "resume_enabled":
        return False
    if key == "speed_limit_kb":
        return 0
    if key == "chunk_size":
        return 65536
    return default


def test_bilibili_dash_without_audio_does_not_complete(tmp_path: Path) -> None:
    item = VideoItem(url="https://cdn.example/video.m4s", title="clip", source="bilibili")
    item.meta["bilibili_stream"] = "dash"
    events: list[tuple[int, str | None]] = []
    save_path = tmp_path / "clip.mp4"

    def progress(value: int, **kwargs) -> None:
        events.append((value, kwargs.get("phase")))

    with (
        patch("app.core.downloaders.bilibili.FFmpegExternalTool.resolve_executable", return_value="ffmpeg"),
        patch("app.core.downloaders.bilibili.cfg.get", side_effect=_bili_cfg),
        patch("app.core.downloaders.bilibili.requests.get", return_value=_StreamResponse()),
        patch(
            "app.core.downloaders.bilibili.FFmpegExternalTool.probe_contains_audio",
            return_value=(True, False),
        ),
        pytest.raises(StreamDownloadError, match="缺少音频"),
    ):
        BilibiliDownloader().download(item, str(save_path), progress, lambda: False)

    assert not any(value == 100 or phase == "completed" for value, phase in events)
    assert not save_path.exists()


def test_bilibili_muxed_durl_can_complete(tmp_path: Path) -> None:
    item = VideoItem(url="https://cdn.example/video.mp4", title="clip", source="bilibili")
    item.meta["bilibili_stream"] = "durl"
    events: list[tuple[int, str | None]] = []
    save_path = tmp_path / "clip.mp4"

    def progress(value: int, **kwargs) -> None:
        events.append((value, kwargs.get("phase")))

    def fake_merge(_command, **kwargs) -> None:
        assert "-map" in _command and "0" in _command
        Path(kwargs["save_path"]).write_bytes(_FTYP)

    with (
        patch("app.core.downloaders.bilibili.FFmpegExternalTool.resolve_executable", return_value="ffmpeg"),
        patch("app.core.downloaders.bilibili.cfg.get", side_effect=_bili_cfg),
        patch("app.core.downloaders.bilibili.requests.get", return_value=_StreamResponse()),
        patch.object(BilibiliDownloader, "_run_merge_process", side_effect=fake_merge),
    ):
        BilibiliDownloader().download(item, str(save_path), progress, lambda: False)

    assert save_path.read_bytes() == _FTYP
    assert (100, "completed") in events
