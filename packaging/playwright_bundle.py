"""Resolve the exact Playwright Chromium runtime owned by this build."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


_BROWSER_ORDER = (
    "chromium",
    "chromium-headless-shell",
    "ffmpeg",
    "winldd",
)
_REQUIRED_BROWSERS = frozenset(("chromium", "ffmpeg"))
_CACHE_PREFIX = {
    "chromium": "chromium",
    "chromium-headless-shell": "chromium_headless_shell",
    "ffmpeg": "ffmpeg",
    "winldd": "winldd",
}
PLAYWRIGHT_DRIVER_REQUIRED_FILES = (
    Path("node.exe"),
    Path("package") / "cli.js",
    Path("package") / "browsers.json",
)
PLAYWRIGHT_DRIVER_MIN_NODE_BYTES = 1024 * 1024
PLAYWRIGHT_DRIVER_PROBE_TIMEOUT_SECONDS = 15.0


class PlaywrightDriverValidationError(RuntimeError):
    """Raised when the Node driver required by Playwright is incomplete."""


def installed_browser_manifest_path() -> Path:
    import playwright

    return Path(playwright.__file__).resolve().parent / "driver" / "package" / "browsers.json"


def installed_playwright_driver_directory() -> Path:
    """Return the driver directory owned by the installed ``playwright`` package."""

    import playwright

    package_file = getattr(playwright, "__file__", None)
    if not package_file:
        raise PlaywrightDriverValidationError(
            "cannot locate the installed Playwright package file"
        )
    return Path(package_file).resolve().parent / "driver"


def _probe_environment() -> dict[str, str]:
    """Return only the OS runtime environment needed by the bundled Node binary."""

    environment: dict[str, str] = {}
    for name in ("SystemRoot", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT"):
        value = os.environ.get(name)
        if value:
            environment[name] = value

    if os.name == "nt":
        system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
        if system_root:
            root = Path(system_root)
            system_path = (
                root / "System32",
                root,
                root / "System32" / "Wbem",
                root / "System32" / "WindowsPowerShell" / "v1.0",
            )
            environment["PATH"] = os.pathsep.join(str(path) for path in system_path)
        else:
            environment["PATH"] = ""
    else:
        # Release artifacts target Windows.  A deterministic system default keeps
        # local virtualenv or checkout paths from masking a broken driver in tests.
        environment["PATH"] = os.defpath
    return environment


def resolve_playwright_driver_paths(root: Path) -> dict[str, Path]:
    """Resolve the exact driver files visible to the frozen Playwright runtime."""

    resolved_root = Path(root).expanduser().resolve()
    return {
        relative_path.as_posix(): (resolved_root / relative_path).resolve()
        for relative_path in PLAYWRIGHT_DRIVER_REQUIRED_FILES
        if (resolved_root / relative_path).is_file()
    }


def _validate_driver_file(label: str, path: Path) -> None:
    if not path.is_file():
        raise PlaywrightDriverValidationError(
            f"Playwright driver file is missing: {label}: {path}"
        )
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            header = handle.read(2)
    except OSError as exc:
        raise PlaywrightDriverValidationError(
            f"cannot read Playwright driver file {label}: {path}: {exc}"
        ) from exc

    if size <= 0:
        raise PlaywrightDriverValidationError(
            f"Playwright driver file is empty: {label}: {path}"
        )
    if label == "node.exe":
        if size < PLAYWRIGHT_DRIVER_MIN_NODE_BYTES:
            raise PlaywrightDriverValidationError(
                f"Playwright driver node.exe is too small: {path} ({size} bytes)"
            )
        if header != b"MZ":
            raise PlaywrightDriverValidationError(
                f"Playwright driver node.exe is not a PE/MZ file: {path}"
            )
    elif label == "package/browsers.json":
        try:
            payload: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PlaywrightDriverValidationError(
                f"Playwright driver browsers.json is invalid: {path}"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("browsers"), list):
            raise PlaywrightDriverValidationError(
                f"Playwright driver browsers.json has an invalid schema: {path}"
            )


def _probe_playwright_driver(
    paths: dict[str, Path],
    timeout_seconds: float,
) -> None:
    node_path = paths["node.exe"]
    cli_path = paths["package/cli.js"]
    command = [str(node_path.resolve()), str(cli_path.resolve()), "--version"]
    try:
        with tempfile.TemporaryDirectory(prefix="ucrawl-playwright-driver-probe-") as temp_dir:
            completed = subprocess.run(
                command,
                cwd=temp_dir,
                env=_probe_environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                shell=False,
                timeout=timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except subprocess.TimeoutExpired as exc:
        raise PlaywrightDriverValidationError(
            "Playwright driver version probe timed out: "
            f"{node_path} ({timeout_seconds:g}s)"
        ) from exc
    except OSError as exc:
        raise PlaywrightDriverValidationError(
            f"Playwright driver could not start: {node_path}: {exc}"
        ) from exc

    if completed.returncode != 0:
        output_parts: list[str] = []
        for value in (completed.stdout, completed.stderr):
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            if value:
                normalized = str(value).strip()
                if normalized:
                    output_parts.append(normalized)
        output = " ".join(output_parts)
        if len(output) > 500:
            output = output[:500] + "..."
        suffix = f": {output}" if output else ""
        raise PlaywrightDriverValidationError(
            "Playwright driver version probe failed: "
            f"{node_path} (exit {completed.returncode}){suffix}"
        )


def validate_playwright_driver(
    root: Path,
    *,
    probe: bool = True,
    timeout_seconds: float = PLAYWRIGHT_DRIVER_PROBE_TIMEOUT_SECONDS,
) -> dict[str, Path]:
    """Validate the exact driver files and optionally start the bundled Node binary."""

    if timeout_seconds <= 0:
        raise ValueError("Playwright driver probe timeout must be positive")

    resolved_root = Path(root).expanduser().resolve()
    resolved = resolve_playwright_driver_paths(resolved_root)
    missing = [
        relative_path.as_posix()
        for relative_path in PLAYWRIGHT_DRIVER_REQUIRED_FILES
        if relative_path.as_posix() not in resolved
    ]
    if missing:
        expected = ", ".join(str(resolved_root / relative_path) for relative_path in PLAYWRIGHT_DRIVER_REQUIRED_FILES)
        raise PlaywrightDriverValidationError(
            "missing Playwright driver files: "
            + ", ".join(missing)
            + f" (expected under {resolved_root}; checked {expected})"
        )

    for relative_path in PLAYWRIGHT_DRIVER_REQUIRED_FILES:
        _validate_driver_file(relative_path.as_posix(), resolved[relative_path.as_posix()])
    if probe:
        _probe_playwright_driver(resolved, timeout_seconds)
    return resolved


def resolve_playwright_browser_directories(
    browser_root: Path,
    *,
    browser_manifest_path: Path | None = None,
) -> tuple[Path, ...]:
    """Return only browser revisions required by the installed Playwright package.

    The shared Playwright cache accumulates old Chromium, Firefox and WebKit
    revisions over time. Packaging that whole directory makes release size
    depend on machine history and can exceed the updater's safety limit.
    """

    root = Path(browser_root)
    manifest_path = Path(browser_manifest_path or installed_browser_manifest_path())
    try:
        payload: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read Playwright browser manifest: {manifest_path}") from exc
    entries = payload.get("browsers") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError(f"invalid Playwright browser manifest: {manifest_path}")

    revisions: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        revision = str(entry.get("revision") or "").strip()
        if name in _CACHE_PREFIX and revision:
            revisions[name] = revision

    missing_metadata = sorted(_REQUIRED_BROWSERS - revisions.keys())
    if missing_metadata:
        raise RuntimeError(
            "Playwright browser manifest is missing required entries: " + ", ".join(missing_metadata)
        )

    selected: list[Path] = []
    missing_directories: list[Path] = []
    for name in _BROWSER_ORDER:
        revision = revisions.get(name)
        if not revision:
            continue
        path = root / f"{_CACHE_PREFIX[name]}-{revision}"
        if path.is_dir():
            selected.append(path)
        else:
            missing_directories.append(path)
    if missing_directories:
        missing = "\n- ".join(str(path) for path in missing_directories)
        raise FileNotFoundError(
            "Playwright Chromium runtime is incomplete. Run `python -m playwright install chromium`.\n"
            f"- {missing}"
        )
    return tuple(selected)


__all__ = [
    "PLAYWRIGHT_DRIVER_MIN_NODE_BYTES",
    "PLAYWRIGHT_DRIVER_PROBE_TIMEOUT_SECONDS",
    "PLAYWRIGHT_DRIVER_REQUIRED_FILES",
    "PlaywrightDriverValidationError",
    "installed_browser_manifest_path",
    "installed_playwright_driver_directory",
    "resolve_playwright_browser_directories",
    "resolve_playwright_driver_paths",
    "validate_playwright_driver",
]
