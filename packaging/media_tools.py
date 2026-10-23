"""Shared validation for the Windows media tools shipped with the product."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

MEDIA_TOOL_NAMES = ("ffmpeg.exe", "ffprobe.exe", "N_m3u8DL-RE.exe")
MEDIA_TOOL_MIN_BYTES = 1024 * 1024
MEDIA_TOOL_PROBE_TIMEOUT_SECONDS = 15.0

_MEDIA_TOOL_VERSION_ARGUMENTS = {
    "ffmpeg.exe": "-version",
    "ffprobe.exe": "-version",
    "N_m3u8DL-RE.exe": "--version",
}


class MediaToolValidationError(RuntimeError):
    """Raised when a bundled media tool is missing, malformed, or cannot start."""


def _candidate_paths(root: Path, name: str) -> tuple[Path, Path]:
    """Return exactly the two locations visible to the frozen runtime."""

    return root / name, root / "_internal" / name


def resolve_media_tool_paths(root: Path) -> dict[str, Path]:
    """Resolve media tools from the install root or its PyInstaller internals.

    ``runtime_paths.resolve_tool_file`` searches the frozen executable directory
    before ``sys._MEIPASS``.  In an onedir build those correspond to ``root``
    and ``root/_internal`` here.  Deliberately avoid recursive globbing: a tool
    in an arbitrary nested directory is not discoverable by the application.
    """

    resolved_root = Path(root).expanduser().resolve()
    resolved: dict[str, Path] = {}
    for name in MEDIA_TOOL_NAMES:
        for candidate in _candidate_paths(resolved_root, name):
            if candidate.is_file():
                resolved[name] = candidate.resolve()
                break
    return resolved


def _probe_environment() -> dict[str, str]:
    """Return a small environment that cannot borrow the developer checkout.

    The probe uses absolute executable paths and a temporary cwd, so it only
    needs the Windows system search path for OS DLLs.  In particular, it does
    not inherit ``PYTHONPATH``, virtualenv paths, or ``UCRAWL_TOOL_ROOT`` from a
    developer shell.
    """

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
        # The helper is used for Windows artifacts, but keeping a useful PATH
        # makes its unit tests and diagnostics deterministic on other hosts.
        environment["PATH"] = os.environ.get("PATH", "")
    return environment


def _validate_tool_file(name: str, path: Path) -> None:
    if not path.is_file():
        raise MediaToolValidationError(f"media tool is not a file: {name}: {path}")
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            header = handle.read(2)
    except OSError as exc:
        raise MediaToolValidationError(f"cannot read media tool {name}: {path}: {exc}") from exc
    if size < MEDIA_TOOL_MIN_BYTES:
        raise MediaToolValidationError(
            f"media tool is too small: {name}: {path} ({size} bytes)"
        )
    if header != b"MZ":
        raise MediaToolValidationError(f"media tool is not a PE/MZ file: {name}: {path}")


def _probe_tool(name: str, path: Path, timeout_seconds: float) -> None:
    version_argument = _MEDIA_TOOL_VERSION_ARGUMENTS[name]
    command = [str(path.resolve()), version_argument]
    try:
        with tempfile.TemporaryDirectory(prefix="ucrawl-media-tool-probe-") as temp_dir:
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
        raise MediaToolValidationError(
            f"media tool version probe timed out: {name} ({timeout_seconds:g}s)"
        ) from exc
    except OSError as exc:
        raise MediaToolValidationError(
            f"media tool could not start: {name}: {path}: {exc}"
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
        raise MediaToolValidationError(
            f"media tool version probe failed: {name} (exit {completed.returncode}){suffix}"
        )


def validate_media_tools(
    root: Path,
    *,
    probe: bool = True,
    timeout_seconds: float = MEDIA_TOOL_PROBE_TIMEOUT_SECONDS,
) -> dict[str, Path]:
    """Validate and optionally start-probe all shipped Windows media tools."""

    if timeout_seconds <= 0:
        raise ValueError("media tool probe timeout must be positive")

    resolved_root = Path(root).expanduser().resolve()
    resolved = resolve_media_tool_paths(resolved_root)
    missing = [name for name in MEDIA_TOOL_NAMES if name not in resolved]
    if missing:
        expected = ", ".join(
            f"{resolved_root / name} or {resolved_root / '_internal' / name}"
            for name in missing
        )
        raise MediaToolValidationError(f"missing media tools: {expected}")

    for name in MEDIA_TOOL_NAMES:
        _validate_tool_file(name, resolved[name])
    if probe:
        for name in MEDIA_TOOL_NAMES:
            _probe_tool(name, resolved[name], timeout_seconds)
    return resolved


__all__ = [
    "MEDIA_TOOL_MIN_BYTES",
    "MEDIA_TOOL_NAMES",
    "MEDIA_TOOL_PROBE_TIMEOUT_SECONDS",
    "MediaToolValidationError",
    "resolve_media_tool_paths",
    "validate_media_tools",
]
