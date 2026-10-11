from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from app.ui.pages.toolbox_link_candidates import LinkCandidatePanel
from app.ui.pages.toolbox_models import result_lines


ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = ROOT / "app" / "web" / "static"
TOOLBOX_JS = STATIC_DIR / "toolbox_controller.js"
TOOLBOX_CSS = STATIC_DIR / "toolbox.css"
TOOLBOX_CONTRACT_JS = STATIC_DIR / "toolbox_contract.js"
TOOLBOX_VIEW_JS = STATIC_DIR / "toolbox_view.js"


def _run_toolbox_contract(expression: str) -> object:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the toolbox JavaScript contract")

    script = r"""
const fs = require("fs");
const vm = require("vm");
global.window = {};
const filenames = process.env.UCP_TOOLBOX_JS.split(require("path").delimiter);
for (const filename of filenames) {
  vm.runInThisContext(fs.readFileSync(filename, "utf8"), { filename });
}
const contract = window.UcpToolboxContract;
const result = Function("contract", `return (${process.env.UCP_TOOLBOX_EXPRESSION});`)(contract);
process.stdout.write(JSON.stringify(result));
"""
    env = {
        **os.environ,
        "UCP_TOOLBOX_JS": os.pathsep.join((str(TOOLBOX_CONTRACT_JS),)),
        "UCP_TOOLBOX_EXPRESSION": expression,
    }
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env=env,
    )
    return json.loads(completed.stdout)


def test_toolbox_assets_load_before_composition_root() -> None:
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert TOOLBOX_JS.is_file()
    assert TOOLBOX_CONTRACT_JS.is_file()
    assert TOOLBOX_VIEW_JS.is_file()
    assert TOOLBOX_CSS.is_file()
    assert '/static/toolbox.css?' in index
    scripts = [
        '/static/toolbox_contract.js?',
        '/static/toolbox_view.js?',
        '/static/toolbox_controller.js?',
        '/static/app.js?',
    ]
    assert all(script in index for script in scripts)
    assert [index.index(script) for script in scripts] == sorted(index.index(script) for script in scripts)


def test_toolbox_contract_evaluates_without_browser_globals() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the toolbox JavaScript contract")

    script = """
const fs = require("fs");
const vm = require("vm");
const context = { window: {} };
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.env.UCP_TOOLBOX_CONTRACT, "utf8"), context);
process.stdout.write(JSON.stringify(Object.isFrozen(context.window.UcpToolboxContract)));
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "UCP_TOOLBOX_CONTRACT": str(TOOLBOX_CONTRACT_JS)},
    )
    assert json.loads(completed.stdout) is True


def test_toolbox_controller_requests_once_and_ignores_disposed_promise_resolution() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the toolbox JavaScript contract")

    script = r"""
const fs = require("fs");
const vm = require("vm");
let lookups = 0;
global.window = { addEventListener() {}, removeEventListener() {} };
global.document = { getElementById() { lookups += 1; return null; } };
for (const filename of process.env.UCP_TOOLBOX_MODULES.split(require("path").delimiter)) {
  vm.runInThisContext(fs.readFileSync(filename, "utf8"), { filename });
}
let resolveAction;
let calls = 0;
const snapshot = {
  toolbox_items: [{ id: "media_probe", parameters: [{ name: "source", required: true, default: "input.mp4" }] }]
};
const controller = window.UcpToolboxController;
controller.configure({
  getState: () => snapshot,
  requestAction: () => { calls += 1; return new Promise(resolve => { resolveAction = resolve; }); }
});
const pending = controller.start();
const lookupsBeforeDispose = lookups;
controller.dispose();
resolveAction({ status: "ok" });
pending.then(() => process.stdout.write(JSON.stringify({ calls, lookupsBeforeDispose, lookupsAfterResolve: lookups })));
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={
            **os.environ,
            "UCP_TOOLBOX_MODULES": os.pathsep.join(
                (str(TOOLBOX_CONTRACT_JS), str(TOOLBOX_VIEW_JS), str(TOOLBOX_JS))
            ),
        },
    )
    result = json.loads(completed.stdout)
    assert result["calls"] == 1
    assert result["lookupsAfterResolve"] == result["lookupsBeforeDispose"]


