"""插件设置构建与运行参数读取。

界面控件的旧实现仍在 :mod:`app.ui.plugin_settings`。本模块提供与
``interactive_spec`` 对齐的选项、保留未知当前值的数量控件，以及无控件时
读取持久化运行值的函数。

``app.ui.plugin_settings.read_plugin_run_options`` 仍调用 UI 模块里的同名
读取函数，宿主需要改到本模块后，无控件路径才会返回平台的全部运行字段。
"""

from importlib import import_module
from typing import Any

from app.core.plugins.run_options import build_missav_proxy_url

_UI_EXPORTS = {
    "MissAVSettingsWidget",
    "PageLimitSettingsWidget",
    "build_bilibili_settings_widget",
    "build_douyin_settings_widget",
    "build_kuaishou_settings_widget",
    "build_missav_settings_widget",
    "build_xiaohongshu_settings_widget",
    "read_bilibili_run_options",
    "read_douyin_run_options",
    "read_kuaishou_run_options",
    "read_missav_run_options",
    "read_xiaohongshu_run_options",
}

_PAGE_FIELDS = {
    "bilibili": ("max_pages", "页数:", "搜索或列表扫描页数"),
    "douyin": ("max_items", "视频数:", "抖音单次最多处理的视频数量"),
    "kuaishou": ("max_items", "视频数:", "快手单次最多扫描的视频数量"),
    "xiaohongshu": ("max_items", "笔记数:", "小红书单次最多处理的笔记数量"),
}

__all__ = [
    "build_missav_proxy_url",
    "control_choice_values",
    "resolve_choice_value",
    *_UI_EXPORTS,
]


def __getattr__(name: str) -> Any:
    if name not in _UI_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if name in globals() and globals()[name] is not __getattr__:
        return globals()[name]

    module = import_module("app.ui.plugin_settings")
    value = getattr(module, name)
    globals()[name] = value
    return value


def control_choice_values(plugin_id: str, field_key: str) -> list[Any]:
    """返回与 ``interactive_spec`` 同一字段的选项值。"""
    return [choice.value for choice in _field_choices(plugin_id, field_key)]


def resolve_choice_value(current: Any, choices: list[Any] | tuple[Any, ...]) -> Any:
    """找不到当前值时保留原值，不改写成相邻档。"""
    values = [_choice_value(choice) for choice in choices]
    if current in values:
        return current
    return current


def _choice_value(choice: Any) -> Any:
    return choice.value if hasattr(choice, "value") else choice


def _field_choices(plugin_id: str, field_key: str) -> tuple[Any, ...]:
    from app.core.plugin_registry import registry

    plugin = registry.get_plugin(plugin_id)
    if plugin is None:
        return ()
    for field in plugin.get_interactive_spec().fields:
        if field.key == field_key:
            return field.choices
    return ()


def _runtime_options(platform_id: str) -> dict[str, Any]:
    from app.config import get_platform_runtime_defaults

    return dict(get_platform_runtime_defaults(platform_id))


def _page_widget_value(widget: Any) -> int | None:
    if widget is None:
        return None
    from app.ui.plugin_settings import PageLimitSettingsWidget

    if not isinstance(widget, PageLimitSettingsWidget):
        return None
    return int(widget.current_value())


def _read_page_options(platform_id: str, widget: Any) -> dict[str, Any]:
    options = _runtime_options(platform_id)
    field_key = _PAGE_FIELDS[platform_id][0]
    selected = _page_widget_value(widget)
    if selected is None:
        return options
    choices = control_choice_values(platform_id, field_key)
    options[field_key] = resolve_choice_value(selected, choices)
    return options


def read_bilibili_run_options(widget: Any = None) -> dict[str, Any]:
    """无控件时返回 B 站全部持久化运行字段。"""
    return _read_page_options("bilibili", widget)


def read_douyin_run_options(widget: Any = None) -> dict[str, Any]:
    """无控件时返回抖音全部持久化运行字段。"""
    return _read_page_options("douyin", widget)


def read_kuaishou_run_options(widget: Any = None) -> dict[str, Any]:
    """无控件时返回快手全部持久化运行字段。"""
    return _read_page_options("kuaishou", widget)


def read_xiaohongshu_run_options(widget: Any = None) -> dict[str, Any]:
    """无控件时返回小红书全部持久化运行字段，包括排序和笔记类型。"""
    return _read_page_options("xiaohongshu", widget)


def read_missav_run_options(widget: Any = None) -> dict[str, Any]:
    """无控件时返回 MissAV 全部持久化运行字段，而不是出厂默认值。"""
    options = _runtime_options("missav")
    if widget is None:
        return options
    from app.ui.plugin_settings import MissAVSettingsWidget

    if not isinstance(widget, MissAVSettingsWidget):
        return options
    options["individual_only"] = bool(widget.chk_individual.isChecked())
    options["priority"] = resolve_choice_value(
        widget.combo_priority.currentText(),
        control_choice_values("missav", "priority"),
    )
    timeout = widget.combo_timeout.currentData()
    if timeout is None:
        timeout = options.get("timeout", 60)
    options["timeout"] = resolve_choice_value(
        int(timeout),
        control_choice_values("missav", "timeout"),
    )
    options["proxy"] = build_missav_proxy_url(widget.combo_proxy.currentText())
    return options


def _retaining_page_limit_type():
    cached = getattr(_retaining_page_limit_type, "_widget", None)
    if cached is not None:
        return cached
    from app.ui.plugin_settings import PageLimitSettingsWidget

    class RetainingPageLimitSettingsWidget(PageLimitSettingsWidget):
        """当前值不在档位里时保留原值，不改写成相邻档或 max。"""

        def set_current_value(self, value: int, max_pages: int) -> None:
            del max_pages
            index = self.combo_pages.findData(value)
            if index == -1:
                self.combo_pages.addItem(str(value), value)
                index = self.combo_pages.findData(value)
            self.combo_pages.setCurrentIndex(max(index, 0))

    _retaining_page_limit_type._widget = RetainingPageLimitSettingsWidget
    return RetainingPageLimitSettingsWidget


def _build_page_limit_widget(platform_id: str, parent=None):
    field_key, label_text, tooltip = _PAGE_FIELDS[platform_id]
    values = [
        value
        for value in control_choice_values(platform_id, field_key)
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    defaults = _runtime_options(platform_id)
    default_value = defaults.get(field_key, values[0] if values else 1)
    try:
        default_pages = int(default_value)
    except (TypeError, ValueError):
        default_pages = values[0] if values else 1
    widget_type = _retaining_page_limit_type()
    return widget_type(
        parent,
        label_text=label_text,
        max_pages=max(values) if values else default_pages,
        default_pages=default_pages,
        tooltip=tooltip,
        preset_values=values or None,
    )


def build_bilibili_settings_widget(parent=None):
    return _build_page_limit_widget("bilibili", parent)


def build_douyin_settings_widget(parent=None):
    return _build_page_limit_widget("douyin", parent)


def build_kuaishou_settings_widget(parent=None):
    return _build_page_limit_widget("kuaishou", parent)


def build_xiaohongshu_settings_widget(parent=None):
    return _build_page_limit_widget("xiaohongshu", parent)


def build_missav_settings_widget(parent=None):
    module = import_module("app.ui.plugin_settings")
    return module.build_missav_settings_widget(parent)
