from __future__ import annotations

import asyncio
import gc
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from app.web.file_response_service import (
    WebFileResponseService,
    _LeasedStreamingResponse,
)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/media/video-1",
            "raw_path": b"/api/media/video-1",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
        }
    )


def _root_grant(path: Path) -> tuple[str, tuple[int, int]]:
    normalized = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    value = os.lstat(normalized)
    return normalized, (int(value.st_dev), int(value.st_ino))


class _MediaContext(SimpleNamespace):
    @contextmanager
    def lease_approved_root_grants_snapshot(self):
        yield self.approved_root_grants_snapshot()


async def _response_body(response) -> bytes:
    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    await response(_request().scope, receive, send)
    return b"".join(
        bytes(message.get("body", b""))
        for message in messages
        if message.get("type") == "http.response.body"
    )


def _replace_approved_ancestor(
    *,
    ancestor: Path,
    moved_ancestor: Path,
    relative_media: Path,
    attacker_payload: bytes,
) -> bool:
    try:
        ancestor.rename(moved_ancestor)
    except OSError:
        # Windows directory capabilities intentionally deny rename/delete while
        # a response owns the authority.  That is already a safe outcome.
        return False
    replacement = ancestor / relative_media
    replacement.parent.mkdir(parents=True)
    replacement.write_bytes(attacker_payload)
    return True


@pytest.mark.parametrize(
    ("range_header", "expected"),
    (
        (None, b"approved-media-payload"),
        ("bytes=1-8", b"pproved-"),
    ),
)
def test_media_response_keeps_approved_identity_until_body_is_consumed(
    tmp_path: Path,
    range_header: str | None,
    expected: bytes,
) -> None:
    ancestor = tmp_path / "session-ancestor"
    approved = ancestor / "approved"
    media = approved / "nested" / "video.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"approved-media-payload")
    moved_ancestor = tmp_path / "session-ancestor-before-swap"
    attacker_payload = b"attacker-media-payload"
    context = _MediaContext(
        controller=SimpleNamespace(get_media_path=lambda _video_id: os.fspath(media)),
        approved_root_grants_snapshot=lambda: (_root_grant(approved),),
        approved_roots_snapshot=lambda: (os.fspath(approved),),
    )
    service = WebFileResponseService(
        get_request_context=lambda _request: context,
        has_valid_session_token=lambda _request: True,
    )

    response = asyncio.run(
        service.get_media(
            _request(),
            "video-1",
            range_header,
            require_session_token=False,
        )
    )
    swapped = _replace_approved_ancestor(
        ancestor=ancestor,
        moved_ancestor=moved_ancestor,
        relative_media=Path("approved/nested/video.mp4"),
        attacker_payload=attacker_payload,
    )

    body = asyncio.run(_response_body(response))

    assert body == expected
    if swapped:
        assert (ancestor / "approved/nested/video.mp4").read_bytes() == attacker_payload


def test_media_response_cannot_follow_a_final_child_symlink_swap(
    tmp_path: Path,
) -> None:
    approved = tmp_path / "approved"
    media = approved / "video.mp4"
    moved_media = approved / "video-before-swap.mp4"
    outside = tmp_path / "outside.mp4"
    approved.mkdir()
    media.write_bytes(b"approved-media")
    outside.write_bytes(b"outside-media")
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to(outside)
        probe.unlink()
    except OSError as exc:
        pytest.fail(f"file symlink creation unavailable: {exc}; enable Developer Mode or run elevated - symlink attack coverage must not be silently skipped")
    context = _MediaContext(
        controller=SimpleNamespace(get_media_path=lambda _video_id: os.fspath(media)),
        approved_root_grants_snapshot=lambda: (_root_grant(approved),),
        approved_roots_snapshot=lambda: (os.fspath(approved),),
    )
    service = WebFileResponseService(
        get_request_context=lambda _request: context,
        has_valid_session_token=lambda _request: True,
    )

    response = asyncio.run(
        service.get_media(
            _request(),
            "video-1",
            None,
            require_session_token=False,
        )
    )
    media.rename(moved_media)
    media.symlink_to(outside)

    body = asyncio.run(_response_body(response))

    assert body == b"approved-media"
    assert outside.read_bytes() == b"outside-media"


