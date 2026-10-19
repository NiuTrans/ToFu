"""Durable, identity-bound progress for the explicit Tofu-DB migration owner.

MigrationCheckpoint owns its named file plus the reserved ``.lock``/``.new``
siblings. A stopped-source importer supplies independently verified target
counts/digests before advancing; this module never treats a cursor as proof
of current target contents. SQL scans and semantic import adapters live outside
this publication boundary. Limits come from the generated Schema IR contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
from uuid import UUID

from lib.storage_authority_sql.durability import write_json_durable
from lib.storage_authority_sql.migration.generated_tofudb_migration import (
    CHECKPOINT_FORMAT,
    MAX_CHECKPOINT_BYTES,
    MAX_CURSOR_BYTES,
    MAX_DOMAINS,
    MAX_KEY_COLUMNS,
)


class MigrationCheckpointError(ValueError):
    """Invalid, mismatched, unavailable or unverified migration progress."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=True, allow_nan=False).encode('ascii')
    except (TypeError, ValueError, RecursionError) as exc:
        raise MigrationCheckpointError('migration state is not canonical JSON') from exc


def _digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 2**63 - 1


def _cursor(value: Any) -> bool:
    return (
        isinstance(value, list) and len(value) <= MAX_KEY_COLUMNS
        and all(isinstance(part, str) or type(part) is int
                and -(2**63) <= part <= 2**63 - 1 for part in value)
        and sum(len(part) if isinstance(part, str) else 20 for part in value) <= MAX_CURSOR_BYTES
        and len(_canonical(value)) <= MAX_CURSOR_BYTES
    )


@dataclass(frozen=True, slots=True)
class MigrationBinding:
    source_sha256: str
    source_schema_version: int
    target_authority_uuid: str
    plan_sha256: str

    def document(self) -> dict[str, Any]:
        return {
            'sourceSha256': self.source_sha256,
            'sourceSchemaVersion': self.source_schema_version,
            'targetAuthorityUuid': self.target_authority_uuid,
            'planSha256': self.plan_sha256,
        }

    def __post_init__(self) -> None:
        if not isinstance(self.target_authority_uuid, str) or len(self.target_authority_uuid) != 36:
            raise MigrationCheckpointError('invalid target authority identity')
        try:
            canonical_uuid = str(UUID(self.target_authority_uuid))
        except (ValueError, TypeError, AttributeError) as exc:
            raise MigrationCheckpointError('invalid target authority identity') from exc
        if (not _digest(self.source_sha256) or not _digest(self.plan_sha256)
                or not _count(self.source_schema_version)
                or self.source_schema_version == 0
                or canonical_uuid != self.target_authority_uuid
                or UUID(canonical_uuid).int == 0):
            raise MigrationCheckpointError('invalid migration binding')


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MigrationCheckpointError('duplicate migration state field')
        result[key] = value
    return result


