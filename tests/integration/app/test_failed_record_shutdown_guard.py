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
    script = """
import sys
import threading
from pathlib import Path
from app.config.settings import ConfigManager
from app.services.frontend_state_service import FrontendStateService

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
Path(sys.argv[1]).write_text("done", encoding="utf-8")
"""

    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-c", script, str(output_path)],
        cwd=Path(__file__).resolve().parents[3],
        check=False,
        capture_output=True,
        text=True,
        timeout=5.0,
    )

    assert completed.returncode == 0, completed.stderr
    assert output_path.read_text(encoding="utf-8") == "done"
    assert time.monotonic() - started < 2.0
