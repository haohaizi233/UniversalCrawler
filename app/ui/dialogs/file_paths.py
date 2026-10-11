"""把文件对话框的起始位置收成已存在的目录或绝对路径。"""

from __future__ import annotations

from pathlib import Path


def resolve_existing_directory(*candidates: str) -> str:
    """返回第一个已存在的目录；文件路径取其父目录，否则回退到用户主目录。"""

    for candidate in candidates:
        resolved = _directory_if_exists(candidate)
        if resolved:
            return resolved
    return str(Path.home())


def absolute_export_path(filename: str, *directory_candidates: str) -> str:
    """导出对话框使用绝对路径，目录不存在时回退到已有目录。"""

    name = Path(str(filename or "export.txt")).name or "export.txt"
    directory = resolve_existing_directory(*directory_candidates)
    return str((Path(directory) / name).resolve())


def _directory_if_exists(candidate: str) -> str:
    text = str(candidate or "").strip().strip('"').strip("'")
    if not text:
        return ""
    try:
        path = Path(text).expanduser()
    except (OSError, RuntimeError, ValueError):
        return ""
    try:
        if path.is_file():
            path = path.parent
        if path.is_dir():
            return str(path.resolve())
    except (OSError, RuntimeError, ValueError):
        return ""
    return ""
