"""Trusted host orchestration for admitting parsed link candidates."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from app.debug_logger import debug_logger
from app.models import VideoItem
from app.services.frontend_action_result import FrontendActionResult
from app.services.link_candidate_admission_service import (
    LinkCandidateAdmissionError,
    LinkCandidateAdmissionService,
)
from shared.execution_profile import ExecutionProfile


_ADMISSION_KEYS = frozenset({"tool_id", "run_id", "candidate_ids"})


class _BatchDownloadManager(Protocol):
    def add_tasks(self, videos: Sequence[VideoItem], save_dir: str) -> int: ...

    def cancel_videos_and_wait(
        self,
        videos: Sequence[VideoItem],
        timeout_ms: int | None = None,
    ) -> Mapping[str, str | None]: ...


class _FrontendAdmissionHost(Protocol):
    controller: Any
    app_state: Any
    tool_runner_service: Any
    _link_candidate_admission_coordinator: Any

    def _current_save_dir(self) -> str: ...

    def _emit_frontend_event(self, topic: str, payload: Mapping[str, Any]) -> None: ...


class _FrontendHostBindings:
    """Keep GUI/Web host storage details out of the state-service facade."""

    def __init__(self, host: _FrontendAdmissionHost) -> None:
        self._host = host

    def download_manager(self) -> _BatchDownloadManager | None:
        controller = self._host.controller
        return getattr(controller, "dl_manager", None) if controller is not None else None

    def save_directory(self) -> str:
        return self._host._current_save_dir()

    def store_items(self, items: Sequence[VideoItem]) -> None:
        batch = tuple(items)
        controller = self._host.controller
        store_many = getattr(controller, "_store_video_items", None)
        if callable(store_many):
            store_many(list(batch))
            return
        self._host.app_state.upsert_videos(batch)

    def remove_items_if_matches(self, items: Sequence[VideoItem]) -> None:
        expected = {str(item.id): item for item in items if getattr(item, "id", None)}
        if not expected:
            return
        controller = self._host.controller
        controller_state = getattr(controller, "app_state", None)
        remove_many = getattr(controller_state, "remove_videos_if_matches", None)
        if callable(remove_many):
            remove_many(expected)
            return
        videos = getattr(controller, "videos", None)
        state_guard = getattr(controller, "_video_state_guard", None)
        if isinstance(videos, dict) and callable(state_guard):
            with state_guard():
                for video_id, expected_item in expected.items():
                    if videos.get(video_id) is expected_item:
                        videos.pop(video_id, None)
            return
        self._host.app_state.remove_videos_if_matches(expected)


class _HostAwareDownloadManager:
    """Make host projection storage part of the queue admission transaction."""

    def __init__(
        self,
        manager: _BatchDownloadManager,
        *,
        store_items: Callable[[Sequence[VideoItem]], None],
        remove_items_if_matches: Callable[[Sequence[VideoItem]], None],
    ) -> None:
        self._manager = manager
        self._store_items = store_items
        self._remove_items_if_matches = remove_items_if_matches
        self._stored_batch: tuple[VideoItem, ...] = ()
        self._manager_submission_started = False

    def add_tasks(self, videos: Sequence[VideoItem], save_dir: str) -> int:
        batch = tuple(videos)
        self._stored_batch = batch
        try:
            self._store_items(batch)
        except BaseException as primary:
            try:
                self._remove_items_if_matches(batch)
                self._stored_batch = ()
            except BaseException:
                pass
            raise primary
        self._manager_submission_started = True
        try:
            return self._manager.add_tasks(batch, save_dir)
        except BaseException as primary:
            try:
                self.cancel_videos_and_wait(batch)
            except BaseException:
                pass
            raise primary

    def cancel_videos_and_wait(
        self,
        videos: Sequence[VideoItem],
        timeout_ms: int | None = None,
    ) -> Mapping[str, str | None]:
        batch = tuple(videos)
        if self._manager_submission_started:
            result = self._manager.cancel_videos_and_wait(
                batch,
                timeout_ms=timeout_ms,
            )
        else:
            result = {item.id: "cancelled" for item in batch}
        if (
            isinstance(result, Mapping)
            and set(result) == {item.id for item in batch}
            and all(result.get(item.id) == "cancelled" for item in batch)
        ):
            self._manager_submission_started = False
            self._remove_items_if_matches(batch)
            self._stored_batch = ()
        return result


class FrontendLinkCandidateAdmissionCoordinator:
    """Bind an owner-scoped parser result to trusted host queue resources."""

    def __init__(
        self,
        *,
        tool_runner_service: Any,
        download_manager_provider: Callable[[], _BatchDownloadManager | None],
        save_directory_provider: Callable[[], str],
        store_items: Callable[[Sequence[VideoItem]], None],
        remove_items_if_matches: Callable[[Sequence[VideoItem]], None],
    ) -> None:
        self._admission_service = LinkCandidateAdmissionService(tool_runner_service)
        self._download_manager_provider = download_manager_provider
        self._save_directory_provider = save_directory_provider
        self._store_items = store_items
        self._remove_items_if_matches = remove_items_if_matches

    def admit(
        self,
        payload: Mapping[str, Any],
        *,
        execution_profile: ExecutionProfile,
    ) -> dict[str, Any]:
        self._validate_request_shape(payload)
        try:
            manager = self._download_manager_provider()
        except Exception:
            manager = None
        if manager is None:
            raise LinkCandidateAdmissionError(
                "download_queue_unavailable",
                "download queue is unavailable",
            )

        trusted_manager = _HostAwareDownloadManager(
            manager,
            store_items=self._store_items,
            remove_items_if_matches=self._remove_items_if_matches,
        )
        try:
            save_directory = self._save_directory_provider()
        except Exception:
            raise LinkCandidateAdmissionError(
                "save_directory_unavailable",
                "download directory is unavailable",
            ) from None

        items = self._admission_service.admit_to_queue(
            payload["tool_id"],
            payload["run_id"],
            payload["candidate_ids"],
            execution_profile=execution_profile,
            download_manager=trusted_manager,
            save_directory=save_directory,
        )
        return {
            "status": "queued",
            "tool_id": "link_parser",
            "run_id": str(payload["run_id"]).strip(),
            "accepted_count": len(items),
            "candidate_ids": [str(item.meta["link_candidate_id"]) for item in items],
            "video_ids": [str(item.id) for item in items],
        }

    @staticmethod
    def _validate_request_shape(payload: Mapping[str, Any]) -> None:
        try:
            keys = frozenset(payload.keys())
        except Exception:
            keys = frozenset()
        if keys != _ADMISSION_KEYS:
            raise LinkCandidateAdmissionError(
                "invalid_admission_request",
                "link admission request is invalid",
            )


def handle_frontend_link_candidate_admission(
    host: _FrontendAdmissionHost,
    payload: Mapping[str, Any],
    *,
    execution_profile: ExecutionProfile,
) -> FrontendActionResult:
    """Execute one trusted admission action without widening the state facade."""

    coordinator = host._link_candidate_admission_coordinator
    if coordinator is None:
        bindings = _FrontendHostBindings(host)
        coordinator = FrontendLinkCandidateAdmissionCoordinator(
            tool_runner_service=host.tool_runner_service,
            download_manager_provider=bindings.download_manager,
            save_directory_provider=bindings.save_directory,
            store_items=bindings.store_items,
            remove_items_if_matches=bindings.remove_items_if_matches,
        )
        host._link_candidate_admission_coordinator = coordinator
    try:
        receipt = coordinator.admit(payload, execution_profile=execution_profile)
    except LinkCandidateAdmissionError as exc:
        return FrontendActionResult("error", str(exc), {"code": exc.code})
    except Exception as exc:
        _log_exception_best_effort("tool_admit_candidates", exc)
        return FrontendActionResult(
            "error",
            "selected links could not be queued",
            {"code": "link_admission_failed"},
        )
    try:
        host._emit_frontend_event(
            "tool.candidates_admitted",
            {
                "tool_id": receipt["tool_id"],
                "run_id": receipt["run_id"],
                "accepted_count": receipt["accepted_count"],
                "video_ids": list(receipt["video_ids"]),
            },
        )
    except BaseException as exc:
        _log_exception_best_effort("tool_candidates_admitted_event", exc)
    return FrontendActionResult("ok", "selected links were queued", dict(receipt))


def _log_exception_best_effort(action: str, exc: BaseException) -> None:
    try:
        debug_logger.log_exception("FrontendStateService", action, exc)
    except BaseException:
        pass


__all__ = [
    "FrontendLinkCandidateAdmissionCoordinator",
    "handle_frontend_link_candidate_admission",
]
