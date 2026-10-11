from __future__ import annotations

import threading

from app.ui.viewmodels.frontend_action_worker import FrontendActionRequest, FrontendActionWorker


class RecordingService:
    def __init__(self) -> None:
        self.thread_id: int | None = None

    def handle_action(self, action: str, payload: dict) -> dict:
        self.thread_id = threading.get_ident()
        return {"status": "ok", "action": action, "payload": payload}


def test_frontend_action_worker_runs_handle_action_off_calling_thread() -> None:
    service = RecordingService()
    event = threading.Event()
    results = []
    worker = FrontendActionWorker(lambda result: (results.append(result), event.set()))
    try:
        worker.submit(
            FrontendActionRequest(
                sequence=1,
                service=service,
                service_token=id(service),
                action="log_operation",
                payload={"operation": "refresh"},
            )
        )

        assert event.wait(2)
        assert service.thread_id is not None
        assert service.thread_id != threading.get_ident()
        assert results[0].result["status"] == "ok"
        assert results[0].payload == {"operation": "refresh"}
    finally:
        worker.shutdown()


def test_frontend_action_worker_reports_action_errors() -> None:
    class BrokenService:
        def handle_action(self, _action: str, _payload: dict) -> dict:
            raise RuntimeError("https://private.example/token=secret")

    event = threading.Event()
    results = []
    service = BrokenService()
    worker = FrontendActionWorker(lambda result: (results.append(result), event.set()))
    try:
        worker.submit(
            FrontendActionRequest(
                sequence=2,
                service=service,
                service_token=id(service),
                action="log_operation",
                payload={"operation": "export"},
            )
        )

        assert event.wait(2)
        assert results[0].result["status"] == "error"
        assert results[0].result["message"] == "frontend action failed"
        assert "private.example" not in repr(results[0].result)
        assert "secret" not in repr(results[0].result)
    finally:
        worker.shutdown()


def test_frontend_action_worker_survives_control_flow_and_keeps_request_identity() -> None:
    class AbortAction(BaseException):
        pass

    class RecoveringService:
        def __init__(self) -> None:
            self.calls = 0

        def handle_action(self, _action: str, _payload: dict) -> dict:
            self.calls += 1
            if self.calls == 1:
                raise AbortAction("private URL must not reach the receipt")
            return {"status": "ok", "data": {"accepted_count": 1}}

    event = threading.Event()
    results = []
    service = RecoveringService()

    def record(result: object) -> None:
        results.append(result)
        if len(results) == 2:
            event.set()

    worker = FrontendActionWorker(record)
    first_payload = {
        "tool_id": "link_parser",
        "run_id": "run-one",
        "candidate_ids": ["a" * 64],
    }
    second_payload = {
        "tool_id": "link_parser",
        "run_id": "run-two",
        "candidate_ids": ["b" * 64],
    }
    try:
        worker.submit(
            FrontendActionRequest(
                sequence=41,
                service=service,
                service_token=id(service),
                action="tool_admit_candidates",
                payload=first_payload,
            )
        )
        worker.submit(
            FrontendActionRequest(
                sequence=42,
                service=service,
                service_token=id(service),
                action="tool_admit_candidates",
                payload=second_payload,
            )
        )

        assert event.wait(2)
        assert worker._worker._thread.is_alive()
        assert results[0].sequence == 41
        assert results[0].service_token == id(service)
        assert results[0].action == "tool_admit_candidates"
        assert results[0].payload == first_payload
        assert results[0].result == {
            "status": "error",
            "message": "frontend action failed",
            "data": {"error_code": "frontend_action_failed"},
        }
        assert "private URL" not in repr(results[0].result)
        assert results[1].sequence == 42
        assert results[1].payload == second_payload
        assert results[1].result["status"] == "ok"
    finally:
        worker.shutdown()


def test_frontend_action_worker_survives_hostile_exception_stringification() -> None:
    class HostileError(RuntimeError):
        def __str__(self) -> str:
            raise KeyboardInterrupt("secondary formatting failure")

    class RecoveringService:
        def __init__(self) -> None:
            self.calls = 0

        def handle_action(self, _action: str, _payload: dict) -> dict:
            self.calls += 1
            if self.calls == 1:
                raise HostileError()
            return {"status": "ok"}

    event = threading.Event()
    results = []
    service = RecoveringService()

    def record(result: object) -> None:
        results.append(result)
        if len(results) == 2:
            event.set()

    worker = FrontendActionWorker(record)
    try:
        for sequence in (51, 52):
            worker.submit(
                FrontendActionRequest(
                    sequence=sequence,
                    service=service,
                    service_token=id(service),
                    action="tool_admit_candidates",
                    payload={"candidate_ids": ["a" * 64]},
                )
            )

        assert event.wait(2)
        assert worker._worker._thread.is_alive()
        assert results[0].sequence == 51
        assert results[0].result == {
            "status": "error",
            "message": "frontend action failed",
            "data": {"error_code": "frontend_action_failed"},
        }
        assert results[1].sequence == 52
        assert results[1].result["status"] == "ok"
    finally:
        worker.shutdown()


def test_frontend_action_worker_reports_rejection_after_shutdown() -> None:
    worker = FrontendActionWorker(lambda _result: None)
    worker.shutdown()

    accepted = worker.submit(
        FrontendActionRequest(
            sequence=1,
            service=object(),
            service_token=1,
            action="tool_admit_candidates",
            payload={},
        )
    )

    assert accepted is False
