"""Tests for the shared Windows media-tool packaging contract."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from tests.support.paths import PROJECT_ROOT


MEDIA_TOOLS_FILE = PROJECT_ROOT / "packaging" / "media_tools.py"
MEDIA_TOOL_NAMES = ("ffmpeg.exe", "ffprobe.exe", "N_m3u8DL-RE.exe")


def _load_media_tools() -> ModuleType:
    if not MEDIA_TOOLS_FILE.is_file():
        pytest.fail(f"missing shared media-tool helper: {MEDIA_TOOLS_FILE}")
    spec = importlib.util.spec_from_file_location("ucrawl_media_tools_tests", MEDIA_TOOLS_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_packaging_script(script_name: str) -> ModuleType:
    script_path = PROJECT_ROOT / "packaging" / script_name
    module_name = f"ucrawl_{script_path.stem}_gate_tests"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(script_path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def _write_valid_tool(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + b"x" * (1024 * 1024))


def _write_all_valid_tools(root: Path, *, internal: bool = False) -> None:
    base = root / "_internal" if internal else root
    for name in MEDIA_TOOL_NAMES:
        _write_valid_tool(base / name)


def test_resolver_accepts_only_runtime_visible_root_and_internal_locations(tmp_path):
    tool = _load_media_tools()
    _write_all_valid_tools(tmp_path, internal=True)

    resolved = tool.resolve_media_tool_paths(tmp_path)

    assert resolved == {
        name: (tmp_path / "_internal" / name).resolve()
        for name in MEDIA_TOOL_NAMES
    }


def test_resolver_rejects_a_tool_hidden_below_internal(tmp_path):
    tool = _load_media_tools()
    _write_all_valid_tools(tmp_path, internal=True)
    hidden = tmp_path / "_internal" / "nested" / "N_m3u8DL-RE.exe"
    hidden.parent.mkdir(parents=True)
    _write_valid_tool(hidden)
    (tmp_path / "_internal" / "N_m3u8DL-RE.exe").unlink()

    with pytest.raises(tool.MediaToolValidationError, match=r"N_m3u8DL-RE\.exe"):
        tool.validate_media_tools(tmp_path, probe=False)


def test_validator_rejects_missing_tool_before_startup_probe(tmp_path):
    tool = _load_media_tools()
    _write_valid_tool(tmp_path / "ffmpeg.exe")
    _write_valid_tool(tmp_path / "ffprobe.exe")

    with patch.object(tool.subprocess, "run") as run, pytest.raises(
        tool.MediaToolValidationError,
        match=r"missing media tools.*N_m3u8DL-RE\.exe",
    ):
        tool.validate_media_tools(tmp_path)

    run.assert_not_called()


def test_validator_rejects_a_too_small_tool_before_startup_probe(tmp_path):
    tool = _load_media_tools()
    _write_all_valid_tools(tmp_path)
    (tmp_path / "ffmpeg.exe").write_bytes(b"MZ")

    with patch.object(tool.subprocess, "run") as run, pytest.raises(
        tool.MediaToolValidationError,
        match=r"too small.*ffmpeg\.exe",
    ):
        tool.validate_media_tools(tmp_path)

    run.assert_not_called()


def test_validator_rejects_a_non_pe_tool_before_startup_probe(tmp_path):
    tool = _load_media_tools()
    _write_all_valid_tools(tmp_path)
    bad = tmp_path / "ffprobe.exe"
    bad.write_bytes(b"not a PE" + b"x" * (1024 * 1024))

    with patch.object(tool.subprocess, "run") as run, pytest.raises(
        tool.MediaToolValidationError,
        match=r"ffprobe\.exe.*MZ|MZ.*ffprobe\.exe",
    ):
        tool.validate_media_tools(tmp_path)

    run.assert_not_called()


def test_validator_rejects_nonzero_version_probe(tmp_path):
    tool = _load_media_tools()
    _write_all_valid_tools(tmp_path)
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=7,
        stdout="version output",
        stderr="missing dependency",
    )

    with patch.object(tool.subprocess, "run", return_value=completed) as run, pytest.raises(
        tool.MediaToolValidationError,
        match=r"ffmpeg\.exe.*7|7.*ffmpeg\.exe",
    ):
        tool.validate_media_tools(tmp_path)

    argv = run.call_args.args[0]
    assert Path(argv[0]).is_absolute()
    assert argv[1] == "-version"


def test_validator_rejects_a_timed_out_version_probe(tmp_path):
    tool = _load_media_tools()
    _write_all_valid_tools(tmp_path)

    with patch.object(
        tool.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd=["tool"], timeout=1),
    ) as run, pytest.raises(tool.MediaToolValidationError, match="timed out|超时"):
        tool.validate_media_tools(tmp_path, timeout_seconds=1)

    assert run.call_args.kwargs["timeout"] == 1


def test_validator_rejects_a_tool_that_cannot_start(tmp_path):
    tool = _load_media_tools()
    _write_all_valid_tools(tmp_path)

    with patch.object(
        tool.subprocess,
        "run",
        side_effect=OSError("missing runtime dependency"),
    ) as run, pytest.raises(tool.MediaToolValidationError, match="could not start"):
        tool.validate_media_tools(tmp_path)

    assert run.call_count == 1


def test_successful_validation_uses_a_temp_cwd_and_minimal_system_environment(tmp_path):
    tool = _load_media_tools()
    _write_all_valid_tools(tmp_path, internal=True)
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="tool version",
        stderr="",
    )

    with patch.object(tool.subprocess, "run", return_value=completed) as run:
        resolved = tool.validate_media_tools(tmp_path)

    assert set(resolved) == set(MEDIA_TOOL_NAMES)
    assert run.call_count == len(MEDIA_TOOL_NAMES)
    for call in run.call_args_list:
        argv = call.args[0]
        assert Path(argv[0]).is_absolute()
        assert argv[1] in {"-version", "--version"}
        assert Path(call.kwargs["cwd"]).resolve() != tmp_path.resolve()
        assert str(tmp_path.resolve()) not in call.kwargs["env"]["PATH"]
        assert call.kwargs["shell"] is False


def test_portable_prerequisite_gate_blocks_invalid_media_tools(tmp_path):
    tool = _load_packaging_script("build_portable.py")
    required_file = tmp_path / "required.txt"
    required_file.write_text("ok", encoding="utf-8")
    media_error = tool.MediaToolValidationError("missing media tools")

    with (
        patch.object(tool, "PROJECT_ROOT", tmp_path),
        patch.object(tool, "REQUIRED_FILES", [required_file]),
        patch.object(tool, "validate_media_tools", side_effect=media_error) as validate,
        pytest.raises(SystemExit, match="missing media tools"),
    ):
        tool.ensure_prerequisites()

    validate.assert_called_once_with(tmp_path)


def test_portable_output_gate_blocks_invalid_media_tools(tmp_path):
    tool = _load_packaging_script("build_portable.py")
    for name in (
        "APP_EXE_NAME",
        "LAUNCHER_EXE_NAME",
        "CLI_LAUNCHER_EXE_NAME",
        "WEBUI_EXE_NAME",
        "UPDATER_HELPER_EXE_NAME",
    ):
        (tmp_path / getattr(tool, name)).write_bytes(b"MZ")
    (tmp_path / "identity.json").write_text("{}", encoding="utf-8")
    (tmp_path / "chrome.exe").write_bytes(b"MZ")
    internal = tmp_path / "_internal"
    for package, files in {
        "entry": ("__init__.py", "dispatcher.py"),
        "cli": ("__init__.py",),
        "shared": ("__init__.py",),
    }.items():
        package_dir = internal / package
        package_dir.mkdir(parents=True)
        for file_name in files:
            (package_dir / file_name).write_text("", encoding="utf-8")
    media_error = tool.MediaToolValidationError("version probe failed")

    with (
        patch.object(tool, "DIST_DIR", tmp_path),
        patch.object(tool, "RELEASE_IDENTITY_FILENAME", "identity.json"),
        patch.object(tool, "PORTABLE_ROOT_DOCS", ()),
        patch.object(tool, "PYTHON_SQLITE_RUNTIME_FILES", ()),
        patch.object(tool, "FORBIDDEN_BASENAMES", set()),
        patch.object(tool, "validate_media_tools", side_effect=media_error) as validate,
        pytest.raises(SystemExit, match="version probe failed"),
    ):
        tool.verify_output()

    validate.assert_called_once_with(tmp_path)


def test_installer_prerequisite_gate_blocks_invalid_media_tools(tmp_path):
    tool = _load_packaging_script("build_installer.py")
    media_error = tool.MediaToolValidationError("media tool could not start")
    with (
        patch.object(tool, "DIST_DIR", tmp_path),
        patch.object(tool, "REQUIRED_INSTALL_SOURCE_ENTRIES", []),
        patch.object(tool, "validate_media_tools", side_effect=media_error) as validate,
        pytest.raises(SystemExit, match="media tool could not start"),
    ):
        tool.ensure_prerequisites()

    validate.assert_called_once_with(tmp_path)
