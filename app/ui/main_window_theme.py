"""把主窗口主题样式应用到壳层和已打开的页面。"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget


def apply_main_window_theme_stylesheet(
    window,
    *,
    apply_theme,
    config,
    refresh_shell_theme: bool = True,
    sync_settings_theme: bool = True,
    refresh_frontend_snapshot: bool = False,
    update_theme_icon: bool = True,
    freeze_updates: bool = False,
) -> None:
    window._debug_shell_visibility("before_theme_apply")
    window.__dict__["_last_theme_refresh_frontend_snapshot"] = bool(refresh_frontend_snapshot)
    freeze_states: list[tuple[QWidget, bool]] = []
    if freeze_updates and refresh_shell_theme:
        for widget in (window.__dict__.get("app_shell"),):
            if widget is None:
                continue
            try:
                if not widget.isVisible():
                    continue
                was_enabled = bool(widget.updatesEnabled())
                freeze_states.append((widget, was_enabled))
                if was_enabled:
                    widget.setUpdatesEnabled(False)
            except RuntimeError:
                continue
    try:
        if refresh_shell_theme:
            window._close_transient_popups_before_theme()
        window._apply_root_background()
        apply_theme(window.is_dark_theme)
        window._apply_root_background()
        window_chrome = window.__dict__.get("window_chrome")
        if window_chrome is not None:
            window_chrome.apply_theme(window.is_dark_theme)
        title_bar = window.__dict__.get("window_title_bar")
        if title_bar is not None and window_chrome is None:
            title_bar.apply_theme(window.is_dark_theme)
        top_bar = window.__dict__.get("top_bar")
        if top_bar is not None and refresh_shell_theme and update_theme_icon:
            top_bar.set_theme_icon(window.is_dark_theme)
        app_shell = window.__dict__.get("app_shell")
        if app_shell is not None and refresh_shell_theme:
            app_shell.apply_theme(window.is_dark_theme)
        if app_shell is not None and sync_settings_theme:
            settings_page = getattr(app_shell, "pages", {}).get("settings")
            sync_theme = getattr(settings_page, "sync_external_theme", None)
            if callable(sync_theme):
                sync_theme(
                    window.is_dark_theme,
                    follow_system=bool(config.get("appearance", "follow_system", False)),
                )
        window.__dict__["_last_applied_theme_is_dark"] = bool(window.is_dark_theme)
        window.__dict__["_theme_apply_refreshed_snapshot"] = False
        if refresh_frontend_snapshot and "_frontend_state_service" in window.__dict__:
            window._refresh_frontend_after_theme_change()
            window.__dict__["_theme_apply_refreshed_snapshot"] = True
        if not window.__dict__.get("_qt_initialized", False):
            return
    finally:
        for widget, was_enabled in reversed(freeze_states):
            try:
                widget.setUpdatesEnabled(was_enabled)
                if was_enabled:
                    widget.update()
            except RuntimeError:
                continue
        if window.__dict__.get("_qt_initialized", False):
            window._ensure_shell_chrome_visible(reason="theme_apply_finally")
            window._repair_black_shell_if_needed("theme_apply_finally")
            if not window.__dict__.get("_theme_apply_refreshed_snapshot", False):
                window._finalize_theme_repaint()
            window._debug_shell_visibility("after_theme_apply")