def test_toolbox_controller_reconfigure_drops_previous_session_projection() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the toolbox JavaScript contract")

    script = r"""
const fs = require("fs");
const vm = require("vm");
global.window = {
  UcpToolboxView: {
    renderCards() {}, renderEmpty() {}, renderDetail() {},
    patchDetail(model) { global.models.push(model); }
  },
  addEventListener() {}, removeEventListener() {}
};
global.models = [];
for (const filename of process.env.UCP_TOOLBOX_MODULES.split(require("path").delimiter)) {
  vm.runInThisContext(fs.readFileSync(filename, "utf8"), { filename });
}
const controller = window.UcpToolboxController;
const tool = { id: "file_verify", parameters: [] };
controller.configure({
  getState: () => ({
    toolbox_items: [tool],
    toolbox_display_projection: {
      tool_id: "file_verify", state: "running", progress: { value: 71 }
    }
  }),
  requestAction: async () => ({ status: "ok" })
});
controller.render();
controller.configure({
  getState: () => ({ toolbox_items: [tool] }),
  requestAction: async () => ({ status: "ok" })
});
controller.render();
process.stdout.write(JSON.stringify(global.models.map(model => model.execution.progress)));
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={
            **os.environ,
            "UCP_TOOLBOX_MODULES": os.pathsep.join((str(TOOLBOX_CONTRACT_JS), str(TOOLBOX_JS))),
        },
    )

    assert json.loads(completed.stdout) == [71, 0]


def test_toolbox_controller_resumes_after_persisted_page_cache_restore() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the toolbox JavaScript contract")

    script = r"""
const fs = require("fs");
const vm = require("vm");
const listeners = new Map();
let cardRenders = 0;
global.window = {
  UcpToolboxView: {
    renderCards() { cardRenders += 1; }, renderEmpty() {}, renderDetail() {}, patchDetail() {}
  },
  addEventListener(name, handler) { listeners.set(name, handler); },
  removeEventListener(name, handler) { if (listeners.get(name) === handler) listeners.delete(name); }
};
for (const filename of process.env.UCP_TOOLBOX_MODULES.split(require("path").delimiter)) {
  vm.runInThisContext(fs.readFileSync(filename, "utf8"), { filename });
}
const controller = window.UcpToolboxController;
controller.configure({
  getState: () => ({ toolbox_items: [{ id: "file_verify", parameters: [] }] }),
  requestAction: async () => ({ status: "ok" })
});
controller.render();
listeners.get("pagehide")({ persisted: true });
const renderedWhileSuspended = controller.render();
listeners.get("pageshow")({ persisted: true });
process.stdout.write(JSON.stringify({ cardRenders, renderedWhileSuspended, handlers: [...listeners.keys()].sort() }));
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={
            **os.environ,
            "UCP_TOOLBOX_MODULES": os.pathsep.join((str(TOOLBOX_CONTRACT_JS), str(TOOLBOX_JS))),
        },
    )
    result = json.loads(completed.stdout)

    assert result == {
        "cardRenders": 2,
        "renderedWhileSuspended": False,
        "handlers": ["pagehide", "pageshow"],
    }


def test_toolbox_controller_owns_interaction_instead_of_app_monolith() -> None:
    module = TOOLBOX_JS.read_text(encoding="utf-8")
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "window.UcpToolboxController = Object.freeze" in module
    for export in (
        "configure",
        "ingest",
        "render",
        "select",
        "updateParameter",
        "validate",
        "start",
        "cancel",
        "openResult",
        "clearHistory",
        "shouldRender",
        "dispose",
        "contract",
    ):
        assert export in module.split("window.UcpToolboxController = Object.freeze({", 1)[1]

    assert "window.UcpToolboxController.configure" in app
    assert "toolboxControllerService().ingest();" in app
    assert "toolboxControllerService().render()" in app
    assert "toolboxControllerService().shouldRender(sections)" in app
    assert "function normalizeToolDefinition" not in app
    assert "function validateParameters" not in app
    assert "function renderParameter" not in app
    assert "document" not in module
    assert '"Action failed"' not in module
    assert '"操作提交失败"' in module


def test_toolbox_view_routes_visible_copy_through_translation_context() -> None:
    module = TOOLBOX_VIEW_JS.read_text(encoding="utf-8")
    labels = (
        "工具详情",
        "输入示例",
        "输出示例",
        "工具参数",
        "执行状态",
        "执行进度",
        "验证参数",
        "启动工具",
        "取消运行",
        "执行结果",
        "打开结果",
        "使用历史",
        "清空历史",
        "请选择工具",
        "暂无参数",
        "暂无执行结果",
        "暂无使用历史",
        "已启用",
        "未启用",
        "等待操作",
    )

    assert "renderEmpty" in module.split("window.UcpToolboxView = Object.freeze({", 1)[1]
    for label in labels:
        matching_lines = [line for line in module.splitlines() if f'"{label}"' in line]
        assert matching_lines
        assert any("text(context" in line for line in matching_lines)
    for untranslated in ("Tool details", "No parameters", "No result", "No history", "Enabled", "Disabled"):
        assert f'"{untranslated}"' not in module


