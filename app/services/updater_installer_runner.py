"""Lease-bound installer process orchestration for the updater helper."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.services.updater_installer_lease import verified_installer_lease
from app.services.updater_launch_capability import RESTART_HANDOFF_TOKEN_ENV


@dataclass(frozen=True)
class InstallResult:
    exit_code: int
    succeeded: bool
    log_path: Path


class InstallerRunnerCore:
    """Run an installer while its verified file identity remains leased."""

    def __init__(
        self,
        *,
        package_verifier: Any,
        run_func: Callable[..., subprocess.CompletedProcess],
        popen_func: Callable[..., subprocess.Popen],
        default_staging_dir: Callable[[], Path],
        logs_root: Callable[[], Path],
        sanitize_filename: Callable[[str], str],
        log_event: Callable[..., None],
        verification_error: type[Exception],
        install_error: type[Exception],
    ) -> None:
        self.package_verifier = package_verifier
        self._run = run_func
        self._popen = popen_func
        self._default_staging_dir = default_staging_dir
        self._logs_root = logs_root
        self._sanitize_filename = sanitize_filename
        self._log_event = log_event
        self._verification_error = verification_error
        self._install_error = install_error

    def run_verified_installer(
        self,
        path: Path,
        asset: Any,
        *,
        version: str,
        log_path: Path | None = None,
        approved_root: Path | None = None,
    ) -> InstallResult:
        root = approved_root or self._default_staging_dir()
        with verified_installer_lease(
            path,
            approved_root=root,
            error_factory=self._verification_error,
            cleanup_error_callback=lambda exc: self._log_event(
                "update.install.lease_cleanup_failed",
                "verified installer lease cleanup failed after process completion",
                level="ERROR",
                error_type=type(exc).__name__,
            ),
        ) as leased_path:
            self.package_verifier.verify(leased_path, asset)
            effective_log = log_path or self._logs_root() / (
                f"update_install_{self._sanitize_filename(version)}.log"
            )
            argv = self._install_argv(leased_path, asset, effective_log)
            self._log_event(
                "update.install.started",
                "installer started",
                version=version,
                installer=asset.name,
            )
            result = self._run(argv, shell=False, timeout=None)
            succeeded = result.returncode == 0
            self._log_event(
                "update.install.exit",
                "installer exited",
                level="INFO" if succeeded else "ERROR",
                version=version,
                exit_code=result.returncode,
            )
            return InstallResult(
                exit_code=int(result.returncode),
                succeeded=succeeded,
                log_path=effective_log,
            )

    def launch_verified_installer(
        self,
        path: Path,
        asset: Any,
        *,
        version: str,
        log_path: Path,
        install_dir: Path,
        restart_handoff_path: Path | None,
        restart_handoff_token: str = "",
        approved_root: Path | None = None,
    ) -> subprocess.Popen:
        if _normalized_os(asset.os) != "windows" or asset.installer_type.lower() != "inno":
            raise self._install_error(
                "detached self-update currently requires a Windows Inno installer"
            )
        if bool(restart_handoff_path) != bool(restart_handoff_token):
            raise self._install_error(
                "restart handoff path and token must be provided together"
            )
        root = approved_root or self._default_staging_dir()
        leased_descriptors: list[int] = []
        with verified_installer_lease(
            path,
            approved_root=root,
            error_factory=self._verification_error,
            cleanup_error_callback=lambda exc: self._log_event(
                "update.install.lease_cleanup_failed",
                "verified installer lease cleanup failed after the installer inherited it",
                level="ERROR",
                error_type=type(exc).__name__,
            ),
            bind_descriptor=leased_descriptors.append,
        ) as leased_path:
            self.package_verifier.verify(leased_path, asset)
            argv = self._install_argv(
                leased_path,
                asset,
                log_path,
                install_dir=install_dir,
                restart_handoff_path=restart_handoff_path,
                restart_handoff_token=restart_handoff_token,
            )
            self._log_event(
                "update.install.started",
                "installer started",
                version=version,
                installer=asset.name,
                detached=True,
            )
            process_kwargs: dict[str, Any] = {"shell": False, "close_fds": True}
            if os.name == "nt":
                process_kwargs["creationflags"] = getattr(
                    subprocess,
                    "CREATE_NO_WINDOW",
                    0,
                )
            if restart_handoff_token:
                environment = os.environ.copy()
                environment[RESTART_HANDOFF_TOKEN_ENV] = restart_handoff_token
                process_kwargs["env"] = environment
            if not leased_descriptors:
                raise self._install_error("verified installer lease was not acquired")
            process = self._spawn_with_inherited_lease(
                argv,
                leased_descriptors[0],
                process_kwargs,
            )
            self._log_event(
                "update.install.handoff",
                "installer process detached from updater helper",
                version=version,
                installer_pid=getattr(process, "pid", 0),
            )
            return process

    def _spawn_with_inherited_lease(
        self,
        argv: list[str],
        descriptor: int,
        process_kwargs: dict[str, Any],
    ) -> subprocess.Popen:
        """Inherit the deny-delete lease so it outlives CreateProcess.

        The parent descriptor is closed when the lease context exits. The
        installer keeps its inherited duplicate until that process exits, so
        the verified package cannot be replaced after CreateProcess returns.
        """

        import msvcrt

        os.set_inheritable(descriptor, True)
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.lpAttributeList = {
                "handle_list": [int(msvcrt.get_osfhandle(descriptor))],
            }
            process_kwargs["startupinfo"] = startupinfo
            return self._popen(argv, **process_kwargs)
        finally:
            os.set_inheritable(descriptor, False)

    def _install_argv(
        self,
        path: Path,
        asset: Any,
        log_path: Path,
        *,
        install_dir: Path | None = None,
        restart_handoff_path: Path | None = None,
        restart_handoff_token: str = "",
    ) -> list[str]:
        raise NotImplementedError


def build_installer_argv(
    path: Path,
    asset: Any,
    log_path: Path,
    *,
    install_error: type[Exception],
    install_dir: Path | None = None,
    restart_handoff_path: Path | None = None,
    restart_handoff_token: str = "",
) -> list[str]:
    del restart_handoff_token
    os_name = _normalized_os(asset.os)
    installer_type = asset.installer_type.lower()
    if os_name == "windows" and installer_type == "msi":
        return [
            "msiexec.exe",
            "/i",
            str(path),
            "/passive",
            "/norestart",
            "/L*v",
            str(log_path),
        ]
    if os_name == "windows" and installer_type == "inno":
        argv = [
            str(path),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            f"/LOG={log_path}",
        ]
        if install_dir is not None:
            argv.append(f"/DIR={install_dir}")
        if restart_handoff_path is not None:
            argv.append(f"/UCrawlRestartHandoff={restart_handoff_path}")
        return argv
    if os_name == "windows" and installer_type == "nsis":
        return [str(path), "/S"]
    if os_name == "macos" and installer_type == "pkg":
        return ["/usr/sbin/installer", "-pkg", str(path), "-target", "/"]
    if os_name == "linux" and installer_type == "appimage":
        raise install_error("AppImage updates require the updater helper symlink switch path")
    raise install_error(f"unsupported installer type: {asset.os}/{asset.installer_type}")


def _normalized_os(value: Any) -> str:
    raw = str(value or "").lower()
    if raw.startswith("win") or raw == "windows":
        return "windows"
    if raw in {"darwin", "mac", "macos", "osx"}:
        return "macos"
    if raw.startswith("linux"):
        return "linux"
    return raw
