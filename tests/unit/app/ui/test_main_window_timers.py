"""PyQt6 timer contracts used by the main window."""

from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication, QMainWindow

from app.ui.main_window import MainWindow
from app.ui.layout.top_bar import TopBarWidget


class MainWindowTimerTests(unittest.TestCase):
    """Keep delayed theme scheduling compatible with PyQt6's slot overloads."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _make_theme_window(self) -> MainWindow:
        window = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(window)
        window.is_dark_theme = False
        window._last_applied_theme_is_dark = False
        window.top_bar = TopBarWidget(False)
        window.setCentralWidget(window.top_bar)
        window._submit_frontend_action = Mock()
        window.append_log = Mock()
        window._debug_shell_visibility = Mock()
        window._apply_root_background = Mock()
        window._close_transient_popups_before_theme = Mock()
        window._ensure_shell_chrome_visible = Mock()
        window._repair_black_shell_if_needed = Mock()
        window.top_bar.btn_theme.clicked.connect(window.toggle_theme)
        self.addCleanup(window.deleteLater)
        self.addCleanup(window._stop_theme_sequence)
        self.addCleanup(self.app.setFont, self.app.font())
        for name in ("_ucp_font_key", "ui_scale", "ui_font_size", "is_dark_theme"):
            self.addCleanup(self.app.setProperty, name, self.app.property(name))
        # Apply real QSS to this window without restyling widgets owned by other tests.
        roots = patch("app.ui.styles.themes._theme_style_roots", return_value=[window])
        roots.start()
        self.addCleanup(roots.stop)
        return window

    def _wait_for_theme(self, window: MainWindow) -> None:
        loop = QEventLoop()

        def on_changed(_dark: bool) -> None:
            loop.quit()

        window.sig_theme_changed.connect(on_changed)
        deadline = QTimer(loop)
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        deadline.start(1000)
        try:
            loop.exec()
        finally:
            deadline.stop()
            deadline.deleteLater()
            window.sig_theme_changed.disconnect(on_changed)

    def test_real_theme_button_switches_both_directions_and_leaves_busy_state(self) -> None:
        window = self._make_theme_window()
        for expected_dark in (True, False):
            window.top_bar.btn_theme.click()
            self._wait_for_theme(window)

            self.assertEqual(window.is_dark_theme, expected_dark)
            self.assertEqual(window._last_applied_theme_is_dark, expected_dark)
            self.assertFalse(window._theme_transition_in_progress)
            self.assertEqual(window.top_bar.btn_theme.property("themeBusy"), "false")
            self.assertTrue(window.top_bar.btn_theme.isEnabled())
        self.assertEqual(window._submit_frontend_action.call_count, 2)

    def test_scheduling_failure_releases_theme_state_and_allows_button_retry(self) -> None:
        window = self._make_theme_window()
        with patch("app.ui.main_window.QTimer.singleShot", side_effect=TypeError("timer overload")):
            window.toggle_theme()

        self.assertFalse(window._theme_transition_in_progress)
        self.assertIsNone(window._theme_transition_target_is_dark)
        self.assertEqual(window.top_bar.btn_theme.property("themeBusy"), "false")
        window.top_bar.btn_theme.click()
        self._wait_for_theme(window)
        self.assertTrue(window.is_dark_theme)
        self.assertFalse(window._theme_transition_in_progress)

    def test_schedule_theme_callback_runs_with_pyqt6_two_argument_overload(self) -> None:
        window = MainWindow.__new__(MainWindow)
        loop = QEventLoop()
        calls: list[bool] = []

        def callback() -> None:
            calls.append(True)
            loop.quit()

        # Keep the deadline as a local timer so no static one-shot callback can
        # fire after this test has returned.
        deadline = QTimer(loop)
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        deadline.start(1000)
        try:
            MainWindow._schedule_theme_callback(window, 0, callback)
            loop.exec()
        finally:
            deadline.stop()
            deadline.deleteLater()

        self.assertEqual(calls, [True])

    def test_queued_theme_callback_is_ignored_after_theme_sequence_stops(self) -> None:
        window = MainWindow.__new__(MainWindow)
        window.__dict__["_theme_callbacks_stopped"] = False
        window.__dict__["_theme_transition_sequence"] = 1
        window._set_theme_button_busy = Mock()
        window._finish_theme_transition = Mock()
        loop = QEventLoop()
        callback_ran = []

        def callback() -> None:
            MainWindow._commit_theme_toggle_interactive(window, False, 1)
            callback_ran.append(True)
            loop.quit()

        deadline = QTimer(loop)
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        deadline.start(1000)
        try:
            MainWindow._schedule_theme_callback(window, 0, callback)
            MainWindow._stop_theme_sequence(window)
            loop.exec()
        finally:
            deadline.stop()
            deadline.deleteLater()

        self.assertEqual(callback_ran, [True])
        window._set_theme_button_busy.assert_not_called()
        window._finish_theme_transition.assert_not_called()


if __name__ == "__main__":
    unittest.main()
