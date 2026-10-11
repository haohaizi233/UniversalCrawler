"""Atomic handoff from a verified desktop update to the installer helper."""

from __future__ import annotations

import threading

from collections.abc import Callable
from typing import Any

from app.services.update_check_service import PreparedUpdate
from app.ui.main_window_update_types import _PreparedUpdate


def install_prepared_update(
    host: Any,
    *,
    launch: Callable[..., None],
    schedule_close: Callable[[int, Callable[[], None]], None],
) -> None:
    lock = host.__dict__.get("_update_download_lock")
    if lock is None:
        lock = threading.RLock()
        host._update_download_lock = lock
    with lock:
        if bool(host.__dict__.get("_update_install_in_progress", False)):
            return
        prepared = host.__dict__.get("_prepared_update")
        if isinstance(prepared, _PreparedUpdate):
            sequence = int(host.__dict__.get("_update_download_sequence", 0) or 0)
            host._prepared_update = None
            host._update_install_in_progress = True
            host._update_install_claim_sequence = sequence
    if not isinstance(prepared, _PreparedUpdate):
        host._show_update_check_error("更新安装包尚未准备好。")
        return

    dialog = host.__dict__.get("_update_download_dialog")
    _call_dialog(dialog, "set_installing")
    try:
        launch(
            PreparedUpdate(
                installer_path=prepared.installer_path,
                manifest_path=prepared.manifest_path,
                signature_path=prepared.signature_path,
                version=prepared.version,
                log_path=prepared.log_path,
                release_revision=prepared.release_revision,
                revision_authorization_path=prepared.revision_authorization_path,
                revision_authorization_token=prepared.revision_authorization_token,
            ),
            restart_argv=host._restart_argv_after_update(),
        )
    except BaseException as exc:
        restored = _restore_claim(host, prepared, sequence, lock)
        if restored:
            _call_dialog(dialog, "set_ready", prepared.installer_path)
        if not isinstance(exc, Exception):
            raise
        _call_host_safely(
            host,
            "_show_update_check_error",
            "更新安装程序启动失败，请重试。",
        )
        _call_host_safely(
            host,
            "append_log",
            "更新安装程序启动失败",
            level="ERROR",
        )
        return

    with lock:
        if host.__dict__.get("_update_install_claim_sequence") == sequence:
            host._update_install_claim_sequence = None
    _schedule_close_safely(host, schedule_close)
    _call_host_safely(host, "append_log", "更新安装程序已启动，应用即将退出。")


def _restore_claim(host: Any, prepared: _PreparedUpdate, sequence: int, lock: Any) -> bool:
    restored = False
    with lock:
        claim_sequence = host.__dict__.get("_update_install_claim_sequence")
        current_sequence = int(host.__dict__.get("_update_download_sequence", 0) or 0)
        if claim_sequence == sequence:
            if current_sequence == sequence:
                host._prepared_update = prepared
                restored = True
            host._update_install_in_progress = False
            host._update_install_claim_sequence = None
    return restored


def _call_dialog(dialog: Any, method_name: str, *args: object) -> None:
    try:
        callback = getattr(dialog, method_name, None)
    except BaseException:
        return
    if not callable(callback):
        return
    try:
        callback(*args)
    except BaseException:
        pass


def _call_host_safely(host: Any, method_name: str, *args: object, **kwargs: object) -> None:
    try:
        callback = getattr(host, method_name, None)
    except BaseException:
        return
    if not callable(callback):
        return
    try:
        callback(*args, **kwargs)
    except BaseException:
        return


def _schedule_close_safely(
    host: Any,
    schedule_close: Callable[[int, Callable[[], None]], None],
) -> None:
    try:
        close = getattr(host, "close", None)
    except BaseException:
        return
    if not callable(close):
        return
    try:
        schedule_close(250, close)
        return
    except BaseException:
        pass
    try:
        close()
    except BaseException:
        return


__all__ = ["install_prepared_update"]