def test_toolbox_parameter_contract_normalizes_and_validates_values() -> None:
    result = _run_toolbox_contract(
        """
(() => {
  const tool = contract.normalizeToolDefinition({
    id: "media_probe",
    parameters: [
      { name: "source", type: "text", required: true },
      { name: "count", type: "integer", min: 1, max: 5, default: 2 },
      { name: "mode", type: "select", options: ["fast", "safe"] },
      { name: "enabled", type: "boolean", default: false }
    ]
  });
  return {
    names: tool.parameters.map(parameter => parameter.name),
    invalid: contract.validateParameters(tool, {
      source: "",
      count: "9",
      mode: "fast",
      enabled: true
    }),
    valid: contract.validateParameters(tool, {
      source: "input.mp4",
      count: "3",
      mode: "safe",
      enabled: true
    })
  };
})()
"""
    )

    assert result["names"] == ["source", "count", "mode", "enabled"]
    assert result["invalid"]["valid"] is False
    assert set(result["invalid"]["errors"]) == {"source", "count"}
    assert result["valid"] == {
        "valid": True,
        "errors": {},
        "values": {
            "source": "input.mp4",
            "count": 3,
            "mode": "safe",
            "enabled": True,
        },
    }


def test_toolbox_actions_use_new_contract_with_legacy_start_fallback() -> None:
    result = _run_toolbox_contract(
        """
(() => {
  const modern = contract.normalizeToolDefinition({
    id: "media_probe",
    parameters: [{ name: "source", required: true }]
  });
  const legacy = contract.normalizeToolDefinition({ id: "link_parser" });
  return {
    modern: ["validate", "start", "cancel", "openResult", "clearHistory"]
      .map(kind => contract.resolveActionName(kind, modern, {})),
    legacyStart: contract.resolveActionName("start", legacy, {}),
    explicitStart: contract.resolveActionName(
      "start",
      legacy,
      { toolbox_contract: { actions: { start: "tool_start" } } }
    ),
    startPayload: contract.buildActionPayload(
      "start",
      modern,
      { source: "input.mp4" },
      { runId: "run-7" }
    ),
    cancelPayload: contract.buildActionPayload("cancel", modern, {}, { runId: "run-7" }),
    openPayload: contract.buildActionPayload(
      "openResult",
      modern,
      {},
      { runId: "run-7", resultId: "result-9" }
    )
  };
})()
"""
    )

    assert result["modern"] == [
        "tool_validate",
        "tool_start",
        "tool_cancel",
        "tool_open_result",
        "tool_clear_history",
    ]
    assert result["legacyStart"] == "run_tool"
    assert result["explicitStart"] == "tool_start"
    assert result["startPayload"] == {
        "tool_id": "media_probe",
        "parameters": {"source": "input.mp4"},
    }
    assert result["cancelPayload"] == {"tool_id": "media_probe", "run_id": "run-7"}
    assert result["openPayload"] == {"tool_id": "media_probe", "result_id": "result-9"}


def test_toolbox_runtime_consumes_delta_sections_without_polling() -> None:
    module = TOOLBOX_JS.read_text(encoding="utf-8")
    result = _run_toolbox_contract(
        """
({
  relevant: contract.shouldRender(new Set(["app_status", "toolbox_execution"])),
  irrelevant: contract.shouldRender(new Set(["app_status", "queue_items"])),
  execution: contract.executionFor({
    toolbox_execution: {
      tool_id: "media_probe",
      run_id: "run-7",
      status: "running",
      progress: 42,
      result: { path: "D:/output/report.json" }
    }
  }, "media_probe")
})
"""
    )

    assert result["relevant"] is True
    assert result["irrelevant"] is False
    assert result["execution"]["status"] == "running"
    assert result["execution"]["progress"] == 42
    assert result["execution"]["runId"] == "run-7"
    assert result["execution"]["resultPath"] == "D:/output/report.json"
    assert "setInterval(" not in module
    assert "/api/frontend/state" not in module
    assert "/api/frontend/delta" not in module
    assert 'requestAction(action, payload)' in module


def test_toolbox_consumes_shared_display_projection_contract() -> None:
    result = _run_toolbox_contract(
        """
(() => {
  const snapshot = {
    toolbox_items: [{ id: "file_verify", title: "File verify" }],
    toolbox_display_projection: {
      tool_id: "file_verify",
      state: "running",
      status_text: "Calculating",
      run_id: "run-7",
      form: {
        fields: [
          { id: "source", label: "File", type: "text", required: true },
          {
            id: "algorithm",
            label: "Algorithm",
            type: "choice",
            options: [{ value: "sha256", label: "SHA256" }]
          }
        ],
        values: { source: "D:/media/demo.mp4", algorithm: "sha256" }
      },
      progress: { value: 63, text: "63% - demo.mp4" },
      validation: { state: "valid", message: "Parameters are valid" },
      actions: {
        tool_validate: false,
        tool_start: false,
        tool_cancel: true,
        tool_open_result: false,
        tool_clear_history: false
      }
    }
  };
  const projection = contract.projectionFor(snapshot, "file_verify");
  const tool = contract.toolForSnapshot(snapshot, "file_verify");
  return {
    fields: tool.parameters.map(field => [field.name, field.type]),
    values: tool.initialValues,
    execution: contract.executionFor(snapshot, "file_verify"),
    validateEnabled: contract.actionEnabled(projection, "tool_validate", true),
    cancelEnabled: contract.actionEnabled(projection, "tool_cancel", false)
  };
})()
"""
    )

    assert result["fields"] == [["source", "text"], ["algorithm", "select"]]
    assert result["values"] == {"source": "D:/media/demo.mp4", "algorithm": "sha256"}
    assert result["execution"]["status"] == "running"
    assert result["execution"]["statusText"] == "Calculating"
    assert result["execution"]["progress"] == 63
    assert result["execution"]["progressText"] == "63% - demo.mp4"
    assert result["execution"]["runId"] == "run-7"
    assert result["validateEnabled"] is False
    assert result["cancelEnabled"] is True


