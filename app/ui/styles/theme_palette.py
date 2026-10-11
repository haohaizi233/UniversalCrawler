"""主题色板、字号缩放和 Qt palette。"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QColor, QFontDatabase, QPalette
from PyQt6.QtWidgets import QApplication

LIGHT = {
    "bg": "#f6f8fb",
    "panel": "#ffffff",
    "panel_soft": "#f8fafc",
    "input": "#ffffff",
    "accent": "#1677ff",
    "accent_hover": "#0f5fd7",
    "accent_soft": "#eaf3ff",
    "row_selected": "#dbeafe",
    "danger": "#ef4444",
    "danger_hover": "#d9363e",
    "success": "#22c55e",
    "warning": "#f59e0b",
    "text": "#111827",
    "muted": "#6b7280",
    "border": "#e5e7eb",
    "border_strong": "#d1d5db",
    "row_alt": "#fbfdff",
    "scrollbar_bg": "#f1f5f9",
    "scrollbar_handle": "#cbd5e1",
    "video_bg": "#f8fafc",
    "log_bg": "#ffffff",
}

DARK = {
    "bg": "#171a1f",
    "panel": "#22262d",
    "panel_soft": "#2b3038",
    "input": "#1c2026",
    "accent": "#3b82f6",
    "accent_hover": "#2563eb",
    "accent_soft": "#1f2d46",
    "row_selected": "#1e3a5f",
    "danger": "#f87171",
    "danger_hover": "#ef4444",
    "success": "#22c55e",
    "warning": "#fbbf24",
    "text": "#e5e7eb",
    "muted": "#9ca3af",
    "border": "#383f48",
    "border_strong": "#4b5563",
    "row_alt": "#252a31",
    "scrollbar_bg": "#14171c",
    "scrollbar_handle": "#4b5563",
    "video_bg": "#0b0f16",
    "log_bg": "#1c2026",
}

ACCENT_PALETTES = {
    "blue": ("#1677ff", "#0f5fd7", "#eaf3ff", "#3b82f6", "#2563eb", "#1f2d46"),
    "green": ("#16a34a", "#15803d", "#e7f8ee", "#22c55e", "#16a34a", "#153523"),
    "purple": ("#7c3aed", "#6d28d9", "#f1eaff", "#a78bfa", "#8b5cf6", "#312548"),
    "orange": ("#ea580c", "#c2410c", "#fff1e7", "#fb923c", "#f97316", "#3d2718"),
    "red": ("#dc2626", "#b91c1c", "#feecec", "#f87171", "#ef4444", "#402020"),
}

FONT_POINT_SIZES = {"small": 8, "medium": 9, "large": 10}
FONT_PIXEL_SIZES = {"small": 12, "medium": 13, "large": 15}
SCALE_FACTORS = {"90%": 0.9, "100%": 1.0, "110%": 1.1, "125%": 1.25}
_FONTS_LOADED = False
_PREFERRED_UI_FONT_FAMILY: str | None = None

def _configured_appearance_value(key: str, fallback: str) -> str:
    try:
        from app.config import cfg

        return str(cfg.get("appearance", key, fallback) or fallback)
    except (ImportError, AttributeError, RuntimeError):
        return fallback

def _scale_factor(value: str | None) -> float:
    return SCALE_FACTORS.get(str(value or "100%").strip(), 1.0)

def _scaled_point_size(font_size: str, scale: str) -> int:
    base = FONT_POINT_SIZES.get(str(font_size or "medium").strip().lower(), 9)
    return max(8, round(base * _scale_factor(scale)))

def _scaled_pixel_size(font_size: str, scale: str) -> int:
    base = FONT_PIXEL_SIZES.get(str(font_size or "medium").strip().lower(), 13)
    return max(11, round(base * _scale_factor(scale)))

def theme_colors(is_dark: bool) -> dict[str, str]:
    colors = dict(DARK if is_dark else LIGHT)
    accent = _configured_appearance_value("accent", "blue").strip().lower()
    palette = ACCENT_PALETTES.get(accent)
    if palette:
        light_accent, light_hover, light_soft, dark_accent, dark_hover, dark_soft = palette
        colors["accent"] = dark_accent if is_dark else light_accent
        colors["accent_hover"] = dark_hover if is_dark else light_hover
        colors["accent_soft"] = dark_soft if is_dark else light_soft
        colors["row_selected"] = dark_soft if is_dark else light_soft
    return colors

def _ensure_cjk_fonts_loaded() -> None:
    """为离屏渲染和打包应用加载常见 Windows CJK 字体。"""
    global _FONTS_LOADED
    if _FONTS_LOADED:
        return
    font_dir = Path("C:/Windows/Fonts")
    for name in ("msyh.ttc", "msyhbd.ttc", "simsun.ttc"):
        candidate = font_dir / name
        if candidate.is_file():
            QFontDatabase.addApplicationFont(str(candidate))
    _FONTS_LOADED = True

def _preferred_ui_font_family() -> str:
    global _PREFERRED_UI_FONT_FAMILY
    if _PREFERRED_UI_FONT_FAMILY:
        return _PREFERRED_UI_FONT_FAMILY
    _ensure_cjk_fonts_loaded()
    families = set(QFontDatabase.families())
    for family in (
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "SimSun",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "Segoe UI",
    ):
        if family in families:
            _PREFERRED_UI_FONT_FAMILY = family
            return family
    _PREFERRED_UI_FONT_FAMILY = QApplication.instance().font().family() if QApplication.instance() is not None else "Sans Serif"
    return _PREFERRED_UI_FONT_FAMILY

def build_palette(is_dark: bool) -> QPalette:
    """构建 Qt palette，使忽略 QSS 的视图仍能跟随主题。"""
    c = theme_colors(is_dark)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(c["bg"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(c["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(c["panel"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(c["row_alt"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(c["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(c["panel"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(c["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(c["row_selected"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(c["text"]))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(c["muted"]))
    palette.setColor(QPalette.ColorRole.Mid, QColor(c["muted"]))
    palette.setColor(QPalette.ColorRole.Light, QColor(c["border"]))
    palette.setColor(QPalette.ColorRole.Dark, QColor(c["border_strong"]))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(c["panel"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(c["text"]))
    return palette
