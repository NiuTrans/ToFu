"""Descriptor-pinned reads shared by publication and explicit file provenance.

Entry: read_project_snapshot. Callers supply authorized roots and cancellation;
this module grants no access. It rechecks file metadata and every path edge,
optionally streams a digest/quote witnesses without retaining source bodies.
"""
from dataclasses import dataclass
import codecs
import hashlib
import os
from pathlib import Path
import stat


@dataclass(frozen=True)
class FileSnapshot:
    root: str
    path: str
    relative_path: str
    size: int
    sha256: str
    found_quotes: frozenset[str]
    content: bytes | None
    root_identity: tuple[int, int]


def read_project_snapshot(path, roots, *, check=None, include_bytes=False,
                          fingerprint=True, quotes=(), expected_root_identity=None, utf8_text=False,
                          allow_empty=False):
    """Read one stable nonempty regular file; memory stays bounded unless requested."""
    if check is not None:
        check()
    if not isinstance(path, str) or not path or '\x00' in path or '..' in Path(path).parts:
        raise ValueError('Invalid project path')
    roots = list(roots or [])
    if not roots or any(not isinstance(root, str) or not os.path.isabs(root)
                        or '\x00' in root or '..' in Path(root).parts for root in roots):
        raise PermissionError('Verified absolute roots required')
    candidate = os.path.abspath(path if os.path.isabs(path) else os.path.join(roots[0], path))
    matching = [os.path.abspath(root) for root in roots
                if os.path.commonpath([candidate, os.path.abspath(root)]) == os.path.abspath(root)
                and candidate != os.path.abspath(root)]
    if not matching:
        raise PermissionError('File outside verified project roots')
    root = max(matching, key=len)
    needles = {quote: quote.encode('utf-8') for quote in quotes if quote}
    overlap = max((len(needle) - 1 for needle in needles.values()), default=0)
    found, tail = set(), b''
    digest = hashlib.sha256() if fingerprint else None
    decoder = codecs.getincrementaldecoder('utf-8')() if utf8_text else None
    chunks = [] if include_bytes else None
    parts = Path(candidate).parts[1:]
    fds = [os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    edges = []
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fds[-1])
            edges.append((fds[-1], part, child)); fds.append(child)
            if len(fds) == len(Path(root).parts) and expected_root_identity is not None:
                root_stat = os.fstat(child)
                if (root_stat.st_dev, root_stat.st_ino) != tuple(expected_root_identity):
                    raise PermissionError('Verified project root changed')
        before = os.fstat(fds[-1])
        if not stat.S_ISREG(before.st_mode) or (before.st_size == 0 and not allow_empty):
            raise ValueError('Only nonempty regular files supported')
        size = 0
        while size <= before.st_size:
            if check is not None:
                check()
            chunk = os.read(fds[-1], min(65536, before.st_size + 1 - size))
            if not chunk:
                break
            if decoder is not None:
                decoder.decode(chunk)
                if b'\x00' in chunk:
                    raise ValueError('File sources require UTF-8 text without NUL bytes')
            if chunks is not None:
                chunks.append(chunk)
            if digest is not None:
                digest.update(chunk)
            if needles:
                window = tail + chunk
                found.update(quote for quote, needle in needles.items() if needle in window)
                tail = window[-overlap:] if overlap else b''
            size += len(chunk)
        if decoder is not None:
            decoder.decode(b'', final=True)
        after = os.fstat(fds[-1])
        fields = ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if size != before.st_size or any(getattr(before, key) != getattr(after, key) for key in fields):
            raise ValueError('File changed while publishing')
        for parent, part, child in edges:
            current = os.stat(part, dir_fd=parent, follow_symlinks=False)
            pinned = os.fstat(child)
            if (current.st_dev, current.st_ino, current.st_mode) != (pinned.st_dev, pinned.st_ino, pinned.st_mode):
                raise ValueError('File path changed while publishing')
        if check is not None:
            check()
        root_stat = os.fstat(fds[len(Path(root).parts) - 1])
        return FileSnapshot(root, candidate, os.path.relpath(candidate, root), size,
                            digest.hexdigest() if digest else '', frozenset(found),
                            b''.join(chunks) if chunks is not None else None,
                            (root_stat.st_dev, root_stat.st_ino))
    finally:
        for descriptor in reversed(fds):
            os.close(descriptor)
