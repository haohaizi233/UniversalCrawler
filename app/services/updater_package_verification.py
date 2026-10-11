"""Platform signature verification used by the signed updater pipeline."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

_AUTHENTICODE_SCRIPT = """param(
    [Parameter(Mandatory = $true)]
    [string] $LiteralPath
)
$ErrorActionPreference = 'Stop'
$signature = Get-AuthenticodeSignature -LiteralPath $LiteralPath
$certificate = $signature.SignerCertificate
[Console]::OutputEncoding = [Text.Encoding]::UTF8
if ($null -eq $certificate) {
    ConvertTo-Json @{Status=[string]$signature.Status; Subject=''; Thumbprint=''; SHA256Fingerprint=''}
    exit 0
}
$sha256 = [System.BitConverter]::ToString(
    [System.Security.Cryptography.SHA256]::Create().ComputeHash($certificate.RawData)
).Replace('-', '')
ConvertTo-Json @{
    Status = [string]$signature.Status
    Subject = [string]$certificate.Subject
    Thumbprint = [string]$certificate.Thumbprint
    SHA256Fingerprint = $sha256
}
"""


class PlatformPackageVerifier:
    """Verify manifest integrity first, then the platform publisher identity."""

    def __init__(
        self,
        *,
        os_name: str,
        trusted_publishers: tuple[str, ...],
        trusted_thumbprints: tuple[str, ...],
        require_os_signature: bool,
        verify_func: Callable[[Path, Any], None] | None,
        run_func: Callable[..., subprocess.CompletedProcess],
        normalize_os: Callable[[str], str],
        normalize_fingerprint: Callable[[object], str],
        verify_file_hash: Callable[[Path, Any], None],
        error_factory: Callable[[str], BaseException],
    ) -> None:
        self.os_name = normalize_os(os_name)
        self.trusted_publishers = trusted_publishers
        self.trusted_thumbprints = tuple(
            normalize_fingerprint(value)
            for value in trusted_thumbprints
            if str(value).strip()
        )
        self.require_os_signature = bool(require_os_signature)
        self._verify_func = verify_func
        self._run = run_func
        self._normalize_fingerprint = normalize_fingerprint
        self._verify_file_hash = verify_file_hash
        self._error_factory = error_factory

    def verify(self, path: Path, asset: Any) -> None:
        self._verify_file_hash(path, asset)
        if not self.require_os_signature:
            return
        if self._verify_func:
            self._verify_func(path, asset)
        elif self.os_name == "windows":
            self._verify_windows(path)
        elif self.os_name == "macos":
            self._verify_macos(path, asset)
        else:
            raise self._error(
                f"OS package signature verification is not supported for {self.os_name}"
            )

    def _error(self, message: str) -> BaseException:
        return self._error_factory(message)

    def _verify_windows(self, path: Path) -> None:
        if not self.trusted_thumbprints:
            raise self._error("Windows updater requires trusted thumbprint allowlist")
        script_fd, script_name = tempfile.mkstemp(
            prefix="ucrawl-authenticode-",
            suffix=".ps1",
        )
        script_path = Path(script_name)
        try:
            os.write(script_fd, _AUTHENTICODE_SCRIPT.encode("ascii"))
            os.close(script_fd)
            script_fd = -1
            result = self._run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    os.fspath(script_path),
                    "-LiteralPath",
                    os.fspath(path),
                ],
                capture_output=True,
                text=True,
                shell=False,
                timeout=20,
            )
        finally:
            if script_fd >= 0:
                try:
                    os.close(script_fd)
                except OSError:
                    pass
            try:
                script_path.unlink()
            except OSError:
                pass
        if result.returncode != 0:
            raise self._error("Authenticode verification command failed")
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise self._error("Authenticode output was invalid") from exc
        if payload.get("Status") != "Valid":
            raise self._error("Authenticode signature is not valid")
        subject = str(payload.get("Subject") or "")
        thumbprints = {
            self._normalize_fingerprint(payload.get("Thumbprint")),
            self._normalize_fingerprint(payload.get("SHA256Fingerprint")),
        }
        if not any(value and value in self.trusted_thumbprints for value in thumbprints):
            raise self._error("Authenticode signer thumbprint is not trusted")
        if self.trusted_publishers and not any(
            publisher in subject for publisher in self.trusted_publishers
        ):
            raise self._error("Authenticode signer publisher is not trusted")

    def _verify_macos(self, path: Path, asset: Any) -> None:
        if not self.trusted_publishers:
            raise self._error("macOS updater requires trusted publisher allowlist")
        results: tuple[subprocess.CompletedProcess[Any], ...]
        if asset.installer_type == "pkg":
            signature_result = self._run(
                ["pkgutil", "--check-signature", str(path)],
                capture_output=True,
                text=True,
                shell=False,
                timeout=20,
            )
            gatekeeper_result = self._run(
                ["spctl", "-a", "-vv", "-t", "install", str(path)],
                capture_output=True,
                text=True,
                shell=False,
                timeout=20,
            )
            results = (signature_result, gatekeeper_result)
        else:
            results = (
                self._run(
                    ["spctl", "-a", "-vv", "-t", "open", str(path)],
                    capture_output=True,
                    text=True,
                    shell=False,
                    timeout=20,
                ),
            )
        if any(result.returncode != 0 for result in results):
            raise self._error("macOS package signature check failed")
        output = "\n".join(f"{result.stdout}\n{result.stderr}" for result in results)
        if not any(publisher in output for publisher in self.trusted_publishers):
            raise self._error("macOS signer is not trusted")
