"""提供 PyQt6 shell 的主题样式工具。"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import QApplication, QDialog, QMainWindow, QPlainTextEdit, QTableView, QTableWidget, QWidget

from app.services.icon_registry import resolve_ui_icon_path

from .log_center_styles import generate_log_center_stylesheet as _generate_log_center_stylesheet

from .theme_palette import (
    _configured_appearance_value,
    _preferred_ui_font_family,
    _scaled_pixel_size,
    _scaled_point_size,
    build_palette,
    theme_colors,
)


def apply_opaque_fullscreen_theme(widget: QWidget, is_dark: bool) -> None:
    """全屏媒体窗使用不透明背景，避免整份应用样式表把它刷成透明。"""

    resolved = bool(is_dark)
    apply_dialog_theme(widget, is_dark=resolved)
    colors = theme_colors(resolved)
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    widget.setAutoFillBackground(True)
    palette = widget.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(colors["video_bg"]))
    widget.setPalette(palette)
    opaque = f"QWidget#MediaFullscreenWindow {{ background-color: {colors['video_bg']}; }}"
    current = widget.styleSheet()
    if colors["video_bg"] not in current:
        widget.setStyleSheet(f"{current}\n{opaque}".strip())


def _apply_preserved_window_theme(widget: QWidget, is_dark: bool) -> bool:
    """已打开的无边框 chrome 对话框和全屏窗不套用整份应用样式表。"""

    try:
        if str(widget.objectName() or "") == "MediaFullscreenWindow":
            apply_opaque_fullscreen_theme(widget, is_dark)
            return True
        apply_chrome = getattr(widget, "apply_chrome_theme", None)
        if callable(apply_chrome):
            apply_chrome(bool(is_dark))
            return True
    except RuntimeError:
        return True
    return False


def apply_application_theme(is_dark: bool, *, font_size: str | None = None) -> None:
    """在 QApplication 层应用样式表与 palette。"""
    app = QApplication.instance()
    if app is None:
        return
    roots = _theme_style_roots(app)
    update_states: list[tuple[QWidget, bool]] = []
    for widget in roots:
        try:
            was_enabled = widget.updatesEnabled()
            update_states.append((widget, was_enabled))
            if was_enabled:
                widget.setUpdatesEnabled(False)
        except RuntimeError:
            continue
    configured_font_size = str(font_size or _configured_appearance_value("font_size", "medium")).strip().lower()
    configured_scale = _configured_appearance_value("scale", "100%")
    font_family = _preferred_ui_font_family()
    font_point_size = _scaled_point_size(configured_font_size, configured_scale)
    try:
        font_key = f"{font_family}:{font_point_size}"
        if app.property("_ucp_font_key") != font_key:
            app.setFont(QFont(font_family, font_point_size))
            app.setProperty("_ucp_font_key", font_key)
        app.setProperty("ui_scale", configured_scale)
        app.setProperty("ui_font_size", configured_font_size)
        app.setProperty("is_dark_theme", bool(is_dark))
        palette = build_palette(is_dark)
        stylesheet = generate_stylesheet(is_dark, font_size=configured_font_size, scale=configured_scale)
        theme_key = f"{int(bool(is_dark))}:{configured_font_size}:{configured_scale}"
        for widget in roots:
            try:
                if _apply_preserved_window_theme(widget, is_dark):
                    widget.setProperty("_ucp_theme_key", theme_key)
                    continue
                widget.setPalette(palette)
                widget.setAutoFillBackground(True)
                if widget.property("_ucp_theme_key") != theme_key or widget.styleSheet() != stylesheet:
                    widget.setProperty("_ucp_theme_key", theme_key)
                    widget.setStyleSheet(stylesheet)
            except RuntimeError:
                continue
    finally:
        for widget, was_enabled in reversed(update_states):
            try:
                widget.setUpdatesEnabled(was_enabled)
                if was_enabled:
                    widget.update()
            except RuntimeError:
                continue

def _theme_style_roots(app: QApplication) -> list[QWidget]:
    roots: list[QWidget] = []
    for widget in app.topLevelWidgets():
        try:
            if widget.windowType() == Qt.WindowType.Popup:
                continue
            is_registered_theme_root = bool(widget.property("ucpThemeRoot"))
            if not isinstance(widget, (QMainWindow, QDialog)) and not is_registered_theme_root:
                continue
            roots.append(widget)
        except RuntimeError:
            continue
    return roots


def polish_data_views(root: QWidget | None, is_dark: bool) -> None:
    """强制 item view 与日志面板采用当前 palette。"""
    if root is None:
        return
    palette = build_palette(is_dark)
    for view in root.findChildren(QTableView):
        view.setPalette(palette)
        header = view.horizontalHeader()
        if header is not None:
            header.setPalette(palette)
        viewport = view.viewport()
        if viewport is not None:
            viewport.setPalette(palette)
            viewport.setAutoFillBackground(True)
        view.setAutoFillBackground(True)
    for table in root.findChildren(QTableWidget):
        table.setPalette(palette)
        header = table.horizontalHeader()
        if header is not None:
            header.setPalette(palette)
        viewport = table.viewport()
        if viewport is not None:
            viewport.setPalette(palette)
            viewport.setAutoFillBackground(True)
        table.setAutoFillBackground(True)
    for editor in root.findChildren(QPlainTextEdit):
        editor.setPalette(palette)
        editor.setAutoFillBackground(True)

def apply_dialog_theme(widget: QWidget, *, parent: QWidget | None = None, is_dark: bool | None = None) -> bool:
    """为对话框应用 palette，同时不覆盖应用级样式表。"""
    resolved = resolve_is_dark_theme(parent) if is_dark is None else bool(is_dark)
    widget.setPalette(build_palette(resolved))
    parent_stylesheet = ""
    if parent is not None:
        try:
            parent_stylesheet = parent.styleSheet()
        except RuntimeError:
            parent_stylesheet = ""
    stylesheet = parent_stylesheet or generate_stylesheet(resolved)
    if widget.styleSheet() != stylesheet:
        widget.setStyleSheet(stylesheet)
    polish_data_views(widget, resolved)
    return resolved

def resolve_is_dark_theme(parent: QWidget | None = None) -> bool:
    current = parent
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        theme = getattr(current, "is_dark_theme", None)
        if theme is not None:
            return bool(theme)
        window = current.window()
        if window is not None and window is not current:
            current = window
            continue
        current = current.parentWidget()
    app = QApplication.instance()
    if app is not None:
        theme = app.property("is_dark_theme")
        if theme is not None:
            return bool(theme)
    from app.config import cfg

    return bool(cfg.get("common", "dark_theme", cfg.get("common", "theme", "light") == "dark"))


def generate_settings_stylesheet(is_dark: bool = False) -> str:
    """页面局部样式未生效时，为 SettingsPage 滚动区提供回退 QSS。"""
    c = theme_colors(is_dark)
    return f"""
