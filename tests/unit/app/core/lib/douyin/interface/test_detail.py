import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.core.lib.douyin.interface.detail import Detail
from app.core.lib.douyin.interface.template import API


class DetailResponseIsolationTests(unittest.IsolatedAsyncioTestCase):
    def _detail(self, detail_id: str, response):
        detail = object.__new__(Detail)
        detail.detail_id = detail_id
        detail.response = response
        detail.log = SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        )
        return detail

    async def test_run_clears_response_before_request_and_keeps_matching_id(self):
        detail = self._detail("2222222222222222222", {"aweme_id": "1111111111111111111"})
        seen = {}

        async def fake_run(self, *args, **kwargs):
            del args, kwargs
            seen["during"] = self.response
            self.response = {"aweme_id": self.detail_id}
            return self.response

        with patch.object(API, "run", fake_run):
            result = await Detail.run(detail)

        self.assertEqual(seen["during"], [])
        self.assertEqual(result["aweme_id"], "2222222222222222222")

    async def test_failed_request_does_not_return_previous_success(self):
        detail = self._detail("2222222222222222222", {"aweme_id": "1111111111111111111"})

        async def fake_run(self, *args, **kwargs):
            del args, kwargs
            return self.response

        with patch.object(API, "run", fake_run):
            result = await Detail.run(detail)

        self.assertEqual(result, [])
        self.assertEqual(detail.response, [])

    def test_mismatched_payload_is_not_stored(self):
        detail = self._detail("2222222222222222222", {"aweme_id": "1111111111111111111"})

        detail.check_response(
            {"aweme_detail": {"aweme_id": "1111111111111111111"}},
            "aweme_detail",
        )

        self.assertEqual(detail.response, [])


if __name__ == "__main__":
    unittest.main()
