"""把 AppShell 页面控件上的可见文案翻译到当前语言。"""

from __future__ import annotations

from PyQt6 import sip
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QComboBox, QLineEdit, QTableView, QTableWidget, QWidget

from app.ui.components.combo_popup import fit_combo_width_to_contents, refresh_themed_combo_boxes
from shared.localization import is_translation_of, source_text_for_translation, tr


def qt_widget_alive(widget: QWidget | None) -> bool:
    if widget is None:
        return False
    try:
        return not sip.isdeleted(widget)
    except (AttributeError, RuntimeError, TypeError):
        return False


def safe_find_children(root: QWidget, widget_type):
    if not qt_widget_alive(root):
        return []
    try:
        return [child for child in root.findChildren(widget_type) if qt_widget_alive(child)]
    except RuntimeError:
        return []


def apply_language_to_widget_tree(root: QWidget, language: str) -> None:
    seen: set[int] = set()
    for widget in (root, *safe_find_children(root, QWidget)):
        if not qt_widget_alive(widget):
            continue
        marker = id(widget)
        if marker in seen:
            continue
        seen.add(marker)
        setter = getattr(widget, "set_language", None)
        if callable(setter):
            try:
                setter(language)
            except RuntimeError:
                continue


def close_combo_popups(host: QWidget, root: QWidget | None) -> None:
    if root is None:
        return
    for combo in safe_find_children(root, QComboBox):
        try:
            view = combo.view()
            if view is not None and (view.isVisible() or view.window().isVisible()):
                combo.hidePopup()
            combo.setProperty("popupOpen", "false")
        except RuntimeError:
            continue
    for popup in QApplication.topLevelWidgets():
        try:
            if popup is host.window():
                continue
            if popup.objectName() == "PolishedComboPopupWindow":
                popup.hide()
                popup.setProperty("popupOpen", "false")
        except RuntimeError:
            continue


def translate_text_widget(widget: QWidget, getter, setter, language: str) -> None:
    text = str(getter() or "")
    if not text:
        return
    source = widget.property("_i18n_source_text")
    if source is None:
        source = source_text_for_translation(text)
        widget.setProperty("_i18n_source_text", source)
    elif not is_translation_of(text, str(source)):
        source = source_text_for_translation(text)
        widget.setProperty("_i18n_source_text", source)
    translated = tr(str(source), language)
    if translated != text:
        setter(translated)


def translate_tooltip(widget: QWidget, language: str) -> None:
    text = str(widget.toolTip() or "")
    if not text:
        return
    source = widget.property("_i18n_source_tooltip")
    if source is None:
        source = source_text_for_translation(text)
        widget.setProperty("_i18n_source_tooltip", source)
    elif not is_translation_of(text, str(source)):
        source = source_text_for_translation(text)
        widget.setProperty("_i18n_source_tooltip", source)
    translated = tr(str(source), language)
    if translated != text:
        widget.setToolTip(translated)


def translate_placeholder(line_edit: QLineEdit, language: str) -> None:
    text = str(line_edit.placeholderText() or "")
    if not text:
        return
    source = line_edit.property("_i18n_source_placeholder")
    if source is None:
        source = source_text_for_translation(text)
        line_edit.setProperty("_i18n_source_placeholder", source)
    elif not is_translation_of(text, str(source)):
        source = source_text_for_translation(text)
        line_edit.setProperty("_i18n_source_placeholder", source)
    translated = tr(str(source), language)
    if translated != text:
        line_edit.setPlaceholderText(translated)


def translate_combo(combo: QComboBox, language: str) -> None:
    try:
        view = combo.view()
        if view is not None and (view.isVisible() or view.window().isVisible()):
            combo.hidePopup()
            combo.setProperty("popupOpen", "false")
    except RuntimeError:
        return
    source_role = int(Qt.ItemDataRole.UserRole) + 77
    try:
        blocked = combo.blockSignals(True)
    except RuntimeError:
        return
    try:
        for index in range(combo.count()):
            source = combo.itemData(index, source_role)
            if source is None:
                source = source_text_for_translation(combo.itemText(index))
                combo.setItemData(index, source, source_role)
            elif not is_translation_of(combo.itemText(index), str(source)):
                source = source_text_for_translation(combo.itemText(index))
                combo.setItemData(index, source, source_role)
            translated = tr(str(source), language)
            if translated != combo.itemText(index):
                combo.setItemText(index, translated)
    except RuntimeError:
        return
    finally:
        try:
            combo.blockSignals(blocked)
        except RuntimeError:
            return
    padding = combo.property("contentWidthPadding")
    if padding is not None:
        try:
            extra = int(padding)
            min_width = int(combo.property("contentMinWidth") or 0)
            max_width = int(combo.property("contentMaxWidth") or 16777215)
        except (TypeError, ValueError):
            return
        if max_width > 0:
            try:
                fit_combo_width_to_contents(
                    combo,
                    min_width=min_width,
                    max_width=max_width,
                    horizontal_padding=extra,
                )
                refresh_themed_combo_boxes(combo)
            except RuntimeError:
                return


def translate_table_headers(table: QTableWidget, language: str) -> None:
    source_role = int(Qt.ItemDataRole.UserRole) + 78
    for index in range(table.columnCount()):
        item = table.horizontalHeaderItem(index)
        if item is None:
            continue
        source = item.data(source_role)
        if source is None:
            source = source_text_for_translation(item.text())
            item.setData(source_role, source)
        elif not is_translation_of(item.text(), str(source)):
            source = source_text_for_translation(item.text())
            item.setData(source_role, source)
        item.setText(tr(str(source), language))


def translate_table_view_headers(table: QTableView, language: str) -> None:
    model = table.model()
    if model is None:
        return
    set_language = getattr(model, "set_language", None)
    if callable(set_language):
        set_language(language)
        return
    column_count = model.columnCount()
    current = [
        str(model.headerData(index, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) or "")
        for index in range(column_count)
    ]
    source = table.property("_i18n_source_headers")
    if not isinstance(source, list) or len(source) != column_count:
        source = [source_text_for_translation(value) for value in current]
        table.setProperty("_i18n_source_headers", source)
    else:
        translated_source = [tr(str(value), language) for value in source]
        if current != translated_source and not all(
            is_translation_of(text, str(source_value))
            for text, source_value in zip(current, source, strict=True)
        ):
            source = [source_text_for_translation(value) for value in current]
            table.setProperty("_i18n_source_headers", source)
    translated = [tr(str(value), language) for value in source]
    set_headers = getattr(model, "set_headers", None)
    if callable(set_headers):
        set_headers(translated)
