"""Douyin retry_lite 必须在停止标志置位后结束重试。"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.core.lib.douyin.tools.retry import Retry


class RetryLiteStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_lite_stops_before_another_attempt(self):
        calls: list[int] = []
        owner = SimpleNamespace(_stop_requested=False)

        @Retry.retry_lite
        async def attempt(target):
            calls.append(1)
            target._stop_requested = True
            return None

        with patch("app.core.lib.douyin.tools.retry.wait", new=AsyncMock()) as wait:
            result = await attempt(owner)

        self.assertIsNone(result)
        self.assertEqual(calls, [1])
        wait.assert_not_awaited()

    async def test_retry_lite_does_not_call_when_already_stopped(self):
        calls: list[int] = []
        owner = SimpleNamespace(_stop_check=lambda: True)

        @Retry.retry_lite
        async def attempt(target):
            calls.append(1)
            return target

        result = await attempt(owner)

        self.assertIsNone(result)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