def test_media_response_reads_the_opened_file_after_a_regular_child_replacement(
    tmp_path: Path,
) -> None:
    approved = tmp_path / "approved"
    media = approved / "video.mp4"
    moved_media = approved / "video-before-swap.mp4"
    approved.mkdir()
    media.write_bytes(b"approved-media")
    context = _MediaContext(
        controller=SimpleNamespace(get_media_path=lambda _video_id: os.fspath(media)),
        approved_root_grants_snapshot=lambda: (_root_grant(approved),),
        approved_roots_snapshot=lambda: (os.fspath(approved),),
    )
    service = WebFileResponseService(
        get_request_context=lambda _request: context,
        has_valid_session_token=lambda _request: True,
    )

    response = asyncio.run(
        service.get_media(
            _request(),
            "video-1",
            None,
            require_session_token=False,
        )
    )
    media.rename(moved_media)
    media.write_bytes(b"attacker-media")

    body = asyncio.run(_response_body(response))

    assert body == b"approved-media"
    assert media.read_bytes() == b"attacker-media"


def test_media_response_preserves_send_baseexception_over_lease_cleanup(
    tmp_path: Path,
) -> None:
    class PrimaryAbort(BaseException):
        pass

    class CleanupAbort(BaseException):
        pass

    approved = tmp_path / "approved"
    media = approved / "video.mp4"
    approved.mkdir()
    media.write_bytes(b"approved")
    context = _MediaContext(
        controller=SimpleNamespace(get_media_path=lambda _video_id: os.fspath(media)),
        approved_root_grants_snapshot=lambda: (_root_grant(approved),),
        approved_roots_snapshot=lambda: (os.fspath(approved),),
    )
    service = WebFileResponseService(
        get_request_context=lambda _request: context,
        has_valid_session_token=lambda _request: True,
    )
    response = asyncio.run(
        service.get_media(
            _request(),
            "video-1",
            None,
            require_session_token=False,
        )
    )
    lease = response._approved_file_lease
    real_close = lease.close
    primary = PrimaryAbort("send stopped")
    cleanup_calls = 0

    def hostile_close(body_error=None) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        real_close(body_error)
        raise CleanupAbort("lease cleanup stopped")

    lease.close = hostile_close

    async def invoke() -> None:
        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            if message.get("type") == "http.response.body":
                raise primary

        await response(_request().scope, receive, send)

    with pytest.raises(PrimaryAbort) as exc_info:
        asyncio.run(invoke())

    assert exc_info.value is primary
    assert cleanup_calls == 1


def test_successful_media_response_propagates_cleanup_control_flow() -> None:
    class CleanupAbort(BaseException):
        pass

    cleanup_abort = CleanupAbort("lease cleanup stopped")

    class FakeLease:
        @staticmethod
        def close(_primary=None) -> None:
            raise cleanup_abort

    response = _LeasedStreamingResponse(
        FakeLease(),
        iter((b"approved",)),
        media_type="video/mp4",
    )

    async def invoke() -> None:
        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message: dict[str, object]) -> None:
            return None

        await response(_request().scope, receive, send)

    with pytest.raises(CleanupAbort) as exc_info:
        asyncio.run(invoke())

    assert exc_info.value is cleanup_abort


def test_invalid_range_releases_approved_root_before_return(tmp_path: Path) -> None:
    ancestor = tmp_path / "ancestor"
    approved = ancestor / "approved"
    media = approved / "video.mp4"
    approved.mkdir(parents=True)
    media.write_bytes(b"approved")
    context = _MediaContext(
        controller=SimpleNamespace(get_media_path=lambda _video_id: os.fspath(media)),
        approved_root_grants_snapshot=lambda: (_root_grant(approved),),
        approved_roots_snapshot=lambda: (os.fspath(approved),),
    )
    service = WebFileResponseService(
        get_request_context=lambda _request: context,
        has_valid_session_token=lambda _request: True,
    )

    response = asyncio.run(
        service.get_media(
            _request(),
            "video-1",
            "bytes=999-1000",
            require_session_token=False,
        )
    )
    moved = tmp_path / "ancestor-after-response"
    ancestor.rename(moved)

    assert response.status_code == 416
    assert moved.is_dir()


def test_unconsumed_media_response_releases_lease_when_discarded(
    tmp_path: Path,
) -> None:
    ancestor = tmp_path / "ancestor"
    approved = ancestor / "approved"
    media = approved / "video.mp4"
    approved.mkdir(parents=True)
    media.write_bytes(b"approved")
    context = _MediaContext(
        controller=SimpleNamespace(get_media_path=lambda _video_id: os.fspath(media)),
        approved_root_grants_snapshot=lambda: (_root_grant(approved),),
        approved_roots_snapshot=lambda: (os.fspath(approved),),
    )
    service = WebFileResponseService(
        get_request_context=lambda _request: context,
        has_valid_session_token=lambda _request: True,
    )

    response = asyncio.run(
        service.get_media(
            _request(),
            "video-1",
            None,
            require_session_token=False,
        )
    )
    del response
    gc.collect()
    moved = tmp_path / "ancestor-after-discard"
    ancestor.rename(moved)

    assert moved.is_dir()