QScrollArea#SettingsPlatformScroll QScrollBar:horizontal {{
    height: 8px;
    background: transparent;
}}
QScrollArea#SettingsPlatformScroll QScrollBar::handle:horizontal {{
    background: {c["scrollbar_handle"]};
    border-radius: 4px;
    min-width: 24px;
}}
"""


def generate_log_center_stylesheet(is_dark: bool = False) -> str:
    """为 LogCenterPage 及其具名子控件生成主题感知 QSS。"""
    return _generate_log_center_stylesheet(theme_colors(is_dark))

def _status_success_icon_url() -> str:
    resolved = resolve_ui_icon_path("status_success.png")
    if resolved is not None:
        return resolved.as_posix()
    return "UI/icon/status_success.png"


def generate_stylesheet(is_dark: bool = False, *, font_size: str | None = None, scale: str | None = None) -> str:
    """返回应用样式表。

    产品默认使用浅色以匹配基准 UI；深色模式仍由用户显式选择。
    """

    c = theme_colors(is_dark)
    configured_font_size = str(font_size or _configured_appearance_value("font_size", "medium")).strip().lower()
    configured_scale = str(scale or _configured_appearance_value("scale", "100%")).strip()
    base_font_px = _scaled_pixel_size(configured_font_size, configured_scale)
    nav_checked_border = c["accent"] if not is_dark else c["accent"]
    check_icon_url = _status_success_icon_url()
    return f"""
