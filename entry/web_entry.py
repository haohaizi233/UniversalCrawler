"""UCrawl Web UI 进程入口。

该入口拥有 Web 专属的参数解析、传输与访问令牌校验、端口冲突处理、可选 Qt
托盘/浏览器集成以及 uvicorn 生命周期；FastAPI 应用本身由
``app.web.server.create_app`` 构造。``pyproject.toml`` 将其注册为
``ucrawl-web``。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import ipaddress
import os
import signal
import socket
import sys
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PyQt6.QtGui import QIcon

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 顺延查找的最大尝试次数（找不到就报错/弹窗）
_PORT_PROBE_RANGE = 10


def _validate_transport_security(
    host: str,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
) -> str:
    """服务器可接收其它主机流量时强制使用 TLS。"""
    normalized_host = str(host or "").strip().strip("[]")
    try:
        is_loopback = ipaddress.ip_address(normalized_host).is_loopback
    except ValueError:
        is_loopback = normalized_host.lower() == "localhost"

    has_cert = bool(ssl_certfile)
    has_key = bool(ssl_keyfile)
    if has_cert != has_key:
        raise ValueError("--ssl-certfile and --ssl-keyfile must be provided together")
    if not is_loopback and not (has_cert and has_key):
        raise ValueError("non-loopback Web binding requires HTTPS certificate and key")
    for option, file_path in (("--ssl-certfile", ssl_certfile), ("--ssl-keyfile", ssl_keyfile)):
        if file_path and not Path(file_path).expanduser().is_file():
            raise ValueError(f"{option} does not exist or is not a file: {file_path}")
    return "https" if has_cert and has_key else "http"

def _is_port_in_use(host: str, port: int) -> bool:
    normalized_host = str(host or "").strip().strip("[]")
    try:
        family = socket.AF_INET6 if ipaddress.ip_address(normalized_host).version == 6 else socket.AF_INET
    except ValueError:
        family = socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.bind((normalized_host, port))
            return False
        except OSError:
            return True

def _reserve_listener(host: str, port: int) -> socket.socket | None:
    """绑定并监听最终端口，把套接字占住交给 uvicorn，避免探测后被抢走。"""
    normalized_host = str(host or "").strip().strip("[]")
    try:
        family = socket.AF_INET6 if ipaddress.ip_address(normalized_host).version == 6 else socket.AF_INET
    except ValueError:
        family = socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if family == socket.AF_INET6:
            try:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            except OSError:
                pass
        sock.bind((normalized_host, port))
        sock.listen(socket.SOMAXCONN)
    except OSError:
        sock.close()
        return None
    return sock


def _close_listener(listener: socket.socket | None) -> None:
    if listener is None:
        return
    try:
        listener.close()
    except OSError:
        pass


def _server_accepts_sockets(server: object) -> bool:
    serve = getattr(server, "serve", None)
    try:
        parameters = inspect.signature(serve).parameters
    except (TypeError, ValueError):
        return False
    return "sockets" in parameters


def _find_available_port(host: str, start_port: int, max_probe: int = _PORT_PROBE_RANGE) -> int | None:
    """从 start_port 开始顺延查找可用端口，最多尝试 max_probe 次。

    用于 ``--no-qt`` 模式下的自动顺延。探测 socket 会在每次检查后关闭，
    因而返回值只表示端口在探测时空闲，并不预留该端口；实际监听仍由服务器
    启动阶段完成。

    返回：
        当时可用的端口号；若探测范围内都被占用则返回 None。
    """
    for offset in range(max_probe + 1):
        port = start_port + offset
        if port > 65535:
            break
        if not _is_port_in_use(host, port):
            return port
    return None

def _load_app_icon() -> "QIcon | None":
    """委托共享 Qt 图标解析，同时保留入口级调用接口。"""
    from entry.qt_entry_utils import load_qt_icon

    return load_qt_icon(["Web.ico"], fallback_names=["favicon.ico"])


def _ensure_app_user_model_id() -> None:
    """委托共享 Windows AppUserModelID 设置，同时保留入口级调用接口。"""
    from entry.qt_entry_utils import WEB_APP_USER_MODEL_ID, ensure_windows_app_user_model_id

    ensure_windows_app_user_model_id(WEB_APP_USER_MODEL_ID)

def _resolve_port_with_dialog(default_port: int, host: str) -> int:
    """委托 Qt 对话框选择端口；探测 host 与 uvicorn 绑定 host 相同。"""
    from entry.web_port_dialog import resolve_port_with_dialog

    return resolve_port_with_dialog(
        default_port,
        host=host,
        is_port_in_use=_is_port_in_use,
        port_probe_range=_PORT_PROBE_RANGE,
    )

def _create_tray_icon(qt_app, url: str, shutdown_event: threading.Event):
    from PyQt6.QtGui import QAction, QIcon
    from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        icon_path = Path(sys._MEIPASS).resolve() / "Web.ico"
    else:
        icon_path = Path(__file__).resolve().parent.parent / "Web.ico"

    if icon_path.exists():
        icon = QIcon(str(icon_path))
    else:
        icon = qt_app.style().standardIcon(qt_app.style().StandardPixmap.SP_ComputerIcon)

    tray = QSystemTrayIcon(icon, qt_app)
    tray.setToolTip(f"UCrawl Web - {url}")

    menu = QMenu()
    open_action = QAction("打开浏览器", menu)
    open_action.triggered.connect(lambda: webbrowser.open(url))
    menu.addAction(open_action)
    menu.addSeparator()
    quit_action = QAction("退出", menu)
    quit_action.triggered.connect(lambda: shutdown_event.set())
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: webbrowser.open(url)
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick
        else None
    )
    tray.show()
    tray.showMessage(
        "UCrawl Web 已启动",
        f"服务运行中: {url}\n右键托盘图标可打开或退出",
        QSystemTrayIcon.MessageIcon.Information,
        3000,
    )
    return tray

def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ucrawl-web",
        description="UCrawl Web UI - FastAPI + 浏览器",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认: 127.0.0.1；局域网访问需显式指定)")
    parser.add_argument("--port", type=int, default=8000, help="监听端口 (默认: 8000)")
    parser.add_argument("--no-qt", action="store_true", help="无 Qt 模式（适合服务器 / 容器部署）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")

    # 启动时脚本注入参数
    parser.add_argument("--script", help="启动后自动调用的脚本 (路径)")
    parser.add_argument(
        "--script-arg",
        action="append",
        default=[],
        help="传递给脚本的 key=value 形式参数 (可多次)",
    )
    parser.add_argument("--script-strict", action="store_true", help="脚本失败时退出 web 服务")
    parser.add_argument("--script-delay", type=float, default=0.0, help="执行脚本前延迟秒数")
    parser.add_argument("--ssl-certfile", help="TLS certificate file (required for non-loopback binding)")
    parser.add_argument("--ssl-keyfile", help="TLS private key file (required for non-loopback binding)")
    parser.add_argument(
        "--access-token",
        help="Web access token (prefer UCRAWL_WEB_ACCESS_TOKEN so it is not exposed in process arguments)",
    )
    parser.add_argument("--access-token-file", help="Persistent Web access token file for non-loopback binding")
    return parser

def main(argv: list[str] | None = None) -> int:
    """启动 Web UI，并保留参数错误与端口冲突各自的进程退出语义。"""
    parser = _build_argparser()
    args = parser.parse_args(argv)
    try:
        url_scheme = _validate_transport_security(args.host, args.ssl_certfile, args.ssl_keyfile)
    except ValueError as exc:
        parser.error(str(exc))

    qt_app = None
    shutdown_event = threading.Event()

    from cli import __version__
    from entry.web_launch_runtime import (
        build_access_url,
        build_web_url,
        resolve_existing_web_url,
        resolve_web_access_token,
        run_server_with_qt,
        try_reuse_existing_instance,
    )
    from app.utils.runtime_paths import user_data_root

    token_file = (
        args.access_token_file
        or os.getenv("UCRAWL_WEB_ACCESS_TOKEN_FILE")
        or str(user_data_root() / "web-access-token")
    )
    try:
        access_token = resolve_web_access_token(
            args.host,
            args.access_token,
            token_file=token_file,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    port_in_use = _is_port_in_use(args.host, args.port)
    # 仅凭目标端口被占用，不能把远程访问 token 发送给未知进程；只有本地无密码
    # 实例可以使用同版本复用探测。
    if port_in_use and not args.script and access_token is None:
        reused = try_reuse_existing_instance(
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
            resolve_existing_url=lambda host, port: resolve_existing_web_url(
                host,
                port,
                url_scheme,
                ssl_certfile=args.ssl_certfile,
                expected_version=__version__,
            ),
            browser_opener=lambda existing_url: webbrowser.open(
                build_access_url(existing_url, access_token)
            ),
            stderr=sys.stderr,
        )
        if reused:
            return 0

    if not args.no_qt:
        from PyQt6.QtWidgets import QApplication
        qt_app = QApplication.instance() or QApplication(sys.argv)

    if port_in_use:
        if args.no_qt:
            # 无 Qt 模式无法询问用户，因此自动顺延；仅在探测范围耗尽后返回失败。
            new_port = _find_available_port(args.host, args.port)
            if new_port is None:
                sys.stderr.write(
                    f"错误: 端口 {args.port} 及后续 {_PORT_PROBE_RANGE} 个端口都被占用，\n"
                    f"      请使用 --port 指定其他端口后再启动。\n"
                )
                return 1
            sys.stderr.write(
                f"⚠️  端口 {args.port} 已被占用，自动改用端口 {new_port}\n"
            )
            args.port = new_port
        else:
            # Qt 模式让用户确认建议端口或输入其他端口。
            args.port = _resolve_port_with_dialog(args.port, args.host)

    from app.web.server import create_app
    from app.web.script_api import parse_kv_args, inject_script_async

    @asynccontextmanager
    async def lifespan(app):
        # 启动时执行注入脚本
        if args.script:
            from app.web.server import controller as web_controller
            script_kwargs = parse_kv_args(args.script_arg)
            inject_script_async(
                args.script,
                web_controller,
                strict=args.script_strict,
                delay=args.script_delay,
                app=app,
                **script_kwargs,
            )
        yield
        registry = getattr(app.state, "web_session_registry", None)
        shutdown_all = getattr(registry, "shutdown_all", None)
        if callable(shutdown_all):
            drained = shutdown_all(wait=True, timeout=5.0)
            if drained is False:
                app.state.web_shutdown_drained = False
                if exit_code[0] == 0:
                    exit_code[0] = 1
                sys.stderr.write("Web 会话关闭超时，仍有会话未排空。\n")
        else:
            # 兼容自定义 create_app 实现的后备清理路径。
            from app.web.server import controller
            if controller:
                controller.shutdown()

    app = create_app(lifespan=lifespan, access_token=access_token)
    original_args = list(argv) if argv is not None else list(sys.argv[1:])
    app.state.web_restart_argv = (
        [sys.executable, *original_args]
        if getattr(sys, "frozen", False)
        else [sys.executable, "-m", "entry.web_entry", *original_args]
    )

    display_url = build_web_url(args.host, args.port, url_scheme)
    url = build_access_url(display_url, access_token)
    sys.stderr.write("\n  UCrawl Web UI\n")
    # 不把凭据写入终端历史和容器日志；token 仍可从环境配置或持久化文件读取。
    sys.stderr.write(f"  {display_url}\n")
    sys.stderr.write("  保存目录: downloads/\n")
    if args.script:
        sys.stderr.write(f"  启动时注入脚本: {args.script}\n")
    sys.stderr.write("\n")
    sys.stderr.flush()

    if not args.no_browser:
        def _open_browser():
            import time
            time.sleep(1.5)
            webbrowser.open(url)

        threading.Thread(target=_open_browser, daemon=True).start()

    def _signal_handler(signum, frame):
        sys.stderr.write("\n  收到终止信号，正在关闭服务...\n")
        shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    listener = _reserve_listener(args.host, args.port)
    if listener is None:
        sys.stderr.write(f"错误: 无法绑定 {args.host}:{args.port}，启动已中止。\n")
        return 1

    import uvicorn
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level="warning",
            ssl_certfile=args.ssl_certfile,
            ssl_keyfile=args.ssl_keyfile,
        )
    )

    exit_code = [0]

    def request_web_shutdown(code: int = 0) -> None:
        """排空后关闭 Web 服务，并保留脚本失败退出码。

        ``app.state.web_shutdown_callback`` 是 ``--script-strict`` 的关闭入口。
        注入脚本会收到本应用；回调存在时由这里记下退出码并结束服务。
        """
        if isinstance(code, int) and code != 0:
            exit_code[0] = code
        shutdown_event.set()
        server.should_exit = True

    def _escalate_web_shutdown() -> None:
        server.should_exit = True
        server.force_exit = True

    app.state.web_shutdown_callback = request_web_shutdown

    # 抑制 asyncio ProactorEventLoop 在 Windows 上的连接重置噪音
    # 根因：远程服务器关闭连接后，asyncio 回调中调用 socket.shutdown()，
    # 此时连接已关闭，抛出 ConnectionResetError(10054)。不影响功能，仅刷屏。
    # 参考：https://github.com/encode/uvicorn/issues/1580
    def _patch_proactor_exc_handler(loop: asyncio.AbstractEventLoop):
        orig_handler = loop.get_exception_handler()

        def _filtered_handler(loop_ctx, context):
            exc = context.get("exception")
            msg = context.get("message", "")
            if exc is not None:
                exc_msg = str(exc)
                if isinstance(exc, ConnectionResetError) and "10054" in exc_msg:
                    return  # 抑制 Windows ProactorEventLoop 的连接重置噪音
                if isinstance(exc, ConnectionResetError):
                    return
            if msg and "connection_lost" in str(msg).lower():
                return
            if orig_handler:
                orig_handler(loop_ctx, context)
            else:
                loop.default_exception_handler(context)

        loop.set_exception_handler(_filtered_handler)

    async def _serve_with_patch():
        loop = asyncio.get_running_loop()
        _patch_proactor_exc_handler(loop)
        if _server_accepts_sockets(server):
            await server.serve(sockets=[listener])
        else:
            _close_listener(listener)
            await server.serve()
        if getattr(app.state, "web_shutdown_drained", True) is False:
            raise SystemExit(exit_code[0] or 1)

    try:
        if qt_app:
            status = run_server_with_qt(
                qt_app,
                url=url,
                shutdown_event=shutdown_event,
                serve_async=_serve_with_patch,
                create_tray_icon=_create_tray_icon,
                request_shutdown=request_web_shutdown,
                escalate_shutdown=_escalate_web_shutdown,
            )
            if status != 0:
                return status
        else:
            # 无 Qt 模式：主线程直接跑 asyncio（同样应用连接重置异常抑制）
            try:
                asyncio.run(_serve_with_patch())
            except SystemExit as exc:
                code = exc.code
                if code is None:
                    code = 0
                elif not isinstance(code, int):
                    code = 1
                return exit_code[0] or code
        return exit_code[0]
    finally:
        _close_listener(listener)

if __name__ == "__main__":
    sys.exit(main())
