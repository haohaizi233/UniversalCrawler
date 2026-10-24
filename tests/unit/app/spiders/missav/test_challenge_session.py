from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app.spiders.missav.challenge_session import (
    ChallengeWaitCancelled,
    ExternalChromeChallengeSession,
)


def _session(tmp_path: Path, **kwargs) -> ExternalChromeChallengeSession:
    return ExternalChromeChallengeSession(
        browser_executable=tmp_path / "chrome.exe",
        profile_dir=tmp_path / "profile",
        poll_interval_seconds=0,
        **kwargs,
    )


def test_external_chrome_launch_keeps_native_browser_fingerprint(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, proxy_server="http://127.0.0.1:7890")
    process = Mock()
    process.poll.return_value = None

    with patch(
        "app.spiders.missav.challenge_session.subprocess.Popen", return_value=process
    ) as popen:
        session.start("https://missav.ai/cn/search/CAWD-377")

    command = popen.call_args.args[0]
    assert "--proxy-server=http=127.0.0.1:7890;https=127.0.0.1:7890" in command
    assert "--proxy-bypass-list=localhost;127.0.0.1;::1;<local>" in command
    assert any(item.startswith("--remote-debugging-port=") for item in command)
    assert any(item.startswith("--user-data-dir=") for item in command)
    assert not any("AutomationControlled" in item for item in command)
    assert not any(item.startswith("--user-agent=") for item in command)
    assert not any("headless" in item for item in command)


def test_external_chrome_headless_launch_is_explicit(tmp_path: Path) -> None:
    session = _session(tmp_path, headless=True)
    process = Mock()
    process.poll.return_value = None

    with patch(
        "app.spiders.missav.challenge_session.subprocess.Popen", return_value=process
    ) as popen:
        session.start("https://missav.ai/cn/search/CAWD-377")

    command = popen.call_args.args[0]
    assert "--headless=new" in command


def test_external_chrome_opens_second_url_in_same_profile(tmp_path: Path) -> None:
    session = _session(tmp_path)
    process = Mock()
    process.poll.return_value = None

    with patch(
        "app.spiders.missav.challenge_session.subprocess.Popen", return_value=process
    ) as popen:
        session.start("https://missav.ai/cn/search/CAWD-377")
        session.open_url(
            "https://missav.ai/cn/search/CAWD-377?filters=chinese-subtitle"
        )

    first_command = popen.call_args_list[0].args[0]
    second_command = popen.call_args_list[1].args[0]
    first_profile = next(
        item for item in first_command if item.startswith("--user-data-dir=")
    )
    assert first_profile in second_command
    assert "--new-tab" in second_command
    assert session._processes == [process, process]


def test_direct_launch_disables_inherited_system_proxy(tmp_path: Path) -> None:
    session = _session(tmp_path)
    process = Mock()
    process.poll.return_value = None

    with patch(
        "app.spiders.missav.challenge_session.subprocess.Popen", return_value=process
    ) as popen:
        session.start("https://missav.ai/cn/search/CAWD-377")

    command = popen.call_args.args[0]
    assert "--no-proxy-server" in command
    assert not any(item.startswith("--proxy-server=") for item in command)


def test_close_ends_every_recorded_pid_before_deleting_profile(tmp_path: Path) -> None:
    session = _session(tmp_path)
    order: list[object] = []
    state = {101: None, 202: None}

    def make_process(pid: int) -> Mock:
        process = Mock()
        process.pid = pid

        def poll() -> int | None:
            return state[pid]

        def wait(timeout: float = 5) -> int:
            state[pid] = 0
            order.append(("wait", pid))
            return 0

        process.poll.side_effect = poll
        process.wait.side_effect = wait
        return process

    primary = make_process(101)
    secondary = make_process(202)
    session._processes = [primary, secondary]
    session._process = primary
    profile = Mock()
    profile.cleanup.side_effect = lambda: order.append("cleanup")
    session._temporary_profile = profile

    def run(command, check=False, capture_output=True):
        order.append(("taskkill", int(command[2])))
        return Mock(returncode=0)

    with patch("app.spiders.missav.challenge_session.subprocess.run", side_effect=run):
        session.close()

    assert order == [
        ("taskkill", 101),
        ("taskkill", 202),
        ("wait", 101),
        ("wait", 202),
        "cleanup",
    ]
    primary.kill.assert_not_called()
    secondary.kill.assert_not_called()


