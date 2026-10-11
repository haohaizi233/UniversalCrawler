from __future__ import annotations

import os
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication

from app.ui.pages.toolbox_link_candidates import LinkCandidatePanel, LinkCandidateTableModel
from app.ui.pages.toolbox_page import ToolboxPage
from app.ui.viewmodels.frontend_action_worker import (
    FrontendActionRequest,
    FrontendActionWorker,
)
from shared.localization import tr


_PAGE_ID = "a" * 64
_MEDIA_ID = "b" * 64
_PLAYLIST_ID = "c" * 64
_GENERIC_MEDIA_ID = "d" * 64
_SUPPORTED_MEDIA_ID = "e" * 64


def _projection(run_id: str = "run-link-1") -> dict:
    return {
        "tool_id": "link_parser",
        "run_id": run_id,
        "state": "success",
        "result": {
            "id": run_id,
            "display_text": "Parsed 3 links",
            "rows": [
                {
                    "id": _PAGE_ID,
                    "candidate_id": _PAGE_ID,
                    "label": "generic page / PLATFORM",
                    "value": "https://example.com/[redacted]",
                    "platform": "generic",
                    "resource_kind": "page",
                    "format_hint": "PLATFORM",
                    "expanded": False,
                    "admittable": False,
                    "admission_reason_code": "candidate_requires_crawl_resolution",
                },
                {
                    "id": _MEDIA_ID,
                    "candidate_id": _MEDIA_ID,
                    "label": "bilibili media / MP4",
                    "value": "https://www.bilibili.com/[redacted]",
                    "platform": "bilibili",
                    "resource_kind": "media",
                    "format_hint": "MP4",
                    "expanded": True,
                    "admittable": True,
                    "admission_reason_code": "",
                },
                {
                    "id": _PLAYLIST_ID,
                    "candidate_id": _PLAYLIST_ID,
                    "label": "missav playlist / HLS",
                    "value": "https://missav.ai/[redacted]",
                    "platform": "missav",
                    "resource_kind": "playlist",
                    "format_hint": "HLS",
                    "expanded": True,
                    "admittable": True,
                    "admission_reason_code": "",
                },
            ],
        },
        "actions": {"tool_admit_candidates": True},
        "action_payloads": {
            "tool_admit_candidates": {
                "tool_id": "link_parser",
                "run_id": run_id,
            }
        },
    }


def _snapshot(projection: dict | None = None) -> dict:
    return {
        "toolbox_items": [
            {
                "id": "link_parser",
                "title": "Link parser",
                "summary": "Parse mixed text",
                "input_example": "https://example.com/video",
                "output_example": "Candidate preview",
                "icon_file": "tool_link_parser.png",
            }
        ],
        "toolbox_display_projection": projection or _projection(),
        "toolbox_recent_items": [],
    }


def _admission_payload(run_id: str = "run-link-1") -> dict:
    return {
        "tool_id": "link_parser",
        "run_id": run_id,
        "candidate_ids": [_MEDIA_ID, _PLAYLIST_ID],
    }


class LinkCandidatePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.panel = LinkCandidatePanel(str)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.panel.close()
        self.panel.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_page_candidates_are_display_only_and_downloadable_rows_are_selected(self) -> None:
        self.panel.render(_projection())
        model = self.panel.model

        self.assertIsInstance(model, LinkCandidateTableModel)
        self.assertEqual(model.rowCount(), 3)
        self.assertFalse(model.flags(model.index(0, 0)) & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertTrue(model.flags(model.index(1, 0)) & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertTrue(model.flags(model.index(2, 0)) & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertEqual(model.selected_candidate_ids(), [_MEDIA_ID, _PLAYLIST_ID])
        self.assertNotIn("private_url", self.panel.table.model().data(model.index(1, 4)) or "")

    def test_server_admission_capability_is_the_only_selection_authority(self) -> None:
        rows = [
            {
                "candidate_id": _GENERIC_MEDIA_ID,
                "resource_kind": "media",
                "platform": "generic",
                "format_hint": "MP4",
                "value": "https://cdn.example/[redacted]",
                "admittable": False,
                "admission_reason_code": "candidate_downloader_unavailable",
            },
            {
                "candidate_id": _PLAYLIST_ID,
                "resource_kind": "playlist",
                "platform": "douyin",
                "format_hint": "HLS",
                "value": "https://www.douyin.com/[redacted]",
                "admittable": False,
                "admission_reason_code": "candidate_downloader_unavailable",
            },
            {
                "candidate_id": _MEDIA_ID,
                "resource_kind": "playlist",
                "platform": "missav",
                "format_hint": "HLS",
                "value": "https://missav.ai/[redacted]",
                "admittable": True,
                "admission_reason_code": "",
            },
            {
                "candidate_id": _SUPPORTED_MEDIA_ID,
                "resource_kind": "media",
                "platform": "bilibili",
                "format_hint": "MP4",
                "value": "https://www.bilibili.com/[redacted]",
                "admittable": True,
                "admission_reason_code": "",
            },
        ]

        self.panel.model.set_candidates("run-policy", rows)

        self.assertFalse(
            self.panel.model.flags(self.panel.model.index(0, 0))
            & Qt.ItemFlag.ItemIsUserCheckable
        )
        self.assertFalse(
            self.panel.model.flags(self.panel.model.index(1, 0))
            & Qt.ItemFlag.ItemIsUserCheckable
        )
        self.assertTrue(
            self.panel.model.flags(self.panel.model.index(2, 0))
            & Qt.ItemFlag.ItemIsUserCheckable
        )
        self.assertTrue(
            self.panel.model.flags(self.panel.model.index(3, 0))
            & Qt.ItemFlag.ItemIsUserCheckable
        )
        self.assertEqual(
            self.panel.model.selected_candidate_ids(),
            [_MEDIA_ID, _SUPPORTED_MEDIA_ID],
        )

    def test_same_result_preserves_selection_and_new_result_resets_it(self) -> None:
        self.panel.render(_projection())
        self.assertTrue(
            self.panel.model.setData(
                self.panel.model.index(1, 0),
                Qt.CheckState.Unchecked,
                Qt.ItemDataRole.CheckStateRole,
            )
        )
        self.assertEqual(self.panel.model.selected_candidate_ids(), [_PLAYLIST_ID])

        self.panel.render(_projection())
        self.assertEqual(self.panel.model.selected_candidate_ids(), [_PLAYLIST_ID])

        self.panel.render(_projection("run-link-2"))
        self.assertEqual(self.panel.model.selected_candidate_ids(), [_MEDIA_ID, _PLAYLIST_ID])

    def test_candidate_model_bounds_rows_before_rendering(self) -> None:
        rows = [
            {
                "candidate_id": f"{index + 1:064x}",
                "resource_kind": "media",
                "value": "https://example.com/[redacted]",
                "admittable": True,
                "admission_reason_code": "",
            }
            for index in range(80)
        ]

        self.panel.model.set_candidates("run-bounded", rows)

        self.assertEqual(self.panel.model.rowCount(), 64)
        self.assertEqual(len(self.panel.model.selected_candidate_ids()), 64)

    def test_candidate_model_keeps_first_64_unique_valid_rows(self) -> None:
        rows = [
            *[
                {
                    "candidate_id": "invalid",
                    "resource_kind": "media",
                    "admittable": True,
                    "admission_reason_code": "",
                }
                for _ in range(64)
            ],
            {
                "candidate_id": _MEDIA_ID,
                "resource_kind": "media",
                "value": "first",
                "admittable": True,
                "admission_reason_code": "",
            },
            {
                "candidate_id": _MEDIA_ID,
                "resource_kind": "media",
                "value": "duplicate",
                "admittable": True,
                "admission_reason_code": "",
            },
            *[
                {
                    "candidate_id": f"{index + 1:064x}",
                    "resource_kind": "playlist",
                    "value": f"valid-{index + 1}",
                    "admittable": True,
                    "admission_reason_code": "",
                }
                for index in range(70)
            ],
        ]

        self.panel.model.set_candidates("run-unique-valid", rows)

        self.assertEqual(self.panel.model.rowCount(), 64)
        self.assertEqual(len(self.panel.model.selected_candidate_ids()), 64)
        self.assertEqual(self.panel.model.data(self.panel.model.index(0, 4)), "first")
        candidate_ids = [
            self.panel.model.data(
                self.panel.model.index(row, 0),
                Qt.ItemDataRole.UserRole,
            )
            for row in range(self.panel.model.rowCount())
        ]
        self.assertEqual(len(set(candidate_ids)), 64)

    def test_confirmation_emits_frozen_run_and_candidate_identity(self) -> None:
        emitted: list[tuple[str, tuple[str, ...]]] = []
        self.panel.confirm_requested.connect(emitted.append)
        self.panel.render(_projection())

        self.panel.confirm_button.click()

        self.assertEqual(
            emitted,
            [("run-link-1", (_MEDIA_ID, _PLAYLIST_ID))],
        )

    def test_confirmation_requires_canonical_top_level_run_identity(self) -> None:
        emitted: list[tuple[str, tuple[str, ...]]] = []
        projection = _projection()
        projection.pop("run_id")
        projection["result"]["id"] = "fallback-result-id"
        self.panel.confirm_requested.connect(emitted.append)

        self.panel.render(projection)
        self.panel.confirm_button.click()

        self.assertFalse(self.panel.confirm_button.isEnabled())
        self.assertEqual(emitted, [])

    def test_confirmation_ignores_conflicting_result_identity(self) -> None:
        emitted: list[tuple[str, tuple[str, ...]]] = []
        projection = _projection("run-canonical")
        projection["result"]["id"] = "run-conflicting-result"
        self.panel.confirm_requested.connect(emitted.append)

        self.panel.render(projection)
        self.panel.confirm_button.click()

        self.assertEqual(
            emitted,
            [("run-canonical", (_MEDIA_ID, _PLAYLIST_ID))],
        )

    def test_confirmation_enters_pending_state_and_ignores_double_click(self) -> None:
        emitted: list[tuple[str, tuple[str, ...]]] = []
        self.panel.confirm_requested.connect(emitted.append)
        self.panel.render(_projection())

        self.panel.confirm_button.click()
        self.panel.confirm_button.click()

        self.assertEqual(
            emitted,
            [("run-link-1", (_MEDIA_ID, _PLAYLIST_ID))],
        )
        self.assertFalse(self.panel.confirm_button.isEnabled())

    def test_receipt_rejects_the_same_unsafe_video_ids_as_web(self) -> None:
        request = (
            "link_parser",
            "run-link-1",
            (_MEDIA_ID,),
            None,
        )
        for video_id in ("bad id", "x" * 129, "../escape", {}):
            with self.subTest(video_id=video_id):
                self.assertIsNone(
                    self.panel._validated_receipt_count(
                        {
                            "status": "queued",
                            "tool_id": "link_parser",
                            "run_id": "run-link-1",
                            "accepted_count": 1,
                            "candidate_ids": [_MEDIA_ID],
                            "video_ids": [video_id],
                        },
                        request,
                    )
                )

    def test_receipt_feedback_is_visible_without_reusing_private_result_fields(self) -> None:
        self.panel.render(_projection())
        self.panel.confirm_button.click()

        self.panel.apply_action_result(
            "tool_admit_candidates",
            _admission_payload(),
            {
                "status": "ok",
                "message": "selected links were queued",
                "data": {
                    "status": "queued",
                    "tool_id": "link_parser",
                    "run_id": "run-link-1",
                    "accepted_count": 2,
                    "candidate_ids": [_MEDIA_ID, _PLAYLIST_ID],
                    "video_ids": ["video-1", "video-2"],
                },
            },
        )

        self.assertTrue(self.panel.receipt_label.isVisible())
        self.assertIn("2", self.panel.receipt_label.text())
        self.assertNotIn("https://", self.panel.receipt_label.text())

    def test_submission_error_clears_pending_and_reenables_retry(self) -> None:
        self.panel.render(_projection())
        self.panel.confirm_button.click()
        self.panel.bind_action_request(
            "tool_admit_candidates",
            _admission_payload(),
            sequence=17,
        )

        self.panel.apply_action_result(
            "tool_admit_candidates",
            _admission_payload(),
            {
                "status": "error",
                "message": "tool action submission failed",
                "data": {"code": "tool_action_submit_failed"},
            },
            sequence=17,
        )

        self.assertFalse(self.panel._admission_pending)
        self.assertIsNone(self.panel._pending_request)
        self.assertTrue(self.panel.confirm_button.isEnabled())
        self.assertEqual(self.panel.receipt_label.text(), "tool action submission failed")

    def test_background_control_flow_receipt_clears_pending_and_worker_recovers(self) -> None:
        class RecoveringService:
            def __init__(self) -> None:
                self.calls = 0

            def handle_action(self, _action: str, _payload: dict) -> dict:
                self.calls += 1
                if self.calls == 1:
                    raise KeyboardInterrupt("pre-commit abort")
                return {"status": "ok"}

        self.panel.render(_projection())
        self.panel.confirm_button.click()
        payload = _admission_payload()
        self.panel.bind_action_request(
            "tool_admit_candidates",
            payload,
            sequence=71,
        )
        terminal = threading.Event()
        results = []

        def record(result: object) -> None:
            results.append(result)
            if len(results) == 2:
                terminal.set()

        service = RecoveringService()
        worker = FrontendActionWorker(record)
        try:
            worker.submit(
                FrontendActionRequest(
                    sequence=71,
                    service=service,
                    service_token=id(service),
                    action="tool_admit_candidates",
                    payload=payload,
                )
            )
            worker.submit(
                FrontendActionRequest(
                    sequence=72,
                    service=service,
                    service_token=id(service),
                    action="tool_refresh",
                    payload={"tool_id": "link_parser"},
                )
            )

            self.assertTrue(terminal.wait(2))
            self.panel.apply_action_result(
                results[0].action,
                results[0].payload,
                results[0].result,
                sequence=results[0].sequence,
            )

            self.assertFalse(self.panel._admission_pending)
            self.assertIsNone(self.panel._pending_request)
            self.assertTrue(self.panel.confirm_button.isEnabled())
            self.assertEqual(results[1].result["status"], "ok")
            self.assertTrue(worker._worker._thread.is_alive())
        finally:
            worker.shutdown()

    def test_malformed_receipt_count_is_bounded_without_raising(self) -> None:
        self.panel.render(_projection())
        self.panel.confirm_button.click()

        self.panel.apply_action_result(
            "tool_admit_candidates",
            _admission_payload(),
            {
                "status": "ok",
                "data": {
                    "status": "queued",
                    "tool_id": "link_parser",
                    "run_id": "run-link-1",
                    "accepted_count": "9" * 10_000,
                    "candidate_ids": [_MEDIA_ID, _PLAYLIST_ID],
                    "video_ids": ["video-1", "video-2"],
                },
            },
        )

        self.assertFalse(self.panel._admission_complete)
        self.assertTrue(self.panel.confirm_button.isEnabled())
        self.assertEqual(self.panel.receipt_label.text(), "候选资源回执无效")

    def test_stale_receipt_cannot_mutate_a_new_run(self) -> None:
        self.panel.render(_projection())
        self.panel.confirm_button.click()
        self.panel.render(_projection("run-link-2"))

        self.panel.apply_action_result(
            "tool_admit_candidates",
            _admission_payload("run-link-1"),
            {
                "status": "ok",
                "data": {
                    "status": "queued",
                    "tool_id": "link_parser",
                    "run_id": "run-link-1",
                    "accepted_count": 2,
                    "candidate_ids": [_MEDIA_ID, _PLAYLIST_ID],
                    "video_ids": ["video-1", "video-2"],
                },
            },
        )

        self.assertFalse(self.panel.receipt_label.isVisible())
        self.assertFalse(self.panel._admission_complete)

    def test_matching_receipt_updates_hidden_panel_state(self) -> None:
        self.panel.render(_projection())
        self.panel.confirm_button.click()
        self.panel.hide()

        self.panel.apply_action_result(
            "tool_admit_candidates",
            _admission_payload(),
            {
                "status": "ok",
                "data": {
                    "status": "queued",
                    "tool_id": "link_parser",
                    "run_id": "run-link-1",
                    "accepted_count": 2,
                    "candidate_ids": [_MEDIA_ID, _PLAYLIST_ID],
                    "video_ids": ["video-1", "video-2"],
                },
            },
        )

        self.assertTrue(self.panel._admission_complete)
        self.assertEqual(self.panel.receipt_label.text(), "已加入下载队列: 2 项")

    def test_language_refresh_translates_fixed_copy_and_dynamic_counts(self) -> None:
        language = {"value": "zh-CN"}
        panel = LinkCandidatePanel(lambda value: tr(str(value), language["value"]))
        panel.show()
        self.addCleanup(panel.close)

        panel.render(_projection())
        language["value"] = "en-US"
        panel.set_language("en-US")

        self.assertEqual(panel.title_label.text(), "Candidate resources")
        self.assertEqual(
            panel.hint_label.text(),
            "Only candidates marked downloadable by the server can be added; other items are preview-only.",
        )
        self.assertEqual(panel.confirm_button.text(), "Add to download queue")
        self.assertEqual(panel.selection_label.text(), "Selected 2 items")
        self.assertEqual(panel.model.data(panel.model.index(0, 2)), "Page")
        self.assertEqual(panel.model.data(panel.model.index(1, 2)), "Media")
        self.assertEqual(panel.model.data(panel.model.index(2, 2)), "Playlist")

        panel.confirm_button.click()
        panel.apply_action_result(
            "tool_admit_candidates",
            _admission_payload(),
            {
                "status": "ok",
                "data": {
                    "status": "queued",
                    "tool_id": "link_parser",
                    "run_id": "run-link-1",
                    "accepted_count": 2,
                    "candidate_ids": [_MEDIA_ID, _PLAYLIST_ID],
                    "video_ids": ["video-1", "video-2"],
                },
            },
        )
        self.assertEqual(panel.receipt_label.text(), "Added to download queue: 2 items")

        language["value"] = "zh-CN"
        panel.set_language("zh-CN")
        self.assertEqual(panel.receipt_label.text(), "已加入下载队列: 2 项")

    def test_receipt_must_match_the_bound_action_sequence(self) -> None:
        self.panel.render(_projection())
        self.panel.confirm_button.click()
        self.panel.bind_action_request(
            "tool_admit_candidates",
            _admission_payload(),
            sequence=41,
        )
        receipt = {
            "status": "ok",
            "data": {
                "status": "queued",
                "tool_id": "link_parser",
                "run_id": "run-link-1",
                "accepted_count": 2,
                "candidate_ids": [_MEDIA_ID, _PLAYLIST_ID],
                "video_ids": ["video-1", "video-2"],
            },
        }

        self.panel.apply_action_result(
            "tool_admit_candidates",
            _admission_payload(),
            receipt,
            sequence=40,
        )
        self.assertFalse(self.panel.receipt_label.isVisible())
        self.assertFalse(self.panel._admission_complete)

        self.panel.apply_action_result(
            "tool_admit_candidates",
            _admission_payload(),
            receipt,
            sequence=41,
        )
        self.assertTrue(self.panel._admission_complete)
        self.assertEqual(self.panel.receipt_label.text(), "已加入下载队列: 2 项")


class ToolboxPageLinkCandidateIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.page = ToolboxPage()
        self.page.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.page.close()
        self.page.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_page_emits_exact_admission_payload_without_private_fields(self) -> None:
        actions: list[tuple[str, dict]] = []
        self.page.action_requested.connect(lambda action, payload: actions.append((action, payload)))
        self.page.render(_snapshot())

        self.page.link_candidate_panel.confirm_button.click()

        self.assertEqual(
            actions,
            [
                (
                    "tool_admit_candidates",
                    {
                        "tool_id": "link_parser",
                        "run_id": "run-link-1",
                        "candidate_ids": [_MEDIA_ID, _PLAYLIST_ID],
                    },
                )
            ],
        )
        self.assertFalse({"url", "private_url", "save_directory"}.intersection(actions[0][1]))

    def test_confirmed_request_keeps_its_run_when_projection_changes(self) -> None:
        frozen_requests: list[tuple[str, tuple[str, ...]]] = []
        actions: list[tuple[str, dict]] = []
        self.page.execution_panel.candidate_confirm_requested.disconnect(
            self.page._emit_candidate_admission
        )
        self.page.execution_panel.candidate_confirm_requested.connect(
            frozen_requests.append
        )
        self.page.action_requested.connect(
            lambda action, payload: actions.append((action, payload))
        )
        self.page.render(_snapshot(_projection("run-link-1")))

        self.page.link_candidate_panel.confirm_button.click()
        self.page.render(_snapshot(_projection("run-link-2")))
        self.page._emit_candidate_admission(frozen_requests[0])

        self.assertEqual(
            actions,
            [
                (
                    "tool_admit_candidates",
                    {
                        "tool_id": "link_parser",
                        "run_id": "run-link-1",
                        "candidate_ids": [_MEDIA_ID, _PLAYLIST_ID],
                    },
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
