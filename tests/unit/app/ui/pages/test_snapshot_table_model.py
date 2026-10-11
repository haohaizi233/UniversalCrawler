import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QStyle, QStyleOptionViewItem

from app.ui.pages.common import SnapshotActionDelegate
from app.ui.pages.active_downloads_page import ActiveDownloadsModel
from app.ui.styles.table_rows import normalize_table_item_option, row_interaction_fill_color, selection_fill_color
from app.ui.styles.themes import build_palette
from app.ui.viewmodels.snapshot_table_model import SnapshotTableModel


class SnapshotTableModelTests(unittest.TestCase):
    def test_set_rows_appends_tail_without_model_reset(self):
        model = SnapshotTableModel(headers=["Title"], columns=["title"])
        model.set_rows([{"id": "v1", "title": "one"}])
        resets: list[bool] = []
        inserted: list[tuple[int, int]] = []
        model.modelReset.connect(lambda: resets.append(True))
        model.rowsInserted.connect(lambda _parent, first, last: inserted.append((first, last)))

        changed = model.set_rows(
            [
                {"id": "v1", "title": "one"},
                {"id": "v2", "title": "two"},
                {"id": "v3", "title": "three"},
            ]
        )

        self.assertTrue(changed)
        self.assertEqual(resets, [])
        self.assertEqual(inserted, [(1, 2)])
        self.assertEqual(model.id_order(), ["v1", "v2", "v3"])

    def test_set_rows_removes_tail_without_model_reset(self):
        model = SnapshotTableModel(headers=["Title"], columns=["title"])
        model.set_rows(
            [
                {"id": "v1", "title": "one"},
                {"id": "v2", "title": "two"},
                {"id": "v3", "title": "three"},
            ]
        )
        resets: list[bool] = []
        removed: list[tuple[int, int]] = []
        model.modelReset.connect(lambda: resets.append(True))
        model.rowsRemoved.connect(lambda _parent, first, last: removed.append((first, last)))

        changed = model.set_rows([{"id": "v1", "title": "one"}])

        self.assertTrue(changed)
        self.assertEqual(resets, [])
        self.assertEqual(removed, [(1, 2)])
        self.assertEqual(model.id_order(), ["v1"])

    def test_set_rows_removes_middle_and_pulls_next_row_without_model_reset(self):
        model = SnapshotTableModel(headers=["Title"], columns=["title"])
        model.set_rows(
            [
                {"id": "v1", "title": "one"},
                {"id": "v2", "title": "two"},
                {"id": "v3", "title": "three"},
            ]
        )
        resets: list[bool] = []
        removed: list[tuple[int, int]] = []
        inserted: list[tuple[int, int]] = []
        model.modelReset.connect(lambda: resets.append(True))
        model.rowsRemoved.connect(lambda _parent, first, last: removed.append((first, last)))
        model.rowsInserted.connect(lambda _parent, first, last: inserted.append((first, last)))

        changed = model.set_rows(
            [
                {"id": "v1", "title": "one"},
                {"id": "v3", "title": "three"},
                {"id": "v4", "title": "four"},
            ]
        )

        self.assertTrue(changed)
        self.assertEqual(resets, [])
        self.assertEqual(removed, [(1, 1)])
        self.assertEqual(inserted, [(2, 2)])
        self.assertEqual(model.id_order(), ["v1", "v3", "v4"])

    def test_set_rows_patches_existing_rows_without_model_reset(self):
        model = SnapshotTableModel(headers=["Title"], columns=["title"])
        model.set_rows([{"id": "v1", "title": "old"}])
        resets: list[bool] = []
        changed_rows: list[tuple[int, int]] = []
        model.modelReset.connect(lambda: resets.append(True))
        model.dataChanged.connect(lambda first, last, _roles: changed_rows.append((first.row(), last.row())))

        changed = model.set_rows([{"id": "v1", "title": "new"}])

        self.assertTrue(changed)
        self.assertEqual(resets, [])
        self.assertEqual(changed_rows, [(0, 0)])
        self.assertEqual(model.row_at(0), {"id": "v1", "title": "new"})

    def test_set_rows_reorders_same_ids_without_model_reset(self):
        model = SnapshotTableModel(headers=["Title"], columns=["title"])
        model.set_rows(
            [
                {"id": "v1", "title": "one"},
                {"id": "v2", "title": "two"},
                {"id": "v3", "title": "three"},
            ]
        )
        resets: list[bool] = []
        layouts: list[bool] = []
        model.modelReset.connect(lambda: resets.append(True))
        model.layoutChanged.connect(lambda: layouts.append(True))

        changed = model.set_rows(
            [
                {"id": "v3", "title": "three"},
                {"id": "v1", "title": "one"},
                {"id": "v2", "title": "two"},
            ]
        )

        self.assertTrue(changed)
        self.assertEqual(resets, [])
        self.assertEqual(layouts, [True])
        self.assertEqual(model.id_order(), ["v3", "v1", "v2"])

    def test_active_downloads_model_reorders_same_ids_without_model_reset(self):
        model = ActiveDownloadsModel()
        model.set_rows(
            [
                {"id": "v1", "title": "one", "platform": "Bilibili", "progress": 10},
                {"id": "v2", "title": "two", "platform": "Bilibili", "progress": 20},
            ]
        )
        resets: list[bool] = []
        layouts: list[bool] = []
        model.modelReset.connect(lambda: resets.append(True))
        model.layoutChanged.connect(lambda: layouts.append(True))

        changed = model.set_rows(
            [
                {"id": "v2", "title": "two", "platform": "Bilibili", "progress": 20},
                {"id": "v1", "title": "one", "platform": "Bilibili", "progress": 10},
            ]
        )

        self.assertTrue(changed)
        self.assertEqual(resets, [])
        self.assertEqual(layouts, [True])
        self.assertEqual(model.item_id_at(0), "v2")
        self.assertEqual(model.item_id_at(1), "v1")

    def test_force_reset_prevents_duplicate_append_after_signature_clear(self):
        model = SnapshotTableModel(headers=["Title"], columns=["title"])
        rows = [{"id": "v1", "title": "one"}]
        model.set_rows(rows)
        model.force_reset()
        resets: list[bool] = []
        inserted: list[tuple[int, int]] = []
        model.modelReset.connect(lambda: resets.append(True))
        model.rowsInserted.connect(lambda _parent, first, last: inserted.append((first, last)))

        changed = model.set_rows(rows)

        self.assertTrue(changed)
        self.assertEqual(resets, [True])
        self.assertEqual(inserted, [])
        self.assertEqual(model.id_order(), ["v1"])

    def test_decoration_role_caches_loaded_icons(self):
        model = SnapshotTableModel(
            headers=["Platform"],
            columns=["platform"],
            icon_columns={"platform"},
        )
        model.set_rows([{"id": "v1", "platform": "Douyin", "platform_id": "douyin"}])
        icon = QIcon()

        with patch("app.ui.viewmodels.snapshot_table_model.load_qt_icon", Mock(return_value=icon)) as loader:
            index = model.index(0, 0)
            self.assertIs(model.data(index, Qt.ItemDataRole.DecorationRole), icon)
            self.assertIs(model.data(index, Qt.ItemDataRole.DecorationRole), icon)

        loader.assert_called_once()

    def test_decoration_role_remembers_missing_icons(self):
        model = SnapshotTableModel(
            headers=["Status"],
            columns=["status"],
            icon_columns={"status"},
        )
        model.set_rows([{"id": "v1", "status": "missing"}])

        with patch("app.ui.viewmodels.snapshot_table_model.load_qt_icon", Mock(return_value=None)) as loader:
            index = model.index(0, 0)
            self.assertIsNone(model.data(index, Qt.ItemDataRole.DecorationRole))
            self.assertIsNone(model.data(index, Qt.ItemDataRole.DecorationRole))

        loader.assert_called_once()

    def test_action_delegate_caches_action_icons(self):
        delegate = SnapshotActionDelegate(
            progress_columns=set(),
            icon_columns=set(),
            title_columns=set(),
            action_column=0,
            action_ids=("delete",),
        )
        icon = QIcon()

        with patch("app.ui.pages.common.load_qt_icon", Mock(return_value=icon)) as loader:
            self.assertIs(delegate._action_icon("delete"), icon)
            self.assertIs(delegate._action_icon("delete"), icon)

        loader.assert_called_once()

    def test_action_delegate_remembers_missing_action_icons(self):
        delegate = SnapshotActionDelegate(
            progress_columns=set(),
            icon_columns=set(),
            title_columns=set(),
            action_column=0,
            action_ids=("delete",),
        )

        with patch("app.ui.pages.common.load_qt_icon", Mock(return_value=None)) as loader:
            self.assertIsNone(delegate._action_icon("delete"))
            self.assertIsNone(delegate._action_icon("delete"))

        loader.assert_called_once()

    def test_table_item_option_removes_focus_rect_even_when_selected(self):
        option = QStyleOptionViewItem()
        option.state |= QStyle.StateFlag.State_Selected
        option.state |= QStyle.StateFlag.State_HasFocus

        normalize_table_item_option(option)

        self.assertTrue(option.state & QStyle.StateFlag.State_Selected)
        self.assertFalse(option.state & QStyle.StateFlag.State_HasFocus)

    def test_table_item_option_removes_native_hover_overlay(self):
        option = QStyleOptionViewItem()
        option.state |= QStyle.StateFlag.State_Selected
        option.state |= QStyle.StateFlag.State_MouseOver

        normalize_table_item_option(option)

        self.assertTrue(option.state & QStyle.StateFlag.State_Selected)
        self.assertFalse(option.state & QStyle.StateFlag.State_MouseOver)

    def test_table_row_interaction_selection_wins_over_hover(self):
        option = QStyleOptionViewItem()
        option.palette = build_palette(False)
        option.state |= QStyle.StateFlag.State_Selected
        option.state |= QStyle.StateFlag.State_MouseOver

        self.assertEqual(row_interaction_fill_color(option), selection_fill_color(option))

    def test_active_downloads_middle_insert_and_delete_keep_stable_ids(self):
        model = ActiveDownloadsModel()
        model.set_rows(
            [
                {"id": "a", "title": "A", "platform": "Bilibili", "progress": 10, "speed": "1", "remaining_time": "9"},
                {"id": "c", "title": "C", "platform": "Bilibili", "progress": 30, "speed": "3", "remaining_time": "7"},
            ]
        )
        resets: list[bool] = []
        inserted: list[tuple[int, int]] = []
        removed: list[tuple[int, int]] = []
        model.modelReset.connect(lambda: resets.append(True))
        model.rowsInserted.connect(lambda _parent, first, last: inserted.append((first, last)))
        model.rowsRemoved.connect(lambda _parent, first, last: removed.append((first, last)))

        inserted_changed = model.set_rows(
            [
                {"id": "a", "title": "A", "platform": "Bilibili", "progress": 10, "speed": "1", "remaining_time": "9"},
                {"id": "b", "title": "B", "platform": "Bilibili", "progress": 20, "speed": "2", "remaining_time": "8"},
                {"id": "c", "title": "C", "platform": "Bilibili", "progress": 30, "speed": "3", "remaining_time": "7"},
            ]
        )

        self.assertTrue(inserted_changed)
        self.assertEqual(resets, [])
        self.assertEqual(inserted, [(1, 1)])
        self.assertEqual([model.item_id_at(index) for index in range(3)], ["a", "b", "c"])

        removed_changed = model.set_rows(
            [
                {"id": "a", "title": "A", "platform": "Bilibili", "progress": 10, "speed": "1", "remaining_time": "9"},
                {"id": "c", "title": "C", "platform": "Bilibili", "progress": 30, "speed": "3", "remaining_time": "7"},
            ]
        )

        self.assertTrue(removed_changed)
        self.assertEqual(resets, [])
        self.assertEqual(removed, [(1, 1)])
        self.assertEqual([model.item_id_at(index) for index in range(2)], ["a", "c"])

    def test_active_downloads_progress_update_touches_only_progress_cells(self):
        model = ActiveDownloadsModel()
        model.set_rows(
            [
                {"id": "a", "title": "A", "platform": "Bilibili", "progress": 10, "speed": "1", "remaining_time": "9"},
                {"id": "b", "title": "B", "platform": "Bilibili", "progress": 20, "speed": "2", "remaining_time": "8"},
            ]
        )
        resets: list[bool] = []
        changed: list[tuple[int, int]] = []
        model.modelReset.connect(lambda: resets.append(True))
        model.dataChanged.connect(
            lambda top, bottom, _roles: changed.append((top.row(), top.column(), bottom.column()))
        )

        model.update_progress_rows(
            [{"id": "b", "title": "replaced", "progress": 55, "speed": "9MB/s", "remaining_time": "1s"}]
        )

        self.assertEqual(resets, [])
        self.assertEqual(changed, [(1, ActiveDownloadsModel.PROGRESS_COLUMN, ActiveDownloadsModel.PROGRESS_COLUMN + 2)])
        self.assertEqual(model.data(model.index(1, 0)), "B")
        self.assertEqual(model.data(model.index(1, 2)), 55)
        self.assertEqual(model.data(model.index(1, 3)), "9MB/s")
        self.assertEqual(model.data(model.index(0, 2)), 10)

    def test_active_downloads_blank_or_duplicate_ids_do_not_reset_model(self):
        model = ActiveDownloadsModel()
        model.set_rows(
            [
                {"id": "", "title": "A", "platform": "Bilibili", "progress": 10, "speed": "1", "remaining_time": "9"},
                {"id": "same", "title": "B", "platform": "Bilibili", "progress": 20, "speed": "2", "remaining_time": "8"},
                {"id": "same", "title": "C", "platform": "Bilibili", "progress": 30, "speed": "3", "remaining_time": "7"},
            ]
        )
        resets: list[bool] = []
        model.modelReset.connect(lambda: resets.append(True))

        changed = model.set_rows(
            [
                {"id": "same", "title": "C", "platform": "Bilibili", "progress": 31, "speed": "3", "remaining_time": "7"},
                {"id": "", "title": "A", "platform": "Bilibili", "progress": 11, "speed": "1", "remaining_time": "9"},
                {"id": "same", "title": "B", "platform": "Bilibili", "progress": 21, "speed": "2", "remaining_time": "8"},
            ]
        )

        self.assertTrue(changed)
        self.assertEqual(resets, [])
        self.assertEqual(model.data(model.index(0, 0)), "C")
        self.assertEqual(model.data(model.index(0, 2)), 31)
        self.assertEqual(model.data(model.index(1, 0)), "A")
        self.assertEqual(model.data(model.index(1, 2)), 11)
        self.assertEqual(model.data(model.index(2, 0)), "B")
        self.assertEqual(model.data(model.index(2, 2)), 21)


if __name__ == "__main__":
    unittest.main()
