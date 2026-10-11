"""Pure candidate projection shared by the update dialog constructor."""

from __future__ import annotations

from typing import Any


def normalize_update_candidates(
    candidates: tuple[Any, ...],
    *,
    latest_version: str,
    release_url: str,
    selected_candidate_id: str = "",
) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for candidate in candidates or ():
        version = str(_candidate_value(candidate, "version") or "").strip()
        if not version:
            continue
        revision = int(_candidate_value(candidate, "release_revision") or 0)
        tag = str(_candidate_value(candidate, "tag_name") or "").strip()
        candidate_id = str(_candidate_value(candidate, "candidate_id") or tag).strip()
        options.append(
            {
                "version": version,
                "revision": revision,
                "candidate_id": candidate_id,
                "tag": tag,
                "name": str(_candidate_value(candidate, "release_name") or ""),
                "release_url": str(_candidate_value(candidate, "html_url") or release_url or ""),
                "notes": str(_candidate_value(candidate, "notes") or ""),
                "asset": str(_candidate_value(candidate, "asset_name") or ""),
            }
        )
    selected_id = str(selected_candidate_id or "").strip()
    selection_hint = str(latest_version or "").strip()
    if not selected_id and selection_hint and options:
        exact_matches = [
            option
            for option in options
            if selection_hint in {option["candidate_id"], option["tag"]}
        ]
        version_matches = [
            option for option in options if option["version"] == selection_hint
        ]
        inferred = exact_matches or version_matches
        if len(inferred) == 1:
            selected_id = str(inferred[0]["candidate_id"])
        elif len(inferred) > 1:
            raise ValueError(
                f"selected update candidate is ambiguous: {selection_hint}"
            )
    if selected_id:
        for index, option in enumerate(options):
            if option["candidate_id"] == selected_id:
                options.insert(0, options.pop(index))
                break
        else:
            raise ValueError(f"selected update candidate is unavailable: {selected_id}")
    elif len(options) == 1:
        selected_version = str(latest_version or "").strip()
        if selected_version and options[0]["version"] != selected_version:
            raise ValueError("selected update version does not match the only candidate")
    if not options and latest_version:
        options.append(
            {
                "version": str(latest_version),
                "revision": 0,
                "candidate_id": str(latest_version),
                "tag": str(latest_version),
                "name": str(latest_version),
                "release_url": str(release_url or ""),
                "notes": "",
                "asset": "",
            }
        )
    return options


def _candidate_value(candidate: Any, key: str) -> Any:
    if isinstance(candidate, dict):
        return candidate.get(key)
    return getattr(candidate, key, "")
