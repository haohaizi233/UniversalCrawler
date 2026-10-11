"""设置控件构建器的字段映射与布局测试。"""

import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

from app.ui.plugin_settings import (
    MissAVSettingsWidget,
    PageLimitSettingsWidget,
    read_bilibili_run_options,
    read_douyin_run_options,
    read_missav_run_options,
    read_plugin_run_options,
    read_xiaohongshu_run_options,
)

class SettingsBuildersTests(unittest.TestCase):
    """验证平台设置控件和运行参数读取逻辑。"""

    @classmethod
    def setUpClass(cls):
        
        cls.app = QApplication.instance() or QApplication([])

    def test_page_limit_widget_caps_values_to_max_entry(self):
        """验证 `test_page_limit_widget_caps_values_to_max_entry` 对应场景是否符合预期，供 `SettingsBuildersTests` 使用。"""
        widget = PageLimitSettingsWidget(
            label_text="页数:",
            max_pages=9999,
            default_pages=9999,
            tooltip="demo",
        )

        self.assertEqual(widget.current_value(), 9999)
        self.assertEqual(widget.combo_pages.currentText(), "max")

    def test_page_limit_widget_keeps_unknown_small_value(self):
        widget = PageLimitSettingsWidget(
            label_text="页数:",
            max_pages=20,
            default_pages=4,
            tooltip="demo",
        )

        self.assertEqual(widget.current_value(), 4)

    def test_page_limit_widget_keeps_value_missing_from_choices(self):
        widget = PageLimitSettingsWidget(
            label_text="视频数",
            max_pages=9999,
            default_pages=100,
            tooltip="demo",
            preset_values=[10, 20, 30, 50, 9999],
        )

        self.assertEqual(widget.current_value(), 100)

    @patch("app.ui.plugin_settings.cfg.set")
    def test_read_bilibili_run_options_reads_selected_pages_without_persisting(self, mocked_set):
        """验证 `test_read_bilibili_run_options_persists_selected_pages` 对应场景是否符合预期，供 `SettingsBuildersTests` 使用。"""
        widget = PageLimitSettingsWidget(
            label_text="页数:",
            max_pages=9999,
            default_pages=1,
            tooltip="demo",
            preset_values=[1, 2, 3, 5, 9999],
        )
        widget.set_current_value(3, 9999)

        result = read_bilibili_run_options(widget)

        self.assertEqual(result["max_pages"], 3)
        self.assertIn("max_items", result)
        self.assertIn("timeout", result)
        mocked_set.assert_not_called()

    @patch(
        "app.config.get_platform_runtime_defaults",
        return_value={"max_items": 12, "timeout": 45},
    )
    def test_read_douyin_run_options_returns_persisted_runtime_without_widget(self, runtime_defaults):
        result = read_douyin_run_options(None)

        self.assertEqual(result, {"max_items": 12, "timeout": 45})
        runtime_defaults.assert_called_with("douyin")
        self.assertEqual(read_plugin_run_options("douyin", None), {"max_items": 12, "timeout": 45})

    @patch(
        "app.config.get_platform_runtime_defaults",
        return_value={
            "max_items": 12,
            "search_max_pages": 2,
            "timeout": 40,
            "request_interval": 0.2,
            "detail_request_interval": 0.1,
            "sort": "popular",
            "note_type": "video",
        },
    )
    def test_read_xiaohongshu_run_options_returns_persisted_runtime_without_widget(self, runtime_defaults):
        result = read_xiaohongshu_run_options(None)

        self.assertEqual(result["max_items"], 12)
        self.assertEqual(result["sort"], "popular")
        self.assertEqual(result["note_type"], "video")
        self.assertEqual(result["request_interval"], 0.2)
        runtime_defaults.assert_called_with("xiaohongshu")

    @patch("app.ui.plugin_settings.cfg.update_missav_proxy")
    @patch("app.ui.plugin_settings.cfg.set")
    def test_read_missav_run_options_reads_form_without_persisting(self, mocked_set, mocked_update_proxy):
        """验证 `test_read_missav_run_options_updates_config_and_normalizes_proxy` 对应场景是否符合预期，供 `SettingsBuildersTests` 使用。"""
        widget = MissAVSettingsWidget()
        widget.chk_individual.setChecked(True)
        widget.combo_priority.setCurrentText("无码流出优先")
        widget.combo_timeout.setCurrentIndex(widget.combo_timeout.findData(120))
        widget.combo_proxy.setCurrentText("127.0.0.1:9001")

        result = read_missav_run_options(widget)

        self.assertEqual(result["individual_only"], True)
        self.assertEqual(result["priority"], "无码流出优先")
        self.assertEqual(result["timeout"], 120)
        self.assertEqual(result["proxy"], "http://127.0.0.1:9001")
        self.assertIn("max_items", result)
        mocked_set.assert_not_called()
        mocked_update_proxy.assert_not_called()

    @patch(
        "app.config.get_platform_runtime_defaults",
        return_value={
            "individual_only": False,
            "priority": "中文字幕优先",
            "timeout": 60,
            "proxy": "http://127.0.0.1:7890",
            "max_items": 20,
            "search_max_pages": 5,
        },
    )
    def test_read_missav_run_options_returns_persisted_runtime_for_invalid_widget(self, runtime_defaults):
        result = read_missav_run_options(object())

        self.assertEqual(result["individual_only"], False)
        self.assertEqual(result["priority"], "中文字幕优先")
        self.assertEqual(result["timeout"], 60)
        self.assertEqual(result["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(result["max_items"], 20)
        self.assertEqual(result["search_max_pages"], 5)
        runtime_defaults.assert_called_with("missav")

    @patch(
        "app.config.get_platform_runtime_defaults",
        return_value={
            "individual_only": True,
            "priority": "无码流出优先",
            "timeout": 120,
            "proxy": "http://127.0.0.1:7897",
            "max_items": 30,
        },
    )
    def test_read_missav_run_options_uses_saved_runtime_proxy_without_widget(self, runtime_defaults):
        """主窗口未挂载旧平台控件时，仍应使用配置中心保存的代理。"""
        result = read_missav_run_options(None)

        self.assertEqual(result["proxy"], "http://127.0.0.1:7897")
        self.assertTrue(result["individual_only"])
        self.assertEqual(result["priority"], "无码流出优先")
        self.assertEqual(result["timeout"], 120)
        runtime_defaults.assert_called_once_with("missav")

if __name__ == "__main__":
    unittest.main()
