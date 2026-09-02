"""Authenticated browser-to-server file transfer.

This module owns the one transport that turns a response readable only in a
user's browser session into a bounded file under ``data/fetched/``.  It does
not use Chrome's download manager: the extension streams response bytes back
to the server that issued the command.

Authority is explicit at every boundary.  A transfer is addressed by owner,
browser device, random transfer id and one-time token; redirects re-enter the
browser read policy before bytes are accepted and again before atomic commit.
The process-local registry and local blob writer are deliberately contained in
one class so a future distributed deployment can replace this transient store
without changing routes, task handlers or the extension wire contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import hmac
import mimetypes
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from urllib.parse import unquote, urlsplit

from lib.browser.log_safety import text_for_log
from lib.config_dir import fetched_path
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

TRANSFER_TTL_SECONDS = 180
MAX_ACTIVE_TRANSFERS = 8
MAX_ACTIVE_TRANSFERS_PER_OWNER = 2
MAX_TRANSFER_BYTES = 500 * 1024 * 1024
MAX_CHUNK_BYTES = 256 * 1024
MIN_TRANSFER_CHUNK_BYTES = 16 * 1024
MAX_STAGING_ARTIFACTS = 2048
STAGING_HANDOFF_GRACE_SECONDS = 10 * 60
STAGING_FILENAME_PREFIX = 'browser-transfer-'
_STAGING_PART_PREFIX = f'.{STAGING_FILENAME_PREFIX}'
SERVER_DOWNLOAD_FILENAME_PREFIX = 'server-download-'
_SERVER_DOWNLOAD_PART_PREFIX = f'.{SERVER_DOWNLOAD_FILENAME_PREFIX}'
_BROWSER_STAGING_TTL_HOURS = 7 * 24
_MAX_TRANSFER_URL_CHARS = 8192


class BrowserFileTransferError(RuntimeError):
    """Typed failure raised by the transfer owner and projected by routes."""

    def __init__(self, code: str, message: str, *, status: int = 400):
        self.code = str(code)
        self.status = int(status)
        super().__init__(message)


@dataclass
class _TransferState:
    transfer_id: str
    token: str
    owner_user_id: str
    client_id: str
    profile: str
    source_url: str
    max_bytes: int
    created_at: float
    last_activity_at: float
    part_path: str
    phase: str = 'created'
    final_url: str = ''
    content_type: str = ''
    content_disposition: str = ''
    suggested_filename: str = ''
    declared_length: int | None = None
    received_bytes: int = 0
    next_sequence: int = 0
    chunk_receipts: dict[int, tuple[int, str]] = field(default_factory=dict)
    digest: object = field(default_factory=hashlib.sha256)
    receipt: dict | None = None


def _positive_owner(value) -> str:
    owner = str(value or '').strip()
    try:
        normalized = int(owner) if owner.isascii() and owner.isdigit() else 0
    except (TypeError, ValueError, OverflowError):
        normalized = 0
    if normalized < 1:
        raise BrowserFileTransferError(
            'browser_file_transfer_invalid_owner',
            'owner_user_id must be a positive integer',
        )
    return str(normalized)


def _token_matches(expected: str, candidate: object) -> bool:
    """Compare an untrusted header value without a Unicode TypeError path."""
    try:
        supplied = str(candidate or '').encode('utf-8')
        reference = str(expected or '').encode('ascii')
    except (TypeError, ValueError, UnicodeError):
        return False
    return bool(supplied) and hmac.compare_digest(supplied, reference)


def _http_url(value, *, field_name: str) -> str:
    raw = str(value or '').strip()
    if len(raw) > _MAX_TRANSFER_URL_CHARS:
        raise BrowserFileTransferError(
            'browser_file_transfer_invalid_url',
            f'{field_name} exceeds the {_MAX_TRANSFER_URL_CHARS}-character limit',
        )
    try:
        parsed = urlsplit(raw)
        # Accessing ``port`` performs validation that urlsplit deliberately
        # defers (for example ``https://host:not-a-port``).
        parsed.port
    except ValueError as exc:
        raise BrowserFileTransferError(
            'browser_file_transfer_invalid_url',
            f'{field_name} must be a valid HTTP(S) URL',
        ) from exc
    if (parsed.scheme.lower() not in ('http', 'https')
            or not parsed.hostname
            or any(ord(char) <= 0x20 or ord(char) == 0x7f for char in raw)):
        raise BrowserFileTransferError(
            'browser_file_transfer_invalid_url',
            f'{field_name} must be a valid HTTP(S) URL',
        )
    return raw


def _bounded_text(value, limit: int) -> str:
    return str(value or '').strip()[:limit]


def _content_disposition_filename(value: str) -> str:
    """Extract a bounded filename hint without treating it as a path."""
    raw = _bounded_text(value, 1024)
    encoded = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", raw, re.I)
    if encoded:
        try:
            return unquote(encoded.group(1).strip().strip('"'))[:240]
        except (TypeError, ValueError):
            pass
    plain = re.search(r'filename\s*=\s*(?:"([^"]+)"|([^;]+))', raw, re.I)
    if plain:
        return (plain.group(1) or plain.group(2) or '').strip()[:240]
    return ''


def _safe_staged_filename(state: _TransferState) -> str:
    """Return a unique basename; response metadata can never choose a path."""
    hint = (
        _content_disposition_filename(state.content_disposition)
        or state.suggested_filename
    )
    if not hint:
        try:
            hint = os.path.basename(unquote(urlsplit(state.final_url).path))
        except (TypeError, ValueError):
            hint = ''
    hint = os.path.basename(str(hint or '').replace('\\', '/'))
    stem, ext = os.path.splitext(hint)
    if not ext:
        guessed = mimetypes.guess_extension(
            state.content_type.split(';', 1)[0].strip().lower()) or ''
        ext = '.jpg' if guessed == '.jpe' else guessed
    clean_stem = re.sub(r'[^\w.-]+', '_', stem, flags=re.UNICODE)
    clean_stem = clean_stem.strip('._-')[:72] or 'browser-file'
    clean_ext = re.sub(r'[^A-Za-z0-9.]', '', ext.lower())[:16]
    if clean_ext and not clean_ext.startswith('.'):
        clean_ext = '.' + clean_ext
    return (
        f'{STAGING_FILENAME_PREFIX}{state.transfer_id}-'
        f'{clean_stem}{clean_ext}'
    )


def _safe_server_download_filename(
    source_url: str,
    suggested_filename: str,
    content_type: str,
) -> str:
    """Return a unique server-direct staging basename without path authority."""
    hint = os.path.basename(str(suggested_filename or '').replace('\\', '/'))
    if not hint:
        try:
            hint = os.path.basename(unquote(urlsplit(source_url).path))
        except (TypeError, ValueError):
            hint = ''
    stem, ext = os.path.splitext(hint)
    if not ext:
        guessed = mimetypes.guess_extension(
            str(content_type or '').split(';', 1)[0].strip().lower()) or ''
        ext = '.jpg' if guessed == '.jpe' else guessed
    clean_stem = re.sub(r'[^\w.-]+', '_', stem, flags=re.UNICODE)
    clean_stem = clean_stem.strip('._-')[:72] or 'server-file'
    clean_ext = re.sub(r'[^A-Za-z0-9.]', '', ext.lower())[:16]
    if clean_ext and not clean_ext.startswith('.'):
        clean_ext = '.' + clean_ext
    return (
        f'{SERVER_DOWNLOAD_FILENAME_PREFIX}{uuid.uuid4().hex}-'
        f'{clean_stem}{clean_ext}'
    )


def _browser_staging_budget_bytes() -> int:
    """Return the boot-probed budget with a non-bypassable hard ceiling."""
    from runtime_guards import resolve_resource_budget

    return resolve_resource_budget(
        'TOFU_BROWSER_STAGING_MAX_MIB', minimum=16, maximum=4096,
    ) * 1024 * 1024


def _browser_staging_ttl_seconds() -> int:
    try:
        hours = int(os.environ.get(
            'TOFU_BROWSER_STAGING_TTL_HOURS',
            _BROWSER_STAGING_TTL_HOURS,
        ))
    except (TypeError, ValueError, OverflowError):
        hours = _BROWSER_STAGING_TTL_HOURS
    return max(1, min(30 * 24, hours)) * 3600


@lru_cache(maxsize=1)
def _storage_min_free_bytes() -> int:
    """Resolve the shared live-volume reserve without an unbounded override."""
    from runtime_guards import resolve_resource_budget

    return resolve_resource_budget(
        'TOFU_STORAGE_MIN_FREE_BYTES',
        minimum=64 * 1024 * 1024,
        maximum=1024 * 1024 * 1024 * 1024,
    )


def _live_staging_headroom(growth_bytes: int) -> bool:
    """Fail lean when the actual staging volume cannot preserve its reserve."""
    root = os.path.dirname(fetched_path('path-probe'))
    try:
        free_bytes = int(shutil.disk_usage(root).free)
    except (OSError, ValueError) as exc:
        logger.warning('[BrowserFileTransfer] disk headroom probe failed: %s', exc)
        return False
    return int(growth_bytes) <= max(0, free_bytes - _storage_min_free_bytes())


class BrowserFileTransferStore:
    """Bounded transient registry plus local atomic blob writer."""

    def __init__(
        self,
        *,
        ttl_seconds: int = TRANSFER_TTL_SECONDS,
        max_active: int = MAX_ACTIVE_TRANSFERS,
        max_per_owner: int = MAX_ACTIVE_TRANSFERS_PER_OWNER,
        clock=time.time,
    ):
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_active = max(1, int(max_active))
        self.max_per_owner = max(1, int(max_per_owner))
        self._clock = clock
        self._lock = threading.RLock()
        self._transfers: dict[str, _TransferState] = {}

    @staticmethod
    def _delete_path(path: str) -> bool:
        """Delete one reconstructible artifact and report actual reclamation."""
        if not path:
            return True
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            logger.warning('[BrowserFileTransfer] staging cleanup failed '
                           '(errno=%s)', getattr(exc, 'errno', None))
            return False

    def _delete_state_files(self, state: _TransferState) -> None:
        self._delete_path(state.part_path)
        if state.receipt:
            self._delete_path(str(state.receipt.get('path') or ''))

    def _reserve_staging_capacity(
        self,
        additional_bytes: int,
        additional_files: int,
    ) -> bool:
        """Reclaim old staging before reserving bounded bytes and file count."""
        root = os.path.dirname(fetched_path('path-probe'))
        protected = {
            os.path.realpath(str(state.receipt.get('path') or ''))
            for state in self._transfers.values()
            if state.receipt and state.receipt.get('path')
        }
        protected.update(
            os.path.realpath(state.part_path)
            for state in self._transfers.values()
            if state.part_path
        )
        now = self._clock()
        ttl = _browser_staging_ttl_seconds()
        rows = []
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    is_committed = entry.name.startswith((
                        STAGING_FILENAME_PREFIX,
                        SERVER_DOWNLOAD_FILENAME_PREFIX,
                    ))
                    is_partial = (
                        entry.name.startswith((
                            _STAGING_PART_PREFIX,
                            _SERVER_DOWNLOAD_PART_PREFIX,
                        ))
                        and entry.name.endswith('.part'))
                    if (not (is_committed or is_partial)
                            or not entry.is_file(follow_symlinks=False)):
                        continue
                    path = os.path.realpath(entry.path)
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    # Active partials are already represented by their full
                    # max_bytes/file reservation supplied by create(); counting
                    # their current artifact again rejects safe concurrency.
                    if is_partial and path in protected:
                        continue
                    expiry = self.ttl_seconds if is_partial else ttl
                    if (path not in protected
                            and now - stat.st_mtime > expiry
                            and self._delete_path(path)):
                        continue
                    # Failed deletion is still real usage. Never pretend bytes
                    # or inodes were reclaimed because cleanup was attempted.
                    rows.append((stat.st_mtime, stat.st_size, path))
        except OSError as exc:
            logger.warning('[BrowserFileTransfer] staging scan failed: %s', exc)
            return False

        total = sum(size for _mtime, size, _path in rows)
        file_count = len(rows)
        budget = _browser_staging_budget_bytes()
        if (additional_bytes > budget
                or additional_files > MAX_STAGING_ARTIFACTS):
            return False
        for _mtime, size, path in sorted(rows):
            if (total + additional_bytes <= budget
                    and file_count + additional_files
                        <= MAX_STAGING_ARTIFACTS):
                break
            if path in protected:
                continue
            if now - _mtime < STAGING_HANDOFF_GRACE_SECONDS:
                # A receipt must remain useful long enough for the model/user
                # to inspect or materialize it. Under fresh-file pressure,
                # reject new work instead of invalidating a just-returned path.
                continue
            if self._delete_path(path):
                total -= size
                file_count -= 1
        return (
            total + additional_bytes <= budget
            and file_count + additional_files <= MAX_STAGING_ARTIFACTS
        )

    def sweep_expired(self) -> int:
        """Delete stale reconstructible transport state and partial files."""
        now = self._clock()
        with self._lock:
            expired = [
                transfer_id for transfer_id, state in self._transfers.items()
                if now - state.last_activity_at > self.ttl_seconds
            ]
            for transfer_id in expired:
                state = self._transfers.pop(transfer_id)
                self._delete_state_files(state)
        return len(expired)

    def stage_server_response(
        self,
        *,
        owner_user_id,
        source_url: str,
        body: bytes,
        content_type: str = '',
        suggested_filename: str = '',
    ) -> dict:
        """Atomically commit one bounded server-fetched response to staging.

        Direct and browser-authenticated downloads share the same disk/inode
        budget, TTL sweep, free-space reserve and fresh-receipt grace.  This
        method owns no network policy; callers pass bytes already accepted by
        the server fetch owner and an explicit principal for audit/evolution.
        """
        owner = _positive_owner(owner_user_id)
        source = _http_url(source_url, field_name='source_url')
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise BrowserFileTransferError(
                'server_download_invalid_body',
                'Server download body must be bytes',
            )
        payload = bytes(body)
        size_bytes = len(payload)
        if size_bytes > MAX_TRANSFER_BYTES:
            raise BrowserFileTransferError(
                'server_download_too_large',
                f'Server download exceeds the {MAX_TRANSFER_BYTES}-byte hard limit',
                status=413,
            )

        self.sweep_expired()
        with self._lock:
            if not _live_staging_headroom(size_bytes):
                raise BrowserFileTransferError(
                    'server_download_disk_headroom',
                    'Server staging cannot preserve its live free-space reserve',
                    status=503,
                )
            if not self._reserve_staging_capacity(size_bytes, 1):
                raise BrowserFileTransferError(
                    'server_download_staging_capacity',
                    'Server download exceeds the shared staging budget',
                    status=503,
                )
            filename = _safe_server_download_filename(
                source, suggested_filename, content_type)
            final_path = fetched_path(filename)
            partial_path = fetched_path(f'.{filename}.{uuid.uuid4().hex}.part')
            try:
                with open(partial_path, 'xb') as stream:
                    os.chmod(partial_path, 0o600)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(partial_path, final_path)
            except OSError as exc:
                self._delete_path(partial_path)
                raise BrowserFileTransferError(
                    'server_download_storage_error',
                    'Could not commit server download staging file',
                    status=500,
                ) from exc

        digest = hashlib.sha256(payload).hexdigest()
        audit_log(
            'server_download_staged',
            owner_user_id=owner,
            source_domain=(urlsplit(source).hostname or ''),
            size_bytes=size_bytes,
            sha256=digest,
        )
        return {
            'location': 'server_staging',
            'path': final_path,
            'filename': filename,
            'contentType': _bounded_text(content_type, 256),
            'sizeBytes': size_bytes,
            'sha256': digest,
        }

    def create(
        self,
        *,
        owner_user_id,
        client_id: str,
        profile: str,
        source_url: str,
        max_bytes: int,
    ) -> dict:
        owner = _positive_owner(owner_user_id)
        device = str(client_id or '').strip()
        if not device or len(device) > 128:
            raise BrowserFileTransferError(
                'browser_file_transfer_invalid_device',
                'client_id must be a non-empty stable device ID',
            )
        source = _http_url(source_url, field_name='source_url')
        try:
            bounded_max = int(max_bytes)
        except (TypeError, ValueError) as exc:
            raise BrowserFileTransferError(
                'browser_file_transfer_invalid_limit',
                'max_bytes must be an integer',
            ) from exc
        if bounded_max < 1 or bounded_max > MAX_TRANSFER_BYTES:
            raise BrowserFileTransferError(
                'browser_file_transfer_invalid_limit',
                f'max_bytes must be between 1 and {MAX_TRANSFER_BYTES}',
            )

        self.sweep_expired()
        now = self._clock()
        with self._lock:
            if len(self._transfers) >= self.max_active:
                raise BrowserFileTransferError(
                    'browser_file_transfer_capacity',
                    'Browser file-transfer capacity is currently full',
                    status=503,
                )
            owner_count = sum(
                state.owner_user_id == owner
                for state in self._transfers.values()
            )
            if owner_count >= self.max_per_owner:
                raise BrowserFileTransferError(
                    'browser_file_transfer_owner_capacity',
                    'This owner already has the maximum active browser transfers',
                    status=503,
                )
            active_states = [
                state for state in self._transfers.values()
                if state.phase != 'completed'
            ]
            active_reservation = sum(
                state.max_bytes for state in active_states)
            active_growth = sum(
                max(0, state.max_bytes - state.received_bytes)
                for state in active_states
            )
            if not _live_staging_headroom(active_growth + bounded_max):
                raise BrowserFileTransferError(
                    'browser_file_transfer_disk_headroom',
                    'Server staging cannot preserve its live free-space reserve',
                    status=503,
                )
            if not self._reserve_staging_capacity(
                    active_reservation + bounded_max,
                    len(active_states) + 1):
                raise BrowserFileTransferError(
                    'browser_file_transfer_staging_capacity',
                    'Browser file exceeds the current server-staging budget',
                    status=503,
                )
            transfer_id = uuid.uuid4().hex
            token = secrets.token_urlsafe(32)
            part_path = fetched_path(
                f'{_STAGING_PART_PREFIX}{transfer_id}.part')
            self._transfers[transfer_id] = _TransferState(
                transfer_id=transfer_id,
                token=token,
                owner_user_id=owner,
                client_id=device,
                profile=_bounded_text(profile, 80),
                source_url=source,
                max_bytes=bounded_max,
                created_at=now,
                last_activity_at=now,
                part_path=part_path,
            )
        return {
            'transferId': transfer_id,
            'transferToken': token,
            'maxBytes': bounded_max,
            'chunkBytes': MAX_CHUNK_BYTES,
            'expiresInSeconds': self.ttl_seconds,
        }

    def _authorized(
        self,
        transfer_id: str,
        *,
        owner_user_id,
        client_id: str,
        token: str,
    ) -> _TransferState:
        owner = _positive_owner(owner_user_id)
        transfer_id = str(transfer_id or '').strip()
        with self._lock:
            state = self._transfers.get(transfer_id)
            if state is None:
                raise BrowserFileTransferError(
                    'browser_file_transfer_not_found',
                    'Browser file transfer was not found',
                    status=404,
                )
            if self._clock() - state.last_activity_at > self.ttl_seconds:
                self._transfers.pop(transfer_id, None)
                self._delete_state_files(state)
                raise BrowserFileTransferError(
                    'browser_file_transfer_expired',
                    'Browser file transfer expired',
                    status=410,
                )
            token_ok = _token_matches(state.token, token)
            if (state.owner_user_id != owner
                    or state.client_id != str(client_id or '').strip()
                    or not token_ok):
                raise BrowserFileTransferError(
                    'browser_file_transfer_forbidden',
                    'Browser file transfer does not belong to this owner/device',
                    status=403,
                )
            state.last_activity_at = self._clock()
            return state

    @staticmethod
    def _require_read_policy(state: _TransferState, url: str) -> None:
        from lib.browser.access import require_access

        try:
            require_access(
                state.owner_user_id,
                url,
                access='read',
                client_id=state.client_id,
                profile=state.profile,
            )
        except Exception as exc:
            raise BrowserFileTransferError(
                'browser_file_transfer_redirect_denied',
                'Browser file transfer redirect is not allowed by read policy',
                status=403,
            ) from exc

    def start(
        self,
        transfer_id: str,
        *,
        owner_user_id,
        client_id: str,
        token: str,
        final_url: str,
        response_status,
        content_type: str = '',
        content_disposition: str = '',
        content_length=None,
        suggested_filename: str = '',
    ) -> dict:
        with self._lock:
            state = self._authorized(
                transfer_id,
                owner_user_id=owner_user_id,
                client_id=client_id,
                token=token,
            )
            final = _http_url(final_url, field_name='final_url')
            self._require_read_policy(state, state.source_url)
            self._require_read_policy(state, final)
            try:
                status = int(response_status)
            except (TypeError, ValueError) as exc:
                raise BrowserFileTransferError(
                    'browser_file_transfer_invalid_response',
                    'response_status must be an integer',
                ) from exc
            if status < 200 or status >= 300:
                raise BrowserFileTransferError(
                    'browser_file_transfer_upstream_status',
                    f'Browser received upstream HTTP {status}',
                    status=409,
                )
            if status == 206:
                raise BrowserFileTransferError(
                    'browser_file_transfer_partial_response',
                    'Partial upstream responses require an explicit range contract',
                    status=409,
                )
            declared = None
            if content_length not in (None, ''):
                try:
                    declared = int(content_length)
                except (TypeError, ValueError) as exc:
                    raise BrowserFileTransferError(
                        'browser_file_transfer_invalid_length',
                        'content_length must be an integer when present',
                    ) from exc
                if declared < 0:
                    declared = None
                elif declared > state.max_bytes:
                    raise BrowserFileTransferError(
                        'browser_file_transfer_too_large',
                        f'Browser response exceeds the {state.max_bytes}-byte limit',
                        status=413,
                    )

            metadata = (
                final,
                _bounded_text(content_type, 200),
                _bounded_text(content_disposition, 1024),
                _bounded_text(suggested_filename, 240),
                declared,
            )
            if state.phase != 'created':
                existing = (
                    state.final_url,
                    state.content_type,
                    state.content_disposition,
                    state.suggested_filename,
                    state.declared_length,
                )
                if state.phase == 'started' and not state.received_bytes \
                        and existing == metadata:
                    return {
                        'transferId': state.transfer_id,
                        'maxBytes': state.max_bytes,
                        'chunkBytes': MAX_CHUNK_BYTES,
                        'receivedBytes': 0,
                    }
                raise BrowserFileTransferError(
                    'browser_file_transfer_state_conflict',
                    f'Browser file transfer is already {state.phase}',
                    status=409,
                )

            try:
                fd = os.open(
                    state.part_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.close(fd)
            except FileExistsError as exc:
                raise BrowserFileTransferError(
                    'browser_file_transfer_state_conflict',
                    'Browser file transfer staging path already exists',
                    status=409,
                ) from exc
            except OSError as exc:
                raise BrowserFileTransferError(
                    'browser_file_transfer_storage_error',
                    'Could not create browser file-transfer staging file',
                    status=500,
                ) from exc

            (
                state.final_url,
                state.content_type,
                state.content_disposition,
                state.suggested_filename,
                state.declared_length,
            ) = metadata
            state.phase = 'started'
            return {
                'transferId': state.transfer_id,
                'maxBytes': state.max_bytes,
                'chunkBytes': MAX_CHUNK_BYTES,
                'receivedBytes': 0,
            }

    def append_chunk(
        self,
        transfer_id: str,
        sequence: int,
        payload: bytes,
        *,
        owner_user_id,
        client_id: str,
        token: str,
        declared_sha256: str = '',
    ) -> dict:
        body = bytes(payload or b'')
        if not body:
            raise BrowserFileTransferError(
                'browser_file_transfer_empty_chunk',
                'Transfer chunks must not be empty',
            )
        if len(body) > MAX_CHUNK_BYTES:
            raise BrowserFileTransferError(
                'browser_file_transfer_chunk_too_large',
                f'Transfer chunk exceeds the {MAX_CHUNK_BYTES}-byte limit',
                status=413,
            )
        try:
            seq = int(sequence)
        except (TypeError, ValueError) as exc:
            raise BrowserFileTransferError(
                'browser_file_transfer_invalid_sequence',
                'Chunk sequence must be an integer',
            ) from exc
        if seq < 0:
            raise BrowserFileTransferError(
                'browser_file_transfer_invalid_sequence',
                'Chunk sequence must be non-negative',
            )
        digest = hashlib.sha256(body).hexdigest()
        supplied_digest = str(declared_sha256 or '').strip().lower()
        if supplied_digest and (
                not re.fullmatch(r'[0-9a-f]{64}', supplied_digest)
                or not hmac.compare_digest(supplied_digest, digest)):
            raise BrowserFileTransferError(
                'browser_file_transfer_chunk_digest_mismatch',
                'Transfer chunk digest did not match its body',
                status=409,
            )

        with self._lock:
            state = self._authorized(
                transfer_id,
                owner_user_id=owner_user_id,
                client_id=client_id,
                token=token,
            )
            if state.phase != 'started':
                raise BrowserFileTransferError(
                    'browser_file_transfer_state_conflict',
                    f'Cannot append to a transfer in phase {state.phase}',
                    status=409,
                )
            if seq < state.next_sequence:
                prior = state.chunk_receipts.get(seq)
                if prior == (len(body), digest):
                    return {
                        'transferId': state.transfer_id,
                        'acceptedSequence': seq,
                        'nextSequence': state.next_sequence,
                        'receivedBytes': state.received_bytes,
                        'duplicate': True,
                    }
                raise BrowserFileTransferError(
                    'browser_file_transfer_chunk_conflict',
                    'A different body was already accepted for this sequence',
                    status=409,
                )
            if seq != state.next_sequence:
                raise BrowserFileTransferError(
                    'browser_file_transfer_out_of_order',
                    f'Expected chunk {state.next_sequence}, received {seq}',
                    status=409,
                )
            if state.next_sequence:
                previous_size = state.chunk_receipts[
                    state.next_sequence - 1][0]
                if previous_size < MIN_TRANSFER_CHUNK_BYTES:
                    raise BrowserFileTransferError(
                        'browser_file_transfer_short_chunk_not_final',
                        'A sub-minimum transfer chunk must be the final chunk',
                        status=409,
                    )
            if (state.declared_length is not None
                    and state.received_bytes + len(body)
                        < state.declared_length
                    and len(body) < MIN_TRANSFER_CHUNK_BYTES):
                raise BrowserFileTransferError(
                    'browser_file_transfer_chunk_too_small',
                    'Non-final transfer chunks must meet the minimum size',
                    status=409,
                )
            max_chunks = max(
                1,
                (state.max_bytes + MIN_TRANSFER_CHUNK_BYTES - 1)
                // MIN_TRANSFER_CHUNK_BYTES,
            )
            if seq >= max_chunks:
                raise BrowserFileTransferError(
                    'browser_file_transfer_too_many_chunks',
                    'Transfer used more chunks than its bounded byte budget allows',
                    status=413,
                )
            if state.received_bytes + len(body) > state.max_bytes:
                raise BrowserFileTransferError(
                    'browser_file_transfer_too_large',
                    f'Browser response exceeds the {state.max_bytes}-byte limit',
                    status=413,
                )
            if not _live_staging_headroom(len(body)):
                raise BrowserFileTransferError(
                    'browser_file_transfer_disk_headroom',
                    'Server staging cannot preserve its live free-space reserve',
                    status=507,
                )
            try:
                if os.path.getsize(state.part_path) != state.received_bytes:
                    raise OSError('staging size does not match transfer state')
                with open(state.part_path, 'ab') as stream:
                    stream.write(body)
            except OSError as exc:
                raise BrowserFileTransferError(
                    'browser_file_transfer_storage_error',
                    'Could not append browser file-transfer bytes',
                    status=500,
                ) from exc
            state.digest.update(body)
            state.chunk_receipts[seq] = (len(body), digest)
            state.received_bytes += len(body)
            state.next_sequence += 1
            return {
                'transferId': state.transfer_id,
                'acceptedSequence': seq,
                'nextSequence': state.next_sequence,
                'receivedBytes': state.received_bytes,
                'duplicate': False,
            }

    def complete(
        self,
        transfer_id: str,
        *,
        owner_user_id,
        client_id: str,
        token: str,
        total_bytes,
        chunk_count,
    ) -> dict:
        with self._lock:
            state = self._authorized(
                transfer_id,
                owner_user_id=owner_user_id,
                client_id=client_id,
                token=token,
            )
            if state.phase == 'completed' and state.receipt:
                return self._public_receipt(state.receipt)
            if state.phase != 'started':
                raise BrowserFileTransferError(
                    'browser_file_transfer_state_conflict',
                    f'Cannot complete a transfer in phase {state.phase}',
                    status=409,
                )
            try:
                expected_bytes = int(total_bytes)
                expected_chunks = int(chunk_count)
            except (TypeError, ValueError) as exc:
                raise BrowserFileTransferError(
                    'browser_file_transfer_invalid_completion',
                    'total_bytes and chunk_count must be integers',
                ) from exc
            if (expected_bytes != state.received_bytes
                    or expected_chunks != state.next_sequence):
                raise BrowserFileTransferError(
                    'browser_file_transfer_incomplete',
                    'Transfer completion totals do not match accepted chunks',
                    status=409,
                )
            if (state.declared_length is not None
                    and state.declared_length != state.received_bytes):
                raise BrowserFileTransferError(
                    'browser_file_transfer_length_mismatch',
                    'Received bytes do not match upstream Content-Length',
                    status=409,
                )

            # A denial added while a long transfer was in flight wins before
            # the file becomes durable staging.  Redirect authority is checked
            # both at response start and here at the last possible boundary.
            self._require_read_policy(state, state.source_url)
            self._require_read_policy(state, state.final_url)
            filename = _safe_staged_filename(state)
            final_path = fetched_path(filename)
            try:
                # Keep every fallible file preparation step on the hidden
                # partial.  ``os.replace`` is the single visibility boundary:
                # a failed commit remains retryable and can never expose a
                # half-prepared final file.
                if os.path.getsize(state.part_path) != state.received_bytes:
                    raise OSError('staging size does not match transfer state')
                os.chmod(state.part_path, 0o600)
                with open(state.part_path, 'rb') as stream:
                    os.fsync(stream.fileno())
                os.replace(state.part_path, final_path)
            except OSError as exc:
                raise BrowserFileTransferError(
                    'browser_file_transfer_storage_error',
                    'Could not commit browser file-transfer staging file',
                    status=500,
                ) from exc

            receipt = {
                'transferId': state.transfer_id,
                'location': 'server_staging',
                'path': final_path,
                'filename': filename,
                'contentType': state.content_type,
                'isAttachment': bool(re.search(
                    r'(^|;)\s*attachment(?:\s*;|$)',
                    state.content_disposition,
                    re.I,
                )),
                'hasFilename': bool(_content_disposition_filename(
                    state.content_disposition)),
                'sizeBytes': state.received_bytes,
                'sha256': state.digest.hexdigest(),
            }
            state.receipt = receipt
            state.phase = 'completed'
            state.last_activity_at = self._clock()
            audit_log(
                'browser_file_transfer_completed',
                owner_user_id=state.owner_user_id,
                browser_client=state.client_id,
                source_domain=(urlsplit(state.source_url).hostname or ''),
                final_domain=(urlsplit(state.final_url).hostname or ''),
                size_bytes=state.received_bytes,
                sha256=receipt['sha256'],
            )
            return self._public_receipt(receipt)

    @staticmethod
    def _public_receipt(receipt: dict) -> dict:
        """Return only stream confirmation; all metadata stays server-side."""
        return {
            key: receipt[key]
            for key in (
                'transferId', 'location', 'sizeBytes', 'sha256',
            )
            if key in receipt
        }

    def consume_completed(
        self,
        transfer_id: str,
        *,
        owner_user_id,
        client_id: str,
    ) -> dict:
        owner = _positive_owner(owner_user_id)
        with self._lock:
            state = self._transfers.get(str(transfer_id or ''))
            if state is None:
                raise BrowserFileTransferError(
                    'browser_file_transfer_not_found',
                    'Browser file transfer was not found',
                    status=404,
                )
            if (state.owner_user_id != owner
                    or state.client_id != str(client_id or '').strip()):
                raise BrowserFileTransferError(
                    'browser_file_transfer_forbidden',
                    'Browser file transfer does not belong to this owner/device',
                    status=403,
                )
            if state.phase != 'completed' or not state.receipt:
                raise BrowserFileTransferError(
                    'browser_file_transfer_incomplete',
                    'Browser file transfer did not complete',
                    status=409,
                )
            try:
                self._require_read_policy(state, state.source_url)
                self._require_read_policy(state, state.final_url)
            except BrowserFileTransferError:
                self._transfers.pop(state.transfer_id, None)
                self._delete_state_files(state)
                raise
            try:
                handoff_at = self._clock()
                os.utime(
                    str(state.receipt.get('path') or ''),
                    (handoff_at, handoff_at),
                    follow_symlinks=False,
                )
            except OSError as exc:
                logger.debug('[BrowserFileTransfer] could not refresh handoff '
                             'retention timestamp (errno=%s)',
                             getattr(exc, 'errno', None))
            self._transfers.pop(state.transfer_id, None)
            return dict(state.receipt)

    def abort(
        self,
        transfer_id: str,
        *,
        owner_user_id,
        client_id: str,
        token: str | None = None,
        internal: bool = False,
    ) -> bool:
        owner = _positive_owner(owner_user_id)
        with self._lock:
            state = self._transfers.get(str(transfer_id or ''))
            if state is None:
                return False
            if (state.owner_user_id != owner
                    or state.client_id != str(client_id or '').strip()
                    or (not internal and (
                        not _token_matches(state.token, token)))):
                raise BrowserFileTransferError(
                    'browser_file_transfer_forbidden',
                    'Browser file transfer does not belong to this owner/device',
                    status=403,
                )
            self._transfers.pop(state.transfer_id, None)
            self._delete_state_files(state)
            return True

    def clear_for_tests(self) -> None:
        """Remove all state/files; intentionally explicit for focused tests."""
        with self._lock:
            states = list(self._transfers.values())
            self._transfers.clear()
            for state in states:
                self._delete_state_files(state)


file_transfer_store = BrowserFileTransferStore()


def fetch_file_via_browser(
    url: str,
    *,
    max_bytes: int,
    timeout: int,
    client_id: str,
    owner_user_id,
) -> dict:
    """Stream one authenticated browser response into server staging.

    Returns a server-authored receipt.  Transport/protocol/policy failures are
    raised so host adapters can report or intentionally degrade them.
    """
    from lib.browser.access import require_access
    from lib.browser.protocol import BrowserCapability, require_capabilities
    from lib.browser.queue import is_extension_connected, send_browser_command

    owner = _positive_owner(owner_user_id)
    device = str(client_id or '').strip()
    source = _http_url(url, field_name='url')
    if not is_extension_connected(device, owner_user_id=owner):
        raise BrowserFileTransferError(
            'browser_file_transfer_offline',
            'The selected browser extension is not connected for this user',
            status=503,
        )
    info = require_capabilities(device, [BrowserCapability.FILE_EXPORT])
    require_access(
        owner,
        source,
        access='read',
        client_id=device,
        profile=info.get('profile', ''),
    )
    transfer = file_transfer_store.create(
        owner_user_id=owner,
        client_id=device,
        profile=info.get('profile', ''),
        source_url=source,
        max_bytes=max_bytes,
    )
    transfer_id = transfer['transferId']
    consumed = False
    try:
        command_timeout = max(35, min(120, int(timeout or 35)))
        result, error = send_browser_command(
            'fetch_file_to_server',
            {
                'url': source,
                'transferId': transfer_id,
                'transferToken': transfer['transferToken'],
                'maxBytes': transfer['maxBytes'],
                'chunkBytes': transfer['chunkBytes'],
                # Leave five seconds for command-result settlement after the
                # extension's own fetch/stream deadline.
                'timeoutMs': (command_timeout - 5) * 1000,
            },
            timeout=command_timeout,
            client_id=device,
            owner_user_id=owner,
        )
        if error:
            raise BrowserFileTransferError(
                'browser_file_transfer_command_failed',
                f'Browser file transfer failed: {text_for_log(error)}',
                status=502,
            )
        if not isinstance(result, dict):
            raise BrowserFileTransferError(
                'browser_file_transfer_invalid_receipt',
                'Browser returned an invalid file-transfer receipt',
                status=502,
            )
        if (str(result.get('transferId') or '') != transfer_id
                or result.get('location') != 'server_staging'):
            raise BrowserFileTransferError(
                'browser_file_transfer_invalid_receipt',
                'Browser returned a mismatched file-transfer receipt',
                status=502,
            )
        receipt = file_transfer_store.consume_completed(
            transfer_id,
            owner_user_id=owner,
            client_id=device,
        )
        consumed = True
        return receipt
    finally:
        if not consumed:
            file_transfer_store.abort(
                transfer_id,
                owner_user_id=owner,
                client_id=device,
                internal=True,
            )


__all__ = [
    'BrowserFileTransferError', 'BrowserFileTransferStore',
    'TRANSFER_TTL_SECONDS', 'MAX_ACTIVE_TRANSFERS',
    'MAX_ACTIVE_TRANSFERS_PER_OWNER', 'MAX_TRANSFER_BYTES',
    'MAX_CHUNK_BYTES', 'MIN_TRANSFER_CHUNK_BYTES',
    'MAX_STAGING_ARTIFACTS', 'STAGING_HANDOFF_GRACE_SECONDS',
    'STAGING_FILENAME_PREFIX',
    'SERVER_DOWNLOAD_FILENAME_PREFIX',
    'file_transfer_store',
    'fetch_file_via_browser',
]