def test_toolbox_display_batch_wins_and_result_projection_omits_raw_logs() -> None:
    result = _run_toolbox_contract(
        """
(() => {
  const snapshot = {
    toolbox_display_projection: { tool_id: "file_verify", state: "ready" },
    toolbox_display_batch: {
      projections: [{
        tool_id: "file_verify",
        state: "success",
        result: {
          id: "result-9",
          display_text: "SHA256 complete",
          rows: [{ label: "Digest", value: "abc123" }],
          raw_logs: ["RAW_LOG_MUST_NOT_RENDER"]
        },
        history: [{
          id: "history-1",
          title: "File verify",
          status_text: "Success",
          finished_at: "2026-07-27 10:20"
        }],
        actions: { tool_open_result: true, tool_clear_history: true }
      }],
      events: [{ message: "RAW_EVENT_MUST_NOT_RENDER" }]
    }
  };
  const projection = contract.projectionFor(snapshot, "file_verify");
  const execution = contract.executionFor(snapshot, "file_verify");
  return {
    state: projection.state,
    lines: contract.resultDisplayLines(execution.result),
    history: contract.historyFor(snapshot, "file_verify"),
    openEnabled: contract.actionEnabled(projection, "tool_open_result", false)
  };
})()
"""
    )

    assert result["state"] == "success"
    assert result["lines"] == ["SHA256 complete", "Digest: abc123"]
    assert "RAW_LOG_MUST_NOT_RENDER" not in json.dumps(result)
    assert "RAW_EVENT_MUST_NOT_RENDER" not in json.dumps(result)
    assert result["history"][0]["historyId"] == "history-1"
    assert result["history"][0]["title"] == "File verify"
    assert result["openEnabled"] is True


def test_gui_and_web_render_the_same_safe_link_parser_projection() -> None:
    candidate_id = "a" * 64
    projection = {
        "id": "run-link-parser",
        "display_text": "Parsed 1 link",
        "rows": [
            {
                "id": candidate_id,
                "candidate_id": candidate_id,
                "label": "generic page · PLATFORM",
                "value": "https://example.com/[redacted]",
                "platform": "generic",
                "resource_kind": "page",
                "format_hint": "PLATFORM",
                "expanded": False,
            }
        ],
        "warnings": [],
    }

    gui_lines = result_lines(projection, str)
    web_lines = _run_toolbox_contract(
        f"contract.resultDisplayLines({json.dumps(projection, ensure_ascii=False)})"
    )

    assert gui_lines == web_lines == [
        "Parsed 1 link",
        "generic page · PLATFORM: https://example.com/[redacted]",
    ]
    rendered = json.dumps({"gui": gui_lines, "web": web_lines}, ensure_ascii=False)
    assert "private_url" not in rendered
    assert "token=" not in rendered


