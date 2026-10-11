"""插件设置兼容导出必须保持惰性，不能让 core 导入时拉起 Qt。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.plugins import settings_builders
from app.core.plugins.run_options import build_missav_proxy_url


def test_proxy_builder_is_reexported_without_loading_ui() -> None:
    assert settings_builders.build_missav_proxy_url is build_missav_proxy_url


def test_ui_export_is_loaded_lazily_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    export_name = "build_douyin_settings_widget"
    sentinel = object()
    imported: list[str] = []
    original = settings_builders.__dict__.get(export_name)
    settings_builders.__dict__.pop(export_name, None)

    def fake_import(name: str) -> SimpleNamespace:
        imported.append(name)
        return SimpleNamespace(**{export_name: sentinel})

    monkeypatch.setattr(settings_builders, "import_module", fake_import)
    try:
        assert settings_builders.__getattr__(export_name) is sentinel
        assert settings_builders.__dict__[export_name] is sentinel
        assert imported == ["app.ui.plugin_settings"]
    finally:
        if original is not None:
            settings_builders.__dict__[export_name] = original
        else:
            settings_builders.__dict__.pop(export_name, None)


def test_unknown_compatibility_export_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="missing_export"):
        settings_builders.__getattr__("missing_export")


def test_control_choices_match_interactive_spec() -> None:
    from app.core.plugins.definitions import (
        BilibiliPlugin,
        DouyinPlugin,
        MissAVPlugin,
        XiaohongshuPlugin,
    )

    def values(plugin, key: str) -> list[object]:
        for field in plugin.interactive_spec.fields:
            if field.key == key:
                return [choice.value for choice in field.choices]
        raise AssertionError(key)

    assert values(DouyinPlugin, "max_items") == [10, 20, 30, 50, 9999]
    assert values(XiaohongshuPlugin, "max_items") == [10, 20, 30, 50, 9999]
    assert values(BilibiliPlugin, "max_pages") == [1, 2, 3, 5, 9999]
    assert settings_builders.control_choice_values("douyin", "max_items") == values(DouyinPlugin, "max_items")
    assert settings_builders.control_choice_values("bilibili", "max_pages") == values(BilibiliPlugin, "max_pages")
    assert "Clash Verge (7897)" in values(MissAVPlugin, "proxy")
    assert 60 in values(MissAVPlugin, "timeout")


def test_unknown_choice_keeps_original_value() -> None:
    assert settings_builders.resolve_choice_value(4, [1, 2, 3, 5, 9999]) == 4
    assert settings_builders.resolve_choice_value(100, [10, 20, 30, 50, 9999]) == 100


def test_read_without_widget_returns_full_runtime_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "max_items": 7,
        "search_max_pages": 3,
        "timeout": 12,
        "request_interval": 0.2,
        "detail_request_interval": 0.1,
        "sort": "time",
        "note_type": 1,
    }

    def fake_runtime(source: str) -> dict:
        assert source == "xiaohongshu"
        return dict(payload)

    monkeypatch.setattr("app.config.get_platform_runtime_defaults", fake_runtime)

    assert settings_builders.read_xiaohongshu_run_options(None) == payload
