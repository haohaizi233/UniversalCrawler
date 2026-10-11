"""Canonical download-admission capability for parsed link candidates."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


_SUPPORTED_DOWNLOAD_SOURCES = frozenset(
    {"bilibili", "douyin", "kuaishou", "missav", "xiaohongshu"}
)
_DIRECT_VIDEO_EXTENSIONS = frozenset(
    {".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".ts", ".webm"}
)


@dataclass(frozen=True, slots=True)
class LinkCandidateAdmissionCapability:
    """Public, non-sensitive explanation of whether a candidate is admissible."""

    admittable: bool
    reason_code: str


def link_candidate_admission_capability(
    url: str,
    *,
    platform: str,
    resource_kind: str,
) -> LinkCandidateAdmissionCapability:
    """Return the single server-owned admission decision for one canonical URL."""

    if resource_kind == "page":
        return LinkCandidateAdmissionCapability(
            False,
            "candidate_requires_crawl_resolution",
        )
    if platform not in _SUPPORTED_DOWNLOAD_SOURCES:
        return LinkCandidateAdmissionCapability(
            False,
            "candidate_downloader_unavailable",
        )
    if resource_kind == "playlist":
        return LinkCandidateAdmissionCapability(
            platform == "missav",
            "" if platform == "missav" else "candidate_downloader_unavailable",
        )
    if resource_kind == "media":
        path = urlsplit(url).path.lower()
        supported = any(path.endswith(suffix) for suffix in _DIRECT_VIDEO_EXTENSIONS)
        return LinkCandidateAdmissionCapability(
            supported,
            "" if supported else "candidate_media_type_unsupported",
        )
    return LinkCandidateAdmissionCapability(False, "candidate_integrity_error")


__all__ = [
    "LinkCandidateAdmissionCapability",
    "link_candidate_admission_capability",
]
