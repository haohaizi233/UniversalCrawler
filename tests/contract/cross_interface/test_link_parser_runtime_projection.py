from __future__ import annotations

import json
from pathlib import Path

from shared.execution_profile import local_execution_profile


_CANDIDATE_KEYS = {
    "candidate_id",
    "display_url",
    "platform",
    "resource_kind",
    "format_hint",
    "expanded",
    "admittable",
    "admission_reason_code",
}


def _web_link_candidate_state(rows: list[dict]) -> dict:
    import os
    import shutil
    import subprocess

    import pytest

    from tests.support.paths import PROJECT_ROOT

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the toolbox JavaScript contract")
    contract_path = PROJECT_ROOT / "app" / "web" / "static" / "toolbox_contract.js"
    script = r"""
const fs = require("fs");
const vm = require("vm");
global.window = {};
vm.runInThisContext(fs.readFileSync(process.env.UCP_TOOLBOX_CONTRACT, "utf8"));
const state = window.UcpToolboxContract.linkCandidateState({
  tool_id: "link_parser",
  run_id: "run-shared-projection",
  state: "success",
  result: { rows: JSON.parse(process.env.UCP_LINK_ROWS) },
  actions: { tool_admit_candidates: true }
}, {});
process.stdout.write(JSON.stringify({
  rows: state.rows.map(row => [
    row.candidateId,
    row.value,
    row.platform,
    row.resourceKind,
    row.formatHint,
    row.selectable,
    row.admissionReasonCode
  ]),
  selectedIds: state.selectedIds
}));
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={
            **os.environ,
            "UCP_TOOLBOX_CONTRACT": str(contract_path),
            "UCP_LINK_ROWS": json.dumps(rows),
        },
    )
    return json.loads(completed.stdout)


def _sdk_profile(tmp_path: Path):
    return local_execution_profile(
        host_surface="sdk",
        owner_id="sdk:link-parser-contract",
        approved_roots=(tmp_path,),
        tool_permissions=("network",),
        allow_external_plugins=False,
    )


def test_cli_and_sdk_receive_the_same_safe_link_parser_runtime_projection(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from app.services import tool_runner_service as runner_module
    from cli.commands import tools as tools_command
    from cli.main import main
    from ucrawl.tools import ToolsAPI

    monkeypatch.setattr(tools_command, "get_default_save_dir", lambda: str(tmp_path))
    monkeypatch.setattr(runner_module, "user_cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(runner_module, "user_data_root", lambda: tmp_path / "data")

    parameters = {
        "text": " ".join(
            (
                "https://cdn.example.com/private/generic.mp4?token=generic-secret",
                "https://www.douyin.com/private/master.m3u8?token=douyin-secret",
                "https://missav.ai/private/master.m3u8?token=missav-secret",
                "https://www.bilibili.com/private/video.mp4?token=bilibili-secret",
            )
        )
    }
    assert (
        main(
            [
                "tools",
                "run",
                "link_parser",
                "--params",
                json.dumps(parameters),
            ]
        )
        == 0
    )
    cli_result = json.loads(capsys.readouterr().out)

    api = ToolsAPI(execution_profile=_sdk_profile(tmp_path))
    try:
        sdk_result = api.run_sync("link_parser", parameters, timeout=2.0)
    finally:
        assert api.close()

    cli_links = cli_result["result"]["data"]["links"]
    sdk_links = sdk_result["result"]["data"]["links"]
    assert cli_links == sdk_links
    assert all(set(row) == _CANDIDATE_KEYS for row in cli_links)
    assert {
        (row["platform"], row["resource_kind"]): (
            row["admittable"],
            row["admission_reason_code"],
        )
        for row in cli_links
    } == {
        ("generic", "media"): (False, "candidate_downloader_unavailable"),
        ("douyin", "playlist"): (False, "candidate_downloader_unavailable"),
        ("missav", "playlist"): (True, ""),
        ("bilibili", "media"): (True, ""),
    }
    assert all(row["display_url"].endswith("/[redacted]") for row in cli_links)
    public_text = json.dumps({"cli": cli_result, "sdk": sdk_result}, ensure_ascii=False)
    for secret in (
        "generic-secret",
        "douyin-secret",
        "missav-secret",
        "bilibili-secret",
        "/private/",
        "?token=",
    ):
        assert secret not in public_text


def test_tool_runner_tools_api_and_cli_fail_closed_for_foreign_runtime_rows(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from app.services.tool_runner_service import ToolRunnerService
    from cli.commands import tools as tools_command
    from cli.main import main
    from ucrawl.tools import ToolsAPI

    class _ForeignQueuedToolsAPI(ToolsAPI):
        def __init__(self, execution_profile, service, run_id: str) -> None:
            super().__init__(execution_profile=execution_profile, service=service)
            self._foreign_run_id = run_id

        def run(self, tool_id, params):
            del tool_id, params
            return {"status": "queued", "run_id": self._foreign_run_id}

    parameters = {"text": "https://www.bilibili.com/private/video.mp4?token=owner"}
    owner_profile = local_execution_profile(
        host_surface="sdk",
        owner_id="sdk:owner",
        approved_roots=(tmp_path,),
        tool_permissions=("network",),
        allow_external_plugins=False,
    )
    other_profile = local_execution_profile(
        host_surface="sdk",
        owner_id="sdk:other",
        approved_roots=(tmp_path,),
        tool_permissions=("network",),
        allow_external_plugins=False,
    )
    history_path = tmp_path / "owner-history.json"
    service = ToolRunnerService(history_path=history_path, max_workers=1)
    owner_api = ToolsAPI(execution_profile=owner_profile, service=service)
    other_api = ToolsAPI(execution_profile=other_profile, service=service)
    try:
        queued = owner_api.run("link_parser", parameters)
        owner_terminal = owner_api.wait_for_run(queued["run_id"], timeout=2.0)
        assert owner_terminal["result"]["data"]["links"]

        assert other_api.get_run(queued["run_id"]) is None
        mismatch = other_api.wait_for_run(queued["run_id"], timeout=0)
        assert mismatch == {
            "status": "forbidden",
            "code": "tool_owner_mismatch",
            "message": "tool run belongs to another owner",
            "run_id": queued["run_id"],
        }
        owner_history = owner_api.history(tool_id="link_parser")
        assert "data" not in owner_history[0]["result"]

        monkeypatch.setattr(
            tools_command, "get_default_save_dir", lambda: str(tmp_path)
        )
        monkeypatch.setattr(
            tools_command,
            "ToolsAPI",
            lambda *, execution_profile: _ForeignQueuedToolsAPI(
                execution_profile,
                service,
                queued["run_id"],
            ),
        )
        assert (
            main(
                [
                    "tools",
                    "run",
                    "link_parser",
                    "--params",
                    json.dumps(parameters),
                ]
            )
            != 0
        )
        assert json.loads(capsys.readouterr().out) == mismatch
    finally:
        assert other_api.close()
        assert owner_api.close()

    persisted = json.loads(history_path.read_text(encoding="utf-8"))
    assert "data" not in persisted[0]["result"]


def test_shared_runtime_link_projection_has_equivalent_gui_and_web_semantics() -> None:
    from app.services.frontend_toolbox_state import FrontendToolboxStateMixin
    from app.services.tool_history_projection import (
        project_link_parser_runtime_links,
    )

    raw_rows = [
        {
            "candidate_id": "a" * 64,
            "display_url": "https://cdn.example.com/[redacted]",
            "platform": "generic",
            "resource_kind": "media",
            "format_hint": "MP4",
            "expanded": False,
            "admittable": False,
            "admission_reason_code": "candidate_downloader_unavailable",
        },
        {
            "candidate_id": "b" * 64,
            "display_url": "https://www.douyin.com/[redacted]",
            "platform": "douyin",
            "resource_kind": "playlist",
            "format_hint": "HLS",
            "expanded": False,
            "admittable": False,
            "admission_reason_code": "candidate_downloader_unavailable",
        },
        {
            "candidate_id": "c" * 64,
            "display_url": "https://missav.ai/[redacted]",
            "platform": "missav",
            "resource_kind": "playlist",
            "format_hint": "HLS",
            "expanded": False,
            "admittable": True,
            "admission_reason_code": "",
        },
        {
            "candidate_id": "d" * 64,
            "display_url": "https://www.bilibili.com/[redacted]",
            "platform": "bilibili",
            "resource_kind": "media",
            "format_hint": "MP4",
            "expanded": True,
            "admittable": True,
            "admission_reason_code": "",
        },
        {
            "candidate_id": "e" * 64,
            "display_url": "https://www.bilibili.com/private/video.mp4?token=secret",
            "platform": "bilibili",
            "resource_kind": "media",
            "format_hint": "VIDEO",
            "expanded": False,
            "admittable": True,
            "admission_reason_code": "",
        },
    ]
    safe_rows = project_link_parser_runtime_links({"links": raw_rows})
    assert [row["candidate_id"] for row in safe_rows] == [
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "d" * 64,
    ]

    gui_projector = object.__new__(FrontendToolboxStateMixin)
    gui_rows = []
    for safe_row in safe_rows:
        gui_row, truncated = gui_projector._project_link_parser_row(safe_row)
        assert gui_row is not None
        assert truncated is False
        gui_rows.append(
            [
                gui_row["candidate_id"],
                gui_row["value"],
                gui_row["platform"],
                gui_row["resource_kind"],
                gui_row["format_hint"],
                gui_row["admittable"],
                gui_row["admission_reason_code"],
            ]
        )

    web_state = _web_link_candidate_state(safe_rows)
    assert web_state["rows"] == gui_rows
    assert web_state["selectedIds"] == ["c" * 64, "d" * 64]