QMainWindow {{
    background-color: {c["bg"]};
    color: {c["text"]};
    font-family: 'Microsoft YaHei UI', 'Microsoft YaHei', 'SimSun', 'Segoe UI', sans-serif;
    font-size: {base_font_px}px;
}}

QDialog {{
    background-color: {c["bg"]};
    color: {c["text"]};
}}

QWidget {{
    background-color: transparent;
    color: {c["text"]};
    font-family: 'Microsoft YaHei UI', 'Microsoft YaHei', 'SimSun', 'Segoe UI', sans-serif;
    font-size: {base_font_px}px;
}}

QLabel {{
    background-color: transparent;
}}

QAbstractScrollArea::viewport {{
    background-color: {c["panel"]};
    color: {c["text"]};
}}

QWidget#AppShell {{
    background-color: {c["bg"]};
}}

QFrame#IslandCard,
QFrame#PlatformIsland,
QFrame#NavIsland,
QFrame#ControlIsland,
QFrame#ContentIsland,
QFrame#StatusIsland,
QFrame#QueueTableIsland,
QFrame#ActivityIsland,
QFrame#PageIsland {{
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    border-radius: 12px;
}}

QFrame#TopBar {{
    background-color: transparent;
    border: none;
}}

QFrame#TopBarInner {{
    background: transparent;
    border: none;
}}

QFrame#ContentPanel, QFrame#PagePanel {{
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    border-radius: 10px;
}}

QFrame#PageFrame, QFrame#Sidebar {{
    background: transparent;
    border: none;
}}

QFrame#NavSeparator {{
    color: {c["border"]};
    background-color: {c["border"]};
    border: none;
    margin: 6px 8px;
    max-height: 1px;
}}

QStackedWidget, QStackedWidget#PageStack {{
    background: transparent;
    border: none;
}}

QWidget#AppBody {{
    background: transparent;
}}

QLabel#PageTitle {{
    color: {c["text"]};
    font-size: 21px;
    font-weight: 700;
}}

QFrame#ActiveTableCard,
QFrame#ActiveDetailCard,
QFrame#ActiveEventsCard,
QFrame#CompletedTableCard,
QFrame#CompletedPreviewCard,
QFrame#CompletedInfoCard,
QFrame#FailedTableCard,
QFrame#FailedDetailCard,
QFrame#FailedSolutionsCard {{
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
}}

QFrame#ActiveDetailCard QLabel,
QFrame#ActiveEventsCard QLabel,
QFrame#CompletedInfoCard QLabel,
QFrame#FailedDetailCard QLabel,
QFrame#FailedSolutionsCard QLabel {{
    background: transparent;
    font-size: 13px;
}}

QLabel#SmartWrapLabel,
QLabel#LinkValueLabel {{
}}
QFrame#QueueControlPanel {{
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
}}

QLabel#SectionTitle {{
    color: {c["text"]};
    font-size: 15px;
    font-weight: 700;
}}

QLabel#LinkValueLabel {{
    color: {c["accent"]};
}}

QPushButton#ActiveControlButton {{
    min-height: 40px;
    min-width: 110px;
    background-color: {c["panel_soft"]};
    border: 1px solid {c["border"]};
    border-radius: 7px;
    color: {c["text"]};
    font-weight: 600;
    padding: 0px 14px;
}}

QPushButton#ActiveControlButton:hover {{
    background-color: {c["accent_soft"]};
    border-color: {c["accent"]};
    color: {c["accent"]};
}}

QTableView#ActiveDownloadsTable {{
    border: none;
    border-radius: 0px;
    background-color: {c["panel"]};
}}

QTableView#ActiveDownloadsTable::item {{
    padding-left: 0px;
    padding-right: 0px;
}}

QTableView#ActiveDownloadsTable QHeaderView::section {{
    padding-left: 10px;
    padding-right: 10px;
}}

QTableView#CompletedItemsTable {{
    border: none;
    border-radius: 0px;
    background-color: {c["panel"]};
}}

