"""Descriptor-anchored undo mutations within an explicit project root.

Entry points: remove_file, write_file, read_text. Dependencies: the canonical
snapshot path anchor and project read budget. No journal publication here.
Unsupported platforms fail visibly rather than falling back to racy paths.
"""
from contextlib import contextmanager
from functools import lru_cache
import os
import stat
import uuid

from lib.project_mod.write_intents import recovery_file_bytes
from lib.project_mod.snapshot_content import anchored_parent, file_fingerprint


def _stat_regular(parent, name):
    try:
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError('Undo target changed to a non-regular file')
    return metadata


@contextmanager
def _parent(base, target, *, create=False):
    base = os.path.realpath(os.path.abspath(base))
    target = os.path.abspath(target)
    if target == base or os.path.commonpath([base, target]) != base:
        raise ValueError('Undo target escapes project root')
    relative = os.path.relpath(target, base)
    metadata = os.stat(base, follow_symlinks=False)
    root_identity = metadata.st_dev, metadata.st_ino
    with anchored_parent(base, relative, root_identity, create=create) as (parent, name):
        def validate_parent():
            with anchored_parent(base, relative, root_identity) as (current, _):
                current_stat, original_stat = os.fstat(current), os.fstat(parent)
                if (current_stat.st_dev, current_stat.st_ino) != (original_stat.st_dev, original_stat.st_ino):
                    raise ValueError('Undo parent directory changed')
        yield parent, name, validate_parent


def remove_file(base, target, *, expected_image=None):
    """Remove one tracked regular file; a replaced parent is never followed."""
    try:
        with _parent(base, target) as (parent, name, validate_parent):
            if _stat_regular(parent, name) is None:
                return False
            validate_parent()
            if expected_image is not None and fingerprint_file(base, target) != expected_image:
                raise ValueError('Undo target changed before deletion')
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
            return True
    except FileNotFoundError:
        return False


def write_file(base, target, data, *, expected_content=None, expected_image=None):
    """Publish a fsynced pre-image by rename within the pinned directory."""
    if isinstance(data, str):
        data = data.encode('utf-8')
    with _parent(base, target, create=True) as (parent, name, validate_parent):
        before = _stat_regular(parent, name)
        temporary = '.tofu_undo_' + uuid.uuid4().hex
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
        descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(data)
                handle.flush()
                if before is not None:
                    os.fchmod(handle.fileno(), stat.S_IMODE(before.st_mode))
                os.fsync(handle.fileno())
            validate_parent()
            current = _stat_regular(parent, name)
            if ((None if before is None else file_fingerprint(before)) !=
                    (None if current is None else file_fingerprint(current))):
                raise ValueError('Undo target changed during restore')
            if expected_content is not None:
                flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0)
                check_fd = os.open(name, flags, dir_fd=parent)
                with os.fdopen(check_fd, 'rb') as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        raise ValueError('Undo reverse-patch target changed')
                    if handle.read(len(expected_content) + 1) != expected_content:
                        raise ValueError('Undo reverse-patch content changed before restore')
            if expected_image is not None and fingerprint_file(base, target) != expected_image:
                raise ValueError('Undo target changed before restore')
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def read_text(base, target):
    """Read a complete regular file for reverse-patch matching, never a FIFO."""
    with _parent(base, target) as (parent, name, validate_parent):
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0)
        descriptor = os.open(name, flags, dir_fd=parent)
        with os.fdopen(descriptor, 'rb') as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('Undo reverse-patch source is not a regular file')
            data = handle.read()
            if len(data) != before.st_size or file_fingerprint(os.fstat(handle.fileno())) != file_fingerprint(before):
                raise ValueError('Undo reverse-patch file changed while reading')
        validate_parent()
        return data.decode('utf-8')


def fingerprint_file(base, target, *, byte_budget=None, file_byte_limit=None):
    """Capture bounded post-image content evidence, never a stat-only promise.

    byte_budget is a caller-owned one-element remaining-byte counter. Command
    batches retain their shared budget; direct writes have no recovery quota.
    Command capture explicitly supplies its separate per-file display cap.
    A missing file is explicit evidence; errors/oversized files are unavailable.
    """
    import hashlib
    file_limit = recovery_file_bytes() if file_byte_limit is None else file_byte_limit
    try:
        root = os.stat(os.path.realpath(base), follow_symlinks=False)
        root_identity = [root.st_dev, root.st_ino]
        with _parent(base, target) as (parent, name, validate_parent):
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0)
            descriptor = os.open(name, flags, dir_fd=parent)
            with os.fdopen(descriptor, 'rb') as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > file_limit:
                    return {'kind': 'unavailable'}
                allowance = before.st_size + 1
                if byte_budget is not None:
                    if allowance > byte_budget[0]:
                        return {'kind': 'unavailable'}
                    byte_budget[0] -= allowance
                digest = hashlib.blake2b(digest_size=32)
                consumed = 0
                while consumed < allowance:
                    chunk = handle.read(min(65536, allowance - consumed))
                    if not chunk:
                        break
                    digest.update(chunk)
                    consumed += len(chunk)
                if consumed != before.st_size or file_fingerprint(os.fstat(handle.fileno())) != file_fingerprint(before):
                    return {'kind': 'unavailable'}
            validate_parent()
            current = _stat_regular(parent, name)
            if current is None or file_fingerprint(current) != file_fingerprint(before):
                return {'kind': 'unavailable'}
            return {'kind': 'content', 'bytes': consumed, 'digest': digest.hexdigest(), 'root': root_identity}
    except FileNotFoundError:
        return {'kind': 'absent', 'root': root_identity} if 'root_identity' in locals() else {'kind': 'unavailable'}
    except (OSError, ValueError, NotImplementedError):
        return {'kind': 'unavailable'}


@lru_cache(maxsize=1)
def _postimage_validator():
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    schema = Path(__file__).resolve().parents[2] / 'contracts/project_undo_postimage_v1.schema.json'
    return Draft202012Validator(json.loads(schema.read_text()))


def valid_postimage(value):
    """Validate persisted evidence before it can authorize a mutation."""
    return next(_postimage_validator().iter_errors(value), None) is None


def stat_file(base, target):
    """Read regular-file restore metadata without reopening a path-based target."""
    with _parent(base, target) as (parent, name, validate_parent):
        metadata = _stat_regular(parent, name)
        validate_parent()
        return metadata


def touch_file(base, target):
    """Nudge editor polling through the same no-follow root authority."""
    with _parent(base, target) as (parent, name, validate_parent):
        metadata = _stat_regular(parent, name)
        if metadata is None:
            return
        validate_parent()
        os.utime(name, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1000),
                 dir_fd=parent, follow_symlinks=False)
