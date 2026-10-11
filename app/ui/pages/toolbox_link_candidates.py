from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.ui.pages.toolbox_models import action_enabled, phase_for


_RESOURCE_KINDS = frozenset({"page", "media", "playlist"})
_RESOURCE_KIND_LABELS = {
    "page": "页面",
    "media": "媒体",
    "playlist": "播放列表",
}
_HEADERS = ("选择", "平台", "类型", "格式", "候选地址")
_COLUMNS = ("selected", "platform", "resource_kind", "format_hint", "value")


class LinkCandidateTableModel(QAbstractTableModel):
    """Display-only link candidates with stable ID-based check state."""

    selection_changed = pyqtSignal(int)

    def __init__(
        self,
        translate: Callable[[object], str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._translate = translate
        self._rows: list[dict[str, Any]] = []
        self._selected_ids: set[str] = set()
        self._identity: tuple[str, tuple[str, ...]] = ("", ())

    @property
    def identity(self) -> tuple[str, tuple[str, ...]]:
        return self._identity

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(_COLUMNS)

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        key = _COLUMNS[index.column()]
        candidate_id = row["candidate_id"]
        selectable = row["admittable"] is True
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            if not selectable:
                return None
            return (
                Qt.CheckState.Checked
                if candidate_id in self._selected_ids
                else Qt.CheckState.Unchecked
            )
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                return ""
            if key == "resource_kind":
                return self._translate(_RESOURCE_KIND_LABELS.get(str(row.get(key)), ""))
            return self._translate(row.get(key, ""))
        if role == Qt.ItemDataRole.ToolTipRole:
            if not selectable:
                reason_code = str(row.get("admission_reason_code") or "")
                message = self._translate("仅预览，不能加入下载队列")
                return f"{message} ({reason_code})" if reason_code else message
            return str(row.get("value") or row.get("label") or "")
        if role == Qt.ItemDataRole.UserRole:
            return candidate_id
        if role == Qt.ItemDataRole.TextAlignmentRole:
            alignment = (
                Qt.AlignmentFlag.AlignCenter
                if index.column() < 4
                else Qt.AlignmentFlag.AlignLeft
            )
            return int(Qt.AlignmentFlag.AlignVCenter | alignment)
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:  # noqa: N802
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(_HEADERS)
        ):
            return self._translate(_HEADERS[section])
        return super().headerData(section, orientation, role)

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if (
            index.column() == 0
            and self._rows[index.row()]["admittable"] is True
        ):
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(
        self,
        index: QModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:  # noqa: N802
        if (
            role != Qt.ItemDataRole.CheckStateRole
            or not index.isValid()
            or index.column() != 0
            or self._rows[index.row()]["admittable"] is not True
        ):
            return False
        candidate_id = self._rows[index.row()]["candidate_id"]
        if value == Qt.CheckState.Checked or value == Qt.CheckState.Checked.value:
            self._selected_ids.add(candidate_id)
        else:
            self._selected_ids.discard(candidate_id)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.selection_changed.emit(len(self._selected_ids))
        return True

    def set_candidates(
        self,
        run_id: str,
        rows: object,
    ) -> bool:
        normalized = self._normalize_rows(rows)
        identity = (
            str(run_id or "").strip(),
            tuple(row["candidate_id"] for row in normalized),
        )
        identity_changed = identity != self._identity
        if identity_changed:
            self.beginResetModel()
            self._rows = normalized
            self._identity = identity
            self._selected_ids = {
                row["candidate_id"]
                for row in normalized
                if row["admittable"] is True
            }
            self.endResetModel()
        elif normalized != self._rows:
            self._rows = normalized
            if self._rows:
                self.dataChanged.emit(
                    self.index(0, 0),
                    self.index(len(self._rows) - 1, len(_COLUMNS) - 1),
                    [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole],
                )
        self.selection_changed.emit(len(self._selected_ids))
        return identity_changed

    def selected_candidate_ids(self) -> list[str]:
        return [
            row["candidate_id"]
            for row in self._rows
            if row["candidate_id"] in self._selected_ids
            and row["admittable"] is True
        ]

    def refresh_language(self) -> None:
        if _HEADERS:
            self.headerDataChanged.emit(
                Qt.Orientation.Horizontal,
                0,
                len(_HEADERS) - 1,
            )
        if self._rows:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._rows) - 1, len(_COLUMNS) - 1),
                [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole],
            )

    @staticmethod
    def _normalize_rows(rows: object) -> list[dict[str, Any]]:
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            return []
        normalized: list[dict[str, Any]] = []
        seen_candidate_ids: set[str] = set()
        for value in rows:
            if len(normalized) >= 64:
                break
            if not isinstance(value, Mapping):
                continue
            candidate_id = str(value.get("candidate_id") or value.get("id") or "")
            resource_kind = str(value.get("resource_kind") or "").lower()
            admittable = value.get("admittable")
            admission_reason_code = value.get("admission_reason_code")
            if (
                len(candidate_id) != 64
                or any(character not in "0123456789abcdef" for character in candidate_id)
                or resource_kind not in _RESOURCE_KINDS
                or type(admittable) is not bool
                or not isinstance(admission_reason_code, str)
                or len(admission_reason_code) > 80
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in admission_reason_code
                )
                or admittable == bool(admission_reason_code)
                or candidate_id in seen_candidate_ids
            ):
                continue
            seen_candidate_ids.add(candidate_id)
            normalized.append(
                {
                    "candidate_id": candidate_id,
                    "label": str(value.get("label") or ""),
                    "value": str(value.get("value") or ""),
                    "platform": str(value.get("platform") or ""),
                    "resource_kind": resource_kind,
                    "format_hint": str(value.get("format_hint") or ""),
                    "expanded": type(value.get("expanded")) is bool
                    and value["expanded"],
                    "admittable": admittable,
                    "admission_reason_code": admission_reason_code,
                }
            )
        return normalized


