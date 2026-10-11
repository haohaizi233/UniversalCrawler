"""小红书共享解析辅助函数。"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.exceptions import SpiderParseError
from app.utils.filenames import sanitize_filename

NOTE_CARD_KEYS = ("note_card", "noteCard")
NOTE_INFO_KEYS = ("note_info", "noteInfo")
USER_KEYS = ("user", "user_info", "userInfo", "author")
AUTHOR_NAME_KEYS = ("nickname", "nickName", "nick_name", "name")
USER_ID_KEYS = ("user_id", "userId", "userid", "id")
RED_ID_KEYS = ("red_id", "redId", "xhs_id", "display_id", "redid")
TITLE_KEYS = ("display_title", "displayTitle", "title")
NOTE_TYPE_KEYS = ("type", "note_type", "noteType")
XSEC_TOKEN_KEYS = ("xsec_token", "xsecToken")
XSEC_SOURCE_KEYS = ("xsec_source", "xsecSource")
IMAGE_LIST_KEYS = ("image_list", "imageList")
INFO_LIST_KEYS = ("info_list", "infoList")
IMAGE_SCENE_KEYS = ("image_scene", "imageScene")
IMAGE_URL_KEYS = ("url_default", "urlDefault", "url")
VIDEO_URL_KEYS = ("master_url", "masterUrl", "backup_url", "backupUrl", "backup_urls", "backupUrls")
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".avif")
_INITIAL_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__=(\{.*?\})\s*;?\s*$",
    re.S,
)

def _base36_encode(number: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if number == 0:
        return "0"
    sign = ""
    if number < 0:
        sign = "-"
        number = -number
    base36 = ""
    while number:
        number, idx = divmod(number, len(alphabet))
        base36 = alphabet[idx] + base36
    return sign + base36

def build_search_id() -> str:
    epoch_part = int(time.time() * 1000) << 64
    random_part = int(random.uniform(0, 2147483646))
    return _base36_encode(epoch_part + random_part)

@dataclass(slots=True)
class NoteUrlInfo:
    note_id: str
    xsec_token: str
    xsec_source: str

@dataclass(slots=True)
class CreatorUrlInfo:
    user_id: str
    xsec_token: str
    xsec_source: str
    nickname: str = ""
    red_id: str = ""
    note_hint: str = ""

@dataclass(slots=True)
class CreatorLookupInfo:
    keyword: str

def extract_url_params_to_dict(url: str) -> dict[str, str]:
    """把查询参数解析成单值字典。"""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    return {key: values[-1] for key, values in params.items() if values}

def extract_first_url(raw_text: str) -> str:
    """从复制的分享文案提取首个 URL；没有 URL 时返回清理后的原文。"""
    raw = str(raw_text or "").strip()
    match = re.search(r"https?://[^\s`，。！？；;,)）\]'\"]+", raw)
    candidate = match.group(0) if match else raw
    return candidate.rstrip("，。！？；;,.!?)）]}'\"")

def is_note_url(text: str) -> bool:
    lowered = text.lower()
    return "xiaohongshu.com/explore/" in lowered or "xiaohongshu.com/discovery/item/" in lowered

def is_creator_url(text: str) -> bool:
    return "xiaohongshu.com/user/profile/" in text.lower()

def parse_creator_lookup_input(text: str) -> CreatorLookupInfo | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    prefixes = ("小红书号:", "小红书号：", "账号:", "账号：", "user:", "uid:", "redid:")
    lowered = raw.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix.lower()):
            keyword = raw[len(prefix):].strip().lstrip("@")
            return CreatorLookupInfo(keyword=keyword) if keyword else None
    if raw.startswith("@") and len(raw) > 1:
        return CreatorLookupInfo(keyword=raw[1:].strip())
    if raw.isdigit() and len(raw) >= 6:
        return CreatorLookupInfo(keyword=raw)
    return None

def parse_note_info_from_note_url(url: str) -> NoteUrlInfo:
    """从小红书笔记 URL 解析笔记 ID 和 xsec 参数。"""
    parsed = urlparse(url.strip())
    note_id = parsed.path.rstrip("/").split("/")[-1]
    params = extract_url_params_to_dict(url)
    return NoteUrlInfo(
        note_id=note_id,
        xsec_token=params.get("xsec_token", ""),
        xsec_source=params.get("xsec_source", ""),
    )

def parse_creator_info_from_url(url: str) -> CreatorUrlInfo:
    """从小红书作者 URL 或原始用户 ID 解析作者与 xsec 参数。"""
    raw = url.strip()
    if len(raw) == 24 and all(ch in "0123456789abcdef" for ch in raw.lower()):
        return CreatorUrlInfo(user_id=raw, xsec_token="", xsec_source="")

    match = re.search(r"/user/profile/([^/?]+)", raw)
    if not match:
        raise ValueError(f"无法从链接中解析小红书作者信息: {url}")
    params = extract_url_params_to_dict(url)
    return CreatorUrlInfo(
        user_id=match.group(1),
        xsec_token=params.get("xsec_token", ""),
        xsec_source=params.get("xsec_source", ""),
    )

def _replace_json_undefined_literals(text: str) -> str:
    """只把 JSON 里的 undefined 字面量换成 null，不改字符串内容。"""
    result: list[str] = []
    in_string = False
    escape = False
    index = 0
    length = len(text)
    token = "undefined"
    while index < length:
        char = text[index]
        if in_string:
            result.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if text.startswith(token, index):
            previous = text[index - 1] if index else ""
            following = text[index + len(token)] if index + len(token) < length else ""
            previous_ok = not previous or not (previous.isalnum() or previous == "_")
            following_ok = not following or not (following.isalnum() or following == "_")
            if previous_ok and following_ok:
                result.append("null")
                index += len(token)
                continue
        result.append(char)
        index += 1
    return "".join(result)


def extract_note_detail_from_html(note_id: str, html: str) -> dict[str, Any] | None:
    """从小红书 HTML 初始状态中提取笔记详情。"""
    marker = "window.__INITIAL_STATE__="
    start = html.find(marker)
    if start < 0 or "noteDetailMap" not in html:
        return None
    segment = html[start:]
    script_end = segment.find("</script>")
    if script_end >= 0:
        segment = segment[:script_end]
    match = _INITIAL_STATE_RE.search(segment)
    if not match:
        raise SpiderParseError("小红书 HTML 初始状态解析失败")
    state_text = _replace_json_undefined_literals(match.group(1))
    try:
        state = json.loads(state_text, strict=False)
    except json.JSONDecodeError as exc:
        raise SpiderParseError("小红书 HTML 初始状态解析失败") from exc
    if not isinstance(state, dict):
        raise SpiderParseError("小红书 HTML 初始状态解析失败")
    note_container = state.get("note", {})
    if not isinstance(note_container, dict):
        return None
    detail_map = note_container.get("noteDetailMap", {})
    if not isinstance(detail_map, dict):
        return None
    note_entry = detail_map.get(note_id, {})
    if isinstance(note_entry, dict):
        note = note_entry.get("note")
        return note if isinstance(note, dict) else None
    return None


def _dict_layer(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _mapping_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        text = str(payload.get(key) or "").strip()
        if text:
            return text
    return ""


def embedded_note_user(entry: dict[str, Any]) -> dict[str, Any]:
    """按搜索和作者列表共用的层级取出用户对象。"""
    if not isinstance(entry, dict):
        return {}
    fragments = (
        _dict_layer(entry, *NOTE_CARD_KEYS),
        _dict_layer(entry, *NOTE_INFO_KEYS),
        _dict_layer(entry, "note"),
        entry,
    )
    for fragment in fragments:
        for key in USER_KEYS:
            value = fragment.get(key)
            if isinstance(value, dict) and value:
                return value
    return {}


def note_display_title(entry: dict[str, Any]) -> str:
    """搜索候选和作者候选共用的标题字段。"""
    if not isinstance(entry, dict):
        return ""
    note_card = _dict_layer(entry, *NOTE_CARD_KEYS)
    note_info = _dict_layer(entry, *NOTE_INFO_KEYS)
    note = _dict_layer(entry, "note")
    return (
        _mapping_text(entry, *TITLE_KEYS)
        or _mapping_text(note_card, *TITLE_KEYS)
        or _mapping_text(note_info, *TITLE_KEYS)
        or _mapping_text(note, *TITLE_KEYS)
        or _mapping_text(entry, "desc")
        or _mapping_text(note_card, "desc")
        or _mapping_text(note_info, "desc")
        or _mapping_text(note, "desc")
    )


def extract_note_ref(
    entry: dict[str, Any],
    *,
    default_source: str = "",
    default_token: str = "",
    default_author: str = "",
    fallback_source: str = "pc_search",
) -> dict[str, str] | None:
    """用同一套字段从搜索结果或作者列表提取笔记候选。"""
    if not isinstance(entry, dict):
        return None
    if entry.get("model_type") in {"rec_query", "hot_query"}:
        return None
    note_card = _dict_layer(entry, *NOTE_CARD_KEYS)
    note_info = _dict_layer(entry, *NOTE_INFO_KEYS)
    note = _dict_layer(entry, "note")
    note_id = (
        _mapping_text(entry, "id", "note_id", "noteId")
        or _mapping_text(note_card, "note_id", "noteId", "id")
        or _mapping_text(note_info, "note_id", "noteId", "id")
        or _mapping_text(note, "note_id", "noteId", "id")
    )
    if not note_id:
        return None
    user = embedded_note_user(entry)
    title = note_display_title(entry)
    return {
        "note_id": note_id,
        "xsec_source": (
            _mapping_text(entry, *XSEC_SOURCE_KEYS)
            or _mapping_text(note_card, *XSEC_SOURCE_KEYS)
            or _mapping_text(note_info, *XSEC_SOURCE_KEYS)
            or _mapping_text(note, *XSEC_SOURCE_KEYS)
            or str(default_source or "").strip()
            or fallback_source
        ),
        "xsec_token": (
            _mapping_text(entry, *XSEC_TOKEN_KEYS)
            or _mapping_text(note_card, *XSEC_TOKEN_KEYS)
            or _mapping_text(note_info, *XSEC_TOKEN_KEYS)
            or _mapping_text(note, *XSEC_TOKEN_KEYS)
            or str(default_token or "").strip()
        ),
        "title": title,
        "author": _mapping_text(user, *AUTHOR_NAME_KEYS) or str(default_author or "").strip(),
        "note_type": (
            _mapping_text(entry, *NOTE_TYPE_KEYS)
            or _mapping_text(note_card, *NOTE_TYPE_KEYS)
            or _mapping_text(note_info, *NOTE_TYPE_KEYS)
            or _mapping_text(note, *NOTE_TYPE_KEYS)
        ),
    }

def sanitize_note_title(note: dict[str, Any]) -> str:
    """生成适合 UI 选择列表和文件命名的稳定标题。"""
    title = str(note.get("title") or "").strip()
    if title:
        if sanitize_filename(title) != "untitled":
            return title[:60]
    desc = str(note.get("desc") or "").strip()
    if desc:
        if sanitize_filename(desc) != "untitled":
            return desc[:60]
    note_id = str(note.get("note_id") or note.get("noteId") or "xiaohongshu-note")
    return note_id

def note_author_name(note: dict[str, Any]) -> str:
    """提取作者昵称，缺失时使用兜底名称。"""
    user = embedded_note_user(note) or _dict_layer(note, *USER_KEYS)
    return _mapping_text(user, *AUTHOR_NAME_KEYS) or "未知作者"


def _is_probably_image_url(url: str) -> bool:
    path = url.split("?", 1)[0].lower()
    return path.endswith(_IMAGE_SUFFIXES)

def extract_video_candidates(note: dict[str, Any]) -> list[str]:
    """从小红书笔记详情提取视频流候选 URL。"""
    if str(note.get("type", "")).lower() != "video":
        return []
    video_dict = note.get("video") or {}
    consumer = video_dict.get("consumer") or {}
    origin_video_key = consumer.get("origin_video_key") or consumer.get("originVideoKey") or ""
    media = video_dict.get("media") or {}
    stream = media.get("stream") or {}
    candidates: list[str] = []
    for codec in ("h264", "h265", "av1"):
        values = stream.get(codec) or []
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            for key in VIDEO_URL_KEYS:
                url = item.get(key)
                if isinstance(url, list):
                    candidates.extend(str(entry) for entry in url if entry)
                elif url:
                    candidates.append(str(url))
    if origin_video_key:
        candidates.append(f"http://sns-video-bd.xhscdn.com/{origin_video_key}")
    deduped: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        if _is_probably_image_url(url) or url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    def _candidate_score(url: str) -> tuple[int, int, int, int, int]:
        lowered = url.lower()
        bak_rank = 0 if "sns-bak" in lowered else 1
        no_watermark_rank = 0 if "_114." in lowered else 1
        medium_rank = 0 if "_115." in lowered or "_130." in lowered or "_108." in lowered else 1
        origin_penalty = 1 if "sns-video-bd" in lowered else 0
        watermark_penalty = 1 if "_259." in lowered else 0
        return (bak_rank, no_watermark_rank, medium_rank, origin_penalty, watermark_penalty)
    deduped.sort(key=_candidate_score)
    return deduped

def _preferred_image_url(image: dict[str, Any]) -> str:
    info_list = None
    for key in INFO_LIST_KEYS:
        value = image.get(key)
        if isinstance(value, list):
            info_list = value
            break
    if isinstance(info_list, list):
        for info in info_list:
            if not isinstance(info, dict):
                continue
            scene = _mapping_text(info, *IMAGE_SCENE_KEYS)
            if scene != "WB_DFT":
                continue
            url = _mapping_text(info, "url")
            if url:
                return url
    return _mapping_text(image, *IMAGE_URL_KEYS)


def extract_image_entries(note: dict[str, Any]) -> list[dict[str, str]]:
    """从小红书笔记详情提取图片 URL，优先使用 WB_DFT。"""
    result: list[dict[str, str]] = []
    images: list[Any] = []
    for key in IMAGE_LIST_KEYS:
        value = note.get(key)
        if isinstance(value, list):
            images = value
            break
    for image in images:
        if not isinstance(image, dict):
            continue
        image_url = _preferred_image_url(image)
        if image_url:
            result.append({"image_url": image_url})
    return result


def note_has_downloadable_media(note: dict[str, Any]) -> bool:
    """视频只认可播放地址；图文至少要有一张图片 URL。"""
    note_type = str(note.get("type") or note.get("note_type") or note.get("noteType") or "").strip().lower()
    videos = [
        url
        for url in (str(item).strip() for item in (note.get("video_candidates") or []))
        if url and not _is_probably_image_url(url)
    ]
    if note_type == "video":
        return bool(videos)
    if videos:
        return True
    images = note.get("images_data") or []
    if not isinstance(images, list):
        return False
    return any(isinstance(item, dict) and str(item.get("image_url") or "").strip() for item in images)

def build_note_summary(note: dict[str, Any]) -> str:
    """为选择界面生成简洁的笔记摘要。"""
    title = sanitize_note_title(note)
    author = note_author_name(note)
    note_type = str(note.get("type") or "normal")
    image_count = len(extract_image_entries(note))
    if note_type == "video":
        media_hint = "视频"
    elif image_count:
        media_hint = f"图文 {image_count} 张"
    else:
        media_hint = "笔记"
    return f"{title} | {author} | {media_hint}"
