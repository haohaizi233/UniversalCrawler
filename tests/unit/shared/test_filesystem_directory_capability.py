from __future__ import annotations

import errno
import os
import queue
import sys
import threading
import time

import pytest

import shared.filesystem_directory_capability as capability_module

_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000


def _require_symlink(link, target, *, directory: bool = False):
    kind = "directory symlink" if directory else "file symlink"
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.fail(
            f"{kind} creation failed; this test needs permission to create symbolic links "
            "(Windows: enable Developer Mode or grant SeCreateSymbolicLinkPrivilege): "
            f"{exc}"
        )


def test_snapshot_children_returns_bounded_primitive_metadata_and_skips_links(
    tmp_path,
):
    approved = tmp_path / "approved"
    approved.mkdir()
    payload = approved / "payload.bin"
    payload.write_bytes(b"payload")
    child_directory = approved / "nested"
    child_directory.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    link = approved / "outside-link"
    _require_symlink(link, outside)

    with capability_module.filesystem_directory_capability(approved) as capability:
        snapshot = capability.snapshot_children(max_entries=8)

    children = {child.name: child for child in snapshot.children}
    payload_stat = os.lstat(payload)
    directory_stat = os.lstat(child_directory)

    assert snapshot.examined == 3
    assert snapshot.truncated is False
    assert set(children) == {"nested", "payload.bin"}
    assert children["payload.bin"] == capability_module.DirectoryChildSnapshot(
        name="payload.bin",
        mode=int(payload_stat.st_mode),
        size=7,
        modified_ns=int(payload_stat.st_mtime_ns),
        identity=(int(payload_stat.st_dev), int(payload_stat.st_ino)),
    )
    assert children["payload.bin"].is_file is True
    assert children["payload.bin"].is_directory is False
    assert children["nested"].mode == int(directory_stat.st_mode)
    assert children["nested"].is_file is False
    assert children["nested"].is_directory is True


def test_snapshot_children_consumes_only_one_entry_past_the_requested_budget(
    tmp_path,
):
    approved = tmp_path / "approved"
    approved.mkdir()
    for index in range(5):
        (approved / f"payload-{index}.bin").write_bytes(bytes((index,)))

    with capability_module.filesystem_directory_capability(approved) as capability:
        truncated = capability.snapshot_children(max_entries=2)
        complete = capability.snapshot_children(max_entries=8)

    assert len(truncated.children) == 2
    assert truncated.examined == 3
    assert truncated.truncated is True
    assert len(complete.children) == 5
    assert complete.examined == 5
    assert complete.truncated is False


@pytest.mark.parametrize("max_entries", (True, 0, -1, 4097, 10**100))
def test_snapshot_children_rejects_unbounded_or_non_integer_budgets(
    tmp_path,
    max_entries,
):
    with capability_module.filesystem_directory_capability(tmp_path) as capability:
        with pytest.raises(ValueError, match="max_entries"):
            capability.snapshot_children(max_entries=max_entries)


def test_snapshot_children_bounds_hostile_iterators_by_examined_entries(
    tmp_path,
    monkeypatch,
):
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")
    next_calls = 0
    closed = False

    class EndlessDirectoryIterator:
        def __iter__(self):
            return self

        def __next__(self):
            nonlocal next_calls
            next_calls += 1
            return type("Entry", (), {"name": payload.name})()

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(
        capability_module.os,
        "scandir",
        lambda _source: EndlessDirectoryIterator(),
    )

    with capability_module.filesystem_directory_capability(tmp_path) as capability:
        snapshot = capability.snapshot_children(max_entries=2)

    assert snapshot.examined == 3
    assert snapshot.truncated is True
    assert len(snapshot.children) == 2
    assert next_calls == 3
    assert closed is True


@pytest.mark.parametrize("through_ancestor", (False, True))
def test_snapshot_children_rejects_root_or_ancestor_generation_replacement(
    tmp_path,
    through_ancestor,
):
    ancestor = tmp_path / "ancestor"
    approved = ancestor / "approved"
    approved.mkdir(parents=True)
    (approved / "payload.bin").write_bytes(b"approved")
    target = ancestor if through_ancestor else approved
    moved = tmp_path / "before-swap"

    capability = capability_module.open_filesystem_directory_capability(approved)
    try:
        try:
            target.rename(moved)
        except PermissionError:
            snapshot = capability.snapshot_children(max_entries=4)
            assert [child.name for child in snapshot.children] == ["payload.bin"]
            return

        if through_ancestor:
            replacement = target / approved.name
            replacement.mkdir(parents=True)
        else:
            target.mkdir()
        with pytest.raises(OSError, match="identity changed"):
            capability.snapshot_children(max_entries=4)
    finally:
        capability.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor enumeration contract")
def test_snapshot_children_posix_enumerates_the_held_descriptor_without_path_reopen(
    tmp_path,
    monkeypatch,
):
    (tmp_path / "payload.bin").write_bytes(b"payload")
    real_scandir = os.scandir

    def descriptor_only_scandir(source):
        if type(source) is not int:
            raise AssertionError("POSIX directory snapshot reopened a path")
        return real_scandir(source)

    monkeypatch.setattr(capability_module.os, "scandir", descriptor_only_scandir)

    with capability_module.filesystem_directory_capability(tmp_path) as capability:
        snapshot = capability.snapshot_children(max_entries=4)
        capability.assert_bound()

    assert [child.name for child in snapshot.children] == ["payload.bin"]