QTableView#CompletedItemsTable QHeaderView::section {{
    padding-left: 12px;
    padding-right: 10px;
}}

QLabel#MutedLabel {{
    color: {c["muted"]};
    background: transparent;
}}

QLabel#PathLabel {{
    color: {c["accent"]};
    font-family: 'Consolas', 'Cascadia Code', monospace;
    font-weight: 600;
}}

QLabel#SelectionDialogHeader {{
    color: {c["text"]};
    font-size: 14px;
    font-weight: 700;
    background: transparent;
}}

QTableWidget#SelectionTable {{
    border: none;
    background-color: {c["panel"]};
}}

QTableWidget#SelectionTable::item {{
    padding-top: 2px;
    padding-bottom: 2px;
    padding-left: 8px;
    padding-right: 8px;
    border-bottom: 1px solid {c["border"]};
}}

QTableWidget#SelectionTable::item:selected {{
    background-color: {c["row_selected"]};
    color: {c["text"]};
}}

QTableWidget#SelectionTable::item:selected:active {{
    background-color: {c["row_selected"]};
    color: {c["text"]};
}}

QTableWidget#SelectionTable QHeaderView::section {{
    padding-top: 6px;
    padding-bottom: 6px;
}}

QLineEdit, QComboBox, QSpinBox {{
    min-height: 36px;
    border: 1px solid {c["border_strong"]};
    border-radius: 7px;
    background-color: {c["input"]};
    color: {c["text"]};
    padding: 0px 10px;
    selection-background-color: {c["accent"]};
    selection-color: #ffffff;
}}

QComboBox QAbstractItemView {{
    background-color: {c["panel"]};
    color: {c["text"]};
    border: 2px solid {c["accent"]};
    border-radius: 7px;
    selection-background-color: {c["accent"]};
    selection-color: #ffffff;
    padding: 0px;
}}

QComboBox QAbstractItemView::item {{
    min-height: 32px;
    padding: 5px 9px;
}}

QComboBox QAbstractItemView::item:selected {{
    background-color: transparent;
    color: #ffffff;
}}

QComboBox QAbstractItemView::item:selected:hover {{
    background-color: transparent;
    color: #ffffff;
}}

QComboBox QAbstractItemView::item:hover {{
    background-color: transparent;
    color: {c["accent"]};
}}

QComboBox::drop-down {{
    border: none;
    background: transparent;
}}

QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border: 2px solid {c["accent"]};
    background-color: {c["input"]};
}}

QComboBox:on,
QComboBox:focus:on {{
    border: 2px solid {c["accent"]};
    background-color: {c["input"]};
    color: {c["text"]};
}}

QCheckBox {{
    spacing: 8px;
    color: {c["text"]};
    background: transparent;
}}

QCheckBox:disabled {{
    color: {c["muted"]};
}}

QCheckBox#ActiveAutoRetryCheck {{
    min-height: 34px;
    padding-left: 10px;
    padding-right: 12px;
    border: 1px solid {c["border"]};
    border-radius: 7px;
    background-color: {c["panel_soft"]};
    font-weight: 600;
}}

QCheckBox#ActiveAutoRetryCheck:hover {{
    border-color: {c["accent"]};
    background-color: {c["accent_soft"]};
}}

QCheckBox#ActiveAutoRetryCheck::indicator {{
    width: 20px;
    height: 20px;
    border-radius: 6px;
    border: 2px solid {c["border_strong"]};
    background-color: {c["input"]};
}}

QCheckBox#ActiveAutoRetryCheck::indicator:checked {{
    background-color: {c["accent"]};
    border-color: {c["accent"]};
    image: url("{check_icon_url}");
}}

QCheckBox#ActiveAutoRetryCheck::indicator:hover {{
    border-color: {c["accent"]};
}}

WideHitCheckBox#ActiveAutoRetryCheck {{
    min-height: 34px;
    padding-left: 10px;
    padding-right: 12px;
    border: 1px solid {c["border"]};
    border-radius: 7px;
    background-color: {c["panel_soft"]};
    color: {c["text"]};
    font-weight: 600;
}}

