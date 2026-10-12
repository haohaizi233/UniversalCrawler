"""Web 文件响应服务。"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import secrets
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.services.debug_service import DebugArtifactsService
from app.services.path_policy import ApprovedFileLease, PathPolicy
from shared.filesystem_directory_capability import (
    open_filesystem_directory_capability,
)

_MAX_RANGE_INTEGER_DIGITS = 20


class _LeasedStreamingResponse(StreamingResponse):
    """Keep an approved file/directory lease through the complete ASGI send."""

    def __init__(self, lease: ApprovedFileLease, *args: Any, **kwargs: Any) -> None:
        self._approved_file_lease = lease
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send) -> None:
        primary: BaseException | None = None
        try:
            await super().__call__(scope, receive, send)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                self._approved_file_lease.close(primary)
            except BaseException as cleanup_error:
                if primary is None and not isinstance(cleanup_error, Exception):
                    raise
                # Once a successful response body has been sent, an authority
                # cleanup diagnostic cannot be turned into a second response.
                pass

    def __del__(self) -> None:
        try:
            self._approved_file_lease.close()
        except BaseException:
            pass


class WebFileResponseService:
    """承载媒体文件与调试产物的文件响应逻辑。"""

    MEDIA_TYPE_OVERRIDES = {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
        ".flv": "video/x-flv",
        ".wmv": "video/x-ms-wmv",
        ".ts": "video/mp2t",
        ".m3u8": "application/vnd.apple.mpegurl",
    }

    def __init__(
        self,
        *,
        get_request_context: Callable[[Request], object],
        has_valid_session_token: Callable[[Request], bool],
        path_policy: PathPolicy | None = None,
        debug_artifacts_service: DebugArtifactsService | None = None,
    ) -> None:
        self._get_request_context = get_request_context
        self._has_valid_session_token = has_valid_session_token
        self._path_policy = path_policy or PathPolicy()
        self._debug_artifacts_service = (
            debug_artifacts_service or DebugArtifactsService()
        )
        self._debug_root_close_lock = threading.Lock()
        self._debug_root_closed = False
        self._debug_root_authorities: dict[str, Any] = {}
        self._debug_root_grants: dict[str, tuple[str, Any]] = {}
        # 构造时立即绑定当前日志根，钉住其目录代际；之后同路径被替换会被拒绝。
        self._debug_root_authority = self._bind_debug_root(
            self._debug_artifacts_service.logs_dir
        )

    @staticmethod
    def _debug_root_key(logs_dir: Any) -> str:
        return os.path.normcase(os.path.normpath(os.fspath(logs_dir)))

    def _bind_debug_root(self, logs_dir: Any):
        authority = open_filesystem_directory_capability(logs_dir)
        try:
            debug_root, debug_identity = authority.snapshot_bound_identity()
            grant = (os.path.normcase(os.fspath(debug_root)), debug_identity)
        except BaseException as primary:
            try:
                authority.close()
            except BaseException:
                try:
                    primary.add_note("debug root authority cleanup failed")
                except BaseException:
                    pass
            raise
        key = self._debug_root_key(logs_dir)
        self._debug_root_authorities[key] = authority
        self._debug_root_grants[key] = grant
        return authority

    def _debug_grant_for(self, path: str) -> tuple[str, Any]:
        """取被服务文件所属日志根的能力授予。

        日志根可能在运行期切换（例如用户数据根环境值变化后 debug logger
        重新锚定），此时为新根惰性绑定能力；已绑定根保持钉住的目录代际，
        同路径目录被替换仍会被 PathPolicy 拒绝。
        """
        key = self._debug_root_key(Path(path).parent)
        with self._debug_root_close_lock:
            if self._debug_root_closed:
                raise PermissionError("debug root authority is closed")
            grant = self._debug_root_grants.get(key)
            if grant is None:
                self._bind_debug_root(Path(path).parent)
                grant = self._debug_root_grants[key]
            return grant

    def close(self) -> None:
        with self._debug_root_close_lock:
            if self._debug_root_closed:
                return
            self._debug_root_closed = True
            authorities = list(self._debug_root_authorities.values())
            self._debug_root_authorities.clear()
            self._debug_root_grants.clear()
        for authority in authorities:
            authority.close()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass

    def _require_session_token(self, request: Request) -> None:
        if self._has_valid_session_token(request):
            return
        if self._same_origin_media_element(request):
            return
        raise HTTPException(status_code=403, detail="缺少或无效的会话令牌")

    def _same_origin_media_element(self, request: Request) -> bool:
        """`<video>` 发不出自定义头。只接受本源媒体元素，并核对 HttpOnly 会话 Cookie。"""

        site = str(request.headers.get("sec-fetch-site") or "").strip().lower()
        dest = str(request.headers.get("sec-fetch-dest") or "").strip().lower()
        if site != "same-origin" or dest not in {"video", "audio", "image"}:
            return False
        context = self._get_request_context(request)
        cookie = str(request.cookies.get("ucrawl_session") or "")
        session_id = str(context.session_id or "")
        if not cookie or len(cookie) != len(session_id):
            return False
        return secrets.compare_digest(cookie, session_id)

    def _guess_media_type(self, path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        if ext in self.MEDIA_TYPE_OVERRIDES:
            return self.MEDIA_TYPE_OVERRIDES[ext]
        content_type, _ = mimetypes.guess_type(path)
        return content_type or "application/octet-stream"

    async def get_media(self, request: Request, video_id: str, range_header: str | None, *, require_session_token: bool = True):
        if require_session_token:
            self._require_session_token(request)
        context = self._get_request_context(request)
        path = context.controller.get_media_path(video_id)
        if not path:
            raise HTTPException(status_code=404, detail="file not found")
        try:
            content_type = self._guess_media_type(path)
            lease = await self._open_session_approved_file_async(
                context,
                path,
            )
            file_size = lease.size
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="file not found") from None
        except (PermissionError, OSError):
            raise HTTPException(
                status_code=403,
                detail="media path is not authorized",
            ) from None

        primary: BaseException | None = None
        try:
            effective_range_header = range_header or request.headers.get("range")
            if effective_range_header:
                parsed_range = self._parse_byte_range(effective_range_header, file_size)
                if parsed_range is None:
                    lease.close()
                    return Response(
                        status_code=416,
                        headers={
                            "Content-Range": f"bytes */{file_size}",
                            "Accept-Ranges": "bytes",
                        },
                    )
                start, end = parsed_range
                chunk_size = end - start + 1
                return _LeasedStreamingResponse(
                    lease,
                    lease.iter_bytes(start=start, length=chunk_size),
                    status_code=206,
                    media_type=content_type,
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{file_size}",
                        "Accept-Ranges": "bytes",
                        "Content-Length": str(chunk_size),
                    },
                )

            return _LeasedStreamingResponse(
                lease,
                lease.iter_bytes(length=file_size),
                media_type=content_type,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(file_size),
                },
            )
        except BaseException as exc:
            primary = exc
            raise
        finally:
            if primary is not None:
                lease.close(primary)

    @staticmethod
    def _parse_byte_range(value: str, file_size: int) -> tuple[int, int] | None:
        """解析媒体播放器支持的单段字节范围。"""
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", str(value).strip())
        if match is None or file_size <= 0:
            return None
        start_text, end_text = match.groups()
        if (
            len(start_text) > _MAX_RANGE_INTEGER_DIGITS
            or len(end_text) > _MAX_RANGE_INTEGER_DIGITS
        ):
            return None
        if not start_text:
            if not end_text:
                return None
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return None
            return max(0, file_size - suffix_length), file_size - 1
        start = int(start_text)
        requested_end = int(end_text) if end_text else file_size - 1
        if start >= file_size or requested_end < start:
            return None
        return start, min(requested_end, file_size - 1)

    async def _open_approved_file_async(
        self,
        path: str,
        approved_root_grants,
    ) -> ApprovedFileLease:
        open_worker = asyncio.get_running_loop().run_in_executor(
            None,
            self._path_policy.open_approved_file,
            path,
            approved_root_grants,
        )
        try:
            return await asyncio.shield(open_worker)
        except asyncio.CancelledError:
            open_worker.add_done_callback(self._close_late_open_result)
            raise

    async def _open_session_approved_file_async(
        self,
        context: object,
        path: str,
    ) -> ApprovedFileLease:
        lease_factory = getattr(
            context,
            "lease_approved_root_grants_snapshot",
            None,
        )
        if not callable(lease_factory):
            raise PermissionError("目录未被当前会话授权访问")

        session_lease = lease_factory()
        try:
            approved_root_grants = session_lease.__enter__()
        except (AttributeError, TypeError):
            raise PermissionError("目录未被当前会话授权访问") from None

        try:
            open_worker = asyncio.get_running_loop().run_in_executor(
                None,
                self._path_policy.open_approved_file,
                path,
                approved_root_grants,
            )
        except BaseException as primary:
            session_lease.__exit__(
                type(primary),
                primary,
                primary.__traceback__,
            )
            raise

        try:
            opened = await asyncio.shield(open_worker)
        except asyncio.CancelledError as primary:
            def finish_cancelled_open(
                worker,
                retained_lease=session_lease,
                retained_primary=primary,
            ) -> None:
                self._finish_cancelled_session_open(
                    worker,
                    retained_lease,
                    retained_primary,
                )

            open_worker.add_done_callback(finish_cancelled_open)
            raise
        except BaseException as primary:
            session_lease.__exit__(
                type(primary),
                primary,
                primary.__traceback__,
            )
            raise

        try:
            session_lease.__exit__(None, None, None)
        except BaseException as primary:
            opened.close(primary)
            raise
        return opened

    @staticmethod
    def _finish_cancelled_session_open(
        worker,
        session_lease,
        primary: asyncio.CancelledError,
    ) -> None:
        try:
            opened = worker.result()
        except BaseException:
            opened = None
        if opened is not None:
            try:
                opened.close(primary)
            except BaseException:
                pass
        try:
            session_lease.__exit__(
                type(primary),
                primary,
                primary.__traceback__,
            )
        except BaseException:
            pass

    @staticmethod
    def _close_late_open_result(worker) -> None:
        try:
            opened = worker.result()
        except BaseException:
            return
        try:
            opened.close()
        except BaseException:
            return

    @staticmethod
    def _full_lease_response(
        lease: ApprovedFileLease,
        *,
        media_type: str,
        download_name: str | None = None,
    ) -> _LeasedStreamingResponse:
        headers = {"Content-Length": str(lease.size)}
        if download_name is not None:
            headers["Content-Disposition"] = (
                f'attachment; filename="{download_name}"'
            )
        try:
            return _LeasedStreamingResponse(
                lease,
                lease.iter_bytes(length=lease.size),
                media_type=media_type,
                headers=headers,
            )
        except BaseException as primary:
            try:
                lease.close(primary)
            except BaseException:
                pass
            raise

    async def _async_debug_artifact_response(
        self,
        path: str,
        *,
        media_type: str,
        download_name: str,
    ):
        try:
            lease = await self._open_approved_file_async(
                path,
                (self._debug_grant_for(path),),
            )
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="debug artifact not found") from None
        except (PermissionError, OSError):
            raise HTTPException(
                status_code=403,
                detail="debug artifact is not authorized",
            ) from None
        return self._full_lease_response(
            lease,
            media_type=media_type,
            download_name=download_name,
        )

    def _debug_artifact_response(
        self,
        path: str,
        *,
        media_type: str,
        download_name: str,
    ):
        try:
            lease = self._path_policy.open_approved_file(
                path,
                (self._debug_grant_for(path),),
            )
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="debug artifact not found") from None
        except (PermissionError, OSError):
            raise HTTPException(
                status_code=403,
                detail="debug artifact is not authorized",
            ) from None
        return self._full_lease_response(
            lease,
            media_type=media_type,
            download_name=download_name,
        )

    async def async_latest_log_response(self, request: Request):
        self._require_session_token(request)
        return await self._async_debug_artifact_response(
            self._debug_artifacts_service.latest_log_path(),
            media_type="text/plain",
            download_name="latest_debug.log",
        )

    def latest_log_response(self, request: Request):
        self._require_session_token(request)
        return self._debug_artifact_response(
            self._debug_artifacts_service.latest_log_path(),
            media_type="text/plain",
            download_name="latest_debug.log",
        )

    async def async_latest_error_summary_response(self, request: Request):
        self._require_session_token(request)
        return await self._async_debug_artifact_response(
            self._debug_artifacts_service.latest_error_summary_path(),
            media_type="text/markdown",
            download_name="latest_error_summary.md",
        )

    def latest_error_summary_response(self, request: Request):
        self._require_session_token(request)
        return self._debug_artifact_response(
            self._debug_artifacts_service.latest_error_summary_path(),
            media_type="text/markdown",
            download_name="latest_error_summary.md",
        )
