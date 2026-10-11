"""下载管理器启动时的有界恢复清理。"""

from __future__ import annotations

import os
import time
from typing import Any

from app.config import cfg
from app.debug_logger import debug_logger
from app.exceptions import AppError
from app.core.download_path_policy import (
    is_drive_or_filesystem_root,
    is_within_save_root,
    list_directory_entries,
)


class DownloadManagerStartupMaintenance:
    """由 DownloadManagerCore 继承的启动恢复清理。"""

    def _run_startup_maintenance(self) -> None:
        """在分发任务前异步清理过期工作目录，避免阻塞界面线程。"""
        started_at = time.monotonic()
        stats: dict[str, Any] = {}
        maintenance_error: Exception | None = None
        try:
            debug_logger.log(
                component="DownloadManager",
                action="startup_maintenance_started",
                message="Started bounded download recovery maintenance",
                status_code="DL_STARTUP_MAINTENANCE_START",
            )
        except Exception:
            pass
        try:
            result = self._sweep_m3u8_orphan_workspaces_on_startup()
            if isinstance(result, dict):
                stats = result
        except Exception as exc:  # pragma: no cover - 防御性工作线程隔离
            maintenance_error = exc
        finally:
            # The sweep has returned, successfully or otherwise. Diagnostics
            # must never keep queued downloads behind the startup gate.
            self._startup_maintenance_done.set()
            self._get_dispatch_slot_gate().set()

        if maintenance_error is not None:
            try:
                debug_logger.log_exception(
                    "DownloadManager",
                    "startup_maintenance_error",
                    maintenance_error,
                )
            except Exception:
                pass
        try:
            debug_logger.log(
                component="DownloadManager",
                action="startup_maintenance_completed",
                message="Completed bounded download recovery maintenance",
                status_code="DL_STARTUP_MAINTENANCE_DONE",
                details={
                    **stats,
                    "duration_ms": round((time.monotonic() - started_at) * 1000, 2),
                },
            )
        except Exception:
            pass

    def _sweep_m3u8_orphan_workspaces_on_startup(self) -> dict[str, Any]:
        """启动时只清理当前保存根之内、且仍属于本次 generation 的临时目录。"""
        stats: dict[str, Any] = {
            "removed_count": 0,
            "recovery_records_consumed": 0,
            "legacy_directories_scanned": 0,
            "legacy_scan_pending": False,
        }
        try:
            download_dir = str(cfg.get("common", "save_directory", "") or "").strip()
        except (OSError, RuntimeError, TypeError, ValueError, AppError) as exc:
            debug_logger.log_exception(
                "DownloadManager",
                "download_temp_sweep_config_error",
                exc,
            )
            return stats
        if not download_dir:
            return stats
        if is_drive_or_filesystem_root(download_dir):
            stats["rejected_drive_root"] = True
            debug_logger.log(
                component="DownloadManager",
                action="startup_cleanup_reject_drive_root",
                level="WARN",
                message="Startup cleanup refused a drive or filesystem root",
                status_code="DL_STARTUP_CLEANUP_REJECT_ROOT",
                details={"download_dir": download_dir},
            )
            return stats

        maintenance_root = os.path.normcase(os.path.abspath(os.path.expanduser(download_dir)))
        with self._STARTUP_MAINTENANCE_LOCK:
            if maintenance_root in self._STARTUP_MAINTENANCE_ROOTS:
                stats["skipped_duplicate_root"] = True
                return stats
            self._STARTUP_MAINTENANCE_ROOTS.add(maintenance_root)

        recovery_store = getattr(self, "_download_recovery_store", None)
        recovery_batch = None
        enumeration_failed = False
        if recovery_store is not None:
            try:
                recovery_batch = recovery_store.recovery_batch()
            except Exception as exc:
                enumeration_failed = True
                recovery_batch = None
                debug_logger.log_exception(
                    "DownloadManager",
                    "download_recovery_store_load_error",
                    exc,
                )

        owned_directories = self._startup_cleanup_directories(
            recovery_batch,
            maintenance_root,
        )
        sweep_targets = self._bounded_startup_sweep_targets(
            [download_dir, *owned_directories],
            maintenance_root,
        )

        hls_sweep_failed = False
        try:
            from app.core.downloaders.m3u8 import N_m3u8DL_RE_Downloader

            if sweep_targets:
                stats["removed_count"] += N_m3u8DL_RE_Downloader.sweep_orphaned_workspaces(
                    sweep_targets
                )
        except Exception as exc:
            hls_sweep_failed = True
            debug_logger.log_exception(
                "DownloadManager",
                "m3u8_orphan_workspace_sweep_error",
                exc,
                details={"download_dir": download_dir},
            )
            sweep_targets = []
        try:
            from app.services.file_service import MediaLibraryService

            allowed_directories: set[str] = set()
            if recovery_store is None or enumeration_failed:
                # 账本没读出来时只扫当前保存根，不确认任何记录。
                if download_dir in sweep_targets or self._startup_listing_accepts(download_dir):
                    stats["removed_count"] += MediaLibraryService.sweep_orphan_download_temp_artifacts(
                        [download_dir],
                        max_depth=2,
                    )
            else:
                if recovery_store.needs_legacy_sweep(download_dir):
                    self._run_legacy_startup_sweep(
                        recovery_store,
                        download_dir,
                        maintenance_root,
                        MediaLibraryService,
                        stats,
                        allowed_directories,
                    )
                elif self._record_startup_directory_sweep(
                    MediaLibraryService,
                    download_dir,
                    maintenance_root,
                    recovery_store,
                    recovery_batch,
                    recovery_owned=False,
                    stats=stats,
                ):
                    allowed_directories.add(self._normalize_cleanup_directory(recovery_store, download_dir))

                for directory in owned_directories:
                    if self._record_startup_directory_sweep(
                        MediaLibraryService,
                        directory,
                        maintenance_root,
                        recovery_store,
                        recovery_batch,
                        recovery_owned=True,
                        stats=stats,
                    ):
                        allowed_directories.add(directory)

            if (
                recovery_store is not None
                and recovery_batch is not None
                and not enumeration_failed
                and not hls_sweep_failed
            ):
                filtered = recovery_store.filter_recovery_batch(
                    recovery_batch,
                    allowed_directories=allowed_directories,
                )
                stats["recovery_records_consumed"] = recovery_store.consume_recovery_records(filtered)
            debug_logger.log(
                component="DownloadManager",
                action="download_temp_artifact_sweep",
                message="Processed stale download temp artifacts at application startup",
                status_code="DL_TEMP_SWEEP",
                details={"download_dir": download_dir, **stats},
            )
        except Exception as exc:
            debug_logger.log_exception(
                "DownloadManager",
                "download_temp_artifact_sweep_error",
                exc,
                details={"download_dir": download_dir},
            )
        return stats

    def _startup_cleanup_directories(self, recovery_batch: Any, maintenance_root: str) -> list[str]:
        """账本里位于当前保存根之外的旧路径不参与本次删除。"""
        if recovery_batch is None:
            return []
        selected: list[str] = []
        for directory in list(getattr(recovery_batch, "directories", ()) or ()):
            if not self._startup_directory_in_root(directory, maintenance_root):
                debug_logger.log(
                    component="DownloadManager",
                    action="startup_cleanup_skip_outside_root",
                    level="WARN",
                    message="Skipped a recovery directory outside the current save root",
                    status_code="DL_STARTUP_CLEANUP_SKIP_OUTSIDE",
                    details={"directory": str(directory)},
                )
                continue
            selected.append(str(directory))
        return selected

    @staticmethod
    def _startup_directory_in_root(directory: str, maintenance_root: str) -> bool:
        if is_drive_or_filesystem_root(directory):
            return False
        return is_within_save_root(directory, maintenance_root)

    @staticmethod
    def _startup_listing_accepts(directory: str) -> bool:
        _names, truncated, error = list_directory_entries(directory)
        return not truncated and not error

    def _bounded_startup_sweep_targets(self, directories: list[str], maintenance_root: str) -> list[str]:
        """先做有上限的 iterdir。截断、列举失败或盘符根都不交给无上限扫描。"""
        accepted: list[str] = []
        seen: set[str] = set()
        for directory in directories:
            key = os.path.normcase(os.path.abspath(os.path.expanduser(str(directory))))
            if key in seen:
                continue
            seen.add(key)
            if not self._startup_directory_in_root(directory, maintenance_root):
                continue
            names, truncated, error = list_directory_entries(directory)
            if truncated or error:
                self._log_startup_listing_degraded(directory, truncated=truncated, error=error, scanned=len(names))
                continue
            accepted.append(str(directory))
        return accepted

    @staticmethod
    def _log_startup_listing_degraded(
        directory: str,
        *,
        truncated: bool,
        error: str,
        scanned: int,
    ) -> None:
        debug_logger.log(
            component="DownloadManager",
            action="startup_directory_listing_degraded",
            level="WARN",
            message="Startup cleanup refused a truncated, failed, or rooted directory listing",
            status_code="DL_STARTUP_LISTING_DEGRADED",
            details={
                "directory": str(directory),
                "truncated": truncated,
                "error": error,
                "scanned_entries": scanned,
            },
        )

    def _normalize_cleanup_directory(self, recovery_store: Any, directory: str) -> str:
        normalize = getattr(recovery_store, "normalize_directory", None)
        if callable(normalize):
            try:
                return str(normalize(directory))
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        return os.path.normcase(os.path.abspath(os.path.expanduser(directory)))

    def _record_startup_directory_sweep(
        self,
        media_library: Any,
        directory: str,
        maintenance_root: str,
        recovery_store: Any,
        recovery_batch: Any,
        *,
        recovery_owned: bool,
        stats: dict[str, Any],
    ) -> bool:
        """扫完且没有截断或错误时，该目录才可以进入本次 consume。"""
        if not self._startup_directory_in_root(directory, maintenance_root):
            return False
        if not self._startup_listing_accepts(directory):
            return False
        if recovery_owned and recovery_batch is not None:
            checker = getattr(recovery_store, "directory_still_in_snapshot", None)
            if callable(checker) and not checker(directory, recovery_batch):
                return False
        result = media_library.sweep_orphan_download_temp_directory(
            directory,
            depth=0,
            max_depth=0,
        )
        stats["removed_count"] += int(getattr(result, "removed_count", 0) or 0)
        if getattr(result, "truncated", False) or getattr(result, "error", ""):
            debug_logger.log(
                component="DownloadManager",
                action="recovery_directory_scan_degraded",
                level="WARN",
                message="Recovery directory scan was truncated or failed; its ledger row stays for confirmation",
                status_code="DL_RECOVERY_SCAN_DEGRADED",
                details={
                    "directory": str(directory),
                    "truncated": bool(getattr(result, "truncated", False)),
                    "error": getattr(result, "error", ""),
                },
            )
            return False
        return True

    def _run_legacy_startup_sweep(
        self,
        recovery_store: Any,
        download_dir: str,
        maintenance_root: str,
        media_library: Any,
        stats: dict[str, Any],
        allowed_directories: set[str],
    ) -> None:
        recovery_store.prepare_legacy_sweep(download_dir)
        deadline = time.monotonic() + media_library.ORPHAN_SWEEP_TIME_BUDGET_SECONDS
        while time.monotonic() < deadline:
            frontier_item = recovery_store.next_legacy_sweep_directory(download_dir)
            if frontier_item is None:
                break
            directory, depth = frontier_item
            if not self._startup_directory_in_root(directory, maintenance_root):
                recovery_store.complete_legacy_sweep_directory(download_dir, directory, [])
                continue
            names, truncated, error = list_directory_entries(directory)
            if truncated or error:
                self._log_startup_listing_degraded(
                    directory,
                    truncated=truncated,
                    error=error,
                    scanned=len(names),
                )
                break
            result = media_library.sweep_orphan_download_temp_directory(
                directory,
                depth=depth,
                max_depth=2,
            )
            stats["removed_count"] += int(getattr(result, "removed_count", 0) or 0)
            stats["legacy_directories_scanned"] += 1
            if getattr(result, "truncated", False) or getattr(result, "error", ""):
                debug_logger.log(
                    component="DownloadManager",
                    action="legacy_directory_scan_degraded",
                    level="WARN",
                    message="A legacy directory scan was bounded or degraded",
                    status_code="DL_LEGACY_SCAN_DEGRADED",
                    details={
                        "directory": directory,
                        "truncated": result.truncated,
                        "error": result.error,
                        "scanned_entries": result.scanned_entries,
                    },
                )
                break
            recovery_store.complete_legacy_sweep_directory(
                download_dir,
                directory,
                result.children,
            )
            allowed_directories.add(self._normalize_cleanup_directory(recovery_store, directory))
        stats["legacy_scan_pending"] = (
            recovery_store.next_legacy_sweep_directory(download_dir) is not None
        )
