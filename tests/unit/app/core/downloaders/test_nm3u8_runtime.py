"""外部 HLS 工具的依赖定位与失败诊断。"""

import io
from unittest.mock import Mock

import pytest

from app.core.downloaders.external import FFmpegExternalTool, NM3U8DLREExternalTool
from app.core.downloaders.m3u8 import N_m3u8DL_RE_Downloader
from app.core.downloaders.nm3u8_progress import _Nm3u8OutputProgress
from app.exceptions import ExternalToolError
from app.models import VideoItem


def test_nm3u8_uses_resolved_ffmpeg_when_tools_are_in_separate_directories(tmp_path, monkeypatch):
    ffmpeg = tmp_path / "安装目录" / "_internal" / "ffmpeg.exe"
    ffmpeg.parent.mkdir(parents=True)
    ffmpeg.touch()
    monkeypatch.setattr(FFmpegExternalTool, "resolve_executable", lambda: str(ffmpeg))

    command = NM3U8DLREExternalTool.build_download_command(
        str(tmp_path / "tools" / "N_m3u8DL-RE.exe"),
        "https://cdn.example/index.m3u8", str(tmp_path / "output.mp4"), "ua", "https://example.com/",
    )

    assert "--ffmpeg-binary-path" in command
    assert command[command.index("--ffmpeg-binary-path") + 1] == str(ffmpeg)


def test_nm3u8_preserves_bounded_output_for_failure_diagnosis():
    progress = _Nm3u8OutputProgress()
    for index in range(100):
        progress.feed(f"line-{index} " + "x" * 5000)
    progress.feed("ERROR: ffmpeg not found")

    tail = progress.output_tail()

    assert "ffmpeg not found" in tail
    assert "line-0 " not in tail
    assert len(tail) <= 16_400


def test_startup_sweep_does_not_report_missing_temp_root_as_reparse_point(tmp_path, monkeypatch):
    log = Mock()
    monkeypatch.setattr("app.core.downloaders.m3u8.debug_logger.log", log)

    cleaned = N_m3u8DL_RE_Downloader.sweep_orphaned_workspaces([tmp_path])

    assert cleaned == 0
    summary = next(
        call.kwargs for call in log.call_args_list
        if call.kwargs.get("status_code") == "M3U8_TMP_SWEEP"
    )
    assert summary["details"]["errors"] == []
    assert not (tmp_path / N_m3u8DL_RE_Downloader.NM3U8_TEMP_ROOT_NAME).exists()


def test_nm3u8_nonzero_exit_records_dependency_error_before_cleanup(tmp_path, monkeypatch):
    downloader = N_m3u8DL_RE_Downloader()
    item = VideoItem(url="https://cdn.example/index.m3u8", title="demo", source="douyin")
    process = Mock()
    process.returncode = 1
    process.poll.return_value = 1
    process.stdout = io.StringIO("ERROR: ffmpeg not found\n")
    monkeypatch.setattr(downloader, "_popen_nm3u8_process", lambda *_args: process)
    monkeypatch.setattr(downloader, "_wait_external_process_with_file_progress", lambda *_args, **_kwargs: None)
    log = Mock()
    monkeypatch.setattr("app.core.downloaders.m3u8.debug_logger.log", log)

    with pytest.raises(ExternalToolError):
        downloader._run_nm3u8_external_command(
            item, str(tmp_path / "demo.mp4"), "N_m3u8DL-RE.exe", item.url,
            "ua", "https://example.com/", None, {}, 1,
            lambda *_args, **_kwargs: None, lambda: False, local_proxy=False,
        )

    failures = [call.kwargs for call in log.call_args_list if call.kwargs.get("status_code") == "M3U8_EXTERNAL_FAILED"]
    assert len(failures) == 1
    assert "ffmpeg not found" in failures[0]["details"]["output_tail"]
    assert failures[0]["details"]["returncode"] == 1
    assert not (tmp_path / downloader.NM3U8_TEMP_ROOT_NAME).exists()