def test_link_candidate_contract_preserves_selection_and_builds_exact_admission_payload() -> None:
    page_id = "a" * 64
    media_id = "b" * 64
    playlist_id = "c" * 64
    expression = f"""
(() => {{
  const projection = {{
    tool_id: "link_parser",
    run_id: "run-1",
    state: "success",
    result: {{ rows: [
      {{ candidate_id: "{page_id}", resource_kind: "page", platform: "generic", format_hint: "PLATFORM", value: "https://page.example/[redacted]", admittable: false, admission_reason_code: "candidate_requires_crawl_resolution" }},
      {{ candidate_id: "{media_id}", resource_kind: "media", platform: "bilibili", format_hint: "MP4", value: "https://media.example/[redacted]", admittable: true, admission_reason_code: "" }},
      {{ candidate_id: "{playlist_id}", resource_kind: "playlist", platform: "missav", format_hint: "HLS", value: "https://list.example/[redacted]", admittable: true, admission_reason_code: "" }}
    ] }},
    actions: {{ tool_admit_candidates: true }}
  }};
  const first = contract.linkCandidateState(projection, {{}});
  const same = contract.linkCandidateState(projection, {{
    ...first,
    selectedIds: ["{playlist_id}"]
  }});
  const next = contract.linkCandidateState({{
    ...projection,
    run_id: "run-2"
  }}, same);
  const tool = contract.normalizeToolDefinition({{ id: "link_parser", parameters: [] }});
  return {{
    rows: first.rows.map(row => [row.candidateId, row.selectable]),
    firstSelected: first.selectedIds,
    sameSelected: same.selectedIds,
    nextSelected: next.selectedIds,
    action: contract.resolveActionName("admitCandidates", tool, {{
      toolbox_display_projection: projection
    }}),
    payload: contract.buildActionPayload("admitCandidates", tool, {{}}, {{
      runId: same.runId,
      candidateIds: same.selectedIds,
    }})
  }};
}})()
"""

    result = _run_toolbox_contract(expression)

    assert result["rows"] == [
        [page_id, False],
        [media_id, True],
        [playlist_id, True],
    ]
    assert result["firstSelected"] == [media_id, playlist_id]
    assert result["sameSelected"] == [playlist_id]
    assert result["nextSelected"] == [media_id, playlist_id]
    assert result["action"] == "tool_admit_candidates"
    assert result["payload"] == {
        "tool_id": "link_parser",
        "run_id": "run-1",
        "candidate_ids": [playlist_id],
    }
    assert "private_url" not in json.dumps(result)


def test_link_candidate_contract_consumes_server_admission_capability() -> None:
    generic_media_id = "d" * 64
    douyin_playlist_id = "e" * 64
    missav_playlist_id = "f" * 64
    bilibili_media_id = "1" * 64
    expression = f"""
(() => {{
  const state = contract.linkCandidateState({{
    tool_id: "link_parser",
    run_id: "run-policy",
    state: "success",
    result: {{ rows: [
      {{ candidate_id: "{generic_media_id}", resource_kind: "media", platform: "generic", format_hint: "MP4", value: "https://cdn.example/[redacted]", admittable: false, admission_reason_code: "candidate_downloader_unavailable" }},
      {{ candidate_id: "{douyin_playlist_id}", resource_kind: "playlist", platform: "douyin", format_hint: "HLS", value: "https://www.douyin.com/[redacted]", admittable: false, admission_reason_code: "candidate_downloader_unavailable" }},
      {{ candidate_id: "{missav_playlist_id}", resource_kind: "playlist", platform: "missav", format_hint: "HLS", value: "https://missav.ai/[redacted]", admittable: true, admission_reason_code: "" }},
      {{ candidate_id: "{bilibili_media_id}", resource_kind: "media", platform: "bilibili", format_hint: "MP4", value: "https://www.bilibili.com/[redacted]", admittable: true, admission_reason_code: "" }}
    ] }},
    actions: {{ tool_admit_candidates: true }}
  }}, {{}});
  return {{
    rows: state.rows.map(row => [row.candidateId, row.selectable, row.admissionReasonCode]),
    selectedIds: state.selectedIds,
  }};
}})()
"""

    result = _run_toolbox_contract(expression)

    assert result == {
        "rows": [
            [generic_media_id, False, "candidate_downloader_unavailable"],
            [douyin_playlist_id, False, "candidate_downloader_unavailable"],
            [missav_playlist_id, True, ""],
            [bilibili_media_id, True, ""],
        ],
        "selectedIds": [missav_playlist_id, bilibili_media_id],
    }


def test_link_candidate_contract_rejects_non_array_ids_and_bounds_rows() -> None:
    result = _run_toolbox_contract(
        """
(() => {
  const rows = Array.from({ length: 80 }, (_, index) => ({
    candidate_id: (index + 1).toString(16).padStart(64, "0"),
    resource_kind: "media",
    platform: "bilibili",
    format_hint: "MP4",
    value: "https://media.example/[redacted]",
    admittable: true,
    admission_reason_code: ""
  }));
  const state = contract.linkCandidateState({
    tool_id: "link_parser",
    run_id: "run-bounded",
    state: "success",
    result: { rows },
    actions: { tool_admit_candidates: true }
  }, {});
  const tool = contract.normalizeToolDefinition({ id: "link_parser", parameters: [] });
  return {
    rowCount: state.rows.length,
    selectedCount: state.selectedIds.length,
    malformed: contract.buildActionPayload("admitCandidates", tool, {}, {
      runId: "run-bounded",
      candidate_ids: "not-an-array"
    })
  };
})()
"""
    )

    assert result == {
        "rowCount": 64,
        "selectedCount": 64,
        "malformed": {
            "tool_id": "link_parser",
            "run_id": "run-bounded",
            "candidate_ids": [],
        },
    }


