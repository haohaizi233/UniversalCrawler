"""Cross-process read-modify-write transactions for updater state."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, TypeVar

from app.services.updater_file_lock import exclusive_file_lock

StateT = TypeVar("StateT")


def mutate_update_state(
    path: Path,
    *,
    load: Callable[[Path], StateT],
    save: Callable[[StateT, Path], None],
    mutate: Callable[[StateT], None],
) -> StateT:
    state_path = Path(path)
    lock_path = state_path.with_name(f".{state_path.name}.lock")
    with exclusive_file_lock(lock_path):
        state = load(state_path)
        mutate(state)
        save(state, state_path)
        return state
