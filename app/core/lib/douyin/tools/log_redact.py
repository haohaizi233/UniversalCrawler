"""去掉日志文本里的 msToken，避免失败记录带出查询串凭据。"""

import re
from urllib.parse import unquote

_URL_IN_TEXT = re.compile(r"https?://[^\s'\"<>]+")


def strip_ms_token_query(value: str | None) -> str:
    """去掉查询串中的 msToken，保留其余参数。"""
    if not isinstance(value, str) or not value:
        return ""
    prefix, separator, query = value.partition("?")
    target = query if separator else value
    kept: list[str] = []
    for piece in target.split("&"):
        if not piece:
            continue
        name = unquote(piece.split("=", 1)[0])
        if name == "msToken":
            continue
        kept.append(piece)
    if not separator:
        return "&".join(kept)
    if not kept:
        return prefix
    return f"{prefix}?{'&'.join(kept)}"


def redact_logged_text(value: object) -> str:
    """去掉日志文本中 URL 查询串里的 msToken。"""
    text = "" if value is None else str(value)
    return _URL_IN_TEXT.sub(lambda match: strip_ms_token_query(match.group(0)), text)
