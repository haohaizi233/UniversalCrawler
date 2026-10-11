"""管理 yt-dlp、FFmpeg 等外部下载进程的启动、停止和清理。"""

from __future__ import annotations

import os
import subprocess
import urllib.parse
import time

from app.config import DEFAULT_USER_AGENT, cfg
from app.debug_logger import debug_logger
from app.exceptions import DownloaderStoppedError
from app.utils.runtime_paths import resolve_tool_file
from shared.runtime_options import DomainPolicyViolation
from shared.subprocess_env import isolated_media_subprocess_env

from .base import ProgressCallback, StopCheck, durations_consistent, reject_control_characters, validate_header_token

def build_hidden_startupinfo():
    if os.name != "nt" or not hasattr(subprocess, "STARTUPINFO"):
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo

def build_new_console_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

def build_no_window_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)

def _configured_retry_count(default: int = 3) -> int:
    try:
        retry_count = int(cfg.get("download", "max_retries", default))
    except (TypeError, ValueError):
        retry_count = default
    return max(0, min(retry_count, 10))

def _configured_request_timeout(default: int = 60) -> int:
    try:
        timeout = int(cfg.get("download", "request_timeout", default))
    except (TypeError, ValueError):
        timeout = default
    return max(1, min(timeout, 3600))


def _configured_speed_limit_kb(default: int = 0) -> int:
    try:
        speed_limit = int(cfg.get("download", "speed_limit_kb", default))
    except (TypeError, ValueError):
        speed_limit = default
    return max(0, min(speed_limit, 999999))

