from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from app.config.settings import ConfigManager
from app.core.tools.link_candidate_admission_policy import (
    link_candidate_admission_capability,
)
from app.services.frontend_state_service import FrontendStateService
from app.services.link_candidate_admission_service import LinkCandidateAdmissionError
from app.services.tool_runner_service import PrivateToolResult
from shared.execution_profile import ExecutionProfile, local_execution_profile


class _Runner:
    def __init__(self, result: PrivateToolResult) -> None:
        self.result = result
        self.lookup_calls: list[tuple[str, ExecutionProfile]] = []
        self.claimed: set[tuple[str, str]] = set()

    def lookup_private_result(
        self,
        run_id: str,
        *,
        execution_profile: ExecutionProfile,
    ) -> PrivateToolResult | None:
        self.lookup_calls.append((run_id, execution_profile))
        return self.result

    def _claim_private_candidates(
        self,
        run_id: str,
        candidate_ids: Sequence[str],
        *,
        execution_profile: ExecutionProfile,
    ) -> bool:
        del execution_profile
        keys = {(run_id, candidate_id) for candidate_id in candidate_ids}
        if keys & self.claimed:
            return False
        self.claimed.update(keys)
        return True

    def _release_private_candidates(
        self,
        run_id: str,
        candidate_ids: Sequence[str],
        *,
        execution_profile: ExecutionProfile,
    ) -> bool:
        del execution_profile
        keys = {(run_id, candidate_id) for candidate_id in candidate_ids}
        if not keys <= self.claimed:
            return False
        self.claimed.difference_update(keys)
        return True


class _DownloadManager:
    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self.events = events
        self.add_calls: list[tuple[tuple[Any, ...], str]] = []

    def add_tasks(self, videos: Sequence[Any], save_dir: str) -> int:
        batch = tuple(videos)
        self.events.append(("manager.add", batch))
        self.add_calls.append((batch, save_dir))
        return len(batch)

    def cancel_videos_and_wait(
        self,
        videos: Sequence[Any],
        timeout_ms: int | None = None,
    ) -> dict[str, str]:
        del timeout_ms
        return {item.id: "cancelled" for item in videos}


class _InterruptingDownloadManager(_DownloadManager):
    def __init__(
        self,
        events: list[tuple[str, Any]],
        interruption: BaseException,
    ) -> None:
        super().__init__(events)
        self.interruption = interruption

    def add_tasks(self, videos: Sequence[Any], save_dir: str) -> int:
        super().add_tasks(videos, save_dir)
        raise self.interruption


class _NoopFailedRecordStore:
    def set_refresh_callback(self, _callback: Any) -> None:
        return None

    def shutdown(self, timeout: float = 1.0) -> bool:
        del timeout
        return True


class _FakeCoordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[Mapping[str, Any], ExecutionProfile]] = []

    def admit(
        self,
        payload: Mapping[str, Any],
        *,
        execution_profile: ExecutionProfile,
    ) -> dict[str, Any]:
        self.calls.append((dict(payload), execution_profile))
        return {
            "status": "queued",
            "tool_id": "link_parser",
            "run_id": "run-link-parser",
            "accepted_count": 1,
            "candidate_ids": ["a" * 64],
            "video_ids": ["video-1"],
        }


def _profile(root: Path) -> ExecutionProfile:
    return local_execution_profile(
        host_surface="test",
        owner_id="desktop:link-lab",
        approved_roots=(root,),
        tool_permissions=("network",),
        allow_external_plugins=False,
    )


def _private_result(private_url: str) -> PrivateToolResult:
    candidate_id = hashlib.sha256(private_url.encode("utf-8")).hexdigest()
    admission = link_candidate_admission_capability(
        private_url,
        platform="bilibili",
        resource_kind="media",
    )
    public_row = {
        "candidate_id": candidate_id,
        "display_url": "https://www.bilibili.com/[redacted]",
        "platform": "bilibili",
        "resource_kind": "media",
        "format_hint": "MP4",
        "expanded": False,
        "admittable": admission.admittable,
        "admission_reason_code": admission.reason_code,
    }
    return PrivateToolResult(
        run_id="run-link-parser",
        tool_id="link_parser",
        output_paths=(),
        structured_data={"links": [public_row]},
        private_data={"candidates": [{**public_row, "private_url": private_url}]},
    )


