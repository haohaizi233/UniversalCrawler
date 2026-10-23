"""Runtime log translation behavior for newly added diagnostics."""

from __future__ import annotations

import pytest

from shared.log_i18n import localize_log_text


@pytest.mark.parametrize(
    ("source", "english", "traditional"),
    [
        (
            "⚠️ 抖音号格式无效，请使用 @账号名（1-20 位字母、数字、下划线或点）",
            "⚠️ Invalid Douyin ID format; use an @handle (1-20 letters, digits, underscores, or dots)",
            "⚠️ 抖音號格式無效，請使用 @帳號名（1-20 位字母、數字、底線或點）",
        ),
        (
            "❌ 未捕获到可下载媒体流，未提交任何任务",
            "❌ No downloadable media stream was captured; no tasks were submitted",
            "❌ 未擷取到可下載媒體串流，未提交任何任務",
        ),
        (
            "N_m3u8DL-RE 执行失败，保留工具输出以定位缺失依赖",
            "N_m3u8DL-RE execution failed; retained tool output to help identify the missing dependency",
            "N_m3u8DL-RE 執行失敗，保留工具輸出以協助定位缺少的相依性",
        ),
    ],
)
def test_runtime_log_messages_translate_between_supported_languages(
    source: str,
    english: str,
    traditional: str,
) -> None:
    assert localize_log_text(source, "en-US") == english
    assert localize_log_text(english, "zh-CN") == source
    assert localize_log_text(english, "zh-TW") == traditional
    assert localize_log_text(source, "zh-CN") == source
    assert localize_log_text(source, "zh-TW") == traditional


@pytest.mark.parametrize(("submitted", "total"), [(1, 3), (2, 5)])
def test_partial_capture_translation_preserves_counts_emoji_and_punctuation(
    submitted: int,
    total: int,
) -> None:
    source = f"⚠️ 捕获不完整: 已提交 {submitted}/{total} 个下载任务"
    english = f"⚠️ Capture incomplete: submitted {submitted}/{total} download tasks"
    traditional = f"⚠️ 捕獲不完整: 已提交 {submitted}/{total} 個下載任務"

    assert localize_log_text(source, "en-US") == english
    assert localize_log_text(english, "zh-CN") == source
    assert localize_log_text(english, "zh-TW") == traditional
    assert localize_log_text(source, "zh-CN") == source
    assert localize_log_text(source, "zh-TW") == traditional
