"""产品版本与发布修订组成的稳定身份协议。

产品版本仍由 :mod:`shared.version` 维护；这里的修订号描述同一产品版本下
不可变发布物的先后次序。模块刻意不依赖 GUI 或更新服务，供运行时与构建工具共用。
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from functools import total_ordering
from pathlib import Path
from typing import Any, Mapping

from shared.filesystem_directory_capability import (
    filesystem_directory_capability,
    is_link_or_reparse,
)


RELEASE_IDENTITY_FILENAME = "release_identity.json"
MAX_RELEASE_VERSION_LENGTH = 40
MAX_RELEASE_CANDIDATE_ID_LENGTH = 80
MAX_RELEASE_REVISION = 2_147_483_647
MAX_RELEASE_IDENTITY_BYTES = 16 * 1024
_SEMVER_PATTERN = re.compile(
    r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?"
)
_REVISION_TAG_PATTERN = re.compile(r"v(.+)-r([1-9][0-9]*)")


@dataclass(frozen=True)
class SemVer:
    """足以完成更新排序的 SemVer 值对象。"""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: str = ""

    @classmethod
    def parse(cls, value: Any) -> "SemVer":
        raw = str(value or "").strip()
        if raw.lower().startswith("v"):
            raw = raw[1:]
        if len(raw) > MAX_RELEASE_VERSION_LENGTH:
            raise ValueError(f"invalid semver: {value!r}")
        match = _SEMVER_PATTERN.fullmatch(raw)
        if not match:
            raise ValueError(f"invalid semver: {value!r}")
        core_parts = match.group(1), match.group(2), match.group(3)
        if any(len(part) > 1 and part.startswith("0") for part in core_parts):
            raise ValueError(f"invalid semver: {value!r}")
        prerelease_text = match.group(4) or ""
        prerelease = tuple(prerelease_text.split(".")) if prerelease_text else ()
        if any(
            not part or (part.isdigit() and len(part) > 1 and part.startswith("0"))
            for part in prerelease
        ):
            raise ValueError(f"invalid semver: {value!r}")
        build = match.group(5) or ""
        if build and any(not part for part in build.split(".")):
            raise ValueError(f"invalid semver: {value!r}")
        return cls(
            major=int(match.group(1)),
            minor=int(match.group(2)),
            patch=int(match.group(3)),
            prerelease=prerelease,
            build=build,
        )

    def __str__(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += "-" + ".".join(self.prerelease)
        if self.build:
            value += "+" + self.build
        return value


def _compare_prerelease(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    if not left and not right:
        return 0
    if not left:
        return 1
    if not right:
        return -1
    for left_part, right_part in zip(left, right, strict=False):
        left_num = left_part.isdigit()
        right_num = right_part.isdigit()
        if left_num and right_num:
            left_number, right_number = int(left_part), int(right_part)
            if left_number != right_number:
                return -1 if left_number < right_number else 1
            continue
        if left_num != right_num:
            return -1 if left_num else 1
        if left_part != right_part:
            return -1 if left_part < right_part else 1
    if len(left) == len(right):
        return 0
    return -1 if len(left) < len(right) else 1


def compare_semver(left: Any, right: Any) -> int:
    """按 SemVer 优先级比较；构建元数据不参与先后排序。"""

    left_version = SemVer.parse(left)
    right_version = SemVer.parse(right)
    left_core = (left_version.major, left_version.minor, left_version.patch)
    right_core = (right_version.major, right_version.minor, right_version.patch)
    if left_core != right_core:
        return -1 if left_core < right_core else 1
    return _compare_prerelease(left_version.prerelease, right_version.prerelease)


def _validate_revision(value: Any) -> int:
    # ``bool`` 是 ``int`` 的子类；显式排除可避免 JSON true 被解释为修订 1。
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_RELEASE_REVISION
    ):
        raise ValueError("release revision is outside the supported range")
    return value


@total_ordering
@dataclass(frozen=True, eq=False)
class ReleaseIdentity:
    """一个可排序、可哈希且不可变的产品发布身份。"""

    version: str
    revision: int = 0
    _parsed_version: SemVer = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        parsed = SemVer.parse(self.version)
        if parsed.build:
            raise ValueError("release identity must not contain build metadata")
        if re.search(r"-r[0-9]+\Z", str(parsed)):
            raise ValueError("release version is ambiguous with revision tag syntax")
        revision = _validate_revision(self.revision)
        tag = _format_identity_tag(str(parsed), revision)
        if len(tag) > MAX_RELEASE_CANDIDATE_ID_LENGTH:
            raise ValueError("release candidate id is too long")
        object.__setattr__(self, "version", str(parsed))
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "_parsed_version", parsed)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ReleaseIdentity):
            return NotImplemented
        return compare_semver(self.version, other.version) == 0 and self.revision == other.revision

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ReleaseIdentity):
            return NotImplemented
        version_order = compare_semver(self.version, other.version)
        if version_order:
            return version_order < 0
        return self.revision < other.revision

    def __hash__(self) -> int:
        parsed = self._parsed_version
        return hash((parsed.major, parsed.minor, parsed.patch, parsed.prerelease, self.revision))

    @property
    def tag(self) -> str:
        return _format_identity_tag(self.version, self.revision)

    @property
    def candidate_id(self) -> str:
        """返回可跨 GUI/Web 往返、不会混淆同版本修订的稳定标识。"""

        return self.tag

    def display_label(self) -> str:
        if self.revision == 0:
            return f"{self.version} 初始版"
        return f"{self.version} 修订 {self.revision}"


def format_release_tag(version: Any, revision: int = 0) -> str:
    """把发布身份编码为唯一且规范的 Git/GitHub tag。"""

    identity = ReleaseIdentity(version, revision)
    return _format_identity_tag(identity.version, identity.revision)


def _format_identity_tag(version: str, revision: int) -> str:
    return f"v{version}" if revision == 0 else f"v{version}-r{revision}"


def parse_release_tag(value: Any) -> ReleaseIdentity:
    """解析规范发布 tag；不接受省略 ``v`` 或显式 ``-r0`` 的别名。"""

    raw = str(value or "").strip()
    if not raw.startswith("v"):
        raise ValueError(f"invalid release tag: {value!r}")
    revision_match = _REVISION_TAG_PATTERN.fullmatch(raw)
    if revision_match:
        try:
            identity = ReleaseIdentity(revision_match.group(1), int(revision_match.group(2)))
        except ValueError as exc:
            raise ValueError(f"invalid release tag: {value!r}") from exc
    else:
        try:
            identity = ReleaseIdentity(raw[1:], 0)
        except ValueError as exc:
            raise ValueError(f"invalid release tag: {value!r}") from exc
    if identity.tag != raw:
        raise ValueError(f"invalid release tag: {value!r}")
    return identity


def _fallback_identity(version: Any) -> ReleaseIdentity:
    return ReleaseIdentity(version, 0)


def load_runtime_release_identity(
    install_root: str | Path | None = None,
    *,
    fallback_version: Any | None = None,
) -> ReleaseIdentity:
    """读取打包时写入的身份文件，缺失或损坏时兼容为初始修订。

    身份文件不是信任根；安装前仍由签名清单校验。这里的容错只保证旧安装和
    源码开发态可以启动，同时绝不采纳部分有效、部分冲突的元数据。
    """

    if fallback_version is None:
        from shared.version import __version__

        fallback_version = __version__
    fallback = _fallback_identity(fallback_version)
    root = Path(install_root) if install_root is not None else _default_install_root()
    try:
        payload = json.loads(
            _read_identity_marker(root).decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_identity_object,
        )
        if not isinstance(payload, Mapping):
            return fallback
        revision = payload.get("revision", payload.get("releaseRevision", 0))
        identity = ReleaseIdentity(str(payload.get("version") or ""), revision)
        if str(payload.get("tag") or "") != identity.tag:
            return fallback
        return identity
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return fallback


def _read_identity_marker(root: Path) -> bytes:
    directory = Path(os.path.abspath(os.fspath(root)))
    _require_regular_directory_ancestry(directory)
    with filesystem_directory_capability(directory) as capability:
        before = capability.stat_child(RELEASE_IDENTITY_FILENAME)
        if (
            not _safe_regular_file(before)
            or before.st_size > MAX_RELEASE_IDENTITY_BYTES
        ):
            raise OSError("release identity marker is not a safe regular file")
        descriptor = capability.open_child(RELEASE_IDENTITY_FILENAME, os.O_RDONLY)
        primary_error: BaseException | None = None
        try:
            opened = os.fstat(descriptor)
            after = capability.stat_child(RELEASE_IDENTITY_FILENAME)
            opened_identity = (opened.st_dev, opened.st_ino)
            if (
                not _safe_regular_file(opened)
                or not _safe_regular_file(after)
                or opened.st_size > MAX_RELEASE_IDENTITY_BYTES
                or after.st_size > MAX_RELEASE_IDENTITY_BYTES
                or opened_identity != (before.st_dev, before.st_ino)
                or opened_identity != (after.st_dev, after.st_ino)
            ):
                raise OSError("release identity marker changed during open")
            chunks: list[bytes] = []
            remaining = MAX_RELEASE_IDENTITY_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(4096, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > MAX_RELEASE_IDENTITY_BYTES:
                raise OSError("release identity marker is too large")
            return payload
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                os.close(descriptor)
            except BaseException:
                if primary_error is None:
                    raise


def _require_regular_directory_ancestry(path: Path) -> None:
    chain: list[Path] = []
    current = path
    while True:
        chain.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for candidate in reversed(chain):
        value = os.lstat(candidate)
        if not stat.S_ISDIR(value.st_mode) or _is_link_or_reparse(value):
            raise OSError("release identity directory ancestry is unsafe")


def _safe_regular_file(value: os.stat_result) -> bool:
    return (
        stat.S_ISREG(value.st_mode)
        and value.st_nlink == 1
        and not _is_link_or_reparse(value)
    )


def _is_link_or_reparse(value: os.stat_result) -> bool:
    return is_link_or_reparse(value)


def _unique_identity_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate release identity field")
        result[key] = value
    return result


def _default_install_root() -> Path:
    if bool(getattr(sys, "frozen", False)):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


__all__ = [
    "MAX_RELEASE_CANDIDATE_ID_LENGTH",
    "MAX_RELEASE_IDENTITY_BYTES",
    "MAX_RELEASE_REVISION",
    "MAX_RELEASE_VERSION_LENGTH",
    "RELEASE_IDENTITY_FILENAME",
    "ReleaseIdentity",
    "SemVer",
    "compare_semver",
    "format_release_tag",
    "load_runtime_release_identity",
    "parse_release_tag",
]
