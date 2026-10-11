"""Best-effort updater cleanup that never replaces an active primary failure."""

from __future__ import annotations

from collections.abc import Callable, Iterable

CleanupAction = tuple[Callable[[], object], str]
CleanupErrorFactory = Callable[[str], BaseException]


def run_cleanup_actions(
    primary_error: BaseException | None,
    actions: Iterable[CleanupAction],
) -> None:
    """Run every cleanup and preserve the exact active exception, if any."""

    first_ordinary_error: BaseException | None = None
    first_control_error: BaseException | None = None
    for cleanup, note in actions:
        try:
            cleanup()
        except BaseException as cleanup_error:
            if primary_error is not None:
                _attach_note_best_effort(primary_error, note)
            elif not isinstance(cleanup_error, Exception):
                if first_control_error is None:
                    first_control_error = cleanup_error
            elif first_ordinary_error is None:
                first_ordinary_error = cleanup_error
    if primary_error is None:
        if first_control_error is not None:
            raise first_control_error
        if first_ordinary_error is not None:
            raise first_ordinary_error


def run_typed_cleanup_actions(
    primary_error: BaseException | None,
    actions: Iterable[CleanupAction],
    *,
    error_factory: CleanupErrorFactory,
    failure_message: str,
) -> None:
    """Normalize ordinary cleanup-only failures while preserving control flow."""

    try:
        run_cleanup_actions(primary_error, actions)
    except BaseException as exc:
        if primary_error is not None or not isinstance(exc, Exception):
            raise
        raise error_factory(failure_message) from exc


def _attach_note_best_effort(primary_error: BaseException, note: str) -> None:
    try:
        add_note = object.__getattribute__(primary_error, "add_note")
    except BaseException:
        return
    if not callable(add_note):
        return
    try:
        add_note(note)
    except BaseException:
        return
