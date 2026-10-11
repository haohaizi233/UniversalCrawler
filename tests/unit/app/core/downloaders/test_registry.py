import unittest

from app.core.downloaders.douyin import DouyinDownloader
from app.core.downloaders.registry import downloader_registry
from app.models import VideoItem

class DownloaderRegistryTests(unittest.TestCase):
    def test_builtin_registry_resolves_by_video_source(self):
        item = VideoItem(url="https://example.com/video.mp4", title="demo", source="douyin")

        downloader_cls = downloader_registry.resolve(item)

        self.assertIs(downloader_cls, DouyinDownloader)

    def test_builtin_registry_lists_expected_sources(self):
        source_ids = {cls.source_id for cls in downloader_registry.all()}

        self.assertTrue({"douyin", "xiaohongshu", "kuaishou", "missav", "bilibili"}.issubset(source_ids))

    def test_failed_load_stays_unloaded_until_retry_succeeds(self):
        from unittest.mock import patch

        from app.core.downloaders.registry import DownloaderRegistry

        class _FakeRegistry:
            def __init__(self) -> None:
                self._loaded = True
                self.calls = 0

            def get_all_plugins_safe(self):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("discovery failed")
                return []

        fake = _FakeRegistry()
        registry = DownloaderRegistry()
        registry._downloaders["preset"] = object()
        with patch("app.core.plugin_registry.registry", fake):
            with self.assertRaises(RuntimeError):
                registry.get("douyin")
            self.assertFalse(registry._loaded)
            self.assertNotIn("douyin", registry._downloaders)
            self.assertIsNone(registry.get("douyin"))

        self.assertTrue(registry._loaded)
        self.assertEqual(fake.calls, 2)