def test_link_candidate_contract_keeps_first_64_unique_valid_rows() -> None:
    result = _run_toolbox_contract(
        """
(() => {
  const firstId = "a".repeat(64);
  const rows = [
    ...Array.from({ length: 64 }, () => ({
      candidate_id: "invalid",
      resource_kind: "media"
    })),
    { candidate_id: firstId, resource_kind: "media", platform: "bilibili", format_hint: "MP4", value: "first", admittable: true, admission_reason_code: "" },
    { candidate_id: firstId, resource_kind: "media", platform: "bilibili", format_hint: "MP4", value: "duplicate", admittable: true, admission_reason_code: "" },
    ...Array.from({ length: 70 }, (_, index) => ({
      candidate_id: (index + 1).toString(16).padStart(64, "0"),
      resource_kind: "playlist",
      platform: "missav",
      format_hint: "HLS",
      value: `valid-${index + 1}`,
      admittable: true,
      admission_reason_code: ""
    }))
  ];
  const state = contract.linkCandidateState({
    tool_id: "link_parser",
    run_id: "run-unique-valid",
    state: "success",
    result: { rows },
    actions: { tool_admit_candidates: true }
  }, {});
  return {
    rowCount: state.rows.length,
    selectedCount: state.selectedIds.length,
    firstValue: state.rows[0]?.value,
    uniqueCount: new Set(state.rows.map(row => row.candidateId)).size
  };
})()
"""
    )

    assert result == {
        "rowCount": 64,
        "selectedCount": 64,
        "firstValue": "first",
        "uniqueCount": 64,
    }


def test_toolbox_controller_admits_only_selected_safe_link_candidates() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the toolbox JavaScript contract")

    media_id = "b" * 64
    playlist_id = "c" * 64
    script = rf"""
const fs = require("fs");
const vm = require("vm");
global.models = [];
global.window = {{
  UcpToolboxView: {{
    renderCards() {{}}, renderEmpty() {{}}, renderDetail() {{}},
    patchDetail(model) {{ global.models.push(JSON.parse(JSON.stringify(model))); }}
  }},
  addEventListener() {{}}, removeEventListener() {{}}
}};
for (const filename of process.env.UCP_TOOLBOX_MODULES.split(require("path").delimiter)) {{
  vm.runInThisContext(fs.readFileSync(filename, "utf8"), {{ filename }});
}}
const projection = {{
  tool_id: "link_parser", run_id: "run-1", state: "success",
  result: {{ rows: [
    {{ candidate_id: "{media_id}", resource_kind: "media", platform: "bilibili", format_hint: "MP4", value: "https://media.example/[redacted]", admittable: true, admission_reason_code: "" }},
    {{ candidate_id: "{playlist_id}", resource_kind: "playlist", platform: "missav", format_hint: "HLS", value: "https://list.example/[redacted]", admittable: true, admission_reason_code: "" }}
  ] }},
  actions: {{ tool_admit_candidates: true }}
}};
const snapshot = {{ toolbox_items: [{{ id: "link_parser", parameters: [] }}], toolbox_display_projection: projection }};
const calls = [];
const controller = window.UcpToolboxController;
controller.configure({{
  getState: () => snapshot,
  requestAction: async (action, payload) => {{
    calls.push([action, payload]);
    return {{
      status: "ok",
      data: {{
        status: "queued", tool_id: "link_parser", run_id: "run-1",
        accepted_count: 1, candidate_ids: ["{playlist_id}"],
        video_ids: ["video-1"],
        private_url: "https://secret.example/token"
      }}
    }};
  }}
}});
controller.render();
controller.toggleCandidate("{media_id}", false);
controller.render();
controller.admitCandidates().then(() => {{
  const receiptModel = global.models[global.models.length - 1];
  snapshot.toolbox_display_projection = {{ ...projection, run_id: "run-2" }};
  controller.render();
  const nextModel = global.models[global.models.length - 1];
  process.stdout.write(JSON.stringify({{
    calls,
    sameSelected: receiptModel.candidates.selectedIds,
    receipt: receiptModel.candidateReceipt,
    nextSelected: nextModel.candidates.selectedIds
  }}));
}});
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={
            **os.environ,
            "UCP_TOOLBOX_MODULES": os.pathsep.join(
                (str(TOOLBOX_CONTRACT_JS), str(TOOLBOX_JS))
            ),
        },
    )
    result = json.loads(completed.stdout)

    assert result["calls"] == [
        [
            "tool_admit_candidates",
            {
                "tool_id": "link_parser",
                "run_id": "run-1",
                "candidate_ids": [playlist_id],
            },
        ]
    ]
    assert result["sameSelected"] == [playlist_id]
    assert result["nextSelected"] == [media_id, playlist_id]
    assert result["receipt"] == {
        "status": "queued",
        "tool_id": "link_parser",
        "run_id": "run-1",
        "accepted_count": 1,
        "candidate_ids": [playlist_id],
        "video_ids": ["video-1"],
    }
    assert "private_url" not in json.dumps(result)


def test_toolbox_controller_rejects_mismatched_or_malformed_candidate_receipts() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the toolbox JavaScript contract")

    media_id = "b" * 64
    script = rf"""
