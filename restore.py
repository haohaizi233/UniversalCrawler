#!/usr/bin/env python3
"""Restore an ordinary-Git LFS archive into this clone's local LFS cache."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


DEFAULT_ARCHIVE_REF = "origin/migration/lfs-archive"
MAX_CHUNK_SIZE = 40 * 1024 * 1024
OID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RestoreError(RuntimeError):
    pass


def git(root, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
    )
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RestoreError(f"git {' '.join(args[:3])} failed: {detail or result.returncode}")
    return result


def find_root(start):
    return Path(git(start, "rev-parse", "--show-toplevel").stdout.decode("utf-8").strip()).resolve()


def checked_manifest(root, archive_ref):
    raw = git(root, "show", f"{archive_ref}:manifest.json").stdout
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RestoreError(f"Archive manifest is not valid UTF-8 JSON: {exc}") from None
    if not isinstance(manifest, dict):
        raise RestoreError("Archive manifest is not a JSON object.")
    if manifest.get("format") != "gitee-lfs-archive-v1":
        raise RestoreError("Unsupported or missing LFS archive format.")
    chunk_size = manifest.get("chunk_size")
    if type(chunk_size) is not int or not 1 <= chunk_size <= MAX_CHUNK_SIZE:
        raise RestoreError("Archive manifest has an invalid chunk size.")
    objects = manifest.get("objects")
    if not isinstance(objects, list):
        raise RestoreError("Archive manifest has no object list.")
    return raw, manifest


def _valid_existing(path, oid, expected_size):
    if path.is_symlink() or not path.is_file():
        return False
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            data = stream.read(1024 * 1024)
            if not data:
                break
            size += len(data)
            digest.update(data)
    return size == expected_size and digest.hexdigest() == oid


def _safe_object_parent(git_dir, oid):
    root = git_dir / "lfs" / "objects"
    for path in (git_dir / "lfs", root, root / oid[:2], root / oid[:2] / oid[2:4]):
        if path.exists() and path.is_symlink():
            raise RestoreError(f"Refusing to write through a symlink in the LFS cache: {path}")
        path.mkdir(parents=True, exist_ok=True)
    root_real = root.resolve()
    parent = (root / oid[:2] / oid[2:4]).resolve()
    if os.path.commonpath((str(root_real), str(parent))) != str(root_real):
        raise RestoreError("LFS object path escapes the local cache directory.")
    return root / oid[:2] / oid[2:4] / oid


def restore_objects(root, archive_ref):
    _, manifest = checked_manifest(root, archive_ref)
    git_dir = Path(git(root, "rev-parse", "--absolute-git-dir").stdout.decode("utf-8").strip()).resolve()
    restored = 0
    skipped = 0
    total_bytes = 0
    seen = set()

    for entry in manifest["objects"]:
        if not isinstance(entry, dict):
            raise RestoreError("Archive object entry is malformed.")
        oid = entry.get("oid")
        full_size = entry.get("size")
        chunks = entry.get("chunks")
        if not isinstance(oid, str) or not OID_PATTERN.fullmatch(oid) or oid in seen:
            raise RestoreError("Archive contains an invalid or duplicate LFS object ID.")
        seen.add(oid)
        if entry.get("sha256") != oid:
            raise RestoreError(f"Archive full-object hash does not match its ID: {oid}")
        if type(full_size) is not int or full_size < 0 or not isinstance(chunks, list):
            raise RestoreError(f"Archive object metadata is malformed: {oid}")

        target = _safe_object_parent(git_dir, oid)
        needs_restore = True
        if target.exists():
            if _valid_existing(target, oid, full_size):
                needs_restore = False
            else:
                raise RestoreError(f"Existing local LFS object is invalid; refusing to overwrite it: {target}")

        full_digest = hashlib.sha256()
        actual_size = 0
        temp_name = None
        output = None
        try:
            if needs_restore:
                file_descriptor, temp_name = tempfile.mkstemp(prefix=f".{oid}.", suffix=".tmp", dir=target.parent)
                output = os.fdopen(file_descriptor, "wb")
            for index, chunk in enumerate(chunks):
                if not isinstance(chunk, dict):
                    raise RestoreError(f"Archive chunk metadata is malformed: {oid}")
                expected_path = f"objects/{oid}/part-{index:03d}"
                path = chunk.get("path")
                chunk_size = chunk.get("size")
                chunk_hash = chunk.get("sha256")
                if path != expected_path:
                    raise RestoreError(f"Archive chunk path is invalid: {oid} part {index}")
                if type(chunk_size) is not int or not 1 <= chunk_size <= manifest["chunk_size"]:
                    raise RestoreError(f"Archive chunk size is invalid: {oid} part {index}")
                if not isinstance(chunk_hash, str) or not OID_PATTERN.fullmatch(chunk_hash):
                    raise RestoreError(f"Archive chunk hash is invalid: {oid} part {index}")
                raw = git(root, "show", f"{archive_ref}:{path}").stdout
                actual_chunk_hash = hashlib.sha256(raw).hexdigest()
                if len(raw) != chunk_size:
                    raise RestoreError(f"chunk size mismatch for {oid} part {index}")
                if actual_chunk_hash != chunk_hash:
                    raise RestoreError(f"chunk SHA-256 mismatch for {oid} part {index}")
                if output is not None:
                    output.write(raw)
                full_digest.update(raw)
                actual_size += len(raw)
            if output is not None:
                output.flush()
                os.fsync(output.fileno())
                output.close()
                output = None

            if actual_size != full_size:
                raise RestoreError(f"full-object size mismatch for {oid}")
            if full_digest.hexdigest() != oid:
                raise RestoreError(f"full-object SHA-256 mismatch for {oid}")
            if not needs_restore:
                skipped += 1
                total_bytes += full_size
                continue
            try:
                os.link(temp_name, target)
                os.unlink(temp_name)
                temp_name = None
            except FileExistsError:
                if not _valid_existing(target, oid, full_size):
                    raise RestoreError(f"Another process created an invalid LFS object: {target}")
                os.unlink(temp_name)
                temp_name = None
                skipped += 1
            except OSError:
                if target.exists():
                    if not _valid_existing(target, oid, full_size):
                        raise RestoreError(f"Another process created an invalid LFS object: {target}")
                    os.unlink(temp_name)
                    temp_name = None
                    skipped += 1
                else:
                    os.replace(temp_name, target)
                    temp_name = None
            if target.is_symlink() or not _valid_existing(target, oid, full_size):
                raise RestoreError(f"Restored LFS object failed final validation: {target}")
            restored += 1
            total_bytes += full_size
        finally:
            if output is not None:
                output.close()
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)

    return {"restored_objects": restored, "already_present_objects": skipped, "total_bytes": total_bytes}


def maybe_checkout(root, requested):
    if not requested:
        return "not requested"
    status = git(root, "status", "--porcelain=v1", "--untracked-files=normal").stdout
    if status.strip():
        return "skipped because the working tree is dirty"
    result = subprocess.run(["git", "-C", str(root), "lfs", "checkout"], capture_output=True)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RestoreError(f"git lfs checkout failed: {detail or result.returncode}")
    return "completed"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-ref", default=DEFAULT_ARCHIVE_REF, help="archive ref (default: %(default)s)")
    parser.add_argument("--checkout", action="store_true", help="run git lfs checkout only when the working tree is clean")
    args = parser.parse_args(argv)
    try:
        root = find_root(Path.cwd())
        summary = restore_objects(root, args.archive_ref)
        summary["checkout"] = maybe_checkout(root, args.checkout)
        print(json.dumps(summary, sort_keys=True))
        return 0
    except (OSError, RestoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
