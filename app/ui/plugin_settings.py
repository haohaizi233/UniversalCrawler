"""提供仅用于 Qt 的插件设置控件及运行参数适配。"""

from __future__ import annotations

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QWidget

from app.config import cfg, get_platform_runtime_defaults
from app.ui.components.combo_popup import ThemedComboBox

class PageLimitSettingsWidget(QWidget):
    """统一的数量下拉控件，固定为少量常用档位。"""

    def __init__(
        self,
        parent=None,
        *,
        label_text: str,
        max_pages: int,
        default_pages: int,
        tooltip: str,
        preset_values: list[int] | None = None,
    ):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        label = QLabel(label_text)
        layout.addWidget(label)

        self.combo_pages = ThemedComboBox(row_height=32)
        self.combo_pages.setToolTip(tooltip)
        self.combo_pages.setMinimumWidth(84)
        self.combo_pages.setMaximumWidth(84)
        self.combo_pages.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._page_values = list(preset_values or [1, 2, 3, 5, max_pages])
        seen_values: set[int] = set()
        for value in self._page_values:
            if value in seen_values:
                continue
            seen_values.add(value)
            text = "max" if value == max_pages else str(value)
            self.combo_pages.addItem(text, value)
        self.set_current_value(default_pages, max_pages)
        layout.addWidget(self.combo_pages)
        layout.setAlignment(self.combo_pages, Qt.AlignmentFlag.AlignVCenter)

    def current_value(self) -> int:
        return int(self.combo_pages.currentData() or 1)

    def set_current_value(self, value: int, max_pages: int) -> None:
        del max_pages
        index = self.combo_pages.findData(value)
        if index == -1:
            self.combo_pages.addItem(str(value), value)
            index = self.combo_pages.findData(value)
        self.combo_pages.setCurrentIndex(max(index, 0))