def test_snapshot_children_preserves_first_control_flow_over_hostile_close(
    tmp_path,
    monkeypatch,
):
    primary = KeyboardInterrupt("directory enumeration interrupted")
    cleanup = SystemExit("directory iterator close interrupted")

    class HostileDirectoryIterator:
        def __iter__(self):
            return self

        def __next__(self):
            raise primary

        def close(self) -> None:
            raise cleanup

    monkeypatch.setattr(
        capability_module.os,
        "scandir",
        lambda _source: HostileDirectoryIterator(),
    )

    with capability_module.filesystem_directory_capability(tmp_path) as capability:
        with pytest.raises(KeyboardInterrupt) as exc_info:
            capability.snapshot_children(max_entries=4)

    assert exc_info.value is primary


def test_open_capability_can_be_asserted_and_closed_from_another_thread(
    tmp_path,
):
    capability = capability_module.open_filesystem_directory_capability(tmp_path)
    outcomes: queue.Queue[BaseException | None] = queue.Queue()

    def verify_and_close() -> None:
        try:
            capability.assert_bound()
            capability.close()
            capability.close()
        except BaseException as exc:
            outcomes.put(exc)
        else:
            outcomes.put(None)

    worker = threading.Thread(target=verify_and_close)
    worker.start()
    worker.join(1.0)

    assert not worker.is_alive()
    assert outcomes.get_nowait() is None
    capability.close()
    with pytest.raises(OSError, match="closed"):
        capability.assert_bound()


def test_open_capability_is_not_registered_as_an_active_context_capability(
    tmp_path,
):
    owned = capability_module.open_filesystem_directory_capability(tmp_path)
    try:
        with capability_module.filesystem_directory_capability(tmp_path) as scoped:
            assert scoped is not owned
            with capability_module.filesystem_directory_capability(
                tmp_path
            ) as nested:
                assert nested is scoped
    finally:
        owned.close()


def test_open_capability_projects_the_identity_of_the_same_live_handle(tmp_path):
    approved = tmp_path / "approved"
    approved.mkdir()
    capability = capability_module.open_filesystem_directory_capability(approved)
    try:
        root, identity = capability.snapshot_bound_identity()
        current = os.lstat(approved)

        assert root == approved.absolute()
        assert identity == (int(current.st_dev), int(current.st_ino))
    finally:
        capability.close()


def test_open_capability_rejects_a_replacement_root_generation(tmp_path):
    approved = tmp_path / "approved"
    approved.mkdir()
    moved = tmp_path / "approved-before-swap"
    capability = capability_module.open_filesystem_directory_capability(approved)
    try:
        try:
            approved.rename(moved)
        except PermissionError:
            capability.assert_bound()
            assert approved.is_dir()
            return

        approved.mkdir()
        with pytest.raises(OSError, match="identity changed"):
            capability.assert_bound()

        replacement = capability_module.open_filesystem_directory_capability(approved)
        try:
            replacement.assert_bound()
        finally:
            replacement.close()
    finally:
        capability.close()


