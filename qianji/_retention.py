"""Bounded retention of derived files; never traverse links or junctions."""
from __future__ import annotations

import os
import re
import shutil
import stat
import time
import warnings
from contextlib import contextmanager
from pathlib import Path


def checked(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    if path == root or not path.is_relative_to(root):
        raise ValueError("Retention target must be strictly inside its workspace")
    if root.resolve() != root:
        raise ValueError("Retention workspace must not be a link")
    for item in [path, *path.parents]:
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Retention refuses links and junctions")
        if item == root:
            break
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Retention target escapes its workspace")
    return path


def inventory(root, path):
    path = checked(root, path)
    entries = [path]
    if path.is_dir():
        for parent, folders, files in os.walk(path, followlinks=False):
            for name in folders + files:
                item = Path(parent) / name
                info = item.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("Retention refuses linked descendants")
                entries.append(item)
    return entries


def prune(root, directory, keep, pattern, *, grace_seconds=3600, apply=True):
    """Call only under the publisher's lock, after its new current is verified."""
    directory = checked(root, directory)
    result = {"removed": [], "bytes_removed": 0, "deferred": []}
    if not directory.exists():
        return result
    cutoff = time.time() - grace_seconds
    keep = set(keep)
    markers = checked(root, directory / ".retired")
    if markers.exists():
        for marker in markers.iterdir():
            checked(root, marker)
            if re.fullmatch(pattern, marker.name) and marker.stat().st_mtime > cutoff:
                keep.add(marker.name)
    for path in sorted(directory.iterdir()):
        if path.name in keep or not re.fullmatch(pattern, path.name):
            continue
        try:
            entries = inventory(root, path)
            if max(p.stat().st_mtime for p in entries) > cutoff:
                continue
            size = sum(p.stat().st_size for p in entries if p.is_file())
            if apply:
                checked(root, path)
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            result["removed"].append({"path": str(path), "bytes": size})
            result["bytes_removed"] += size
        except (OSError, ValueError) as exc:
            result["deferred"].append({"path": str(path), "reason": str(exc)})
    if apply and markers.exists():
        for marker in markers.iterdir():
            checked(root, marker)
            if (re.fullmatch(pattern, marker.name) and marker.is_file()
                    and marker.stat().st_mtime <= cutoff):
                marker.unlink()
    return result


def retire(root, directory, name):
    """Start the reader grace period without modifying immutable data files."""
    marker = checked(root, Path(directory) / ".retired" / name)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def best_effort(operation, *args, **kwargs):
    """Cleanup failure must not turn a committed publication into a failed one."""
    try:
        result = operation(*args, **kwargs)
        if result.get("deferred"):
            warnings.warn("Some retired files are in use or unsafe; cleanup deferred", RuntimeWarning)
        return result
    except Exception as exc:  # noqa: BLE001 - publication has already committed
        warnings.warn("Retention deferred: " + str(exc), RuntimeWarning)
        return {"removed": [], "bytes_removed": 0, "deferred": [str(exc)]}


@contextmanager
def publication_lock(root):
    root = Path(root).absolute()
    path = checked(root, root / ".retention.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