def test_cancelled_media_open_closes_a_late_file_lease() -> None:
    opened = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    session_lease_entered = threading.Event()
    session_lease_released = threading.Event()

    class FakeLease:
        size = 8

        def close(self, _primary=None) -> None:
            closed.set()

    class BlockingPathPolicy:
        @staticmethod
        def open_approved_file(_path, _approved_roots):
            opened.set()
            assert release.wait(2.0)
            return FakeLease()

    @contextmanager
    def lease_session_grants():
        session_lease_entered.set()
        try:
            yield (("approved", (1, 1)),)
        finally:
            session_lease_released.set()

    context = _MediaContext(
        controller=SimpleNamespace(get_media_path=lambda _video_id: "video.mp4"),
        approved_root_grants_snapshot=lambda: (("approved", (1, 1)),),
        approved_roots_snapshot=lambda: ("approved",),
        lease_approved_root_grants_snapshot=lease_session_grants,
    )
    service = WebFileResponseService(
        get_request_context=lambda _request: context,
        has_valid_session_token=lambda _request: True,
        path_policy=BlockingPathPolicy(),
    )

    async def cancel_open() -> None:
        task = asyncio.create_task(
            service.get_media(
                _request(),
                "video-1",
                None,
                require_session_token=False,
            )
        )
        assert await asyncio.to_thread(opened.wait, 2.0)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
            assert session_lease_entered.is_set()
            assert not session_lease_released.is_set()
        finally:
            release.set()
        for _attempt in range(100):
            if closed.is_set() and session_lease_released.is_set():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("late media open did not release both owned leases")

    asyncio.run(cancel_open())
    assert closed.is_set()


def test_media_open_rejects_a_root_replaced_since_the_session_grant(
    tmp_path: Path,
) -> None:
    ancestor = tmp_path / "ancestor"
    approved = ancestor / "approved"
    media = approved / "video.mp4"
    approved.mkdir(parents=True)
    media.write_bytes(b"approved")
    grant = _root_grant(approved)
    moved = tmp_path / "ancestor-at-grant"
    ancestor.rename(moved)
    attacker_media = ancestor / "approved/video.mp4"
    attacker_media.parent.mkdir(parents=True)
    attacker_media.write_bytes(b"attacker")
    context = _MediaContext(
        controller=SimpleNamespace(get_media_path=lambda _video_id: os.fspath(media)),
        approved_root_grants_snapshot=lambda: (grant,),
        approved_roots_snapshot=lambda: (os.fspath(approved),),
    )
    service = WebFileResponseService(
        get_request_context=lambda _request: context,
        has_valid_session_token=lambda _request: True,
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            service.get_media(
                _request(),
                "video-1",
                None,
                require_session_token=False,
            )
        )

    assert getattr(exc_info.value, "status_code", None) == 403
    assert attacker_media.read_bytes() == b"attacker"


def test_media_open_does_not_fall_back_to_legacy_root_strings(
    tmp_path: Path,
) -> None:
    approved = tmp_path / "approved"
    media = approved / "video.mp4"
    approved.mkdir()
    media.write_bytes(b"approved")
    context = _MediaContext(
        controller=SimpleNamespace(get_media_path=lambda _video_id: os.fspath(media)),
        approved_roots_snapshot=lambda: (os.fspath(approved),),
    )
    service = WebFileResponseService(
        get_request_context=lambda _request: context,
        has_valid_session_token=lambda _request: True,
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            service.get_media(
                _request(),
                "video-1",
                None,
                require_session_token=False,
            )
        )

    assert getattr(exc_info.value, "status_code", None) == 403


