"""Bounded, descriptor-anchored pre-image capture for command undo.

Entry: read_snapshot_content. Dependency: metadata captured by run_command's
snapshot owner. No authorization expansion, persistence, or automatic retry.
A missing safe platform primitive or changed path produces no backup bytes.
"""
from contextlib import contextmanager
import os
import stat

UNDO_BATCH_CONTENT_BYTES = 20 * 1024 * 1024

_ANCHORED_OPEN_SUPPORTED = hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd
_ANCHORED_SCAN_SUPPORTED = _ANCHORED_OPEN_SUPPORTED and os.scandir in os.supports_fd


def file_fingerprint(metadata):
    """Identity plus content-change metadata; size/mtime alone are insufficient."""
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


@contextmanager
def anchored_parent(base, relative, root_identity, *, create=False):
    """Pin each directory beneath a known root without following symlinks."""
    if not _ANCHORED_OPEN_SUPPORTED:
        raise NotImplementedError('Descriptor-anchored content capture unavailable')
    parts = relative.split(os.sep)
    if os.path.isabs(relative) or not parts or any(p in ('', '.', '..') for p in parts):
        raise ValueError('Snapshot path must remain beneath its root')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
    descriptor = os.open(base, flags)
    try:
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != root_identity:
            raise ValueError('Snapshot root changed')
        for component in parts[:-1]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def read_snapshot_content(base, relative, snapshot, limit):
    """Return a complete unchanged regular-file pre-image, or None.

    Read at most limit + 1 bytes. Nonblocking no-follow open prevents a swapped
    FIFO from hanging a worker. Re-open the parent chain after reading so a
    directory rename cannot relabel captured bytes as another current path.
    Platforms without anchored open decline capture rather than follow paths.
    """
    expected = getattr(snapshot, 'fingerprints', {}).get(relative)
    root_identity = getattr(snapshot, 'root_identity', None)
    if expected is None or root_identity is None or limit < 0 or expected[2] > limit:
        return None
    try:
        with anchored_parent(base, relative, root_identity) as (parent, name):
            flags = (os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0)
                     | getattr(os, 'O_CLOEXEC', 0))
            descriptor = os.open(name, flags, dir_fd=parent)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or file_fingerprint(before) != expected:
                    return None
                chunks = bytearray()
                while len(chunks) <= limit:
                    chunk = os.read(descriptor, min(65536, limit + 1 - len(chunks)))
                    if not chunk:
                        break
                    chunks.extend(chunk)
                if len(chunks) != expected[2] or file_fingerprint(os.fstat(descriptor)) != expected:
                    return None
            finally:
                os.close(descriptor)
        with anchored_parent(base, relative, root_identity) as (parent, name):
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode) or file_fingerprint(current) != expected:
                return None
        return bytes(chunks)
    except (OSError, ValueError, NotImplementedError):
        return None


@contextmanager
def anchored_directory(base, relative, root_identity):
    """Open the directory being scanned without following a substituted path."""
    if not _ANCHORED_SCAN_SUPPORTED:
        raise NotImplementedError('Descriptor-anchored directory scans unavailable')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
    if relative == '.':
        descriptor = os.open(base, flags)
    else:
        with anchored_parent(base, relative, root_identity) as (parent, name):
            descriptor = os.open(name, flags, dir_fd=parent)
    try:
        if relative == '.':
            metadata = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) != root_identity:
                raise ValueError('Snapshot root changed')
        yield descriptor
    finally:
        os.close(descriptor)