def test_taskkill_failure_kills_processes_that_are_still_alive(tmp_path: Path) -> None:
    session = _session(tmp_path)
    process = Mock()
    process.pid = 303
    polls = iter([None, None, 0, 0])
    process.poll.side_effect = lambda: next(polls)
    profile = Mock()
    session._processes = [process]
    session._process = process
    session._temporary_profile = profile

    with patch(
        "app.spiders.missav.challenge_session.subprocess.run",
        return_value=Mock(returncode=1),
    ):
        session.close()

    process.kill.assert_called_once()
    profile.cleanup.assert_called_once()


def test_profile_stays_when_recorded_process_survives_kill(tmp_path: Path) -> None:
    session = _session(tmp_path)
    process = Mock()
    process.pid = 404
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired(["chrome"], 5)
    profile = Mock()
    session._processes = [process]
    session._temporary_profile = profile

    with patch(
        "app.spiders.missav.challenge_session.subprocess.run",
        return_value=Mock(returncode=1),
    ):
        session.close()

    assert process.kill.call_count >= 1
    profile.cleanup.assert_not_called()


def test_wait_for_ready_page_does_not_attach_during_challenge(tmp_path: Path) -> None:
    session = _session(tmp_path)
    challenge = {
        "id": "challenge",
        "type": "page",
        "url": "https://missav.ai/cn/search/CAWD-377?filters=chinese-subtitle",
        "title": "请稍候…",
    }
    ready = {
        **challenge,
        "id": "ready",
        "title": "cawd-377的搜寻结果 - MissAV | 免费高清AV在线看",
    }
    session._read_targets = Mock(side_effect=[[challenge], [ready]])
    challenge_callback = Mock()

    target = session.wait_for_ready_page(
        challenge["url"],
        timeout_seconds=1,
        cancelled=lambda: False,
        on_challenge=challenge_callback,
    )

    assert target.target_id == "ready"
    challenge_callback.assert_called_once_with("请稍候…")


def test_wait_for_ready_page_honours_task_cancellation(tmp_path: Path) -> None:
    session = _session(tmp_path)

    with pytest.raises(ChallengeWaitCancelled):
        session.wait_for_ready_page(
            "https://missav.ai/cn/search/CAWD-377",
            timeout_seconds=1,
            cancelled=lambda: True,
        )


def test_loading_title_does_not_report_cloudflare(tmp_path: Path) -> None:
    session = _session(tmp_path)
    target_url = "https://missav.ai/dm37/cn/actresses/demo"
    loading = {"id": "page", "type": "page", "url": target_url, "title": ""}
    session._read_targets = Mock(
        side_effect=[[loading], [{**loading, "title": "Demo - MissAV"}]]
    )
    reported: list[str] = []

    target = session.wait_for_ready_page(
        target_url, timeout_seconds=1, cancelled=lambda: False,
        on_challenge=reported.append,
    )

    assert target.title == "Demo - MissAV"
    assert reported == []


@pytest.mark.parametrize("path", ["天音唯", "%e5%a4%a9%e9%9f%b3%e5%94%af"])
def test_ready_page_matches_browser_encoded_chinese_path(tmp_path: Path, path: str) -> None:
    session = _session(tmp_path)
    browser_url = "https://missav.ai/dm37/cn/actresses/%E5%A4%A9%E9%9F%B3%E5%94%AF"
    ready = {"id": "ready", "type": "page", "url": browser_url, "title": "天音唯 - MissAV"}
    session._read_targets = Mock(return_value=[ready])

    with patch("app.spiders.missav.challenge_session.time.monotonic", side_effect=[0, 0, 2]):
        target = session.wait_for_ready_page(
            f"https://missav.ai/dm37/cn/actresses/{path}",
            timeout_seconds=1, cancelled=lambda: False,
        )

    assert target.target_id == "ready"


