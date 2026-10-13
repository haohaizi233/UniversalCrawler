from __future__ import annotations

import builtins
import shlex
import unittest
from unittest import mock

from tests.support.paths import PROJECT_ROOT
from tests.support.toml_compat import loads


def _parse_bandit_command(command: str) -> tuple[list[str], list[str], bool]:
    argv = shlex.split(command)
    if argv[:3] != ["python", "-m", "bandit"]:
        raise AssertionError(f"not a bandit command: {command}")
    recursive = False
    excludes: list[str] = []
    targets: list[str] = []
    tokens = argv[3:]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"-r", "--recursive"}:
            recursive = True
        elif token in {"-x", "--exclude"}:
            index += 1
            excludes.extend(part for part in tokens[index].split(",") if part)
        elif token.startswith("--exclude="):
            excludes.extend(part for part in token.split("=", 1)[1].split(",") if part)
        elif not token.startswith("-"):
            targets.append(token)
        index += 1
    return targets, excludes, recursive


class GitHubActionsWorkflowTests(unittest.TestCase):
    def test_python_workflow_has_layered_quality_gates(self) -> None:
        workflow = PROJECT_ROOT / ".github" / "workflows" / "python-tests.yml"
        source = workflow.read_text(encoding="utf-8")

        self.assertIn("permissions:\n  contents: read", source)
        self.assertIn("concurrency:", source)
        self.assertIn("cancel-in-progress: true", source)
        self.assertIn("quality:", source)
        self.assertIn("compatibility:", source)
        self.assertIn("security:", source)
        self.assertIn("core-tests:", source)
        self.assertIn("core-coverage:", source)
        self.assertIn("browser-tests:", source)
        self.assertIn("required-check:", source)
        self.assertIn("runs-on: ubuntu-latest", source)
        self.assertIn("runs-on: windows-latest", source)
        self.assertGreaterEqual(source.count("timeout-minutes:"), 4)
        self.assertIn("uses: actions/checkout@v6", source)
        self.assertIn("uses: actions/setup-python@v6", source)
        self.assertIn("cache: pip", source)
        self.assertIn("uses: actions/upload-artifact@v7", source)
        self.assertIn("python -m ruff check", source)
        self.assertIn("python -m mypy", source)
        self.assertIn("python -m bandit", source)
        self.assertIn("python -m pip_audit --local", source)
        self.assertIn("python -m build", source)
        self.assertIn("tests/architecture", source)
        self.assertIn("tests/e2e", source)
        self.assertIn("tests/performance", source)
        self.assertNotIn("--ignore=", source)
        self.assertNotIn("--ignore-glob=", source)
        self.assertIn("--junitxml=", source)
        self.assertIn("python -X faulthandler -m pytest", source)
        self.assertNotIn("python -m unittest discover", source)
        self.assertNotIn("sudo apt-get", source)
        self.assertNotIn("xvfb-run", source)
        self.assertIn("QT_QPA_PLATFORM: offscreen", source)
        self.assertIn('PYTHONFAULTHANDLER: "1"', source)
        self.assertIn("python -m playwright install chromium", source)
        self.assertIn("python -m coverage @coverageArgs -m pytest", source)
        self.assertIn("python -m coverage report", source)
        self.assertNotIn("coverage report --fail-under", source)

        pyproject = loads(
            (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertGreaterEqual(
            pyproject["tool"]["coverage"]["report"]["fail_under"], 75
        )

    def test_python_workflow_covers_declared_python_compatibility(self) -> None:
        workflow = PROJECT_ROOT / ".github" / "workflows" / "python-tests.yml"
        source = workflow.read_text(encoding="utf-8")
        compatibility_block = source.split("  compatibility:", 1)[1].split("  security:", 1)[0]

        self.assertIn("strategy:", compatibility_block)
        self.assertIn("fail-fast: false", compatibility_block)
        self.assertIn('python-version: ["3.10", "3.11", "3.12", "3.13"]', compatibility_block)
        self.assertIn("python -m build", compatibility_block)
        self.assertIn("pip install --no-deps --force-reinstall", compatibility_block)
        self.assertIn("importlib.metadata", compatibility_block)
        self.assertIn("entry.test_entry --self-check", compatibility_block)

    def test_package_smoke_check_does_not_pin_a_stale_release_version(self) -> None:
        workflow = PROJECT_ROOT / ".github" / "workflows" / "python-tests.yml"
        source = workflow.read_text(encoding="utf-8")
        compatibility_block = source.split("  compatibility:", 1)[1].split("  security:", 1)[0]

        self.assertIn("from shared.version import __version__", compatibility_block)
        self.assertIn("assert version('ucrawl') == __version__", compatibility_block)
        self.assertNotRegex(
            compatibility_block,
            r"""version\(['"]ucrawl['"]\)\s*==\s*['"]\d""",
        )

    def test_security_scanner_covers_protocol_signing_code(self) -> None:
        workflow = PROJECT_ROOT / ".github" / "workflows" / "python-tests.yml"
        source = workflow.read_text(encoding="utf-8")
        quality_block = source.split("  quality:", 1)[1].split("  compatibility:", 1)[0]
        raw_command = next(
            line for line in quality_block.splitlines() if "python -m bandit" in line
        )
        command = raw_command[raw_command.index("python -m bandit"):].strip()
        targets, excludes, recursive = _parse_bandit_command(command)
        high_risk_packages = (
            "app",
            "cli",
            "entry",
            "shared",
            "app/core",
            "app/core/lib",
            "app/core/lib/douyin",
            "app/core/lib/douyin/encrypt",
            "app/services",
            "app/services/secure_updater.py",
            "app/services/updater_package_verification.py",
            "app/config/update_trust.py",
        )

        self.assertTrue(recursive)
        self.assertEqual(targets, ["app", "cli", "entry", "shared"])
        self.assertEqual(excludes, ["tests"])
        for excluded in excludes:
            normalized = excluded.replace("\\", "/").strip("/")
            for package in high_risk_packages:
                self.assertFalse(
                    normalized == package
                    or normalized.startswith(package + "/")
                    or package.startswith(normalized + "/"),
                    f"high-risk package {package} is excluded by {excluded}",
                )

    def test_linux_quality_runs_native_exact_generation_contracts(self) -> None:
        workflow = PROJECT_ROOT / ".github" / "workflows" / "python-tests.yml"
        source = workflow.read_text(encoding="utf-8")
        quality_block = source.split("  quality:", 1)[1].split(
            "  compatibility:", 1
        )[0]

        self.assertIn("runs-on: ubuntu-24.04", quality_block)
        self.assertIn("Linux exact-generation contracts", quality_block)
        self.assertIn('findmnt -T "$RUNNER_TEMP"', quality_block)
        self.assertIn(
            '--basetemp="$RUNNER_TEMP/pytest-exact-generation"',
            quality_block,
        )
        self.assertIn(
            "--junitxml=artifacts/linux-exact-generation.xml",
            quality_block,
        )
        self.assertIn(
            "tests/integration/shared/test_linux_owned_generation.py",
            quality_block,
        )
        self.assertIn(
            "tests/unit/shared/test_filesystem_directory_capability.py",
            quality_block,
        )
        self.assertIn(
            "tests/unit/app/services/test_updater_owned_generation.py",
            quality_block,
        )

    def test_browser_job_caches_playwright_runtime(self) -> None:
        workflow = PROJECT_ROOT / ".github" / "workflows" / "python-tests.yml"
        source = workflow.read_text(encoding="utf-8")
        browser_block = source.split("  browser-tests:", 1)[1].split("  required-check:", 1)[0]

        self.assertIn("PLAYWRIGHT_BROWSERS_PATH", browser_block)
        self.assertIn("uses: actions/cache@v5", browser_block)
        self.assertIn("playwright-${{ runner.os }}-chromium-", browser_block)

    def test_browser_report_upload_is_failure_only_and_non_blocking(self) -> None:
        workflow = PROJECT_ROOT / ".github" / "workflows" / "python-tests.yml"
        source = workflow.read_text(encoding="utf-8")
        browser_block = source.split("  browser-tests:", 1)[1].split("  required-check:", 1)[0]
        run_block = browser_block.split("      - name: Run browser shard", 1)[1].split(
            "      - name: Upload browser report",
            1,
        )[0]
        upload_block = browser_block.split("      - name: Upload browser report", 1)[1].split(
            "  required-check:",
            1,
        )[0]

        self.assertIn("id: browser_suite", run_block)
        self.assertNotIn("continue-on-error", run_block)
        self.assertIn("if: failure() && steps.browser_suite.outcome == 'failure'", upload_block)
        self.assertIn("continue-on-error: true", upload_block)
        self.assertIn("uses: actions/upload-artifact@v7", upload_block)
        self.assertIn("name: browser-tests-report-${{ matrix.shard }}", upload_block)
        self.assertIn("artifacts/browser-tests-*.xml", upload_block)
        self.assertIn("compression-level: 0", upload_block)

    def test_performance_benchmarks_run_without_coverage_instrumentation(self) -> None:
        workflow = PROJECT_ROOT / ".github" / "workflows" / "python-tests.yml"
        source = workflow.read_text(encoding="utf-8")
        core_block = source.split("  core-tests:", 1)[1].split("  browser-tests:", 1)[0]

        # 性能预算不插桩：coverage 只通过 @coverageArgs 传给 pytest，任何提及
        # tests/performance 的行都不得同时携带 coverage 插桩。
        for line in core_block.splitlines():
            if "tests/performance" in line:
                self.assertNotIn("coverage", line)
        self.assertIn(
            "python -X faulthandler -m pytest tests/performance -q",
            core_block,
        )

    def test_full_suite_is_visible_and_partitioned_without_coverage_gaps(self) -> None:
        workflow = PROJECT_ROOT / ".github" / "workflows" / "python-tests.yml"
        source = workflow.read_text(encoding="utf-8")
        core_block = source.split("  core-tests:", 1)[1].split("  browser-tests:", 1)[0]
        browser_block = source.split("  browser-tests:", 1)[1].split("  required-check:", 1)[0]

        self.assertIn(
            "name: Core shard / ${{ matrix.shard }}",
            core_block,
        )
        self.assertIn("name: Browser shard / ${{ matrix.shard }}", browser_block)
        # 分片后没有单一的 coverage 命令行：每条插桩调用都通过 @coverageArgs
        # 携带同一份配置，这里聚合所有插桩行，确认核心套件无一漏网。
        instrumented = "\n".join(
            line for line in core_block.splitlines() if "@coverageArgs" in line
        )
        self.assertIn("coverage @coverageArgs -m pytest", instrumented)
        for suite_path in (
            "tests/unit",
            "tests/integration",
            "tests/release",
            "tests/architecture",
            "tests/testkit",
        ):
            self.assertIn(suite_path, instrumented)
        # contract 节点通过 catalog 变量插桩运行，e2e/performance 始终不插桩。
        self.assertIn("coverage @coverageArgs -m pytest $chunk", instrumented)
        for suite_path in ("tests/contract", "tests/e2e", "tests/performance"):
            self.assertNotIn(suite_path, instrumented)
        self.assertIn("pytest tests/performance", core_block)
        self.assertIn("pytest tests/e2e", browser_block)
        # 所有分片的插桩数据由独立 job 合并成一份报告，不产生覆盖缺口。
        self.assertIn("python -m coverage combine covdata/.coverage-*", source)

    def test_contract_suite_is_catalog_driven_and_runs_in_bounded_process_shards(self) -> None:
        workflow = PROJECT_ROOT / ".github" / "workflows" / "python-tests.yml"
        source = workflow.read_text(encoding="utf-8")
        core_block = source.split("  core-tests:", 1)[1].split("  browser-tests:", 1)[0]

        self.assertIn("get_resolved_files('contract')", core_block)
        self.assertNotIn("Get-ChildItem -LiteralPath tests/contract/frontend", core_block)
        for legacy_domain in (
            "tests/contract/cli",
            "tests/contract/config",
            "tests/contract/cross_interface",
            "tests/contract/entry",
            "tests/contract/frontend",
            "tests/contract/python",
            "tests/contract/web",
        ):
            self.assertNotIn(legacy_domain, core_block)
        self.assertIn("--collect-only -q", core_block)
        # 契约节点按目录发现顺序均分为固定数量的分片，每片独立 pytest 进程，
        # 不再把数千节点挤进单个进程。
        self.assertIn("$groupCount = 4", core_block)
        self.assertIn("$contractNodes[$start..$end]", core_block)
        self.assertIn("coverage @coverageArgs -m pytest $chunk", core_block)
        self.assertIn('artifacts/core-contract-$shardIndex.xml', core_block)

    def test_required_check_aggregates_all_blocking_jobs(self) -> None:
        workflow = PROJECT_ROOT / ".github" / "workflows" / "python-tests.yml"
        source = workflow.read_text(encoding="utf-8")
        required_block = source.split("  required-check:", 1)[1]

        for job in (
            "quality",
            "compatibility",
            "security",
            "core-tests",
            "core-coverage",
            "browser-tests",
        ):
            self.assertIn(f"      - {job}", required_block)

    def test_linux_quality_job_does_not_install_runtime_requirements(self) -> None:
        workflow = PROJECT_ROOT / ".github" / "workflows" / "python-tests.yml"
        source = workflow.read_text(encoding="utf-8")
        quality_block = source.split("  quality:", 1)[1].split("  compatibility:", 1)[0]

        self.assertIn("requirements-dev.txt", quality_block)
        self.assertNotIn("requirements.txt", quality_block.replace("requirements-dev.txt", ""))
        self.assertNotIn("playwright install", quality_block)

    def test_quality_fixtures_do_not_import_qt_workers_without_qt_runtime(self) -> None:
        from tests import conftest

        real_import = builtins.__import__

        def reject_qt_worker_imports(name, *args, **kwargs):
            if name.startswith(("app.services.frontend_state_service", "app.ui.viewmodels")):
                self.fail(f"quality-only test imported Qt worker module: {name}")
            return real_import(name, *args, **kwargs)

        with (
            mock.patch.object(conftest, "_qt_runtime_available", return_value=False),
            mock.patch("builtins.__import__", side_effect=reject_qt_worker_imports),
        ):
            self.assertEqual(conftest._background_worker_cleanup_targets(), ())

    def test_docker_workflow_uses_buildx_cache_and_bounded_execution(self) -> None:
        workflow = PROJECT_ROOT / ".github" / "workflows" / "docker-build.yml"
        source = workflow.read_text(encoding="utf-8")

        self.assertIn("permissions:\n  contents: read", source)
        self.assertIn("concurrency:", source)
        self.assertIn("cancel-in-progress: true", source)
        self.assertIn("timeout-minutes:", source)
        self.assertIn("uses: actions/checkout@v6", source)
        self.assertIn("uses: docker/setup-buildx-action@v4", source)
        self.assertIn("uses: docker/build-push-action@v7", source)
        self.assertIn("cache-from: type=gha", source)
        self.assertIn("cache-to: type=gha,mode=max", source)

    def test_docker_image_copies_shared_runtime_and_ui_assets(self) -> None:
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY --chown=ucrawl:ucrawl shared ./shared", dockerfile)
        self.assertIn("COPY --chown=ucrawl:ucrawl UI ./UI", dockerfile)

    def test_docker_workflow_smoke_tests_loaded_image_over_https(self) -> None:
        workflow = PROJECT_ROOT / ".github" / "workflows" / "docker-build.yml"
        source = workflow.read_text(encoding="utf-8")
        build_block = source.split("      - name: Build Image", 1)[1].split(
            "      - name:", 1
        )[0]

        self.assertIn("load: true", build_block)
        self.assertIn("      - name: Smoke Test Image", source)
        smoke_block = source.split("      - name: Smoke Test Image", 1)[1]
        self.assertIn("docker run --detach", smoke_block)
        self.assertIn("ucrawl-web:test", smoke_block)
        self.assertIn("https://127.0.0.1:18443/healthz", smoke_block)
        self.assertIn("curl --fail --insecure --silent --show-error", smoke_block)
        self.assertIn("--max-time", smoke_block)
        self.assertRegex(smoke_block, r"for attempt in \$\(seq 1 \d+\); do")
        self.assertIn("trap cleanup EXIT", smoke_block)
        self.assertIn('docker logs "$container_name"', smoke_block)
        self.assertIn('docker rm --force "$container_name"', smoke_block)

    def test_runtime_requirements_exclude_optional_report_and_fedora_tools(self) -> None:
        requirements = PROJECT_ROOT / "requirements.txt"
        lines = {
            line.strip().split("==", 1)[0].split("~=", 1)[0].split(">=", 1)[0].lower()
            for line in requirements.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }

        self.assertNotIn("the-new-hotness", lines)
        self.assertNotIn("beautifulreport", lines)
        self.assertIn("curl-cffi", lines)
        self.assertIn("pycryptodome", lines)
        self.assertIn("uvicorn", lines)
        self.assertIn("yt-dlp", lines)

    def test_setuptools_security_floor_covers_pysec_2026_3447(self) -> None:
        requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
        pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn("setuptools~=83.0.0", requirements)
        self.assertNotIn("setuptools~=81.0.0", requirements)
        self.assertIn('requires = ["setuptools>=83.0.0", "wheel"]', pyproject)


if __name__ == "__main__":
    unittest.main()