@pytest.mark.parametrize("through_ancestor", (False, True))
def test_open_capability_rejects_directory_reparse_points(
    tmp_path,
    through_ancestor,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    if through_ancestor:
        (outside / "approved").mkdir()
        candidate = tmp_path / "ancestor-link"
        target = candidate / "approved"
    else:
        candidate = tmp_path / "approved-link"
        target = candidate
    _require_symlink(candidate, outside, directory=True)

    with pytest.raises(OSError, match="regular directory"):
        capability_module.open_filesystem_directory_capability(target)


def test_open_capability_prefers_close_control_flow_over_identity_error(
    tmp_path,
    monkeypatch,
):
    identity_error = OSError("directory identity changed")
    close_control = KeyboardInterrupt("directory close interrupted")
    calls: list[str] = []

    class FailingCapability:
        def assert_bound(self) -> None:
            calls.append("assert")
            raise identity_error

        def close(self) -> None:
            calls.append("close")
            raise close_control

    monkeypatch.setattr(
        capability_module,
        "_require_regular_directory_ancestry",
        lambda _path: None,
    )
    monkeypatch.setattr(
        capability_module,
        "_open_directory_capability",
        lambda _path: FailingCapability(),
    )

    with pytest.raises(KeyboardInterrupt) as exc_info:
        capability_module.open_filesystem_directory_capability(tmp_path)

    assert exc_info.value is close_control
    assert calls == ["assert", "close"]


def test_posix_generation_lease_opens_nonblocking_before_type_check(
    tmp_path,
    monkeypatch,
):
    seed = os.open(tmp_path / "seed.bin", os.O_RDWR | os.O_CREAT)
    capability = capability_module.FilesystemDirectoryCapability(
        tmp_path,
        descriptor=91,
        identity=(0, 0),
    )
    flags_seen: list[int] = []
    nonblocking = 0x800

    def fake_open(_name, flags, **_kwargs):
        flags_seen.append(flags)
        return os.dup(seed)

    monkeypatch.setattr(capability, "assert_bound", lambda: None)
    monkeypatch.setattr(capability_module.os, "open", fake_open)
    monkeypatch.setattr(
        capability_module.os,
        "O_NONBLOCK",
        nonblocking,
        raising=False,
    )

    descriptor = capability.open_owned_generation_lease(
        "a" * 64 + "-target.bin"
    )
    os.close(descriptor)
    os.close(seed)

    assert flags_seen and flags_seen[0] & nonblocking


def test_posix_open_child_passes_nofollow_and_directory_fd(tmp_path, monkeypatch):
    nofollow = getattr(os, "O_NOFOLLOW", 0) or 0x20000
    monkeypatch.setattr(capability_module.os, "O_NOFOLLOW", nofollow, raising=False)
    seen: dict[str, object] = {}

    def fake_open(name, flags, mode=0o777, *, dir_fd=None):
        seen["name"] = name
        seen["flags"] = flags
        seen["dir_fd"] = dir_fd
        return 77

    capability = capability_module.FilesystemDirectoryCapability(
        tmp_path,
        descriptor=91,
        identity=(0, 0),
    )
    monkeypatch.setattr(capability_module.os, "open", fake_open)
    monkeypatch.setattr(capability, "assert_bound", lambda: None)

    descriptor = capability.open_child("payload.bin", os.O_RDONLY)

    assert descriptor == 77
    assert seen["name"] == "payload.bin"
    assert int(seen["flags"]) & nofollow == nofollow
    assert seen["dir_fd"] == 91


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux FIFO nonblocking integration contract",
)
def test_linux_generation_lease_rejects_fifo_without_blocking(tmp_path):
    target_name = "a" * 64 + "-target.fifo"
    target = tmp_path / target_name
    os.mkfifo(target)
    outcomes: queue.Queue[BaseException | int] = queue.Queue()

    with capability_module.filesystem_directory_capability(tmp_path) as capability:
        def open_fifo() -> None:
            try:
                outcomes.put(capability.open_owned_generation_lease(target_name))
            except BaseException as exc:
                outcomes.put(exc)

        started = time.monotonic()
        worker = threading.Thread(target=open_fifo, daemon=True)
        worker.start()
        worker.join(0.5)
        if worker.is_alive():
            writer = os.open(target, os.O_WRONLY | os.O_NONBLOCK)
            os.close(writer)
            worker.join(1.0)
            pytest.fail("FIFO lease open blocked before regular-file validation")
        elapsed = time.monotonic() - started

    outcome = outcomes.get_nowait()
    if isinstance(outcome, int):
        os.close(outcome)
        pytest.fail("FIFO was accepted as a published generation")
    assert isinstance(outcome, OSError)
    assert elapsed < 0.5


def test_linux_owned_generation_is_created_with_otmpfile(monkeypatch):
    calls: list[tuple[object, ...]] = []
    temporary_flag = 0x410000

    def fake_open(*args, **kwargs):
        calls.append((*args, kwargs))
        return 701

    monkeypatch.setattr(capability_module.os, "open", fake_open)
    monkeypatch.setattr(
        capability_module.os,
        "set_inheritable",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        capability_module.os,
        "O_TMPFILE",
        temporary_flag,
        raising=False,
    )

    descriptor = capability_module._create_linux_owned_generation(91, 0o640)

    assert descriptor == 701
    assert calls == [
        (
            ".",
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | temporary_flag,
            0o640,
            {"dir_fd": 91},
        )
    ]


def test_linux_publish_links_the_exact_source_descriptor(monkeypatch):
    calls: list[tuple[int, bytes, int, bytes, int]] = []

    def fake_linkat(
        source_directory: int,
        source_name: bytes,
        target_directory: int,
        target_name: bytes,
        flags: int,
    ) -> None:
        calls.append(
            (
                source_directory,
                source_name,
                target_directory,
                target_name,
                flags,
            )
        )

    monkeypatch.setattr(capability_module, "_call_linkat", fake_linkat)

    capability_module._publish_linux_descriptor_no_replace(
        72,
        93,
        "a" * 64 + "-installer.exe",
    )

    assert calls == [
        (
            72,
            b"",
            93,
            ("a" * 64 + "-installer.exe").encode(),
            capability_module._AT_EMPTY_PATH,
        )
    ]


def test_linux_publish_uses_procfd_only_for_empty_path_unavailability(monkeypatch):
    calls: list[tuple[int, bytes, int, bytes, int]] = []

    def fake_linkat(
        source_directory: int,
        source_name: bytes,
        target_directory: int,
        target_name: bytes,
        flags: int,
    ) -> None:
        calls.append(
            (
                source_directory,
                source_name,
                target_directory,
                target_name,
                flags,
            )
        )
        if len(calls) == 1:
            raise OSError(errno.EPERM, "AT_EMPTY_PATH unavailable")

    monkeypatch.setattr(capability_module, "_call_linkat", fake_linkat)

    capability_module._publish_linux_descriptor_no_replace(72, 93, "target.bin")

    assert calls == [
        (72, b"", 93, b"target.bin", capability_module._AT_EMPTY_PATH),
        (
            capability_module._AT_FDCWD,
            b"/proc/self/fd/72",
            93,
            b"target.bin",
            capability_module._AT_SYMLINK_FOLLOW,
        ),
    ]