WideHitCheckBox#ActiveAutoRetryCheck:hover {{
    border-color: {c["accent"]};
    background-color: {c["accent_soft"]};
}}

WideHitCheckBox#ActiveAutoRetryCheck::indicator {{
    width: 20px;
    height: 20px;
    border-radius: 6px;
    border: 2px solid {c["border_strong"]};
    background-color: {c["input"]};
}}

WideHitCheckBox#ActiveAutoRetryCheck::indicator:checked {{
    background-color: {c["accent"]};
    border-color: {c["accent"]};
    image: url("{check_icon_url}");
}}

QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 5px;
    border: 2px solid {c["border_strong"]};
    background-color: {c["input"]};
}}

QCheckBox::indicator:hover {{
    border-color: {c["accent"]};
    background-color: {c["panel_soft"]};
}}

QCheckBox::indicator:checked {{
    background-color: {c["accent"]};
    border-color: {c["accent"]};
    image: url("{check_icon_url}");
}}

QCheckBox::indicator:checked:hover {{
    background-color: {c["accent_hover"]};
    border-color: {c["accent_hover"]};
}}

QCheckBox::indicator:disabled {{
    background-color: {c["panel_soft"]};
    border-color: {c["border"]};
}}

QCheckBox::indicator:checked:disabled {{
    background-color: {c["border_strong"]};
    border-color: {c["border_strong"]};
}}

QPushButton#SelectionActionBtn {{
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    border-radius: 7px;
    color: {c["text"]};
    font-weight: 600;
}}

QPushButton#SelectionActionBtn:hover {{
    border-color: {c["accent"]};
    color: {c["accent"]};
    background-color: {c["panel_soft"]};
}}

QPushButton#SelectionActionBtn:pressed {{
    background-color: {c["accent_soft"]};
    border-color: {c["accent"]};
    padding-top: 1px;
}}

QPushButton {{
    min-height: 34px;
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    border-radius: 7px;
    color: {c["text"]};
    padding: 0px 13px;
}}

QPushButton:hover {{
    background-color: {c["panel_soft"]};
    border-color: {c["border_strong"]};
}}

QPushButton:pressed {{
    background-color: {c["accent_soft"]};
}}

QPushButton:disabled {{
    color: {c["muted"]};
    background-color: {c["panel_soft"]};
    border-color: {c["border"]};
}}

QPushButton#PrimaryBtn {{
    background-color: {c["accent"]};
    border: 1px solid {c["accent"]};
    color: white;
    font-weight: 700;
}}

QPushButton#PrimaryBtn:hover:enabled {{
    background-color: {c["accent_hover"]};
    border-color: {c["accent_hover"]};
}}

QPushButton#PrimaryBtn:pressed {{
    background-color: {c["accent_hover"]};
    border-color: {c["accent_hover"]};
    padding-top: 2px;
    padding-bottom: 0px;
}}

QPushButton#StartTaskBtn {{
    background-color: {c["accent"]};
    border: 1px solid {c["accent"]};
    color: white;
    font-weight: 700;
}}

QPushButton#StartTaskBtn:hover:enabled {{
    background-color: {c["accent_hover"]};
    border-color: {c["accent_hover"]};
}}

QPushButton#StartTaskBtn:pressed:enabled {{
    background-color: {c["accent_hover"]};
    border-color: {c["accent_hover"]};
}}

QPushButton#StartTaskBtn:disabled {{
    color: rgba(255, 255, 255, 0.72);
    background-color: {c["border_strong"]};
    border-color: {c["border_strong"]};
}}

QPushButton#StartTaskBtn[running="true"] {{
    background-color: {c["accent"]};
    border: 1px solid {c["accent"]};
    color: white;
    font-weight: 700;
}}

QPushButton#StartTaskBtn[running="true"]:disabled {{
    background-color: {c["accent"]};
    border-color: {c["accent"]};
    color: white;
}}

QPushButton#DangerBtn {{
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    color: {c["text"]};
    font-weight: 600;
}}

QPushButton#DangerBtn:hover:enabled {{
    color: {c["danger"]};
    border-color: {c["danger"]};
    background-color: {c["panel_soft"]};
}}

