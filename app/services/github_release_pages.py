"""Bounded GitHub Releases pagination used by the secure updater."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from app.services.updater_cleanup import run_typed_cleanup_actions

MAX_RELEASE_API_BYTES = 2_000_000
MAX_RELEASE_API_PAGES = 5
MAX_SCANNED_RELEASES = 500
MAX_LINK_HEADER_BYTES = 8_192

T = TypeVar("T")


class GitHubReleasePageError(RuntimeError):
    pass


def collect_release_candidates(
    *,
    opener: Callable[..., Any],
    first_url: str,
    headers: Mapping[str, str],
    timeout: float,
    owner: str,
    repo: str,
    page_size: int,
    validate_url: Callable[[str, set[str]], None],
    candidate_factory: Callable[[Mapping[str, Any], str, str], T | None],
) -> tuple[T, ...]:
    candidates: list[T] = []
    seen_pages: set[str] = set()
    scanned_releases = 0
    url = first_url
    for page_number in range(MAX_RELEASE_API_PAGES):
        if not url:
            break
        if url in seen_pages:
            raise GitHubReleasePageError("GitHub releases pagination contains a cycle")
        seen_pages.add(url)
        page_headers = dict(headers)
        if page_number:
            page_headers.pop("If-None-Match", None)
            page_headers.pop("If-Modified-Since", None)
        request = urllib.request.Request(url, headers=page_headers)
        response: Any | None = None
        response_error: BaseException | None = None
        try:
            response = opener(request, timeout=timeout)
            final_url = response_final_url(response, url)
            _validate_release_page_url(
                final_url,
                owner=owner,
                repo=repo,
                validate_url=validate_url,
            )
            payload = response.read(MAX_RELEASE_API_BYTES + 1)
            if len(payload) > MAX_RELEASE_API_BYTES:
                raise GitHubReleasePageError("GitHub releases response is too large")
            try:
                data = json.loads(payload.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
                raise GitHubReleasePageError(
                    "GitHub releases response was not valid JSON"
                ) from exc
            response_headers = getattr(response, "headers", {})
            next_url = _next_release_page(
                str(response_headers.get("Link") or ""),
                owner=owner,
                repo=repo,
                page_size=page_size,
                validate_url=validate_url,
            )
        except BaseException as exc:
            response_error = exc
            raise
        finally:
            if response is not None:
                run_typed_cleanup_actions(
                    response_error,
                    ((_response_close_action(response), "GitHub releases response cleanup failed"),),
                    error_factory=GitHubReleasePageError,
                    failure_message="GitHub releases response cleanup failed",
                )
        if not isinstance(data, list):
            raise GitHubReleasePageError(
                "GitHub releases response had an unexpected shape"
            )
        for release in data:
            if scanned_releases >= MAX_SCANNED_RELEASES:
                break
            scanned_releases += 1
            if not isinstance(release, Mapping):
                continue
            candidate = candidate_factory(release, "", "")
            if candidate is not None:
                candidates.append(candidate)
            if len(candidates) >= page_size:
                break
        if len(candidates) >= page_size or scanned_releases >= MAX_SCANNED_RELEASES:
            break
        url = next_url
    return tuple(candidates)


def response_final_url(response: Any, fallback: str) -> str:
    getter = getattr(response, "geturl", None)
    return str(getter() if callable(getter) else fallback)


def release_tag_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    match = re.search(r"/releases/tag/([^/?#]+)", parsed.path)
    return urllib.parse.unquote(match.group(1)) if match else ""


def release_tag_from_html(html: str) -> str:
    match = re.search(r'href="[^"]*/releases/tag/([^"/?#]+)', html)
    return urllib.parse.unquote(match.group(1)) if match else ""


def release_html_url(tag_name: str, page_url: str) -> str:
    parsed = urllib.parse.urlparse(page_url)
    parts = parsed.path.strip("/").split("/")
    owner, repo = parts[:2] if len(parts) >= 2 else ("haohaizi554", "UniversalCrawler")
    scheme = parsed.scheme or "https"
    host = parsed.netloc or "github.com"
    return f"{scheme}://{host}/{owner}/{repo}/releases/tag/{urllib.parse.quote(tag_name)}"


def _response_close_action(response: Any) -> Callable[[], object]:
    def close() -> object:
        candidate = getattr(response, "close", None)
        if callable(candidate):
            return candidate()
        return None

    return close


def _validate_release_page_url(
    url: str,
    *,
    owner: str,
    repo: str,
    validate_url: Callable[[str, set[str]], None],
) -> None:
    try:
        validate_url(url, {"api.github.com"})
    except Exception as exc:
        raise GitHubReleasePageError(
            "GitHub releases pagination target is invalid"
        ) from exc
    parsed = urllib.parse.urlsplit(url)
    if parsed.path != f"/repos/{owner}/{repo}/releases" or parsed.fragment:
        raise GitHubReleasePageError("GitHub releases pagination target is invalid")
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if not query or set(query) - {"per_page", "page"}:
        raise GitHubReleasePageError("GitHub releases pagination query is invalid")
    for key, values in query.items():
        if len(values) != 1 or not values[0].isdigit():
            raise GitHubReleasePageError("GitHub releases pagination query is invalid")
        value = int(values[0])
        if value <= 0 or (key == "per_page" and value > 100):
            raise GitHubReleasePageError("GitHub releases pagination query is invalid")


def _next_release_page(
    link_header: str,
    *,
    owner: str,
    repo: str,
    page_size: int,
    validate_url: Callable[[str, set[str]], None],
) -> str:
    if not link_header:
        return ""
    if len(link_header.encode("utf-8")) > MAX_LINK_HEADER_BYTES:
        raise GitHubReleasePageError("GitHub releases Link header is too large")
    for match in re.finditer(r"<([^<>]+)>\s*((?:;\s*[^,]*)*)", link_header):
        if re.search(
            r"(?:^|;)\s*rel\s*=\s*['\"]?next['\"]?(?:\s*;|\s*$)",
            match.group(2),
            re.IGNORECASE,
        ) is None:
            continue
        next_url = match.group(1).strip()
        _validate_release_page_url(
            next_url,
            owner=owner,
            repo=repo,
            validate_url=validate_url,
        )
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(next_url).query,
            keep_blank_values=True,
        )
        if query.get("per_page") != [str(page_size)] or "page" not in query:
            raise GitHubReleasePageError("GitHub releases pagination query is invalid")
        return next_url
    return ""
