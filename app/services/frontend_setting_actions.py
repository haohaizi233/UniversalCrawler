"""前端设置、重试与文件关联动作。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.config.settings import get_setting_default
from app.core.plugins.run_options import build_missav_proxy_url
from app.core.state import VideoStatus
from app.debug_logger import debug_logger
from app.exceptions import ConfigValidationError, ConfigWriteError
from app.services.frontend_action_result import FrontendActionResult


class FrontendSettingActionsMixin:
    """从 FrontendStateService 拆出的设置与本地文件动作。"""

    def _action_update_setting(self, payload: Mapping[str, Any]) -> FrontendActionResult:
        section = str(payload.get("section") or payload.get("group") or "").strip()
        key = str(payload.get("key") or payload.get("name") or "").strip()
        value = payload.get("value")
        if section == "basic":
            section = "common"
        if section == "appearance" and key == "theme":
            section = "common"
        if not section or not key:
            return FrontendActionResult("error", "setting section and key are required")
        try:
            default = get_setting_default(section, key)
        except ConfigValidationError:
            default = None
        if isinstance(default, bool) and not isinstance(value, bool):
            return FrontendActionResult("error", f"{section}.{key} must be a boolean")
        if section == "common":
            return self._action_update_basic_setting({"key": key, "value": value})
        if section == "download" and key in {"max_concurrent", "max_retries", "video_only", "image_respects_concurrency"}:
            options = self.download_options_snapshot()
            options[key] = value
            return self._action_update_download_options(options)
        section_models = getattr(self.config, "SECTION_MODELS", {})
        if section not in section_models:
            return FrontendActionResult("error", f"unknown setting section: {section}")
        try:
            if section == "missav" and key in {"proxy_app", "proxy_url"}:
                proxy_text = str(value or "").strip()
                proxy_url = (
                    str(self.config.get("missav", "proxy_url", "") or "").strip()
                    if key == "proxy_app" and proxy_text == "自定义"
                    else build_missav_proxy_url(proxy_text)
                )
                proxy_app = proxy_text if key == "proxy_app" else "自定义"
                updater = getattr(self.config, "update_missav_proxy", None)
                if callable(updater):
                    updater(proxy_app or "自定义", proxy_url)
                else:
                    self.config.set("missav", key, value)
                    if key == "proxy_app":
                        self.config.set("missav", "proxy_url", proxy_url)
                    elif key == "proxy_url":
                        self.config.set("missav", "proxy_app", "自定义")
                current_value = self.config.get("missav", key)
                if not self._config_events_drive_runtime:
                    self._static_snapshot_cache = None
                    self.record_event("settings.update", {"section": section, "key": key})
                return FrontendActionResult(
                    "ok",
                    "setting updated",
                    {"section": section, "key": key, "value": current_value, "proxy_url": proxy_url},
                )
            with self._capture_config_runtime_failures() as runtime_failures:
                self.config.set(section, key, value)
                current_value = self.config.get(section, key)
                if not self._config_events_drive_runtime:
                    self._apply_runtime_setting(section, key, current_value)
        except (ConfigValidationError, ConfigWriteError) as exc:
            return FrontendActionResult("error", str(exc), {"section": section, "key": key})
        except Exception as exc:
            debug_logger.log_exception(
                "FrontendStateService",
                "apply_runtime_setting",
                exc,
                details={"section": section, "key": key},
            )
            return FrontendActionResult(
                "error",
                f"setting persisted but runtime apply failed: {exc}",
                {"section": section, "key": key},
            )
        if runtime_failures:
            return FrontendActionResult(
                "error",
                self._runtime_failure_message("setting", runtime_failures),
                {"section": section, "key": key, "value": current_value},
            )
        if not self._config_events_drive_runtime:
            if self._platform_auth_config_affects_status(section, key):
                self._invalidate_platform_auth_cache()
            else:
                self._static_snapshot_cache = None
            self.record_event("settings.update", {"section": section, "key": key})
        return FrontendActionResult(
            "ok",
            "setting updated",
            {"section": section, "key": key, "value": current_value},
        )

    def _action_retry_failed(self, payload: Mapping[str, Any]) -> FrontendActionResult:
        video_id = str(payload.get("id") or payload.get("video_id") or "")
        item = self._video_for_update(video_id)
        manager = self._dl_manager()
        add_task = getattr(manager, "add_task", None) if manager is not None else None
        if not video_id or item is None or not callable(add_task):
            return FrontendActionResult("error", "task cannot be retried", {"video_id": video_id})
        previous_status = item.status
        previous_progress = item.progress
        item.status = VideoStatus.PENDING.label
        item.progress = 0
        try:
            added = add_task(item, self._current_save_dir())
        except Exception as exc:
            item.status = previous_status
            item.progress = previous_progress
            debug_logger.log_exception(
                "FrontendStateService",
                "retry_failed",
                exc,
                details={"video_id": video_id},
            )
            return FrontendActionResult("error", f"retry failed: {exc}", {"video_id": video_id})
        if added is False or added == 0:
            item.status = previous_status
            item.progress = previous_progress
            return FrontendActionResult("error", "task cannot be retried", {"video_id": video_id})
        self.record_event("videos.upsert", {"video_id": video_id})
        return FrontendActionResult("ok", "retry queued", {"video_id": video_id})

    def _action_copy_diagnostics(self, payload: Mapping[str, Any]) -> FrontendActionResult:
        video_id = str(payload.get("id") or payload.get("video_id") or "")
        item = self._videos().get(video_id)
        trace_id = self._trace_id(item) if item else ""
        if not trace_id and video_id:
            record = self._failed_record_for_id(video_id)
            trace_id = str((record or {}).get("trace_id") or "")
        if not item and not trace_id:
            return FrontendActionResult("error", "task not found")
        if not trace_id:
            return FrontendActionResult("error", "trace id not found", {"video_id": video_id})
        return FrontendActionResult("ok", "trace id ready", {"text": trace_id, "trace_id": trace_id})

    def _failed_record_for_id(self, video_id: str) -> dict[str, Any] | None:
        normalized_id = str(video_id or "")
        if not normalized_id:
            return None
        for row in self._failed_record_snapshot_items():
            if str(row.get("id") or row.get("video_id") or "") == normalized_id:
                return row
        record_by_id = getattr(self.failed_record_store, "record_by_id", None)
        if not callable(record_by_id):
            return None
        try:
            row = record_by_id(normalized_id)
        except Exception as exc:
            debug_logger.log_exception(
                "FrontendStateService",
                "failed_record_store.record_by_id",
                exc,
                details={"video_id": normalized_id},
            )
            return None
        return dict(row) if isinstance(row, Mapping) else None

    def _action_update_basic_setting(self, payload: Mapping[str, Any]) -> FrontendActionResult:
        key = str(payload.get("key") or payload.get("name") or "").strip()
        value = payload.get("value")
        aliases = {"download_directory": "save_directory", "save_directory": "save_directory"}
        config_key = aliases.get(key, key)
        allowed = {
            "save_directory",
            "last_source",
            "filename_template",
            "open_after_download",
            "default_open_mode",
            "show_browser_window",
            "theme",
        }
        if config_key not in allowed:
            return FrontendActionResult("error", f"unknown basic setting: {key}")
        try:
            default = get_setting_default("common", config_key)
        except ConfigValidationError:
            default = None
        if isinstance(default, bool) and not isinstance(value, bool):
            return FrontendActionResult("error", f"common.{config_key} must be a boolean")
        if config_key == "save_directory" and value is None:
            value = payload.get("directory")
        runtime_failures: list[tuple[str, str, Exception]] = []
        try:
            with self._capture_config_runtime_failures() as runtime_failures:
                manual = self._strict_boolean(payload, "manual", default=True)
                manual_theme = config_key == "theme" and manual
                set_batch = getattr(self.config, "set_batch", None)
                if manual_theme and callable(set_batch):
                    set_batch(
                        {
                            "appearance": {"follow_system": False},
                            "common": {config_key: value},
                        }
                    )
                else:
                    if manual_theme:
                        self.config.set("appearance", "follow_system", False)
                    self.config.set("common", config_key, value)
                current_value = self.config.get("common", config_key)
                if not self._config_events_drive_runtime:
                    self._apply_runtime_setting("common", config_key, current_value)
        except (ConfigValidationError, ConfigWriteError) as exc:
            return FrontendActionResult("error", str(exc), {"key": key or config_key})
        except Exception as exc:
            debug_logger.log_exception(
                "FrontendStateService",
                "apply_basic_runtime_setting",
                exc,
                details={"key": config_key},
            )
            return FrontendActionResult(
                "error",
                f"setting persisted but runtime apply failed: {exc}",
                {"key": key or config_key},
            )
        if runtime_failures:
            return FrontendActionResult(
                "error",
                self._runtime_failure_message("setting", runtime_failures),
                {"section": "common", "key": key or config_key, "config_key": config_key, "value": current_value},
            )
        controller = self.controller
        if config_key == "save_directory" and controller is not None and hasattr(controller, "current_save_dir"):
            controller.current_save_dir = str(current_value)
        if not self._config_events_drive_runtime:
            self._static_snapshot_cache = None
            self.record_event("settings.update", {"section": "common", "key": config_key})
        data = {"section": "common", "key": key or config_key, "config_key": config_key, "value": current_value}
        if config_key == "save_directory":
            data["directory"] = current_value
        return FrontendActionResult("ok", "basic setting updated", data)

    def _action_change_directory(self, payload: Mapping[str, Any]) -> FrontendActionResult:
        return self._action_update_basic_setting({
            "key": "download_directory",
            "value": payload.get("directory"),
        })

    def _action_open_directory(self, payload: Mapping[str, Any]) -> FrontendActionResult:
        video_id = str(payload.get("id") or payload.get("video_id") or "")
        item = self._videos().get(video_id)
        file_path = getattr(item, "local_path", "") if item else ""
        if not file_path:
            return FrontendActionResult("error", "file path is unavailable")
        directory_path = Path(file_path).expanduser().parent
        if not directory_path.exists():
            return FrontendActionResult("error", "directory does not exist", {"directory": str(directory_path)})
        directory = str(directory_path)
        try:
            self._directory_opener(directory)
        except Exception as exc:
            return FrontendActionResult("error", str(exc), {"directory": directory})
        return FrontendActionResult("ok", "directory opened", {"directory": directory})

    def _action_open_file(self, payload: Mapping[str, Any]) -> FrontendActionResult:
        video_id = str(payload.get("id") or payload.get("video_id") or "")
        item = self._videos().get(video_id)
        file_path = getattr(item, "local_path", "") if item else ""
        if not file_path:
            return FrontendActionResult("error", "file path is unavailable")
        path = Path(file_path).expanduser()
        if not path.exists():
            return FrontendActionResult("error", "file does not exist", {"path": str(path)})
        try:
            self._open_file_path(path)
        except Exception as exc:
            return FrontendActionResult("error", str(exc), {"path": str(path)})
        return FrontendActionResult("ok", "file opened", {"path": str(path)})


    def _action_register_file_associations(self, payload: Mapping[str, Any]) -> FrontendActionResult:
        try:
            include_video = self._strict_boolean(payload, "include_video", default=True)
            include_image = self._strict_boolean(payload, "include_image", default=True)
        except ConfigValidationError as exc:
            return FrontendActionResult("error", str(exc))
        if not include_video and not include_image:
            return FrontendActionResult("error", "no file type selected")
        try:
            service = self._make_association_service()
            result = service.register_current_user(
                self._executable_path_provider(),
                include_video=include_video,
                include_image=include_image,
            )
            if not getattr(result, "registered", False):
                return FrontendActionResult("error", getattr(result, "message", "") or "file association registration failed")

            default_result = service.set_current_user_defaults(include_video=include_video, include_image=include_image)
            diagnostics = service.diagnose_current_user(include_video=include_video, include_image=include_image)
            pending = tuple(getattr(diagnostics, "pending_extensions", ()) or ())
            opened_settings = False
            if getattr(diagnostics, "available", False) and pending:
                opened_settings = bool(service.open_default_apps_settings())

            failed = tuple(getattr(default_result, "failed_extensions", ()) or ())
            defaulted = tuple(getattr(default_result, "defaulted_extensions", ()) or ())
            if pending:
                message = "default apps settings opened" if opened_settings else "manual default app confirmation required"
            elif failed:
                message = "some defaults failed"
            elif defaulted:
                message = "default file associations applied"
            else:
                message = getattr(default_result, "message", "") or "file association action completed"
            return FrontendActionResult(
                "ok",
                message,
                {
                    "defaulted_extensions": list(defaulted),
                    "failed_extensions": list(failed),
                    "pending_extensions": list(pending),
                    "opened_settings": opened_settings,
                },
            )
        except Exception as exc:
            return FrontendActionResult("error", str(exc))

    def _make_association_service(self) -> Any:
        if self._association_service_factory is not None:
            return self._association_service_factory()
        from app.services.windows_file_association_service import WindowsFileAssociationService

        return WindowsFileAssociationService()
