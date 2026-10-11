from app.spiders.missav.playlist_capture import PlaylistCapture


MASTER_LOW = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=640x360
low.m3u8
"""
MASTER_HIGH = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1920x1080
high.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720
mid.m3u8
"""
MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXTINF:4.0,
segment.ts
"""


def test_master_playlist_replaces_media_playlist() -> None:
    capture = PlaylistCapture()
    capture.observe("https://cdn.example/1080p/video.m3u8", 200, {"Referer": "a"}, MEDIA)
    capture.observe("https://cdn.example/playlist.m3u8", 200, {"Referer": "b"}, MASTER_LOW)

    submission = capture.submission()
    assert submission is not None
    url, cache = submission
    assert url == "https://cdn.example/playlist.m3u8"
    assert cache == {url: MASTER_LOW}
    assert list(cache) == [url]


def test_media_only_keeps_highest_bandwidth_then_resolution() -> None:
    capture = PlaylistCapture()
    low = MEDIA + "BANDWIDTH=400000\nRESOLUTION=1920x1080\n"
    high_bandwidth = MEDIA + "BANDWIDTH=900000\nRESOLUTION=640x360\n"
    capture.observe("https://cdn.example/low.m3u8", 200, {}, low)
    capture.observe("https://cdn.example/high.m3u8", 206, {}, high_bandwidth)
    capture.observe("https://cdn.example/720p/tied.m3u8", 200, {}, MEDIA)
    capture.observe("https://cdn.example/1080p/later.m3u8", 200, {}, MEDIA)

    assert capture.url == "https://cdn.example/high.m3u8"
    assert capture.cache[capture.url] == high_bandwidth


def test_media_resolution_breaks_bandwidth_ties() -> None:
    capture = PlaylistCapture()
    small = MEDIA + "BANDWIDTH=800000\nRESOLUTION=640x360\n"
    large = MEDIA + "BANDWIDTH=800000\nRESOLUTION=1920x1080\n"
    capture.observe("https://cdn.example/small.m3u8", 200, {}, small)
    capture.observe("https://cdn.example/large.m3u8", 200, {}, large)

    assert capture.url == "https://cdn.example/large.m3u8"


def test_failed_response_does_not_replace_confirmed_playlist() -> None:
    capture = PlaylistCapture()
    capture.observe("https://cdn.example/playlist.m3u8", 200, {"Referer": "ok"}, MASTER_HIGH)
    capture.observe("https://cdn.example/other.m3u8", 403, {"Referer": "bad"}, MASTER_LOW)
    capture.observe("https://cdn.example/playlist.m3u8", 500, {}, "Forbidden")

    submission = capture.submission()
    assert submission is not None
    url, cache = submission
    assert url == "https://cdn.example/playlist.m3u8"
    assert cache[url] == MASTER_HIGH
    assert capture.selected is not None
    assert capture.selected.status == 200
    assert capture.selected.headers == {"Referer": "ok"}


def test_higher_master_replaces_lower_master_and_refreshes_same_url() -> None:
    capture = PlaylistCapture()
    capture.observe("https://cdn.example/low.m3u8", 200, {}, MASTER_LOW)
    capture.observe("https://cdn.example/high.m3u8", 200, {}, MASTER_HIGH)
    refreshed = MASTER_HIGH + "# refreshed\n"
    capture.observe("https://cdn.example/high.m3u8", 200, {}, refreshed)

    assert capture.url == "https://cdn.example/high.m3u8"
    assert capture.cache == {"https://cdn.example/high.m3u8": refreshed}


def test_non_playlist_body_is_not_confirmed() -> None:
    capture = PlaylistCapture()
    capture.observe("https://cdn.example/playlist.m3u8", 200, {}, "not a playlist")

    assert capture.submission() is None