def test_linux_publish_does_not_retry_an_ambiguous_native_failure(monkeypatch):
    calls = 0

    def fake_linkat(*_args) -> None:
        nonlocal calls
        calls += 1
        raise OSError(errno.EIO, "ambiguous filesystem failure")

    monkeypatch.setattr(capability_module, "_call_linkat", fake_linkat)

    with pytest.raises(OSError) as exc_info:
        capability_module._publish_linux_descriptor_no_replace(
            72,
            93,
            "target.bin",
        )

    assert exc_info.value.errno == errno.EIO
    assert calls == 1


def test_windows_publish_uses_same_source_handle_absolute_target_and_no_replace(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[int, int, str, bool]] = []
    monkeypatch.setattr(
        capability_module,
        "_windows_raw_handle_from_descriptor",
        lambda descriptor: descriptor + 1000,
    )
    monkeypatch.setattr(
        capability_module,
        "_set_windows_handle_rename",
        lambda source, target, name, *, replace: calls.append(
            (source, target, name, replace)
        ),
    )

    capability_module._publish_windows_descriptor_no_replace(
        17,
        tmp_path / "target.bin",
    )

    assert calls == [(1017, 0, str(tmp_path / "target.bin"), False)]


def test_darwin_publish_clones_from_the_exact_source_descriptor(monkeypatch):
    calls: list[tuple[int, int, bytes, int]] = []
    monkeypatch.setattr(
        capability_module,
        "_call_fclonefileat",
        lambda source, target, name, flags: calls.append(
            (source, target, name, flags)
        ),
    )

    capability_module._publish_darwin_descriptor_no_replace(
        17,
        29,
        "target.bin",
    )

    assert calls == [(17, 29, b"target.bin", 0)]


def test_darwin_owned_generation_creation_is_explicitly_unsupported(
    tmp_path,
    monkeypatch,
):
    capability = capability_module.FilesystemDirectoryCapability(
        tmp_path,
        descriptor=73,
        identity=(0, 0),
    )
    monkeypatch.setattr(capability, "assert_bound", lambda: None)
    monkeypatch.setattr(sys, "platform", "darwin")

    with pytest.raises(OSError) as exc_info:
        capability.create_owned_generation()

    assert exc_info.value.errno == errno.ENOTSUP


def test_committed_publish_postcheck_failure_returns_uncertain_receipt(
    tmp_path,
    monkeypatch,
):
    source_descriptor = os.open(tmp_path / "source.bin", os.O_RDWR | os.O_CREAT)
    capability = capability_module.FilesystemDirectoryCapability(
        tmp_path,
        descriptor=91,
        identity=(0, 0),
    )
    source = capability_module.OwnedGenerationHandle._for_test(
        capability,
        source_descriptor,
        platform="linux",
    )
    checks = iter((None, OSError("root swapped after commit")))

    def assert_bound() -> None:
        outcome = next(checks)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(capability, "assert_bound", assert_bound)
    monkeypatch.setattr(
        capability_module,
        "_publish_linux_descriptor_no_replace",
        lambda *_args: None,
    )

    receipt = capability.publish_owned_generation_no_replace(
        source,
        "a" * 64 + "-installer.exe",
    )

    assert receipt.status is capability_module.DescriptorPublishStatus.COMMITTED_AUTHORITY_UNCERTAIN
    assert source.published is True
    source.detach_published_descriptor()
    os.close(source_descriptor)


def test_owned_generation_publish_rejects_a_fixed_non_digest_target(
    tmp_path,
    monkeypatch,
):
    descriptor = os.open(tmp_path / "source.bin", os.O_RDWR | os.O_CREAT)
    capability = capability_module.FilesystemDirectoryCapability(
        tmp_path,
        descriptor=91,
        identity=(0, 0),
    )
    source = capability_module.OwnedGenerationHandle._for_test(
        capability,
        descriptor,
        platform="linux",
    )
    monkeypatch.setattr(capability, "assert_bound", lambda: None)

    with pytest.raises(OSError, match="not content-addressed"):
        capability.publish_owned_generation_no_replace(
            source,
            "installer.exe",
        )

    assert source.publish_attempted is False
    source.close()


