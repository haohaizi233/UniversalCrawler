"""Bilibili 登录取消、停止、Cookie、音画和搜索边界。"""

import threading
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import Error as PlaywrightError

from app.exceptions import LoginCancelledError, SpiderParseError
from app.models import VideoItem
from app.spiders.bilibili.api_client import BiliAPI
from app.spiders.bilibili.parser import BilibiliParser
from app.spiders.bilibili.spider import BilibiliSpider
from app.spiders.bilibili.task_builder import BilibiliTaskBuilder
from app.utils.bilibili_wbi import BILIBILI_WBI_SIGNER


def _spider() -> BilibiliSpider:
    spider = BilibiliSpider.__new__(BilibiliSpider)
    spider.is_running = True
    spider.config = {"timeout": 5, "max_items": 10, "api_workers": 1}
    spider.log = Mock()
    spider.debug_state = Mock()
    spider.emit_video = Mock()
    spider.raw_bv_queue = Mock()
    spider.api = Mock()
    spider.user_agent = "ua-test"
    spider.interruptible_sleep = Mock(return_value=True)
    return spider


def _task(bvid: str = "BV1demo0001") -> dict:
    return {
        "trace_id": f"trace-{bvid}",
        "bvid": bvid,
        "cid": 101,
        "file_name": "示例.mp4",
        "referer": f"https://www.bilibili.com/video/{bvid}",
    }


def _api() -> BiliAPI:
    BILIBILI_WBI_SIGNER.set_keys(
        "7cd084941338484aae1ad9425b84077c",
        "4932caff0ff746eab6f01bf08b70ac45",
    )
    api = BiliAPI.__new__(BiliAPI)
    api.sess = Mock()
    api.parser = BilibiliParser()
    api.request_timeout = 5
    api._session_lock = threading.RLock()
    return api


