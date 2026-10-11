"""独立更新辅助进程入口。

GUI 进程下载并验证安装包后再启动此辅助进程。跨进程不能继承内存中的验证
结论，因此辅助进程必须重新验证签名清单与包哈希，并在启动平台安装器前
立即执行配置的 OS 签名策略。
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
import traceback
from pathlib import Path

from app.config.update_trust import (
    UPDATE_PUBLIC_KEY_PEM,
    UPDATE_REQUIRE_OS_SIGNATURE,
    UPDATE_TRUSTED_PUBLISHERS,
    UPDATE_TRUSTED_THUMBPRINTS,
)
from app.services.secure_updater import (
    AssetSelector,
    InstallerRunner,
    PackageVerifier,
    UpdateAsset,
    UpdateManifestVerifier,
    VerificationError,
    default_update_staging_dir,
    log_update_event,
)
from app.services.update_check_service import (
    REVISION_AUTH_TOKEN_ENV,
    REVISION_AUTH_TTL_SECONDS,
    default_update_metadata_dir,
)
from app.services.updater_launch_capability import (
    LAUNCH_CAPABILITY_TOKEN_ENV,
    RESTART_HANDOFF_TOKEN_ENV,
    LaunchBinding,
    RestartHandoffBinding,
    UpdaterCapabilityError,
    consume_launch_capability,
    consume_restart_handoff,
    file_sha256,
    issue_restart_handoff,
    load_installed_release_identity,
)
from app.services.updater_manifest_json import (
    StrictManifestJsonError,
    load_strict_manifest_json,
)
from app.services.updater_path_safety import (
    ensure_approved_directory,
    ensure_regular_or_missing,
    open_regular_binary,
    replace_regular_file,
    require_regular_file,
)
from app.services.updater_cleanup import run_typed_cleanup_actions
from app.services.updater_file_lock import exclusive_file_lock
from shared.release_identity import ReleaseIdentity

_ALLOWED_RESTART_EXECUTABLES = frozenset(
    {
        "universalcrawlerpro.exe",
        "crawlerwebportal.exe",
        "ucrawllauncher.exe",
    }
)


def _best_effort_update_log(event: str, message: str, *, level: str = "INFO", **details: object) -> None:
    """遥测故障不能阻断安装器交接、失败落盘或安装后的应用重启。"""

    try:
        log_update_event(event, message, level=level, **details)
    except BaseException:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ucrawl-updater-helper")
    parser.add_argument("--installer", default="", help="已下载的安装包路径")
    parser.add_argument("--manifest", default="", help="已验证更新清单路径")
    parser.add_argument("--signature", default="", help="更新清单 Ed25519 签名路径")
    parser.add_argument("--version", default="", help="待安装版本")
    parser.add_argument("--release-revision", type=int, default=0, help="待安装发布修订号")
    parser.add_argument("--log-path", default="", help="安装器日志输出路径")
    parser.add_argument("--install-dir", default="", help="当前冻结应用安装目录")
    parser.add_argument("--restart-argv-json", default="", help="安装成功后用于重启应用的 argv JSON 数组")
    parser.add_argument("--wait-pid", type=int, default=0, help="运行安装器前等待此进程退出")
    parser.add_argument("--launch-capability", default="")
    parser.add_argument("--restart-handoff-output", default="")
    parser.add_argument(
        "--revision-authorization",
        default="",
        help="本地用户确认回退或重装后生成的一次性授权文件",
    )
    parser.add_argument(
        "--complete-install",
        action="store_true",
        help="由 Inno 安装成功后调用，消费重启交接文件",
    )
    parser.add_argument("--restart-handoff", default="", help="安装后重启交接 JSON 路径")
    return parser


def _load_verified_asset(
    *,
    manifest_path: str | Path,
    signature_path: str | Path,
    expected_version: str,
    expected_revision: int = 0,
    os_name: str | None = None,
    arch: str | None = None,
) -> UpdateAsset:
    """在独立辅助进程内重新建立已签名元数据的信任。"""

    manifest = UpdateManifestVerifier(public_key_pem=UPDATE_PUBLIC_KEY_PEM).load_verified(
        Path(manifest_path),
        Path(signature_path),
    )
    expected_identity = ReleaseIdentity(expected_version, expected_revision)
    if manifest.identity != expected_identity:
        raise VerificationError(
            "signed manifest release identity "
            f"{manifest.identity.tag} does not match requested version/release identity "
            f"{expected_identity.tag}"
        )
    return AssetSelector(os_name=os_name, arch=arch).select(manifest)


def _consume_revision_authorization(
    *,
    authorization_path: str | Path,
    token: str,
    manifest_path: str | Path,
    target_identity: ReleaseIdentity,
    now: float | None = None,
    approved_metadata_root: Path | None = None,
) -> None:
    """校验并销毁与清单摘要绑定的一次性本地回退授权。"""

    root = ensure_approved_directory(
        approved_metadata_root or default_update_metadata_dir(),
    )
    try:
        manifest = require_regular_file(
            Path(manifest_path),
            approved_root=root,
        )
        requested_authorization = Path(authorization_path)
        claim_lock = root / f".{requested_authorization.name}.claim.lock"
        with exclusive_file_lock(
            claim_lock,
            approved_root=root,
            error_factory=VerificationError,
        ):
            authorization = require_regular_file(
                requested_authorization,
                approved_root=root,
            )
            if (
                not authorization.name.startswith(".ucrawl-revision-auth-")
                or authorization.suffix != ".json"
            ):
                raise OSError("revision authorization filename is invalid")
            raw = _read_revision_authorization(authorization, root=root)
            _validate_revision_authorization(
                raw,
                token=token,
                manifest=manifest,
                target_identity=target_identity,
                now=now,
                root=root,
            )
            claimed = authorization.with_name(
                f".{authorization.stem}.claimed-{os.getpid()}-"
                f"{secrets.token_hex(8)}.json"
            )
            replace_regular_file(authorization, claimed, approved_root=root)
            claimed_raw = _read_revision_authorization(claimed, root=root)
            if not hmac.compare_digest(raw, claimed_raw):
                raise VerificationError(
                    "revision authorization changed while it was claimed"
                )
    except OSError as exc:
        raise VerificationError(
            "revision authorization is missing or invalid"
        ) from exc
    except (StrictManifestJsonError, TypeError, ValueError) as exc:
        raise VerificationError(
            "revision authorization is missing or invalid"
        ) from exc


def _read_revision_authorization(path: Path, *, root: Path) -> bytes:
    handle = None
    primary_error: BaseException | None = None
    try:
        handle = open_regular_binary(path, approved_root=root, mode="rb")
        raw = handle.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            raise VerificationError("revision authorization is missing or invalid")
        return raw
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if handle is not None:
            run_typed_cleanup_actions(
                primary_error,
                ((handle.close, "revision authorization handle cleanup failed"),),
                error_factory=VerificationError,
                failure_message="revision authorization handle cleanup failed",
            )


def _validate_revision_authorization(
    raw: bytes,
    *,
    token: str,
    manifest: Path,
    target_identity: ReleaseIdentity,
    now: float | None,
    root: Path,
) -> None:
    payload = load_strict_manifest_json(raw)
    if set(payload) != {
        "schema",
        "targetVersion",
        "targetRevision",
        "targetTag",
        "manifestSha256",
        "tokenSha256",
        "issuedAt",
        "expiresAt",
    } or payload.get("schema") != 1:
        raise VerificationError("revision authorization payload is invalid")
    payload_identity = ReleaseIdentity(
        payload.get("targetVersion"),
        payload.get("targetRevision"),
    )
    if (
        payload_identity != target_identity
        or payload.get("targetTag") != target_identity.tag
    ):
        raise VerificationError("revision authorization target does not match")
    issued_at = payload.get("issuedAt")
    expires_at = payload.get("expiresAt")
    if (
        type(issued_at) is not int
        or type(expires_at) is not int
        or expires_at <= issued_at
        or expires_at - issued_at > REVISION_AUTH_TTL_SECONDS
    ):
        raise VerificationError("revision authorization lifetime is invalid")
    current_time = int(time.time() if now is None else now)
    if issued_at > current_time + 60 or expires_at <= current_time:
        raise VerificationError("revision authorization has expired")
    token_digest = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
    stored_token_digest = payload.get("tokenSha256")
    if (
        type(stored_token_digest) is not str
        or not token
        or not hmac.compare_digest(token_digest, stored_token_digest)
    ):
        raise VerificationError("revision authorization token does not match")
    stored_manifest_digest = payload.get("manifestSha256")
    if type(stored_manifest_digest) is not str or not hmac.compare_digest(
        file_sha256(manifest, approved_root=root, max_bytes=2_000_000),
        stored_manifest_digest,
    ):
        raise VerificationError("revision authorization manifest does not match")


def _authorize_release_transition(
    *,
    current_identity: ReleaseIdentity,
    target_identity: ReleaseIdentity,
    manifest_path: str | Path,
    authorization_path: str | Path | None,
    token: str,
    approved_metadata_root: Path | None = None,
) -> None:
    """升级无需额外授权；重装和回退必须消费当前会话生成的授权。"""

    if target_identity > current_identity:
        if authorization_path or token:
            raise VerificationError("revision authorization is not valid for an upgrade")
        return
    if not authorization_path or not token:
        raise VerificationError(
            "revision reinstall or downgrade requires one-time local authorization"
        )
    _consume_revision_authorization(
        authorization_path=authorization_path,
        token=token,
        manifest_path=manifest_path,
        target_identity=target_identity,
        approved_metadata_root=approved_metadata_root,
    )


def _load_restart_argv(value: str) -> list[str]:
    if not str(value or "").strip():
        return []
    payload = json.loads(value)
    if not isinstance(payload, list) or not all(
        type(item) is str and item for item in payload
    ):
        raise ValueError("restart argv must be a string array")
    return payload


def _require_install_arguments(args: argparse.Namespace) -> None:
    missing = [
        option
        for option, value in (
            ("--installer", args.installer),
            ("--manifest", args.manifest),
            ("--signature", args.signature),
            ("--version", args.version),
            ("--log-path", args.log_path),
            ("--install-dir", args.install_dir),
            ("--launch-capability", args.launch_capability),
        )
        if not str(value or "").strip()
    ]
    if missing:
        raise ValueError(f"missing updater helper arguments: {', '.join(missing)}")


def _resolve_install_dir(value: str | Path) -> Path:
    try:
        install_dir = ensure_approved_directory(Path(value).expanduser())
    except OSError as exc:
        raise ValueError("update install directory does not exist or is unsafe") from exc
    if not install_dir.is_absolute():
        raise ValueError("update install directory does not exist")
    return install_dir


def _validate_restart_argv(argv: list[str], install_dir: Path) -> list[str]:
    """只允许重启当前安装目录下的正式窗口入口，拒绝任意程序交接。"""

    if not argv:
        return []
    install_root = ensure_approved_directory(install_dir)
    executable = Path(argv[0])
    if not executable.is_absolute():
        executable = install_root / executable
    try:
        executable = require_regular_file(executable, approved_root=install_root)
    except OSError as exc:
        raise ValueError(
            "restart executable must be a safe file in the current install directory"
        ) from exc
    if executable.name.casefold() not in _ALLOWED_RESTART_EXECUTABLES:
        raise ValueError("restart executable is not an approved UCrawl entry")
    return [str(executable), *argv[1:]]


def _write_restart_handoff(
    *,
    target_path: Path,
    restart_argv: list[str],
    install_dir: Path,
    version: str,
    release_revision: int,
    log_path: Path,
    staging_root: Path,
) -> tuple[Path, str]:
    """原子写入 Inno 安装完成后由新 helper 消费的一次性交接文件。"""

    target = ensure_regular_or_missing(target_path, approved_root=staging_root)
    binding = RestartHandoffBinding(
        restart_argv=tuple(restart_argv),
        install_root=install_dir,
        target_identity=ReleaseIdentity(str(version), int(release_revision)),
        log_path=log_path,
    )
    token = issue_restart_handoff(
        target,
        binding,
        staging_root=staging_root,
    )
    return target, token


def _post_install_restart(handoff_path: Path, token: str) -> int:
    """在 Inno 已完成文件替换后，由新版本 helper 重启原入口。"""

    if not getattr(sys, "frozen", False):
        raise RuntimeError("post-install restart requires the frozen updater helper")
    current_install_dir = ensure_approved_directory(
        Path(sys.executable).resolve().parent
    )
    staging_root = ensure_approved_directory(default_update_staging_dir())
    binding = consume_restart_handoff(
        handoff_path,
        token,
        expected_install_root=current_install_dir,
        staging_root=staging_root,
    )
    identity = load_installed_release_identity(current_install_dir)
    if identity != binding.target_identity:
        raise UpdaterCapabilityError(
            "installed release identity does not match restart handoff"
        )
    restart_argv = _validate_restart_argv(
        list(binding.restart_argv),
        current_install_dir,
    )
    process_kwargs: dict[str, object] = {
        "shell": False,
        "cwd": str(current_install_dir),
        "close_fds": True,
    }
    if os.name == "nt":
        process_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _best_effort_update_log(
        "update.install.exit",
        "installer completed and invoked post-install helper",
        version=identity.version,
        revision=identity.revision,
        exit_code=0,
    )
    subprocess.Popen(restart_argv, **process_kwargs)
    _best_effort_update_log(
        "update.install.restart",
        "restart command launched",
        version=identity.version,
        revision=identity.revision,
    )
    return 0


def _option_value(argv: list[str], option: str) -> str:
    for index, item in enumerate(argv):
        if item.startswith(f"{option}="):
            return item.split("=", 1)[1]
        if item == option and index + 1 < len(argv):
            return argv[index + 1]
    return ""


def _failure_log_path(argv: list[str]) -> Path | None:
    del argv
    try:
        staging_root = ensure_approved_directory(
            default_update_staging_dir(),
            create=True,
        )
        return ensure_regular_or_missing(
            staging_root / "updater-helper.log",
            approved_root=staging_root,
        )
    except BaseException:
        return None


def _safe_exception_text(exc: BaseException) -> str:
    try:
        return str(exc) or type(exc).__name__
    except BaseException:
        return type(exc).__name__


def _record_helper_failure(argv: list[str], exc: BaseException) -> None:
    try:
        error_text = _safe_exception_text(exc)
        _best_effort_update_log(
            "update.install.helper_failed",
            error_text,
            level="ERROR",
            error_type=type(exc).__name__,
        )
        log_path = _failure_log_path(argv)
        if log_path is None:
            return
        try:
            trace_text = traceback.format_exc()
        except BaseException:
            trace_text = f"{type(exc).__name__}\n"
        payload = (
            f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"updater helper failed: {type(exc).__name__}: {error_text}\n"
            f"{trace_text}"
        ).encode("utf-8", errors="replace")
        stream = open_regular_binary(
            log_path,
            approved_root=log_path.parent,
            mode="ab",
        )
        try:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            try:
                stream.close()
            except BaseException:
                pass
    except BaseException:
        pass


def _wait_for_process_exit(pid: int, *, timeout_seconds: float = 120.0) -> None:
    """替换安装文件前等待 GUI 进程释放占用。"""

    pid = int(pid or 0)
    if pid <= 0:
        return
    if pid == os.getpid():
        raise RuntimeError("updater helper cannot wait for itself")
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        # SYNCHRONIZE：只申请等待父进程退出所需的最小权限。
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            error_code = ctypes.get_last_error()
            if error_code == 87:  # ERROR_INVALID_PARAMETER：进程已经退出。
                return
            raise OSError(error_code, "could not open parent process")
        try:
            result = kernel32.WaitForSingleObject(handle, max(0, int(timeout_seconds * 1000)))
        finally:
            kernel32.CloseHandle(handle)
        if result == 0:  # WAIT_OBJECT_0：父进程已退出。
            return
        if result == 258:  # WAIT_TIMEOUT：继续替换文件会与父进程冲突。
            raise TimeoutError(f"parent process {pid} did not exit before updater timeout")
        raise OSError(ctypes.get_last_error(), "waiting for parent process failed")

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return
            raise
        if time.monotonic() >= deadline:
            raise TimeoutError(f"parent process {pid} did not exit before updater timeout")
        time.sleep(0.1)


def _launch_install(args: argparse.Namespace) -> int:
    _require_install_arguments(args)
    if not getattr(sys, "frozen", False):
        raise RuntimeError("update installation requires the frozen updater helper")
    try:
        staging_root = ensure_approved_directory(default_update_staging_dir())
        metadata_root = ensure_approved_directory(default_update_metadata_dir())
        installer_path = require_regular_file(
            Path(args.installer),
            approved_root=staging_root,
        )
        manifest_path = require_regular_file(
            Path(args.manifest),
            approved_root=metadata_root,
        )
        signature_path = require_regular_file(
            Path(args.signature),
            approved_root=metadata_root,
        )
        log_path = ensure_regular_or_missing(
            Path(args.log_path),
            approved_root=staging_root,
        )
        capability_path = require_regular_file(
            Path(args.launch_capability),
            approved_root=metadata_root,
        )
        authorization_path = None
        if str(args.revision_authorization or ""):
            authorization_path = require_regular_file(
                Path(args.revision_authorization),
                approved_root=metadata_root,
            )
        restart_handoff_path = None
        if str(args.restart_handoff_output or ""):
            restart_handoff_path = ensure_regular_or_missing(
                Path(args.restart_handoff_output),
                approved_root=staging_root,
            )
    except OSError as exc:
        raise VerificationError("updater helper paths are outside approved roots") from exc
    install_dir = _resolve_install_dir(args.install_dir)
    helper_install_dir = ensure_approved_directory(
        Path(sys.executable).resolve().parent
    )
    if os.path.normcase(os.fspath(install_dir)) != os.path.normcase(
        os.fspath(helper_install_dir)
    ):
        raise VerificationError(
            "update install directory does not match the running updater helper"
        )
    target_identity = ReleaseIdentity(str(args.version), int(args.release_revision))
    current_identity = load_installed_release_identity(install_dir)
    restart_argv = _load_restart_argv(args.restart_argv_json)
    restart_argv = _validate_restart_argv(restart_argv, install_dir)
    if bool(restart_argv) != bool(restart_handoff_path):
        raise VerificationError(
            "restart argv and restart handoff output must be provided together"
        )
    launch_binding = LaunchBinding(
        installer_path=installer_path,
        manifest_path=manifest_path,
        signature_path=signature_path,
        log_path=log_path,
        install_root=install_dir,
        target_identity=target_identity,
        current_identity=current_identity,
        restart_argv=tuple(restart_argv),
        parent_pid=int(args.wait_pid),
        restart_handoff_path=restart_handoff_path,
        revision_authorization_path=authorization_path,
    )
    asset = _load_verified_asset(
        manifest_path=manifest_path,
        signature_path=signature_path,
        expected_version=str(args.version),
        expected_revision=int(args.release_revision),
    )
    launch_capability_token = os.environ.pop(LAUNCH_CAPABILITY_TOKEN_ENV, "")
    consume_launch_capability(
        capability_path,
        launch_capability_token,
        expected=launch_binding,
        staging_root=staging_root,
        metadata_root=metadata_root,
    )
    authorization_token = os.environ.pop(REVISION_AUTH_TOKEN_ENV, "")
    _authorize_release_transition(
        current_identity=current_identity,
        target_identity=target_identity,
        manifest_path=manifest_path,
        authorization_path=authorization_path,
        token=authorization_token,
        approved_metadata_root=metadata_root,
    )
    _wait_for_process_exit(args.wait_pid)
    verifier = PackageVerifier(
        trusted_publishers=UPDATE_TRUSTED_PUBLISHERS,
        trusted_thumbprints=UPDATE_TRUSTED_THUMBPRINTS,
        require_os_signature=UPDATE_REQUIRE_OS_SIGNATURE,
    )
    runner = InstallerRunner(package_verifier=verifier)
    restart_handoff_token = ""
    if restart_argv:
        restart_handoff_path, restart_handoff_token = _write_restart_handoff(
            target_path=restart_handoff_path,
            restart_argv=restart_argv,
            install_dir=install_dir,
            version=str(args.version),
            release_revision=int(args.release_revision),
            log_path=log_path,
            staging_root=staging_root,
        )
    runner.launch_verified_installer(
        installer_path,
        asset,
        version=str(args.version),
        log_path=log_path,
        install_dir=install_dir,
        restart_handoff_path=restart_handoff_path,
        restart_handoff_token=restart_handoff_token,
        approved_root=staging_root,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else list(sys.argv[1:])
    try:
        args = build_parser().parse_args(raw_argv)
        if args.complete_install:
            restart_handoff_token = os.environ.pop(RESTART_HANDOFF_TOKEN_ENV, "")
            if not str(args.restart_handoff or "").strip() or not str(
                restart_handoff_token
            ).strip():
                raise ValueError(
                    "restart handoff path and inherited token are required with "
                    "--complete-install"
                )
            return _post_install_restart(
                Path(args.restart_handoff),
                restart_handoff_token,
            )
        return _launch_install(args)
    except Exception as exc:
        _record_helper_failure(raw_argv, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
