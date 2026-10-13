"""覆盖率策略必须在本地与 CI 使用同一份可审计配置。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.toml_compat import loads


pytestmark = pytest.mark.architecture

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COVERAGE_SOURCE = ("app", "cli", "entry", "shared")
# 只省略本地纯算法/签名实现。网络、Cookie、会话文件必须留在门槛内，名单不能再扩大。
PURE_LOCAL_ALGORITHM_OMIT = (
    "app/core/lib/douyin/encrypt/aBogus.py",
    "app/core/lib/douyin/encrypt/verifyFp.py",
    "app/core/lib/douyin/encrypt/xBogus.py",
    "app/core/lib/douyin/encrypt/xGnarly.py",
)
MEASURED_NETWORK_COOKIE_OR_SESSION = (
    "app/core/lib/douyin/encrypt/__init__.py",
    "app/core/lib/douyin/encrypt/device_id.py",
    "app/core/lib/douyin/encrypt/msToken.py",
    "app/core/lib/douyin/encrypt/ttWid.py",
    "app/core/lib/douyin/encrypt/webID.py",
    "app/core/lib/douyin/interface/template.py",
    "app/core/lib/douyin/link/requester.py",
    "app/core/lib/douyin/tools/session.py",
)
_CI_COVERAGE_ARGS = (
    '"--data-file=artifacts/coverage-$shard.dat", "--branch", '
    '"--source=app,cli,entry,shared", "--omit=$omit"'
)


def _coverage_config() -> dict:
    pyproject = loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["tool"]["coverage"]


def _core_workflow_block() -> str:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "python-tests.yml").read_text(
        encoding="utf-8"
    )
    return workflow.split("  core-tests:", 1)[1].split("  browser-tests:", 1)[0]


def test_coverage_run_policy_tracks_core_production_packages_and_branches() -> None:
    run = _coverage_config()["run"]
    omit = tuple(run["omit"])

    assert run["branch"] is True
    assert tuple(run["source"]) == COVERAGE_SOURCE
    assert omit == PURE_LOCAL_ALGORITHM_OMIT
    assert "app/core/lib/*" not in omit
    assert not any(set(pattern) & set("*?[]") for pattern in omit)
    for relative in omit:
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        lowered = source.lower()
        assert "cookie" not in lowered
        for marker in ("request_params", "httpx", "urlopen", "requests.", "http.client", "curl_cffi"):
            assert marker not in lowered
    assert set(MEASURED_NETWORK_COOKIE_OR_SESSION).isdisjoint(omit)


def test_coverage_report_policy_is_visible_and_prevents_regression() -> None:
    report = _coverage_config()["report"]

    assert report["show_missing"] is True
    assert report["precision"] >= 1
    assert report["fail_under"] >= 75


def test_ci_uses_the_same_coverage_floor() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "python-tests.yml").read_text(
        encoding="utf-8"
    )
    run = _coverage_config()["run"]
    core_block = _core_workflow_block()

    assert _coverage_config()["report"]["fail_under"] >= 75
    assert "python -m coverage report" in workflow
    assert "coverage report --fail-under" not in workflow
    # 每个分片把插桩数据写进自己的 data file（非隐藏名：upload-artifact 的
    # glob 不匹配点前缀文件），再由 core-coverage job 合并统一报告；
    # 门槛仍然只由 pyproject 的 fail_under 控制，不允许 CI 私自另设。
    assert _CI_COVERAGE_ARGS in core_block
    assert "python -m coverage combine covdata/coverage-*.dat" in workflow
    expected_source = ",".join(COVERAGE_SOURCE)
    expected_omit = ",".join(PURE_LOCAL_ALGORITHM_OMIT)
    assert expected_source == ",".join(run["source"])
    assert expected_omit == ",".join(run["omit"])
    assert f'$omit = "{expected_omit}"' in core_block


def test_coverage_policy_imports_when_stdlib_tomllib_is_unavailable() -> None:
    script = r"""
import builtins
import runpy
import sys

try:
    import tomli as backend
except ModuleNotFoundError:
    import tomllib as backend

import pytest  # Load pytest before simulating Python 3.10.

sys.modules["tomli"] = backend
real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == "tomllib":
        raise ModuleNotFoundError("simulated Python 3.10")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
runpy.run_path(sys.argv[1], run_name="coverage_policy_compat_probe")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, __file__],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
