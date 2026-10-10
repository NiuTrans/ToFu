"""Bounded streaming extraction of uploaded TeX project archives.

This module is a PURE bounded extractor: callers own root/attachment
authority and read-only enforcement. Extraction always lands in a private
staging sibling and is published with one same-filesystem rename, so a
failed archive never leaves a half-written tree and never overwrites
existing files (the final directory is auto-suffixed).

Limits mirror the paper source snapshot budget (512 files / 64 MiB total /
32 MiB per file) so an extracted tree is always compilable by the build
service. Archive bytes themselves are capped at 128 MiB compressed.
"""
from __future__ import annotations

import os
import shutil
import stat
import tarfile
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lib.paper.builds.errors import BuildError

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_FILES = 512
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_PATH_CHARS = 512
MAX_DEPTH = 32
_CHUNK = 1024 * 1024


@dataclass
class ExtractionReceipt:
    directory: Path
    files: int = 0
    bytes_written: int = 0
    skipped: list = field(default_factory=list)  # (name, reason), bounded

    def as_dict(self, root: Path) -> dict:
        return {'directory': self.directory.name,
                'path': str(self.directory),
                'files': self.files, 'bytes': self.bytes_written,
                'skipped': self.skipped[:16]}


def stream_to_temp(stream, *, max_bytes: int = MAX_ARCHIVE_BYTES) -> Path:
    """Spool one bounded upload body to a private temp file."""
    fd, name = tempfile.mkstemp(prefix='tofu-archive-', suffix='.bin')
    size = 0
    try:
        with os.fdopen(fd, 'wb') as target:
            while chunk := stream.read(_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise BuildError('archive_limit', 'Archive exceeds the byte limit')
                target.write(chunk)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def _member_parts(raw: str) -> tuple[str, ...]:
    if '\x00' in raw:
        raise BuildError('archive_path', 'Archive member has an invalid name')
    parts = []
    for part in raw.replace('\\', '/').split('/'):
        if part in ('', '.', '..'):
            continue
        parts.append(part)
    if not parts or len(parts) > MAX_DEPTH or len(raw) > MAX_PATH_CHARS:
        raise BuildError('archive_path', 'Archive member path is unsafe')
    return tuple(parts)


def _check_entry(name: str, size: int, budget: dict) -> None:
    budget['files'] += 1
    if budget['files'] > MAX_FILES:
        raise BuildError('archive_limit', 'Archive has too many files')
    if size > MAX_FILE_BYTES:
        raise BuildError('archive_limit', 'Archive member exceeds the byte limit')
    budget['bytes'] += size
    if budget['bytes'] > MAX_TOTAL_BYTES:
        raise BuildError('archive_limit', 'Archive exceeds the total byte limit')


def _safe_target(staging: Path, parts: tuple[str, ...]) -> Path:
    candidate = staging
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise BuildError('archive_path', 'Archive member conflicts with a link')
    return candidate


def _copy_stream(source, target: Path, size: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    remaining = size
    try:
        with os.fdopen(fd, 'wb') as out:
            while remaining > 0:
                chunk = source.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _extract_zip(archive: Path, staging: Path, receipt: ExtractionReceipt,
                 budget: dict) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            mode = (info.external_attr >> 16) & 0o170000
            if mode and mode != stat.S_IFREG and mode != stat.S_IFDIR:
                receipt.skipped.append((info.filename[:120], 'special file'))
                continue
            parts = _member_parts(info.filename)
            if not parts:
                continue
            if info.is_dir():
                _safe_target(staging, parts).mkdir(parents=True, exist_ok=True)
                continue
            _check_entry(info.filename, info.file_size, budget)
            with bundle.open(info) as source:
                _copy_stream(source, _safe_target(staging, parts), info.file_size)
            receipt.files += 1
            receipt.bytes_written += info.file_size


def _extract_tar(archive: Path, staging: Path, receipt: ExtractionReceipt,
                 budget: dict) -> None:
    with tarfile.open(archive) as bundle:
        for member in bundle:
            parts = _member_parts(member.name)
            if not parts:
                continue
            if member.isdir():
                _safe_target(staging, parts).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isreg():
                receipt.skipped.append((member.name[:120], 'link or special file'))
                continue
            _check_entry(member.name, member.size, budget)
            source = bundle.extractfile(member)
            if source is None:
                raise BuildError('archive_invalid', 'Archive member cannot be read')
            with source:
                _copy_stream(source, _safe_target(staging, parts), member.size)
            receipt.files += 1
            receipt.bytes_written += member.size


def extract_archive(archive: Path, destination_parent: Path, *,
                    name_hint: str = '') -> ExtractionReceipt:
    """Extract into ``destination_parent/<stem>/``; never merges or overwrites.

    The stem derives from the archive file name (or ``name_hint``) and is
    auto-suffixed on collision. Any failure removes the private staging tree.
    """
    destination_parent = Path(destination_parent)
    if not destination_parent.is_dir():
        raise BuildError('project_root', 'Archive destination is unavailable')
    stem = ''.join(ch if ch.isalnum() or ch in '._- ' else '-'
                   for ch in (name_hint or archive.name))[:80].strip('. ') or 'archive'
    for suffix in ('.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz',
                   '.tar', '.zip', '.bin'):
        if stem.lower().endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    staging = destination_parent / ('.tofu-extract-%s' % uuid.uuid4().hex[:12])
    staging.mkdir(mode=0o700)
    receipt = ExtractionReceipt(directory=staging)
    budget = {'files': 0, 'bytes': 0}
    try:
        if zipfile.is_zipfile(archive):
            _extract_zip(archive, staging, receipt, budget)
        elif tarfile.is_tarfile(archive):
            _extract_tar(archive, staging, receipt, budget)
        else:
            raise BuildError('archive_invalid', 'Only .zip/.tar/.tar.gz archives are supported')
        if not receipt.files:
            raise BuildError('archive_empty', 'Archive contains no regular files')
        final = destination_parent / stem
        counter = 1
        while final.exists():
            final = destination_parent / ('%s (%d)' % (stem, counter))
            counter += 1
            if counter > 64:
                raise BuildError('archive_path', 'Cannot name the extracted directory')
        os.rename(staging, final)  # same filesystem; final is deduped above
        receipt.directory = final
        return receipt
    finally:
        shutil.rmtree(staging, ignore_errors=True)