const fs = require("fs");
const vm = require("vm");
global.models = [];
global.window = {{
  UcpToolboxView: {{
    renderCards() {{}}, renderEmpty() {{}}, renderDetail() {{}},
    patchDetail(model) {{ global.models.push(JSON.parse(JSON.stringify(model))); }}
  }},
  addEventListener() {{}}, removeEventListener() {{}}
}};
for (const filename of process.env.UCP_TOOLBOX_MODULES.split(require("path").delimiter)) {{
  vm.runInThisContext(fs.readFileSync(filename, "utf8"), {{ filename }});
}}
const projection = {{
  tool_id: "link_parser", run_id: "run-expected", state: "success",
  result: {{ rows: [{{ candidate_id: "{media_id}", resource_kind: "media", platform: "bilibili", format_hint: "MP4", value: "safe", admittable: true, admission_reason_code: "" }}] }},
  actions: {{ tool_admit_candidates: true }}
}};
const snapshot = {{ toolbox_items: [{{ id: "link_parser", parameters: [] }}], toolbox_display_projection: projection }};
const controller = window.UcpToolboxController;
controller.configure({{
  getState: () => snapshot,
  requestAction: async () => ({{
    status: "ok",
    data: {{
      status: "queued", tool_id: "link_parser", run_id: "run-stale",
      accepted_count: true,
      candidate_ids: Array(65).fill("{media_id}"),
      video_ids: Array(65).fill("video-1")
    }}
  }})
}});
controller.render();
controller.admitCandidates().then(() => {{
  const model = global.models[global.models.length - 1];
  process.stdout.write(JSON.stringify({{
    receipt: model.candidateReceipt,
    selectedIds: model.candidates.selectedIds,
    actionMessage: model.actionMessage
  }}));
}});
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={
            **os.environ,
            "UCP_TOOLBOX_MODULES": os.pathsep.join(
                (str(TOOLBOX_CONTRACT_JS), str(TOOLBOX_JS))
            ),
        },
    )
    result = json.loads(completed.stdout)

    assert result["receipt"] == {}
    assert result["selectedIds"] == [media_id]
    assert result["actionMessage"]


def test_gui_and_web_reject_the_same_unsafe_candidate_receipt_video_id() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the toolbox JavaScript contract")

    candidate_id = "b" * 64
    unsafe_video_id = "video id with spaces"
    gui_receipt = LinkCandidatePanel._validated_receipt_count(
        {
            "status": "queued",
            "tool_id": "link_parser",
            "run_id": "run-expected",
            "accepted_count": 1,
            "candidate_ids": [candidate_id],
            "video_ids": [unsafe_video_id],
        },
        ("link_parser", "run-expected", (candidate_id,), None),
    )

    script = rf"""
const fs = require("fs");
const vm = require("vm");
global.models = [];
global.window = {{
  UcpToolboxView: {{
    renderCards() {{}}, renderEmpty() {{}}, renderDetail() {{}},
    patchDetail(model) {{ global.models.push(JSON.parse(JSON.stringify(model))); }}
  }},
  addEventListener() {{}}, removeEventListener() {{}}
}};
for (const filename of process.env.UCP_TOOLBOX_MODULES.split(require("path").delimiter)) {{
  vm.runInThisContext(fs.readFileSync(filename, "utf8"), {{ filename }});
}}
const projection = {{
  tool_id: "link_parser", run_id: "run-expected", state: "success",
  result: {{ rows: [{{ candidate_id: "{candidate_id}", resource_kind: "media", value: "safe" }}] }},
  actions: {{ tool_admit_candidates: true }}
}};
const snapshot = {{ toolbox_items: [{{ id: "link_parser", parameters: [] }}], toolbox_display_projection: projection }};
const controller = window.UcpToolboxController;
controller.configure({{
  getState: () => snapshot,
  requestAction: async () => ({{
    status: "ok",
    data: {{
      status: "queued", tool_id: "link_parser", run_id: "run-expected",
      accepted_count: 1, candidate_ids: ["{candidate_id}"],
      video_ids: ["{unsafe_video_id}"]
    }}
  }})
}});
controller.render();
controller.admitCandidates().then(() => {{
  const model = global.models[global.models.length - 1];
  process.stdout.write(JSON.stringify({{ receipt: model.candidateReceipt }}));
}});
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        encoding="utf-8",
        env={
            **os.environ,
            "UCP_TOOLBOX_MODULES": os.pathsep.join(
                (str(TOOLBOX_CONTRACT_JS), str(TOOLBOX_JS))
            ),
        },
    )

    assert gui_receipt is None
    assert json.loads(completed.stdout)["receipt"] == {}


def test_toolbox_projection_reducer_preserves_form_across_progress_only_delta() -> None:
    result = _run_toolbox_contract(
        """
