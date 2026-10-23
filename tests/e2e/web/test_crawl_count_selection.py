"""Browser regressions for the WebUI crawl count control."""

from __future__ import annotations

import unittest

import pytest

from tests.support.browser_runtime import WebUIBrowserTestBase, _playwright_available


@pytest.mark.browser
@pytest.mark.slow
@unittest.skipUnless(_playwright_available(), "playwright not installed")
class CrawlCountSelectionBrowserTests(WebUIBrowserTestBase):
    def test_user_count_survives_settings_refresh_and_is_sent_to_crawl(self):
        self._goto_ready()

        setup = self._page.evaluate(
            """
            () => {
              window.__isolateFrontendStateForTest();
              const platformRows = [
                {
                  id: "douyin",
                  name: "Douyin",
                  default_count: 20,
                  count_config_key: "max_items",
                  count_unit: "videos",
                  count_options: [
                    { value: "20", label: "20 videos" },
                    { value: "30", label: "30 videos" },
                    { value: "50", label: "50 videos" }
                  ]
                },
                {
                  id: "bilibili",
                  name: "Bilibili",
                  default_count: 1,
                  count_config_key: "max_pages",
                  count_unit: "pages",
                  count_options: [{ value: "1", label: "1 page" }]
                }
              ];
              frontendState.settings_snapshot = {
                "基础设置": { last_source: "" },
                "外观设置": { theme: "light", accent: "blue", language: "zh-CN" },
                "平台设置": platformRows
              };
              platforms = platformRows.map(({ id, name }) => ({ id, name }));
              const sourceSelect = document.getElementById("sourceSelect");
              sourceSelect.innerHTML = platforms.map(({ id, name }) => `<option value="${id}">${name}</option>`).join("");
              syncCustomSelectForSelect(sourceSelect);
              sourceSelect.value = "douyin";
              const countSelect = document.getElementById("videoCountSelect");
              updatePlaceholder();
              const initialValue = countSelect.value;

              platformRows[0].default_count = 30;
              renderFrontendSections(new Set(["settings_snapshot"]));
              const updatedDefault = countSelect.value;
              return { initialValue, updatedDefault };
            }
            """
        )

        self.assertEqual(setup["initialValue"], "20")
        self.assertEqual(setup["updatedDefault"], "30")

        countSelect = self._page.locator(".custom-select-count")
        countSelect.locator(".custom-select-button").click()
        countSelect.locator('.custom-select-option[data-value="50"]').click()

        result = self._page.evaluate(
            """
            () => {
              const countSelect = document.getElementById("videoCountSelect");
              frontendState.settings_snapshot["外观设置"].theme = "dark";
              renderFrontendSections(new Set(["settings_snapshot"]));
              const valueAfterThemeRefresh = countSelect.value;

              window.__crawlActions = [];
              window.sendWS = (action, payload) => {
                window.__crawlActions.push({ action, payload });
                return true;
              };
              sendWS = window.sendWS;
              document.getElementById("searchInput").value = "fixture-keyword";
              startCrawl();
              return {
                valueAfterThemeRefresh,
                actions: window.__crawlActions
              };
            }
            """
        )

        self.assertEqual(result["valueAfterThemeRefresh"], "50")
        self.assertEqual(result["actions"][0]["action"], "start_crawl")
        self.assertEqual(result["actions"][0]["payload"]["config"]["max_items"], 50)

    def test_platform_and_unit_changes_use_their_own_default(self):
        self._goto_ready()

        self._page.evaluate(
            """
            () => {
              window.__isolateFrontendStateForTest();
              const platformRows = [
                {
                  id: "douyin",
                  name: "Douyin",
                  default_count: 20,
                  count_config_key: "max_items",
                  count_unit: "videos",
                  count_options: [{ value: "20" }, { value: "50" }]
                },
                {
                  id: "bilibili",
                  name: "Bilibili",
                  default_count: 1,
                  count_config_key: "max_pages",
                  count_unit: "pages",
                  count_options: [{ value: "1" }, { value: "2" }]
                },
                {
                  id: "xiaohongshu",
                  name: "Xiaohongshu",
                  default_count: 10,
                  count_config_key: "max_notes",
                  count_unit: "notes",
                  count_options: [{ value: "10" }, { value: "20" }]
                }
              ];
              frontendState.settings_snapshot = {
                "基础设置": { last_source: "" },
                "平台设置": platformRows
              };
              platforms = platformRows.map(({ id, name }) => ({ id, name }));
              const sourceSelect = document.getElementById("sourceSelect");
              sourceSelect.innerHTML = platforms.map(({ id, name }) => `<option value="${id}">${name}</option>`).join("");
              syncCustomSelectForSelect(sourceSelect);
              const countSelect = document.getElementById("videoCountSelect");
              sourceSelect.value = "douyin";
              updatePlaceholder();
            }
            """
        )

        countSelect = self._page.locator(".custom-select-count")
        countSelect.locator(".custom-select-button").click()
        countSelect.locator('.custom-select-option[data-value="50"]').click()
        sourceSelect = self._page.locator(".custom-select-source")
        sourceSelect.locator(".custom-select-button").click()
        sourceSelect.locator('.custom-select-option[data-value="bilibili"]').click()

        page = self._page.evaluate(
            """
            () => {
              const countSelect = document.getElementById("videoCountSelect");
              const pageDefault = countSelect.value;
              const pageLabel = document.querySelector(".count-label").textContent;

              const platformRows = frontendState.settings_snapshot["平台设置"];
              platformRows[1].count_unit = "notes";
              platformRows[1].count_config_key = "max_notes";
              platformRows[1].default_count = 10;
              platformRows[1].count_options = [{ value: "10" }, { value: "20" }];
              renderFrontendSections(new Set(["settings_snapshot"]));
              const changedUnitDefault = countSelect.value;
              const changedUnitLabel = document.querySelector(".count-label").textContent;
              return { pageDefault, pageLabel, changedUnitDefault, changedUnitLabel };
            }
            """
        )

        sourceSelect = self._page.locator(".custom-select-source")
        sourceSelect.locator(".custom-select-button").click()
        sourceSelect.locator('.custom-select-option[data-value="xiaohongshu"]').click()

        result = self._page.evaluate(
            """
            () => {
              const countSelect = document.getElementById("videoCountSelect");
              const otherPlatformDefault = countSelect.value;
              const otherPlatformLabel = document.querySelector(".count-label").textContent;
              return { otherPlatformDefault, otherPlatformLabel };
            }
            """
        )

        self.assertEqual(page["pageDefault"], "1")
        self.assertIn("页", page["pageLabel"])
        self.assertEqual(page["changedUnitDefault"], "10")
        self.assertIn("笔记", page["changedUnitLabel"])
        self.assertEqual(result["otherPlatformDefault"], "10")
        self.assertIn("笔记", result["otherPlatformLabel"])

    def test_missing_or_invalid_count_options_fall_back_to_unit_options(self):
        self._goto_ready()

        result = self._page.evaluate(
            """
            () => {
              window.__isolateFrontendStateForTest();
              const row = {
                id: "douyin",
                name: "Douyin",
                default_count: 30,
                count_config_key: "max_items",
                count_unit: "videos"
              };
              frontendState.settings_snapshot = {
                "基础设置": { last_source: "" },
                "平台设置": [row]
              };
              platforms = [{ id: row.id, name: row.name }];
              const sourceSelect = document.getElementById("sourceSelect");
              sourceSelect.innerHTML = `<option value="${row.id}">${row.name}</option>`;
              sourceSelect.value = row.id;
              updatePlaceholder();
              const countSelect = document.getElementById("videoCountSelect");
              const missingOptions = Array.from(countSelect.options).map(option => option.value);
              const missingOptionsDefault = countSelect.value;

              row.default_count = 40;
              row.count_options = [{ value: "" }, {}, null];
              renderFrontendSections(new Set(["settings_snapshot"]));
              return {
                missingOptions,
                missingOptionsDefault,
                invalidOptions: Array.from(countSelect.options).map(option => option.value),
                invalidOptionsDefault: countSelect.value
              };
            }
            """
        )

        self.assertEqual(result["missingOptionsDefault"], "30")
        self.assertTrue({"10", "20", "30", "50", "9999"}.issubset(set(result["missingOptions"])))
        self.assertEqual(result["invalidOptionsDefault"], "40")
        self.assertTrue({"10", "20", "30", "50", "9999"}.issubset(set(result["invalidOptions"])))
        self.assertIn("40", result["invalidOptions"])
