"""从 SPI、Python 入口点及外部目录发现插件。"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BasePlugin

logger = logging.getLogger(__name__)


class PluginDiscoveryError(RuntimeError):
    """插件发现未完整成功。调用方应保留错误并允许重试。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) or "插件发现失败")


@dataclass
class PluginDiscoveryResult:
    """一次发现的插件类，以及未能导入的错误。"""

    classes: list[type[BasePlugin]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    origins: dict[str, str] = field(default_factory=dict)
    removed_paths: list[str] = field(default_factory=list)
    failed_paths: list[str] = field(default_factory=list)

    def __iter__(self):
        return iter(self.classes)

    def __len__(self) -> int:
        return len(self.classes)

    def __getitem__(self, index):
        return self.classes[index]


def _load_lock():
    """SPI、外部目录缓存和下载器索引共用这一把锁。"""
    from .registry import plugin_load_lock

    return plugin_load_lock


def _as_result(value: PluginDiscoveryResult | list) -> PluginDiscoveryResult:
    if isinstance(value, PluginDiscoveryResult):
        return value
    return PluginDiscoveryResult(classes=list(value))


def _record_error(errors: list[str], message: str) -> None:
    logger.error(message)
    errors.append(message)


# 内置 SPI 插件

def discover_builtin_plugins() -> PluginDiscoveryResult:
    """导入内置定义以触发 SPI 自动注册，并返回排好序的插件类。"""
    from .base import BasePlugin

    with _load_lock():
        # 单纯导入定义模块即可触发 ``BasePlugin.__init_subclass__``。
        from . import definitions  # noqa: F401

        classes = list(BasePlugin.get_subclasses().values())
        return PluginDiscoveryResult(classes=_sort_classes(classes))


# 通过 pip 安装并声明入口点的插件

DISCOVERY_ENTRY_POINT_GROUP = "ucrawl.plugins"

def discover_entry_point_plugins() -> PluginDiscoveryResult:
    """发现注册在 ``ucrawl.plugins`` 入口点组中的插件。"""
    from .base import BasePlugin

    with _load_lock():
        classes: list[type[BasePlugin]] = []
        errors: list[str] = []
        try:
            from importlib.metadata import entry_points
            eps = entry_points(group=DISCOVERY_ENTRY_POINT_GROUP)
        except (ImportError, TypeError):
            return PluginDiscoveryResult()

        for ep in eps:
            name = getattr(ep, "name", repr(ep))
            try:
                plugin_cls = ep.load()
            except Exception as exc:
                _record_error(errors, f"入口点 {name!r} 导入失败: {exc}")
                continue
            if (
                inspect.isclass(plugin_cls)
                and issubclass(plugin_cls, BasePlugin)
                and plugin_cls is not BasePlugin
            ):
                classes.append(plugin_cls)
            else:
                _record_error(errors, f"入口点 {name!r} 未返回插件类")

        return PluginDiscoveryResult(classes=_sort_classes(classes), errors=errors)


# 用户外部目录插件

_EXTERNAL_PLUGIN_DIR: str | None = None
_EXTERNAL_MTIME: dict[str, float] = {}  # 文件路径到最近修改时间的缓存
_EXTERNAL_MODULES: dict[str, str] = {}  # 文件路径到模块名

def set_external_plugin_dir(path: str | None) -> None:
    """设置外部插件目录；传入 ``None`` 时关闭该发现来源。"""
    with _load_lock():
        _set_external_plugin_dir_unlocked(path)


def _set_external_plugin_dir_unlocked(path: str | None) -> None:
    global _EXTERNAL_PLUGIN_DIR
    normalized = os.path.abspath(path) if path else None
    if normalized != _EXTERNAL_PLUGIN_DIR:
        _EXTERNAL_MTIME.clear()
        _EXTERNAL_MODULES.clear()
    _EXTERNAL_PLUGIN_DIR = normalized


def get_external_plugin_dir() -> str | None:
    """返回已配置的外部插件目录，未启用时返回 ``None``。"""
    with _load_lock():
        return _EXTERNAL_PLUGIN_DIR


def _plugin_stem(fname: str) -> str:
    stem = fname[:-3] if fname.lower().endswith(".py") else fname
    if "." in stem:
        raise ValueError(f"拒绝带点的插件文件名: {fname}")
    return stem


def _load_module_from_path(module_name: str, path: str):
    """按绝对路径加载模块，导入期间临时加入目录，结束后恢复 ``sys.path``。"""
    abs_path = os.path.abspath(path)
    spec = importlib.util.spec_from_file_location(module_name, abs_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法按路径加载插件: {abs_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    parent = os.path.dirname(abs_path)
    saved_path = list(sys.path)
    try:
        if parent not in sys.path:
            sys.path.insert(0, parent)
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    finally:
        sys.path[:] = saved_path
    return module


def _rollback_module_spi(module_name: str, snapshot: dict[str, type]) -> None:
    from .base import BasePlugin

    for pid, klass in list(BasePlugin._subclasses.items()):
        if klass.__module__ == module_name:
            BasePlugin._subclasses.pop(pid, None)
    for pid, klass in snapshot.items():
        BasePlugin._subclasses[pid] = klass


def discover_external_plugins(
    plugin_dir: str | None = None,
    *,
    force: bool = False,
) -> PluginDiscoveryResult:
    """从目录中的 ``.py`` 文件发现插件类。

    使用绝对路径加载，不把插件目录留在 ``sys.path``。文件名主干里带点的文件
    会被拒绝。导入失败记入结果的 ``errors``，不会被当成成功跳过。
    """
    from .base import BasePlugin

    with _load_lock():
        target_dir = plugin_dir or _EXTERNAL_PLUGIN_DIR
        if not target_dir or not os.path.isdir(target_dir):
            return PluginDiscoveryResult()

        target_dir = os.path.abspath(target_dir)
        errors: list[str] = []
        failed_paths: list[str] = []
        classes: list[type[BasePlugin]] = []
        origins: dict[str, str] = {}

        py_files = sorted(
            f for f in os.listdir(target_dir)
            if f.endswith(".py") and not f.startswith("_")
        )
        current_paths: set[str] = set()

        for fname in py_files:
            fpath = os.path.abspath(os.path.join(target_dir, fname))
            try:
                stem = _plugin_stem(fname)
            except ValueError as exc:
                _record_error(errors, str(exc))
                failed_paths.append(fpath)
                continue
            try:
                mtime = os.path.getmtime(fpath)
            except OSError as exc:
                _record_error(errors, f"外部插件无法读取修改时间 {fpath}: {exc}")
                failed_paths.append(fpath)
                continue

            current_paths.add(fpath)
            mod_name = f"ucrawl_ext_plugin_{stem}"

            # 未变更文件沿用已加载模块，避免热重载反复执行模块级副作用。
            if not force and _EXTERNAL_MTIME.get(fpath) == mtime and mod_name in sys.modules:
                module = sys.modules[mod_name]
                _collect_module_classes(module, fpath, classes, origins)
                continue

            if mod_name in sys.modules:
                del sys.modules[mod_name]

            spi_snapshot = {
                pid: klass
                for pid, klass in BasePlugin._subclasses.items()
                if klass.__module__ == mod_name
            }
            try:
                module = _load_module_from_path(mod_name, fpath)
            except Exception as exc:
                _rollback_module_spi(mod_name, spi_snapshot)
                _EXTERNAL_MTIME.pop(fpath, None)
                _EXTERNAL_MODULES.pop(fpath, None)
                failed_paths.append(fpath)
                _record_error(errors, f"外部插件导入失败 {fpath}: {exc}")
                continue

            _EXTERNAL_MTIME[fpath] = mtime
            _EXTERNAL_MODULES[fpath] = mod_name
            _collect_module_classes(module, fpath, classes, origins)

        removed_paths: list[str] = []
        prefix = target_dir + os.sep
        for cached in list(_EXTERNAL_MTIME):
            if cached != target_dir and not cached.startswith(prefix):
                continue
            if cached in current_paths:
                continue
            removed_paths.append(cached)
            module_name = _EXTERNAL_MODULES.pop(cached, None)
            _EXTERNAL_MTIME.pop(cached, None)
            if module_name:
                sys.modules.pop(module_name, None)

        return PluginDiscoveryResult(
            classes=_sort_classes(classes),
            errors=errors,
            origins=origins,
            removed_paths=removed_paths,
            failed_paths=failed_paths,
        )


def _collect_module_classes(module, fpath: str, classes: list, origins: dict[str, str]) -> None:
    from .base import BasePlugin

    for _name, obj in inspect.getmembers(module, inspect.isclass):
        if (
            issubclass(obj, BasePlugin)
            and obj is not BasePlugin
            and obj.__module__ == module.__name__
        ):
            classes.append(obj)
            origins[getattr(obj, "id", "")] = fpath


# 汇总所有发现来源

def iter_plugin_classes() -> PluginDiscoveryResult:
    """汇总内置 SPI、Python 入口点与外部目录插件。

    相同平台 ID 的不同类记为错误，不静默覆盖。结果按
    ``(sort_order, name, class_name)`` 稳定排序。
    """
    with _load_lock():
        seen: dict[str, type[BasePlugin]] = {}
        all_classes: list[type[BasePlugin]] = []
        errors: list[str] = []
        origins: dict[str, str] = {}
        removed_paths: list[str] = []
        failed_paths: list[str] = []

        for discover_fn in (
            discover_builtin_plugins,
            discover_entry_point_plugins,
            discover_external_plugins,
        ):
            discovered = _as_result(discover_fn())
            errors.extend(discovered.errors)
            origins.update(discovered.origins)
            removed_paths.extend(discovered.removed_paths)
            failed_paths.extend(discovered.failed_paths)
            for cls in discovered.classes:
                pid = getattr(cls, "id", None)
                if not pid:
                    continue
                previous = seen.get(pid)
                if previous is not None:
                    if previous is cls:
                        continue
                    _record_error(
                        errors,
                        f"重复的插件 ID: {pid} ({cls.__module__}.{cls.__name__})",
                    )
                    continue
                seen[pid] = cls
                all_classes.append(cls)

        return PluginDiscoveryResult(
            classes=_sort_classes(all_classes),
            errors=errors,
            origins=origins,
            removed_paths=removed_paths,
            failed_paths=failed_paths,
        )


def discover_builtin_plugin_instances() -> PluginDiscoveryResult:
    """实例化全部已发现的插件类，并保留发现错误。"""
    discovered = _as_result(iter_plugin_classes())
    instances = [cls() for cls in discovered.classes]
    return PluginDiscoveryResult(
        classes=instances,
        errors=list(discovered.errors),
        origins=dict(discovered.origins),
        removed_paths=list(discovered.removed_paths),
        failed_paths=list(discovered.failed_paths),
    )


# 内部排序规则

def _sort_classes(
    classes: list[type["BasePlugin"]],
) -> list[type["BasePlugin"]]:
    """依次按排序值、显示名称和类名排列插件类。"""
    return sorted(
        classes,
        key=lambda cls: (
            getattr(cls, "sort_order", 1000),
            getattr(cls, "name", cls.__name__),
            cls.__name__,
        ),
    )