def test_context_exit_marks_committed_receipt_uncertain_without_retry_error(
    tmp_path,
    monkeypatch,
):
    descriptor = os.open(
        tmp_path / "capability-handle.bin",
        os.O_RDWR | os.O_CREAT,
    )
    capability = capability_module.FilesystemDirectoryCapability(
        tmp_path,
        descriptor=descriptor,
        identity=(0, 0),
    )
    receipt = capability._new_committed_receipt("target.bin")
    monkeypatch.setattr(
        capability,
        "assert_bound",
        lambda: (_ for _ in ()).throw(OSError("root postcheck failed")),
    )

    capability_module._finish_capability(capability, None)

    assert receipt.status is capability_module.DescriptorPublishStatus.COMMITTED_AUTHORITY_UNCERTAIN
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_ambiguous_windows_publish_abandon_never_deletes_by_handle(
    tmp_path,
    monkeypatch,
):
    descriptor = os.open(tmp_path / "source.bin", os.O_RDWR | os.O_CREAT)
    capability = capability_module.FilesystemDirectoryCapability(
        tmp_path,
        descriptor=91,
        identity=(0, 0),
    )
    source = capability_module.OwnedGenerationHandle._for_test(
        capability,
        descriptor,
        platform="windows",
    )
    source._begin_publish()
    delete_calls: list[int] = []
    monkeypatch.setattr(
        capability_module,
        "_delete_windows_generation_descriptor",
        lambda value: delete_calls.append(value),
    )

    source.abandon_after_ambiguous_publish()

    assert delete_calls == []
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_known_collision_cleanup_closes_descriptor_and_preserves_control_flow(
    tmp_path,
    monkeypatch,
):
    descriptor = os.open(tmp_path / "source.bin", os.O_RDWR | os.O_CREAT)
    capability = capability_module.FilesystemDirectoryCapability(
        tmp_path,
        descriptor=91,
        identity=(0, 0),
    )
    source = capability_module.OwnedGenerationHandle._for_test(
        capability,
        descriptor,
        platform="windows",
    )
    source._begin_publish()
    control = KeyboardInterrupt("exact cleanup interrupted")
    monkeypatch.setattr(
        capability_module,
        "_delete_windows_generation_descriptor",
        lambda _value: (_ for _ in ()).throw(control),
    )

    with pytest.raises(KeyboardInterrupt) as exc_info:
        source.discard_after_known_no_commit()

    assert exc_info.value is control
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_capability_closes_after_exit_identity_failure(tmp_path, monkeypatch):
    close_calls = 0

    class FailingCapability:
        def assert_bound(self) -> None:
            raise OSError("directory identity changed")

        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    monkeypatch.setattr(
        capability_module,
        "_require_regular_directory_ancestry",
        lambda _path: None,
    )
    monkeypatch.setattr(
        capability_module,
        "_open_directory_capability",
        lambda _path: FailingCapability(),
    )

    with pytest.raises(OSError, match="identity changed"):
        with capability_module.filesystem_directory_capability(tmp_path):
            pass

    assert close_calls == 1


def test_windows_capability_close_prefers_control_flow_after_ordinary_failure(
    tmp_path,
    monkeypatch,
):
    ordinary = OSError("first handle close failed")
    control = KeyboardInterrupt("second handle close interrupted")
    closed: list[int] = []
    capability = capability_module.FilesystemDirectoryCapability(
        tmp_path,
        windows_handles=((101, 1), (202, 2)),
        identity=(0, 2),
    )

    def close_handle(handle: int) -> None:
        closed.append(handle)
        if handle == 202:
            raise ordinary
        raise control

    monkeypatch.setattr(capability_module, "_close_windows_handle", close_handle)

    with pytest.raises(KeyboardInterrupt) as exc_info:
        capability.close()

    assert exc_info.value is control
    assert closed == [202, 101]


def test_finish_capability_prefers_close_control_flow_over_identity_error():
    identity_error = OSError("directory identity changed")
    close_control = SystemExit("handle close interrupted")
    calls: list[str] = []

    class FailingCapability:
        def assert_bound(self) -> None:
            calls.append("assert")
            raise identity_error

        def close(self) -> None:
            calls.append("close")
            raise close_control

    with pytest.raises(SystemExit) as exc_info:
        capability_module._finish_capability(FailingCapability(), None)

    assert exc_info.value is close_control
    assert calls == ["assert", "close"]


def test_context_preserves_body_error_over_identity_and_close_control_flow(
    tmp_path,
    monkeypatch,
):
    primary = RuntimeError("body failed")
    calls: list[str] = []

    class FailingCapability:
        def assert_bound(self) -> None:
            calls.append("assert")
            raise KeyboardInterrupt("identity check interrupted")

        def close(self) -> None:
            calls.append("close")
            raise SystemExit("handle close interrupted")

    monkeypatch.setattr(
        capability_module,
        "_require_regular_directory_ancestry",
        lambda _path: None,
    )
    monkeypatch.setattr(
        capability_module,
        "_open_directory_capability",
        lambda _path: FailingCapability(),
    )

    with pytest.raises(RuntimeError) as exc_info:
        with capability_module.filesystem_directory_capability(tmp_path):
            raise primary

    assert exc_info.value is primary
    assert calls == ["assert", "close"]


@pytest.mark.parametrize(
    "name",
    (
        "manifest.json:stream",
        "manifest.json.",
        "manifest.json ",
        "CON",
        "con.json",
        "AUX.txt",
        "COM1.log",
        "LPT9",
        "CONIN$",
        "conin$.log",
        "CONOUT$",
        "conout$.json",
        "CLOCK$",
        "clock$.log",
        "COM¹",
        "com¹.log",
        "COM²",
        "com².log",
        "COM³",
        "com³.log",
        "LPT¹",
        "lpt¹.log",
        "LPT²",
        "lpt².log",
        "LPT³",
        "lpt³.log",
        "bad\x1fname",
    ),
)
def test_capability_rejects_windows_alias_child_names(tmp_path, name):
    with capability_module.filesystem_directory_capability(tmp_path) as capability:
        with pytest.raises(OSError, match="child name is invalid"):
            capability.stat_child(name)


def test_capability_rejects_hostile_non_string_name_without_coercion(tmp_path):
    class HostileName:
        def __bool__(self) -> bool:
            raise SystemExit("child-name truthiness must not run")

        def __str__(self) -> str:
            raise SystemExit("child-name stringification must not run")

    with capability_module.filesystem_directory_capability(tmp_path) as capability:
        with pytest.raises(OSError, match="child name is invalid"):
            capability.stat_child(HostileName())  # type: ignore[arg-type]


