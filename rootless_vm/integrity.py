"""Small host-tree integrity snapshots for negative isolation tests."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TreeSnapshot:
    root: Path
    entries: tuple[tuple[str, str, int, int, str], ...]

    @property
    def digest(self) -> str:
        hasher = hashlib.sha256()
        for entry in self.entries:
            hasher.update(repr(entry).encode("utf-8", errors="surrogateescape"))
            hasher.update(b"\0")
        return hasher.hexdigest()


def snapshot_tree(root: Path, *, exclude: tuple[Path, ...] = ()) -> TreeSnapshot:
    """Hash a tree without following symlinks.

    This is intended for before/after assertions around hostile guest tests,
    not as a substitute for a filesystem monitor.
    """

    root = root.expanduser().resolve(strict=True)
    excluded = tuple(path.expanduser().resolve() for path in exclude)
    entries: list[tuple[str, str, int, int, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if not any((current / name).resolve() == item or item in (current / name).resolve().parents for item in excluded)
        ]
        for name in sorted((*dirnames, *filenames)):
            path = current / name
            if any(path.resolve() == item or item in path.resolve().parents for item in excluded):
                continue
            info = path.lstat()
            relative = str(path.relative_to(root))
            mode = stat.S_IMODE(info.st_mode)
            if path.is_symlink():
                kind = "symlink"
                content = os.readlink(path)
            elif path.is_file():
                kind = "file"
                hasher = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                content = hasher.hexdigest()
            elif path.is_dir():
                kind = "dir"
                content = ""
            else:
                kind = "other"
                content = ""
            entries.append((relative, kind, mode, info.st_size, content))
    return TreeSnapshot(root=root, entries=tuple(entries))
