from __future__ import annotations

import pytest

from app.services.updater_cleanup import run_cleanup_actions, run_typed_cleanup_actions


def _raising(error: BaseException, calls: list[str], label: str):
    def action() -> None:
        calls.append(label)
        raise error

    return action


def test_cleanup_without_primary_prefers_first_control_flow_and_runs_every_action():
    calls: list[str] = []
    ordinary = ValueError("ordinary cleanup")
    first_control = KeyboardInterrupt("first control cleanup")
    later_control = SystemExit("later control cleanup")
    actions = (
        (_raising(ordinary, calls, "ordinary"), "ordinary note"),
        (lambda: calls.append("success"), "success note"),
        (_raising(first_control, calls, "first-control"), "control note"),
        (_raising(later_control, calls, "later-control"), "later note"),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        run_cleanup_actions(None, actions)

    assert caught.value is first_control
    assert calls == ["ordinary", "success", "first-control", "later-control"]


def test_cleanup_without_primary_preserves_first_ordinary_when_no_control_flow():
    calls: list[str] = []
    first = ValueError("first cleanup")
    second = RuntimeError("second cleanup")

    with pytest.raises(ValueError) as caught:
        run_cleanup_actions(
            None,
            (
                (_raising(first, calls, "first"), "first note"),
                (_raising(second, calls, "second"), "second note"),
            ),
        )

    assert caught.value is first
    assert calls == ["first", "second"]


def test_cleanup_with_primary_runs_all_actions_without_replacing_the_primary():
    calls: list[str] = []
    primary = GeneratorExit("active primary")

    run_cleanup_actions(
        primary,
        (
            (_raising(ValueError("ordinary"), calls, "ordinary"), "ordinary note"),
            (_raising(SystemExit("control"), calls, "control"), "control note"),
        ),
    )

    assert calls == ["ordinary", "control"]
    assert getattr(primary, "__notes__", []) == ["ordinary note", "control note"]


def test_typed_cleanup_normalizes_first_ordinary_failure_after_running_all_actions():
    calls: list[str] = []
    first = ValueError("first cleanup")

    with pytest.raises(OSError, match="normalized cleanup") as caught:
        run_typed_cleanup_actions(
            None,
            (
                (_raising(first, calls, "first"), "first note"),
                (_raising(RuntimeError("second"), calls, "second"), "second note"),
            ),
            error_factory=OSError,
            failure_message="normalized cleanup",
        )

    assert caught.value.__cause__ is first
    assert calls == ["first", "second"]


def test_typed_cleanup_propagates_first_control_flow_over_earlier_ordinary_failure():
    calls: list[str] = []
    control = SystemExit("control cleanup")

    with pytest.raises(SystemExit) as caught:
        run_typed_cleanup_actions(
            None,
            (
                (_raising(ValueError("ordinary"), calls, "ordinary"), "ordinary note"),
                (_raising(control, calls, "control"), "control note"),
                (lambda: calls.append("success"), "success note"),
            ),
            error_factory=OSError,
            failure_message="must not normalize control flow",
        )

    assert caught.value is control
    assert calls == ["ordinary", "control", "success"]


def test_typed_cleanup_with_primary_never_raises_a_cleanup_failure():
    calls: list[str] = []
    primary = KeyboardInterrupt("active primary")

    run_typed_cleanup_actions(
        primary,
        (
            (_raising(RuntimeError("ordinary"), calls, "ordinary"), "ordinary note"),
            (_raising(SystemExit("control"), calls, "control"), "control note"),
        ),
        error_factory=OSError,
        failure_message="unused",
    )

    assert calls == ["ordinary", "control"]
    assert getattr(primary, "__notes__", []) == ["ordinary note", "control note"]
