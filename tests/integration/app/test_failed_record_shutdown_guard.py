from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def test_non_daemon_guardian_blocks_natural_exit_until_pending_write_finishes(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "persisted.txt"
    script = """
import sys
import time
from pathlib import Path
from app.services.failed_record_shutdown_guard import ensure_failed_record_store_drained

class SlowStore:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def shutdown(self, timeout=1.0):
        self.calls += 1
        if self.calls == 1:
            return False
        time.sleep(0.25)
        self.output.write_text("persisted", encoding="utf-8")
        return True

store = SlowStore(Path(sys.argv[1]))
assert store.shutdown() is False
ensure_failed_record_store_drained(store)
"""

    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-c", script, str(output_path)],
        cwd=Path(__file__).resolve().parents[3],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3])},
        check=False,
        capture_output=True,
        text=True,
        timeout=10.0,
    )

    assert completed.returncode == 0, completed.stderr
    assert output_path.read_text(encoding="utf-8") == "persisted"
    assert time.monotonic() - started >= 0.2


def test_process_without_pending_work_has_no_guardian_exit_delay(tmp_path: Path) -> None:
    output_path = tmp_path / "done.txt"
    # 导入链（requests/playwright 等下载器家族）的重量与 guardian 无关，
    # 因此子进程把「导入完成」与「脚本收尾」两个时间戳写回文件，
    # 父进程只对服务生命周期窗口和解释器退出窗口设预算：
    # guardian 退出延迟（非 daemon 线程 join、销账重试循环）只会落在这两个窗口里。
    script = """
import sys
import threading
import time
from pathlib import Path
from app.config.settings import ConfigManager
from app.services.frontend_state_service import FrontendStateService

imports_done = time.time()

class ReadyStore:
    def shutdown(self, timeout=1.0):
        return True

store = ReadyStore()
service = FrontendStateService(
    config_manager=ConfigManager(str(Path(sys.argv[1]).with_suffix(".json"))),
    failed_record_store=store,
)
assert service.destroy() is True
assert not any(
    thread.name.startswith("failed-record-drain-")
    for thread in threading.enumerate()
)
Path(sys.argv[1]).write_text(
    "done|%.3f|%.3f" % (imports_done, time.time()), encoding="utf-8"
)
"""

    started = time.time()
    completed = subprocess.run(
        [sys.executable, "-c", script, str(output_path)],
        cwd=Path(__file__).resolve().parents[3],
        check=False,
        capture_output=True,
        text=True,
        timeout=20.0,
    )
    stopped = time.time()

    assert completed.returncode == 0, completed.stderr
    marker, imports_done_raw, script_end_raw = (
        output_path.read_text(encoding="utf-8").split("|")
    )
    assert marker == "done"
    imports_done = float(imports_done_raw)
    script_end = float(script_end_raw)

    # 无待处理工作时：构造 + destroy + 线程检查必须走快路径。
    assert script_end - imports_done < 1.0
    # 自然退出不得被 guardian 线程 join 或销账重试拖住。
    assert stopped - script_end < 1.0
    # 整个子进程仍受超时保护；导入开销单独记录，只做宽松上限。
    assert stopped - started < 15.0
