"""由插件系统驱动的下载器注册表。

注册表通过 ``BasePlugin.get_downloader_class()`` 收集实现；每个插件只需覆盖
一次该方法，下载分发层便无需维护硬编码导入清单。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import VideoItem
    from .base import BaseDownloader


class DownloaderRegistry:
    """按需构建插件下载器类索引。"""

    def __init__(self) -> None:
        self._downloaders: dict[str, type[BaseDownloader]] = {}
        self._loaded = False
        self._loading = False

    def _ensure_loaded(self) -> None:
        from app.core.plugins.registry import plugin_load_lock

        with plugin_load_lock:
            self._ensure_loaded_unlocked()

    def _ensure_loaded_unlocked(self) -> None:
        if self._loaded or self._loading:
            return
        from app.core.plugin_registry import registry

        snapshot = dict(self._downloaders)
        self._loading = True
        try:
            plugins = registry.get_all_plugins_safe()
            if not registry._loaded:
                return
            for plugin in plugins:
                downloader_cls = plugin.get_downloader_class()
                if downloader_cls is not None:
                    sid = getattr(downloader_cls, "source_id", None) or plugin.id
                    existing = self._downloaders.get(sid)
                    if existing and existing is not downloader_cls:
                        raise ValueError(f"重复的下载器 source_id: {sid}")
                    self._downloaders[sid] = downloader_cls
            self._loaded = True
        except Exception:
            self._downloaders = snapshot
            self._loaded = False
            raise
        finally:
            self._loading = False

    def register(self, downloader_cls: type[BaseDownloader]) -> None:
        from app.core.plugins.registry import plugin_load_lock

        with plugin_load_lock:
            sid = getattr(downloader_cls, "source_id", None)
            if not sid:
                return
            existing = self._downloaders.get(sid)
            if existing and existing is not downloader_cls:
                raise ValueError(f"重复的下载器 source_id: {sid}")
            self._downloaders[sid] = downloader_cls

    def get(self, source_id: str) -> type[BaseDownloader] | None:
        from app.core.plugins.registry import plugin_load_lock

        with plugin_load_lock:
            self._ensure_loaded_unlocked()
            return self._downloaders.get(source_id)

    def all(self) -> list[type[BaseDownloader]]:
        from app.core.plugins.registry import plugin_load_lock

        with plugin_load_lock:
            self._ensure_loaded_unlocked()
            return list(self._downloaders.values())

    def resolve(self, video_item: VideoItem) -> type[BaseDownloader] | None:
        """查找 ``can_handle()`` 能处理 ``video_item`` 的下载器。"""
        from app.core.plugins.registry import plugin_load_lock

        with plugin_load_lock:
            self._ensure_loaded_unlocked()
            for downloader_cls in self._downloaders.values():
                if downloader_cls.can_handle(video_item):
                    return downloader_cls
            return None


downloader_registry = DownloaderRegistry()
