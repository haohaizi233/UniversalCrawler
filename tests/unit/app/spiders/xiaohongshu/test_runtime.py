"""小红书运行结果标记，以及停止后不再恢复流水线。"""

import unittest
from unittest.mock import Mock, patch

from app.spiders.xiaohongshu.spider import XiaohongshuSpider


class XiaohongshuRuntimeTests(unittest.TestCase):
    def test_run_marks_success_when_the_main_flow_finishes(self):
        spider = XiaohongshuSpider("关键词", {})
        spider.log = Mock()
        spider.debug_state = Mock()
        spider._emit_finished = Mock()
        spider._normalize_keyword = Mock(return_value="关键词")
        spider._classify_input = Mock(return_value=("keyword", "关键词"))
        spider._ensure_cookie_string = Mock(return_value="a1=ok")
        client = Mock()
        client.check_cookie_ready.return_value = True
        client.probe_login_status.return_value = True
        client.close = Mock()
        spider._build_client = Mock(return_value=client)
        spider._collect_search_refs = Mock(return_value=[])
        spider._handle_multi_refs = Mock()

        spider.run()

        self.assertTrue(spider.run_succeeded)
        spider._emit_finished.assert_called_once()

    def test_run_stays_failed_when_cookie_is_missing(self):
        spider = XiaohongshuSpider("关键词", {})
        spider.log = Mock()
        spider.debug_state = Mock()
        spider._emit_finished = Mock()
        spider._normalize_keyword = Mock(return_value="关键词")
        spider._classify_input = Mock(return_value=("keyword", "关键词"))
        spider._ensure_cookie_string = Mock(return_value="")

        spider.run()

        self.assertFalse(spider.run_succeeded)

    def test_missing_runtime_records_traceback_and_filename_at_error_level(self):
        spider = XiaohongshuSpider("关键词", {})
        spider.log = Mock()
        spider.debug_state = Mock()
        spider._emit_finished = Mock()
        spider._ensure_cookie_string = Mock(
            side_effect=FileNotFoundError(2, "missing browser driver", "node.exe")
        )

        spider.run()

        self.assertFalse(spider.run_succeeded)
        failure = next(
            call.kwargs for call in spider.debug_state.call_args_list
            if call.kwargs.get("action") == "run_error"
        )
        self.assertEqual(failure["level"], "ERROR")
        self.assertEqual(failure["details"]["exception_type"], "FileNotFoundError")
        self.assertEqual(failure["details"]["filename"], "node.exe")
        self.assertIn("_ensure_cookie_string", failure["details"]["traceback"])
        spider._emit_finished.assert_called_once()

    def test_stopped_parse_without_delivery_does_not_revive(self):
        spider = XiaohongshuSpider("主页", {"stream_downloads": True})
        spider.log = Mock()
        refs = [{"note_id": "note-1", "xsec_source": "pc_feed", "xsec_token": "token-1"}]

        def fetch_detail(_client, ref):
            spider.is_running = False
            return {"note_id": ref["note_id"]}

        with patch.object(spider, "_fetch_note_detail", side_effect=fetch_detail), patch.object(
            spider, "_emit_note_items", return_value=0
        ), patch.object(spider, "revive_for_partial_selection") as revive:
            spider._handle_multi_refs_streaming(Mock(), refs, "a1=demo")

        revive.assert_not_called()
        self.assertFalse(spider.is_running)


if __name__ == "__main__":
    unittest.main()