def _regular(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise MigrationCheckpointError('migration state must be a private regular file')
    return metadata


class MigrationCheckpoint:
    """One exclusively locked progress file; reopen after any publish failure."""

    def __init__(self, path: Path, binding: MigrationBinding):
        import fcntl

        self.path = Path(path)
        self.binding = binding
        self._lock_fd: int | None = None
        self._usable = False
        if not self.path.is_absolute() or not self.path.parent.is_dir():
            raise MigrationCheckpointError('checkpoint requires an absolute existing parent')
        lock_path = self.path.with_name(self.path.name + '.lock')
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise MigrationCheckpointError('migration lock must be a private regular file')
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = descriptor
            self._domains: dict[str, dict[str, Any]] = {}
            if self.path.exists() or self.path.is_symlink():
                self._domains = self._read()
            replacement = self.path.with_name(self.path.name + '.new')
            if replacement.exists() or replacement.is_symlink():
                if _regular(replacement).st_size > MAX_CHECKPOINT_BYTES:
                    raise MigrationCheckpointError('stale checkpoint temporary exceeds its bound')
                replacement.unlink()
            self._usable = True
            if not self.path.exists():
                self._publish({})
        except BaseException:
            self._lock_fd = None
            os.close(descriptor)
            raise

    def __enter__(self) -> MigrationCheckpoint:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self._usable = False
        if self._lock_fd is not None:
            descriptor, self._lock_fd = self._lock_fd, None
            os.close(descriptor)

    def _require_usable(self) -> None:
        if not self._usable:
            raise MigrationCheckpointError('checkpoint is closed or requires reopen after failure')

    def _read(self) -> dict[str, dict[str, Any]]:
        if _regular(self.path).st_size > MAX_CHECKPOINT_BYTES:
            raise MigrationCheckpointError('checkpoint exceeds its byte bound')
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            raw = stream.read(MAX_CHECKPOINT_BYTES + 1)
        if len(raw) > MAX_CHECKPOINT_BYTES:
            raise MigrationCheckpointError('checkpoint exceeds its byte bound')
        try:
            envelope = json.loads(raw, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise MigrationCheckpointError('checkpoint is unreadable') from exc
        if not isinstance(envelope, dict) or set(envelope) != {'body', 'sha256'}:
            raise MigrationCheckpointError('checkpoint envelope is invalid')
        body = envelope['body']
        if (not _digest(envelope['sha256'])
                or hashlib.sha256(_canonical(body)).hexdigest() != envelope['sha256']
                or not isinstance(body, dict)
                or set(body) != {'format', 'binding', 'domains'}
                or body['format'] != CHECKPOINT_FORMAT
                or body['binding'] != self.binding.document()):
            raise MigrationCheckpointError('checkpoint checksum or source/target/plan binding differs')
        domains = body['domains']
        if not isinstance(domains, dict) or len(domains) > MAX_DOMAINS:
            raise MigrationCheckpointError('checkpoint domain bound exceeded')
        for domain, entry in domains.items():
            self._validate_entry(domain, entry)
        return domains

    @staticmethod
    def _validate_entry(domain: str, entry: Any) -> None:
        if (not isinstance(domain, str)
                or re.fullmatch('[a-z][a-z0-9_.-]{0,127}', domain) is None
                or not isinstance(entry, dict)
                or set(entry) != {'cursor', 'rows', 'sha256', 'complete'}
                or not _cursor(entry['cursor']) or not _count(entry['rows'])
                or not _digest(entry['sha256']) or type(entry['complete']) is not bool
                or entry['complete'] and entry['cursor'] != []
                or not entry['complete'] and (entry['rows'] == 0) != (entry['cursor'] == [])):
            raise MigrationCheckpointError('invalid migration domain progress')

    def domain(self, name: str) -> dict[str, Any] | None:
        self._require_usable()
        entry = self._domains.get(name)
        return None if entry is None else {**entry, 'cursor': list(entry['cursor'])}

    def record_verified_prefix(
        self, domain: str, *, cursor: list[str | int], source_rows: int,
        source_sha256: str, target_rows: int, target_sha256: str,
        complete: bool = False,
    ) -> None:
        """Persist only after independent source and target enumeration agree.

        ``complete`` additionally requires both enumerations to be exhausted.
        A resumed caller must revalidate target contents before skipping work.
        """
        self._require_usable()
        if not _cursor(cursor):
            raise MigrationCheckpointError('invalid migration resume cursor')
        # A completed domain is revalidated by count/digest, so retaining its
        # potentially large last key has no resume value.
        entry = {'cursor': [] if complete else cursor, 'rows': source_rows, 'sha256': source_sha256,
                 'complete': complete}
        self._validate_entry(domain, entry)
        if (not _count(target_rows) or target_rows != source_rows
                or not _digest(target_sha256) or target_sha256 != source_sha256):
            raise MigrationCheckpointError('source and independently verified target differ')
        prior = self._domains.get(domain)
        if prior is not None:
            if entry == prior:
                return
            if (prior['complete'] or source_rows < prior['rows']
                    or not complete and source_rows > prior['rows'] and cursor == prior['cursor']
                    or source_rows == prior['rows'] and (
                        not complete and cursor != prior['cursor']
                        or source_sha256 != prior['sha256'])):
                raise MigrationCheckpointError('migration progress regressed or changed a verified prefix')
        domains = {**self._domains, domain: {**entry, 'cursor': list(entry['cursor'])}}
        if len(domains) > MAX_DOMAINS:
            raise MigrationCheckpointError('checkpoint domain bound exceeded')
        self._publish(domains)

    def _publish(self, domains: dict[str, dict[str, Any]]) -> None:
        body = {'format': CHECKPOINT_FORMAT, 'binding': self.binding.document(), 'domains': domains}
        envelope = {'body': body, 'sha256': hashlib.sha256(_canonical(body)).hexdigest()}
        if len(_canonical(envelope)) + 1 > MAX_CHECKPOINT_BYTES:
            raise MigrationCheckpointError('checkpoint exceeds its aggregate byte bound')
        try:
            write_json_durable(self.path, envelope)
        except BaseException:
            self._usable = False
            raise
        self._domains = domains
