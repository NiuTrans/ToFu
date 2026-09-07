"""Bounded, crash-recoverable logical commit shadow segments.

Responsibility: persist the filesystem-sink representation of committed
logical records without changing today's database authority. Production
commands reach this writer only through ``logical_outbox``: the semantic
mutation and pending record share one database transaction, then a background
publisher performs this file I/O after commit. This writer remains deliberately
``authoritative = False`` until replay/projection/cutover evidence promotes it.

Each private segment has a checksummed identity header followed by
length-prefixed canonical-JSON records.  Every append is fsynced; rotation is
an atomic rename plus directory fsync; startup truncates only an incomplete
tail of the sole open segment.  Complete checksum/sequence corruption fails
closed.  Byte and record ceilings are explicit and the writer never deletes
segments to make room.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import struct
import threading
import time
from typing import Any, BinaryIO, Iterator

from lib.storage_sidecar.durability import fsync_directory


FORMAT_VERSION = 'tofu.logical-shadow.v1'
RECORD_FORMAT = 'tofu.logical-commit.v1'
AUTHORITATIVE = False

_MAGIC = b'TOFU-LSHADOW\x00\x01\n'
_LENGTH = struct.Struct('>I')
_DIGEST_BYTES = hashlib.sha256().digest_size
_HEADER_MAX_BYTES = 16 * 1024
_SEALED_PATTERN = re.compile(
    r'^segment-(?P<start>[0-9]{20})-(?P<end>[0-9]{20})\.sealed$')
_OPEN_PATTERN = re.compile(r'^segment-(?P<start>[0-9]{20})\.open$')
_STREAM_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
_HEX_DIGEST_PATTERN = re.compile(r'^[0-9a-f]{64}$')

DEFAULT_MAX_SEGMENT_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_RECORD_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024


class LogicalShadowError(RuntimeError):
    """Base error for the non-authoritative logical shadow."""


class LogicalShadowCorruptionError(LogicalShadowError):
    """A complete frame or lineage invariant failed validation."""


class LogicalShadowCapacityError(LogicalShadowError):
    """A bounded record, segment, or total-byte ceiling was reached."""


class LogicalShadowPermissionError(LogicalShadowError):
    """The shadow cannot guarantee private files or exclusive ownership."""


class LogicalShadowUnavailableError(LogicalShadowError):
    """The storage environment cannot currently sustain durable appends."""


class LogicalShadowBusyError(LogicalShadowUnavailableError):
    """Another healthy publisher currently owns the stream writer lock."""


@dataclass(frozen=True, slots=True)
class LogicalCommitReceipt:
    sequence: int
    record_digest: str
    request_digest: str
    segment: str
    offset: int
    frame_bytes: int
    durability: str = 'file-fsync-before-return'
    duplicate: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            'sequence': self.sequence,
            'record_digest': self.record_digest,
            'request_digest': self.request_digest,
            'segment': self.segment,
            'offset': self.offset,
            'frame_bytes': self.frame_bytes,
            'durability': self.durability,
            'duplicate': self.duplicate,
        }


@dataclass(frozen=True, slots=True)
class LogicalShadowStatus:
    format: str
    authoritative: bool
    stream_id: str
    next_sequence: int
    records: int
    sealed_segments: int
    active_segment: str
    bytes_used: int
    max_segment_bytes: int
    max_record_bytes: int
    max_total_bytes: int
    repaired_tail_bytes: int
    fsync_each_append: bool
    access_mode: str
    closed: bool
    poisoned: bool

    def as_dict(self) -> dict[str, object]:
        return {
            'format': self.format,
            'authoritative': self.authoritative,
            'stream_id': self.stream_id,
            'next_sequence': self.next_sequence,
            'records': self.records,
            'sealed_segments': self.sealed_segments,
            'active_segment': self.active_segment,
            'bytes_used': self.bytes_used,
            'max_segment_bytes': self.max_segment_bytes,
            'max_record_bytes': self.max_record_bytes,
            'max_total_bytes': self.max_total_bytes,
            'repaired_tail_bytes': self.repaired_tail_bytes,
            'fsync_each_append': self.fsync_each_append,
            'access_mode': self.access_mode,
            'closed': self.closed,
            'poisoned': self.poisoned,
        }


@dataclass(frozen=True, slots=True)
class _SegmentScan:
    start_sequence: int
    end_sequence: int
    records: int
    valid_bytes: int
    repaired_tail_bytes: int


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(',', ':'),
            sort_keys=True,
        ).encode('utf-8')
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError('logical commit payload must be canonical JSON') from exc


def _frame(payload: bytes) -> bytes:
    return _LENGTH.pack(len(payload)) + payload + hashlib.sha256(payload).digest()


def _validate_stream_id(stream_id: str) -> str:
    if not _STREAM_ID_PATTERN.fullmatch(stream_id):
        raise ValueError(
            'stream_id must be 1-128 safe identifier characters')
    return stream_id


def _validate_record(record: dict[str, Any], *, stream_id: str,
                     expected_sequence: int) -> None:
    if record.get('format') != RECORD_FORMAT:
        raise LogicalShadowCorruptionError('logical record format mismatch')
    if record.get('stream_id') != stream_id:
        raise LogicalShadowCorruptionError('logical record stream mismatch')
    if record.get('sequence') != expected_sequence:
        raise LogicalShadowCorruptionError('logical record sequence gap')
    event_id = record.get('event_id')
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 256:
        raise LogicalShadowCorruptionError('logical record event_id is invalid')
    operation = record.get('operation')
    if not isinstance(operation, str) or not 1 <= len(operation) <= 256:
        raise LogicalShadowCorruptionError('logical record operation is invalid')
    tenant_id = record.get('tenant_id')
    if not isinstance(tenant_id, str) or not 1 <= len(tenant_id) <= 128:
        raise LogicalShadowCorruptionError('logical record tenant_id is invalid')
    owner_user_id = record.get('owner_user_id')
    if (not isinstance(owner_user_id, int) or isinstance(owner_user_id, bool)
            or owner_user_id < 0):
        raise LogicalShadowCorruptionError(
            'logical record owner_user_id is invalid')
    committed_at_ms = record.get('committed_at_ms')
    if (not isinstance(committed_at_ms, int)
            or isinstance(committed_at_ms, bool)
            or committed_at_ms < 0):
        raise LogicalShadowCorruptionError(
            'logical record committed_at_ms is invalid')
    command_id = record.get('command_id')
    if command_id is not None and (
            not isinstance(command_id, str) or not 1 <= len(command_id) <= 256):
        raise LogicalShadowCorruptionError('logical record command_id is invalid')
    request_digest = record.get('request_digest')
    if (not isinstance(request_digest, str)
            or not _HEX_DIGEST_PATTERN.fullmatch(request_digest)):
        raise LogicalShadowCorruptionError(
            'logical record request_digest is invalid')
    payload = record.get('payload')
    if not isinstance(payload, dict):
        raise LogicalShadowCorruptionError('logical record payload is not an object')


class LogicalCommitShadow:
    """Single-writer logical shadow with bounded disk use.

    ``stream_id`` is the authority lineage UUID, not a machine-global name.
    The directory must be dedicated to this shadow. Owner access is the
    default. Explicit group access accepts only owner+group read/write files
    and an owner+group traversable directory; world access is always rejected.
    Existing permissions are validated instead of being silently changed.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        stream_id: str,
        max_segment_bytes: int = DEFAULT_MAX_SEGMENT_BYTES,
        max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        access_mode: str = 'owner',
    ) -> None:
        self.root = Path(root)
        self.stream_id = _validate_stream_id(stream_id)
        self.max_segment_bytes = int(max_segment_bytes)
        self.max_record_bytes = int(max_record_bytes)
        self.max_total_bytes = int(max_total_bytes)
        self.access_mode = str(access_mode).strip().lower()
        if self.access_mode not in {'owner', 'group'}:
            raise ValueError('access_mode must be owner or group')
        self._directory_mode = (
            0o700 if self.access_mode == 'owner' else 0o2770)
        self._file_mode = 0o600 if self.access_mode == 'owner' else 0o660
        if self.max_segment_bytes < 1024:
            raise ValueError('max_segment_bytes must be at least 1024')
        if not 1 <= self.max_record_bytes <= self.max_segment_bytes - 512:
            raise ValueError(
                'max_record_bytes must leave room for the segment header')
        if self.max_total_bytes < self.max_segment_bytes:
            raise ValueError('max_total_bytes must cover at least one segment')

        self._mutex = threading.RLock()
        self._lock_handle: BinaryIO | None = None
        self._active_handle: BinaryIO | None = None
        self._active_path: Path | None = None
        self._active_records = 0
        self._next_sequence = 1
        self._records = 0
        self._sealed_segments = 0
        self._bytes_used = 0
        self._repaired_tail_bytes = 0
        self._closed = False
        self._poisoned = False

        try:
            self._prepare_private_directory()
            self._acquire_writer_lock()
            self._recover_segments()
        except LogicalShadowError:
            self._close_active_handle()
            self._release_writer_lock()
            raise
        except OSError as exc:
            self._close_active_handle()
            self._release_writer_lock()
            raise LogicalShadowUnavailableError(
                'logical shadow storage operation failed') from exc
        except BaseException:
            self._close_active_handle()
            self._release_writer_lock()
            raise

    def _prepare_private_directory(self) -> None:
        existed = self.root.exists()
        try:
            self.root.mkdir(
                mode=self._directory_mode & 0o777,
                parents=True,
                exist_ok=True,
            )
        except OSError as exc:
            raise LogicalShadowPermissionError(
                'logical shadow directory cannot be created') from exc
        if not self.root.is_dir():
            raise LogicalShadowPermissionError(
                'logical shadow path is not a directory')
        if not existed:
            try:
                os.chmod(self.root, self._directory_mode)
            except OSError as exc:
                raise LogicalShadowPermissionError(
                    'logical shadow directory cannot be made private') from exc
        try:
            allowed = self._permissions_allowed(self.root, directory=True)
        except OSError as exc:
            raise LogicalShadowPermissionError(
                'logical shadow directory permissions are unreadable') from exc
        if not allowed:
            raise LogicalShadowPermissionError(
                'logical shadow directory violates its declared group/world '
                'access mode')

    def _permissions_allowed(self, path: Path, *, directory: bool) -> bool:
        bits = stat.S_IMODE(path.stat().st_mode)
        if bits & 0o007:
            return False
        owner_required = 0o700 if directory else 0o600
        if bits & owner_required != owner_required:
            return False
        if self.access_mode == 'owner':
            return bits & 0o077 == 0
        group_required = 0o070 if directory else 0o060
        return bits & group_required == group_required

    def _acquire_writer_lock(self) -> None:
        lock_path = self.root / '.writer.lock'
        lock_attempted = False
        try:
            try:
                descriptor = os.open(
                    lock_path,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL,
                    self._file_mode,
                )
                if hasattr(os, 'fchmod'):
                    os.fchmod(descriptor, self._file_mode)
                else:  # pragma: no cover - Windows CI
                    os.chmod(lock_path, self._file_mode)
            except FileExistsError:
                descriptor = os.open(lock_path, os.O_RDWR)
            handle = os.fdopen(descriptor, 'r+b', buffering=0)
            if not self._permissions_allowed(lock_path, directory=False):
                handle.close()
                raise LogicalShadowPermissionError(
                    'logical shadow writer lock violates its access mode')
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b'\0')
                os.fsync(handle.fileno())
            handle.seek(0)
            lock_attempted = True
            if os.name == 'nt':  # pragma: no cover - Windows CI
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except LogicalShadowPermissionError:
            raise
        except (BlockingIOError, OSError) as exc:
            try:
                handle.close()
            except (NameError, OSError):
                pass
            if lock_attempted and (
                    isinstance(exc, BlockingIOError)
                    or getattr(exc, 'errno', None) in {11, 13, 35}):
                raise LogicalShadowBusyError(
                    'another writer owns the logical shadow') from exc
            raise LogicalShadowUnavailableError(
                'another writer owns the logical shadow or locking failed') from exc
        self._lock_handle = handle

    def _release_writer_lock(self) -> None:
        handle, self._lock_handle = self._lock_handle, None
        if handle is None:
            return
        try:
            if os.name == 'nt':  # pragma: no cover - Windows CI
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()

    def _header_payload(self, start_sequence: int) -> bytes:
        return _canonical_json_bytes({
            'created_at_ms': int(time.time() * 1000),
            'format': FORMAT_VERSION,
            'start_sequence': start_sequence,
            'stream_id': self.stream_id,
        })

    @staticmethod
    def _open_name(start_sequence: int) -> str:
        return f'segment-{start_sequence:020d}.open'

    @staticmethod
    def _sealed_name(start_sequence: int, end_sequence: int) -> str:
        return (
            f'segment-{start_sequence:020d}-{end_sequence:020d}.sealed')

    def _create_open_segment(self, start_sequence: int) -> None:
        path = self.root / self._open_name(start_sequence)
        payload = self._header_payload(start_sequence)
        header = _MAGIC + _frame(payload)
        if self._bytes_used + len(header) > self.max_total_bytes:
            raise LogicalShadowCapacityError(
                'logical shadow byte budget cannot create another segment')
        try:
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                self._file_mode,
            )
            if hasattr(os, 'fchmod'):
                os.fchmod(descriptor, self._file_mode)
            else:  # pragma: no cover - Windows CI
                os.chmod(path, self._file_mode)
            handle = os.fdopen(descriptor, 'r+b', buffering=0)
            written = handle.write(header)
            if written != len(header):
                raise OSError('short segment-header write')
            os.fsync(handle.fileno())
            fsync_directory(self.root)
        except BaseException:
            try:
                handle.close()
            except (NameError, OSError):
                pass
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self._active_handle = handle
        self._active_path = path
        self._active_records = 0
        self._bytes_used += len(header)

    def _read_frame(
        self,
        stream: BinaryIO,
        *,
        maximum_bytes: int,
        partial_tail_ok: bool,
    ) -> tuple[bytes | None, int]:
        offset = stream.tell()
        prefix = stream.read(_LENGTH.size)
        if not prefix:
            return None, offset
        if len(prefix) != _LENGTH.size:
            if partial_tail_ok:
                return None, offset
            raise LogicalShadowCorruptionError('truncated frame length')
        (size,) = _LENGTH.unpack(prefix)
        if not 1 <= size <= maximum_bytes:
            raise LogicalShadowCorruptionError('logical frame length is invalid')
        payload = stream.read(size)
        digest = stream.read(_DIGEST_BYTES)
        if len(payload) != size or len(digest) != _DIGEST_BYTES:
            if partial_tail_ok:
                return None, offset
            raise LogicalShadowCorruptionError('truncated logical frame')
        if not hmac.compare_digest(
                hashlib.sha256(payload).digest(), digest):
            raise LogicalShadowCorruptionError('logical frame checksum mismatch')
        return payload, offset

    def _read_header(self, stream: BinaryIO, expected_start: int) -> None:
        if stream.read(len(_MAGIC)) != _MAGIC:
            raise LogicalShadowCorruptionError('logical segment magic mismatch')
        payload, _ = self._read_frame(
            stream, maximum_bytes=_HEADER_MAX_BYTES, partial_tail_ok=False)
        if payload is None:
            raise LogicalShadowCorruptionError('logical segment header is missing')
        try:
            header = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LogicalShadowCorruptionError(
                'logical segment header is invalid JSON') from exc
        if not isinstance(header, dict) or _canonical_json_bytes(header) != payload:
            raise LogicalShadowCorruptionError(
                'logical segment header is not canonical')
        if header.get('format') != FORMAT_VERSION:
            raise LogicalShadowCorruptionError('logical segment format mismatch')
        if header.get('stream_id') != self.stream_id:
            raise LogicalShadowCorruptionError('logical segment stream mismatch')
        if header.get('start_sequence') != expected_start:
            raise LogicalShadowCorruptionError(
                'logical segment start sequence mismatch')

    def _scan_segment(
        self,
        path: Path,
        *,
        expected_start: int,
        repair_tail: bool,
    ) -> _SegmentScan:
        mode = 'r+b' if repair_tail else 'rb'
        if not self._permissions_allowed(path, directory=False):
            raise LogicalShadowPermissionError(
                f'logical segment violates its access mode: {path.name}')
        repaired_tail_bytes = 0
        with path.open(mode, buffering=0) as stream:
            self._read_header(stream, expected_start)
            sequence = expected_start
            records = 0
            while True:
                payload, frame_offset = self._read_frame(
                    stream,
                    maximum_bytes=self.max_record_bytes,
                    partial_tail_ok=repair_tail,
                )
                if payload is None:
                    end_offset = stream.seek(0, os.SEEK_END)
                    if end_offset > frame_offset:
                        if not repair_tail:
                            raise LogicalShadowCorruptionError(
                                'sealed segment has an incomplete tail')
                        repaired_tail_bytes = end_offset - frame_offset
                        stream.truncate(frame_offset)
                        stream.flush()
                        os.fsync(stream.fileno())
                    valid_bytes = frame_offset
                    break
                try:
                    record = json.loads(payload)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise LogicalShadowCorruptionError(
                        'logical record is invalid JSON') from exc
                if (not isinstance(record, dict)
                        or _canonical_json_bytes(record) != payload):
                    raise LogicalShadowCorruptionError(
                        'logical record is not canonical')
                _validate_record(
                    record, stream_id=self.stream_id,
                    expected_sequence=sequence)
                sequence += 1
                records += 1
        return _SegmentScan(
            start_sequence=expected_start,
            end_sequence=sequence - 1,
            records=records,
            valid_bytes=valid_bytes,
            repaired_tail_bytes=repaired_tail_bytes,
        )

    def _segment_inventory(self) -> tuple[list[tuple[int, int, Path]],
                                          list[tuple[int, Path]]]:
        sealed: list[tuple[int, int, Path]] = []
        opened: list[tuple[int, Path]] = []
        try:
            children = list(self.root.iterdir())
        except OSError as exc:
            raise LogicalShadowUnavailableError(
                'logical shadow directory cannot be listed') from exc
        for child in children:
            sealed_match = _SEALED_PATTERN.fullmatch(child.name)
            if sealed_match:
                sealed.append((
                    int(sealed_match.group('start')),
                    int(sealed_match.group('end')),
                    child,
                ))
                continue
            open_match = _OPEN_PATTERN.fullmatch(child.name)
            if open_match:
                opened.append((int(open_match.group('start')), child))
                continue
            if child.name.startswith('segment-'):
                raise LogicalShadowCorruptionError(
                    f'unrecognized logical segment name: {child.name}')
        return sorted(sealed), sorted(opened)

    def _recover_segments(self) -> None:
        sealed, opened = self._segment_inventory()
        if len(opened) > 1:
            raise LogicalShadowCorruptionError(
                'multiple open logical segments require operator recovery')
        expected = 1
        bytes_used = 0
        records = 0
        for start, named_end, path in sealed:
            if start != expected or named_end < start:
                raise LogicalShadowCorruptionError(
                    'sealed logical segment sequence is not contiguous')
            scan = self._scan_segment(
                path, expected_start=start, repair_tail=False)
            if scan.records == 0 or scan.end_sequence != named_end:
                raise LogicalShadowCorruptionError(
                    'sealed logical segment name does not match its records')
            expected = named_end + 1
            records += scan.records
            bytes_used += scan.valid_bytes
        self._sealed_segments = len(sealed)

        if opened:
            start, path = opened[0]
            if start != expected:
                raise LogicalShadowCorruptionError(
                    'open logical segment sequence is not contiguous')
            scan = self._scan_segment(
                path, expected_start=start, repair_tail=True)
            expected = scan.end_sequence + 1
            records += scan.records
            bytes_used += scan.valid_bytes
            self._repaired_tail_bytes = scan.repaired_tail_bytes
            self._active_handle = path.open('r+b', buffering=0)
            self._active_handle.seek(0, os.SEEK_END)
            self._active_path = path
            self._active_records = scan.records

        self._next_sequence = expected
        self._records = records
        self._bytes_used = bytes_used
        if not opened:
            self._create_open_segment(expected)

    def _assert_appendable(self) -> None:
        if self._closed:
            raise LogicalShadowUnavailableError('logical shadow is closed')
        if self._poisoned:
            raise LogicalShadowUnavailableError(
                'logical shadow is poisoned after a failed append')
        if self._active_handle is None or self._active_path is None:
            raise LogicalShadowUnavailableError(
                'logical shadow has no active segment')

    def _rotate(self) -> None:
        assert self._active_handle is not None and self._active_path is not None
        if self._active_records <= 0:
            raise LogicalShadowCapacityError(
                'logical record cannot fit in an empty segment')
        start_match = _OPEN_PATTERN.fullmatch(self._active_path.name)
        if start_match is None:
            raise LogicalShadowCorruptionError('active segment name is invalid')
        start = int(start_match.group('start'))
        end = self._next_sequence - 1
        sealed = self.root / self._sealed_name(start, end)
        self._active_handle.flush()
        os.fsync(self._active_handle.fileno())
        self._active_handle.close()
        self._active_handle = None
        if sealed.exists():
            self._poisoned = True
            raise LogicalShadowCorruptionError(
                'logical sealed segment already exists')
        os.replace(self._active_path, sealed)
        fsync_directory(self.root)
        self._sealed_segments += 1
        self._active_path = None
        self._active_records = 0
        self._create_open_segment(self._next_sequence)

    def append(
        self,
        *,
        operation: str,
        tenant_id: str,
        owner_user_id: int,
        payload: dict[str, Any],
        command_id: str | None = None,
        request_digest: str | None = None,
        committed_at_ms: int | None = None,
        event_id: str | None = None,
        expected_sequence: int | None = None,
    ) -> LogicalCommitReceipt:
        """Append one already-committed semantic record and fsync it.

        This method cannot make a separate database transaction atomic with
        the append. Runtime integration supplies a transactional outbox, and
        callers still must not treat this receipt as database authority.
        """
        if not isinstance(payload, dict):
            raise ValueError('payload must be a JSON object')
        payload_bytes = _canonical_json_bytes(payload)
        digest = request_digest or hashlib.sha256(payload_bytes).hexdigest()
        timestamp = int(time.time() * 1000) if committed_at_ms is None else committed_at_ms
        with self._mutex:
            self._assert_appendable()
            sequence = (
                self._next_sequence
                if expected_sequence is None else expected_sequence)
            if (not isinstance(sequence, int) or isinstance(sequence, bool)
                    or sequence < 1):
                raise ValueError('expected_sequence must be a positive integer')
            resolved_event_id = event_id or f'sequence:{sequence}'
            record = {
                'command_id': command_id,
                'committed_at_ms': timestamp,
                'event_id': resolved_event_id,
                'format': RECORD_FORMAT,
                'operation': operation,
                'owner_user_id': owner_user_id,
                'payload': payload,
                'request_digest': digest,
                'sequence': sequence,
                'stream_id': self.stream_id,
                'tenant_id': tenant_id,
            }
            try:
                _validate_record(
                    record, stream_id=self.stream_id,
                expected_sequence=sequence)
            except LogicalShadowCorruptionError as exc:
                raise ValueError(str(exc)) from exc
            encoded = _canonical_json_bytes(record)
            if sequence < self._next_sequence:
                existing = self._record_location(sequence)
                if existing is None:
                    raise LogicalShadowCorruptionError(
                        'logical retry references missing committed sequence')
                existing_record, segment, offset, frame_bytes, record_digest = existing
                if _canonical_json_bytes(existing_record) != encoded:
                    raise LogicalShadowCorruptionError(
                        'logical sequence was reused for a different event')
                return LogicalCommitReceipt(
                    sequence=sequence,
                    record_digest=record_digest,
                    request_digest=digest,
                    segment=segment,
                    offset=offset,
                    frame_bytes=frame_bytes,
                    duplicate=True,
                )
            if sequence > self._next_sequence:
                raise LogicalShadowCorruptionError(
                    'logical append would create a sequence gap')
            if len(encoded) > self.max_record_bytes:
                raise LogicalShadowCapacityError(
                    'logical record exceeds max_record_bytes')
            frame = _frame(encoded)
            assert self._active_handle is not None
            active_size = self._active_handle.seek(0, os.SEEK_END)
            if active_size + len(frame) > self.max_segment_bytes:
                next_header_bytes = len(
                    _MAGIC + _frame(self._header_payload(self._next_sequence)))
                if (self._bytes_used + next_header_bytes + len(frame)
                        > self.max_total_bytes):
                    raise LogicalShadowCapacityError(
                        'logical shadow reached max_total_bytes; '
                        'no records deleted')
                try:
                    self._rotate()
                except LogicalShadowError:
                    raise
                except OSError as exc:
                    self._poisoned = True
                    raise LogicalShadowUnavailableError(
                        'logical shadow segment rotation failed') from exc
                assert self._active_handle is not None
                active_size = self._active_handle.seek(0, os.SEEK_END)
            if active_size + len(frame) > self.max_segment_bytes:
                raise LogicalShadowCapacityError(
                    'logical record cannot fit within max_segment_bytes')
            if self._bytes_used + len(frame) > self.max_total_bytes:
                raise LogicalShadowCapacityError(
                    'logical shadow reached max_total_bytes; no records deleted')

            assert self._active_path is not None
            offset = active_size
            try:
                written = self._active_handle.write(frame)
                if written != len(frame):
                    raise OSError('short logical-frame write')
                self._active_handle.flush()
                os.fsync(self._active_handle.fileno())
            except OSError as exc:
                self._poisoned = True
                try:
                    self._active_handle.truncate(offset)
                    self._active_handle.flush()
                    os.fsync(self._active_handle.fileno())
                except OSError:
                    pass
                raise LogicalShadowUnavailableError(
                    'logical shadow append/fsync failed') from exc

            self._next_sequence += 1
            self._records += 1
            self._active_records += 1
            self._bytes_used += len(frame)
            return LogicalCommitReceipt(
                sequence=sequence,
                record_digest=hashlib.sha256(encoded).hexdigest(),
                request_digest=digest,
                segment=self._active_path.name,
                offset=offset,
                frame_bytes=len(frame),
            )

    def _record_location(
        self, sequence: int,
    ) -> tuple[dict[str, Any], str, int, int, str] | None:
        """Locate one sequence without materializing the full history."""
        for path in self._ordered_segment_paths():
            sealed_match = _SEALED_PATTERN.fullmatch(path.name)
            open_match = _OPEN_PATTERN.fullmatch(path.name)
            match = sealed_match or open_match
            if match is None:
                continue
            start = int(match.group('start'))
            end = (
                int(sealed_match.group('end'))
                if sealed_match is not None else self._next_sequence - 1)
            if not start <= sequence <= end:
                continue
            with path.open('rb', buffering=0) as stream:
                self._read_header(stream, start)
                expected = start
                while True:
                    payload, offset = self._read_frame(
                        stream,
                        maximum_bytes=self.max_record_bytes,
                        partial_tail_ok=False,
                    )
                    if payload is None:
                        return None
                    frame_bytes = _LENGTH.size + len(payload) + _DIGEST_BYTES
                    if expected == sequence:
                        try:
                            record = json.loads(payload)
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise LogicalShadowCorruptionError(
                                'logical record is invalid JSON') from exc
                        if not isinstance(record, dict):
                            raise LogicalShadowCorruptionError(
                                'logical record is not an object')
                        _validate_record(
                            record,
                            stream_id=self.stream_id,
                            expected_sequence=sequence,
                        )
                        return (
                            record,
                            path.name,
                            offset,
                            frame_bytes,
                            hashlib.sha256(payload).hexdigest(),
                        )
                    expected += 1
        return None

    def _ordered_segment_paths(self) -> list[Path]:
        sealed, opened = self._segment_inventory()
        paths = [path for _start, _end, path in sealed]
        paths.extend(path for _start, path in opened)
        return paths

    def _iter_segment_records(self, path: Path) -> Iterator[dict[str, Any]]:
        sealed_match = _SEALED_PATTERN.fullmatch(path.name)
        open_match = _OPEN_PATTERN.fullmatch(path.name)
        start = int((sealed_match or open_match).group('start'))  # type: ignore[union-attr]
        with path.open('rb', buffering=0) as stream:
            self._read_header(stream, start)
            sequence = start
            while True:
                payload, _ = self._read_frame(
                    stream,
                    maximum_bytes=self.max_record_bytes,
                    partial_tail_ok=False,
                )
                if payload is None:
                    return
                try:
                    record = json.loads(payload)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise LogicalShadowCorruptionError(
                        'logical record is invalid JSON') from exc
                if (not isinstance(record, dict)
                        or _canonical_json_bytes(record) != payload):
                    raise LogicalShadowCorruptionError(
                        'logical record is not canonical')
                _validate_record(
                    record, stream_id=self.stream_id,
                    expected_sequence=sequence)
                sequence += 1
                yield record

    def read_records(
        self,
        *,
        start_sequence: int = 1,
        max_records: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return one bounded replay page beginning at an exact sequence."""
        if (
            not isinstance(start_sequence, int)
            or isinstance(start_sequence, bool)
            or start_sequence < 1
        ):
            raise ValueError('start_sequence must be a positive integer')
        if not 1 <= max_records <= 10_000:
            raise ValueError('max_records must be between 1 and 10000')
        with self._mutex:
            if self._active_handle is not None:
                self._active_handle.flush()
            records: list[dict[str, Any]] = []
            sealed, opened = self._segment_inventory()
            paths = [
                path for _start, end, path in sealed
                if end >= start_sequence
            ]
            paths.extend(
                path for start, path in opened
                if start <= self._next_sequence
            )
            for path in paths:
                for record in self._iter_segment_records(path):
                    if int(record['sequence']) < start_sequence:
                        continue
                    records.append(record)
                    if len(records) >= max_records:
                        return records
            return records

    def status(self) -> LogicalShadowStatus:
        with self._mutex:
            return LogicalShadowStatus(
                format=FORMAT_VERSION,
                authoritative=AUTHORITATIVE,
                stream_id=self.stream_id,
                next_sequence=self._next_sequence,
                records=self._records,
                sealed_segments=self._sealed_segments,
                active_segment=(
                    self._active_path.name if self._active_path is not None else ''),
                bytes_used=self._bytes_used,
                max_segment_bytes=self.max_segment_bytes,
                max_record_bytes=self.max_record_bytes,
                max_total_bytes=self.max_total_bytes,
                repaired_tail_bytes=self._repaired_tail_bytes,
                fsync_each_append=True,
                access_mode=self.access_mode,
                closed=self._closed,
                poisoned=self._poisoned,
            )

    def _close_active_handle(self) -> None:
        handle, self._active_handle = self._active_handle, None
        if handle is None:
            return
        try:
            handle.flush()
            os.fsync(handle.fileno())
        except OSError:
            self._poisoned = True
        finally:
            handle.close()

    def close(self) -> None:
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            self._close_active_handle()
            self._release_writer_lock()

    def __enter__(self) -> 'LogicalCommitShadow':
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


__all__ = [
    'AUTHORITATIVE',
    'DEFAULT_MAX_RECORD_BYTES',
    'DEFAULT_MAX_SEGMENT_BYTES',
    'DEFAULT_MAX_TOTAL_BYTES',
    'FORMAT_VERSION',
    'LogicalCommitReceipt',
    'LogicalCommitShadow',
    'LogicalShadowCapacityError',
    'LogicalShadowBusyError',
    'LogicalShadowCorruptionError',
    'LogicalShadowError',
    'LogicalShadowPermissionError',
    'LogicalShadowStatus',
    'LogicalShadowUnavailableError',
    'RECORD_FORMAT',
]
