"""支持 SPI、Python 入口点与外部目录的插件注册表。"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BasePlugin

# SPI、外部目录缓存和下载器索引共用这一把可重入锁。
plugin_load_lock = threading.RLock()


class PluginRegistry:
    """线程安全的中心插件注册表。

    支持内置 SPI、``ucrawl.plugins`` 入口点和可热重载的用户外部目录三种来源。
    新平台只需提供一个启动时可导入的 ``BasePlugin`` 子类，无需修改本注册表。
    是否已完成发现由独立的 ``_loaded`` 标志表示，空注册表不等于已加载。
    """

    def __init__(
        self,
        plugins: list[BasePlugin] | None = None,
        *,
        external_plugin_dir: str | None = None,
    ) -> None:
        self._lock = plugin_load_lock
        self._plugins: dict[str, BasePlugin] = {}
        self._external_sources: dict[str, str] = {}
        self._loaded = False
        self._loading = False

        with plugin_load_lock:
            # 必须先设置外部目录，再执行自动发现，否则首次快照会漏掉用户插件。
            if external_plugin_dir is not None:
                from .discovery import _set_external_plugin_dir_unlocked

                _set_external_plugin_dir_unlocked(external_plugin_dir)

            # ``None`` 表示立即发现全部来源；显式列表只以给定插件初始化，空列表则不加载默认项。
            if plugins is None:
                self._ensure_loaded_unlocked()
            else:
                for plugin in plugins:
                    self._register_unlocked(plugin)
                self._loaded = True

    # 注册与注销

    def register(self, plugin: BasePlugin) -> None:
        """注册插件实例；平台 ID 重复时抛出 ``ValueError``。"""
        with plugin_load_lock:
            self._register_unlocked(plugin)

    def _register_unlocked(self, plugin: BasePlugin) -> None:
        if plugin.id in self._plugins:
            raise ValueError(f"重复的插件 ID: {plugin.id}")
        self._plugins[plugin.id] = plugin

    def unregister(self, plugin_id: str) -> bool:
        """按 ID 移除插件，并返回是否确实移除了实例。"""
        with plugin_load_lock:
            return self._unregister_unlocked(plugin_id)

    def _unregister_unlocked(self, plugin_id: str) -> bool:
        plugin = self._plugins.pop(plugin_id, None)
        self._external_sources.pop(plugin_id, None)
        if plugin is None:
            return False
        from .base import BasePlugin

        current = BasePlugin._subclasses.get(plugin_id)
        if current is type(plugin):
            BasePlugin._subclasses.pop(plugin_id, None)
        return True

    def register_from_class(self, plugin_cls: type[BasePlugin]) -> BasePlugin:
        """实例化 ``plugin_cls``、完成注册并返回该实例。"""
        instance = plugin_cls()
        self.register(instance)
        return instance

    # 查询稳定快照

    def get_all_plugins(self) -> list[BasePlugin]:
        """返回当前全部插件的稳定快照。"""
        with plugin_load_lock:
            return list(self._plugins.values())

    def get_plugin(self, plugin_id: str) -> BasePlugin | None:
        """按平台 ID 查询插件实例。"""
        with plugin_load_lock:
            return self._plugins.get(plugin_id)

    def get_plugin_class(self, plugin_id: str) -> type[BasePlugin] | None:
        """按平台 ID 查询插件类。与实例查询使用同一份注册表。"""
        with plugin_load_lock:
            plugin = self._plugins.get(plugin_id)
            if plugin is None:
                return None
            return type(plugin)

    # 首次查询时才执行的汇总发现

    def _ensure_loaded(self) -> None:
        """发现并登记所有来源的插件。失败时不置位，下次调用可以重试。"""
        with plugin_load_lock:
            self._ensure_loaded_unlocked()

    def _ensure_loaded_unlocked(self) -> None:
        if self._loaded or self._loading:
            return
        self._loading = True
        snapshot = dict(self._plugins)
        sources = dict(self._external_sources)
        try:
            from .discovery import PluginDiscoveryError, discover_builtin_plugin_instances

            discovered = discover_builtin_plugin_instances()
            if discovered.errors:
                raise PluginDiscoveryError(discovered.errors)
            for plugin in discovered.classes:
                self._register_unlocked(plugin)
                origin = discovered.origins.get(getattr(plugin, "id", ""))
                if origin:
                    self._external_sources[plugin.id] = origin
            self._loaded = True
        except Exception:
            self._plugins = snapshot
            self._external_sources = sources
            self._loaded = False
            raise
        finally:
            self._loading = False

    def _hot_reload_external(self) -> list[str]:
        """按 ID 替换外部插件实例，并卸下已删除文件对应的插件。

        ID 冲突或导入错误抛出前，把已经换上的实例和来源恢复回去。
        """
        from .base import BasePlugin
        from .discovery import PluginDiscoveryError, discover_external_plugins

        discovered = discover_external_plugins()
        snapshot_plugins = dict(self._plugins)
        snapshot_sources = dict(self._external_sources)
        snapshot_subclasses = dict(BasePlugin._subclasses)
        try:
            new_ids = self._commit_external_reload_unlocked(discovered)
            if discovered.errors:
                raise PluginDiscoveryError(discovered.errors)
            return new_ids
        except Exception:
            self._plugins = snapshot_plugins
            self._external_sources = snapshot_sources
            BasePlugin._subclasses.clear()
            BasePlugin._subclasses.update(snapshot_subclasses)
            raise

    def _commit_external_reload_unlocked(self, discovered) -> list[str]:
        new_ids: list[str] = []
        active = {
            cls.id: discovered.origins.get(cls.id, "")
            for cls in discovered.classes
            if getattr(cls, "id", None)
        }
        failed_paths = set(discovered.failed_paths)
        removed_paths = set(discovered.removed_paths)

        for cls in discovered.classes:
            pid = getattr(cls, "id", None)
            if not pid:
                continue
            existing = self._plugins.get(pid)
            if existing is not None and pid not in self._external_sources and type(existing) is not cls:
                raise ValueError(f"重复的插件 ID: {pid}")
            if existing is None:
                new_ids.append(pid)
            self._plugins[pid] = cls()
            origin = discovered.origins.get(pid)
            if origin:
                self._external_sources[pid] = origin

        for pid, path in list(self._external_sources.items()):
            if path in failed_paths:
                continue
            if path in removed_paths or pid not in active:
                self._unregister_unlocked(pid)
        return new_ids

    # 外部插件热重载

    def reload_plugins(self) -> list[str]:
        """热重载外部插件并返回新注册的 ID。导入失败会抛出，而不是假装成功。"""
        with plugin_load_lock:
            return self._hot_reload_external()

    # 自动触发发现的便捷查询

    def get_plugin_safe(self, plugin_id: str) -> BasePlugin | None:
        """与 ``get_plugin`` 相同，但首次调用前会自动发现插件。"""
        self._ensure_loaded()
        return self.get_plugin(plugin_id)

    def get_all_plugins_safe(self) -> list[BasePlugin]:
        """与 ``get_all_plugins`` 相同，但首次调用前会自动发现插件。"""
        self._ensure_loaded()
        return self.get_all_plugins()

    def __repr__(self) -> str:
        n = len(self._plugins)
        return f"<PluginRegistry ({n} plugins)>"


# 模块级单例故意不在导入时自动发现。需要完整插件集的调用方应使用
# ``get_plugin_safe`` / ``get_all_plugins_safe``；延迟加载可避免 Qt 设置构建器等
# 模块在导入阶段提前产生副作用。
registry = PluginRegistry()

__all__ = [
    "PluginRegistry",
    "plugin_load_lock",
    "registry",
]