def test_coordinator_uses_trusted_resources_and_returns_safe_receipt(
    tmp_path: Path,
) -> None:
    from app.services.frontend_link_candidate_admission import (
        FrontendLinkCandidateAdmissionCoordinator,
    )

    save_directory = tmp_path / "downloads"
    save_directory.mkdir()
    private_url = "https://www.bilibili.com/media/demo.mp4?token=private"
    candidate_id = hashlib.sha256(private_url.encode("utf-8")).hexdigest()
    runner = _Runner(_private_result(private_url))
    events: list[tuple[str, Any]] = []
    manager = _DownloadManager(events)

    def store_items(items: Sequence[Any]) -> None:
        events.append(("host.store", tuple(items)))

    coordinator = FrontendLinkCandidateAdmissionCoordinator(
        tool_runner_service=runner,
        download_manager_provider=lambda: manager,
        save_directory_provider=lambda: str(save_directory),
        store_items=store_items,
        remove_items_if_matches=lambda _items: None,
    )

    receipt = coordinator.admit(
        {
            "tool_id": "link_parser",
            "run_id": "run-link-parser",
            "candidate_ids": [candidate_id],
        },
        execution_profile=_profile(tmp_path),
    )

    assert events[0][0] == "host.store"
    assert events[1][0] == "manager.add"
    assert Path(manager.add_calls[0][1]).resolve() == save_directory.resolve()
    assert receipt == {
        "status": "queued",
        "tool_id": "link_parser",
        "run_id": "run-link-parser",
        "accepted_count": 1,
        "candidate_ids": [candidate_id],
        "video_ids": [manager.add_calls[0][0][0].id],
    }
    assert "url" not in repr(receipt).lower()
    assert "private" not in repr(receipt).lower()


def test_coordinator_rejects_client_owned_host_resources_before_lookup(
    tmp_path: Path,
) -> None:
    from app.services.frontend_link_candidate_admission import (
        FrontendLinkCandidateAdmissionCoordinator,
    )

    private_url = "https://www.bilibili.com/media/demo.mp4"
    candidate_id = hashlib.sha256(private_url.encode("utf-8")).hexdigest()
    runner = _Runner(_private_result(private_url))
    coordinator = FrontendLinkCandidateAdmissionCoordinator(
        tool_runner_service=runner,
        download_manager_provider=lambda: pytest.fail("manager must not be resolved"),
        save_directory_provider=lambda: pytest.fail("directory must not be resolved"),
        store_items=lambda _items: pytest.fail("items must not be stored"),
        remove_items_if_matches=lambda _items: None,
    )

    with pytest.raises(LinkCandidateAdmissionError) as exc_info:
        coordinator.admit(
            {
                "tool_id": "link_parser",
                "run_id": "run-link-parser",
                "candidate_ids": [candidate_id],
                "save_directory": str(tmp_path / "attacker-controlled"),
            },
            execution_profile=_profile(tmp_path),
        )

    assert exc_info.value.code == "invalid_admission_request"
    assert runner.lookup_calls == []


def test_frontend_action_captures_profile_and_returns_only_safe_receipt(
    tmp_path: Path,
) -> None:
    coordinator = _FakeCoordinator()
    profile = _profile(tmp_path)
    service = FrontendStateService(
        config_manager=ConfigManager(str(tmp_path / "config.json")),
        failed_record_store=_NoopFailedRecordStore(),
        tool_runner_service=object(),
        execution_profile=profile,
        link_candidate_admission_coordinator=coordinator,
    )
    try:
        response = service.handle_action(
            "tool_admit_candidates",
            {
                "tool_id": "link_parser",
                "run_id": "run-link-parser",
                "candidate_ids": ["a" * 64],
            },
        )
    finally:
        service.destroy()

    assert response == {
        "status": "ok",
        "message": "selected links were queued",
        "data": {
            "status": "queued",
            "tool_id": "link_parser",
            "run_id": "run-link-parser",
            "accepted_count": 1,
            "candidate_ids": ["a" * 64],
            "video_ids": ["video-1"],
        },
    }
    assert coordinator.calls == [
        (
            {
                "tool_id": "link_parser",
                "run_id": "run-link-parser",
                "candidate_ids": ["a" * 64],
            },
            profile,
        )
    ]


def test_post_commit_event_failure_keeps_the_successful_receipt(
    tmp_path: Path,
) -> None:
    coordinator = _FakeCoordinator()
    service = FrontendStateService(
        config_manager=ConfigManager(str(tmp_path / "config.json")),
        failed_record_store=_NoopFailedRecordStore(),
        tool_runner_service=object(),
        execution_profile=_profile(tmp_path),
        link_candidate_admission_coordinator=coordinator,
    )

    def fail_event(_topic: str, _payload: Mapping[str, Any]) -> None:
        raise RuntimeError("private notification failure")

    service._emit_frontend_event = fail_event  # type: ignore[method-assign]
    try:
        response = service.handle_action(
            "tool_admit_candidates",
            {
                "tool_id": "link_parser",
                "run_id": "run-link-parser",
                "candidate_ids": ["a" * 64],
            },
        )
    finally:
        service.destroy()

    assert response["status"] == "ok"
    assert response["data"]["status"] == "queued"
    assert "private" not in repr(response).lower()
    assert len(coordinator.calls) == 1


