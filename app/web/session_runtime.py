"""Web 会话运行时：会话上下文、鉴权令牌与目录授权。"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import os
import ipaddress
import secrets
import threading
import time
import asyncio
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from app.services.path_policy import ApprovedRootGrant
from shared.execution_profile import ExecutionProfile, public_web_profile
from shared.filesystem_directory_capability import (
    FilesystemDirectoryCapability,
    ensure_regular_directory,
    open_filesystem_directory_capability,
)

SendFactory = Callable[[str], Callable[[str, Any], Any]]
TEST_TRANSPORT_HOSTS = {"testserver", "testclient"}
DEFAULT_DISPOSAL_WORKERS = 2
DEFAULT_DISPOSAL_QUEUE_CAPACITY = 8


@dataclass
class _SessionRootAuthority:
    grant: ApprovedRootGrant
    capability: FilesystemDirectoryCapability
    references: int = 0
    retired: bool = False


def _attach_cleanup_note(error: BaseException, note: str) -> None:
    try:
        add_note = getattr(error, "add_note", None)
        if callable(add_note):
            add_note(note)
    except BaseException:
        pass


def _preferred_failure(
    current: BaseException | None,
    candidate: BaseException,
) -> BaseException:
    if current is None:
        return candidate
    if isinstance(current, Exception) and not isinstance(candidate, Exception):
        _attach_cleanup_note(candidate, "an earlier session cleanup step also failed")
        return candidate
    _attach_cleanup_note(current, "an additional session cleanup step failed")
    return current


class WebSessionCapacityError(RuntimeError):
    """Raised when a new Web session cannot be admitted without exceeding bounds."""


def build_public_web_session_profile(
    session_id: str,
    *,
    approved_roots: Iterable[str | Path] = (),
) -> ExecutionProfile:
    """Build one canonical identity for every capability owned by a Web session."""

    return public_web_profile(
        owner_id=f"web:{session_id}",
        approved_roots=(Path(root) for root in approved_roots),
    )

def normalize_directory(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))

def is_within_root(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False

def normalize_origin(origin: str) -> str:
    parts = urlsplit(origin)
    if not parts.scheme or not parts.hostname:
        raise ValueError("invalid origin")
    host = parts.hostname.lower()
    port = parts.port
    default_port = 443 if parts.scheme == "https" else 80
    if port is None or port == default_port:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"

def configured_allowed_origins() -> set[str]:
    raw = os.getenv("UCRAWL_ALLOWED_ORIGINS", "")
    origins: set[str] = set()
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            origins.add(normalize_origin(candidate))
        except ValueError:
            continue
    return origins

def is_loopback_host(host: str | None) -> bool:
    normalized = (host or "").strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def is_local_host(host: str | None) -> bool:
    normalized = (host or "").strip().lower()
    return is_loopback_host(normalized) or normalized in TEST_TRANSPORT_HOSTS


def is_loopback_request_host(host: str | None, client_host: str | None) -> bool:
    """仅为 TestClient 的模拟客户端放行对应的模拟主机名。"""
    normalized_host = (host or "").strip().lower()
    normalized_client = (client_host or "").strip().lower()
    return is_loopback_host(normalized_host) or (
        normalized_host in TEST_TRANSPORT_HOSTS
        and normalized_client in TEST_TRANSPORT_HOSTS
    )

def is_allowed_origin(origin: str | None, *, expected_origin: str | None = None) -> bool:
    if not origin:
        return False
    try:
        normalized_origin = normalize_origin(origin)
    except ValueError:
        return False
    if expected_origin and normalized_origin == normalize_origin(expected_origin):
        return True
    return normalized_origin in configured_allowed_origins()

class WebSessionContext:
    def __init__(
        self,
        session_id: str,
        *,
        send_factory: SendFactory,
        controller_factory: Callable[[Any, Callable[[str, Any], Any]], Any],
        workflow_factory: Callable[[Any, Callable[[str, Any], Any]], Any],
    ) -> None:
        # `controller`、`workflow`、令牌和授权目录均归当前会话，不能跨会话复用。
        self.session_id = session_id
        self.send = send_factory(session_id)
        # 不在 create_app 时获取事件循环，因为 uvicorn 可能使用不同的事件循环
        # 传入 None，在首次 emit 时延迟获取正确的事件循环
        self.controller = controller_factory(None, self.send)
        self.workflow = workflow_factory(self.controller, self.send)
        self.session_token = secrets.token_urlsafe(24)
        self.csrf_token = secrets.token_urlsafe(24)
        self.approved_roots: set[str] = set()
        self._approved_root_authorities: dict[str, _SessionRootAuthority] = {}
        self._approved_roots_closed = False
        self._approved_roots_lock = threading.RLock()
        self._prepared_update_lock = threading.RLock()
        self._prepared_update: Any = None
        self.background_tasks: set[asyncio.Task] = set()
        self._background_tasks_lock = threading.RLock()
        self._access_lock = threading.RLock()
        self._monotonic: Callable[[], float] = time.monotonic
        self._active_http_requests = 0
        self._active_websockets = 0
        self.last_access_at = self._monotonic()
        try:
            self._approve_directory(self.controller.current_save_dir, create=True)
            self._bind_tool_execution_profile_provider()
        except BaseException as primary_error:
            selected_error = self._retire_approved_root_authorities(primary_error)
            if selected_error is not primary_error:
                raise selected_error
            raise

    def touch(self) -> None:
        with self._access_lock:
            self.last_access_at = self._monotonic()

    def mark_websocket_connected(self) -> None:
        with self._access_lock:
            self._active_websockets += 1
            self.last_access_at = self._monotonic()

    def mark_http_request_started(self) -> None:
        now = self._monotonic()
        with self._access_lock:
            self._active_http_requests += 1
            self.last_access_at = now

    def mark_http_request_finished(self) -> None:
        with self._access_lock:
            if self._active_http_requests <= 0:
                raise RuntimeError("HTTP request reference underflow")
            self._active_http_requests -= 1
            self.last_access_at = self._monotonic()

    def has_active_http_request(self) -> bool:
        with self._access_lock:
            return self._active_http_requests > 0

    def has_active_request(self) -> bool:
        with self._access_lock:
            return self._active_http_requests > 0 or self._active_websockets > 0

    def mark_websocket_disconnected(self) -> None:
        with self._access_lock:
            self._active_websockets = max(0, self._active_websockets - 1)
            self.last_access_at = self._monotonic()

    def has_active_websocket(self) -> bool:
        with self._access_lock:
            return self._active_websockets > 0

    def track_background_task(self, task: asyncio.Task) -> asyncio.Task:
        with self._background_tasks_lock:
            self.background_tasks.add(task)

        def _discard(done_task: asyncio.Task) -> None:
            with self._background_tasks_lock:
                self.background_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logging.getLogger(__name__).exception("Web session background task failed")

        task.add_done_callback(_discard)
        return task

    def approve_directory(self, directory: str) -> str:
        """Approve an existing regular directory selected for this session."""
        return self._approve_directory(directory, create=False)

    def _approve_directory(self, directory: str, *, create: bool) -> str:
        lexical_directory = os.path.abspath(
            os.path.normpath(os.path.expanduser(directory))
        )
        with self._approved_roots_lock:
            if self._approved_roots_closed:
                raise PermissionError("Web session directory authority is closed")
        ensure_regular_directory(lexical_directory, create=create)
        capability = open_filesystem_directory_capability(lexical_directory)
        try:
            bound_root, identity = capability.snapshot_bound_identity()
        except BaseException as primary_error:
            selected_error = self._close_root_authorities(
                (_SessionRootAuthority(("", (0, 0)), capability),),
                primary_error,
            )
            if selected_error is not primary_error:
                raise selected_error
            raise
        grant_path = os.path.normcase(os.path.abspath(os.fspath(bound_root)))
        grant: ApprovedRootGrant = (
            grant_path,
            (int(identity[0]), int(identity[1])),
        )
        normalized = normalize_directory(grant_path)
        authority = _SessionRootAuthority(grant, capability)
        retired: tuple[_SessionRootAuthority, ...] = ()
        with self._approved_roots_lock:
            if self._approved_roots_closed:
                installation_error = PermissionError(
                    "Web session directory authority is closed"
                )
            else:
                installation_error = None
                previous = self._approved_root_authorities.get(normalized)
                self.approved_roots.add(normalized)
                self._approved_root_authorities[normalized] = authority
                if previous is not None:
                    previous.retired = True
                    if previous.references == 0:
                        retired = (previous,)
        if installation_error is not None:
            selected_error = self._close_root_authorities(
                (authority,), installation_error
            )
            raise selected_error
        selected_error = self._close_root_authorities(retired)
        if selected_error is not None:
            raise selected_error
        return normalized

    @staticmethod
    def _close_root_authorities(
        authorities: Iterable[_SessionRootAuthority],
        primary_error: BaseException | None = None,
    ) -> BaseException | None:
        selected_error = primary_error
        for authority in authorities:
            try:
                authority.capability.close()
            except BaseException as cleanup_error:
                selected_error = _preferred_failure(selected_error, cleanup_error)
        return selected_error

    def _retire_approved_root_authorities(
        self,
        primary_error: BaseException | None = None,
    ) -> BaseException | None:
        with self._approved_roots_lock:
            self._approved_roots_closed = True
            authorities = tuple(self._approved_root_authorities.values())
            self._approved_root_authorities.clear()
            self.approved_roots.clear()
            for authority in authorities:
                authority.retired = True
            closable = tuple(
                authority for authority in authorities if authority.references == 0
            )
        return self._close_root_authorities(closable, primary_error)

    def close_approved_root_authorities(self) -> None:
        selected_error = self._retire_approved_root_authorities()
        if selected_error is not None:
            raise selected_error

    def _release_root_authorities(
        self,
        authorities: tuple[_SessionRootAuthority, ...],
        primary_error: BaseException | None,
    ) -> BaseException | None:
        selected_error = primary_error
        closable: list[_SessionRootAuthority] = []
        with self._approved_roots_lock:
            for authority in authorities:
                if authority.references <= 0:
                    selected_error = _preferred_failure(
                        selected_error,
                        RuntimeError("session directory lease reference underflow"),
                    )
                    continue
                authority.references -= 1
                if authority.retired and authority.references == 0:
                    closable.append(authority)
        return self._close_root_authorities(closable, selected_error)

    @contextmanager
    def lease_approved_root_grants_snapshot(
        self,
    ) -> Iterator[tuple[ApprovedRootGrant, ...]]:
        with self._approved_roots_lock:
            if self._approved_roots_closed:
                raise PermissionError("目录未被当前会话授权访问")
            authorities = tuple(self._approved_root_authorities.values())
            if not authorities:
                raise PermissionError("目录未被当前会话授权访问")
            for authority in authorities:
                authority.references += 1
            grants = tuple(authority.grant for authority in authorities)

        primary_error: BaseException | None = None
        try:
            try:
                for authority in authorities:
                    authority.capability.assert_bound()
            except (OSError, TypeError, ValueError):
                raise PermissionError("目录未被当前会话授权访问") from None
            yield grants
        except BaseException as error:
            primary_error = error
            raise
        finally:
            selected_error = self._release_root_authorities(
                authorities,
                primary_error,
            )
            if selected_error is not None and selected_error is not primary_error:
                raise selected_error

    def _bind_tool_execution_profile_provider(self) -> None:
        frontend_state_service = getattr(
            self.controller,
            "frontend_state_service",
            None,
        )
        bind_provider = getattr(
            frontend_state_service,
            "set_tool_execution_profile_provider",
            None,
        )
        if not callable(bind_provider):
            return

        def profile_provider():
            return build_public_web_session_profile(self.session_id)

        bind_provider(profile_provider)

    def is_directory_allowed(self, directory: str) -> bool:
        normalized = normalize_directory(directory)
        roots = self.approved_roots_snapshot()
        return any(is_within_root(normalized, root) for root in roots)

    def approved_roots_snapshot(self) -> tuple[str, ...]:
        """Return currently bound roots for non-authorizing diagnostics."""
        with self._approved_roots_lock:
            pairs = tuple(
                (root, self._approved_root_authorities.get(root))
                for root in self.approved_roots
            )
        valid_roots: list[str] = []
        for root, authority in pairs:
            if authority is None:
                continue
            try:
                authority.capability.assert_bound()
            except (OSError, TypeError, ValueError):
                continue
            valid_roots.append(root)
        return tuple(valid_roots)

    def approved_root_grants_snapshot(self) -> tuple[ApprovedRootGrant, ...]:
        """Return diagnostic grants; authorizing operations must use the lease API."""
        with self._approved_roots_lock:
            return tuple(
                authority.grant
                for authority in self._approved_root_authorities.values()
            )

    def store_prepared_update(self, prepared: Any) -> None:
        with self._prepared_update_lock:
            self._prepared_update = prepared

    def prepared_update_snapshot(self) -> Any:
        with self._prepared_update_lock:
            return self._prepared_update

    def take_prepared_update(self) -> Any:
        """以原子方式取走已校验的更新包，确保它只能启动一次。"""
        with self._prepared_update_lock:
            prepared = self._prepared_update
            self._prepared_update = None
            return prepared

    def clear_prepared_update(self) -> None:
        with self._prepared_update_lock:
            self._prepared_update = None

    def require_directory(self, directory: str) -> str:
        normalized = normalize_directory(directory)
        if not self.is_directory_allowed(normalized):
            raise PermissionError("目录未被当前会话授权访问")
        return normalized

class WebSessionRegistry:
    """管理会话上下文及其最终关闭。

    `pinned_session_ids` 中的固定会话和存在活动 WebSocket 的会话不参与 TTL 或容量淘汰，
    因此 `max_contexts` 是上下文总量的软目标，受保护会话可使总量超过该值。其余会话
    超过 `idle_ttl_seconds` 后可被清理，容量溢出时按最久未访问顺序淘汰。注册表先摘除
    上下文并取消其工作流和后台任务，再在线程池调用控制器的 `shutdown()`；
    `shutdown_all()` 取得包括固定会话和活动会话在内的最终关闭所有权。
    """
    def __init__(
        self,
        *,
        send_factory: SendFactory,
        controller_factory: Callable[[Any, Callable[[str, Any], Any]], Any],
        workflow_factory: Callable[[Any, Callable[[str, Any], Any]], Any],
        max_contexts: int = 64,
        idle_ttl_seconds: float = 30 * 60,
        pinned_session_ids: set[str] | None = None,
        monotonic: Callable[[], float] | None = None,
        disposal_workers: int = DEFAULT_DISPOSAL_WORKERS,
        disposal_queue_capacity: int = DEFAULT_DISPOSAL_QUEUE_CAPACITY,
    ) -> None:
        self._send_factory = send_factory
        self._controller_factory = controller_factory
        self._workflow_factory = workflow_factory
        self._contexts: dict[str, WebSessionContext] = {}
        self._lock = threading.RLock()
        self._max_contexts = max(1, int(max_contexts))
        self._idle_ttl_seconds = max(float(idle_ttl_seconds), 0.0)
        self._pinned_session_ids = set(pinned_session_ids or ())
        self._monotonic = monotonic or time.monotonic
        self._closed = False
        self._disposal_submit_lock = threading.RLock()
        self._disposal_executor = ThreadPoolExecutor(
            max_workers=max(1, int(disposal_workers)),
            thread_name_prefix="web-session-shutdown",
        )
        disposal_capacity = max(1, int(disposal_workers)) + max(0, int(disposal_queue_capacity))
        self._disposal_slots = threading.BoundedSemaphore(disposal_capacity)
        self._disposal_futures: set[Future[Any]] = set()
        self._terminal_disposal_backlog: deque[WebSessionContext] = deque()
        self._disposal_idle = threading.Condition(self._lock)
        self._disposal_executor_closed = False

    def get_or_create(self, session_id: str) -> WebSessionContext:
        return self._get_or_create_context(session_id, acquire_http=False)

    def acquire_http_context(self, session_id: str) -> WebSessionContext:
        """Find or create and reserve an HTTP context in one critical section."""
        return self._get_or_create_context(session_id, acquire_http=True)

    def _get_or_create_context(
        self,
        session_id: str,
        *,
        acquire_http: bool,
    ) -> WebSessionContext:
        if not acquire_http:
            self.prune()
        detached: tuple[str, WebSessionContext] | None = None
        with self._disposal_submit_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Web session registry is shutting down")
                context = self._contexts.get(session_id)
                if context is None:
                    if len(self._contexts) >= self._max_contexts:
                        candidate = self._oldest_evictable_context_locked()
                        if candidate is None or not self._disposal_slots.acquire(
                            blocking=False
                        ):
                            raise WebSessionCapacityError(
                                "Web session capacity is exhausted; retry later"
                            )
                        detached = candidate

                    try:
                        context = WebSessionContext(
                            session_id,
                            send_factory=self._send_factory,
                            controller_factory=self._controller_factory,
                            workflow_factory=self._workflow_factory,
                        )
                    except BaseException:
                        if detached is not None:
                            self._disposal_slots.release()
                        raise
                    context._monotonic = self._monotonic
                    if detached is not None:
                        self._contexts.pop(detached[0], None)
                    self._contexts[session_id] = context

            if detached is not None:
                try:
                    self._submit_reserved_disposal(detached[1])
                except RuntimeError as submit_error:
                    with self._lock:
                        if self._contexts.get(session_id) is context:
                            self._contexts.pop(session_id, None)
                        self._contexts.setdefault(detached[0], detached[1])
                    self._disposal_slots.release()
                    shutdown = getattr(context.controller, "shutdown", None)
                    selected_error = self._dispose_context_resources(
                        context,
                        shutdown if callable(shutdown) else None,
                        submit_error,
                    )
                    if selected_error is not submit_error:
                        raise selected_error
                    raise
            if acquire_http:
                context.mark_http_request_started()
            else:
                context.touch()
            return context

    @staticmethod
    def release_http_context(context: WebSessionContext) -> None:
        context.mark_http_request_finished()

    def acquire_websocket_context(
        self,
        session_id: str,
        session_token: str,
    ) -> WebSessionContext | None:
        """Authenticate and reserve an existing context in one registry critical section."""
        with self._disposal_submit_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Web session registry is shutting down")
                context = self._contexts.get(session_id)
                if context is None or not secrets.compare_digest(
                    str(session_token or ""),
                    context.session_token,
                ):
                    return None
                context.touch()
                context.mark_websocket_connected()
                return context

    def _oldest_evictable_context_locked(self) -> tuple[str, WebSessionContext] | None:
        candidates = (
            (getattr(context, "last_access_at", 0.0), session_id, context)
            for session_id, context in self._contexts.items()
            if session_id not in self._pinned_session_ids
            and not context.has_active_request()
        )
        try:
            _, session_id, context = min(candidates, key=lambda item: item[0])
        except ValueError:
            return None
        return session_id, context

    def prune(self) -> None:
        if self._idle_ttl_seconds <= 0:
            return
        now = self._monotonic()
        with self._lock:
            expired_session_ids = [
                session_id
                for session_id, context in self._contexts.items()
                if session_id not in self._pinned_session_ids
                and not context.has_active_request()
                and now - getattr(context, "last_access_at", now) > self._idle_ttl_seconds
            ]
        idle_cutoff = now - self._idle_ttl_seconds
        for session_id in expired_session_ids:
            self._dispose_context(session_id, idle_cutoff=idle_cutoff)

    def _dispose_context(
        self,
        session_id: str,
        *,
        idle_cutoff: float | None = None,
    ) -> Future[Any] | None:
        with self._disposal_submit_lock:
            with self._lock:
                context = self._contexts.get(session_id)
                if context is None:
                    return None
                with context._access_lock:
                    if (
                        context._active_http_requests > 0
                        or context._active_websockets > 0
                    ):
                        return None
                    if idle_cutoff is not None and context.last_access_at > idle_cutoff:
                        return None
                    if not self._disposal_slots.acquire(blocking=False):
                        return None
                    if self._contexts.get(session_id) is not context:
                        self._disposal_slots.release()
                        return None
                    self._contexts.pop(session_id)
            try:
                return self._submit_reserved_disposal(context)
            except RuntimeError:
                with self._lock:
                    self._contexts.setdefault(session_id, context)
                self._disposal_slots.release()
                raise

    def _submit_reserved_disposal(self, context: WebSessionContext) -> Future[Any] | None:
        controller = getattr(context, "controller", None)
        shutdown = getattr(controller, "shutdown", None)
        if not callable(shutdown):
            shutdown = None

        start_gate = threading.Event()
        future = self._disposal_executor.submit(
            self._shutdown_context_after_gate,
            start_gate,
            context,
            shutdown,
        )
        try:
            with self._lock:
                self._disposal_futures.add(future)
            future.add_done_callback(self._disposal_finished)
            self._cancel_context_activity(context)
        finally:
            start_gate.set()
        return future

    @staticmethod
    def _cancel_context_activity(context: WebSessionContext) -> None:
        workflow = getattr(context, "workflow", None)
        cancel_broadcasts = getattr(workflow, "cancel_pending_broadcasts", None)
        if callable(cancel_broadcasts):
            try:
                cancel_broadcasts()
            except Exception:
                logging.getLogger(__name__).warning(
                    "Web session broadcast cancellation failed",
                    exc_info=True,
                )
        with context._background_tasks_lock:
            tasks = tuple(context.background_tasks)
        for task in tasks:
            if task.done():
                continue
            try:
                task.cancel()
            except Exception:
                logging.getLogger(__name__).warning(
                    "Web session background task cancellation failed",
                    exc_info=True,
                )

    @classmethod
    def _shutdown_context_after_gate(
        cls,
        start_gate: threading.Event,
        context: WebSessionContext,
        shutdown: Callable[[], Any] | None,
    ) -> None:
        start_gate.wait()
        selected_error = cls._dispose_context_resources(context, shutdown)
        if selected_error is not None:
            raise selected_error

    @staticmethod
    def _dispose_context_resources(
        context: WebSessionContext,
        shutdown: Callable[[], Any] | None,
        primary_error: BaseException | None = None,
    ) -> BaseException | None:
        selected_error = primary_error
        if shutdown is not None:
            try:
                shutdown()
            except BaseException as shutdown_error:
                selected_error = _preferred_failure(
                    selected_error,
                    shutdown_error,
                )
        return context._retire_approved_root_authorities(selected_error)

    def _disposal_finished(self, future: Future[Any]) -> None:
        with self._disposal_submit_lock:
            self._disposal_slots.release()
            with self._disposal_idle:
                self._disposal_futures.discard(future)
            self._schedule_terminal_backlog_locked()
            self._close_disposal_executor_if_idle_locked()
            with self._disposal_idle:
                self._disposal_idle.notify_all()

    def _schedule_terminal_backlog_locked(self) -> None:
        while self._terminal_disposal_backlog:
            if not self._disposal_slots.acquire(blocking=False):
                return
            with self._lock:
                context = self._terminal_disposal_backlog.popleft()
            try:
                self._submit_reserved_disposal(context)
            except RuntimeError as submit_error:
                self._disposal_slots.release()
                self._cancel_context_activity(context)
                shutdown = getattr(context.controller, "shutdown", None)
                selected_error = self._dispose_context_resources(
                    context,
                    shutdown if callable(shutdown) else None,
                    submit_error,
                )
                if selected_error is not None and not isinstance(
                    selected_error, Exception
                ):
                    raise selected_error
                logging.getLogger(__name__).warning(
                    "Web session terminal disposal fallback failed",
                    exc_info=selected_error,
                )

    def _close_disposal_executor_if_idle_locked(self) -> None:
        with self._lock:
            idle = not self._disposal_futures and not self._terminal_disposal_backlog
        if not self._closed or not idle or self._disposal_executor_closed:
            return
        self._disposal_executor_closed = True
        self._disposal_executor.shutdown(wait=False, cancel_futures=False)

    def shutdown_all(self, *, wait: bool = False, timeout: float | None = 5.0) -> None:
        """封闭会话注册表，并释放包括固定会话在内的所有上下文。

        常规空闲淘汰会保留固定会话；进程关闭时则必须释放所有控制器
        持有的工作线程和文件句柄，之后解释器才能退出。
        """
        with self._disposal_submit_lock:
            with self._lock:
                self._closed = True
                contexts = tuple(self._contexts.values())
                self._contexts.clear()

            for context in contexts:
                self._cancel_context_activity(context)
            with self._disposal_idle:
                self._terminal_disposal_backlog.extend(contexts)
            self._schedule_terminal_backlog_locked()
            self._close_disposal_executor_if_idle_locked()
        if not wait:
            return

        wait_timeout = None if timeout is None else max(0.0, float(timeout))
        deadline = None if wait_timeout is None else time.monotonic() + wait_timeout
        with self._disposal_idle:
            while self._disposal_futures or self._terminal_disposal_backlog:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._disposal_idle.wait(timeout=remaining)
        with self._disposal_submit_lock:
            self._close_disposal_executor_if_idle_locked()

    @staticmethod
    def _safe_shutdown_controller(shutdown: Callable[[], Any]) -> None:
        try:
            shutdown()
        except Exception:
            logging.getLogger(__name__).warning("Controller shutdown callback failed", exc_info=True)