def test_capability_rejects_lone_surrogate_child_name(tmp_path):
    with capability_module.filesystem_directory_capability(tmp_path) as capability:
        with pytest.raises(OSError, match="child name is invalid"):
            capability.stat_child("bad\ud800name")


def test_open_preserves_identity_error_over_descriptor_close_failure(
    tmp_path,
    monkeypatch,
):
    class HostileCleanup(BaseException):
        pass

    primary = OSError("directory identity changed")
    cleanup = HostileCleanup("descriptor close failed")
    capability = capability_module.FilesystemDirectoryCapability(
        tmp_path,
        descriptor=91,
        identity=(0, 0),
    )
    checks = iter((None, primary))
    real_close = os.close

    def assert_bound() -> None:
        outcome = next(checks)
        if outcome is not None:
            raise outcome

    def close(descriptor: int) -> None:
        if descriptor == 73:
            raise cleanup
        real_close(descriptor)

    monkeypatch.setattr(capability, "assert_bound", assert_bound)
    monkeypatch.setattr(capability_module.os, "open", lambda *_a, **_k: 73)
    monkeypatch.setattr(capability_module.os, "close", close)

    with pytest.raises(OSError) as exc_info:
        capability.open_child("payload.bin", os.O_RDONLY)

    assert exc_info.value is primary
    if hasattr(BaseException, "add_note"):
        assert getattr(primary, "__notes__", []) == [
            "approved directory child descriptor cleanup failed"
        ]


def test_capability_rejects_ancestor_replacement_after_acquisition(tmp_path):
    ancestor = tmp_path / "ancestor"
    approved = ancestor / "approved"
    approved.mkdir(parents=True)
    (approved / "payload.bin").write_bytes(b"approved")
    moved_ancestor = tmp_path / "ancestor-before-swap"
    outside_ancestor = tmp_path / "outside-ancestor"
    outside_approved = outside_ancestor / approved.name
    outside_approved.mkdir(parents=True)
    outside_payload = outside_approved / "payload.bin"
    outside_payload.write_bytes(b"outside")
    probe = tmp_path / "directory-link-probe"
    _require_symlink(probe, outside_ancestor, directory=True)
    probe.unlink()

    with pytest.raises(OSError):
        with capability_module.filesystem_directory_capability(approved) as capability:
            ancestor.rename(moved_ancestor)
            ancestor.symlink_to(outside_ancestor, target_is_directory=True)
            descriptor = capability.open_child("payload.bin", os.O_WRONLY)
            try:
                os.write(descriptor, b"mutated")
            finally:
                os.close(descriptor)

    assert outside_payload.read_bytes() == b"outside"


def test_derived_child_capability_cannot_outlive_parent_authority(tmp_path):
    ancestor = tmp_path / "ancestor"
    approved = ancestor / "approved"
    child_path = approved / "child"
    child_path.mkdir(parents=True)
    (child_path / "payload.bin").write_bytes(b"approved")
    outside_ancestor = tmp_path / "outside-ancestor"
    outside_child = outside_ancestor / approved.name / child_path.name
    outside_child.mkdir(parents=True)
    outside_payload = outside_child / "payload.bin"
    outside_payload.write_bytes(b"outside")
    moved_ancestor = tmp_path / "ancestor-before-swap"
    probe = tmp_path / "directory-link-probe"
    _require_symlink(probe, outside_ancestor, directory=True)
    probe.unlink()

    parent = capability_module._open_directory_capability(approved)
    child = parent.open_directory_child(child_path.name)
    parent.close()
    try:
        with pytest.raises(OSError):
            ancestor.rename(moved_ancestor)
            ancestor.symlink_to(outside_ancestor, target_is_directory=True)
            child.open_child("payload.bin", os.O_WRONLY)
    finally:
        child.close()

    assert outside_payload.read_bytes() == b"outside"


def _final_file_symlink(tmp_path):
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    link = approved / "payload.bin"
    _require_symlink(link, outside)
    return approved, link, outside


def test_capability_stat_rejects_final_file_symlink(tmp_path):
    approved, link, outside = _final_file_symlink(tmp_path)

    with capability_module.filesystem_directory_capability(approved) as capability:
        with pytest.raises(OSError, match="reparse|link"):
            capability.stat_child(link.name)

    assert outside.read_bytes() == b"outside"


def test_capability_open_rejects_final_file_symlink(tmp_path):
    approved, link, outside = _final_file_symlink(tmp_path)
    descriptor: int | None = None

    try:
        with capability_module.filesystem_directory_capability(approved) as capability:
            with pytest.raises(OSError):
                descriptor = capability.open_child(
                    link.name,
                    os.O_WRONLY | os.O_TRUNC,
                )
    finally:
        if descriptor is not None:
            os.close(descriptor)

    assert outside.read_bytes() == b"outside"


