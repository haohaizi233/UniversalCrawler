"""抖音 run 成功标记、空输入和登录进程树回收。"""

import asyncio
import unittest
from unittest.mock import Mock, patch

from app.exceptions import SpiderAuthError
from app.spiders.douyin.spider import DouyinSpider, _terminate_process_tree


class DouyinRuntimeTests(unittest.TestCase):
    def _spider(self, keyword: str = "demo") -> DouyinSpider:
        spider = DouyinSpider.__new__(DouyinSpider)
        spider.keyword = keyword
        spider.config = {}
        spider.is_running = True
        spider._interrupt_requested = False
        spider.run_succeeded = True
        spider.log = Mock()
        spider.debug_state = Mock()
        spider._emit_finished = Mock()
        return spider

    def test_run_marks_success_only_after_main_flow_finishes(self):
        spider = self._spider()
        spider._load_or_login = Mock(return_value="sessionid_ss=ok")

        with patch("app.spiders.douyin.spider.asyncio.run", return_value=None):
            spider.run()

        self.assertTrue(spider.run_succeeded)
        spider._emit_finished.assert_called_once()

    def test_run_keeps_failure_when_login_fails_or_user_stops(self):
        failed = self._spider()
        failed._load_or_login = Mock(side_effect=SpiderAuthError("login failed"))
        failed.run()
        self.assertFalse(failed.run_succeeded)

        stopped = self._spider()
        stopped._load_or_login = Mock(return_value="sessionid_ss=ok")

        def stop_during_main(_coroutine):
            stopped.is_running = False
            stopped._interrupt_requested = True

        with patch("app.spiders.douyin.spider.asyncio.run", side_effect=stop_during_main):
            stopped.run()

        self.assertFalse(stopped.run_succeeded)

    def test_empty_input_does_not_refresh_remote_params(self):
        spider = self._spider("   ")
        spider._build_runtime_params = Mock()
        spider._should_stop = Mock(return_value=False)

        asyncio.run(spider._async_main_body("sessionid_ss=ok"))

        spider._build_runtime_params.assert_not_called()
        spider.log.assert_any_call("⚠️ 输入为空，已停止")

    def test_taskkill_failure_kills_process(self):
        process = Mock()
        process.pid = 4242
        completed = Mock(returncode=1)

        with patch("app.spiders.douyin.spider.os.name", "nt"), patch(
            "app.spiders.douyin.spider.subprocess.run",
            return_value=completed,
        ) as mocked_run:
            _terminate_process_tree(process)

        command = mocked_run.call_args.args[0]
        self.assertEqual(command[:4], ["taskkill", "/PID", "4242", "/T"])
        process.kill.assert_called_once()

    def test_taskkill_success_does_not_kill_process(self):
        process = Mock()
        process.pid = 4242
        completed = Mock(returncode=0)

        with patch("app.spiders.douyin.spider.os.name", "nt"), patch(
            "app.spiders.douyin.spider.subprocess.run",
            return_value=completed,
        ):
            _terminate_process_tree(process)

        process.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
