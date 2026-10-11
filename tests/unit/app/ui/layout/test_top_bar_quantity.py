"""顶栏数量不在预设档位时保留原值。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.ui.layout.top_bar import TopBarWidget


def test_quantity_outside_presets_keeps_original_value() -> None:
    app = QApplication.instance() or QApplication([])
    del app
    bar = TopBarWidget(is_dark_theme=False)
    bar.configure_for_platform("douyin", {"max_items": 17})

    assert bar.current_video_count() == 17

    bar.set_video_count(18)

    assert bar.current_video_count() == 18
    bar.deleteLater()


def test_bilibili_page_count_outside_presets_is_not_snapped() -> None:
    app = QApplication.instance() or QApplication([])
    del app
    bar = TopBarWidget(is_dark_theme=False)
    bar.configure_for_platform("bilibili", {"max_pages": 4, "max_items": 12})

    assert bar.quantity_mode() == "pages"
    assert bar.current_video_count() == 4
    bar.deleteLater()
