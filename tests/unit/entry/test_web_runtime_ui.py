"""Web 入口端口对话框与托盘组件的离屏 Qt 行为测试。"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def qt_app():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _disable_entry_branding(monkeypatch: pytest.MonkeyPatch) -> None:
    from entry import web_port_dialog

    monkeypatch.setattr(web_port_dialog, "load_qt_icon", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        web_port_dialog,
        "ensure_windows_app_user_model_id",
        lambda *_args, **_kwargs: None,
    )


def test_port_dialog_returns_default_without_constructing_conflict_dialog(
    qt_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from entry.web_port_dialog import resolve_port_with_dialog

    _disable_entry_branding(monkeypatch)
    probes: list[tuple[str, int]] = []

    result = resolve_port_with_dialog(
        8000,
        host="127.0.0.1",
        is_port_in_use=lambda host, port: probes.append((host, port)) or False,
        port_probe_range=5,
    )

    assert result == 8000
    assert probes == [("127.0.0.1", 8000)]


def test_port_dialog_accepts_first_suggested_available_port(
    qt_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PyQt6.QtWidgets import QDialog

    from entry.web_port_dialog import resolve_port_with_dialog

    _disable_entry_branding(monkeypatch)
    monkeypatch.setattr(QDialog, "exec", lambda _dialog: QDialog.DialogCode.Accepted)
    probes: list[int] = []
    hosts: list[str] = []

    def is_port_in_use(host: str, port: int) -> bool:
        hosts.append(host)
        probes.append(port)
        return port == 8000

    assert resolve_port_with_dialog(
        8000,
        host="192.168.1.25",
        is_port_in_use=is_port_in_use,
        port_probe_range=3,
    ) == 8001
    assert probes == [8000, 8001, 8001]
    assert hosts == ["192.168.1.25", "192.168.1.25", "192.168.1.25"]


def test_port_dialog_cancel_exits_without_starting_server(
    qt_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PyQt6.QtWidgets import QDialog

    from entry.web_port_dialog import resolve_port_with_dialog

    _disable_entry_branding(monkeypatch)
    monkeypatch.setattr(QDialog, "exec", lambda _dialog: QDialog.DialogCode.Rejected)

    with pytest.raises(SystemExit) as exc_info:
        resolve_port_with_dialog(
            8000,
            host="127.0.0.1",
            is_port_in_use=lambda _host, port: port == 8000,
            port_probe_range=2,
        )

    assert exc_info.value.code == 0


def test_tray_menu_opens_browser_and_signals_shutdown(
    qt_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PyQt6.QtWidgets import QSystemTrayIcon

    from entry import web_tray_ui

    project_root = Path(__file__).resolve().parents[3]
    opened: list[str] = []
    shutdown_event = Mock()
    monkeypatch.setattr(web_tray_ui, "resolve_icon_path", lambda *_args, **_kwargs: project_root / "Web.ico")
    monkeypatch.setattr(web_tray_ui.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(QSystemTrayIcon, "show", lambda _tray: None)
    monkeypatch.setattr(QSystemTrayIcon, "showMessage", lambda *_args, **_kwargs: None)

    tray = web_tray_ui.create_tray_icon(qt_app, "http://127.0.0.1:8000", shutdown_event)
    actions = [action for action in tray.contextMenu().actions() if not action.isSeparator()]
    actions[0].trigger()
    actions[1].trigger()
    tray.activated.emit(QSystemTrayIcon.ActivationReason.DoubleClick)

    assert tray.toolTip() == "UCrawl Web - http://127.0.0.1:8000"
    assert opened == ["http://127.0.0.1:8000", "http://127.0.0.1:8000"]
    shutdown_event.set.assert_called_once_with()
    tray.deleteLater()


def test_qt_server_thread_is_not_daemon_and_startup_failure_is_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from entry import web_launch_runtime

    captured: dict[str, threading.Thread] = {}
    real_thread = threading.Thread

    class _Thread(real_thread):
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs
            super().__init__(*args, **kwargs)
            captured["thread"] = self

    class _Timer:
        def __init__(self, *_args, **_kwargs):
            self.timeout = Mock()

        def start(self, *_args, **_kwargs) -> None:
            return None

    monkeypatch.setattr(web_launch_runtime.threading, "Thread", _Thread)
    monkeypatch.setattr("PyQt6.QtCore.QTimer", _Timer)

    async def serve_async() -> None:
        raise SystemExit(1)

    qt_app = Mock()

    def _exec() -> int:
        captured["thread"].join(timeout=2)
        return 0

    qt_app.exec.side_effect = _exec
    shutdown_event = threading.Event()

    code = web_launch_runtime.run_server_with_qt(
        qt_app,
        url="http://127.0.0.1:8000",
        shutdown_event=shutdown_event,
        serve_async=serve_async,
        create_tray_icon=lambda *_args, **_kwargs: Mock(),
        request_shutdown=lambda: shutdown_event.set(),
    )

    assert captured["kwargs"]["daemon"] is False
    assert code != 0


def test_qt_server_join_timeout_does_not_return_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import threading

    from entry import web_launch_runtime

    monkeypatch.setattr(web_launch_runtime, "_QT_SERVER_JOIN_TIMEOUT", 0.01)
    monkeypatch.setattr(web_launch_runtime, "_QT_SERVER_FORCE_JOIN_TIMEOUT", 0.01)
    captured: dict[str, threading.Thread] = {}
    real_thread = threading.Thread

    class _Thread(real_thread):
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs
            super().__init__(*args, **kwargs)
            captured["thread"] = self

    class _Timer:
        def __init__(self, *_args, **_kwargs):
            self.timeout = Mock()

        def start(self, *_args, **_kwargs) -> None:
            return None

    monkeypatch.setattr(web_launch_runtime.threading, "Thread", _Thread)
    monkeypatch.setattr("PyQt6.QtCore.QTimer", _Timer)

    release = threading.Event()

    async def serve_async() -> None:
        while not release.is_set():
            await asyncio.sleep(0.01)

    qt_app = Mock()
    qt_app.exec.return_value = 0
    try:
        code = web_launch_runtime.run_server_with_qt(
            qt_app,
            url="http://127.0.0.1:8000",
            shutdown_event=threading.Event(),
            serve_async=serve_async,
            create_tray_icon=lambda *_args, **_kwargs: None,
            request_shutdown=lambda: None,
        )
        assert code != 0
        assert captured["kwargs"]["daemon"] is False
    finally:
        release.set()
        captured["thread"].join(timeout=2)


def test_script_shutdown_callback_records_exit_code_without_hard_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI

    from entry import web_entry

    class _Listener:
        def close(self) -> None:
            return None

    class _Server:
        def __init__(self, config):
            self.app = config.app
            self.should_exit = False
            self.force_exit = False

        async def serve(self) -> None:
            self.app.state.web_shutdown_callback(7)
            async with self.app.router.lifespan_context(self.app):
                return None

    fake_uvicorn = Mock()
    fake_uvicorn.Config.side_effect = lambda app, **_kwargs: Mock(app=app)
    fake_uvicorn.Server.side_effect = _Server

    monkeypatch.setattr(web_entry, "_is_port_in_use", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(web_entry, "_reserve_listener", lambda *_args, **_kwargs: _Listener())
    monkeypatch.setattr(web_entry.signal, "signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.web.server.controller", None, raising=False)
    monkeypatch.setattr(
        "app.web.server.create_app",
        lambda *, lifespan, access_token=None: FastAPI(lifespan=lifespan),
    )
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake_uvicorn)

    assert web_entry.main(["--no-qt", "--no-browser", "--host", "127.0.0.1", "--port", "9"]) == 7


def test_script_strict_passes_the_app_into_inject(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI

    from entry import web_entry

    class _Listener:
        def close(self) -> None:
            return None

    class _Server:
        def __init__(self, config):
            self.app = config.app

        async def serve(self) -> None:
            async with self.app.router.lifespan_context(self.app):
                return None

    captured: dict[str, object] = {}

    def _inject(*_args, **kwargs):
        captured.update(kwargs)
        return Mock()

    fake_uvicorn = Mock()
    fake_uvicorn.Config.side_effect = lambda app, **_kwargs: Mock(app=app)
    fake_uvicorn.Server.side_effect = _Server

    monkeypatch.setattr(web_entry, "_is_port_in_use", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(web_entry, "_reserve_listener", lambda *_args, **_kwargs: _Listener())
    monkeypatch.setattr(web_entry.signal, "signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.web.server.controller", None, raising=False)
    monkeypatch.setattr(
        "app.web.server.create_app",
        lambda *, lifespan, access_token=None: FastAPI(lifespan=lifespan),
    )
    monkeypatch.setattr("app.web.script_api.inject_script_async", _inject)
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake_uvicorn)

    assert web_entry.main(
        ["--no-qt", "--no-browser", "--host", "127.0.0.1", "--port", "9", "--script", "demo.py", "--script-strict"]
    ) == 0
    assert captured["strict"] is True
    assert captured["app"] is not None
    assert callable(captured["app"].state.web_shutdown_callback)


def test_undrained_shutdown_is_not_a_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI

    from entry import web_entry

    class _Listener:
        def close(self) -> None:
            return None

    class _Registry:
        def shutdown_all(self, *, wait: bool, timeout: float) -> bool:
            assert wait is True
            assert timeout == 5.0
            return False

    class _Server:
        def __init__(self, config):
            self.app = config.app

        async def serve(self) -> None:
            async with self.app.router.lifespan_context(self.app):
                return None

    fake_uvicorn = Mock()
    fake_uvicorn.Config.side_effect = lambda app, **_kwargs: Mock(app=app)
    fake_uvicorn.Server.side_effect = _Server

    def _create_app(*, lifespan, access_token=None):
        app = FastAPI(lifespan=lifespan)
        app.state.web_session_registry = _Registry()
        return app

    monkeypatch.setattr(web_entry, "_is_port_in_use", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(web_entry, "_reserve_listener", lambda *_args, **_kwargs: _Listener())
    monkeypatch.setattr(web_entry.signal, "signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.web.server.controller", None, raising=False)
    monkeypatch.setattr("app.web.server.create_app", _create_app)
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake_uvicorn)

    assert web_entry.main(["--no-qt", "--no-browser", "--host", "127.0.0.1", "--port", "9"]) != 0