@pytest.mark.parametrize(("candidate", "target"), [
    ("https://evil.example/cn/actresses/天音唯", "https://missav.ai/cn/actresses/天音唯"),
    ("https://missav.ai/cn/actresses/other", "https://missav.ai/cn/actresses/天音唯"),
    ("https://missav.ai/cn/actresses/a%2Fb", "https://missav.ai/cn/actresses/a/b"),
    ("https://missav.ai/cn/actresses/%25E5%25A4%25A9", "https://missav.ai/cn/actresses/天"),
    ("https://missav.ai/cn/actresses/天音唯", "https://missav.ai/cn/actresses/天音唯?filters=individual"),
])
def test_target_matching_keeps_host_and_path_boundaries(candidate: str, target: str) -> None:
    assert not ExternalChromeChallengeSession._urls_match(candidate, target)


@pytest.mark.parametrize("prefix", ["", "/dm37"])
def test_attach_matches_browser_encoded_chinese_path(tmp_path: Path, prefix: str) -> None:
    session = _session(tmp_path)
    page = Mock(url="https://missav.ai/dm37/cn/actresses/%E5%A4%A9%E9%9F%B3%E5%94%AF")
    context = Mock(pages=[page])
    playwright = Mock()
    playwright.chromium.connect_over_cdp.return_value = Mock(contexts=[context])

    attachment = session.attach(playwright, f"https://missav.ai{prefix}/cn/actresses/天音唯")

    assert attachment.page is page


def test_ready_page_recognizes_missav_route_prefix_redirect(tmp_path: Path) -> None:
    session = _session(tmp_path)
    ready = {
        "id": "ready", "type": "page", "title": "天音唯 - MissAV",
        "url": "https://missav.ai/dm37/cn/actresses/%E5%A4%A9%E9%9F%B3%E5%94%AF?page=2",
    }
    session._read_targets = Mock(return_value=[ready])
    with patch("app.spiders.missav.challenge_session.time.monotonic", side_effect=[0, 0, 2]):
        result = session.wait_for_ready_page(
            "https://missav.ai/cn/actresses/天音唯?page=2",
            timeout_seconds=1, cancelled=lambda: False,
        )
    assert result.target_id == "ready"


@pytest.mark.parametrize("candidate", [
    "https://missav.ai/other/cn/actresses/demo?page=2",
    "https://missav.ai/dm37/cn/actresses/other?page=2",
    "https://missav.ai/dm37/cn/actresses/demo?page=3",
    "https://missav.ai/dm37/cn/actresses/demo",
    "https://evil.example/dm37/cn/actresses/demo?page=2",
    "https://missav.ai/dm37/other/actresses/demo?page=2",
])
def test_route_prefix_alias_does_not_weaken_other_boundaries(candidate: str) -> None:
    assert not ExternalChromeChallengeSession._urls_match(
        candidate, "https://missav.ai/cn/actresses/demo?page=2"
    )


def test_attach_selects_the_requested_filtered_tab(tmp_path: Path) -> None:
    session = _session(tmp_path)
    base_page = Mock(url="https://missav.ai/cn/search/CAWD-377")
    filtered_page = Mock(
        url="https://missav.ai/cn/search/CAWD-377?filters=chinese-subtitle"
    )
    context = Mock(pages=[base_page, filtered_page])
    browser = Mock(contexts=[context])
    playwright = Mock()
    playwright.chromium.connect_over_cdp.return_value = browser

    attachment = session.attach(
        playwright,
        "https://missav.ai/cn/search/CAWD-377?filters=chinese-subtitle",
    )

    assert attachment.browser is browser
    assert attachment.context is context
    assert attachment.page is filtered_page
    playwright.chromium.connect_over_cdp.assert_called_once_with(
        session.cdp_endpoint,
        timeout=session.CDP_CONNECT_TIMEOUT_MS,
    )
