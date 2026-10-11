"""Failure-isolated shutdown orchestration for the Web controller."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.debug_logger import debug_logger


_FAILED = object()


def shutdown_web_controller(host: Any) -> None:
    """Attempt every shutdown step and preserve the first fatal failure."""

    fatal_failures: list[BaseException] = []

    def attempt(
        action: str,
        callback: Callable[[], Any],
        *,
        propagate_exceptions: bool = False,
    ) -> Any:
        try:
            return callback()
        except BaseException as exc:
            _log_exception_safely(action, exc)
            if propagate_exceptions or not isinstance(exc, Exception):
                fatal_failures.append(exc)
            return _FAILED

    attempt(
        "stop_media_directory_watch",
        lambda: host._stop_media_directory_watch(),
        propagate_exceptions=True,
    )
    frontend_state_service = attempt(
        "resolve_frontend_state_service",
        lambda: getattr(host, "frontend_state_service", None),
        propagate_exceptions=True,
    )
    lifecycle: dict[str, Any] = {}
    attempt(
        "begin_lifecycle",
        lambda: _begin_lifecycle_shutdown(host, lifecycle),
        propagate_exceptions=True,
    )
    attempt(
        "stop_spider",
        lambda: _stop_spider(lifecycle.get("spider")),
        propagate_exceptions=True,
    )
    attempt(
        "stop_download_manager",
        lambda: _stop_download_manager(lifecycle.get("manager")),
        propagate_exceptions=True,
    )

    if frontend_state_service is not _FAILED:
        drained = attempt(
            "shutdown_frontend_state_service",
            lambda: _destroy_frontend_state(frontend_state_service),
        )
        if drained is False:
            _log_pending_safely()
        attempt(
            "shutdown_app_state",
            lambda: _shutdown_nested_service(
                frontend_state_service,
                "app_state",
                "shutdown",
            ),
        )
        attempt(
            "shutdown_cache_service",
            lambda: _shutdown_nested_service(
                frontend_state_service,
                "cache_service",
                "close",
            ),
        )

    attempt(
        "reset_lifecycle",
        lambda: _reset_lifecycle_shutdown(host),
        propagate_exceptions=True,
    )
    if fatal_failures:
        raise fatal_failures[0]


def _begin_lifecycle_shutdown(host: Any, lifecycle: dict[str, Any]) -> None:
    def capture() -> None:
        failures: list[BaseException] = []
        _lifecycle_step(failures, lambda: setattr(host, "_is_shutting_down", True))
        spider = _lifecycle_step(failures, lambda: host.current_spider)
        if spider is not _FAILED:
            lifecycle["spider"] = spider
        _lifecycle_step(failures, lambda: setattr(host, "current_spider", None))
        _lifecycle_step(
            failures,
            lambda: setattr(host, "_pending_selection_strategy", None),
        )
        manager = _lifecycle_step(failures, lambda: host._dl_manager)
        if manager is not _FAILED:
            lifecycle["manager"] = manager
        if failures:
            raise failures[0]

    _run_lifecycle_condition(host, capture)


def _stop_spider(spider: Any) -> None:
    if spider is None or not spider.isRunning():
        return
    spider.stop()
    spider.wait(2000)


def _stop_download_manager(manager: Any) -> None:
    if manager is not None:
        manager.stop_all()


def _destroy_frontend_state(frontend_state_service: Any) -> Any:
    destroy = getattr(frontend_state_service, "destroy", None)
    if callable(destroy):
        return destroy()
    return True


def _shutdown_nested_service(owner: Any, attribute: str, action: str) -> None:
    service = getattr(owner, attribute, None)
    callback = getattr(service, action, None)
    if callable(callback):
        callback()


def _reset_lifecycle_shutdown(host: Any) -> None:
    def reset() -> None:
        failures: list[BaseException] = []
        _lifecycle_step(failures, lambda: setattr(host, "_is_shutting_down", False))
        _lifecycle_step(failures, lambda: host._lifecycle_condition.notify_all())
        if failures:
            raise failures[0]

    _run_lifecycle_condition(host, reset)


def _run_lifecycle_condition(host: Any, callback: Callable[[], None]) -> None:
    failures: list[BaseException] = []
    condition = _lifecycle_step(failures, lambda: host._lifecycle_condition)
    entered = False
    if condition is not _FAILED:
        result = _lifecycle_step(failures, lambda: condition.__enter__())
        entered = result is not _FAILED
    _lifecycle_step(failures, callback)
    if entered:
        _lifecycle_step(
            failures,
            lambda: condition.__exit__(None, None, None),
        )
    if failures:
        raise failures[0]


def _lifecycle_step(
    failures: list[BaseException],
    callback: Callable[[], Any],
) -> Any:
    try:
        return callback()
    except BaseException as exc:
        failures.append(exc)
        return _FAILED


def _log_exception_safely(action: str, exc: BaseException) -> None:
    try:
        debug_logger.log_exception("WebController", action, exc)
    except BaseException:
        return


def _log_pending_safely() -> None:
    try:
        debug_logger.log(
            component="WebController",
            action="shutdown.frontend_state_service_pending",
            level="WARN",
            message=(
                "Failed-record persistence is still draining after the shutdown wait "
                "expired"
            ),
            status_code="WEB_FRONTEND_STATE_SHUTDOWN_PENDING",
        )
    except BaseException:
        return


__all__ = ["shutdown_web_controller"]
