"""Process-local and cross-process locks for updater download targets."""

from __future__ import annotations

import hashlib
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import Callable, Iterator

from app.services.updater_cleanup import run_cleanup_actions
from app.services.updater_file_lock import exclusive_file_lock


@dataclass(slots=True)
class _LockEntry:
    lock: Lock
    references: int = 0


_REGISTRY_GUARD = Lock()
_TARGET_LOCKS: dict[str, _LockEntry] = {}


@contextmanager
def target_download_lock(
    target: Path,
    *,
    cancel_event: Event | None,
    timeout_seconds: float,
    error_factory: Callable[[str], BaseException],
) -> Iterator[None]:
    """Serialize one target while keeping wait cancellation and timeout bounded."""

    key = os.path.normcase(os.fspath(Path(target).resolve(strict=False)))
    with _REGISTRY_GUARD:
        entry = _TARGET_LOCKS.get(key)
        if entry is None:
            entry = _LockEntry(Lock())
            _TARGET_LOCKS[key] = entry
        entry.references += 1

    acquired = False
    primary_error: BaseException | None = None
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    try:
        while not acquired:
            if cancel_event is not None and cancel_event.is_set():
                raise error_factory("download was cancelled while waiting for the target")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise error_factory("download target is already in progress")
            acquired = entry.lock.acquire(timeout=min(0.05, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise error_factory("download target is already in progress")
        lock_root = Path(target).parent / ".updater-locks"
        lock_name = f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.lock"
        with exclusive_file_lock(
            lock_root / lock_name,
            timeout_seconds=remaining,
            approved_root=lock_root,
            cancel_event=cancel_event,
            error_factory=error_factory,
        ):
            yield
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        def release_entry() -> None:
            if acquired:
                entry.lock.release()

        def unregister_entry() -> None:
            with _REGISTRY_GUARD:
                entry.references -= 1
                if entry.references == 0:
                    _TARGET_LOCKS.pop(key, None)

        run_cleanup_actions(
            primary_error,
            (
                (release_entry, "download target lock release failed"),
                (unregister_entry, "download target lock registry cleanup failed"),
            ),
        )