def test_post_commit_control_flow_is_contained_without_killing_later_admission(
    tmp_path: Path,
) -> None:
    coordinator = _FakeCoordinator()

    class PostCommitControlFlow(BaseException):
        pass

    primary = PostCommitControlFlow("notification interrupted")
    service = FrontendStateService(
        config_manager=ConfigManager(str(tmp_path / "config.json")),
        failed_record_store=_NoopFailedRecordStore(),
        tool_runner_service=object(),
        execution_profile=_profile(tmp_path),
        link_candidate_admission_coordinator=coordinator,
    )

    def interrupt_event(_topic: str, _payload: Mapping[str, Any]) -> None:
        raise primary

    service._emit_frontend_event = interrupt_event  # type: ignore[method-assign]
    try:
        first = service.handle_action(
            "tool_admit_candidates",
            {
                "tool_id": "link_parser",
                "run_id": "run-link-parser",
                "candidate_ids": ["a" * 64],
            },
        )
        service._emit_frontend_event = lambda *_args: None  # type: ignore[method-assign]
        second = service.handle_action(
            "tool_admit_candidates",
            {
                "tool_id": "link_parser",
                "run_id": "run-link-parser-2",
                "candidate_ids": ["b" * 64],
            },
        )
    finally:
        service.destroy()

    assert first["status"] == "ok"
    assert first["data"]["status"] == "queued"
    assert second["status"] == "ok"
    assert len(coordinator.calls) == 2


def test_unknown_admission_failure_keeps_fixed_receipt_when_logger_is_hostile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _FakeCoordinator()
    service = FrontendStateService(
        config_manager=ConfigManager(str(tmp_path / "config.json")),
        failed_record_store=_NoopFailedRecordStore(),
        tool_runner_service=object(),
        execution_profile=_profile(tmp_path),
        link_candidate_admission_coordinator=coordinator,
    )

    def fail_admission(
        _payload: Mapping[str, Any],
        *,
        execution_profile: ExecutionProfile,
    ) -> dict[str, Any]:
        del execution_profile
        raise RuntimeError("https://private.example/token=secret")

    class HostileLoggerFailure(BaseException):
        pass

    def fail_logger(*_args: Any, **_kwargs: Any) -> None:
        raise HostileLoggerFailure("diagnostic sink failed")

    coordinator.admit = fail_admission  # type: ignore[method-assign]
    try:
        with monkeypatch.context() as context:
            context.setattr(
                "app.services.frontend_link_candidate_admission.debug_logger.log_exception",
                fail_logger,
            )
            response = service.handle_action(
                "tool_admit_candidates",
                {
                    "tool_id": "link_parser",
                    "run_id": "run-link-parser",
                    "candidate_ids": ["a" * 64],
                },
            )
    finally:
        service.destroy()

    assert response == {
        "status": "error",
        "message": "selected links could not be queued",
        "data": {"code": "link_admission_failed"},
    }
    assert "private.example" not in repr(response)
    assert "secret" not in repr(response)


def test_host_storage_is_rolled_back_when_manager_add_is_interrupted(
    tmp_path: Path,
) -> None:
    from app.services.frontend_link_candidate_admission import (
        FrontendLinkCandidateAdmissionCoordinator,
    )

    save_directory = tmp_path / "downloads"
    save_directory.mkdir()
    private_url = "https://www.bilibili.com/media/demo.mp4"
    candidate_id = hashlib.sha256(private_url.encode("utf-8")).hexdigest()
    runner = _Runner(_private_result(private_url))
    events: list[tuple[str, Any]] = []
    primary = SystemExit("manager interrupted")
    manager = _InterruptingDownloadManager(events, primary)
    stored: dict[str, Any] = {}

    def store_items(items: Sequence[Any]) -> None:
        stored.update({item.id: item for item in items})

    def remove_items(items: Sequence[Any]) -> None:
        for item in items:
            if stored.get(item.id) is item:
                stored.pop(item.id)

    coordinator = FrontendLinkCandidateAdmissionCoordinator(
        tool_runner_service=runner,
        download_manager_provider=lambda: manager,
        save_directory_provider=lambda: str(save_directory),
        store_items=store_items,
        remove_items_if_matches=remove_items,
    )

    with pytest.raises(SystemExit) as caught:
        coordinator.admit(
            {
                "tool_id": "link_parser",
                "run_id": "run-link-parser",
                "candidate_ids": [candidate_id],
            },
            execution_profile=_profile(tmp_path),
        )

    assert caught.value is primary
    assert stored == {}
    assert runner.claimed == set()
