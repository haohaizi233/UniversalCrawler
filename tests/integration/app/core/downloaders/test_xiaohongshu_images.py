"""小红书无扩展名图片从任务构建到下载完成的回归。"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core.download_manager import DownloadWorker
from app.spiders.xiaohongshu.task_builder import XiaohongshuTaskBuilder


@pytest.mark.parametrize(
    ("payload", "suffix"),
    [
        (b"\xff\xd8\xff\xe0" + b"image-data", ".jpg"),
        (b"RIFF\x10\x00\x00\x00WEBP" + b"image-data", ".webp"),
        (b"\x89PNG\r\n\x1a\n" + b"image-data", ".png"),
    ],
)
def test_extensionless_image_is_published_with_detected_suffix(
    tmp_path, monkeypatch, payload, suffix
):
    url = "http://sns-webpic-qc.xhscdn.com/spectrum/opaque!nd_dft_wlteh_webp_3"
    item = XiaohongshuTaskBuilder().build_items(
        {"note_id": "note-1", "title": "note", "images_data": [{"image_url": url}]},
        trace_id_factory=lambda _prefix: "xhs-regression",
        referer="https://www.xiaohongshu.com/explore/note-1",
        user_agent="regression-agent",
        cookie_str="test-cookie",
    )[0]
    response = Mock()
    response.headers = {"Content-Length": str(len(payload)), "Content-Type": "image/webp"}
    response.status_code = 200
    response.iter_content.return_value = [payload]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    request = Mock(return_value=response)
    monkeypatch.setattr("app.core.downloaders.base.requests.get", request)
    monkeypatch.setattr("app.core.downloaders.xiaohongshu.cfg.get", lambda _s, _k, default=None: default)
    worker = DownloadWorker(item, str(tmp_path))
    finished, errors = [], []
    worker.sig_finished.connect(finished.append)
    worker.sig_error.connect(lambda *args: errors.append(args))

    worker.run()

    assert errors == []
    assert finished == [item.id]
    assert Path(item.local_path).suffix == suffix
    assert Path(item.local_path).read_bytes() == payload
    assert item.meta["output_filename"] == Path(item.local_path).name
    request.assert_called_once()
    assert request.call_args.args[0] == url
    assert request.call_args.kwargs["headers"]["Referer"] == item.meta["referer"]
    assert request.call_args.kwargs["headers"]["Cookie"] == "test-cookie"
    assert not list(tmp_path.rglob("*.mp4"))
    assert not list(tmp_path.rglob("*.downloading"))
    assert not list(tmp_path.rglob("*.ucrawl-reserve"))


@pytest.mark.parametrize("payload", [b"<html>access denied</html>", b"MZexecutable"])
def test_extensionless_image_rejects_non_media_payload(tmp_path, monkeypatch, payload):
    from app.models import VideoItem

    item = VideoItem(url="https://cdn.example/opaque", title="image", source="xiaohongshu")
    item.meta["content_type"] = "image"
    response = Mock()
    response.headers = {"Content-Length": str(len(payload))}
    response.status_code = 200
    response.iter_content.return_value = [payload]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("app.core.downloaders.base.requests.get", Mock(return_value=response))
    monkeypatch.setattr("app.core.downloaders.xiaohongshu.cfg.get", lambda _s, _k, default=None: default)
    worker = DownloadWorker(item, str(tmp_path))
    finished, errors = [], []
    worker.sig_finished.connect(finished.append)
    worker.sig_error.connect(lambda *args: errors.append(args))

    worker.run()

    assert finished == []
    assert len(errors) == 1
    assert "小红书图片下载失败" in errors[0][1]
    assert item.local_path == ""
    assert not list(tmp_path.iterdir())