class MissAVSettingsWidget(QWidget):
    """MissAV Qt 配置控件。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.chk_individual = QCheckBox("仅单体")
        self.chk_individual.setChecked(cfg.get("missav", "individual_only", False))
        layout.addWidget(self.chk_individual)

        self.combo_priority = ThemedComboBox(row_height=32)
        saved_priority = cfg.get("missav", "priority", "中文字幕优先")
        if saved_priority == "默认排序":
            saved_priority = "中文字幕优先"
        self._fill_choice_combo(self.combo_priority, "priority", saved_priority)
        layout.addWidget(self.combo_priority)

        layout.addWidget(QLabel("超时:"))
        self.combo_timeout = ThemedComboBox(row_height=32)
        timeout_value = int(get_platform_runtime_defaults("missav").get("timeout", 60))
        self._fill_choice_combo(self.combo_timeout, "timeout", timeout_value)
        self.combo_timeout.setMinimumWidth(92)
        layout.addWidget(self.combo_timeout)

        layout.addWidget(QLabel("代理:"))
        self.combo_proxy = ThemedComboBox(row_height=32)
        self.combo_proxy.setEditable(True)
        for choice in _interactive_choices("missav", "proxy"):
            label = str(getattr(choice, "label", "") or getattr(choice, "value", "") or "")
            if label:
                self.combo_proxy.addItem(label, getattr(choice, "value", label))
        saved_proxy = str(cfg.get("missav", "proxy_app", "Clash (7890)"))
        proxy_index = self.combo_proxy.findText(saved_proxy)
        if proxy_index < 0:
            proxy_index = self.combo_proxy.findData(saved_proxy)
        if proxy_index < 0:
            self.combo_proxy.addItem(saved_proxy, saved_proxy)
            proxy_index = self.combo_proxy.findText(saved_proxy)
        if proxy_index >= 0:
            self.combo_proxy.setCurrentIndex(proxy_index)
        else:
            self.combo_proxy.setCurrentText(saved_proxy)
        self.combo_proxy.setMinimumWidth(220)
        layout.addWidget(self.combo_proxy)

    @staticmethod
    def _fill_choice_combo(combo: ThemedComboBox, field_key: str, current) -> None:
        for choice in _interactive_choices("missav", field_key):
            value = getattr(choice, "value", None)
            if value is None:
                continue
            label = str(getattr(choice, "label", "") or value)
            combo.addItem(label, value)
        index = combo.findData(current)
        if index < 0:
            index = combo.findText(str(current))
        if index < 0:
            combo.addItem(str(current), current)
            index = combo.findData(current)
        combo.setCurrentIndex(max(index, 0))

def build_bilibili_settings_widget(parent=None) -> PageLimitSettingsWidget:
    from app.core.plugins.settings_builders import build_bilibili_settings_widget as build_widget

    return build_widget(parent)

def read_bilibili_run_options(widget: QWidget | None) -> dict:
    from app.core.plugins.settings_builders import read_bilibili_run_options as read_runtime

    return read_runtime(widget)

def build_douyin_settings_widget(parent=None) -> PageLimitSettingsWidget:
    from app.core.plugins.settings_builders import build_douyin_settings_widget as build_widget

    return build_widget(parent)

def read_douyin_run_options(widget: QWidget | None) -> dict:
    from app.core.plugins.settings_builders import read_douyin_run_options as read_runtime

    return read_runtime(widget)

def build_xiaohongshu_settings_widget(parent=None) -> PageLimitSettingsWidget:
    from app.core.plugins.settings_builders import build_xiaohongshu_settings_widget as build_widget

    return build_widget(parent)

def read_xiaohongshu_run_options(widget: QWidget | None) -> dict:
    from app.core.plugins.settings_builders import read_xiaohongshu_run_options as read_runtime

    return read_runtime(widget)

def build_kuaishou_settings_widget(parent=None) -> PageLimitSettingsWidget:
    from app.core.plugins.settings_builders import build_kuaishou_settings_widget as build_widget

    return build_widget(parent)

def read_kuaishou_run_options(widget: QWidget | None) -> dict:
    from app.core.plugins.settings_builders import read_kuaishou_run_options as read_runtime

    return read_runtime(widget)

def build_missav_settings_widget(parent=None) -> MissAVSettingsWidget:
    return MissAVSettingsWidget(parent)

def read_missav_run_options(widget: QWidget | None) -> dict:
    from app.core.plugins.settings_builders import read_missav_run_options as read_runtime

    return read_runtime(widget)

def _iter_plugin_settings_functions():
    """为注册表中的每个插件产出 (plugin_id, builder_fn, reader_fn)。

    按本模块中的 ``build_<id>_settings_widget`` 与 ``read_<id>_run_options``
    命名约定自动发现函数，避免维护重复的手工映射表。

    没有设置构建器的插件返回 ``(builder=None, reader=None)``。
    """
    from app.core.plugin_registry import registry

    module = sys.modules[__name__]
    for p in registry.get_all_plugins():
        pid = p.id
        builder = getattr(module, f"build_{pid}_settings_widget", None)
        reader = getattr(module, f"read_{pid}_run_options", None)
        yield pid, builder, reader

def build_plugin_settings_widget(plugin_id: str, parent=None) -> QWidget | None:
    """构建 *plugin_id* 的设置控件；没有构建器时返回 ``None``。"""
    for pid, builder, _reader in _iter_plugin_settings_functions():
        if pid == plugin_id and builder is not None:
            return builder(parent)
    return None

def read_plugin_run_options(plugin_id: str, widget: QWidget | None) -> dict:
    """从 *plugin_id* 的设置控件读取运行参数；无控件时返回全部持久化运行字段。"""
    from app.config import get_platform_runtime_defaults
    from app.core.plugins import settings_builders

    reader = getattr(settings_builders, f"read_{plugin_id}_run_options", None)
    if callable(reader):
        return reader(widget)
    return dict(get_platform_runtime_defaults(plugin_id))


def _interactive_choices(plugin_id: str, field_key: str):
    from app.core.plugin_registry import registry

    plugin = registry.get_plugin(plugin_id)
    if plugin is None:
        return ()
    for field in plugin.get_interactive_spec().fields:
        if field.key == field_key:
            return field.choices
    return ()
