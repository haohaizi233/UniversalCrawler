"""测试包初始化，以及非 pytest 入口的进程级数据隔离。"""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory


# 进程级隔离目录放在仓库 tmp/。系统临时目录（例如 C:\Windows\Temp）的上级
# 目录无法取得目录能力，导入配置时会整批失败。
_PROCESS_TEMP_ROOT = Path(__file__).resolve().parents[1] / "tmp" / "ucrawl-test-runtime"
_PROCESS_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
_PROCESS_TEST_DIRECTORY = TemporaryDirectory(
    prefix="ucrawl-test-runtime-",
    dir=_PROCESS_TEMP_ROOT,
    ignore_cleanup_errors=True,
)
TEST_USER_DATA_ROOT = Path(_PROCESS_TEST_DIRECTORY.name) / "user_data"
TEST_USER_DATA_ROOT.mkdir(parents=True, exist_ok=True)

if not os.environ.get("UCRAWL_USER_DATA_ROOT", "").strip():
    os.environ["UCRAWL_USER_DATA_ROOT"] = str(TEST_USER_DATA_ROOT)
