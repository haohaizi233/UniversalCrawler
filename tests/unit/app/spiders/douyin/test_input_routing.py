"""Regression coverage for Douyin keyword and account input routing."""

from __future__ import annotations

import asyncio
from unittest.mock import ANY, AsyncMock, Mock

import pytest

from app.spiders.douyin.spider import DouyinSpider


def _spider() -> DouyinSpider:
    spider = DouyinSpider.__new__(DouyinSpider)
    spider.log = Mock()
    spider._process_search = AsyncMock()
    spider._process_user_search = AsyncMock()
    spider._process_user = AsyncMock()
    spider._process_mix = AsyncMock()
    spider._process_detail = AsyncMock()
    return spider


@pytest.mark.parametrize("keyword", ["风景", "美食", "travel", "travel2026", "win10"])
def test_plain_keywords_route_to_video_search(keyword: str) -> None:
    spider = _spider()

    asyncio.run(spider._route_input(Mock(), keyword, Mock()))

    spider._process_search.assert_awaited_once_with(ANY, keyword)
    spider._process_user_search.assert_not_awaited()


@pytest.mark.parametrize(
    ("account_input", "account_id"),
    [
        ("@testuser123", "testuser123"),
        ("@travelcreator", "travelcreator"),
        ("@creator_name", "creator_name"),
        ("@creator.name", "creator.name"),
        ("@creator.name_1234567", "creator.name_1234567"),
    ],
)
def test_explicit_account_handle_routes_to_user_search(
    account_input: str,
    account_id: str,
) -> None:
    spider = _spider()
    params = object()

    asyncio.run(spider._route_input(params, account_input, Mock()))

    spider._process_user_search.assert_awaited_once_with(params, account_id)
    spider._process_search.assert_not_awaited()


@pytest.mark.parametrize(
    "account_input",
    ["@", "@creator-name", "@creator name", "@abcdefghijklmnopqrstu"],
)
def test_invalid_explicit_account_handle_is_rejected(
    account_input: str,
) -> None:
    spider = _spider()

    asyncio.run(spider._route_input(Mock(), account_input, Mock()))

    spider._process_user_search.assert_not_awaited()
    spider._process_search.assert_not_awaited()
    assert any("抖音号格式无效" in str(call) for call in spider.log.call_args_list)
