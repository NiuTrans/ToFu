"""One-file personalization for the Windows controlled-end installer.

The published NSIS installer reserves a small fixed-size trailer at the end
of the executable.  A download replaces that empty trailer with the server
routes and (when bridge isolation is enabled) the caller-scoped credential.
NSIS reads the trailer from ``$EXEPATH`` and writes the attachment JSON into
the install directory before first launch.

This deliberately is *not* an archive/container exposed to the user: the
download is one directly runnable ``.exe``.  Keeping the slot fixed-size also
lets the response stream the existing installer instead of copying 50+ MB
into memory or invoking ``makensis`` for every click.
"""

from __future__ import annotations

import json
import os

from lib.log import get_logger


logger = get_logger(__name__)
MAGIC = b'TOFU_AGENT_ATTACH_V2:'
TRAILER_SIZE = 1000
FORMAT = 'nsis-overlay-v1'


def encode_attachment(payload: dict | None = None) -> bytes:
    """Return the exact fixed-size ASCII trailer for *payload*.

    NSIS' standard build has a 1024-character string ceiling.  The 1000-byte
    single-line record stays below it and JSON's trailing whitespace is valid,
    so the installer can copy the record without needing a JSON parser or a
    second file beside it.
    """
    raw = json.dumps(payload or {}, ensure_ascii=True,
                     separators=(',', ':')).encode('ascii')
    used = len(MAGIC) + len(raw) + 1  # final newline
    if used > TRAILER_SIZE:
        raise ValueError(
            'agent installer attachment is too large (%d > %d bytes)'
            % (used, TRAILER_SIZE))
    return MAGIC + raw + (b' ' * (TRAILER_SIZE - used)) + b'\n'


EMPTY_TRAILER = encode_attachment()


def has_attachment_slot(path: str) -> bool:
    """True only for an installer built with the embedded-attach contract."""
    try:
        if os.path.getsize(path) < TRAILER_SIZE:
            return False
        with open(path, 'rb') as f:
            f.seek(-TRAILER_SIZE, os.SEEK_END)
            return f.read(len(MAGIC)) == MAGIC
    except OSError as exc:
        logger.debug('[AgentInstaller] attachment slot probe failed: %s', exc)
        return False


def append_empty_slot(path: str) -> None:
    """Add the build-time empty slot, refusing accidental double-appends."""
    if has_attachment_slot(path):
        return
    with open(path, 'ab') as f:
        f.write(EMPTY_TRAILER)


def iter_personalized(path: str, payload: dict, chunk_size: int = 1024 * 1024):
    """Stream *path* with its empty slot replaced by *payload*.

    Callers must verify :func:`has_attachment_slot` first.  No temporary file
    is created, and at most one normal file chunk is resident in memory.
    """
    remaining = os.path.getsize(path) - TRAILER_SIZE
    if remaining < 0:
        raise ValueError('agent installer is smaller than its trailer')
    with open(path, 'rb') as f:
        while remaining:
            chunk = f.read(min(chunk_size, remaining))
            if not chunk:
                raise OSError('agent installer ended before its trailer')
            remaining -= len(chunk)
            yield chunk
    yield encode_attachment(payload)


__all__ = [
    'MAGIC', 'TRAILER_SIZE', 'FORMAT', 'EMPTY_TRAILER',
    'encode_attachment', 'has_attachment_slot', 'append_empty_slot',
    'iter_personalized',
]
