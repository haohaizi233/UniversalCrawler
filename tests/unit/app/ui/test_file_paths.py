"""文件对话框起始路径规范化测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.ui.dialogs.file_paths import absolute_export_path, resolve_existing_directory


class FilePathTests(unittest.TestCase):
    def test_missing_start_directory_falls_back_to_existing_parent_or_home(self) -> None:
        home = str(Path.home().resolve())
        self.assertEqual(resolve_existing_directory("", str(Path(home) / "missing-ucp-dir")), home)

        with tempfile.TemporaryDirectory() as directory:
            file_path = Path(directory) / "video.mp4"
            file_path.write_text("probe", encoding="utf-8")
            missing = str(Path(directory) / "missing")
            resolved = resolve_existing_directory(missing, str(file_path))
            self.assertEqual(resolved, str(file_path.parent.resolve()))

    def test_export_dialog_path_is_absolute(self) -> None:
        path = Path(absolute_export_path("log_detail.json"))
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.name, "log_detail.json")
        self.assertTrue(path.parent.is_dir())


if __name__ == "__main__":
    unittest.main()