QPushButton#DangerBtn:pressed:enabled {{
    color: {c["danger"]};
    border-color: {c["danger"]};
    background-color: {c["accent_soft"]};
    padding-top: 2px;
    padding-bottom: 0px;
}}

QPushButton#StopTaskBtn {{
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    color: {c["text"]};
    font-weight: 600;
}}

QPushButton#StopTaskBtn:hover:enabled {{
    color: {c["danger"]};
    border-color: {c["danger"]};
    background-color: {c["panel_soft"]};
}}

QPushButton#StopTaskBtn:pressed:enabled {{
    color: {c["danger_hover"]};
    border-color: {c["danger"]};
    background-color: {c["accent_soft"]};
    padding-top: 2px;
    padding-bottom: 0px;
}}

QPushButton#StopTaskBtn:disabled {{
    color: {c["muted"]};
    background-color: {c["panel_soft"]};
    border-color: {c["border"]};
}}

QPushButton#DirBtn {{
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    color: {c["text"]};
    text-align: left;
}}

QPushButton#DirBtn:hover {{
    color: {c["accent"]};
    border-color: {c["accent"]};
}}

QPushButton#ThemeBtn {{
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    border-radius: 18px;
    color: {c["text"]};
    font-weight: 700;
    min-width: 52px;
    padding: 0px;
}}

QPushButton#ThemeBtn:hover {{
    background-color: {c["panel_soft"]};
    border-color: {c["border_strong"]};
}}

QPushButton#TableActionButton {{
    min-width: 30px;
    max-width: 32px;
    min-height: 26px;
    max-height: 28px;
    border-radius: 6px;
    padding: 0px;
    background-color: {c["panel"]};
}}

QPushButton#TableActionButton:hover {{
    background-color: {c["accent_soft"]};
    border-color: {c["accent"]};
}}

QToolButton#ToolCardButton {{
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
    padding: 10px;
    color: {c["text"]};
    text-align: left;
}}

QToolButton#ToolCardButton:hover {{
    background-color: {c["panel_soft"]};
    border-color: {c["accent"]};
}}

QToolButton#ToolCardButton:checked {{
    background-color: {c["accent_soft"]};
    border-color: {c["accent"]};
    color: {c["accent"]};
    font-weight: 700;
}}

QPushButton#NavButton {{
    min-height: 40px;
    border: none;
    border-radius: 8px;
    background-color: transparent;
    color: {c["text"]};
    text-align: left;
    padding-left: 12px;
    padding-right: 10px;
}}

QFrame#NavItem {{
    border: none;
    border-radius: 8px;
    background-color: transparent;
}}

QLabel#NavTitle {{
    color: {c["text"]};
    font-weight: 600;
    background: transparent;
}}

QFrame#NavItem[active="true"] QLabel#NavTitle {{
    color: {c["accent"]};
    font-weight: 700;
}}

QLabel#NavIcon {{
    background: transparent;
}}

QFrame#StatusBar {{
    background-color: transparent;
    border: none;
}}

QLabel#StatusDot {{
    border-radius: 5px;
    min-width: 10px;
    max-width: 10px;
    min-height: 10px;
    max-height: 10px;
    background-color: {c["muted"]};
}}

QLabel#StatusDot[state="idle"] {{
    background-color: {c["muted"]};
}}

QLabel#StatusDot[state="running"] {{
    background-color: #22c55e;
}}

QLabel#StatusDot[state="error"] {{
    background-color: #ef4444;
}}

QLabel#StatusMetricCaption {{
    color: {c["muted"]};
    font-weight: 600;
    background: transparent;
}}

QLabel#StatusMetricValue {{
    color: {c["text"]};
    font-weight: 600;
    background: transparent;
    font-family: "Cascadia Mono", "Consolas", "Courier New", monospace;
}}

QLabel#StatusMetric {{
    color: {c["text"]};
    font-weight: 600;
    background: transparent;
}}

QPushButton#StatusVersionButton {{
    background-color: transparent;
    border: none;
    color: {c["muted"]};
    padding: 0px;
    text-align: right;
}}

QPushButton#StatusVersionButton:hover,
QPushButton#StatusVersionButton:focus {{
    color: {c["accent"]};
}}

