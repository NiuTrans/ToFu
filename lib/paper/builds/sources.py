"""Resolve paper entry points and freeze bounded source trees.

Entry points: resolve_main, snapshot_sources. The caller supplies the already
authorized project root. No global project selection, database or HTTP access.
Sources are copied, never hardlinked; symlinks cannot grant outside-root access.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import BuildError, check_cancelled
from .contract import limit, schema

MAX_SOURCE_FILES = limit('source_files')
MAX_SOURCE_BYTES = limit('source_bytes')
MAX_FILE_BYTES = limit('source_file_bytes')
MAX_SCAN_ENTRIES = limit('scan_entries')
_SKIP_DIRS = frozenset({'.git', '.hg', '.svn', '.tofu', '.texlive',
                        'node_modules', '__pycache__', '.venv', 'venv'})
_INPUT_SUFFIXES = frozenset(schema()['x-source-suffixes'])
_DOCUMENT = re.compile(r'\\documentclass\s*(?:\[[^\]]*\])?\s*\{')
_ROOT_HINT = re.compile(r'^\s*%\s*!\s*TEX\s+root\s*=\s*(.+?)\s*$', re.I | re.M)
_INCLUDE = re.compile(r'\\(?:input|include|subfile)\s*\{([^{}]+)\}')


def contained_path(root: Path, path: str | Path) -> Path:
    root = root.resolve(strict=True)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise BuildError('source_path', 'Source must exist inside the project root') from exc
    return resolved


def read_source(root: Path, path: Path, *, limit: int = MAX_FILE_BYTES) -> bytes:
    """Open a regular file beneath root, refusing raced symlink components.

    POSIX uses descriptor-relative traversal with O_NOFOLLOW on every component.
    Other platforms additionally compare the resolved name and opened identity.
    """
    root = root.resolve(strict=True)
    path = contained_path(root, path)
    relative = path.relative_to(root)
    descriptors = []
    try:
        if os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW'):
            current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(current)
            for part in relative.parts[:-1]:
                current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=current)
                descriptors.append(current)
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), dir_fd=current)
        else:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0))
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise BuildError('source_path', 'Paper inputs must be regular files')
        if before.st_size > limit:
            raise BuildError('source_limit', 'Paper input exceeds the file byte limit')
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        current_stat = contained_path(root, path).stat()
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if signature(before) != signature(after) or signature(after) != signature(current_stat):
            raise BuildError('source_changed', 'Paper source changed while being read; retry')
        if remaining == 0:
            raise BuildError('source_limit', 'Paper input exceeds the file byte limit')
        return b''.join(chunks)
    except OSError as exc:
        raise BuildError('source_path', 'Cannot read paper source safely') from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _text(root: Path, path: Path) -> str:
    return read_source(root, path, limit=4 * 1024 * 1024).decode('utf-8', errors='replace')


def _without_comments(text: str) -> str:
    return re.sub(r'(?<!\\)%[^\n]*', '', text)


def _includes(root: Path, main: Path, edited: Path) -> bool:
    pending = [main]
    seen = set()
    while pending:
        path = pending.pop()
        if path == edited:
            return True
        if path in seen:
            continue
        seen.add(path)
        if len(seen) > MAX_SOURCE_FILES:
            raise BuildError('source_limit', 'Paper include graph exceeds the file limit')
        for name in _INCLUDE.findall(_without_comments(_text(root, path))):
            if '\\' in name or '#' in name:
                continue  # macro expansion belongs to TeX, not a guessed path
            rel = name if Path(name).suffix else name + '.tex'
            for directory in (main.parent, path.parent):
                candidate = directory / rel
                if candidate.exists():
                    pending.append(contained_path(root, candidate))
                    break
    return False


def resolve_main(root: Path, edited: str | Path, *, main_file: str = '') -> Path:
    """Explicit root > TeX root directive > unique containing ancestor document.

    All traversal stops at the authorized root. Ambiguity is an actionable
    error, never resolved using file modification time or a name preference.
    """
    root = root.resolve(strict=True)
    edited_path = contained_path(root, edited)
    if main_file:
        main = contained_path(root, main_file)
        if not _DOCUMENT.search(_without_comments(_text(root, main))):
            raise BuildError('main_file', 'Configured main file has no documentclass')
        return main
    if edited_path.suffix.lower() in {'.tex', '.ltx'}:
        text = _text(root, edited_path)
        hint = _ROOT_HINT.search(text)
        if hint:
            return resolve_main(root, edited_path, main_file=str(edited_path.parent / hint[1]))
        if _DOCUMENT.search(_without_comments(text)):
            return edited_path
    directory = edited_path if edited_path.is_dir() else edited_path.parent
    scanned = 0
    while True:
        candidates = []
        for path in directory.iterdir():
            scanned += 1
            if scanned > MAX_SCAN_ENTRIES:
                raise BuildError('source_limit', 'Main-file discovery exceeds its scan limit')
            if path.suffix.lower() not in {'.tex', '.ltx'} or not path.is_file():
                continue
            path = contained_path(root, path)
            if _DOCUMENT.search(_without_comments(_text(root, path))):
                candidates.append(path)
        connected = [p for p in candidates if _includes(root, p, edited_path)]
        choices = connected or candidates
        if len(choices) == 1:
            return choices[0]
        if len(choices) > 1:
            raise BuildError('main_ambiguous', 'Multiple main documents; configure the main file')
        if directory == root:
            break
        directory = directory.parent
    raise BuildError('main_file', 'No main document found; configure the main file')


@dataclass(frozen=True)
class SourceSnapshot:
    root: Path
    main_file: str
    digest: str
    files: tuple[dict, ...]
    size_bytes: int


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _inventory(root: Path, main: Path, *, cancel, max_files: int) -> dict[str, tuple[int, ...]]:
    """Incrementally enumerate inputs and reject a tree changing during the scan.

    Directory identities fence additions behind the scanner, including in an
    already visited directory. The manifest itself ignores generated outputs.
    scandir is consumed within the entry budget, before allocating a file list.
    """
    pending = [root]
    directories = {}
    files = {}
    scanned = 0
    try:
        while pending:
            check_cancelled(cancel)
            directory = pending.pop()
            before = directory.lstat()
            if not stat.S_ISDIR(before.st_mode):
                raise BuildError('source_path', 'Use a regular directory for paper dependencies')
            directories[directory] = _signature(before)
            with os.scandir(directory) as children:
                for child in children:
                    check_cancelled(cancel)
                    scanned += 1
                    if scanned > MAX_SCAN_ENTRIES:
                        raise BuildError('source_limit', 'Paper tree exceeds scan limit; select a paper directory')
                    if child.name.startswith('.') or child.name in _SKIP_DIRS:
                        continue
                    path = Path(child.path)
                    info = child.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path)
                    elif stat.S_ISLNK(info.st_mode):
                        # Report an outside-root grant using the common authority
                        # check, but never snapshot an in-root symlink either.
                        contained_path(root, path)
                        raise BuildError('source_path', 'Use regular files and directories for paper dependencies')
                    elif path.suffix.lower() in _INPUT_SUFFIXES and path != main.with_suffix('.pdf'):
                        if not stat.S_ISREG(info.st_mode):
                            raise BuildError('source_path', 'Paper inputs must be regular files')
                        if len(files) >= max_files:
                            raise BuildError('source_limit', 'Paper tree exceeds the source file limit')
                        files[path.relative_to(root).as_posix()] = _signature(info)
        for directory, signature in directories.items():
            check_cancelled(cancel)
            if _signature(directory.lstat()) != signature:
                raise BuildError('source_changed', 'Paper source changed during snapshot; retry')
    except FileNotFoundError as exc:
        raise BuildError('source_changed', 'Paper source changed during snapshot; retry') from exc
    except OSError as exc:
        raise BuildError('source_path', 'Cannot scan paper source safely') from exc
    return files


def snapshot_sources(root: Path, main: Path, destination: Path, *,
                     cancel=None, max_bytes: int = MAX_SOURCE_BYTES,
                     max_files: int = MAX_SOURCE_FILES) -> SourceSnapshot:
    """Freeze a bounded project source manifest, including binary dependencies.

    The explicit project root is the snapshot boundary; oversized generic
    workspaces must select a paper directory rather than silently omit inputs.
    Aux/log outputs and hidden tool trees never enter the source digest.
    """
    root = root.resolve(strict=True)
    main = contained_path(root, main)
    if destination.resolve().is_relative_to(root):
        raise BuildError('source_path', 'Snapshot output must be outside the project source tree')
    if not 0 < max_bytes <= MAX_SOURCE_BYTES or not 0 < max_files <= MAX_SOURCE_FILES:
        raise BuildError('source_limit', 'Snapshot limits must fit the source resource budget')
    destination.mkdir(parents=True, exist_ok=False)
    for attempt in range(limit('source_snapshot_attempts')):
        try:
            return _copy_snapshot(root, main, destination, cancel=cancel,
                                  max_bytes=max_bytes, max_files=max_files)
        except BuildError as exc:
            if exc.code != 'source_changed' or attempt + 1 == limit('source_snapshot_attempts'):
                raise
            # The destination was exclusively created by this invocation, and
            # failed attempts have not made their files read-only yet.
            shutil.rmtree(destination)
            destination.mkdir()
    raise AssertionError('Snapshot attempt budget must be positive')


def _snapshot_read(root: Path, path: Path, **kwargs) -> bytes:
    try:
        return read_source(root, path, **kwargs)
    except BuildError as exc:
        if exc.code == 'source_path':
            try:
                path.lstat()
            except FileNotFoundError:
                raise BuildError('source_changed', 'Paper source changed during snapshot; retry') from exc
        raise


def manifest_digest(rows: list[dict] | tuple[dict, ...]) -> str:
    """Canonical content identity shared by filesystem and explicit records."""
    ordered = sorted(rows, key=lambda row: row['path'])
    return hashlib.sha256(json.dumps(ordered, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _copy_snapshot(root: Path, main: Path, destination: Path, *, cancel,
                   max_bytes: int, max_files: int) -> SourceSnapshot:
    rows = []
    checks = []
    size = 0
    inventory = _inventory(root, main, cancel=cancel, max_files=max_files)
    if main.relative_to(root).as_posix() not in inventory:
        raise BuildError('main_file', 'Main document must be a regular supported paper input')
    for relative in sorted(inventory):
        check_cancelled(cancel)
        path = root / relative
        data = _snapshot_read(root, path, limit=min(MAX_FILE_BYTES, max_bytes - size))
        size += len(data)
        copied = destination / relative
        copied.parent.mkdir(parents=True, exist_ok=True)
        copied.write_bytes(data)
        digest = hashlib.sha256(data)
        rows.append({'path': relative, 'sha256': digest.hexdigest(), 'size_bytes': len(data)})
        checks.append((path, digest.digest()))
    for path, digest in checks:
        check_cancelled(cancel)
        if hashlib.sha256(_snapshot_read(root, path)).digest() != digest:
            raise BuildError('source_changed', 'Paper source changed during snapshot; retry')
    if _inventory(root, main, cancel=cancel, max_files=max_files) != inventory:
        raise BuildError('source_changed', 'Paper source changed during snapshot; retry')
    for row in rows:
        (destination / row['path']).chmod(0o444)
    rows.sort(key=lambda row: row['path'])
    return SourceSnapshot(destination, main.relative_to(root).as_posix(), manifest_digest(rows),
                          tuple(rows), size)
