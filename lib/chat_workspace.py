"""Landing zone for archives dropped straight onto the chat composer.

A dropped ``.zip`` / ``.tar(.gz)`` is stored VERBATIM — never extracted — in a
fresh directory under the managed workspace root, and the client mounts that
directory as a project root. The model decides whether and how to unpack it
with its own workspace tools (``run_command``), so an upload costs one
sequential write regardless of member count, and no server-side extraction
budget can refuse, truncate or flatten the archive the user actually sent.
Unlike the project-browser archive drop — which extracts into an
already-attached root — a chat drop commonly lands with NO project attached,
so this module owns the managed root the archive lives under until the
conversation mounts it.

The workspace is mutable USER STATE (an uploaded file referenced by the
conversation's project mount), so it co-locates with the DB via
``runtime_paths.uploads_root()`` exactly like ``uploads/papers`` and
``uploads/videos``. It is intentionally NOT per-user: ``uploads/`` is already
the single-owner state tree and the project mount / ``_roots`` registry is the
single-user primary-root seam today (see the TODO(enterprise) notes in
``routes/api_v1/project.py``); the per-user keying seam is added there, not
duplicated here.

Entry points: ``chat_workspaces_root()`` and ``store_archive()``.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import uuid

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['ArchiveRejected', 'StoredArchive', 'chat_workspaces_root', 'store_archive']

# Longest first so ``.tar.gz`` wins over ``.gz``.
ARCHIVE_SUFFIXES = (
    '.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2', '.txz',
    '.tar', '.zip', '.gz', '.bz2', '.xz',
)
_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_ARCHIVE_NAME_CHARS = 160
_MAX_DIRECTORY_STEM_CHARS = 80
_MAX_DIRECTORY_SUFFIX_ATTEMPTS = 1000


class ArchiveRejected(ValueError):
    """The upload is not an archive this landing zone accepts."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class StoredArchive:
    directory: Path
    archive: Path
    bytes_written: int


def chat_workspaces_root() -> str:
    """Absolute path to the managed chat-drop workspace root (created on demand).

    Each archive lands in ``<root>/<archive-stem>/`` (fresh, auto-suffixed —
    never merged or overwritten). Kept separate from the active project tree so
    a chat drop never writes into a root the user attached for editing.
    """
    try:
        from lib.runtime.runtime_paths import uploads_root
        path = os.path.join(uploads_root(), 'workspaces')
    except Exception as exc:  # pragma: no cover — defensive fallback
        logger.debug('[ChatWorkspace] uploads_root unavailable, in-tree: %s', exc)
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base, 'uploads', 'workspaces')
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        logger.warning('[ChatWorkspace] could not create workspace root %s: %s',
                       path, exc)
    return path


def _archive_name(filename: str) -> tuple[str, str]:
    """Return ``(file name, directory stem)`` for a client-supplied name.

    Client input is never a path: only the printable basename survives, and it
    must carry a supported archive suffix.
    """
    name = os.path.basename(str(filename or '').replace('\\', '/'))
    name = ''.join(ch for ch in name if ch.isprintable()).strip(' .')
    lowered = name.lower()
    suffix = next((item for item in ARCHIVE_SUFFIXES if lowered.endswith(item)), '')
    stem = name[:len(name) - len(suffix)].strip(' .') if suffix else ''
    if not stem:
        raise ArchiveRejected(
            'archive-invalid',
            'Only .zip, .tar, .tar.gz, .tgz, .tar.bz2 and .tar.xz archives are accepted')
    stem = stem[:_MAX_ARCHIVE_NAME_CHARS - len(suffix)]
    directory_stem = ''.join(
        ch if ch.isalnum() or ch in '._- ' else '-' for ch in stem
    )[:_MAX_DIRECTORY_STEM_CHARS].strip('. ') or 'archive'
    return stem + name[len(name) - len(suffix):], directory_stem


def _fresh_directory(root: Path, stem: str) -> Path:
    for attempt in range(_MAX_DIRECTORY_SUFFIX_ATTEMPTS):
        candidate = root / (stem if attempt == 0 else f'{stem} ({attempt})')
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(f'No free workspace directory name for {stem!r}')


def store_archive(stream, filename: str) -> StoredArchive:
    """Store one uploaded archive verbatim in a fresh workspace directory.

    The bytes go to a private part-file that is renamed to the archive name
    only after a complete, fsynced copy. Any failure — including an empty
    upload — removes the fresh directory, so a refused or interrupted upload
    never leaves a mountable half-archive behind.
    """
    name, directory_stem = _archive_name(filename)
    directory = _fresh_directory(Path(chat_workspaces_root()), directory_stem)
    try:
        partial = directory / f'.{uuid.uuid4().hex}.part'
        written = 0
        with open(partial, 'xb') as target:
            while chunk := stream.read(_COPY_CHUNK_BYTES):
                target.write(chunk)
                written += len(chunk)
            if not written:
                raise ArchiveRejected('archive-empty', 'The uploaded archive is empty')
            target.flush()
            os.fsync(target.fileno())
        archive = directory / name
        os.rename(partial, archive)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return StoredArchive(directory=directory, archive=archive, bytes_written=written)
