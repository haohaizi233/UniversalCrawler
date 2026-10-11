"""UCrawl 的 Python SDK 公共入口。

推荐用法:
    from ucrawl import UcrawlSDK, search, list_platforms
    from ucrawl import RuleSelection, PipeSelection
"""

from shared.sdk_runtime import (
    UcrawlSDK,
    search,
    list_platforms,
    scan_directory,
    download_video,
)
from shared.selection_base import SelectionStrategy
from shared.selection_runtime import (
    RuleSelection,
    AutoSelection,
)
from shared.pipe_selection import PipeSelection
from shared.version import __version__
from ucrawl.tools import ToolsAPI

__all__ = [
    "UcrawlSDK",
    "search",
    "list_platforms",
    "scan_directory",
    "download_video",
    "RuleSelection",
    "PipeSelection",
    "AutoSelection",
    "SelectionStrategy",
    "ToolsAPI",
]
