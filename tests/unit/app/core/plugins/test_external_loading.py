"""外部插件加载、ID 冲突和热重载。"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from app.core.plugins.base import BasePlugin
from app.core.plugins.discovery import (
    PluginDiscoveryError,
    get_external_plugin_dir,
    set_external_plugin_dir,
)
from app.core.plugins.registry import PluginRegistry
from app.core.plugin_registry import registry


def _write(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")


def _plugin_source(
    plugin_id: str,
    marker: str,
    class_name: str = "HotDemoPlugin",
    sort_order: int = 1,
) -> str:
    return (
        "from app.core.plugins.base import BasePlugin\n"
        f"class {class_name}(BasePlugin):\n"
        f"    id = {plugin_id!r}\n"
        f"    name = {plugin_id!r}\n"
        f"    sort_order = {sort_order}\n"
        f"    marker = {marker!r}\n"
    )


@pytest.fixture
def isolated_spi():
    saved_classes = dict(BasePlugin._subclasses)
    saved_dir = get_external_plugin_dir()
    try:
        yield
    finally:
        BasePlugin._subclasses.clear()
        BasePlugin._subclasses.update(saved_classes)
        set_external_plugin_dir(saved_dir)
        for name in list(sys.modules):
            if name.startswith("ucrawl_ext_plugin_"):
                sys.modules.pop(name, None)


def test_same_id_conflict_fails_and_class_query_matches_instance(isolated_spi, tmp_path: Path) -> None:
    _write(tmp_path / "evil_plugin.py", _plugin_source("douyin", "evil", "EvilDouyinPlugin"))
    builtin = registry.get_plugin("douyin")

    with pytest.raises(PluginDiscoveryError, match="douyin"):
        PluginRegistry(external_plugin_dir=str(tmp_path))

    assert registry.get_plugin("douyin") is builtin
    assert registry.get_plugin_class("douyin") is type(builtin)


def test_explicit_empty_registry_does_not_treat_empty_dict_as_unloaded(isolated_spi) -> None:
    custom = PluginRegistry([])

    assert custom.get_all_plugins_safe() == []
    assert custom.get_plugin_class("douyin") is None


def test_hot_reload_replaces_instance_and_unloads_deleted_file(isolated_spi, tmp_path: Path) -> None:
    plugin_path = tmp_path / "hot_demo.py"
    _write(plugin_path, _plugin_source("ext_hot_demo", "v1"))
    custom = PluginRegistry(external_plugin_dir=str(tmp_path))
    first = custom.get_plugin("ext_hot_demo")
    assert first is not None
    assert first.marker == "v1"
    assert custom.get_plugin_class("ext_hot_demo") is type(first)

    _write(plugin_path, _plugin_source("ext_hot_demo", "v2"))
    later = time.time() + 5
    os.utime(plugin_path, (later, later))
    custom.reload_plugins()
    second = custom.get_plugin("ext_hot_demo")
    assert second is not first
    assert second.marker == "v2"
    assert custom.get_plugin_class("ext_hot_demo") is type(second)

    plugin_path.unlink()
    custom.reload_plugins()
    assert custom.get_plugin("ext_hot_demo") is None
    assert custom.get_plugin_class("ext_hot_demo") is None


def test_hot_reload_rolls_back_swapped_instances_before_id_conflict(isolated_spi, tmp_path: Path) -> None:
    class _Pinned:
        id = "conflict_target"

    pinned = _Pinned()
    good = tmp_path / "a_good.py"
    _write(good, _plugin_source("ext_good", "v1", "GoodPlugin", sort_order=0))
    custom = PluginRegistry([pinned], external_plugin_dir=str(tmp_path))
    custom.reload_plugins()
    first = custom.get_plugin("ext_good")
    assert first is not None
    assert first.marker == "v1"

    _write(good, _plugin_source("ext_good", "v2", "GoodPlugin", sort_order=0))
    later = time.time() + 5
    os.utime(good, (later, later))
    _write(
        tmp_path / "z_conflict.py",
        _plugin_source("conflict_target", "evil", "ConflictPlugin", sort_order=10),
    )

    with pytest.raises(ValueError, match="conflict_target"):
        custom.reload_plugins()

    assert custom.get_plugin("ext_good") is first
    assert first.marker == "v1"
    assert custom.get_plugin("conflict_target") is pinned


def test_hot_reload_rolls_back_swapped_instances_before_import_error(isolated_spi, tmp_path: Path) -> None:
    good = tmp_path / "a_good.py"
    _write(good, _plugin_source("ext_good", "v1", "GoodPlugin"))
    custom = PluginRegistry(external_plugin_dir=str(tmp_path))
    first = custom.get_plugin("ext_good")
    assert first is not None
    assert first.marker == "v1"

    _write(good, _plugin_source("ext_good", "v2", "GoodPlugin"))
    later = time.time() + 5
    os.utime(good, (later, later))
    _write(tmp_path / "z_broken.py", "raise RuntimeError('broken plugin')\n")

    with pytest.raises(PluginDiscoveryError, match="broken plugin"):
        custom.reload_plugins()

    assert custom.get_plugin("ext_good") is first
    assert first.marker == "v1"


def test_duplicate_discovered_ids_are_errors(isolated_spi) -> None:
    from app.core.plugins.discovery import iter_plugin_classes

    class _Left(BasePlugin):
        id = "dup_probe_left"
        name = "Left"

    class _Right(BasePlugin):
        id = "dup_probe_right"
        name = "Right"

    _Right.id = "dup_probe_left"
    try:
        result = iter_plugin_classes()
    finally:
        BasePlugin._subclasses.pop("dup_probe_left", None)
        BasePlugin._subclasses.pop("dup_probe_right", None)

    assert any("dup_probe_left" in error for error in result.errors)
