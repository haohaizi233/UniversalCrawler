"""设置控件构建器的字段映射与布局测试。"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PyQt6.QtWidgets import QApplication, QComboBox

from app.ui.components.settings_platform_controls import (
    build_platform_count_controls,
    build_platform_search_pages_combo,
)
from app.ui.viewmodels.settings_options import normalize_combo_options

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

    def test_saved_scan_page_change_updates_existing_run_widget_without_quantity_change(self):
        from app.ui.main_window import MainWindow

        for pages in (2, 7):
            with self.subTest(pages=pages):
                widget = MissAVSettingsWidget()
                combo = widget.combo_search_max_pages
                combo.setCurrentIndex(combo.findData(1))
                emitted = []
                combo.currentIndexChanged.connect(emitted.append)
                window = SimpleNamespace(
                    current_plugin=SimpleNamespace(id="missav"),
                    plugin_widget=widget,
                    top_bar=Mock(),
                )

                MainWindow._apply_platform_runtime_setting(window, "missav", "search_max_pages", pages)

                self.assertEqual(combo.currentData(), pages)
                self.assertEqual(emitted, [])
                window.top_bar.configure_for_platform.assert_not_called()
                window.top_bar.set_video_count.assert_not_called()
                with patch("app.core.plugins.settings_builders._runtime_options", return_value={
                    "search_max_pages": pages, "max_items": 20,
                }):
                    options = read_missav_run_options(widget)
                self.assertEqual(options["search_max_pages"], pages)
                self.assertEqual(options["max_items"], 20)
                widget.close()

    @staticmethod
    def _platform_combo(options, current, *, width=0):
        combo = QComboBox()
        if width:
            combo.setFixedWidth(width)
        for value, label in normalize_combo_options(options, current):
            combo.addItem(label, value)
        index = combo.findData(str(current))
        if index >= 0:
            combo.setCurrentIndex(index)
        return combo

    def test_platform_scan_pages_combo_keeps_current_one_and_emits_independently(self):
        changes = []
        row = {
            "id": "missav",
            "search_max_pages": 1,
            "search_max_pages_config_key": "search_max_pages",
            "search_max_pages_editable": True,
            "search_max_pages_options": [
                {"value": str(value), "label": f"{value} 页"}
                for value in (1, 2, 3, 5, 10, 20, 50, 100)
            ],
        }
        combo = build_platform_search_pages_combo(
            row,
            build_combo=self._platform_combo,
            emit_setting_changed=lambda *args: changes.append(args),
            translate=str,
        )

        self.assertEqual(combo.currentData(), "1")
        combo.setCurrentIndex(combo.findData("2"))
        self.assertEqual(changes, [("missav", "search_max_pages", 2)])

    def test_platform_count_controls_keep_video_count_and_scan_pages_separate(self):
        changes = []
        row = {
            "id": "missav",
            "default_count": 20,
            "count_config_key": "max_items",
            "count_editable": True,
            "count_options": [{"value": "20", "label": "20 个视频"}],
            "search_max_pages": 1,
            "search_max_pages_config_key": "search_max_pages",
            "search_max_pages_editable": True,
            "search_max_pages_options": [{"value": "1", "label": "1 页"}],
        }

        controls = build_platform_count_controls(
            row,
            build_combo=self._platform_combo,
            emit_setting_changed=lambda *args: changes.append(args),
            translate=str,
            width=210,
        )

        combos = controls.findChildren(QComboBox)
        self.assertEqual(len(combos), 2)
        self.assertEqual(combos[0].currentData(), "20")
        self.assertEqual(combos[1].currentData(), "1")
        self.assertTrue(combos[1].property("platformSearchPagesControl"))
        self.assertEqual(controls.objectName(), "SettingsPlatformCountControls")

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

    def test_missav_scan_page_control_keeps_saved_one_and_custom_value(self):
        for saved_pages in (1, 4):
            with self.subTest(saved_pages=saved_pages):
                runtime_options = {
                    "individual_only": False,
                    "priority": "中文字幕优先",
                    "timeout": 60,
                    "proxy": "http://127.0.0.1:7890",
                    "max_items": 20,
                    "search_max_pages": saved_pages,
                }
                config_values = {
                    "individual_only": False,
                    "priority": "中文字幕优先",
                    "proxy_app": "Clash (7890)",
                }
                with (
                    patch("app.ui.plugin_settings.cfg.get", side_effect=lambda _section, key, default=None: config_values.get(key, default)),
                    patch("app.ui.plugin_settings.get_platform_runtime_defaults", return_value=runtime_options),
                    patch("app.core.plugins.settings_builders._runtime_options", return_value=runtime_options),
                ):
                    widget = MissAVSettingsWidget()
                    self.assertEqual(widget.label_search_max_pages.text(), "扫描页数:")
                    self.assertEqual(widget.combo_search_max_pages.currentData(), saved_pages)
                    self.assertGreaterEqual(widget.combo_search_max_pages.findData(saved_pages), 0)
                    self.assertEqual(
                        [widget.combo_search_max_pages.itemData(index) for index in range(widget.combo_search_max_pages.count())],
                        [1, 2, 3, 5, 10, 20, 50, 100, *([saved_pages] if saved_pages == 4 else [])],
                    )

                    widget.combo_search_max_pages.setCurrentIndex(widget.combo_search_max_pages.findData(100))
                    result = read_missav_run_options(widget)

                    self.assertEqual(result["search_max_pages"], 100)
                    self.assertEqual(result["max_items"], 20)

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
