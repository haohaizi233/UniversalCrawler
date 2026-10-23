"""解析 N_m3u8DL-RE 控制台进度输出。"""

from __future__ import annotations

import re
import threading
import time
from collections import deque

from app.debug_logger import debug_logger


class _Nm3u8OutputProgress:
    """把 N_m3u8DL-RE 控制台输出转换为前端可消费的进度遥测。"""

    _PERCENT_RE = re.compile(r"(?P<percent>\d+(?:\.\d+)?)%")
    _SPEED_RE = re.compile(
        r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>B|KB|KiB|MB|MiB|GB|GiB|TB|TiB)(?:ps|/s)",
        re.IGNORECASE,
    )
    _UNIT_FACTORS = {
        "B": 1,
        "KB": 1024,
        "KIB": 1024,
        "MB": 1024**2,
        "MIB": 1024**2,
        "GB": 1024**3,
        "GIB": 1024**3,
        "TB": 1024**4,
        "TIB": 1024**4,
    }

    def __init__(self, *, default_progress: int = 50) -> None:
        self._lock = threading.Lock()
        self._progress = max(0, min(100, int(default_progress)))
        self._speed_bps = 0
        self._synthetic_bytes = 0.0
        self._last_at = time.monotonic()
        self._output_lines: deque[str] = deque(maxlen=8)

    def feed(self, text: str) -> None:
        if not text:
            return
        now = time.monotonic()
        with self._lock:
            self._output_lines.append(text[:2048])
            self._advance_locked(now)
            percent_match = None
            for candidate in self._PERCENT_RE.finditer(text):
                percent_match = candidate
            if percent_match is not None:
                try:
                    percent = float(percent_match.group("percent"))
                except (TypeError, ValueError):
                    percent = -1.0
                if percent >= 0:
                    self._progress = max(0, min(95, int(percent)))

            speed_match = None
            for candidate in self._SPEED_RE.finditer(text):
                speed_match = candidate
            if speed_match is not None:
                self._speed_bps = self._parse_speed_match(speed_match)

    def snapshot(self) -> tuple[int, int]:
        now = time.monotonic()
        with self._lock:
            self._advance_locked(now)
            return self._progress, int(self._synthetic_bytes)

    def output_tail(self) -> str:
        """只保留有界尾部，用于进程失败后的诊断日志。"""
        with self._lock:
            return "\n".join(self._output_lines)

    def log_process_failure(self, executable: str, returncode: int, trace_id: str | None) -> None:
        debug_logger.log(
            component="N_m3u8DL_RE_Downloader",
            action="external_process_failed",
            level="ERROR",
            message="N_m3u8DL-RE 执行失败，保留工具输出以定位缺失依赖",
            status_code="M3U8_EXTERNAL_FAILED",
            details={
                "executable": executable,
                "returncode": returncode,
                "output_tail": self.output_tail(),
            },
            trace_id=trace_id,
        )

    def _advance_locked(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_at)
        if self._speed_bps > 0 and elapsed > 0:
            self._synthetic_bytes += self._speed_bps * elapsed
        self._last_at = now

    @classmethod
    def _parse_speed_match(cls, match: re.Match[str]) -> int:
        try:
            value = float(match.group("value"))
        except (TypeError, ValueError):
            return 0
        unit = str(match.group("unit") or "B").upper()
        return max(0, int(value * cls._UNIT_FACTORS.get(unit, 1)))
