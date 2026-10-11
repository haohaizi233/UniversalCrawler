from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.spiders.base import BaseSpider
from app.spiders.missav.browser_executable import resolve_challenge_browser
from app.spiders.missav.challenge_session import SystemBrowserUnavailable
from app.spiders.missav.local_proxy import (
    chromium_proxy_switch,
    discover_client_proxy,
    endpoints_from_config_text,
    proxy_from_pac_text,
)
from app.spiders.missav.spider import MissAVSpider


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return path


def _isolate_browser_search(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(
        "app.spiders.missav.browser_executable.shutil.which",
        lambda _name: None,
    )
    monkeypatch.setenv("PROGRAMFILES", str(root / "missing-program-files"))
    monkeypatch.setenv("PROGRAMFILES(X86)", str(root / "missing-program-files-x86"))
    monkeypatch.setenv("LOCALAPPDATA", str(root / "missing-local"))
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("UCP_BROWSER_EXECUTABLE", raising=False)
    monkeypatch.setattr(
        "app.spiders.missav.browser_executable._bundled_playwright_chromium",
        lambda: None,
    )


def test_browser_search_skips_windowsapps_alias_and_prefers_real_chrome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias = _touch(tmp_path / "WindowsApps" / "msedge.exe")
    chrome = _touch(tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe")
    _isolate_browser_search(monkeypatch, tmp_path)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.setattr(
        "app.spiders.missav.browser_executable.shutil.which",
        lambda name: str(alias) if name == "msedge.exe" else None,
    )

    assert resolve_challenge_browser() == chrome


def test_explicit_browser_and_playwright_path_override_system_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edge = _touch(tmp_path / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    custom = _touch(tmp_path / "custom" / "chrome.exe")
    playwright_root = tmp_path / "browsers"
    playwright_chrome = _touch(
        playwright_root / "chromium-1208" / "chrome-win" / "chrome.exe"
    )
    _isolate_browser_search(monkeypatch, tmp_path)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.setenv("UCP_BROWSER_EXECUTABLE", str(custom))

    assert resolve_challenge_browser() == custom

    monkeypatch.delenv("UCP_BROWSER_EXECUTABLE")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(playwright_root))

    assert resolve_challenge_browser() == playwright_chrome
    assert edge.is_file()


def test_packaged_playwright_path_does_not_hide_system_chrome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chrome = _touch(tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe")
    bundle = tmp_path / "bundle"
    playwright_root = bundle / "ms-playwright"
    _touch(playwright_root / "chromium-1187" / "chrome-win64" / "chrome.exe")
    _isolate_browser_search(monkeypatch, tmp_path)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(playwright_root))
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)

    assert resolve_challenge_browser() == chrome


def test_missing_explicit_browser_falls_back_to_system_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edge = _touch(tmp_path / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    _isolate_browser_search(monkeypatch, tmp_path)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))

    assert resolve_challenge_browser(str(tmp_path / "missing-chrome.exe")) == edge


def test_browser_search_raises_when_nothing_is_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_browser_search(monkeypatch, tmp_path)

    with pytest.raises(SystemBrowserUnavailable):
        resolve_challenge_browser()


def test_clash_verge_config_prefers_listening_mixed_port() -> None:
    text = "enable_system_proxy: false\nverge_mixed_port: 7897\nport: 7890\nsocks-port: 7898\n"

    assert endpoints_from_config_text(text) == [
        "http://127.0.0.1:7897",
        "http://127.0.0.1:7890",
        "socks5://127.0.0.1:7898",
    ]


def test_pac_only_accepts_loopback_proxy() -> None:
    assert proxy_from_pac_text('return "PROXY 127.0.0.1:7897; DIRECT";') == "http://127.0.0.1:7897"
    assert proxy_from_pac_text('return "SOCKS5 127.0.0.1:7898";') == "socks5://127.0.0.1:7898"
    assert proxy_from_pac_text('return "PROXY 8.8.8.8:80";') is None


def test_http_proxy_switch_covers_https() -> None:
    assert chromium_proxy_switch("http://127.0.0.1:7897") == (
        "--proxy-server=http=127.0.0.1:7897;https=127.0.0.1:7897"
    )
    assert chromium_proxy_switch("socks5://127.0.0.1:7898") == "--proxy-server=socks5://127.0.0.1:7898"


def test_discover_client_proxy_skips_closed_verge_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(BaseSpider, "_proxy_from_windows_settings", classmethod(lambda cls: None))
    monkeypatch.setattr(
        "app.spiders.missav.local_proxy.proxy_from_windows_pac",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.spiders.missav.local_proxy.endpoints_from_client_configs",
        lambda files=None: ["http://127.0.0.1:7897", "http://127.0.0.1:7890"],
    )
    monkeypatch.setattr(
        "app.spiders.missav.local_proxy.is_unreachable_loopback_proxy",
        lambda proxy, timeout=0.2: proxy.endswith(":7897"),
    )

    assert discover_client_proxy() == "http://127.0.0.1:7890"


def test_missav_keeps_direct_mode_and_replaces_closed_local_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spider = MissAVSpider.__new__(MissAVSpider)
    spider.config = {"proxy": "http://127.0.0.1:7890"}
    spider.sig_log = Mock()
    monkeypatch.setattr(
        "app.spiders.missav.spider.discover_client_proxy",
        lambda: "http://127.0.0.1:7897",
    )

    assert spider._effective_proxy_server("直连") is None

    monkeypatch.setattr(
        "app.spiders.missav.spider.is_unreachable_loopback_proxy",
        lambda proxy, timeout=0.2: "7890" in str(proxy),
    )

    assert spider._effective_proxy_server() == "http://127.0.0.1:7897"
    spider.sig_log.emit.assert_called_once()
    assert "7897" in spider.sig_log.emit.call_args.args[0]


def test_missav_recognizes_local_proxy_when_system_settings_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spider = MissAVSpider.__new__(MissAVSpider)
    spider.config = {"proxy": "系统代理"}
    spider.sig_log = Mock()
    monkeypatch.setattr(BaseSpider, "_proxy_from_environment", classmethod(lambda cls: None))
    monkeypatch.setattr(BaseSpider, "_proxy_from_windows_settings", classmethod(lambda cls: None))
    monkeypatch.setattr(
        "app.spiders.missav.spider.discover_client_proxy",
        lambda: "http://127.0.0.1:7897",
    )

    assert spider._effective_proxy_server() == "http://127.0.0.1:7897"
    assert "已识别本机代理" in spider.sig_log.emit.call_args.args[0]