def test_capability_open_rejects_file_replaced_by_symlink_after_stat(
    tmp_path,
    monkeypatch,
):
    approved = tmp_path / "approved"
    approved.mkdir()
    payload = approved / "payload.bin"
    payload.write_bytes(b"approved")
    original = approved / "payload-before-swap.bin"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    replacement = approved / "replacement-link"
    _require_symlink(replacement, outside)
    swapped = False

    def swap() -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            payload.replace(original)
            replacement.replace(payload)

    if os.name == "nt":
        real_create = capability_module._create_windows_child_handle

        def create_then_swap(*args, **kwargs):
            swap()
            return real_create(*args, **kwargs)

        monkeypatch.setattr(
            capability_module,
            "_create_windows_child_handle",
            create_then_swap,
        )
    else:
        real_open = capability_module.os.open

        def open_then_swap(*args, **kwargs):
            swap()
            return real_open(*args, **kwargs)

        monkeypatch.setattr(capability_module.os, "open", open_then_swap)

    descriptor: int | None = None
    try:
        with capability_module.filesystem_directory_capability(approved) as capability:
            with pytest.raises(OSError):
                descriptor = capability.open_child(
                    payload.name,
                    os.O_WRONLY | os.O_TRUNC,
                )
    finally:
        if descriptor is not None:
            os.close(descriptor)

    assert payload.is_symlink()
    assert outside.read_bytes() == b"outside"


def test_capability_unlink_rejects_final_file_symlink(tmp_path):
    approved, link, outside = _final_file_symlink(tmp_path)

    with capability_module.filesystem_directory_capability(approved) as capability:
        with pytest.raises(OSError, match="reparse|link"):
            capability.unlink_child(link.name)

    assert link.is_symlink()
    assert outside.read_bytes() == b"outside"


def test_capability_replace_rejects_final_target_symlink(tmp_path):
    approved, link, outside = _final_file_symlink(tmp_path)
    source = approved / "source.bin"
    source.write_bytes(b"approved")

    with capability_module.filesystem_directory_capability(approved) as capability:
        with pytest.raises(OSError, match="reparse|link"):
            capability.replace_child(source.name, capability, link.name)

    assert source.read_bytes() == b"approved"
    assert link.is_symlink()
    assert outside.read_bytes() == b"outside"


