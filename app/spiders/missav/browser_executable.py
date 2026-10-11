"""定位 MissAV 挑战页使用的真实浏览器，而不是应用执行别名。"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_EXPLICIT_ENV = "UCP_BROWSER_EXECUTABLE"
_CHROMIUM_RELATIVE_PATHS = (
    Path("chrome-win/chrome.exe"),
    Path("chrome-win64/chrome.exe"),
    Path("chrome-linux/chrome"),
    Path("chrome-mac/Chromium.app/Contents/MacOS/Chromium"),
    Path("chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium"),
)


def resolve_challenge_browser(configured: str | None = None) -> Path:
    """按手动路径、Playwright 目录、系统 Chrome、系统 Edge 的顺序选择浏览器。"""
    from app.spiders.missav.challenge_session import SystemBrowserUnavailable

    explicit = _existing_browser(configured) or _existing_browser(os.environ.get(_EXPLICIT_ENV))
    if explicit is not None:
        return explicit
    override = _user_playwright_chromium()
    if override is not None:
        return override
    for candidate in _system_browser_candidates():
        if _is_launchable_browser(candidate):
            return candidate
    bundled = _bundled_playwright_chromium()
    if bundled is not None:
        return bundled
    raise SystemBrowserUnavailable("未找到系统 Chrome 或 Edge")


def missing_browser_request(configured: str | None = None) -> str:
    """返回用户指定了但不存在的浏览器路径，供启动日志提示。"""
    text = str(configured or "").strip().strip("\"'")
    if not text:
        text = str(os.environ.get(_EXPLICIT_ENV) or "").strip().strip("\"'")
    if text and not Path(text).is_file():
        return text
    return ""


def browser_launch_label(path: Path) -> str:
    text = str(path).replace("/", "\\").lower()
    if "ms-playwright" in text or "playwright" in text:
        kind = "Playwright Chromium"
    elif "msedge" in path.name.lower():
        kind = "Edge"
    elif "chrome" in path.name.lower() or "chromium" in path.name.lower():
        kind = "Chrome"
    else:
        kind = path.name
    return f"{kind} ({path.name})"


def _existing_browser(value: str | None) -> Path | None:
    text = str(value or "").strip().strip("\"'")
    if not text:
        return None
    path = Path(text)
    if path.is_file() and not _is_rejected_alias(path):
        return path
    return None


def _system_browser_candidates() -> list[Path]:
    candidates: list[Path] = []
    for executable_name in ("chrome.exe", "msedge.exe", "google-chrome", "chromium", "chromium-browser"):
        located = shutil.which(executable_name)
        if located:
            candidates.append(Path(located))
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    program_files = Path(os.environ.get("PROGRAMFILES", ""))
    program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", ""))
    candidates.extend(
        (
            program_files / "Google/Chrome/Application/chrome.exe",
            program_files_x86 / "Google/Chrome/Application/chrome.exe",
            local_app_data / "Google/Chrome/Application/chrome.exe",
            program_files / "Microsoft/Edge/Application/msedge.exe",
            program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
            local_app_data / "Microsoft/Edge/Application/msedge.exe",
        )
    )
    return candidates


def _user_playwright_chromium() -> Path | None:
    raw = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip().strip("\"'")
    if not raw or raw == "0":
        return None
    root = Path(raw)
    bundle = _frozen_bundle_browser_root()
    try:
        if bundle is not None and root.resolve() == bundle.resolve():
            return None
    except OSError:
        return None
    return _chromium_under(root)


def _bundled_playwright_chromium() -> Path | None:
    roots: list[Path] = []
    bundle = _frozen_bundle_browser_root()
    if bundle is not None:
        roots.append(bundle)
    env_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip().strip("\"'")
    if env_root and env_root != "0":
        roots.append(Path(env_root))
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        roots.append(Path(local_app_data) / "ms-playwright")
    roots.append(Path.home() / "AppData" / "Local" / "ms-playwright")
    seen: set[Path] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        chromium = _chromium_under(root)
        if chromium is not None:
            return chromium
    return None


def _frozen_bundle_browser_root() -> Path | None:
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    root = Path(meipass) / "ms-playwright"
    return root if root.is_dir() else None


def _chromium_under(root: Path) -> Path | None:
    if not root.is_dir():
        return None
    revisions: list[tuple[int, Path]] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return None
    for child in children:
        name = child.name.lower()
        if not child.is_dir() or not name.startswith("chromium-") or "headless" in name:
            continue
        suffix = name.rsplit("-", 1)[-1]
        revision = int(suffix) if suffix.isdigit() else -1
        for relative in _CHROMIUM_RELATIVE_PATHS:
            candidate = child / relative
            if _is_launchable_browser(candidate):
                revisions.append((revision, candidate))
    if not revisions:
        return None
    revisions.sort(key=lambda item: item[0])
    return revisions[-1][1]


def _is_launchable_browser(path: Path) -> bool:
    try:
        return path.is_file() and not _is_rejected_alias(path)
    except OSError:
        return False


def _is_rejected_alias(path: Path) -> bool:
    lowered = str(path).replace("/", "\\").lower()
    if "\\windowsapps\\" in lowered:
        return True
    try:
        return path.stat().st_size <= 0
    except OSError:
        return True
