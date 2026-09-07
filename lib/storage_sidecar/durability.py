"""Project-local checksum and fsync primitives for maintenance artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Iterable

from lib.storage.errors import StorageError


_HASH_CHUNK_BYTES = 4 * 1024 * 1024


def _check_deadline(deadline_at: float) -> None:
    if time.monotonic() >= deadline_at:
        raise StorageError(
            'database_timeout', 'Storage maintenance deadline expired',
            retryable=True, retry_after_ms=100,
        )


def fsync_file(path: Path) -> None:
    with path.open('rb') as stream:
        os.fsync(stream.fileno())


def fsync_directory(path: Path) -> None:
    if not hasattr(os, 'O_DIRECTORY'):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sha256_file(path: Path, deadline_at: float) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        try:
            while True:
                _check_deadline(deadline_at)
                chunk = stream.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    return digest.hexdigest()
                digest.update(chunk)
        finally:
            adviser = getattr(os, 'posix_fadvise', None)
            dontneed = getattr(os, 'POSIX_FADV_DONTNEED', None)
            if adviser is not None and dontneed is not None:
                try:
                    adviser(stream.fileno(), 0, 0, dontneed)
                except OSError:
                    pass


def _tree_files(root: Path) -> Iterable[Path]:
    return sorted(
        (path for path in root.rglob('*') if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def durable_tree_manifest(root: Path, deadline_at: float) -> dict[str, Any]:
    """Fsync a directory tree and return a deterministic content digest."""
    digest = hashlib.sha256()
    total = 0
    count = 0
    directories = {root}
    for path in _tree_files(root):
        _check_deadline(deadline_at)
        fsync_file(path)
        size = path.stat().st_size
        relative = path.relative_to(root).as_posix()
        file_digest = sha256_file(path, deadline_at)
        digest.update(relative.encode('utf-8'))
        digest.update(b'\0')
        digest.update(str(size).encode('ascii'))
        digest.update(b'\0')
        digest.update(file_digest.encode('ascii'))
        digest.update(b'\n')
        total += size
        count += 1
        directories.update(path.parents)
    for directory in sorted(
            (path for path in directories if path == root or root in path.parents),
            key=lambda path: len(path.parts), reverse=True):
        fsync_directory(directory)
    return {'sha256': digest.hexdigest(), 'bytes': total, 'files': count}


def write_json_durable(path: Path, payload: dict[str, Any]) -> None:
    replacement = path.with_name(path.name + '.new')
    with replacement.open('x', encoding='utf-8') as stream:
        json.dump(payload, stream, separators=(',', ':'), sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(replacement, path)
    fsync_directory(path.parent)


def load_manifest(path: Path) -> dict[str, Any] | None:
    manifest_path = path.with_name(path.name + '.manifest.json')
    if not manifest_path.is_file():
        return None
    try:
        value = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError('backup checksum manifest is unreadable') from exc
    if not isinstance(value, dict):
        raise RuntimeError('backup checksum manifest is invalid')
    return value


__all__ = [
    'durable_tree_manifest', 'fsync_directory', 'fsync_file', 'load_manifest',
    'sha256_file', 'write_json_durable',
]
