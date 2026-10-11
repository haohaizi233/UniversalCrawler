"""识别 Clash / Clash Verge 等本机代理，并生成浏览器可执行的代理参数。"""

from __future__ import annotations

import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence
from pathlib import Path

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_CONFIG_NAMES = ("verge.yaml", "clash-verge.yaml", "config.yaml")
_PROFILE_DIRS = {"profiles", "config"}
_PORT_KEYS = (
    "verge_mixed_port",
    "mixed-port",
    "mixed_port",
    "verge_port",
    "port",
    "socks-port",
)
_PORT_RE = re.compile(
    r"(?im)^[ \t]*(?P<key>verge_mixed_port|mixed-port|mixed_port|verge_port|socks-port|port)"
    r"[ \t]*:[ \t]*['\"]?(?P<port>\d{1,5})\b"
)
_PAC_PROXY_RE = re.compile(
    r"(?i)\b(?P<kind>PROXY|SOCKS5|SOCKS)\s+"
    r"(?P<host>[A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\])\s*:\s*(?P<port>\d{1,5})\b"
)
_MAX_CONFIG_BYTES = 65536
_CONNECT_TIMEOUT_SECONDS = 0.2


def chromium_proxy_switch(proxy_server: str) -> str:
    """把代理 URL 转成 Edge/Chrome 会同时用于 HTTP 与 HTTPS 的启动参数。"""
    parsed = urllib.parse.urlparse(str(proxy_server or "").strip())
    host = parsed.hostname
    port = parsed.port
    if not host or not port or parsed.username:
        return f"--proxy-server={proxy_server}"
    scheme = (parsed.scheme or "http").lower()
    if scheme in {"socks5", "socks4"}:
        return f"--proxy-server={scheme}://{host}:{port}"
    # 只写 http://host:port 时，部分 Edge 会让 HTTPS 继续直连。
    return f"--proxy-server=http={host}:{port};https={host}:{port}"


def chromium_proxy_bypass_switch() -> str:
    """本地调试端口和回环地址不进入 Clash，避免代理参数把 loopback 也送进代理。"""
    return "--proxy-bypass-list=localhost;127.0.0.1;::1;<local>"


def is_unreachable_loopback_proxy(proxy_url: str, *, timeout: float = _CONNECT_TIMEOUT_SECONDS) -> bool:
    """仅当代理明确指向本机且端口拒绝连接时返回 True。"""
    parsed = urllib.parse.urlparse(str(proxy_url or "").strip())
    host = (parsed.hostname or "").strip("[]").lower()
    if host not in _LOOPBACK_HOSTS or not parsed.port:
        return False
    connect_host = "127.0.0.1" if host == "localhost" else host
    return not _tcp_open(connect_host, int(parsed.port), timeout)


def discover_client_proxy() -> str | None:
    """在系统代理和 Clash Verge 配置里找一个当前能连通的本机代理。"""
    from app.spiders.base import BaseSpider

    candidates: list[str] = []
    registry = BaseSpider._proxy_from_windows_settings()
    if registry:
        candidates.append(registry)
    pac = proxy_from_windows_pac()
    if pac:
        candidates.append(pac)
    candidates.extend(endpoints_from_client_configs())
    return first_reachable_proxy(candidates)


def first_reachable_proxy(candidates: Iterable[str]) -> str | None:
    seen: set[str] = set()
    for candidate in candidates:
        text = str(candidate or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        if not is_unreachable_loopback_proxy(text):
            return text
    return None


def endpoints_from_config_text(text: str) -> list[str]:
    """按 Clash Verge 混合端口优先的顺序提取本机代理。"""
    found: dict[str, int] = {}
    for match in _PORT_RE.finditer(str(text or "")[:_MAX_CONFIG_BYTES]):
        key = match.group("key").lower()
        port = int(match.group("port"))
        if 1 <= port <= 65535:
            found.setdefault(key, port)
    endpoints: list[str] = []
    for key in _PORT_KEYS:
        port = found.get(key)
        if port is None:
            continue
        if key == "socks-port":
            endpoints.append(f"socks5://127.0.0.1:{port}")
        else:
            endpoints.append(f"http://127.0.0.1:{port}")
    return endpoints


def endpoints_from_client_configs(files: Sequence[Path] | None = None) -> list[str]:
    endpoints: list[str] = []
    for path in files if files is not None else iter_proxy_client_config_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        endpoints.extend(endpoints_from_config_text(text))
    return endpoints


def proxy_from_pac_text(text: str) -> str | None:
    match = _PAC_PROXY_RE.search(str(text or "")[:_MAX_CONFIG_BYTES])
    if match is None:
        return None
    host = match.group("host").strip("[]")
    port = int(match.group("port"))
    if host.lower() not in _LOOPBACK_HOSTS or not 1 <= port <= 65535:
        return None
    if match.group("kind").lower().startswith("socks"):
        return f"socks5://{host}:{port}"
    return f"http://{host}:{port}"


def proxy_from_windows_pac() -> str | None:
    autoconfig = _read_windows_autoconfig_url()
    if not autoconfig:
        return None
    text = _read_loopback_pac(autoconfig)
    if text is None:
        return None
    return proxy_from_pac_text(text)


def default_proxy_client_roots() -> tuple[Path, ...]:
    home = Path.home()
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA", "").strip()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if appdata:
        base = Path(appdata)
        roots.extend(
            (
                base / "io.github.clash-verge-rev.clash-verge-rev",
                base / "clash-verge-rev",
                base / "clash-verge",
                base / "clash_win",
            )
        )
    if local_app_data:
        roots.append(Path(local_app_data) / "clash-verge")
    roots.extend(
        (
            home / ".config" / "clash-verge",
            home / ".config" / "clash",
            home / ".config" / "clash-meta",
            home / ".config" / "mihomo",
        )
    )
    return tuple(roots)


def iter_proxy_client_config_files(roots: Sequence[Path] | None = None) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots if roots is not None else default_proxy_client_roots():
        if not root.is_dir():
            continue
        candidates = [root / name for name in _CONFIG_NAMES]
        try:
            children = list(root.iterdir())
        except OSError:
            children = []
        for child in children:
            if child.is_dir() and child.name in _PROFILE_DIRS:
                candidates.extend(child / name for name in _CONFIG_NAMES)
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved in seen or not candidate.is_file():
                continue
            if candidate.stat().st_size <= 0:
                continue
            seen.add(resolved)
            found.append(candidate)
    return found


def _tcp_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_windows_autoconfig_url() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AutoConfigURL")
    except OSError:
        return ""
    return str(value or "").strip()


def _read_loopback_pac(url: str) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme == "file":
        try:
            return Path(urllib.request.url2pathname(parsed.path)).read_text(
                encoding="utf-8",
                errors="replace",
            )[:_MAX_CONFIG_BYTES]
        except OSError:
            return None
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or host not in _LOOPBACK_HOSTS:
        return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=1.0) as response:
            return response.read(_MAX_CONFIG_BYTES).decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError, ValueError):
        return None