QPushButton#StatusVersionButton:disabled {{
    color: {c["muted"]};
}}

QPushButton#StatusHelpBtn {{
    border: none;
    background: transparent;
    padding: 0px;
}}

QPushButton#StatusHelpBtn:hover {{
    background-color: {c["panel_soft"]};
    border-radius: 6px;
}}

QLabel#EventFeedBody {{
    color: {c["text"]};
    padding: 2px 0px;
}}

QPushButton#ToolbarIconBtn {{
    background-color: transparent;
    border: 1px solid {c["border"]};
    border-radius: 8px;
    padding: 0px;
    min-width: 36px;
    max-width: 36px;
    min-height: 36px;
    max-height: 36px;
}}

QPushButton#ToolbarIconBtn:hover {{
    border-color: {c["accent"]};
    background-color: {c["accent_soft"]};
}}

QPushButton#ToolbarRefreshBtn {{
    background-color: transparent;
    border: 1px solid {c["border"]};
    border-radius: 8px;
    color: {c["text"]};
    padding: 4px 12px 4px 8px;
    font-weight: 600;
}}

QPushButton#ToolbarRefreshBtn:hover {{
    border-color: {c["accent"]};
    color: {c["accent"]};
}}

QTableView {{
    background-color: {c["panel"]};
    color: {c["text"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
    gridline-color: transparent;
    selection-background-color: {c["row_selected"]};
    selection-color: {c["text"]};
    alternate-background-color: {c["row_alt"]};
}}

QTableView::item {{
    padding-left: 8px;
    padding-right: 8px;
    border-bottom: 1px solid {c["border"]};
    background-color: transparent;
    color: {c["text"]};
}}

QTableView::item:alternate {{
    background-color: {c["row_alt"]};
}}

QTableView::item:selected {{
    color: {c["text"]};
    background-color: {c["row_selected"]};
}}

QTableView::item:selected:active {{
    color: {c["text"]};
    background-color: {c["row_selected"]};
}}

QTableView::item:selected:hover {{
    color: {c["text"]};
    background-color: {c["row_selected"]};
}}

QTableView#CompletedItemsTable::item:selected,
QTableView#CompletedItemsTable::item:selected:active,
QTableView#FailedItemsTable::item:selected,
QTableView#FailedItemsTable::item:selected:active {{
    color: {c["text"]};
    background-color: transparent;
}}

QFrame#FailedDetailCard,
QFrame#FailedSolutionsCard,
QFrame#FailedTableCard {{
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    border-radius: 10px;
}}

{generate_settings_stylesheet(is_dark)}

{generate_log_center_stylesheet(is_dark)}

QLabel#FailedDetailKey {{
    color: {c["muted"]};
    font-size: 13px;
}}

QWidget#FailedDetailValueText,
QLabel#FailedDetailValueText {{
    color: {c["text"]};
    font-size: 13px;
}}

QFrame#FailedSolutionRow {{
    background-color: {c["panel_soft"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
}}

QFrame#FailedLogRow {{
    background: transparent;
    border: none;
    border-radius: 0;
}}

QLabel#FailedLogTime {{
    color: {c["muted"]};
    font-family: Consolas, 'Microsoft YaHei UI', monospace;
    font-size: 12px;
}}

QLabel#FailedLogLevel {{
    color: {c["accent"]};
    font-family: Consolas, 'Microsoft YaHei UI', monospace;
    font-size: 12px;
    font-weight: 700;
}}

QLabel#FailedLogMessage {{
    color: {c["text"]};
    font-size: 12px;
}}

QLabel#FailedSolutionTitle {{
    color: {c["text"]};
    font-weight: 700;
}}

QLabel#FailedSolutionDescription {{
    color: {c["muted"]};
    font-size: 12px;
}}

QScrollArea#FailedLogExcerptScroll {{
    background: transparent;
    border: none;
}}

QTableView::item:hover {{
    background-color: transparent;
    color: {c["text"]};
}}

QTableView::item:alternate:hover {{
    background-color: {c["row_alt"]};
}}

QTableView::item:focus {{
}}

