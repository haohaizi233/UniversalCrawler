"""正在下载页表格模型。"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt6.QtWidgets import QWidget

from shared.localization import (
    is_translation_of,
    normalize_language,
    platform_display_name,
    source_text_for_translation,
    tr,
)

class ActiveDownloadsModel(QAbstractTableModel):
    HEADERS = ["\u6807\u9898", "\u5e73\u53f0", "\u8fdb\u5ea6", "\u901f\u5ea6", "\u5269\u4f59\u65f6\u95f4", "\u64cd\u4f5c"]
    COLUMNS = ["title", "platform", "progress", "speed", "remaining_time", "actions"]
    PROGRESS_COLUMN = 2
    ACTION_COLUMN = 5
    ROW_ROLE = Qt.ItemDataRole.UserRole

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []
        self._row_signatures: list[tuple] = []
        self._language = "zh-CN"
        self._headers = list(self.HEADERS)

    def rowCount(self, _parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008 - Qt 重写签名
        return len(self._rows)

    def columnCount(self, _parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008 - Qt 重写签名
        return len(self.COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return tr(self._headers[section], self._language)
        return super().headerData(section, orientation, role)

    def set_headers(self, headers: list[str]) -> None:
        if len(headers) != len(self._headers):
            return
        headers = self._source_headers_from_display(headers)
        if headers == self._headers:
            return
        self._headers = headers
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(self._headers) - 1)

    def _source_headers_from_display(self, headers: list[str]) -> list[str]:
        normalized: list[str] = []
        for index, header in enumerate(headers):
            text = str(header or "")
            current_source = self._headers[index] if index < len(self._headers) else ""
            if current_source and is_translation_of(text, current_source):
                normalized.append(current_source)
            else:
                normalized.append(source_text_for_translation(text))
        return normalized

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        row = self._rows[index.row()]
        column = self.COLUMNS[index.column()]
        if role == self.ROW_ROLE:
            return row
        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = Qt.AlignmentFlag.AlignLeft if index.column() == 0 else Qt.AlignmentFlag.AlignCenter
            return Qt.AlignmentFlag.AlignVCenter | align
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(row.get(column, ""))
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if index.column() == self.PROGRESS_COLUMN:
            return int(row.get("progress") or 0)
        if index.column() == self.ACTION_COLUMN:
            from app.ui.pages.active_downloads_page import TEXT

            return tr(TEXT["delete"], self._language)
        if column == "platform":
            return platform_display_name(row.get("platform_id"), self._language, fallback=row.get(column, ""))
        return str(row.get(column, ""))

    def set_language(self, language: str | None) -> None:
        normalized = normalize_language(language)
        if normalized == self._language:
            return
        self._language = normalized
        if self._headers:
            self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(self._headers) - 1)
        if self._rows:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._rows) - 1, max(0, self.columnCount() - 1)),
                [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole],
            )

    def set_rows(self, rows: list[dict[str, Any]]) -> bool:
        rows = list(rows)
        keys = self._stable_row_keys(rows)
        signatures = [self._row_signature(row, key) for row, key in zip(rows, keys, strict=False)]
        if signatures == self._row_signatures:
            return False
        new_ids = [signature[0] for signature in signatures]
        old_ids = [signature[0] for signature in self._row_signatures]
        same_shape = new_ids == old_ids
        if same_shape and len(rows) == len(self._rows):
            self._replace_existing_rows(rows, signatures)
        elif len(rows) > len(self._rows) and new_ids[: len(old_ids)] == old_ids:
            self._replace_existing_rows(rows[: len(self._rows)], signatures[: len(self._rows)])
            first = len(self._rows)
            last = len(rows) - 1
            self.beginInsertRows(QModelIndex(), first, last)
            self._rows.extend(rows[first:])
            self._row_signatures.extend(signatures[first:])
            self.endInsertRows()
            return True
        elif len(rows) < len(self._rows) and old_ids[: len(new_ids)] == new_ids:
            self._replace_existing_rows(rows, signatures)
            first = len(rows)
            last = len(self._rows) - 1
            self.beginRemoveRows(QModelIndex(), first, last)
            del self._rows[first:]
            del self._row_signatures[first:]
            self.endRemoveRows()
            return True
        elif self._has_same_unique_ids(old_ids, new_ids):
            self.layoutAboutToBeChanged.emit()
            self._rows = rows
            self._row_signatures = signatures
            self.layoutChanged.emit()
        elif self._has_unique_ids(old_ids) and self._has_unique_ids(new_ids):
            self._reconcile_unique_rows(rows, signatures, new_ids)
        else:
            self.beginResetModel()
            self._rows = rows
            self._row_signatures = signatures
            self.endResetModel()
        return True

    def _reconcile_unique_rows(
        self,
        rows: list[dict[str, Any]],
        signatures: list[tuple],
        new_ids: list[Any],
    ) -> None:
        """中间行按稳定 ID 插入或删除，避免 beginResetModel 把滚动位置打回顶部。"""

        new_id_set = set(new_ids)
        removed_indexes = [
            index
            for index, signature in enumerate(self._row_signatures)
            if signature[0] not in new_id_set
        ]
        for first, last in reversed(self._contiguous_ranges(removed_indexes)):
            self.beginRemoveRows(QModelIndex(), first, last)
            del self._rows[first : last + 1]
            del self._row_signatures[first : last + 1]
            self.endRemoveRows()

        rows_by_id = {
            str(signature[0]): (row, signature)
            for row, signature in zip(rows, signatures, strict=False)
        }
        current_ids = [signature[0] for signature in self._row_signatures]
        current_id_set = set(current_ids)
        for target_index, item_id in enumerate(new_ids):
            if item_id in current_id_set:
                continue
            incoming = rows_by_id.get(item_id)
            if incoming is None:
                continue
            row, signature = incoming
            insertion_index = min(target_index, len(self._rows))
            self.beginInsertRows(QModelIndex(), insertion_index, insertion_index)
            self._rows.insert(insertion_index, row)
            self._row_signatures.insert(insertion_index, signature)
            self.endInsertRows()
            current_ids.insert(insertion_index, item_id)
            current_id_set.add(item_id)

        current_ids = [signature[0] for signature in self._row_signatures]
        if current_ids != new_ids:
            self.layoutAboutToBeChanged.emit()
            self._rows = list(rows)
            self._row_signatures = list(signatures)
            self.layoutChanged.emit()
            return
        self._replace_existing_rows(rows, signatures)

    def update_progress_rows(self, rows: list[dict[str, Any]]) -> None:
        """只刷新进度、速度和剩余时间，不重置勾选或并发控件。"""

        incoming = {
            str(row.get("id") or ""): row
            for row in rows
            if str(row.get("id") or "")
        }
        if not incoming or not self._rows:
            return
        changed_roles = [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole, self.ROW_ROLE]
        progress_fields = ("progress", "speed", "remaining_time")
        for row_index, current in enumerate(self._rows):
            item_id = str(current.get("id") or "")
            update = incoming.get(item_id)
            if update is None:
                continue
            changed = False
            for field in progress_fields:
                if field not in update:
                    continue
                value = int(update.get(field) or 0) if field == "progress" else str(update.get(field) or "")
                if current.get(field) != value:
                    current[field] = value
                    changed = True
            if not changed:
                continue
            if row_index < len(self._row_signatures):
                previous_key = str(self._row_signatures[row_index][0])
                self._row_signatures[row_index] = self._row_signature(current, previous_key)
            self.dataChanged.emit(
                self.index(row_index, self.PROGRESS_COLUMN),
                self.index(row_index, self.PROGRESS_COLUMN + 2),
                changed_roles,
            )

    def _replace_existing_rows(self, rows: list[dict[str, Any]], signatures: list[tuple]) -> None:
        changed_roles = [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole, self.ROW_ROLE]
        for row_index, (row, signature) in enumerate(zip(rows, signatures, strict=False)):
            if row_index >= len(self._rows):
                return
            old = self._row_signatures[row_index]
            self._rows[row_index] = row
            self._row_signatures[row_index] = signature
            if old == signature:
                continue
            start = self.index(row_index, 0)
            end = self.index(row_index, self.columnCount() - 1)
            self.dataChanged.emit(start, end, changed_roles)

    def row_at(self, row: int) -> dict[str, Any] | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def item_id_at(self, row: int) -> str:
        item = self.row_at(row)
        return str((item or {}).get("id") or "")

    def row_for_id(self, item_id: str) -> int:
        for row, item in enumerate(self._rows):
            if item.get("id") == item_id:
                return row
        return -1

    @staticmethod
    def _row_signature(row: dict[str, Any], key: str | None = None) -> tuple:
        identity = str(row.get("id") or "") if key is None else key
        return (
            identity,
            str(row.get("title", "")),
            str(row.get("platform", "")),
            int(row.get("progress") or 0),
            str(row.get("speed", "")),
            str(row.get("remaining_time", "")),
        )

    def _stable_row_keys(self, rows: list[dict[str, Any]]) -> list[str]:
        """空 ID 用行内容做稳定键，重复 ID 加后缀区分，避免整表重置。"""
        bases = [self._base_row_key(row) for row in rows]
        totals: dict[str, int] = {}
        for base in bases:
            totals[base] = totals.get(base, 0) + 1
        seen: dict[str, int] = {}
        used: set[str] = set()
        keys: list[str] = []
        for row, base in zip(rows, bases, strict=False):
            if totals[base] == 1 and base not in used:
                keys.append(base)
                used.add(base)
                continue
            seen[base] = seen.get(base, 0) + 1
            distinguished = f"{base}#{self._content_key(row)}"
            if distinguished in used:
                distinguished = f"{distinguished}#{seen[base]}"
            while distinguished in used:
                seen[base] += 1
                distinguished = f"{base}#{self._content_key(row)}#{seen[base]}"
            keys.append(distinguished)
            used.add(distinguished)
        return keys

    def _base_row_key(self, row: dict[str, Any]) -> str:
        raw_id = str(row.get("id") or "").strip()
        if raw_id:
            return raw_id
        return f"row:{self._content_key(row)}"

    @staticmethod
    def _content_key(row: dict[str, Any]) -> str:
        volatile = {"id", "progress", "speed", "remaining_time"}
        parts = [f"{key}={row[key]}" for key in sorted(row) if key not in volatile]
        if not parts:
            parts = [f"title={row.get('title', '')}", f"platform={row.get('platform', '')}"]
        return "|".join(parts) or "row"

    @staticmethod
    def _has_same_unique_ids(old_ids: list[Any], new_ids: list[Any]) -> bool:
        if len(old_ids) != len(new_ids) or not old_ids:
            return False
        if any(not item_id for item_id in old_ids) or any(not item_id for item_id in new_ids):
            return False
        return len(set(old_ids)) == len(old_ids) and set(old_ids) == set(new_ids)

    @staticmethod
    def _has_unique_ids(item_ids: list[Any]) -> bool:
        return bool(item_ids) and all(item_ids) and len(set(item_ids)) == len(item_ids)

    @staticmethod
    def _contiguous_ranges(indexes: list[int]) -> list[tuple[int, int]]:
        if not indexes:
            return []
        ranges: list[tuple[int, int]] = []
        first = previous = indexes[0]
        for index in indexes[1:]:
            if index == previous + 1:
                previous = index
                continue
            ranges.append((first, previous))
            first = previous = index
        ranges.append((first, previous))
        return ranges