def test_media_open_redacts_an_absolute_path_from_authorization_errors() -> None:
    secret_path = r"D:\private-session-root\approved\media.mp4"

    class RejectingPathPolicy:
        @staticmethod
        def open_approved_file(_path, _approved_root_grants):
            raise OSError(secret_path + "\nforged-detail")

    context = _MediaContext(
        controller=SimpleNamespace(get_media_path=lambda _video_id: secret_path),
        approved_root_grants_snapshot=lambda: (("approved", (1, 1)),),
    )
    service = WebFileResponseService(
        get_request_context=lambda _request: context,
        has_valid_session_token=lambda _request: True,
        path_policy=RejectingPathPolicy(),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            service.get_media(
                _request(),
                "video-1",
                None,
                require_session_token=False,
            )
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "media path is not authorized"
    assert secret_path not in repr(exc_info.value)
    assert "forged-detail" not in repr(exc_info.value)


def test_media_open_does_not_stringify_a_hostile_authorization_error() -> None:
    class HostileAuthorizationError(PermissionError):
        def __str__(self) -> str:
            raise AssertionError("authorization errors must not be rendered")

    error = HostileAuthorizationError()

    class RejectingPathPolicy:
        @staticmethod
        def open_approved_file(_path, _approved_root_grants):
            raise error

    context = _MediaContext(
        controller=SimpleNamespace(get_media_path=lambda _video_id: "video.mp4"),
        approved_root_grants_snapshot=lambda: (("approved", (1, 1)),),
    )
    service = WebFileResponseService(
        get_request_context=lambda _request: context,
        has_valid_session_token=lambda _request: True,
        path_policy=RejectingPathPolicy(),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            service.get_media(
                _request(),
                "video-1",
                None,
                require_session_token=False,
            )
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "media path is not authorized"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "range_header",
    (
        "bytes=" + "9" * 5000 + "-",
        "bytes=-" + "9" * 5000,
        "bytes=0-" + "9" * 5000,
    ),
)
def test_byte_range_rejects_pathological_integer_fields(range_header: str) -> None:
    assert WebFileResponseService._parse_byte_range(range_header, 1024) is None


@pytest.mark.parametrize(
    ("method_name", "artifact_name", "expected_body"),
    (
        ("latest_log_response", "latest_debug.log", b"approved-log"),
        (
            "latest_error_summary_response",
            "latest_error_summary.md",
            b"approved-summary",
        ),
    ),
)
def test_debug_artifact_response_pins_the_open_file_generation(
    tmp_path: Path,
    method_name: str,
    artifact_name: str,
    expected_body: bytes,
) -> None:
    logs_dir = tmp_path / "logs"
    artifact = logs_dir / artifact_name
    outside = tmp_path / "private.txt"
    logs_dir.mkdir()
    artifact.write_bytes(expected_body)
    outside.write_bytes(b"outside-secret")
    debug_artifacts = SimpleNamespace(
        logs_dir=logs_dir,
        latest_log_path=lambda: os.fspath(logs_dir / "latest_debug.log"),
        latest_error_summary_path=lambda: os.fspath(
            logs_dir / "latest_error_summary.md"
        ),
    )
    service = WebFileResponseService(
        get_request_context=lambda _request: None,
        has_valid_session_token=lambda _request: True,
        debug_artifacts_service=debug_artifacts,
    )

    response = getattr(service, method_name)(_request())
    moved = logs_dir / f"{artifact_name}.opened"
    artifact.rename(moved)
    try:
        artifact.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        response._approved_file_lease.close()
        pytest.fail(f"file symlinks are unavailable: {exc}; enable Developer Mode or run elevated - symlink attack coverage must not be silently skipped")

    try:
        body = asyncio.run(_response_body(response))

        assert body == expected_body
        assert artifact.read_bytes() == b"outside-secret"
        expected_name = (
            "latest_debug.log"
            if method_name == "latest_log_response"
            else "latest_error_summary.md"
        )
        assert response.headers["content-disposition"] == (
            f'attachment; filename="{expected_name}"'
        )
    finally:
        service.close()


@pytest.mark.parametrize(
    "method_name",
    ("latest_log_response", "latest_error_summary_response"),
)
def test_debug_artifact_response_rejects_a_replaced_logs_root(
    tmp_path: Path,
    method_name: str,
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "latest_debug.log").write_bytes(b"approved-log")
    (logs_dir / "latest_error_summary.md").write_bytes(b"approved-summary")
    debug_artifacts = SimpleNamespace(
        logs_dir=logs_dir,
        latest_log_path=lambda: os.fspath(logs_dir / "latest_debug.log"),
        latest_error_summary_path=lambda: os.fspath(
            logs_dir / "latest_error_summary.md"
        ),
    )
    service = WebFileResponseService(
        get_request_context=lambda _request: None,
        has_valid_session_token=lambda _request: True,
        debug_artifacts_service=debug_artifacts,
    )
    try:
        previous_logs = tmp_path / "logs-at-approval"
        try:
            logs_dir.rename(previous_logs)
        except PermissionError:
            service._debug_root_authority.assert_bound()
            assert (logs_dir / "latest_debug.log").read_bytes() == b"approved-log"
            assert (
                logs_dir / "latest_error_summary.md"
            ).read_bytes() == b"approved-summary"
            return
        logs_dir.mkdir()
        attacker_log = logs_dir / "latest_debug.log"
        attacker_summary = logs_dir / "latest_error_summary.md"
        attacker_log.write_bytes(b"attacker-log")
        attacker_summary.write_bytes(b"attacker-summary")

        with pytest.raises(HTTPException) as exc_info:
            getattr(service, method_name)(_request())

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "debug artifact is not authorized"
        assert os.fspath(logs_dir) not in repr(exc_info.value)
        assert attacker_log.read_bytes() == b"attacker-log"
        assert attacker_summary.read_bytes() == b"attacker-summary"
    finally:
        service.close()
