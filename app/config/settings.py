"""统一管理 GUI、WebUI 与命令行共用的持久化配置。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time as time_module
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config.constants import (
    DEFAULT_CONFIG_FILE,
    DEFAULT_DOWNLOAD_DIR,
    DEFAULT_MISSAV_PROXY_URL,
    DEFAULT_USER_AGENT,
    SUPPORTED_THEMES,
)
from app.config.settings_file_lock import (
    acquire_file_lock_descriptor,
    release_file_lock_descriptor,
)
from app.core.event_bus import EventBus
from app.exceptions import ConfigReadError, ConfigValidationError, ConfigWriteError
from app.utils.runtime_paths import is_temporary_path, resolve_user_file
from shared.filesystem_directory_capability import (
    FilesystemDirectoryCapability,
    filesystem_directory_capability,
)

from app.config.settings_options import (
    CURRENT_FILENAME_TEMPLATE,
    DEFAULT_OPEN_MODE,
    FAILED_RECORD_RETENTION_OPTIONS,
    FILENAME_TEMPLATE_OPTIONS,
    FONT_SIZE_OPTIONS,
    IMAGE_AUTO_ADVANCE_INTERVAL_OPTIONS,
    LANGUAGE_OPTIONS,
    LOG_LEVEL_OPTIONS,
    LOG_RETENTION_OPTIONS,
    OPEN_MODE_OPTIONS,
    PLAYBACK_PLAYER_OPTIONS,
    SCALE_OPTIONS,
    _option_values,
    normalize_download_directory_input,
    normalize_platform_timeout,
    ACCENT_OPTIONS as ACCENT_OPTIONS,
    DOWNLOAD_CONCURRENCY_OPTIONS as DOWNLOAD_CONCURRENCY_OPTIONS,
    PLATFORM_COUNT_OPTIONS as PLATFORM_COUNT_OPTIONS,
    PLATFORM_NOTE_COUNT_OPTIONS as PLATFORM_NOTE_COUNT_OPTIONS,
    PLATFORM_PAGE_COUNT_OPTIONS as PLATFORM_PAGE_COUNT_OPTIONS,
    PROXY_APP_OPTIONS as PROXY_APP_OPTIONS,
    REQUEST_TIMEOUT_OPTIONS as REQUEST_TIMEOUT_OPTIONS,
    RETRY_OPTIONS as RETRY_OPTIONS,
    SPEED_LIMIT_OPTIONS as SPEED_LIMIT_OPTIONS,
    UI_LOG_MAX_DISPLAY_DEFAULT as UI_LOG_MAX_DISPLAY_DEFAULT,
    UI_LOG_MAX_DISPLAY_OPTIONS as UI_LOG_MAX_DISPLAY_OPTIONS,
    accent_label as accent_label,
    accent_options as accent_options,
    download_concurrency_options as download_concurrency_options,
    failed_record_retention_options as failed_record_retention_options,
    filename_template_label as filename_template_label,
    filename_template_options as filename_template_options,
    font_size_label as font_size_label,
    font_size_options as font_size_options,
    image_auto_advance_interval_options as image_auto_advance_interval_options,
    language_label as language_label,
    language_options as language_options,
    log_level_label as log_level_label,
    log_level_options as log_level_options,
    log_retention_options as log_retention_options,
    normalize_download_concurrency as normalize_download_concurrency,
    normalize_ui_log_max_display_count as normalize_ui_log_max_display_count,
    open_mode_label as open_mode_label,
    open_mode_options as open_mode_options,
    platform_count_options as platform_count_options,
    platform_note_count_options as platform_note_count_options,
    platform_page_count_options as platform_page_count_options,
    playback_player_label as playback_player_label,
    playback_player_options as playback_player_options,
    proxy_app_options as proxy_app_options,
    request_timeout_options as request_timeout_options,
    retry_options as retry_options,
    scale_options as scale_options,
    speed_limit_options as speed_limit_options,
    ui_log_max_display_options as ui_log_max_display_options,
)

LOCK_WARN_SECONDS = 1.0
CONFIG_FILE_LOCK_TIMEOUT_SECONDS = 3.0
CONFIG_FILE_LOCK_STALE_SECONDS = 30.0
CONFIG_EXTERNAL_SYNC_INTERVAL_SECONDS = 0.5


@dataclass
class CommonSettings:
    """保存下载目录、命名、打开行为和主题等跨平台通用设置。"""
    save_directory: str = DEFAULT_DOWNLOAD_DIR
    last_source: str = "kuaishou"
    filename_template: str = CURRENT_FILENAME_TEMPLATE
    open_after_download: bool = False
    default_open_mode: str = DEFAULT_OPEN_MODE
    show_browser_window: bool = True
    theme: str = "light"
    dark_theme: bool = False
    theme_schema_version: int = 2

    def normalize(self) -> None:

        try:
            self.save_directory = normalize_download_directory_input(self.save_directory)
        except ConfigValidationError:
            self.save_directory = normalize_download_directory_input(DEFAULT_DOWNLOAD_DIR)
        if not str(self.filename_template or "").strip():
            self.filename_template = CURRENT_FILENAME_TEMPLATE
        if self.filename_template not in _option_values(FILENAME_TEMPLATE_OPTIONS):
            self.filename_template = CURRENT_FILENAME_TEMPLATE
        if not str(self.default_open_mode or "").strip():
            self.default_open_mode = DEFAULT_OPEN_MODE
        valid_open_modes = {option["value"] for option in OPEN_MODE_OPTIONS}
        if self.default_open_mode not in valid_open_modes:
            self.default_open_mode = DEFAULT_OPEN_MODE
        self.show_browser_window = bool(self.show_browser_window)
        if self.theme not in SUPPORTED_THEMES:
            self.theme = "dark" if self.dark_theme else "light"
        self.dark_theme = self.theme != "light" if isinstance(self.dark_theme, bool) else False

@dataclass
class MissAVSettings:
    """保存 MissAV 的代理入口、抓取数量和筛选偏好。"""
    proxy_type: str = "clash"  # 支持 Clash、V2Ray 和自定义代理。
    proxy_app: str = "Clash (7890)"
    proxy_port: int = 7890
    proxy_url: str = DEFAULT_MISSAV_PROXY_URL
    max_items: int = 20
    search_max_pages: int = 1
    timeout: int = 60
    priority: str = "中文字幕优先"
    individual_only: bool = False

    def normalize(self) -> None:

        if self.priority not in {"中文字幕优先", "无码流出优先"}:
            self.priority = "中文字幕优先"
        self.max_items = max(1, min(int(self.max_items or 20), 9999))
        self.search_max_pages = max(1, min(int(self.search_max_pages or 1), 100))
        self.timeout = normalize_platform_timeout(self.timeout, default=60)

@dataclass
class BilibiliSettings:
    """保存 Bilibili 鉴权文件、抓取上限和 API 并发参数。"""
    auth_file: str = "bili_auth.json"
    user_agent: str = DEFAULT_USER_AGENT
    max_pages: int = 1
    max_items: int = 9999
    timeout: int = 60
    api_workers: int = 8

    def normalize(self) -> None:

        self.auth_file = str(resolve_user_file(self.auth_file))
        self.max_pages = max(1, min(self.max_pages, 9999))
        self.max_items = max(1, min(self.max_items, 9999))
        self.timeout = normalize_platform_timeout(self.timeout, default=60)
        self.api_workers = max(1, min(self.api_workers, 16))

@dataclass
class DouyinSettings:
    """保存抖音搜索范围和请求超时。"""
    user_agent: str = DEFAULT_USER_AGENT
    search_max_pages: int = 1
    max_items: int = 20
    timeout: int = 60

    def normalize(self) -> None:

        self.search_max_pages = max(1, min(self.search_max_pages, 100))
        self.max_items = max(1, min(self.max_items, 9999))
        self.timeout = normalize_platform_timeout(self.timeout, default=60)

@dataclass
class XiaohongshuSettings:
    """保存小红书鉴权、分页、详情并发和筛选参数。"""

    user_agent: str = DEFAULT_USER_AGENT
    max_items: int = 20
    search_max_pages: int = 5
    timeout: int = 30
    request_interval: float = 0.15
    detail_request_interval: float = 0.0
    sort: str = "general"
    note_type: int = 0  # 0 表示全部，1 表示图文，2 表示视频。

    def normalize(self) -> None:

        self.max_items = max(1, min(self.max_items, 9999))
        self.search_max_pages = max(1, min(self.search_max_pages, 100))
        self.timeout = normalize_platform_timeout(self.timeout, default=30)
        self.request_interval = max(0.0, min(float(self.request_interval), 10.0))
        if self.request_interval == 1.5:
            self.request_interval = 0.15
        self.detail_request_interval = max(0.0, min(float(self.detail_request_interval), 5.0))
        if self.detail_request_interval == 0.5:
            self.detail_request_interval = 0.0
        if self.sort not in {"general", "popularity_descending", "time_descending"}:
            self.sort = "general"
        try:
            note_type = int(self.note_type)
        except (TypeError, ValueError):
            note_type = 0
        self.note_type = note_type if note_type in {0, 1, 2} else 0

@dataclass
class KuaishouSettings:
    """保存快手抓取上限和请求超时。"""
    user_agent: str = DEFAULT_USER_AGENT
    max_items: int = 20
    timeout: int = 60

    def normalize(self) -> None:

        self.max_items = max(1, min(self.max_items, 9999))
        self.timeout = normalize_platform_timeout(self.timeout, default=60)

@dataclass
class AuthSettings:
    """集中保存各平台 Cookie 文件的位置。"""
    bilibili_cookie_file: str = "bili_auth.json"
    kuaishou_cookie_file: str = "ks_auth.json"
    douyin_cookie_file: str = "dy_auth.json"
    xiaohongshu_cookie_file: str = "xhs_auth.json"

    def normalize(self) -> None:

        self.bilibili_cookie_file = str(resolve_user_file(self.bilibili_cookie_file))
        self.kuaishou_cookie_file = str(resolve_user_file(self.kuaishou_cookie_file))
        self.douyin_cookie_file = str(resolve_user_file(self.douyin_cookie_file))
        self.xiaohongshu_cookie_file = str(resolve_user_file(self.xiaohongshu_cookie_file))

@dataclass
class DownloadSettings:
    """保存下载并发、重试、断点续传和图片快速通道参数。"""
    max_concurrent: int = 3
    local_scan_limit: int = 1000
    max_retries: int = 3
    auto_retry: bool = True
    request_timeout: int = 60
    chunk_size: int = 65536
    resume_enabled: bool = True
    speed_limit_kb: int = 0
    video_only: bool = False
    image_respects_concurrency: bool = False
    image_fast_lane_limit: int = 10

    def normalize(self) -> None:

        self.max_concurrent = normalize_download_concurrency(self.max_concurrent)
        self.local_scan_limit = max(100, min(self.local_scan_limit, 5000))
        self.max_retries = max(0, min(self.max_retries, 10))
        self.auto_retry = bool(self.auto_retry)
        self.request_timeout = max(10, min(self.request_timeout, 300))
        self.chunk_size = max(8192, min(self.chunk_size, 1024 * 1024))
        self.speed_limit_kb = max(0, min(self.speed_limit_kb, 999999))
        self.image_respects_concurrency = bool(self.image_respects_concurrency)
        try:
            image_limit = int(self.image_fast_lane_limit or 10)
        except (TypeError, ValueError):
            image_limit = 10
        self.image_fast_lane_limit = max(1, min(image_limit, 10))


def effective_download_retry_count(max_retries: Any, auto_retry: Any = True, *, default: int = 3) -> int:
    """Return the configured failure retry budget, or zero when retries are disabled."""
    if auto_retry is None:
        retries_enabled = True
    elif isinstance(auto_retry, str):
        retries_enabled = auto_retry.strip().lower() not in {"0", "false", "no", "off"}
    else:
        retries_enabled = bool(auto_retry)
    if not retries_enabled:
        return 0
    try:
        retry_count = int(max_retries)
    except (TypeError, ValueError, OverflowError):
        retry_count = default
    return max(0, min(retry_count, 10))

@dataclass
class PlaybackSettings:
    """封装本地播放体验设置，供 GUI 与 WebUI 共用。"""
    default_player: str = DEFAULT_OPEN_MODE
    builtin_player_enabled: bool = True
    remember_position: bool = True
    hardware_acceleration: bool = True
    autoplay_next: bool = True
    manual_image_switch: bool = False
    image_auto_advance_interval_seconds: int = 5

    def normalize(self) -> None:

        if self.default_player not in _option_values(PLAYBACK_PLAYER_OPTIONS):
            self.default_player = DEFAULT_OPEN_MODE
        if self.builtin_player_enabled is False and self.default_player == DEFAULT_OPEN_MODE:
            self.default_player = "system_default"
        self.builtin_player_enabled = bool(self.builtin_player_enabled)
        if str(self.image_auto_advance_interval_seconds) not in _option_values(IMAGE_AUTO_ADVANCE_INTERVAL_OPTIONS):
            self.image_auto_advance_interval_seconds = 5

@dataclass
class LogSettings:
    """封装日志中心展示与清理策略。"""
    retention_days: int = 1
    failed_record_retention_days: int = 7
    level: str = "info"
    ui_log_max_display_count: int = 300
    auto_copy_trace_on_error: bool = True
    # 默认 False。该字段只兼容旧配置，启动时不因此清理旧日志。
    cleanup_old_logs_on_start: bool = False

    def normalize(self) -> None:

        if str(self.retention_days) not in _option_values(LOG_RETENTION_OPTIONS):
            self.retention_days = 1
        if str(self.failed_record_retention_days) not in _option_values(FAILED_RECORD_RETENTION_OPTIONS):
            self.failed_record_retention_days = 7
        self.ui_log_max_display_count = normalize_ui_log_max_display_count(self.ui_log_max_display_count)
        if self.level not in _option_values(LOG_LEVEL_OPTIONS):
            self.level = "info"

@dataclass
class AppearanceSettings:
    """封装主题之外的界面偏好；主题本身保留在 common 以兼容旧入口。"""
    follow_system: bool = False
    accent: str = "blue"
    scale: str = "100%"
    font_size: str = "medium"
    language: str = "zh-CN"

    def normalize(self) -> None:

        if self.accent not in _option_values(ACCENT_OPTIONS):
            self.accent = "blue"
        if self.scale not in _option_values(SCALE_OPTIONS):
            self.scale = "100%"
        if self.font_size not in _option_values(FONT_SIZE_OPTIONS):
            self.font_size = "medium"
        if self.language not in _option_values(LANGUAGE_OPTIONS):
            self.language = "zh-CN"

@dataclass
class UISettings:
    """保存桌面窗口几何与分割器状态。"""
    geometry: str = ""
    window_state: str = ""
    splitter_state: str = ""
    main_splitter_state: str = ""
    right_splitter_state: str = ""
    is_fullscreen_mode: bool = False

@dataclass
class AppSettings:
    """聚合所有配置分区，并统一执行启动时校验与迁移。"""
    common: CommonSettings = field(default_factory=CommonSettings)
    missav: MissAVSettings = field(default_factory=MissAVSettings)
    bilibili: BilibiliSettings = field(default_factory=BilibiliSettings)
    douyin: DouyinSettings = field(default_factory=DouyinSettings)
    xiaohongshu: XiaohongshuSettings = field(default_factory=XiaohongshuSettings)
    kuaishou: KuaishouSettings = field(default_factory=KuaishouSettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    download: DownloadSettings = field(default_factory=DownloadSettings)
    playback: PlaybackSettings = field(default_factory=PlaybackSettings)
    logging: LogSettings = field(default_factory=LogSettings)
    appearance: AppearanceSettings = field(default_factory=AppearanceSettings)
    ui: UISettings = field(default_factory=UISettings)

    def normalize(self) -> None:

        self.common.normalize()
        self.missav.normalize()
        self.bilibili.normalize()
        self.douyin.normalize()
        self.xiaohongshu.normalize()
        self.kuaishou.normalize()
        self.auth.normalize()
        self.download.normalize()
        self.playback.normalize()
        self.logging.normalize()
        self.appearance.normalize()

    def to_dict(self) -> dict[str, Any]:

        return asdict(self)

class _InstrumentedRLock:
    def __init__(self, name: str, *, warn_seconds: float = LOCK_WARN_SECONDS) -> None:
        self._lock = threading.RLock()
        self._name = name
        self._warn_seconds = warn_seconds
        self._logger = logging.getLogger(__name__)
        self._local = threading.local()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        wait_started = time_module.monotonic()
        acquired = self._lock.acquire(blocking, timeout)
        acquired_at = time_module.monotonic()
        if acquired:
            waited = acquired_at - wait_started
            if waited > self._warn_seconds:
                self._logger.warning("%s lock wait %.3fs", self._name, waited)
            stack = getattr(self._local, "acquired_at", None)
            if stack is None:
                stack = []
                self._local.acquired_at = stack
            stack.append(acquired_at)
        return acquired

    def release(self) -> None:
        stack = getattr(self._local, "acquired_at", None)
        acquired_at = stack.pop() if stack else time_module.monotonic()
        held = time_module.monotonic() - acquired_at
        try:
            self._lock.release()
        finally:
            if not stack:
                self._local.acquired_at = None
        if held > self._warn_seconds:
            self._logger.warning("%s lock held %.3fs", self._name, held)

    def __enter__(self) -> "_InstrumentedRLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


class ConfigManager:
    """协调内存配置、磁盘持久化和不同前端进程之间的变更通知。"""
    SECTION_MODELS = {
        "common": CommonSettings,
        "missav": MissAVSettings,
        "bilibili": BilibiliSettings,
        "douyin": DouyinSettings,
        "xiaohongshu": XiaohongshuSettings,
        "kuaishou": KuaishouSettings,
        "auth": AuthSettings,
        "download": DownloadSettings,
        "playback": PlaybackSettings,
        "logging": LogSettings,
        "appearance": AppearanceSettings,
        "ui": UISettings,
    }

    def __init__(self, filename: str = DEFAULT_CONFIG_FILE, event_bus: EventBus | None = None):
        """加载磁盘配置，并准备线程内与跨进程同步所需的状态。"""
        self._lock = _InstrumentedRLock("ConfigManager")
        self.event_bus = event_bus or EventBus()
        self.filename = str(resolve_user_file(filename))
        self._file_lock_path = Path(f"{self.filename}.lock")
        self._transaction_local = threading.local()
        self._disk_signature: tuple[int, int, int] | None = None
        self._external_sync_guard = threading.Lock()
        self._external_sync_refcount = 0
        self._external_sync_stop = threading.Event()
        self._external_sync_thread: threading.Thread | None = None
        # 配置文件可能位于首次启动才创建的用户目录中。
        Path(self.filename).parent.mkdir(parents=True, exist_ok=True)
        self.settings = AppSettings()
        self.last_load_error: ConfigReadError | None = None
        self._load_from_disk()
        self.settings.normalize()
        self._disk_signature = self._current_disk_signature()

    def subscribe(self, topic: str, handler: Callable[[Any], None]) -> Callable[[Any], None]:
        return self.event_bus.subscribe(topic, handler)

    def subscribe_async(self, topic: str, handler: Callable[[Any], None]) -> Callable[[Any], None]:
        """异步通知监听器，避免配置写线程被界面刷新或磁盘操作拖住。"""
        return self.event_bus.subscribe_async(topic, handler)

    def unsubscribe(self, topic: str, handler: Callable[[Any], None] | None = None) -> None:
        self.event_bus.unsubscribe(topic, handler)

    def _current_disk_signature(self) -> tuple[int, int, int] | None:
        try:
            stat = Path(self.filename).stat()
        except OSError:
            return None
        return (int(stat.st_mtime_ns), int(stat.st_size), int(stat.st_ctime_ns))

    def _transaction_depth(self) -> int:
        return int(getattr(self._transaction_local, "depth", 0) or 0)

    def _config_parent_capability(self) -> FilesystemDirectoryCapability | None:
        return getattr(self._transaction_local, "config_parent_capability", None)

    @contextmanager
    def _exclusive_file_lock(self):
        """Serialize configuration writes with a persistent OS advisory lock."""
        if self._transaction_depth() > 0:
            yield
            return

        try:
            with filesystem_directory_capability(Path(self.filename).parent) as capability:
                descriptor = acquire_file_lock_descriptor(
                    self._file_lock_path,
                    timeout_seconds=CONFIG_FILE_LOCK_TIMEOUT_SECONDS,
                )
                capability.assert_bound()
                self._transaction_local.depth = 1
                self._transaction_local.config_parent_capability = capability
                primary_error: BaseException | None = None
                try:
                    yield
                except BaseException as exc:
                    primary_error = exc
                    raise
                finally:
                    self._transaction_local.depth = 0
                    self._transaction_local.config_parent_capability = None
                    release_file_lock_descriptor(
                        descriptor,
                        acquired=True,
                        primary_error=primary_error,
                    )
        except OSError as exc:
            raise ConfigWriteError(str(exc)) from exc

    def _read_disk_data_unlocked(self) -> dict[str, Any]:
        try:
            capability = self._config_parent_capability()
            if capability is not None:
                capability.assert_bound()
            with open(self.filename, "r", encoding="utf-8") as fp:
                saved_data = json.load(fp)
            if capability is not None:
                capability.assert_bound()
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ConfigReadError(str(exc)) from exc
        if not isinstance(saved_data, dict):
            raise ConfigReadError("config root must be an object")
        return saved_data

    def _replace_settings_from_data_unlocked(
        self,
        saved_data: dict[str, Any],
        *,
        normalize_temporary_paths: bool = False,
    ) -> None:
        previous = self.settings
        self.settings = AppSettings()
        try:
            self._apply_data(
                saved_data,
                normalize_temporary_paths=normalize_temporary_paths,
            )
            self.settings.normalize()
        except Exception:
            self.settings = previous
            raise

    @classmethod
    def _diff_settings_data(
        cls,
        old_data: Mapping[str, Any],
        new_data: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for section in cls.SECTION_MODELS:
            old_section = old_data.get(section, {})
            new_section = new_data.get(section, {})
            if not isinstance(old_section, Mapping) or not isinstance(new_section, Mapping):
                continue
            for key, value in new_section.items():
                old_value = old_section.get(key)
                if old_value != value:
                    changes.append(
                        {
                            "section": section,
                            "key": str(key),
                            "value": value,
                            "old_value": old_value,
                        }
                    )
        return changes

    def _read_disk_snapshot_unlocked(
        self,
    ) -> tuple[dict[str, Any] | None, tuple[int, int, int] | None]:
        """读取一份 JSON。只有读前后签名一致时，才返回实际读到的那一版签名。"""
        before = self._current_disk_signature()
        if before is None:
            return None, None
        try:
            saved_data = self._read_disk_data_unlocked()
        except ConfigReadError:
            after = self._current_disk_signature()
            if before != after:
                return None, None
            raise
        after = self._current_disk_signature()
        if before != after:
            return None, None
        return saved_data, before

    def _read_consistent_disk_data_unlocked(
        self,
    ) -> tuple[dict[str, Any], tuple[int, int, int]]:
        """签名在读取期间变化时，在文件锁内重读，并固定为读到的那一版。"""
        saved_data, signature = self._read_disk_snapshot_unlocked()
        if saved_data is not None and signature is not None:
            return saved_data, signature
        with self._exclusive_file_lock():
            for _ in range(3):
                saved_data, signature = self._read_disk_snapshot_unlocked()
                if saved_data is not None and signature is not None:
                    return saved_data, signature
        raise ConfigReadError("config changed while reading")

    def _refresh_from_disk_unlocked(self, *, force: bool = False) -> list[dict[str, Any]]:
        signature = self._current_disk_signature()
        if signature is None:
            return []
        if not force and signature == self._disk_signature:
            return []
        old_data = self.settings.to_dict()
        saved_data, read_signature = self._read_consistent_disk_data_unlocked()
        self._replace_settings_from_data_unlocked(saved_data)
        self.last_load_error = None
        self._disk_signature = read_signature
        return self._diff_settings_data(old_data, self.settings.to_dict())

    @staticmethod
    def _combined_change_payload(
        changes: list[dict[str, Any]],
        *,
        external: bool = False,
    ) -> dict[str, Any] | None:
        if not changes:
            return None
        payload = dict(changes[-1])
        if len(changes) > 1:
            payload["changes"] = [dict(change) for change in changes]
        if external:
            payload["external"] = True
        return payload

    def reload_if_changed(self) -> bool:
        payload: dict[str, Any] | None = None
        with self._lock:
            try:
                changes = self._refresh_from_disk_unlocked()
            except (ConfigReadError, ConfigValidationError) as exc:
                self.last_load_error = ConfigReadError(str(exc))
                return False
            payload = self._combined_change_payload(changes, external=True)
        if payload is not None:
            self.event_bus.publish("config.changed", payload)
            return True
        return False

    @property
    def external_sync_running(self) -> bool:
        with self._external_sync_guard:
            return bool(self._external_sync_thread and self._external_sync_thread.is_alive())

    def start_external_sync(
        self,
        *,
        interval_seconds: float = CONFIG_EXTERNAL_SYNC_INTERVAL_SECONDS,
    ) -> None:
        """按引用计数启动磁盘轮询，使多个前端共享同一条同步线程。"""
        interval = max(0.02, float(interval_seconds or CONFIG_EXTERNAL_SYNC_INTERVAL_SECONDS))
        with self._external_sync_guard:
            self._external_sync_refcount += 1
            if self._external_sync_thread and self._external_sync_thread.is_alive():
                return
            local_stop = threading.Event()
            self._external_sync_stop = local_stop

            def _watch() -> None:
                while not local_stop.wait(interval):
                    try:
                        self.reload_if_changed()
                    except Exception:
                        logging.getLogger(__name__).exception("external config sync failed")

            self._external_sync_thread = threading.Thread(
                target=_watch,
                name="ConfigExternalSync",
                daemon=True,
            )
            self._external_sync_thread.start()

    def stop_external_sync(self) -> None:
        thread: threading.Thread | None = None
        with self._external_sync_guard:
            if self._external_sync_refcount > 0:
                self._external_sync_refcount -= 1
            if self._external_sync_refcount > 0:
                return
            thread = self._external_sync_thread
            self._external_sync_thread = None
            self._external_sync_stop.set()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _load_from_disk(self) -> None:
        with self._lock:
            self._load_from_disk_unlocked()

    def _load_from_disk_unlocked(self) -> None:
        """读取配置；文件缺失时创建默认值，损坏时备份并重建。"""
        try:
            with open(self.filename, "r", encoding="utf-8") as fp:
                saved_data = json.load(fp)
        except FileNotFoundError:
            self.save()
            return
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.last_load_error = ConfigReadError(str(exc))
            self._reset_config()
            return

        if not isinstance(saved_data, dict):
            self._reset_config()
            return

        try:
            self._apply_data(saved_data)
        except ConfigValidationError:
            self._reset_config()

    def _apply_data(
        self,
        saved_data: dict[str, Any],
        *,
        normalize_temporary_paths: bool = True,
    ) -> None:
        """按分区模型恢复已知字段，忽略旧版本遗留的未知字段。"""
        for section_name, section_model in self.SECTION_MODELS.items():
            raw_section = saved_data.get(section_name, {})
            if raw_section is None:
                continue
            if not isinstance(raw_section, dict):
                raise ConfigValidationError(f"{section_name} 必须是对象")
            if section_name == "common":
                raw_section = self._migrate_common_section(raw_section)
                if normalize_temporary_paths and is_temporary_path(raw_section.get("save_directory", "")):
                    raw_section["save_directory"] = DEFAULT_DOWNLOAD_DIR

            current = getattr(self.settings, section_name)
            defaults = asdict(section_model())
            normalized: dict[str, Any] = {}
            for key, default_value in defaults.items():
                value = raw_section.get(key, getattr(current, key))
                normalized[key] = self._coerce_value(key, value, default_value)
            setattr(self.settings, section_name, section_model(**normalized))
            self._normalize_section(section_name)

    @staticmethod
    def _migrate_common_section(raw_section: dict[str, Any]) -> dict[str, Any]:
        migrated = dict(raw_section)
        if "theme_schema_version" not in migrated:
            if migrated.get("theme") == "dark" and migrated.get("dark_theme") is True:
                migrated["theme"] = "light"
                migrated["dark_theme"] = False
            migrated["theme_schema_version"] = 2
        return migrated

    def _coerce_value(self, key: str, value: Any, default_value: Any) -> Any:
        """依据 dataclass 默认值恢复 JSON 标量类型，并拒绝含糊的数值输入。"""
        if value is None:
            return default_value
        expected_type = type(default_value)
        if expected_type is bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in {"1", "true", "yes", "on", "dark"}
            return bool(value)
        if expected_type is int:
            if isinstance(value, bool):
                raise ConfigValidationError(f"{key} 类型非法")
            try:
                return int(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ConfigValidationError(f"{key} 必须是整数") from exc
        if expected_type is float:
            if isinstance(value, bool):
                raise ConfigValidationError(f"{key} 类型非法")
            try:
                return float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ConfigValidationError(f"{key} 必须是数字") from exc
        if expected_type is str:
            return str(value)
        return value

    def _normalize_field_value(self, section: str, key: str, value: Any) -> Any:
        if section == "common" and key == "save_directory":
            return normalize_download_directory_input(value, create=True)
        if section == "common" and key == "filename_template":
            template = str(value or "").strip() or CURRENT_FILENAME_TEMPLATE
            if template not in _option_values(FILENAME_TEMPLATE_OPTIONS):
                raise ConfigValidationError(f"未知文件命名规则: {value}")
            return template
        if section == "common" and key == "default_open_mode":
            mode = str(value or "").strip()
            if mode not in _option_values(OPEN_MODE_OPTIONS):
                raise ConfigValidationError(f"未知默认打开方式: {value}")
            return mode
        if section == "common" and key == "theme":
            theme = str(value or "light").strip().lower()
            if theme not in SUPPORTED_THEMES:
                raise ConfigValidationError(f"未知主题: {value}")
            return theme
        if section == "playback" and key == "default_player":
            player = str(value or DEFAULT_OPEN_MODE).strip()
            if player not in _option_values(PLAYBACK_PLAYER_OPTIONS):
                raise ConfigValidationError(f"未知播放器: {value}")
            return player
        if section == "playback" and key == "image_auto_advance_interval_seconds":
            seconds = int(value)
            if str(seconds) not in _option_values(IMAGE_AUTO_ADVANCE_INTERVAL_OPTIONS):
                raise ConfigValidationError(f"unknown image auto advance interval: {value}")
            return seconds
        if section == "logging" and key == "level":
            level = str(value or "info").strip().lower()
            if level not in _option_values(LOG_LEVEL_OPTIONS):
                raise ConfigValidationError(f"未知日志级别: {value}")
            return level
        if section == "logging" and key == "retention_days":
            days = int(value)
            if str(days) not in _option_values(LOG_RETENTION_OPTIONS):
                raise ConfigValidationError(f"未知日志保留天数: {value}")
            return days
        if section == "logging" and key == "failed_record_retention_days":
            days = int(value)
            if str(days) not in _option_values(FAILED_RECORD_RETENTION_OPTIONS):
                raise ConfigValidationError(f"未知失败记录保留天数: {value}")
            return days
        if section == "appearance" and key == "accent":
            accent = str(value or "blue").strip().lower()
            if accent not in _option_values(ACCENT_OPTIONS):
                raise ConfigValidationError(f"未知主题色: {value}")
            return accent
        if section == "appearance" and key == "scale":
            scale = str(value or "100%").strip()
            if scale not in _option_values(SCALE_OPTIONS):
                raise ConfigValidationError(f"未知界面缩放: {value}")
            return scale
        if section == "appearance" and key == "font_size":
            font_size = str(value or "medium").strip().lower()
            if font_size not in _option_values(FONT_SIZE_OPTIONS):
                raise ConfigValidationError(f"未知字体大小: {value}")
            return font_size
        if section == "appearance" and key == "language":
            language = str(value or "zh-CN").strip()
            if language not in _option_values(LANGUAGE_OPTIONS):
                raise ConfigValidationError(f"未知界面语言: {value}")
            return language
        if section == "logging" and key == "ui_log_max_display_count":
            return normalize_ui_log_max_display_count(value)
        return value

    def _reset_config(self) -> None:
        """先保留损坏配置副本，再用经过规范化的默认配置重建文件。"""
        backup_name = f"{self.filename}.bak.{int(time_module.time())}"
        if os.path.exists(self.filename):
            try:
                shutil.move(self.filename, backup_name)
            except OSError as exc:
                raise ConfigWriteError(f"备份损坏配置失败: {exc}") from exc
        self.settings = AppSettings()
        self.settings.normalize()
        self.save()

    def _normalize_section(self, section: str) -> None:
        """仅规范刚发生变化的分区，避免无关设置被旧值覆盖。"""
        section_obj = getattr(self.settings, section, None)
        normalize = getattr(section_obj, "normalize", None)
        if callable(normalize):
            normalize()

    def save(self) -> None:
        with self._lock:
            with self._exclusive_file_lock():
                self._save_unlocked()

    def _save_unlocked(self) -> None:

        import logging
        logger = logging.getLogger(__name__)
        target_file = Path(self.filename)
        parent = target_file.parent
        capability = self._config_parent_capability()
        try:
            if capability is not None:
                capability.assert_bound()
        except OSError as exc:
            raise ConfigWriteError(str(exc)) from exc
        parent.mkdir(parents=True, exist_ok=True)
        last_permission_error: PermissionError | None = None

        for attempt in range(4):
            temp_file: Path | None = None
            try:
                # Windows 上 GUI、WebUI 或安全软件可能短暂占用 config.json；
                # 每次重试都写入新的临时文件，再执行原子替换，避免留下半份 JSON。
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=parent,
                    prefix=f".{target_file.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as fp:
                    json.dump(self.settings.to_dict(), fp, indent=4, ensure_ascii=False)
                    fp.flush()
                    os.fsync(fp.fileno())
                    temp_file = Path(fp.name)
                if capability is not None:
                    capability.assert_bound()
                temp_file.replace(target_file)
                if capability is not None:
                    capability.assert_bound()
                self._disk_signature = self._current_disk_signature()
                return
            except PermissionError as exc:
                last_permission_error = exc
                if attempt < 3:
                    time_module.sleep(0.05 * (attempt + 1))
                    continue
                break
            except (OSError, TypeError, ValueError) as exc:
                raise ConfigWriteError(str(exc)) from exc
            finally:
                if temp_file is not None:
                    try:
                        temp_file.unlink(missing_ok=True)
                    except OSError:
                        pass

        if last_permission_error is not None:
            logger.warning(f"[ConfigManager] 保存配置失败 (Permission denied): {last_permission_error}")
            raise ConfigWriteError(str(last_permission_error)) from last_permission_error

    @property
    def data(self) -> dict[str, Any]:

        with self._lock:
            return self.settings.to_dict()

    def get(self, section: str, key: str, default: Any = None) -> Any:

        with self._lock:
            section_obj = getattr(self.settings, section, None)
            if section_obj is None:
                return default
            value = getattr(section_obj, key, default)
            return default if value is None else value

    def set(self, section: str, key: str, value: Any) -> None:
        payload: dict[str, Any] | None = None
        with self._lock:
            rollback_data = self.settings.to_dict()
            rollback_disk_signature = self._disk_signature
            try:
                with self._exclusive_file_lock():
                    external_changes = self._refresh_from_disk_unlocked(force=True)
                    rollback_data = self.settings.to_dict()
                    rollback_disk_signature = self._disk_signature
                    local_change = self._set_unlocked(section, key, value)
                    changes = [*external_changes]
                    if local_change is not None:
                        changes.append(local_change)
                    payload = self._combined_change_payload(
                        changes,
                        external=bool(external_changes),
                    )
            except Exception:
                self._replace_settings_from_data_unlocked(rollback_data)
                self._disk_signature = rollback_disk_signature
                raise
        if payload is not None:
            self.event_bus.publish("config.changed", payload)

    def set_many(self, section: str, values: dict[str, Any]) -> None:
        self.set_batch({section: values})

    def set_batch(self, updates: Mapping[str, Mapping[str, Any]]) -> None:
        """在同一次进程间协作租约内校验并提交多个分区，失败时回滚本实例状态。"""
        payload: dict[str, Any] | None = None
        with self._lock:
            rollback_data = self.settings.to_dict()
            rollback_disk_signature = self._disk_signature
            try:
                with self._exclusive_file_lock():
                    external_changes = self._refresh_from_disk_unlocked(force=True)
                    rollback_data = self.settings.to_dict()
                    rollback_disk_signature = self._disk_signature
                    local_changes: list[dict[str, Any]] = []
                    for section, values in (updates or {}).items():
                        if not isinstance(values, Mapping):
                            raise ConfigValidationError(f"{section} 必须是对象")
                        for key, value in values.items():
                            change = self._set_unlocked(str(section), str(key), value, persist=False)
                            if change is not None:
                                local_changes.append(change)
                    if local_changes:
                        self.save()
                    payload = self._combined_change_payload(
                        [*external_changes, *local_changes],
                        external=bool(external_changes),
                    )
            except Exception:
                self._replace_settings_from_data_unlocked(rollback_data)
                self._disk_signature = rollback_disk_signature
                raise
        if payload is not None:
            self.event_bus.publish("config.changed", payload)

    def _set_unlocked(self, section: str, key: str, value: Any, *, persist: bool = True) -> dict[str, Any] | None:

        section_obj = getattr(self.settings, section, None)
        if section_obj is None:
            raise ConfigValidationError(f"未知配置分组: {section}")
        defaults = asdict(self.SECTION_MODELS[section]())
        if key not in defaults:
            raise ConfigValidationError(f"未知配置字段: {section}.{key}")
        old_value = getattr(section_obj, key)
        coerced = self._coerce_value(key, value, defaults[key])
        coerced = self._normalize_field_value(section, key, coerced)
        if old_value == coerced:
            return None
        setattr(section_obj, key, coerced)
        self._normalize_section(section)
        if persist:
            self.save()
        return {
            "section": section,
            "key": key,
            "value": getattr(section_obj, key),
            "old_value": old_value,
        }

    def save_ui_state(
        self,
        geometry: Any,
        state: Any,
        main_splitter: Any,
        right_splitter: Any,
        is_fs: bool,
    ) -> None:
        """在同一次进程间协作租约中保存窗口几何、分栏和全屏状态。"""
        with self._lock:
            rollback_data = self.settings.to_dict()
            rollback_disk_signature = self._disk_signature
            try:
                with self._exclusive_file_lock():
                    self._refresh_from_disk_unlocked(force=True)
                    rollback_data = self.settings.to_dict()
                    rollback_disk_signature = self._disk_signature
                    self.settings.ui.geometry = self._encode_ui_state(geometry)
                    self.settings.ui.window_state = self._encode_ui_state(state)
                    self.settings.ui.main_splitter_state = self._encode_ui_state(main_splitter)
                    self.settings.ui.right_splitter_state = self._encode_ui_state(right_splitter)
                    self.settings.ui.is_fullscreen_mode = is_fs
                    self.save()
            except Exception:
                self._replace_settings_from_data_unlocked(rollback_data)
                self._disk_signature = rollback_disk_signature
                raise

    @staticmethod
    def _encode_ui_state(value: Any) -> str:
        """把 UI 状态编码成十六进制字符串，不依赖 Qt 类型。"""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, memoryview):
            return value.tobytes().hex()
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).hex()
        to_hex = getattr(value, "toHex", None)
        if callable(to_hex):
            hex_value = to_hex()
            if isinstance(hex_value, memoryview):
                return hex_value.tobytes().decode()
            if isinstance(hex_value, (bytes, bytearray)):
                return bytes(hex_value).decode()
            data = getattr(hex_value, "data", None)
            if callable(data):
                raw = data()
                if isinstance(raw, memoryview):
                    raw = raw.tobytes()
                if isinstance(raw, (bytes, bytearray)):
                    return bytes(raw).decode()
                if isinstance(raw, str):
                    return raw
            return str(hex_value)
        data = getattr(value, "data", None)
        if callable(data):
            raw = data()
            if isinstance(raw, memoryview):
                raw = raw.tobytes()
            if isinstance(raw, (bytes, bytearray)):
                try:
                    return bytes(raw).decode()
                except UnicodeDecodeError:
                    return bytes(raw).hex()
            if isinstance(raw, str):
                return raw
        return str(value)

    def update_missav_proxy(self, proxy_app: str = "", port: int | str = 0, url: str = "") -> None:
        """更新 MissAV 代理字段，并兼容仍按两个参数调用的旧入口。"""
        if isinstance(port, str) and not url:
            url = port
            port = 0
        proxy_app = str(proxy_app or "").strip()
        proxy_url = str(url or "").strip()

        def _infer_proxy_type(label: str, resolved_url: str) -> str:
            text = f"{label} {resolved_url}".strip().lower()
            if "\u76f4\u8fde" in text:
                return "direct"
            if "\u7cfb\u7edf\u4ee3\u7406" in text:
                return "system"
            if "v2ray" in text:
                return "v2ray"
            if "sing-box" in text:
                return "sing-box"
            if "nekoray" in text:
                return "nekoray"
            if "\u81ea\u5b9a\u4e49" in text or ":" in resolved_url:
                return "custom"
            return "clash"

        def _infer_port(resolved_url: str, fallback: int) -> int:
            try:
                parsed = urlparse(resolved_url)
                if parsed.port:
                    return int(parsed.port)
            except ValueError:
                pass
            try:
                return int(port or fallback)
            except (TypeError, ValueError):
                return fallback

        with self._lock:
            fallback_port = self.settings.missav.proxy_port
        values: dict[str, Any] = {}
        if proxy_app:
            values["proxy_app"] = proxy_app
            values["proxy_type"] = _infer_proxy_type(proxy_app, proxy_url)
        if proxy_url or proxy_app in {"\u76f4\u8fde", "\u7cfb\u7edf\u4ee3\u7406"}:
            values["proxy_url"] = proxy_url
        resolved_port = _infer_port(proxy_url, fallback_port)
        if resolved_port:
            values["proxy_port"] = resolved_port
        if values:
            self.set_many("missav", values)

def _build_default_settings() -> AppSettings:
    """构建一份已标准化的默认配置快照，供无状态读取场景复用。"""
    settings = AppSettings()
    settings.normalize()
    return settings

DEFAULT_APP_SETTINGS = _build_default_settings()

_PLATFORM_SECTION_MAP = {
    "douyin": "douyin",
    "xiaohongshu": "xiaohongshu",
    "bilibili": "bilibili",
    "kuaishou": "kuaishou",
    "missav": "missav",
}

_PLATFORM_RUNTIME_FIELDS = {
    "douyin": ("max_items", "timeout"),
    "xiaohongshu": (
        "max_items",
        "search_max_pages",
        "timeout",
        "request_interval",
        "detail_request_interval",
        "sort",
        "note_type",
    ),
    "bilibili": ("max_pages", "max_items", "timeout", "api_workers"),
    "kuaishou": ("max_items", "timeout"),
    "missav": ("max_items", "search_max_pages", "timeout", "individual_only", "priority"),
}

def get_setting_default(section: str, key: str) -> Any:
    """读取配置模型中的默认值，避免调用方散落硬编码。"""
    section_obj = getattr(DEFAULT_APP_SETTINGS, section, None)
    if section_obj is None:
        raise ConfigValidationError(f"未知配置分组: {section}")
    if not hasattr(section_obj, key):
        raise ConfigValidationError(f"未知配置字段: {section}.{key}")
    return getattr(section_obj, key)

def get_platform_default_values(source: str) -> dict[str, Any]:
    """返回平台默认运行参数快照，不读取持久化配置。"""
    section_name = _PLATFORM_SECTION_MAP.get(source)
    if not section_name:
        return {}

    section_obj = getattr(DEFAULT_APP_SETTINGS, section_name)
    result = {field: getattr(section_obj, field) for field in _PLATFORM_RUNTIME_FIELDS.get(source, ())}
    if source == "missav":
        result["proxy"] = section_obj.proxy_url
    return result

def get_platform_runtime_defaults(source: str, manager: ConfigManager | None = None) -> dict[str, Any]:
    """返回平台运行配置快照，优先读取持久化配置，兜底配置模型默认值。"""
    if source not in _PLATFORM_SECTION_MAP:
        return {}

    active_manager = manager or globals().get("cfg")
    if active_manager is None:
        return get_platform_default_values(source)

    def _snapshot() -> dict[str, Any]:
        section_name = _PLATFORM_SECTION_MAP[source]
        section_obj = getattr(active_manager.settings, section_name, None)
        if section_obj is None:
            return get_platform_default_values(source)
        result = {field: getattr(section_obj, field) for field in _PLATFORM_RUNTIME_FIELDS.get(source, ())}
        if source == "missav":
            result["proxy"] = section_obj.proxy_url
        return result

    lock = getattr(active_manager, "_lock", None)
    if lock is None:
        return _snapshot()
    with lock:
        return _snapshot()

cfg = ConfigManager()
