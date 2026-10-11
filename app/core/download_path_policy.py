"""分发器与下载线程共用的纯路径策略。"""

from __future__ import annotations

import os
from pathlib import Path

from app.models import VideoItem
from app.utils import sanitize_filename

# 启动清理列举目录时的硬上限。超过后视为截断，不再把该目录交给无上限的 iterdir。
STARTUP_DIRECTORY_ENTRY_LIMIT = 50_000


def is_drive_or_filesystem_root(path: str | os.PathLike[str]) -> bool:
    """盘符根、文件系统根不能作为启动清理的扫描起点。"""
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    anchor = resolved.anchor
    if not anchor:
        return False
    return os.path.normcase(str(resolved)) == os.path.normcase(anchor)


def is_within_save_root(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """路径解析后仍落在保存根之内；盘符根和跨盘路径一律拒绝。"""
    try:
        child = os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))
        parent = os.path.normcase(str(Path(root).expanduser().resolve(strict=False)))
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    if is_drive_or_filesystem_root(child):
        return False
    try:
        return os.path.commonpath((child, parent)) == parent
    except (OSError, ValueError):
        return False


def list_directory_entries(
    directory: str | os.PathLike[str],
    *,
    limit: int | None = None,
) -> tuple[list[str], bool, str]:
    """有上限地 iterdir。盘符根不列举；超过上限时 truncated=True。"""
    entry_limit = STARTUP_DIRECTORY_ENTRY_LIMIT if limit is None else int(limit)
    if entry_limit < 1:
        entry_limit = 1
    if is_drive_or_filesystem_root(directory):
        return [], False, "drive-root"
    try:
        current = Path(directory).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        return [], False, str(exc)
    try:
        if not current.exists() or not current.is_dir():
            return [], False, ""
    except OSError as exc:
        return [], False, str(exc)

    names: list[str] = []
    try:
        for child in current.iterdir():
            if len(names) >= entry_limit:
                return names, True, ""
            names.append(child.name)
    except OSError as exc:
        return names, False, str(exc)
    return names, False, ""


def resolve_task_save_directory(video: VideoItem, save_directory: str) -> str:
    """只计算任务目标目录，不在此阶段创建或修改文件系统。"""
    meta = video.meta if isinstance(getattr(video, "meta", None), dict) else {}
    content_type = str(meta.get("content_type") or "")
    raw_folder_name = str(meta.get("folder_name") or "").strip()
    folder_name = sanitize_filename(raw_folder_name) if raw_folder_name else ""
    uses_subdirectory = bool(
        meta.get("is_gallery")
        or content_type == "gallery"
        or meta.get("is_mix")
        or meta.get("use_subdir")
    )
    if folder_name and uses_subdirectory:
        return os.path.join(str(save_directory), folder_name)
    return str(save_directory)