def test_capability_replace_child_linearizes_at_the_os_source_name(
    tmp_path,
    monkeypatch,
):
    approved = tmp_path / "approved"
    approved.mkdir()
    source = approved / "source.bin"
    original = approved / "source-original.bin"
    target = approved / "target.bin"
    source.write_bytes(b"verified")
    real_replace = capability_module.os.replace
    swapped = False

    def replace_source_then_publish(source_path, target_path, *args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            real_replace(source, original)
            source.write_bytes(b"later generation")
        return real_replace(source_path, target_path, *args, **kwargs)

    monkeypatch.setattr(capability_module.os, "replace", replace_source_then_publish)

    with capability_module.filesystem_directory_capability(approved) as capability:
        capability.replace_child(source.name, capability, target.name)

    assert original.read_bytes() == b"verified"
    assert not source.exists()
    assert target.read_bytes() == b"later generation"


def test_capability_stat_rejects_final_directory_reparse(tmp_path):
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = tmp_path / "outside-directory"
    outside.mkdir()
    link = approved / "directory-link"
    _require_symlink(link, outside, directory=True)

    with capability_module.filesystem_directory_capability(approved) as capability:
        with pytest.raises(OSError, match="reparse|link"):
            capability.stat_child(link.name)


@pytest.mark.parametrize("preexisting", (False, True))
def test_capability_create_truncate_uses_verified_handle(tmp_path, preexisting):
    approved = tmp_path / "approved"
    approved.mkdir()
    target = approved / "payload.bin"
    if preexisting:
        target.write_bytes(b"old content")

    with capability_module.filesystem_directory_capability(approved) as capability:
        descriptor = capability.open_child(
            target.name,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        )
        try:
            os.write(descriptor, b"new")
        finally:
            os.close(descriptor)

    assert target.read_bytes() == b"new"


@pytest.mark.skipif(os.name != "nt", reason="Windows CreateFileW contract")
def test_windows_createfile_opens_reparse_point_and_closes_that_handle(
    tmp_path,
    monkeypatch,
):
    issued_handle = 0x1501
    created_flags: list[int] = []
    returned_handles: list[int] = []
    closed: list[int] = []

    def create_file(*args):
        created_flags.append(int(args[5]))
        returned_handles.append(issued_handle)
        return issued_handle

    def get_information(_handle, information):
        structure = information._obj
        structure.dwFileAttributes = 0x400
        structure.nFileIndexHigh = 0
        structure.nFileIndexLow = 7
        return 1

    def close_handle(handle):
        closed.append(int(handle))
        return 1

    class _Kernel32:
        def __init__(self, *_args, **_kwargs):
            self.CreateFileW = create_file
            self.GetFileInformationByHandle = get_information
            self.CloseHandle = close_handle

    monkeypatch.setattr(capability_module.ctypes, "WinDLL", _Kernel32)

    with pytest.raises(OSError, match="reparse"):
        capability_module._open_windows_child_file(
            tmp_path / "payload.bin",
            os.O_RDONLY,
        )

    assert created_flags
    assert created_flags[0] & _FILE_FLAG_OPEN_REPARSE_POINT == _FILE_FLAG_OPEN_REPARSE_POINT
    assert closed == returned_handles == [issued_handle]


def test_capability_rename_child_no_replace_moves_regular_file(tmp_path):
    approved = tmp_path / "approved"
    approved.mkdir()
    source = approved / "source.bin"
    target = approved / "target.bin"
    source.write_bytes(b"approved")

    with capability_module.filesystem_directory_capability(approved) as capability:
        capability.rename_child_no_replace(source.name, capability, target.name)

    assert not source.exists()
    assert target.read_bytes() == b"approved"


def test_capability_rename_child_no_replace_refuses_existing_target(tmp_path):
    approved = tmp_path / "approved"
    approved.mkdir()
    source = approved / "source.bin"
    target = approved / "target.bin"
    source.write_bytes(b"approved")
    target.write_bytes(b"existing")

    with capability_module.filesystem_directory_capability(approved) as capability:
        with pytest.raises(FileExistsError):
            capability.rename_child_no_replace(source.name, capability, target.name)

    assert source.read_bytes() == b"approved"
    assert target.read_bytes() == b"existing"


def test_capability_rename_child_no_replace_rejects_reparse_target(tmp_path):
    approved, target, outside = _final_file_symlink(tmp_path)
    source = approved / "source.bin"
    source.write_bytes(b"approved")

    with capability_module.filesystem_directory_capability(approved) as capability:
        with pytest.raises(OSError, match="reparse|link"):
            capability.rename_child_no_replace(source.name, capability, target.name)

    assert source.read_bytes() == b"approved"
    assert target.is_symlink()
    assert outside.read_bytes() == b"outside"


def test_capability_rename_child_no_replace_wins_target_creation_race(
    tmp_path,
    monkeypatch,
):
    approved = tmp_path / "approved"
    approved.mkdir()
    source = approved / "source.bin"
    target = approved / "target.bin"
    source.write_bytes(b"approved")

    if os.name == "nt":
        real_rename = capability_module.os.rename

        def create_target_then_rename(*args, **kwargs):
            target.write_bytes(b"racer")
            return real_rename(*args, **kwargs)

        monkeypatch.setattr(capability_module.os, "rename", create_target_then_rename)
    else:
        real_rename = capability_module._rename_posix_no_replace

        def create_target_then_rename(*args, **kwargs):
            target.write_bytes(b"racer")
            return real_rename(*args, **kwargs)

        monkeypatch.setattr(
            capability_module,
            "_rename_posix_no_replace",
            create_target_then_rename,
        )

    with capability_module.filesystem_directory_capability(approved) as capability:
        with pytest.raises(FileExistsError):
            capability.rename_child_no_replace(source.name, capability, target.name)

    assert source.read_bytes() == b"approved"
    assert target.read_bytes() == b"racer"


def test_capability_rename_child_no_replace_linearizes_at_the_os_source_name(
    tmp_path,
    monkeypatch,
):
    approved = tmp_path / "approved"
    approved.mkdir()
    source = approved / "source.bin"
    original = approved / "source-original.bin"
    target = approved / "target.bin"
    source.write_bytes(b"verified")
    swapped = False

    if os.name == "nt":
        real_rename = capability_module.os.rename

        def replace_source_then_rename(source_path, target_path, *args, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                real_rename(source, original)
                source.write_bytes(b"later generation")
            return real_rename(source_path, target_path, *args, **kwargs)

        monkeypatch.setattr(
            capability_module.os,
            "rename",
            replace_source_then_rename,
        )
    else:
        real_rename = capability_module._rename_posix_no_replace

        def replace_source_then_rename(*args, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                source.rename(original)
                source.write_bytes(b"later generation")
            return real_rename(*args, **kwargs)

        monkeypatch.setattr(
            capability_module,
            "_rename_posix_no_replace",
            replace_source_then_rename,
        )

    with capability_module.filesystem_directory_capability(approved) as capability:
        capability.rename_child_no_replace(source.name, capability, target.name)

    assert original.read_bytes() == b"verified"
    assert not source.exists()
    assert target.read_bytes() == b"later generation"


def test_posix_chain_preserves_acquisition_error_over_close_failure(
    tmp_path,
    monkeypatch,
):
    class HostileCleanup(BaseException):
        pass

    primary = OSError("directory identity lookup failed")
    cleanup = HostileCleanup("descriptor cleanup failed")
    real_close = os.close
    monkeypatch.setattr(capability_module.os, "open", lambda *_a, **_k: 881)
    monkeypatch.setattr(
        capability_module.os,
        "fstat",
        lambda _descriptor: (_ for _ in ()).throw(primary),
    )

    def close(descriptor: int) -> None:
        if descriptor == 881:
            raise cleanup
        real_close(descriptor)

    monkeypatch.setattr(capability_module.os, "close", close)

    with pytest.raises(OSError) as exc_info:
        capability_module._open_posix_directory_chain(tmp_path)

    assert exc_info.value is primary


def test_windows_chain_preserves_acquisition_error_over_handle_close_failure(
    tmp_path,
    monkeypatch,
):
    class HostileCleanup(BaseException):
        pass

    primary = OSError("directory handle inspection failed")
    cleanup = HostileCleanup("handle cleanup failed")
    monkeypatch.setattr(
        capability_module,
        "_open_windows_directory_handle",
        lambda _path: 991,
    )
    monkeypatch.setattr(
        capability_module,
        "_windows_handle_information",
        lambda _handle: (_ for _ in ()).throw(primary),
    )
    monkeypatch.setattr(
        capability_module,
        "_close_windows_handle",
        lambda _handle: (_ for _ in ()).throw(cleanup),
    )

    with pytest.raises(OSError) as exc_info:
        capability_module._open_windows_directory_chain(tmp_path)

    assert exc_info.value is primary