class TestBilibiliRuntimeGuards(unittest.TestCase):
    def test_login_cancelled_returns_without_worker_threads(self):
        spider = BilibiliSpider("keyword", {})
        spider.log = Mock()
        spider.debug_state = Mock()
        spider._perform_login_scan = Mock(side_effect=LoginCancelledError("用户在登录过程中终止任务"))
        api = Mock()
        api.check_login.return_value = False

        with patch("app.spiders.bilibili.spider.BiliAPI", return_value=api), patch(
            "app.spiders.bilibili.spider.threading.Thread"
        ) as thread_cls:
            spider.run()

        thread_cls.assert_not_called()
        self.assertIsNone(spider._browser_thread)
        self.assertIsNone(spider._api_pool_thread)
        messages = [str(call.args[0]) for call in spider.log.call_args_list if call.args]
        self.assertFalse(any("以游客身份爬取" in message for message in messages))

    def test_get_play_url_checks_stop_before_second_request(self):
        api = _api()
        state = {"calls": 0}

        def fake_get(*_args, **_kwargs):
            state["calls"] += 1
            response = Mock(status_code=200)
            response.json.return_value = {"code": 0, "data": {}}
            response.url = "https://api.bilibili.com/x/player/playurl"
            return response

        api.sess.get.side_effect = fake_get
        api.stop_check = lambda: state["calls"] >= 1

        result = api.get_play_url("BV1demo", 123)

        self.assertEqual(result, (None, None, 0))
        self.assertEqual(state["calls"], 1)
        api.sess.close.assert_called()

    def test_resolve_omits_login_cookies_from_broadcast_meta(self):
        spider = _spider()
        spider.api.get_play_url.return_value = (
            "https://cdn.example.com/video.m4s",
            "https://cdn.example.com/audio.m4s",
            80,
        )
        spider.api.snapshot_cookies.return_value = {"SESSDATA": "secret", "bili_jct": "token", "buvid3": "edge"}

        item = spider._resolve_download_item(_task())

        self.assertIsInstance(item, VideoItem)
        self.assertNotIn("cookies", item.meta)
        self.assertNotIn("cookie", item.meta)
        rendered = str(item.meta)
        self.assertNotIn("SESSDATA", rendered)
        self.assertNotIn("bili_jct", rendered)
        self.assertNotIn("secret", rendered)

    def test_missing_v_url_returns_none_and_serial_branch_counts_failure(self):
        spider = _spider()
        spider.api.get_play_url.side_effect = [
            (None, None, 0),
            ("https://cdn.example.com/video.m4s", "https://cdn.example.com/audio.m4s", 80),
        ]
        tasks = [_task("BV1fail0001"), _task("BV1ok000001")]

        success_count, failure_count = spider._process_download_tasks_async(tasks)

        self.assertEqual((success_count, failure_count), (1, 1))
        spider.emit_video.assert_called_once()

    def test_empty_v_url_resolve_returns_none(self):
        spider = _spider()
        spider.api.get_play_url.return_value = (False, None, 0)

        self.assertIsNone(spider._resolve_download_item(_task()))
        spider.emit_video.assert_not_called()

    def test_video_only_dash_without_durl_is_explicit_failure(self):
        parser = BilibiliParser()

        with self.assertRaises(SpiderParseError):
            parser.parse_play_url_response(
                {
                    "code": 0,
                    "data": {
                        "dash": {
                            "video": [{"baseUrl": "https://cdn.example.com/video.m4s", "id": 80}],
                            "audio": [],
                        }
                    },
                }
            )

    def test_single_durl_degrades_when_dash_audio_is_missing(self):
        parser = BilibiliParser()

        result = parser.parse_play_url_response(
            {
                "code": 0,
                "data": {
                    "quality": 64,
                    "dash": {"video": [{"baseUrl": "https://cdn.example.com/video.m4s", "id": 80}], "audio": []},
                    "durl": [{"url": "https://cdn.example.com/muxed.mp4", "size": 10}],
                },
            }
        )

        self.assertEqual(result, ("https://cdn.example.com/muxed.mp4", None, 64))

    def test_fallback_skips_video_page_after_empty_static_search(self):
        spider = _spider()
        spider.keyword = "BV19nRWBtEnF合集"
        spider.raw_bv_queue = Mock()
        spider.browser_finished = Mock()
        spider._scan_with_browser_queue = Mock(return_value=[])

        spider._producer_browser_task()

        search_calls = [
            call
            for call in spider._scan_with_browser_queue.call_args_list
            if "search.bilibili.com" in str(call.args[0])
        ]
        video_calls = [
            call
            for call in spider._scan_with_browser_queue.call_args_list
            if "www.bilibili.com/video/" in str(call.args[0])
        ]
        self.assertGreaterEqual(len(search_calls), 1)
        self.assertEqual(video_calls, [])
        self.assertTrue(any("不再启动浏览器" in str(call.args[0]) for call in spider.log.call_args_list))

    def test_static_empty_search_does_not_launch_browser(self):
        spider = _spider()
        response = Mock(status_code=200, text="没有找到相关结果", url="https://search.bilibili.com/all?page=1")

        with patch("app.spiders.bilibili.spider.requests.get", return_value=response) as mocked_get, patch(
            "app.spiders.bilibili.spider.sync_playwright"
        ) as mocked_playwright:
            found = spider._scan_with_browser_queue(
                "https://search.bilibili.com/all?keyword=BV19nRWBtEnF",
                max_pages=3,
                is_search=True,
            )

        self.assertEqual(found, [])
        self.assertEqual(mocked_get.call_count, 1)
        mocked_playwright.assert_not_called()

    def test_static_search_stops_when_page_number_does_not_advance(self):
        spider = _spider()
        first = Mock(
            status_code=200,
            text='<div class="video-list"><div class="bili-video-card"><a href="/video/BV1xgDNBUEXg">one</a></div></div>',
            url="https://search.bilibili.com/all?keyword=BV19nRWBtEnF&page=1",
        )
        second = Mock(
            status_code=200,
            text='<div class="video-list"><div class="bili-video-card"><a href="/video/BV1QY411b7Kf">two</a></div></div>',
            url="https://search.bilibili.com/all?keyword=BV19nRWBtEnF&page=1",
        )

        with patch("app.spiders.bilibili.spider.requests.get", side_effect=[first, second]):
            count = spider._scan_static_bilibili_candidates(
                "https://search.bilibili.com/all?keyword=BV19nRWBtEnF",
                max_pages=3,
                bv_set=set(),
                enqueue=True,
            )

        self.assertEqual(count, 1)
        spider.raw_bv_queue.put.assert_called_once_with("BV1xgDNBUEXg")

    def test_search_cards_ignore_rank_links(self):
        html = """
            <div class="video-list">
              <div class="bili-video-card"><a href="/video/BV1xgDNBUEXg">result</a></div>
            </div>
            <div class="rank-list">
              <div class="bili-video-card"><a href="/video/BV1QY411b7Kf">rank</a></div>
            </div>
        """

        hrefs = BilibiliSpider._extract_search_result_hrefs_from_text(html)

        self.assertEqual(hrefs, ["https://www.bilibili.com/video/BV1xgDNBUEXg"])

    def test_empty_episodes_are_skipped_as_parse_failures(self):
        spider = _spider()
        spider.task_builder = BilibiliTaskBuilder(BilibiliParser())
        spider.ask_user_selection = Mock()
        cached = {
            0: {
                "type": "single",
                "info": {"title": "空分集", "bvid": "BV1empty001", "episodes": []},
            }
        }

        tasks = spider._build_selected_download_tasks([0], cached)

        self.assertEqual(tasks, [])
        spider.ask_user_selection.assert_not_called()
        self.assertTrue(any("解析失败" in str(call.args[0]) for call in spider.log.call_args_list))

    def test_episode_index_out_of_range_is_skipped(self):
        spider = _spider()
        spider.task_builder = BilibiliTaskBuilder(BilibiliParser())
        spider.ask_user_selection = Mock(return_value=[4])
        cached = {
            0: {
                "type": "season",
                "info": {
                    "title": "合集",
                    "season_title": "合集",
                    "episodes": [{"title": "只有一集", "bvid": "BV1only0001", "cid": 1, "page_num": 1}],
                },
            }
        }

        tasks = spider._build_selected_download_tasks([0], cached)

        self.assertEqual(tasks, [])
        self.assertTrue(any("超出范围" in str(call.args[0]) for call in spider.log.call_args_list))

    def test_stopped_submit_pool_shuts_down_executor_and_session(self):
        spider = _spider()
        spider.config["api_workers"] = 2
        spider._close_worker_apis = Mock()
        shutdowns = []

        class FakeFuture:
            def result(self):
                spider.is_running = False
                return None

        class FakeExecutor:
            def __init__(self, *args, **kwargs):
                del args, kwargs

            def submit(self, fn, task):
                del fn, task
                return FakeFuture()

            def shutdown(self, wait=True, cancel_futures=False):
                shutdowns.append({"wait": wait, "cancel_futures": cancel_futures})

        tasks = [_task("BV1aaaaaaaa"), _task("BV1bbbbbbbb")]
        with patch("app.spiders.bilibili.spider.ThreadPoolExecutor", FakeExecutor), patch(
            "app.spiders.bilibili.spider.as_completed",
            side_effect=lambda futures: list(futures),
        ):
            success_count, failure_count = spider._process_download_tasks_async(tasks)

        self.assertEqual(success_count, 0)
        self.assertGreaterEqual(failure_count, 1)
        self.assertEqual(shutdowns, [{"wait": True, "cancel_futures": True}])
        spider.api.close.assert_called()

    def test_login_browser_error_ends_task_instead_of_guest_scan(self):
        spider = BilibiliSpider("keyword", {})
        spider.log = Mock()
        spider.debug_state = Mock()
        spider._perform_login_scan = Mock(side_effect=PlaywrightError("browser crashed"))
        api = Mock()
        api.check_login.return_value = False

        with patch("app.spiders.bilibili.spider.BiliAPI", return_value=api), patch(
            "app.spiders.bilibili.spider.threading.Thread"
        ) as thread_cls:
            spider.run()

        thread_cls.assert_not_called()
        self.assertFalse(spider.run_succeeded)
        messages = [str(call.args[0]) for call in spider.log.call_args_list if call.args]
        self.assertTrue(any("任务结束" in message for message in messages))
        self.assertFalse(any("以游客身份爬取" in message for message in messages))

    def test_login_oserror_while_stopped_is_cancellation(self):
        spider = _spider()
        spider.is_running = False
        spider._interrupt_requested = True
        spider._playwright_browser = None

        with patch("app.spiders.bilibili.spider.sync_playwright", side_effect=OSError("closed")):
            with self.assertRaises(LoginCancelledError):
                spider._perform_login_scan("cookie.json")

    def test_static_search_keeps_bv_inside_its_own_card(self):
        html = """
            <div class="video-list">
              <div class="bili-video-card"><span>空卡片</span></div>
              <a href="/video/BV1GAP000001">gap</a>
              <div class="bili-video-card"><a href="/video/BV1OWN000001">two</a></div>
            </div>
        """

        hrefs = BilibiliSpider._extract_search_result_hrefs_from_text(html)

        self.assertEqual(hrefs, ["https://www.bilibili.com/video/BV1OWN000001"])

    def test_empty_scan_marks_run_succeeded(self):
        spider = BilibiliSpider("keyword", {})
        spider.log = Mock()
        spider.debug_state = Mock()
        spider._emit_finished = Mock()
        spider._close_worker_apis = Mock()
        spider._collect_api_failures = Mock(return_value=[])
        spider._try_api_failure_browser_fallback = Mock(return_value=0)
        spider._log_api_failure_summary = Mock()
        api = Mock()
        api.check_login.return_value = True

        def make_thread(*_args, **kwargs):
            thread = Mock()
            thread.is_alive.return_value = False

            def start():
                if kwargs.get("name") == "bilibili-api":
                    spider.api_pool_finished.set()
                if kwargs.get("name") == "bilibili-browser":
                    spider.browser_finished.set()

            thread.start.side_effect = start
            return thread

        with patch("app.spiders.bilibili.spider.BiliAPI", return_value=api), patch(
            "app.spiders.bilibili.spider.threading.Thread",
            side_effect=make_thread,
        ):
            spider.run()

        self.assertTrue(spider.run_succeeded)
        spider._emit_finished.assert_called_once()

    def test_stop_before_scan_does_not_mark_run_succeeded(self):
        spider = BilibiliSpider("keyword", {})
        spider.log = Mock()
        spider.debug_state = Mock()
        spider.is_running = False
        spider._interrupt_requested = True

        with patch("app.spiders.bilibili.spider.BiliAPI") as api_cls, patch(
            "app.spiders.bilibili.spider.threading.Thread"
        ) as thread_cls:
            spider.run()

        api_cls.assert_not_called()
        thread_cls.assert_not_called()
        self.assertFalse(spider.run_succeeded)
