"""Behavioral tests for the Playwright driver packaging contract."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from tests.support.paths import PROJECT_ROOT


PLAYWRIGHT_BUNDLE_FILE = PROJECT_ROOT / "packaging" / "playwright_bundle.py"
PLAYWRIGHT_DRIVER_FILES = (
    Path("node.exe"),
    Path("package") / "cli.js",
    Path("package") / "browsers.json",
)


def _load_module(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def _load_bundle() -> ModuleType:
    return _load_module(PLAYWRIGHT_BUNDLE_FILE, "ucrawl_playwright_driver_tests")


def _load_packaging_script(script_name: str) -> ModuleType:
    path = PROJECT_ROOT / "packaging" / script_name
    return _load_module(path, f"ucrawl_{path.stem}_playwright_driver_tests")


def _exec_portable_spec() -> dict[str, object]:
    spec_path = PROJECT_ROOT / "packaging" / "portable.spec"
    import playwright

    fake_hooks = ModuleType("PyInstaller.utils.hooks")
    fake_hooks.collect_submodules = lambda _name: []
    fake_hooks.copy_metadata = lambda _name: []
    fake_utils = ModuleType("PyInstaller.utils")
    fake_utils.hooks = fake_hooks
    fake_pyinstaller = ModuleType("PyInstaller")
    fake_pyinstaller.utils = fake_utils
    fake_bundle = ModuleType("playwright_bundle")
    fake_bundle.installed_playwright_driver_directory = lambda: (
        Path(playwright.__file__).resolve().parent / "driver"
    )
    fake_bundle.resolve_playwright_browser_directories = lambda _root: ()
    fake_bundle.validate_playwright_driver = lambda _root, probe=False: None
    namespace: dict[str, object] = {
        "__file__": str(spec_path),
        "__name__": "__test_portable_spec__",
        "SPEC": str(spec_path),
        "Analysis": MagicMock(),
        "PYZ": MagicMock(),
        "EXE": MagicMock(),
        "COLLECT": MagicMock(),
    }
    source = spec_path.read_text(encoding="utf-8")
    sys.path.insert(0, str(spec_path.parent))
    try:
        with patch.dict(
            sys.modules,
            {
                "PyInstaller": fake_pyinstaller,
                "PyInstaller.utils": fake_utils,
                "PyInstaller.utils.hooks": fake_hooks,
                "playwright_bundle": fake_bundle,
            },
        ):
            exec(source, namespace)
    finally:
        sys.path.pop(0)
    return namespace


def _write_driver(root: Path) -> None:
    (root / "package").mkdir(parents=True, exist_ok=True)
    (root / "node.exe").write_bytes(b"MZ" + b"x" * (1024 * 1024 - 2))
    (root / "package" / "cli.js").write_text("console.log('ok');\n", encoding="utf-8")
    (root / "package" / "browsers.json").write_text(
        json.dumps({"browsers": []}),
        encoding="utf-8",
    )


def test_driver_validation_rejects_missing_required_file_before_probe(tmp_path):
    tool = _load_bundle()
    driver = tmp_path / "playwright" / "driver"
    _write_driver(driver)
    (driver / "package" / "browsers.json").unlink()

    with patch.object(tool.subprocess, "run") as run, pytest.raises(
        tool.PlaywrightDriverValidationError,
        match="browsers.json",
    ):
        tool.validate_playwright_driver(driver)

    run.assert_not_called()


def test_driver_validation_rejects_driver_hidden_under_patchright(tmp_path):
    tool = _load_bundle()
    _write_driver(tmp_path / "patchright" / "driver")
    expected = tmp_path / "playwright" / "driver"

    with patch.object(tool.subprocess, "run") as run, pytest.raises(
        tool.PlaywrightDriverValidationError,
        match=r"node\.exe|playwright",
    ):
        tool.validate_playwright_driver(expected)

    run.assert_not_called()


def test_driver_validation_rejects_nonzero_version_probe(tmp_path):
    tool = _load_bundle()
    driver = tmp_path / "playwright" / "driver"
    _write_driver(driver)
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=7,
        stdout="",
        stderr="driver failed",
    )

    with patch.object(tool.subprocess, "run", return_value=completed) as run, pytest.raises(
        tool.PlaywrightDriverValidationError,
        match=r"exit 7",
    ):
        tool.validate_playwright_driver(driver)

    argv = run.call_args.args[0]
    assert Path(argv[0]).is_absolute()
    assert Path(argv[1]).is_absolute()
    assert argv[2] == "--version"
    assert Path(run.call_args.kwargs["cwd"]).resolve() != driver.resolve()
    assert str(tmp_path.resolve()) not in run.call_args.kwargs["env"]["PATH"]
    assert run.call_args.kwargs["timeout"] == 15
    assert run.call_args.kwargs["shell"] is False


def test_driver_validation_rejects_timed_out_version_probe(tmp_path):
    tool = _load_bundle()
    driver = tmp_path / "playwright" / "driver"
    _write_driver(driver)

    with patch.object(
        tool.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(cmd=["node"], timeout=15),
    ) as run, pytest.raises(tool.PlaywrightDriverValidationError, match="timed out|超时"):
        tool.validate_playwright_driver(driver)

    assert run.call_args.kwargs["timeout"] == 15


def test_driver_validation_rejects_driver_that_cannot_start(tmp_path):
    tool = _load_bundle()
    driver = tmp_path / "playwright" / "driver"
    _write_driver(driver)

    with patch.object(
        tool.subprocess,
        "run",
        side_effect=OSError("missing runtime dependency"),
    ) as run, pytest.raises(tool.PlaywrightDriverValidationError, match="could not start"):
        tool.validate_playwright_driver(driver)

    assert run.call_count == 1


def test_driver_validation_accepts_complete_driver(tmp_path):
    tool = _load_bundle()
    driver = tmp_path / "playwright" / "driver"
    _write_driver(driver)
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="1.60.0", stderr="")

    with patch.object(tool.subprocess, "run", return_value=completed) as run:
        resolved = tool.validate_playwright_driver(driver)

    assert resolved == {
        relative_path.as_posix(): (driver / relative_path).resolve()
        for relative_path in PLAYWRIGHT_DRIVER_FILES
    }
    assert run.call_count == 1


def test_portable_spec_copies_the_installed_driver_directory():
    bundle = _load_bundle()
    namespace = _exec_portable_spec()
    driver = bundle.installed_playwright_driver_directory().resolve()

    assert (driver, "playwright/driver") in {
        (Path(source).resolve(), target)
        for source, target in namespace["datas"]  # type: ignore[index]
    }


def test_portable_prerequisite_gate_blocks_invalid_playwright_driver(tmp_path):
    tool = _load_packaging_script("build_portable.py")
    required_file = tmp_path / "required.txt"
    required_file.write_text("ok", encoding="utf-8")
    driver_error = tool.PlaywrightDriverValidationError("missing Playwright driver")

    with (
        patch.object(tool, "PROJECT_ROOT", tmp_path),
        patch.object(tool, "REQUIRED_FILES", [required_file]),
        patch.object(tool, "validate_media_tools"),
        patch.object(tool, "resolve_playwright_browser_directories", return_value=()),
        patch.object(tool, "installed_playwright_driver_directory", return_value=tmp_path / "driver"),
        patch.object(tool, "validate_playwright_driver", side_effect=driver_error) as validate,
        pytest.raises(SystemExit, match="missing Playwright driver"),
    ):
        tool.ensure_prerequisites()

    validate.assert_called_once_with(tmp_path / "driver")


def test_portable_output_gate_blocks_invalid_playwright_driver(tmp_path):
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
    driver_error = tool.PlaywrightDriverValidationError("artifact driver failed")

    with (
        patch.object(tool, "DIST_DIR", tmp_path),
        patch.object(tool, "RELEASE_IDENTITY_FILENAME", "identity.json"),
        patch.object(tool, "PORTABLE_ROOT_DOCS", ()),
        patch.object(tool, "PYTHON_SQLITE_RUNTIME_FILES", ()),
        patch.object(tool, "FORBIDDEN_BASENAMES", set()),
        patch.object(tool, "validate_media_tools"),
        patch.object(tool, "validate_playwright_driver", side_effect=driver_error) as validate,
        pytest.raises(SystemExit, match="artifact driver failed"),
    ):
        tool.verify_output()

    validate.assert_called_once_with(tmp_path / "_internal" / "playwright" / "driver")


def test_installer_prerequisite_gate_blocks_invalid_playwright_driver(tmp_path):
    tool = _load_packaging_script("build_installer.py")
    (tmp_path / "installer.iss").write_text("", encoding="utf-8")
    (tmp_path / "wizard.bmp").write_bytes(b"BM")
    driver_error = tool.PlaywrightDriverValidationError("installer driver failed")

    with (
        patch.object(tool, "DIST_DIR", tmp_path),
        patch.object(tool, "REQUIRED_INSTALL_SOURCE_ENTRIES", []),
        patch.object(tool, "validate_media_tools"),
        patch.object(tool, "validate_playwright_driver", side_effect=driver_error) as validate,
        patch.object(tool, "ISS_FILE", tmp_path / "installer.iss"),
        patch.object(tool, "WIZARD_IMAGE", tmp_path / "wizard.bmp"),
        patch.object(tool, "WIZARD_SMALL_IMAGE", tmp_path / "wizard.bmp"),
        pytest.raises(SystemExit, match="installer driver failed"),
    ):
        tool.ensure_prerequisites()

    validate.assert_called_once_with(tmp_path / "_internal" / "playwright" / "driver")