QPushButton#NavButton:hover {{
    background-color: {c["panel_soft"]};
}}

QPushButton#NavButton:checked {{
    background-color: {c["accent_soft"]};
    color: {c["accent"]};
    border-left: 3px solid {nav_checked_border};
    font-weight: 700;
}}

QTableWidget {{
    background-color: {c["panel"]};
    alternate-background-color: {c["row_alt"]};
    border: 1px solid {c["border"]};
    border-radius: 9px;
    gridline-color: {c["border"]};
    selection-background-color: {c["row_selected"]};
    selection-color: {c["text"]};
}}

QTableWidget::item {{
    padding-left: 10px;
    padding-right: 10px;
    border-bottom: 1px solid {c["border"]};
    background-color: transparent;
    color: {c["text"]};
}}

QTableWidget::item:alternate {{
    background-color: {c["row_alt"]};
}}

QTableWidget::item:selected {{
    color: {c["text"]};
    background-color: {c["row_selected"]};
}}

QTableWidget::item:selected:active {{
    background-color: {c["row_selected"]};
    color: {c["text"]};
}}

QTableWidget::item:selected:hover {{
    background-color: {c["row_selected"]};
    color: {c["text"]};
}}

QTableWidget::item:hover {{
    background-color: transparent;
    color: {c["text"]};
}}

QTableWidget::item:alternate:hover {{
    background-color: {c["row_alt"]};
}}

QTableWidget::item:focus {{
}}

QHeaderView::section {{
    background-color: {c["panel_soft"]};
    color: {c["text"]};
    padding: 9px 10px;
    border: none;
    border-bottom: 1px solid {c["border"]};
    font-weight: 700;
}}

QHeaderView {{
    background-color: transparent;
}}

QProgressBar {{
    min-height: 8px;
    max-height: 8px;
    border: none;
    border-radius: 4px;
    background-color: {c["border"]};
    text-align: center;
    color: transparent;
}}

QProgressBar::chunk {{
    background-color: {c["accent"]};
    border-radius: 4px;
}}

QGroupBox {{
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    border-radius: 10px;
    margin-top: 12px;
    padding: 18px 12px 12px 12px;
    font-weight: 700;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0px 6px;
    color: {c["text"]};
}}

QScrollArea {{
    border: none;
    background-color: transparent;
}}

QFrame#VideoContainer {{
    background-color: {c["video_bg"]};
    border-radius: 10px;
}}

QGraphicsView#VideoSurface,
QVideoWidget,
QVideoWidget#VideoSurface {{
    background-color: transparent;
}}

QWidget#MediaFullscreenWindow {{
    background-color: {c["video_bg"]};
}}

QLabel#ImageLabel {{
    background-color: {c["video_bg"]};
    border-radius: 10px;
}}

QPlainTextEdit {{
    background-color: {c["log_bg"]};
    color: {c["text"]};
    border: 1px solid {c["border"]};
    border-radius: 8px;
}}

QLabel#TimeLabel {{
    color: {c["muted"]};
    font-family: 'Consolas', 'Cascadia Code', monospace;
    font-size: 12px;
}}

QPushButton#PlayBtn, QPushButton#FullscreenBtn {{
    border-radius: 7px;
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    color: {c["text"]};
}}

QSplitter::handle {{
    background-color: {c["bg"]};
}}

QSplitter::handle:hover {{
    background-color: {c["accent"]};
}}

QScrollBar:vertical {{
    background: {c["scrollbar_bg"]};
    width: 10px;
    margin: 0px;
    border-radius: 5px;
}}

QScrollBar::handle:vertical {{
    background: {c["scrollbar_handle"]};
    min-height: 32px;
    border-radius: 5px;
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QScrollBar:horizontal {{
    background: {c["scrollbar_bg"]};
    height: 10px;
    margin: 0px;
    border-radius: 5px;
}}

QScrollBar::handle:horizontal {{
    background: {c["scrollbar_handle"]};
    min-width: 32px;
    border-radius: 5px;
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}
"""

DARK_STYLESHEET = generate_stylesheet(is_dark=True)
LIGHT_STYLESHEET = generate_stylesheet(is_dark=False)