(() => {
  let cache = contract.reduceProjectionCache({}, {
    toolbox_display_projection: {
      tool_id: "file_verify",
      state: "ready",
      form: {
        fields: [{ id: "source", type: "text" }],
        values: { source: "D:/media/demo.mp4" }
      }
    }
  });
  cache = contract.reduceProjectionCache(cache, {
    toolbox_display_batch: [{
      tool_id: "file_verify",
      state: "running",
      progress: { value: 28, text: "2 / 7" }
    }]
  });
  return cache.file_verify;
})()
"""
    )

    assert result["state"] == "running"
    assert result["progress"] == {"value": 28, "text": "2 / 7"}
    assert result["form"]["fields"] == [{"id": "source", "type": "text"}]
    assert result["form"]["values"] == {"source": "D:/media/demo.mp4"}


def test_toolbox_styles_use_theme_tokens_and_responsive_constraints() -> None:
    css = TOOLBOX_CSS.read_text(encoding="utf-8")

    for token in ("var(--panel)", "var(--text)", "var(--muted)", "var(--border)", "var(--accent)"):
        assert token in css
    assert "#page-toolbox" in css
    assert ".toolbox-parameter" in css
    assert ".toolbox-progress" in css
    assert ".toolbox-history" in css
    assert ".toolbox-candidate-list" in css
    assert ".toolbox-candidate-row" in css
    assert "@media (max-width:" in css
    assert "#" not in "\n".join(
        line for line in css.splitlines() if not line.lstrip().startswith("#page-toolbox")
    )


def test_toolbox_visible_labels_have_all_webui_translations() -> None:
    i18n = (STATIC_DIR / "i18n.js").read_text(encoding="utf-8")
    english = i18n.split('"en-US": {', 1)[1].split('"zh-TW": {', 1)[0]
    traditional = i18n.split('"zh-TW": {', 1)[1].split("};", 1)[0]
    labels = (
        "工具参数",
        "验证参数",
        "启动工具",
        "取消运行",
        "执行状态",
        "执行进度",
        "执行结果",
        "使用历史",
        "打开结果",
        "清空历史",
        "请选择工具",
        "暂无参数",
        "暂无执行结果",
        "暂无使用历史",
        "请修正标记的参数",
        "参数已更改，请重新验证",
        "等待操作",
        "正在启动",
        "运行中",
        "正在取消",
        "候选资源",
        "仅可加入服务端标记为可下载的候选项；其他项目仅供预览。",
        "加入下载队列",
        "已选择",
        "项",
        "已加入下载队列",
        "页面",
        "媒体",
        "播放列表",
    )

    for label in labels:
        assert f'"{label}":' in english
        assert f'"{label}":' in traditional


def test_candidate_copy_is_shared_by_gui_webui_and_runtime_catalogs() -> None:
    gui = (ROOT / "app" / "ui" / "pages" / "toolbox_link_candidates.py").read_text(
        encoding="utf-8"
    )
    web = TOOLBOX_VIEW_JS.read_text(encoding="utf-8")
    english = json.loads((ROOT / "app" / "ui" / "i18n" / "en-US.json").read_text(encoding="utf-8"))
    traditional = json.loads(
        (ROOT / "app" / "ui" / "i18n" / "zh-TW.json").read_text(encoding="utf-8")
    )
    labels = (
        "候选资源",
        "仅可加入服务端标记为可下载的候选项；其他项目仅供预览。",
        "加入下载队列",
        "仅预览，不能加入下载队列",
        "已选择",
        "项",
        "已加入下载队列",
        "候选资源加入失败",
        "候选资源回执无效",
        "类型",
        "候选地址",
        "页面",
        "媒体",
        "播放列表",
    )

    for label in labels[:3]:
        assert label in gui
        assert label in web
    for label in labels:
        assert english.get(label)
        assert traditional.get(label)

    assert '"已加入下载队列": "Added to download queue"' in (
        ROOT / "app" / "web" / "static" / "i18n.js"
    ).read_text(encoding="utf-8")


def test_toolbox_view_renders_candidate_rows_from_safe_controller_model() -> None:
    module = TOOLBOX_VIEW_JS.read_text(encoding="utf-8")

    for marker in (
        'id="toolboxCandidateSection"',
        'id="toolboxCandidateList"',
        'id="toolboxAdmitCandidatesButton"',
        'data-toolbox-candidate',
        "context.toggleCandidate",
        "context.admitCandidates",
    ):
        assert marker in module
    assert "private_url" not in module
    assert "private_path" not in module
