"""Validate and atomically install user-owned skill packages.

Packages may arrive as zip bytes, a zip path, or a local directory. The same
resource and filesystem policy applies to every source. Catalog installs add
an immutable revision and a canonical selected-package digest; arbitrary
uploads remain user-authorized but never execute bundled setup scripts.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import os
from pathlib import PurePosixPath
import re
import shutil
import stat
import struct
import tempfile
import threading
from typing import Any, BinaryIO, Iterator
import uuid
import zipfile

from lib.log import audit_log, get_logger
from lib.memory.storage import (
    _make_memory_id,
    _memory_from_file,
    _parse_frontmatter,
)
from lib.skills.paths import resolve_skill_install_dir

logger = get_logger(__name__)

__all__ = [
    'InstallerError',
    'canonical_skill_content_sha256',
    'install_skill_package',
]


_MAX_BYTES = 25 * 1024 * 1024
_MAX_FILES = 2_000
_MAX_DIRECTORIES = 2_000
_MAX_PACKAGE_SCAN_ENTRIES = 6_000
_MAX_ARCHIVE_ENTRIES = 20_000
_MAX_CENTRAL_DIRECTORY_BYTES = 16 * 1024 * 1024
_MAX_PATH_CHARS = 512
_MAX_PATH_DEPTH = 32
_MAX_PATH_COMPONENT_CHARS = 240
_MAX_SKILL_MD_BYTES = 2 * 1024 * 1024
_COPY_CHUNK_BYTES = 64 * 1024
_DENYLIST_NAMES = frozenset({'.DS_Store', 'Thumbs.db', '.git', '.svn'})
_RESERVED_METADATA_NAMES = frozenset({'.catalog_id', '.skill-origin.json'})
_DIGEST_IGNORED_NAMES = _DENYLIST_NAMES | _RESERVED_METADATA_NAMES
_DIGEST_DOMAIN = b'tofu-skill-content-v1\0'
_CATALOG_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,127}$')
_SOURCE_REVISION_RE = re.compile(r'^[0-9A-Za-z][0-9A-Za-z._+-]{0,127}$')
_SOURCE_REGISTRY_RE = re.compile(r'^[a-z0-9][a-z0-9.-]{0,63}$')
_INSTALL_LOCK = threading.RLock()


class InstallerError(Exception):
    """The package failed a bounded validation or atomic-install contract."""


def _normalized_parts(raw_path: str) -> tuple[str, ...]:
    """Return a safe, portable archive-relative path or fail closed."""
    if not isinstance(raw_path, str) or not raw_path or '\x00' in raw_path:
        raise InstallerError('Archive contains an empty or invalid path')
    value = raw_path.replace('\\', '/')
    path = PurePosixPath(value)
    parts = tuple(part for part in path.parts if part not in ('', '.'))
    if (path.is_absolute() or not parts or '..' in parts
            or any(part.endswith(':') for part in parts[:1])):
        raise InstallerError(f'Unsafe archive path: {raw_path}')
    relative = '/'.join(parts)
    if len(relative) > _MAX_PATH_CHARS or len(parts) > _MAX_PATH_DEPTH:
        raise InstallerError(f'Archive path exceeds package limits: {raw_path}')
    if (any(len(part) > _MAX_PATH_COMPONENT_CHARS for part in parts)
            or any(ord(char) < 32 for part in parts for char in part)):
        raise InstallerError(
            f'Archive path has an invalid component: {raw_path}')
    return parts


def _is_denied(parts: tuple[str, ...]) -> bool:
    return any(part in _DENYLIST_NAMES for part in parts)


def _zip_entry_kind(info: zipfile.ZipInfo) -> str:
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if info.is_dir() or file_type == stat.S_IFDIR:
        return 'directory'
    # Some zip writers store permission bits without a POSIX file type. Treat
    # that zero type as a regular file; reject every explicit non-regular type.
    if file_type not in (0, stat.S_IFREG):
        raise InstallerError(
            f'Archive special file or symlink rejected: {info.filename}')
    if info.flag_bits & 0x1:
        raise InstallerError(
            f'Encrypted archive entry rejected: {info.filename}')
    return 'file'


def _guard_zip_directory(source: bytes | str) -> None:
    """Reject oversized/Zip64 central directories before ZipFile parses them."""
    if isinstance(source, (bytes, bytearray)):
        stream = io.BytesIO(source)
        close = stream.close
    else:
        stream = open(source, 'rb')
        close = stream.close
    try:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        tail_size = min(size, 65_557)
        stream.seek(size - tail_size)
        tail = stream.read(tail_size)
        position = tail.rfind(b'PK\x05\x06')
        while position >= 0:
            if len(tail) - position >= 22:
                comment_bytes = struct.unpack_from(
                    '<H', tail, position + 20)[0]
                if position + 22 + comment_bytes == len(tail):
                    break
            position = tail.rfind(b'PK\x05\x06', 0, position)
        if position < 0:
            raise InstallerError('Zip archive has no valid end directory record')
        disk_number, directory_disk, disk_entries, total_entries = (
            struct.unpack_from('<4H', tail, position + 4))
        directory_bytes = struct.unpack_from('<L', tail, position + 12)[0]
        directory_offset = struct.unpack_from('<L', tail, position + 16)[0]
        if (disk_number != 0 or directory_disk != 0
                or disk_entries != total_entries):
            raise InstallerError('Multi-disk zip archives are not supported')
        if (total_entries == 0xFFFF or directory_bytes == 0xFFFFFFFF
                or directory_offset == 0xFFFFFFFF):
            raise InstallerError('Zip64 skill archives are not supported')
        if total_entries > _MAX_ARCHIVE_ENTRIES:
            raise InstallerError(
                f'Archive exceeds {_MAX_ARCHIVE_ENTRIES} directory entries')
        if directory_bytes > _MAX_CENTRAL_DIRECTORY_BYTES:
            raise InstallerError('Archive central directory exceeds 16 MiB')
        if directory_offset + directory_bytes > size:
            raise InstallerError('Zip central directory points outside the archive')
    finally:
        close()


def _selected_zip_prefix(
    entries: list[tuple[zipfile.ZipInfo, tuple[str, ...], str]],
    subdir: str,
) -> tuple[str, ...]:
    wanted = _normalized_parts(subdir.strip('/'))
    candidates: set[tuple[str, ...]] = set()
    for _info, parts, kind in entries:
        if kind != 'file' or not parts or parts[-1] != 'SKILL.md':
            continue
        for index in range(0, len(parts) - len(wanted)):
            if parts[index:index + len(wanted)] == wanted:
                prefix = parts[:index + len(wanted)]
                if parts == prefix + ('SKILL.md',):
                    candidates.add(prefix)
    if not candidates:
        raise InstallerError(
            f'Sub-skill {subdir!r} was not found at an exact SKILL.md root')
    if len(candidates) != 1:
        rendered = ', '.join('/'.join(value) for value in sorted(candidates))
        raise InstallerError(
            f'Sub-skill {subdir!r} is ambiguous in the archive: {rendered}')
    return next(iter(candidates))


def _bounded_copy(source: BinaryIO, destination: BinaryIO, expected: int) -> int:
    copied = 0
    while True:
        chunk = source.read(_COPY_CHUNK_BYTES)
        if not chunk:
            break
        copied += len(chunk)
        if copied > expected or copied > _MAX_BYTES:
            raise InstallerError('Archive entry expanded beyond its declared size')
        destination.write(chunk)
    if copied != expected:
        raise InstallerError('Archive entry size did not match its declaration')
    return copied


def _safe_extract_zip(
    archive: zipfile.ZipFile,
    destination: str,
    *,
    subdir: str | None = None,
    ignored_paths: frozenset[str] = frozenset(),
) -> int:
    """Extract a validated archive, optionally stripping one exact sub-skill."""
    entries: list[tuple[zipfile.ZipInfo, tuple[str, ...], str]] = []
    infos = archive.infolist()
    if len(infos) > _MAX_ARCHIVE_ENTRIES:
        raise InstallerError(
            f'Archive exceeds {_MAX_ARCHIVE_ENTRIES} directory entries')
    for info in infos:
        parts = _normalized_parts(info.filename)
        kind = _zip_entry_kind(info)
        entries.append((info, parts, kind))

    selected_prefix = (
        _selected_zip_prefix(entries, subdir) if subdir else ()
    )
    destination_real = os.path.realpath(destination)
    total_bytes = 0
    file_count = 0
    seen_directories: set[str] = set()
    # ZIP paths are treated as portable package paths.  Case-folding prevents
    # an archive that is safe on Linux from becoming ambiguous when a package
    # is copied to a case-insensitive filesystem later.
    seen_paths: dict[str, tuple[str, str]] = {}

    for info, parts, kind in entries:
        if _is_denied(parts):
            continue
        if selected_prefix:
            if parts[:len(selected_prefix)] != selected_prefix:
                continue
            relative_parts = parts[len(selected_prefix):]
            if not relative_parts:
                continue
        else:
            relative_parts = parts
        if any(part in _RESERVED_METADATA_NAMES for part in relative_parts):
            raise InstallerError(
                'Package contains reserved Tofu origin metadata')
        relative = '/'.join(relative_parts)
        if relative in ignored_paths:
            continue
        target = os.path.realpath(os.path.join(destination, *relative_parts))
        try:
            contained = os.path.commonpath([destination_real, target]) == destination_real
        except ValueError:
            contained = False
        if not contained:
            raise InstallerError(f'Archive path escapes extraction root: {relative}')
        directory_parts = (
            relative_parts if kind == 'directory' else relative_parts[:-1])
        for depth in range(1, len(directory_parts) + 1):
            directory_path = '/'.join(directory_parts[:depth])
            path_key = directory_path.casefold()
            previous = seen_paths.get(path_key)
            if previous and previous != (directory_path, 'directory'):
                raise InstallerError(
                    f'Archive path collision rejected: {relative}')
            seen_paths[path_key] = (directory_path, 'directory')
            seen_directories.add(path_key)
        if len(seen_directories) > _MAX_DIRECTORIES:
            raise InstallerError(
                f'Package exceeds {_MAX_DIRECTORIES} directories')
        if kind == 'directory':
            os.makedirs(target, exist_ok=True)
            continue
        path_key = relative.casefold()
        previous = seen_paths.get(path_key)
        if previous:
            if previous == (relative, 'file'):
                raise InstallerError(
                    f'Duplicate archive entry rejected: {relative}')
            raise InstallerError(
                f'Archive path collision rejected: {relative}')
        seen_paths[path_key] = (relative, 'file')
        file_count += 1
        total_bytes += int(info.file_size)
        if file_count > _MAX_FILES:
            raise InstallerError(f'Package exceeds {_MAX_FILES} files')
        if total_bytes > _MAX_BYTES:
            raise InstallerError(
                f'Package exceeds {_MAX_BYTES // (1024 * 1024)} MiB unpacked')
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with archive.open(info, 'r') as source, open(target, 'xb') as output:
            _bounded_copy(source, output, int(info.file_size))
    return file_count


def _walk_validated_files(root: str) -> Iterator[tuple[str, str, int]]:
    """Yield normalized regular files while enforcing one shared package cap."""
    if os.path.islink(root):
        raise InstallerError('Symlink package roots are rejected')
    root_real = os.path.realpath(root)
    total_bytes = 0
    file_count = 0
    directory_count = 0
    scanned_entries = 0
    pending = [('', root_real)]
    while pending:
        relative_dir, directory = pending.pop()
        entries = []
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    scanned_entries += 1
                    if scanned_entries > _MAX_PACKAGE_SCAN_ENTRIES:
                        raise InstallerError(
                            f'Package scan exceeds '
                            f'{_MAX_PACKAGE_SCAN_ENTRIES} entries')
                    entries.append(entry)
        except OSError as exc:
            logger.warning('[SkillInstaller] cannot scan %s: %s',
                           directory, exc)
            raise InstallerError('Cannot scan package directory') from exc

        child_directories: list[tuple[str, str]] = []
        for entry in sorted(entries, key=lambda item: item.name):
            if entry.name in _DENYLIST_NAMES:
                continue
            relative = (
                f'{relative_dir}/{entry.name}'
                if relative_dir else entry.name)
            try:
                if entry.is_symlink():
                    raise InstallerError(
                        f'Symlink package entry rejected: {relative}')
                if entry.is_dir(follow_symlinks=False):
                    directory_count += 1
                    if directory_count > _MAX_DIRECTORIES:
                        raise InstallerError(
                            f'Package exceeds {_MAX_DIRECTORIES} directories')
                    child_directories.append((relative, entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise InstallerError(
                        f'Special package entry rejected: {relative}')
                file_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                logger.warning('[SkillInstaller] cannot stat %s: %s',
                               entry.path, exc)
                raise InstallerError(
                    f'Cannot stat package file: {entry.name}') from exc
            if not stat.S_ISREG(file_stat.st_mode):
                raise InstallerError(
                    f'Special package entry rejected: {relative}')
            parts = _normalized_parts(relative)
            relative = '/'.join(parts)
            file_count += 1
            total_bytes += int(file_stat.st_size)
            if file_count > _MAX_FILES:
                raise InstallerError(f'Package exceeds {_MAX_FILES} files')
            if total_bytes > _MAX_BYTES:
                raise InstallerError(
                    f'Package exceeds {_MAX_BYTES // (1024 * 1024)} MiB unpacked')
            yield relative, entry.path, int(file_stat.st_size)
        pending.extend(reversed(child_directories))


def _copy_validated_tree(source: str, destination: str) -> int:
    os.makedirs(destination, exist_ok=False)
    count = 0
    for relative, full, _size in _walk_validated_files(source):
        if any(part in _RESERVED_METADATA_NAMES
               for part in relative.split('/')):
            raise InstallerError(
                'Package contains reserved Tofu origin metadata')
        target = os.path.join(destination, *relative.split('/'))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(full, 'rb') as input_file, open(target, 'xb') as output_file:
            _bounded_copy(input_file, output_file, _size)
        count += 1
    return count


def _find_skill_root(start_dir: str) -> str | None:
    direct = os.path.join(start_dir, 'SKILL.md')
    if os.path.isfile(direct):
        return start_dir
    matches: list[str] = []
    for root, dirnames, filenames in os.walk(start_dir, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _DENYLIST_NAMES)
        if 'SKILL.md' in filenames:
            matches.append(root)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise InstallerError(
            'Package contains multiple SKILL.md roots; select an exact subdir')
    return None


def _validate_skill_md(path: str) -> dict[str, Any]:
    try:
        if os.path.getsize(path) > _MAX_SKILL_MD_BYTES:
            raise InstallerError('SKILL.md exceeds the 2 MiB instruction limit')
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
    except OSError as exc:
        logger.warning('[SkillInstaller] cannot read %s: %s', path, exc)
        raise InstallerError('Cannot read package SKILL.md') from exc
    except UnicodeDecodeError as exc:
        raise InstallerError('SKILL.md is not valid UTF-8') from exc
    meta, _body = _parse_frontmatter(text)
    name = str(meta.get('name') or '').strip()
    if not name:
        raise InstallerError('SKILL.md frontmatter missing required key: name')
    if len(name) > 128:
        raise InstallerError('SKILL.md name exceeds 128 characters')
    if not str(meta.get('description') or '').strip():
        raise InstallerError(
            'SKILL.md frontmatter missing required key: description')
    return meta


def canonical_skill_content_sha256(skill_root: str) -> str:
    """Hash normalized relative paths, sizes, and bytes for one package."""
    digest = hashlib.sha256()
    digest.update(_DIGEST_DOMAIN)
    rows = [
        row for row in _walk_validated_files(skill_root)
        if not any(part in _DIGEST_IGNORED_NAMES for part in row[0].split('/'))
    ]
    for relative, full, size in sorted(rows, key=lambda row: row[0]):
        encoded_path = relative.encode('utf-8')
        digest.update(len(encoded_path).to_bytes(4, 'big'))
        digest.update(encoded_path)
        digest.update(size.to_bytes(8, 'big'))
        hashed = 0
        with open(full, 'rb') as handle:
            while True:
                chunk = handle.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                hashed += len(chunk)
                if hashed > size or hashed > _MAX_BYTES:
                    raise InstallerError(
                        'Package file changed while its digest was computed')
                digest.update(chunk)
        if hashed != size:
            raise InstallerError(
                'Package file changed while its digest was computed')
    return digest.hexdigest()


def _normalized_file_manifest(
    manifest: dict[str, dict[str, Any]] | None,
) -> dict[str, tuple[int, str]]:
    """Validate an exact registry-supplied file manifest.

    The manifest is an alternative immutable seal for registries whose release
    identifiers are not Git commits. Every selected package file must be
    present exactly once with the declared size and SHA-256.
    """
    if manifest is None:
        return {}
    if not isinstance(manifest, dict) or not manifest:
        raise InstallerError('Expected file manifest must be a non-empty object')
    if len(manifest) > _MAX_FILES:
        raise InstallerError(f'Expected file manifest exceeds {_MAX_FILES} files')
    normalized: dict[str, tuple[int, str]] = {}
    portable_paths: set[str] = set()
    total_bytes = 0
    for raw_path, raw_spec in manifest.items():
        parts = _normalized_parts(str(raw_path or ''))
        if _is_denied(parts) or any(
                part in _RESERVED_METADATA_NAMES for part in parts):
            raise InstallerError('Expected file manifest contains a denied path')
        relative = '/'.join(parts)
        portable_path = relative.casefold()
        if portable_path in portable_paths:
            raise InstallerError('Expected file manifest has a path collision')
        portable_paths.add(portable_path)
        if not isinstance(raw_spec, dict):
            raise InstallerError('Expected file manifest entry is invalid')
        try:
            size = int(raw_spec.get('size'))
        except (TypeError, ValueError) as exc:
            raise InstallerError(
                'Expected file manifest size is invalid') from exc
        digest = str(raw_spec.get('sha256') or '').lower()
        if (size < 0 or size > _MAX_BYTES or not re.fullmatch(
                r'[0-9a-f]{64}', digest)):
            raise InstallerError('Expected file manifest entry is invalid')
        total_bytes += size
        if total_bytes > _MAX_BYTES:
            raise InstallerError(
                f'Expected file manifest exceeds '
                f'{_MAX_BYTES // (1024 * 1024)} MiB')
        normalized[relative] = (size, digest)
    if 'SKILL.md' not in normalized:
        raise InstallerError('Expected file manifest does not contain SKILL.md')
    return normalized


def _verify_file_manifest(
    skill_root: str,
    expected: dict[str, tuple[int, str]],
) -> None:
    if not expected:
        return
    actual = {
        relative: (full, size)
        for relative, full, size in _walk_validated_files(skill_root)
        if not any(
            part in _DIGEST_IGNORED_NAMES for part in relative.split('/'))
    }
    if set(actual) != set(expected):
        raise InstallerError(
            'Registry file manifest does not match the selected package')
    for relative in sorted(expected):
        expected_size, expected_digest = expected[relative]
        full, actual_size = actual[relative]
        if actual_size != expected_size:
            raise InstallerError(
                'Registry file manifest size does not match the package')
        digest = hashlib.sha256()
        hashed = 0
        with open(full, 'rb') as handle:
            while True:
                chunk = handle.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                hashed += len(chunk)
                if hashed > expected_size:
                    raise InstallerError(
                        'Package file changed during manifest verification')
                digest.update(chunk)
        if (hashed != expected_size or not hmac.compare_digest(
                digest.hexdigest(), expected_digest)):
            raise InstallerError(
                'Registry file manifest digest does not match the package')


def _detect_install_hints(skill_root: str) -> list[dict[str, str]]:
    hints = []
    for name in ('install.sh', 'install-cc.sh', 'install-openclaw.sh'):
        if os.path.isfile(os.path.join(skill_root, name)):
            hints.append({
                'file': name,
                'note': (
                    'Installer script retained but never auto-executed. '
                    'Inspect it before running it manually.'),
            })
    return hints


@contextmanager
def _install_root_lock(target_root: str):
    """Serialize name selection and activation across threads and processes."""
    os.makedirs(target_root, exist_ok=True)
    lock_path = os.path.join(target_root, '.install.lock')
    with _INSTALL_LOCK, open(lock_path, 'a+b') as handle:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            fcntl = None
        try:
            yield
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass


def _write_origin_markers(
    stage: str,
    *,
    catalog_id: str | None,
    source_revision: str | None,
    content_sha256: str,
    owner_user_id: int | None,
    source_registry: str | None,
    source_url: str | None,
) -> None:
    if catalog_id:
        with open(os.path.join(stage, '.catalog_id'), 'x', encoding='utf-8') as handle:
            handle.write(catalog_id.strip() + '\n')
    origin = {
        'contract': 'tofu.skill-origin/v1',
        'catalog_id': catalog_id or '',
        'source_revision': source_revision or '',
        'source_registry': source_registry or '',
        'source_url': source_url or '',
        'content_sha256': content_sha256,
        'owner_user_id': owner_user_id,
        'installed_at': datetime.now(timezone.utc).isoformat(),
        'scripts_executed': False,
    }
    with open(os.path.join(stage, '.skill-origin.json'), 'x', encoding='utf-8') as handle:
        json.dump(origin, handle, ensure_ascii=False, sort_keys=True,
                  separators=(',', ':'))
        handle.write('\n')


def _existing_catalog_target(
    target_root: str,
    catalog_id: str | None,
) -> tuple[str | None, int]:
    """Find an exact catalog target and count bounded root entries."""
    matches: list[str] = []
    scanned = 0
    try:
        entries = os.scandir(target_root)
    except OSError as exc:
        raise InstallerError('Cannot inspect the skill installation root') from exc
    with entries:
        for entry in entries:
            if entry.name.startswith('.'):
                continue
            scanned += 1
            if scanned > _MAX_FILES:
                raise InstallerError(
                    f'Skills root exceeds {_MAX_FILES} package entries')
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if not catalog_id:
                continue
            marker = os.path.join(entry.path, '.catalog_id')
            if os.path.islink(marker) or not os.path.isfile(marker):
                continue
            try:
                with open(marker, encoding='utf-8') as handle:
                    value = handle.read(129)
            except (OSError, UnicodeDecodeError):
                continue
            if (catalog_id and value.strip() == catalog_id
                    and len(value.strip()) <= 128):
                matches.append(entry.name)
    if len(matches) > 1:
        raise InstallerError(
            f'Catalog skill {catalog_id!r} has multiple installed targets')
    return (matches[0] if matches else None), scanned


def install_skill_package(
    source: str | bytes,
    *,
    scope: str = 'project',
    project_path: str | None = None,
    owner_user_id: int | None = None,
    overwrite: bool = False,
    original_filename: str | None = None,
    catalog_id: str | None = None,
    subdir: str | None = None,
    expected_content_sha256: str | None = None,
    source_revision: str | None = None,
    expected_file_manifest: dict[str, dict[str, Any]] | None = None,
    ignored_archive_paths: set[str] | frozenset[str] | None = None,
    source_registry: str | None = None,
    source_url: str | None = None,
) -> dict[str, Any]:
    """Validate, verify, stage, and atomically activate one skill package."""
    catalog_id = str(catalog_id or '').strip() or None
    source_revision = str(source_revision or '').strip() or None
    source_registry = str(source_registry or '').strip().lower() or None
    source_url = str(source_url or '').strip() or None
    if scope not in ('project', 'global'):
        raise InstallerError(f'Invalid scope: {scope!r}')
    expected_manifest = _normalized_file_manifest(expected_file_manifest)
    if catalog_id and (
            not source_revision
            or (not expected_content_sha256 and not expected_manifest)):
        raise InstallerError(
            'Catalog installs require an immutable revision and content seal')
    if catalog_id and not _CATALOG_ID_RE.fullmatch(str(catalog_id)):
        raise InstallerError('Catalog id must be a normalized package identifier')
    if catalog_id and not _SOURCE_REVISION_RE.fullmatch(str(source_revision)):
        raise InstallerError('Catalog source revision is invalid')
    if source_registry and not _SOURCE_REGISTRY_RE.fullmatch(source_registry):
        raise InstallerError('Source registry identifier is invalid')
    if source_url and (
            len(source_url) > 2_048 or not source_url.startswith('https://')):
        raise InstallerError('Source URL must be a bounded HTTPS URL')
    expected_digest = str(expected_content_sha256 or '').lower()
    if expected_digest and (
            len(expected_digest) != 64
            or any(char not in '0123456789abcdef' for char in expected_digest)):
        raise InstallerError('Expected content digest must be 64 lowercase hex chars')
    ignored_paths = frozenset(
        '/'.join(_normalized_parts(str(path or '')))
        for path in (ignored_archive_paths or ()))
    if 'SKILL.md' in ignored_paths:
        raise InstallerError('SKILL.md cannot be ignored during installation')

    label = original_filename or (
        os.path.basename(source) if isinstance(source, str) else '<uploaded>')
    logger.info('[SkillInstaller] validating %s (scope=%s)', label, scope)

    with tempfile.TemporaryDirectory(prefix='tofu-skill-') as temporary:
        extracted = os.path.join(temporary, 'extracted')
        os.makedirs(extracted)
        selected_is_root = False
        try:
            if isinstance(source, (bytes, bytearray)):
                _guard_zip_directory(bytes(source))
                with zipfile.ZipFile(io.BytesIO(source)) as archive:
                    _safe_extract_zip(
                        archive, extracted, subdir=subdir,
                        ignored_paths=ignored_paths)
                selected_is_root = bool(subdir)
            elif isinstance(source, str) and os.path.isdir(source):
                selected_source = source
                if subdir:
                    selected_source = os.path.join(
                        source, *_normalized_parts(subdir.strip('/')))
                    if not os.path.isdir(selected_source):
                        raise InstallerError(f'Sub-skill {subdir!r} was not found')
                    selected_is_root = True
                if os.path.islink(selected_source):
                    raise InstallerError('Symlink package roots are rejected')
                copied = os.path.join(extracted, 'package')
                _copy_validated_tree(selected_source, copied)
                extracted = copied
            elif isinstance(source, str) and os.path.isfile(source):
                if not zipfile.is_zipfile(source):
                    raise InstallerError('Only zip archives and directories are supported')
                _guard_zip_directory(source)
                with zipfile.ZipFile(source) as archive:
                    _safe_extract_zip(
                        archive, extracted, subdir=subdir,
                        ignored_paths=ignored_paths)
                selected_is_root = bool(subdir)
            else:
                raise InstallerError(f'Cannot read source: {source!r}')
        except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise InstallerError(f'Invalid zip archive: {exc}') from exc

        skill_root = extracted if selected_is_root else _find_skill_root(extracted)
        if not skill_root or not os.path.isfile(os.path.join(skill_root, 'SKILL.md')):
            raise InstallerError('No unambiguous SKILL.md package root was found')

        list(_walk_validated_files(skill_root))
        _verify_file_manifest(skill_root, expected_manifest)
        metadata = _validate_skill_md(os.path.join(skill_root, 'SKILL.md'))
        skill_id = _make_memory_id(str(metadata['name']).strip())
        if not skill_id:
            raise InstallerError('SKILL.md name does not produce a valid id')
        content_sha256 = canonical_skill_content_sha256(skill_root)
        if expected_digest and not hmac.compare_digest(
                content_sha256, expected_digest):
            raise InstallerError(
                'Catalog content digest mismatch; the package was not installed')

        target_root = resolve_skill_install_dir(
            scope, project_path, owner_user_id=owner_user_id)
        target_root_real = os.path.realpath(target_root)
        stage = ''
        backup = ''
        target = ''
        replaced = False
        with _install_root_lock(target_root):
            catalog_target_id, existing_root_entries = (
                _existing_catalog_target(target_root, catalog_id))
            if catalog_target_id and not overwrite:
                raise InstallerError(
                    f'Catalog skill {catalog_id!r} is already installed as '
                    f'{catalog_target_id!r}; set overwrite=true to replace it')
            target_id = catalog_target_id or skill_id
            target = os.path.join(target_root, target_id)
            # A catalog overwrite is authorized only for the directory carrying
            # that exact catalog marker. A same-name manual package is a
            # different origin and receives a suffixed neighbor instead.
            replace_target = bool(overwrite and (
                not catalog_id or catalog_target_id))
            if os.path.exists(target) and not replace_target:
                suffix = 2
                while os.path.exists(os.path.join(
                        target_root, f'{skill_id}_{suffix}')):
                    suffix += 1
                target_id = f'{skill_id}_{suffix}'
                target = os.path.join(target_root, target_id)
            if not replace_target and existing_root_entries >= _MAX_FILES:
                raise InstallerError(
                    f'Skills root already contains {_MAX_FILES} package entries')
            try:
                contained = (
                    os.path.commonpath([
                        target_root_real, os.path.realpath(target),
                    ]) == target_root_real)
            except ValueError:
                contained = False
            if not contained:
                raise InstallerError('Resolved install target escapes skills root')
            replaced = os.path.exists(target)
            stage = os.path.join(
                target_root, f'.stage-{target_id}-{uuid.uuid4().hex}')
            backup = os.path.join(
                target_root, f'.backup-{target_id}-{uuid.uuid4().hex}')
            try:
                _copy_validated_tree(skill_root, stage)
                staged_digest = canonical_skill_content_sha256(stage)
                if not hmac.compare_digest(staged_digest, content_sha256):
                    raise InstallerError(
                        'Package content changed while it was staged')
                _write_origin_markers(
                    stage, catalog_id=catalog_id,
                    source_revision=source_revision,
                    content_sha256=content_sha256,
                    owner_user_id=owner_user_id,
                    source_registry=source_registry,
                    source_url=source_url)
                staged = _memory_from_file(
                    os.path.join(stage, 'SKILL.md'), scope=scope,
                    package_dir=stage, memory_id_override=target_id,
                    owner_user_id=owner_user_id)
                if not staged:
                    raise InstallerError('Staged SKILL.md could not be loaded')
                if replaced:
                    os.replace(target, backup)
                try:
                    os.replace(stage, target)
                except Exception:
                    if replaced and os.path.exists(backup) and not os.path.exists(target):
                        os.replace(backup, target)
                    raise
                memory = _memory_from_file(
                    os.path.join(target, 'SKILL.md'), scope=scope,
                    package_dir=target, memory_id_override=target_id,
                    owner_user_id=owner_user_id)
                if not memory:
                    if os.path.isdir(target):
                        shutil.rmtree(target)
                    if replaced and os.path.exists(backup):
                        os.replace(backup, target)
                    raise InstallerError('Activated SKILL.md could not be loaded')
                if os.path.isdir(backup):
                    try:
                        shutil.rmtree(backup)
                    except OSError as exc:
                        logger.warning(
                            '[SkillInstaller] installed %s but retained backup '
                            '%s after cleanup failure: %s', target_id, backup, exc)
                skill_id = target_id
            finally:
                if stage and os.path.isdir(stage):
                    shutil.rmtree(stage)
                # Preserve a leftover backup after rollback failure; it is
                # recoverable user data and must never be silently deleted.

        try:
            from lib.skills.registry import _invalidate_skills_cache
            _invalidate_skills_cache()
        except Exception as exc:
            logger.debug('[SkillInstaller] cache invalidation skipped: %s', exc)
        install_hints = _detect_install_hints(target)
        audit_log(
            'skill_install', skill_id=skill_id, scope=scope,
            owner_user_id=owner_user_id, catalog_id=catalog_id or '',
            source_revision=source_revision or '',
            source_registry=source_registry or '',
            content_sha256=content_sha256, replaced=replaced,
            scripts_executed=False,
            install_hints=[hint['file'] for hint in install_hints],
        )
        logger.info('[SkillInstaller] installed %s digest=%s',
                    skill_id, content_sha256[:16])
        return {
            'memory': memory,
            'install_hints': install_hints,
            'replaced': replaced,
            'catalog_id': catalog_id or '',
            'source_revision': source_revision or '',
            'source_registry': source_registry or '',
            'source_url': source_url or '',
            'content_sha256': content_sha256,
            'scripts_executed': False,
        }