class LinkCandidatePanel(QWidget):
    """Confirm a safe projected candidate selection before queue admission."""

    confirm_requested = pyqtSignal(object)

    def __init__(
        self,
        translate: Callable[[object], str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._translate = translate
        self._admission_allowed = False
        self._admission_complete = False
        self._admission_pending = False
        self._pending_request: tuple[
            str,
            str,
            tuple[str, ...],
            int | None,
        ] | None = None
        self._receipt_state: tuple[str, object] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)
        self.title_label = QLabel()
        self.title_label.setObjectName("ToolboxCandidateTitle")
        layout.addWidget(self.title_label)
        self.hint_label = QLabel()
        self.hint_label.setObjectName("MutedLabel")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        self.model = LinkCandidateTableModel(translate, self)
        self.model.selection_changed.connect(self._sync_selection_state)
        self.table = QTableView()
        self.table.setObjectName("ToolboxCandidateTable")
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        for column in range(4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(150)
        self.table.setMaximumHeight(270)
        layout.addWidget(self.table)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        self.selection_label = QLabel()
        self.selection_label.setObjectName("MutedLabel")
        footer.addWidget(self.selection_label, 1)
        self.confirm_button = QPushButton()
        self.confirm_button.setObjectName("ToolboxCandidateConfirmButton")
        self.confirm_button.clicked.connect(self._confirm)
        footer.addWidget(self.confirm_button)
        layout.addLayout(footer)

        self.receipt_label = QLabel()
        self.receipt_label.setObjectName("ToolboxCandidateReceipt")
        self.receipt_label.setWordWrap(True)
        self.receipt_label.hide()
        layout.addWidget(self.receipt_label)
        self.set_language()
        self.hide()

    def set_language(self, _language: str | None = None) -> None:
        self.title_label.setText(self._translate("候选资源"))
        self.hint_label.setText(
            self._translate("仅可加入服务端标记为可下载的候选项；其他项目仅供预览。")
        )
        self.confirm_button.setText(self._translate("加入下载队列"))
        self.model.refresh_language()
        self._render_receipt()
        self._sync_selection_state(len(self.model.selected_candidate_ids()))

    def render(self, projection: Mapping[str, Any]) -> None:
        result = projection.get("result")
        rows = result.get("rows") if isinstance(result, Mapping) else ()
        raw_run_id = projection.get("run_id")
        run_id = raw_run_id.strip() if isinstance(raw_run_id, str) else ""
        identity_changed = self.model.set_candidates(run_id, rows)
        if identity_changed:
            self._admission_complete = False
            self._admission_pending = False
            self._pending_request = None
            self._receipt_state = None
            self._render_receipt()
        is_link_result = (
            str(projection.get("tool_id") or "") == "link_parser"
            and bool(run_id)
            and bool(self.model.rowCount())
        )
        self.setVisible(is_link_result)
        self._admission_allowed = is_link_result and action_enabled(
            projection,
            "tool_admit_candidates",
            phase_for(projection) in {"success", "completed"},
        )
        self._sync_selection_state(len(self.model.selected_candidate_ids()))

    def apply_action_result(
        self,
        action: str,
        payload: Mapping[str, Any],
        action_result: Mapping[str, Any],
        *,
        sequence: int | None = None,
    ) -> None:
        if action != "tool_admit_candidates" or self._pending_request is None:
            return
        request = self._request_identity(payload, sequence)
        if request is None or not self._request_matches_pending(request):
            return
        data = action_result.get("data")
        data = data if isinstance(data, Mapping) else {}
        if action_result.get("status") == "ok":
            accepted_count = self._validated_receipt_count(data, request)
            if accepted_count is None:
                self._receipt_state = ("error", "候选资源回执无效")
                self._admission_complete = False
            else:
                self._receipt_state = ("success", accepted_count)
                self._admission_complete = True
        else:
            self._receipt_state = (
                "error",
                action_result.get("message") or "候选资源加入失败",
            )
            self._admission_complete = False
        self._admission_pending = False
        self._pending_request = None
        self._render_receipt()
        self._sync_selection_state(len(self.model.selected_candidate_ids()))

    def bind_action_request(
        self,
        action: str,
        payload: Mapping[str, Any],
        *,
        sequence: int,
    ) -> None:
        if action != "tool_admit_candidates" or self._pending_request is None:
            return
        request = self._request_identity(payload, sequence)
        if request is not None and request[:3] == self._pending_request[:3]:
            self._pending_request = request

    @staticmethod
    def _request_identity(
        payload: Mapping[str, Any],
        sequence: int | None,
    ) -> tuple[str, str, tuple[str, ...], int | None] | None:
        if not isinstance(payload, Mapping):
            return None
        tool_id = payload.get("tool_id")
        run_id = payload.get("run_id")
        candidate_ids = payload.get("candidate_ids")
        if (
            tool_id != "link_parser"
            or not isinstance(run_id, str)
            or not run_id.strip()
            or not isinstance(candidate_ids, list)
            or not 1 <= len(candidate_ids) <= 64
            or type(sequence) not in {int, type(None)}
        ):
            return None
        normalized = tuple(candidate_ids)
        if (
            len(set(normalized)) != len(normalized)
            or any(not LinkCandidatePanel._is_candidate_id(value) for value in normalized)
        ):
            return None
        return "link_parser", run_id.strip(), normalized, sequence

    def _request_matches_pending(
        self,
        request: tuple[str, str, tuple[str, ...], int | None],
    ) -> bool:
        pending = self._pending_request
        if pending is None or request[:3] != pending[:3]:
            return False
        return pending[3] is None or request[3] == pending[3]

    @classmethod
    def _validated_receipt_count(
        cls,
        data: Mapping[str, Any],
        request: tuple[str, str, tuple[str, ...], int | None],
    ) -> int | None:
        accepted_count = data.get("accepted_count")
        candidate_ids = data.get("candidate_ids")
        video_ids = data.get("video_ids")
        if (
            data.get("status") != "queued"
            or data.get("tool_id") != request[0]
            or data.get("run_id") != request[1]
            or type(accepted_count) is not int
            or not 0 <= accepted_count <= 64
            or not isinstance(candidate_ids, list)
            or not isinstance(video_ids, list)
            or len(candidate_ids) > 64
            or len(video_ids) > 64
            or accepted_count != len(candidate_ids)
            or accepted_count != len(video_ids)
            or tuple(candidate_ids) != request[2]
            or any(not cls._is_candidate_id(value) for value in candidate_ids)
            or any(not cls._is_video_id(value) for value in video_ids)
            or len(set(candidate_ids)) != len(candidate_ids)
            or len(set(video_ids)) != len(video_ids)
        ):
            return None
        return accepted_count

    @staticmethod
    def _is_candidate_id(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    @staticmethod
    def _is_video_id(value: object) -> bool:
        return (
            isinstance(value, str)
            and 1 <= len(value) <= 128
            and value.isascii()
            and all(character.isalnum() or character in "._:-" for character in value)
        )

    def _render_receipt(self) -> None:
        receipt = self._receipt_state
        if receipt is None:
            self.receipt_label.clear()
            self.receipt_label.hide()
            return
        kind, value = receipt
        if kind == "success":
            self.receipt_label.setText(
                f"{self._translate('已加入下载队列')}: "
                f"{value} {self._translate('项')}"
            )
        else:
            self.receipt_label.setText(self._translate(value))
        self.receipt_label.show()

    def _sync_selection_state(self, selected_count: int) -> None:
        self.selection_label.setText(
            f"{self._translate('已选择')} {selected_count} {self._translate('项')}"
        )
        self.confirm_button.setEnabled(
            self._admission_allowed
            and not self._admission_complete
            and not self._admission_pending
            and selected_count > 0
        )

    def _confirm(self) -> None:
        candidate_ids = self.model.selected_candidate_ids()
        if self.confirm_button.isEnabled() and candidate_ids:
            run_id = self.model.identity[0]
            self._pending_request = (
                "link_parser",
                run_id,
                tuple(candidate_ids),
                None,
            )
            self._admission_pending = True
            self._sync_selection_state(len(candidate_ids))
            self.confirm_requested.emit((run_id, tuple(candidate_ids)))


__all__ = ["LinkCandidatePanel", "LinkCandidateTableModel"]