class ExternalToolRunner:
    @staticmethod
    def resolve_executable(preferred_name: str, fallback_name: str | None = None, version_args: list[str] | None = None) -> str | None:
        """优先查找项目内工具文件，找不到时再回退到系统环境变量。"""
        preferred_path = resolve_tool_file(preferred_name)
        if preferred_path.exists():
            return str(preferred_path)
        if not fallback_name:
            return None
        try:
            subprocess.run(
                [fallback_name, *(version_args or ["-version"])],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            return fallback_name
        except (OSError, subprocess.CalledProcessError):
            return None

    @staticmethod
    def wait_process(
        process: subprocess.Popen,
        check_stop_func: StopCheck,
        progress_callback: ProgressCallback | None = None,
        progress_value: int | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        """轮询等待外部进程结束，并在中途响应停止请求与进度回调。"""
        while process.poll() is None:
            if check_stop_func():
                ExternalToolRunner.terminate_process(process)
                raise DownloaderStoppedError("用户停止下载")
            time.sleep(poll_interval)
            if progress_callback is not None and progress_value is not None:
                try:
                    progress_callback(progress_value)
                except Exception as exc:
                    debug_logger.log_exception(
                        "ExternalToolRunner",
                        "progress_callback",
                        exc,
                        details={"progress_value": progress_value},
                    )

    @staticmethod
    def terminate_process(process: subprocess.Popen, *, timeout: float = 5.0) -> None:
        """先请求外部进程正常退出，超时未响应时再强制结束。"""
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
        if process.poll() is not None:
            return
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

class FFmpegExternalTool:
    EXE_PATH = "ffmpeg.exe"
    CLI_NAME = "ffmpeg"

    @classmethod
    def resolve_executable(cls) -> str | None:
        return ExternalToolRunner.resolve_executable(cls.EXE_PATH, cls.CLI_NAME, ["-version"])

    @classmethod
    def is_available(cls) -> bool:
        return cls.resolve_executable() is not None

    @staticmethod
    def _require_http_input(url: str) -> str:
        """ffmpeg 输入只接受 http/https，拒绝 file:/concat:/subfile:/data: 等协议。"""
        text = str(url or "").strip()
        reject_control_characters("ffmpeg input", text)
        parts = urllib.parse.urlsplit(text)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            raise DomainPolicyViolation("ffmpeg input must be http or https")
        return text

    @classmethod
    def build_download_command(
        cls,
        executable: str,
        url: str,
        save_path: str,
        headers: dict[str, str],
        proxy: str | None = None,
        timeout_seconds: int | None = None,
    ) -> list[str]:
        media_url = cls._require_http_input(url)
        user_agent = reject_control_characters(
            "User-Agent",
            str(headers.get("User-Agent") or DEFAULT_USER_AGENT),
        )
        referer = reject_control_characters("Referer", str(headers.get("Referer") or ""))
        if proxy:
            reject_control_characters("proxy", str(proxy))
        timeout_us = str(max(1, int(timeout_seconds or _configured_request_timeout())) * 1_000_000)
        cmd = [
            executable,
            "-y",
            "-nostats",
            "-progress",
            "pipe:2",
            "-user_agent",
            user_agent,
            "-headers",
            f"Referer: {referer}\r\n",
            "-reconnect",
            "1",
            "-reconnect_at_eof",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-timeout",
            timeout_us,
            "-rw_timeout",
            timeout_us,
        ]
        if proxy:
            cmd.extend(["-http_proxy", proxy])
        cmd.extend([
            "-i",
            media_url,
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            "-bufsize",
            "10M",
            # 下载器实际写入 ``*.mp4.downloading``；显式 muxer 避免临时后缀
            # 让 ffmpeg 把有效视频流误判为未知输出格式。
            "-f",
            "mp4",
            save_path,
        ])
        return cmd

    @classmethod
    def build_merge_command(
        cls,
        executable: str,
        video_path: str,
        audio_path: str | None,
        save_path: str,
        *,
        preserve_embedded_audio: bool = False,
    ) -> list[str]:
        """构造音视频合并命令。单段已含音画时保留全部流，分离视频才只映射视频。"""
        command = [executable, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", video_path]
        if audio_path:
            command.extend(["-i", audio_path])
            command.extend(["-map", "0:v:0", "-map", "1:a:0?"])
        elif preserve_embedded_audio:
            command.extend(["-map", "0"])
        else:
            command.extend(["-map", "0:v:0?"])
        # Bilibili 先写入 ``*.mp4.merging`` 再原子替换最终文件。该临时后缀无法让
        # ffmpeg 自动推断容器，因此必须显式指定 MP4 muxer。
        command.extend(["-c", "copy", "-movflags", "+faststart", "-f", "mp4", save_path])
        return command

    @classmethod
    def probe_duration_seconds(cls, media_path: str) -> tuple[bool, float | None]:
        """返回 (ffprobe 是否可用, 时长秒)。工具缺失时第一项为 False。"""
        executable = ExternalToolRunner.resolve_executable("ffprobe.exe", "ffprobe", ["-version"])
        if not executable:
            return False, None
        try:
            completed = subprocess.run(
                [
                    executable,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    media_path,
                ],
                check=False,
                capture_output=True,
                timeout=30,
                env=isolated_media_subprocess_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return True, None
        if completed.returncode != 0:
            return True, None
        text = completed.stdout.decode("utf-8", errors="replace").strip()
        try:
            duration = float(text)
        except ValueError:
            return True, None
        if duration < 0:
            return True, None
        return True, duration

    @classmethod
    def probe_contains_audio(cls, media_path: str) -> tuple[bool, bool]:
        """返回 (ffprobe 是否可用, 文件里是否有音频流)。"""
        executable = ExternalToolRunner.resolve_executable("ffprobe.exe", "ffprobe", ["-version"])
        if not executable:
            return False, False
        try:
            completed = subprocess.run(
                [
                    executable,
                    "-v",
                    "error",
                    "-select_streams",
                    "a",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "csv=p=0",
                    media_path,
                ],
                check=False,
                capture_output=True,
                timeout=30,
                env=isolated_media_subprocess_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return True, False
        if completed.returncode != 0:
            return True, False
        text = completed.stdout.decode("utf-8", errors="replace").lower()
        return True, "audio" in text

    @classmethod
    def assert_duration_if_expected(
        cls,
        media_path: str,
        expected_duration: float | None,
        *,
        observed_duration: float | None = None,
    ) -> None:
        """有预期时长时必须由 ffprobe 核对。工具不可用或读不到时长都不能当成功。"""
        del observed_duration
        if not isinstance(expected_duration, (int, float)) or isinstance(expected_duration, bool):
            return
        if expected_duration <= 0:
            return
        available, probed = cls.probe_duration_seconds(media_path)
        if not available or probed is None:
            raise DomainPolicyViolation("ffprobe 无法核对媒体时长")
        if not durations_consistent(float(probed), float(expected_duration)):
            raise DomainPolicyViolation("媒体时长与预期不一致")

class NM3U8DLREExternalTool:
    EXE_PATH = "N_m3u8DL-RE.exe"
    DISPLAY_NAME = "N_m3u8DL-RE"
    WINDOWS_CANDIDATES = ("N_m3u8DL-RE.exe", "N_m3u8DL-RE", "n-m3u8dl-re")
    POSIX_CANDIDATES = ("N_m3u8DL-RE", "n-m3u8dl-re")

    @classmethod
    def executable_candidates(cls) -> tuple[str, ...]:
        """返回当前平台优先级排序后的可执行文件候选名。"""
        if os.name == "nt":
            return cls.WINDOWS_CANDIDATES
        return cls.POSIX_CANDIDATES

    @classmethod
    def cli_candidates(cls) -> tuple[str, ...]:
        """返回适合直接走系统 PATH 探测的命令名。"""
        if os.name == "nt":
            return ("N_m3u8DL-RE.exe", "N_m3u8DL-RE")
        return ("N_m3u8DL-RE", "n-m3u8dl-re")

    @classmethod
    def resolve_executable(cls) -> str | None:
        for executable_name in cls.executable_candidates():
            path = resolve_tool_file(executable_name)
            if path.exists():
                return str(path)

        for executable_name in cls.cli_candidates():
            try:
                subprocess.run(
                    [executable_name, "--version"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                )
                return executable_name
            except (OSError, subprocess.CalledProcessError):
                continue
        return None

    @classmethod
    def is_available(cls) -> bool:
        return cls.resolve_executable() is not None

    @classmethod
    def is_m3u8_url(cls, url: str, content_type: str | None = None) -> bool:
        """路径以 .m3u8 结尾，或 Content-Type 是 mpegurl，才视为 HLS。

        URL 里仅仅出现字符串 m3u8 不算。没有该后缀但类型是 mpegurl 的流
        也不能当成普通文件保存成功。
        """
        lowered_type = str(content_type or "").lower()
        if "mpegurl" in lowered_type or "m3u8" in lowered_type:
            return True
        try:
            path = urllib.parse.urlsplit(str(url or "")).path
        except ValueError:
            return False
        return path.lower().endswith(".m3u8")

    @classmethod
    def _m3u8_thread_count(cls) -> str:
        max_concurrent = max(1, int(cfg.get("download", "max_concurrent", 3)))
        return str(max(1, 16 // max_concurrent))

    @classmethod
    def build_download_command(
        cls,
        executable: str,
        source_url: str,
        save_path: str,
        user_agent: str,
        referer: str,
        proxy: str | None = None,
        extra_headers: dict[str, str] | None = None,
        thread_count: str | int | None = None,
        tmp_dir: str | None = None,
    ) -> list[str]:
        save_dir = os.path.dirname(save_path)
        save_name_no_ext = os.path.splitext(os.path.basename(save_path))[0]
        headers = cls._normalized_headers(user_agent, referer, extra_headers)
        if proxy:
            reject_control_characters("proxy", str(proxy))
        thread_count_text = str(thread_count or cls._m3u8_thread_count())
        retry_count_text = str(_configured_retry_count())
        request_timeout_text = str(_configured_request_timeout())
        cmd = [
            executable,
            source_url,
            "--save-dir",
            save_dir,
            "--save-name",
            save_name_no_ext,
        ]
        if tmp_dir:
            cmd.extend(["--tmp-dir", str(tmp_dir)])
        cmd.extend([
            "--thread-count",
            thread_count_text,
            "--download-retry-count",
            retry_count_text,
            "--http-request-timeout",
            request_timeout_text,
            "--auto-select",
            "true",
            "--mux-after-done",
            "format=mp4",
        ])
        speed_limit_kb = _configured_speed_limit_kb()
        if speed_limit_kb > 0:
            # N_m3u8DL-RE 原生支持 K/M 后缀；沿用 KB/s 配置值可避免 Python 侧下载完分片后才延迟。
            cmd.extend(["--max-speed", f"{speed_limit_kb}K"])
        for key, value in headers.items():
            cmd.extend(["--header", f"{key}: {value}"])
        if proxy:
            cmd.extend(["--custom-proxy", proxy])
        return cmd

    @staticmethod
    def _normalized_headers(
        user_agent: str,
        referer: str,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        blocked_headers = {
            "host",
            "content-length",
            "connection",
            "transfer-encoding",
        }
        for key, value in (extra_headers or {}).items():
            key_text = str(key or "").strip()
            value_text = str(value or "").strip()
            if not key_text or not value_text:
                continue
            lowered = key_text.lower()
            if lowered.startswith(":") or lowered in blocked_headers:
                continue
            canonical, safe_value = validate_header_token(key_text, value_text)
            canonical = "-".join(part[:1].upper() + part[1:].lower() for part in canonical.lower().split("-"))
            headers[canonical] = safe_value
        headers["User-Agent"] = validate_header_token(
            "User-Agent",
            headers.get("User-Agent") or str(user_agent or DEFAULT_USER_AGENT),
        )[1]
        if referer:
            headers["Referer"] = validate_header_token(
                "Referer",
                headers.get("Referer") or str(referer),
            )[1]
        return headers
