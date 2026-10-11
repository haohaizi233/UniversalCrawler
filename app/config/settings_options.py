"""设置项枚举、标签、下载目录与平台超时规范化。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from app.exceptions import ConfigValidationError

CURRENT_FILENAME_TEMPLATE = "current"
DEFAULT_OPEN_MODE = "builtin_player"

FILENAME_TEMPLATE_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": CURRENT_FILENAME_TEMPLATE, "label": "\u9ed8\u8ba4"},
    {"value": "{title}", "label": "\u6807\u9898"},
    {"value": "{platform}_{title}", "label": "\u5e73\u53f0_\u6807\u9898"},
    {"value": "{platform}_{title}_{date}", "label": "\u5e73\u53f0_\u6807\u9898_\u65e5\u671f"},
    {"value": "{platform}_{title}_{index}", "label": "\u5e73\u53f0_\u6807\u9898_\u5e8f\u53f7"},
)
OPEN_MODE_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": DEFAULT_OPEN_MODE, "label": "\u5185\u7f6e\u64ad\u653e\u5668"},
    {"value": "system_default", "label": "\u7cfb\u7edf\u9ed8\u8ba4\u6253\u5f00\u65b9\u5f0f"},
)
PLAYBACK_PLAYER_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": DEFAULT_OPEN_MODE, "label": "\u5185\u7f6e\u64ad\u653e\u5668"},
    {"value": "system_default", "label": "\u7cfb\u7edf\u9ed8\u8ba4\u64ad\u653e\u5668"},
)
IMAGE_AUTO_ADVANCE_INTERVAL_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "1", "label": "1 \u79d2"},
    {"value": "3", "label": "3 \u79d2"},
    {"value": "5", "label": "5 \u79d2\uff08\u63a8\u8350\uff09"},
    {"value": "10", "label": "10 \u79d2"},
)
LOG_LEVEL_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "debug", "label": "\u8c03\u8bd5"},
    {"value": "info", "label": "\u4fe1\u606f"},
    {"value": "warning", "label": "\u8b66\u544a"},
    {"value": "error", "label": "\u9519\u8bef"},
)
ACCENT_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "blue", "label": "\u84dd\u8272"},
    {"value": "green", "label": "\u7eff\u8272"},
    {"value": "purple", "label": "\u7d2b\u8272"},
    {"value": "orange", "label": "\u6a59\u8272"},
    {"value": "red", "label": "\u7ea2\u8272"},
)
SCALE_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "90%", "label": "90%"},
    {"value": "100%", "label": "100%\uff08\u63a8\u8350\uff09"},
    {"value": "110%", "label": "110%"},
    {"value": "125%", "label": "125%"},
)
FONT_SIZE_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "small", "label": "\u5c0f"},
    {"value": "medium", "label": "\u4e2d\uff08\u63a8\u8350\uff09"},
    {"value": "large", "label": "\u5927"},
)
LANGUAGE_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "zh-CN", "label": "\u7b80\u4f53\u4e2d\u6587\uff08\u63a8\u8350\uff09"},
    {"value": "en-US", "label": "English"},
    {"value": "zh-TW", "label": "\u7e41\u9ad4\u4e2d\u6587"},
)
DOWNLOAD_CONCURRENCY_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "1", "label": "1"},
    {"value": "3", "label": "3\uff08\u63a8\u8350\uff09"},
    {"value": "5", "label": "5"},
)
REQUEST_TIMEOUT_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "30", "label": "30 \u79d2"},
    {"value": "60", "label": "60 \u79d2\uff08\u63a8\u8350\uff09"},
    {"value": "90", "label": "90 \u79d2"},
    {"value": "120", "label": "120 \u79d2"},
    {"value": "180", "label": "180 \u79d2"},
    {"value": "300", "label": "300 \u79d2"},
)
RETRY_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "0", "label": "0\uff08\u4e0d\u91cd\u8bd5\uff09"},
    {"value": "1", "label": "1"},
    {"value": "2", "label": "2"},
    {"value": "3", "label": "3\uff08\u63a8\u8350\uff09"},
    {"value": "5", "label": "5"},
    {"value": "10", "label": "10"},
)
SPEED_LIMIT_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "0", "label": "\u65e0\u9650\u5236"},
    {"value": "512", "label": "512 KB/s"},
    {"value": "1024", "label": "1 MB/s"},
    {"value": "2048", "label": "2 MB/s"},
    {"value": "5120", "label": "5 MB/s"},
    {"value": "10240", "label": "10 MB/s"},
    {"value": "20480", "label": "20 MB/s"},
)
PLATFORM_COUNT_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "10", "label": "10 \u4e2a\u89c6\u9891"},
    {"value": "20", "label": "20 \u4e2a\u89c6\u9891\uff08\u63a8\u8350\uff09"},
    {"value": "30", "label": "30 \u4e2a\u89c6\u9891"},
    {"value": "50", "label": "50 \u4e2a\u89c6\u9891"},
    {"value": "9999", "label": "max"},
)
PLATFORM_NOTE_COUNT_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "10", "label": "10 \u7bc7\u7b14\u8bb0"},
    {"value": "20", "label": "20 \u7bc7\u7b14\u8bb0\uff08\u63a8\u8350\uff09"},
    {"value": "30", "label": "30 \u7bc7\u7b14\u8bb0"},
    {"value": "50", "label": "50 \u7bc7\u7b14\u8bb0"},
    {"value": "9999", "label": "max"},
)
PLATFORM_PAGE_COUNT_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "1", "label": "1 \u9875\uff08\u63a8\u8350\uff09"},
    {"value": "2", "label": "2 \u9875"},
    {"value": "3", "label": "3 \u9875"},
    {"value": "5", "label": "5 \u9875"},
    {"value": "9999", "label": "max"},
)
LOG_RETENTION_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "1", "label": "1 \u5929\uff08\u63a8\u8350\uff09"},
    {"value": "3", "label": "3 \u5929"},
    {"value": "5", "label": "5 \u5929"},
    {"value": "7", "label": "7 \u5929"},
)
FAILED_RECORD_RETENTION_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "3", "label": "3 \u5929"},
    {"value": "7", "label": "7 \u5929\uff08\u63a8\u8350\uff09"},
    {"value": "14", "label": "14 \u5929"},
    {"value": "30", "label": "30 \u5929"},
)
UI_LOG_MAX_DISPLAY_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "100", "label": "100 \u6761"},
    {"value": "300", "label": "300 \u6761\uff08\u63a8\u8350\uff09"},
    {"value": "500", "label": "500 \u6761"},
)
UI_LOG_MAX_DISPLAY_DEFAULT = 300
PROXY_APP_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "\u7cfb\u7edf\u4ee3\u7406", "label": "\u7cfb\u7edf\u4ee3\u7406"},
    {"value": "\u76f4\u8fde", "label": "\u76f4\u8fde\uff08\u4e0d\u4f7f\u7528\u4ee3\u7406\uff09"},
    {"value": "Clash (7890)", "label": "Clash (7890)"},
    {"value": "Clash Verge (7897)", "label": "Clash Verge (7897)"},
    {"value": "v2rayN (10809)", "label": "v2rayN (10809)"},
    {"value": "V2Ray / Qv2ray (10808)", "label": "V2Ray / Qv2ray (10808)"},
    {"value": "sing-box (2080)", "label": "sing-box (2080)"},
    {"value": "NekoRay (2080)", "label": "NekoRay (2080)"},
    {"value": "\u81ea\u5b9a\u4e49", "label": "\u81ea\u5b9a\u4e49 HTTP/SOCKS5 \u7aef\u70b9"},
)
_INVALID_PATH_CHARS_RE = re.compile(r'[<>:"|?*\x00-\x1f]')
_QUOTE_CHARS = "\"'`\u201c\u201d\u2018\u2019"

# 选项元数据由桌面端和 WebUI 共用，避免两个前端各自维护不同枚举。
def filename_template_options() -> list[dict[str, str]]:
    return [dict(option) for option in FILENAME_TEMPLATE_OPTIONS]


def open_mode_options() -> list[dict[str, str]]:
    return [dict(option) for option in OPEN_MODE_OPTIONS]

def playback_player_options() -> list[dict[str, str]]:
    return [dict(option) for option in PLAYBACK_PLAYER_OPTIONS]


def image_auto_advance_interval_options() -> list[dict[str, str]]:
    return [dict(option) for option in IMAGE_AUTO_ADVANCE_INTERVAL_OPTIONS]


def log_level_options() -> list[dict[str, str]]:
    return [dict(option) for option in LOG_LEVEL_OPTIONS]


def accent_options() -> list[dict[str, str]]:
    return [dict(option) for option in ACCENT_OPTIONS]


def scale_options() -> list[dict[str, str]]:
    return [dict(option) for option in SCALE_OPTIONS]


def font_size_options() -> list[dict[str, str]]:
    return [dict(option) for option in FONT_SIZE_OPTIONS]


def language_options() -> list[dict[str, str]]:
    return [dict(option) for option in LANGUAGE_OPTIONS]


def download_concurrency_options() -> list[dict[str, str]]:
    return [dict(option) for option in DOWNLOAD_CONCURRENCY_OPTIONS]


def normalize_download_concurrency(value: int | str | None, default: int = 3) -> int:
    """把任意输入收敛为下载调度器实际支持的并发档位。"""
    try:
        numeric = int(value if value is not None else default)
    except (TypeError, ValueError):
        numeric = int(default)
    if numeric <= 1:
        return 1
    if numeric <= 3:
        return 3
    return 5


def request_timeout_options() -> list[dict[str, str]]:
    return [dict(option) for option in REQUEST_TIMEOUT_OPTIONS]


def retry_options() -> list[dict[str, str]]:
    return [dict(option) for option in RETRY_OPTIONS]


def speed_limit_options() -> list[dict[str, str]]:
    return [dict(option) for option in SPEED_LIMIT_OPTIONS]


def platform_count_options() -> list[dict[str, str]]:
    return [dict(option) for option in PLATFORM_COUNT_OPTIONS]


def platform_note_count_options() -> list[dict[str, str]]:
    return [dict(option) for option in PLATFORM_NOTE_COUNT_OPTIONS]


def platform_page_count_options() -> list[dict[str, str]]:
    return [dict(option) for option in PLATFORM_PAGE_COUNT_OPTIONS]


def log_retention_options() -> list[dict[str, str]]:
    return [dict(option) for option in LOG_RETENTION_OPTIONS]


def failed_record_retention_options() -> list[dict[str, str]]:
    return [dict(option) for option in FAILED_RECORD_RETENTION_OPTIONS]


def ui_log_max_display_options() -> list[dict[str, str]]:
    return [dict(option) for option in UI_LOG_MAX_DISPLAY_OPTIONS]


def normalize_ui_log_max_display_count(value: Any, default: int = UI_LOG_MAX_DISPLAY_DEFAULT) -> int:
    """限制日志展示量，避免前端一次渲染过多行而阻塞交互。"""
    try:
        numeric = int(value if value is not None else default)
    except (TypeError, ValueError):
        numeric = int(default)
    allowed = {int(option["value"]) for option in UI_LOG_MAX_DISPLAY_OPTIONS}
    return numeric if numeric in allowed else int(default)


def proxy_app_options() -> list[dict[str, str]]:
    return [dict(option) for option in PROXY_APP_OPTIONS]


def _option_values(options: tuple[dict[str, str], ...]) -> set[str]:
    return {str(option["value"]) for option in options}


def _option_label(options: tuple[dict[str, str], ...], value: Any, fallback: str = "") -> str:
    value_text = str(value or fallback)
    for option in options:
        if option["value"] == value_text:
            return option["label"]
    return value_text


def filename_template_label(value: Any) -> str:
    return _option_label(FILENAME_TEMPLATE_OPTIONS, value, CURRENT_FILENAME_TEMPLATE)


def open_mode_label(value: Any) -> str:
    value_text = str(value or DEFAULT_OPEN_MODE)
    if value_text not in _option_values(OPEN_MODE_OPTIONS):
        value_text = DEFAULT_OPEN_MODE
    return _option_label(OPEN_MODE_OPTIONS, value_text, DEFAULT_OPEN_MODE)


def playback_player_label(value: Any) -> str:
    return _option_label(PLAYBACK_PLAYER_OPTIONS, value, DEFAULT_OPEN_MODE)


def log_level_label(value: Any) -> str:
    return _option_label(LOG_LEVEL_OPTIONS, value, "info")


def accent_label(value: Any) -> str:
    return _option_label(ACCENT_OPTIONS, value, "blue")


def font_size_label(value: Any) -> str:
    return _option_label(FONT_SIZE_OPTIONS, value, "medium")


def language_label(value: Any) -> str:
    return _option_label(LANGUAGE_OPTIONS, value, "zh-CN")


def _strip_path_quotes(value: Any) -> str:
    text = str(value or "").strip()
    while len(text) >= 2 and text[0] in _QUOTE_CHARS and text[-1] in _QUOTE_CHARS:
        text = text[1:-1].strip()
    return text


def _file_url_to_path(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme.lower() != "file":
        return value
    path_text = unquote(parsed.path or "")
    if parsed.netloc:
        path_text = f"//{parsed.netloc}{path_text}"
    if os.name == "nt" and path_text.startswith("/") and len(path_text) > 3 and path_text[2] == ":":
        path_text = path_text[1:]
    return path_text


def _path_has_invalid_segment(path: Path) -> bool:
    """只检查普通路径段，跳过盘符、根目录等系统组成部分。"""
    for index, part in enumerate(path.parts):
        if not part or part in {os.sep, os.altsep, path.anchor, path.drive}:
            continue
        if index == 0 and part.endswith(":"):
            continue
        if _INVALID_PATH_CHARS_RE.search(part):
            return True
    return False


def normalize_download_directory_input(value: Any, *, create: bool = False) -> str:
    """把粘贴路径、file URL 或误传的文件路径统一解析为下载目录。"""
    text = _strip_path_quotes(value)
    if not text:
        raise ConfigValidationError("\u4e0b\u8f7d\u76ee\u5f55\u4e0d\u80fd\u4e3a\u7a7a")
    text = _file_url_to_path(text)
    text = os.path.expandvars(os.path.expanduser(text))
    candidate = Path(text)
    raw_text = text.rstrip()
    trailing_separator = raw_text.endswith(("/", "\\"))
    if not trailing_separator and candidate.suffix and not (candidate.exists() and candidate.is_dir()):
        candidate = candidate.parent
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        normalized = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ConfigValidationError(f"\u4e0b\u8f7d\u76ee\u5f55\u8def\u5f84\u65e0\u6cd5\u89e3\u6790: {value}") from exc
    if _path_has_invalid_segment(normalized):
        raise ConfigValidationError(f"\u4e0b\u8f7d\u76ee\u5f55\u8def\u5f84\u5305\u542b\u975e\u6cd5\u5b57\u7b26: {value}")
    if normalized.exists() and not normalized.is_dir():
        raise ConfigValidationError(f"\u4e0b\u8f7d\u76ee\u5f55\u4e0d\u662f\u6587\u4ef6\u5939: {normalized}")
    if create:
        try:
            normalized.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigValidationError(f"\u65e0\u6cd5\u521b\u5efa\u4e0b\u8f7d\u76ee\u5f55: {normalized}") from exc
    return str(normalized)


def normalize_platform_timeout(value: Any, *, default: int = 60) -> int:
    """规范平台请求超时，并迁移早期过短的 10 秒默认值。"""
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = default
    if timeout <= 10:
        timeout = default
    return max(30, min(timeout, 300))
