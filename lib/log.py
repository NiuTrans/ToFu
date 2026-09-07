"""lib/log.py — Centralized logging for Tofu.

Usage in any module:
    from lib.log import get_logger, log_exception, audit_log, log_context
    logger = get_logger(__name__)

    logger.info('Normal operation')
    logger.warning('Something unexpected')
    logger.error('Failed to do X', exc_info=True)   # includes traceback
    logger.exception('Caught error')                  # shorthand for error+exc_info

    # Convenient shorthand for error + traceback
    log_exception(logger, 'Something went wrong')

    # Structured audit logging for critical events
    audit_log('user_login', user='admin', ip='1.2.3.4')

    # Context manager that logs start/end/duration/exception
    with log_context('heavy_computation'):
        do_work()

    # Decorator for route handlers — auto-logs entry, exit, status, duration
    @log_route(logger)
    def my_endpoint():
        ...

    # Context manager for external calls (APIs, DB, etc.)
    with log_external(logger, 'eastmoney_api', url='https://...'):
        resp = requests.get(...)

    # Get current request ID (for correlating logs across modules)
    rid = req_id()  # e.g. 'a3f7' — short hex, set per HTTP request

Log file layout:
    logs/app.log     — Business logic (lib.*, routes.*, server)  INFO+
                       Size + daily rotation with a family budget.
    logs/access.log  — HTTP request log (werkzeug)  INFO+
                       Bounded rotation. Noisy success polls are filtered.
    logs/error.log   — All WARNING/ERROR/CRITICAL from every source
                       High-priority bounded evidence.
    logs/vendor.log  — Third-party libraries, WARNING+ only
                       Lower-priority bounded evidence.
    logs/incident.jsonl — Compact WARNING+ fingerprint/correlation index.
    logs/audit.log   — Bounded structured JSON audit trail.

Exact stream ceilings and retention are defined once in ``lib/log_policy.py``.
"""

import asyncio as _asyncio
import atexit as _atexit
import functools
import json
import logging
import os
import queue as _queue_mod
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from threading import Lock as _Lock
from threading import Thread as _Thread

from lib.log_redaction import sanitize_value

# ── Base directory and log paths ──
# LOG_DIR must be WRITABLE. In a frozen desktop build BASE_DIR resolves inside
# the read-only _internal/ bundle (under Program Files), so we redirect to a
# writable root. Kept inline (not via lib/runtime_paths) to avoid an import
# cycle — runtime_paths imports lib.log. The logic mirrors runtime_paths.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _per_user_base() -> str:
    """Per-user, guaranteed-writable base dir. Byte-for-byte twin of
    ``lib/runtime_paths._per_user_root``."""
    if sys.platform.startswith('win'):
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'Tofu')
    if sys.platform == 'darwin':
        return os.path.join(os.path.expanduser('~'), 'Library',
                            'Application Support', 'Tofu')
    xdg = os.environ.get('XDG_DATA_HOME') or os.path.join(
        os.path.expanduser('~'), '.local', 'share')
    return os.path.join(xdg, 'Tofu')


def _writable_base_dir() -> str:
    """Resolve the writable BASE dir that holds both logs/ and data/.

    This is a byte-for-byte twin of ``lib/runtime_paths._resolve_base`` (kept
    inline because runtime_paths imports lib.log — a cycle). CRITICAL: the
    frozen-fallback decision probes the SHARED base dir (``<exe_dir>``), NOT a
    ``…/logs`` subdir, so this reaches the SAME verdict runtime_paths does for
    ``data/``. Probing different subdirs could split logs and data to different
    roots on a partially-writable install. tests/test_desktop_install_paths.py
    pins the two twins to agree.
    """
    explicit = os.environ.get('TOFU_DATA_DIR')
    if explicit:
        explicit = os.path.abspath(explicit)
        return os.path.dirname(explicit) if os.path.basename(explicit) == 'data' else explicit
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        try:
            os.makedirs(exe_dir, exist_ok=True)
            probe = os.path.join(exe_dir, '.tofu_write_probe')
            with open(probe, 'w'):
                pass
            os.remove(probe)
            return exe_dir
        except OSError as e:
            logging.getLogger('lib.log').debug(
                '[log] exe-dir %s not writable (%s) — using per-user base', exe_dir, e)
            return _per_user_base()
    # Source checkout — mirror runtime_paths._source_checkout_base() exactly:
    # keep user state OUT of the code tree by default (fresh clone → per-user),
    # but keep an existing populated in-tree data/ where it is (zero migration).
    layout = (os.environ.get('TOFU_DATA_LAYOUT') or 'auto').strip().lower()
    if layout == 'intree':
        return BASE_DIR
    if layout == 'xdg':
        return _per_user_base()
    if layout == 'auto':
        data_dir = os.path.join(BASE_DIR, 'data')
        try:
            with os.scandir(data_dir) as it:
                populated = any(True for _ in it)
        except OSError as e:
            logging.getLogger('lib.log').debug(
                '[log] scandir(%s) failed (%s) — treating as unpopulated', data_dir, e)
            populated = False
        return BASE_DIR if populated else _per_user_base()
    # Unknown value → treat as auto's fresh-clone default (per-user).
    data_dir = os.path.join(BASE_DIR, 'data')
    try:
        with os.scandir(data_dir) as it:
            populated = any(True for _ in it)
    except OSError as e:
        logging.getLogger('lib.log').debug(
            '[log] scandir(%s) failed (%s) — treating as unpopulated', data_dir, e)
        populated = False
    return BASE_DIR if populated else _per_user_base()


def _writable_logs_dir() -> str:
    return os.path.join(_writable_base_dir(), 'logs')


LOG_DIR = _writable_logs_dir()

# Primary log files
APP_LOG = os.path.join(LOG_DIR, 'app.log')
ACCESS_LOG = os.path.join(LOG_DIR, 'access.log')
ERROR_LOG = os.path.join(LOG_DIR, 'error.log')
VENDOR_LOG = os.path.join(LOG_DIR, 'vendor.log')
AUDIT_LOG_FILE = os.path.join(LOG_DIR, 'audit.log')
INCIDENT_LOG = os.path.join(LOG_DIR, 'incident.jsonl')

# ══════════════════════════════════════════
#  Request ID — per-request correlation
# ══════════════════════════════════════════

_request_id_var: ContextVar[str] = ContextVar('tofu_request_id', default='')


def _bounded_context_scalar(value: object, max_chars: int) -> str:
    """Return one queue/log-safe identifier without multiline injection."""
    try:
        text = str(value or '').encode('utf-8', 'replace').decode('utf-8')
    except Exception:
        return '<invalid-context-value>'[:max_chars]
    escaped = ''.join(
        char if ord(char) >= 32 and ord(char) != 127
        else '\\x%02x' % ord(char)
        for char in text
    )
    return escaped[:max(1, int(max_chars))]


def set_req_id(rid: str = None) -> str:
    """Set a request ID for the current execution context.

    Args:
        rid: Explicit request ID. If None, generates a short hex UUID.

    Returns:
        The request ID that was set.
    """
    if rid is None:
        rid = uuid.uuid4().hex[:8]
    else:
        rid = _bounded_context_scalar(rid, 64)
    _request_id_var.set(rid)
    return rid


def req_id() -> str:
    """Get the current request ID for this coroutine/thread context.

    Returns empty string if not in a request context (e.g. background threads).
    """
    return _request_id_var.get()


# ══════════════════════════════════════════
#  Principal — per-request authenticated identity
# ══════════════════════════════════════════

_principal_var: ContextVar = ContextVar('tofu_principal', default=None)
_log_fields_var: ContextVar = ContextVar('tofu_log_fields', default=None)


def set_principal(key_id: str = '', user_id: object = '') -> None:
    """Bind the authenticated principal to the current execution context.

    Mirrors the request-id ContextVar pattern above: the auth middleware
    (``routes/api_v1/auth.py``) and the push-WS handshake (``routes/push.py``)
    call this once per connection so ``audit_log`` attaches ``key_id`` /
    ``user_id`` automatically (docs/ENTERPRISE_READINESS_AUDIT.md, R11) —
    event callers no longer need to remember identity by hand.
    """
    _principal_var.set((_bounded_context_scalar(key_id, 128),
                        _bounded_context_scalar(user_id, 128)))


def principal() -> tuple:
    """Return ``(key_id, user_id)`` for this context, else ``('', '')``."""
    value = _principal_var.get()
    return value if value is not None else ('', '')


def log_fields() -> dict:
    """Return a copy of structured fields bound to this execution context."""
    value = _log_fields_var.get()
    return dict(value) if isinstance(value, dict) else {}


def set_log_context(**fields) -> None:
    """Replace ambient correlation fields for a long-lived worker lane."""
    _log_fields_var.set({
        str(key): value for key, value in fields.items()
        if value not in (None, '')
    })


def clear_log_context() -> None:
    """Clear worker correlation before a pooled thread is reused."""
    _log_fields_var.set(None)


@contextmanager
def bind_log_context(**fields):
    """Temporarily add structured correlation fields to every emitted record.

    This is the evolution seam for background work that has no HTTP request
    context.  Task/turn launchers can bind ``conversation_id`` / ``task_id`` /
    ``trace_id`` once instead of interpolating them into every message.
    """
    merged = log_fields()
    merged.update({str(key): value for key, value in fields.items()
                   if value not in (None, '')})
    token = _log_fields_var.set(merged)
    try:
        yield merged
    finally:
        _log_fields_var.reset(token)


class LogContextFilter(logging.Filter):
    """Stamp ContextVar identity before a record crosses the async queue."""

    def filter(self, record: logging.LogRecord) -> bool:
        rid = req_id()
        key_id, user_id = principal()
        ambient = log_fields()
        record.tofu_request_id = _bounded_context_scalar(
            getattr(record, 'tofu_request_id', '') or rid or '', 64)
        record.tofu_key_id = _bounded_context_scalar(
            getattr(record, 'tofu_key_id', '') or key_id or '', 128)
        record.tofu_user_id = _bounded_context_scalar(
            getattr(record, 'tofu_user_id', '') or user_id or
            ambient.get('user_id') or '', 128)
        explicit_fields = getattr(record, 'tofu_event_fields', None)
        try:
            merged_fields = dict(ambient)
            if isinstance(explicit_fields, dict):
                merged_fields.update(explicit_fields)
            safe_fields = sanitize_value(
                merged_fields, field_name='event_fields', max_items=30,
                max_string_chars=600)
        except Exception:
            # Logging context is diagnostic metadata. A hostile ``extra``
            # mapping must never make the business operation itself fail.
            safe_fields = {'<context-unavailable>': True}
        record.tofu_event_fields = (
            safe_fields if isinstance(safe_fields, dict) else {})
        record.tofu_event_name = _bounded_context_scalar(
            getattr(record, 'tofu_event_name', '') or '', 128)
        if not hasattr(record, 'tofu_coalesce_note'):
            record.tofu_coalesce_note = ''
        else:
            record.tofu_coalesce_note = _bounded_context_scalar(
                record.tofu_coalesce_note, 256)

        prefix = ''
        if record.tofu_request_id:
            try:
                message = record.getMessage()
            except Exception:
                message = str(record.msg)
            rid_token = record.tofu_request_id
            if (f'[rid:{rid_token}]' not in message
                    and f'[{rid_token}]' not in message
                    and f'rid={rid_token}' not in message):
                prefix = f'[rid:{rid_token}] '
        record.tofu_correlation_prefix = prefix
        return True


def _rid_prefix() -> str:
    """Return '[rid:XXXX] ' prefix if request ID is set, else ''."""
    rid = req_id()
    return f'[rid:{rid}] ' if rid else ''


# ── Inbound (client-supplied) correlation ids ──
# A client may supply the id so its own logs join the server's. Two channels
# exist because not every transport can set a header: `fetch` sends
# `X-Request-ID` (static/js/api.js), while a browser `WebSocket` handshake
# CANNOT set custom headers and must pass it as the `_rid` query param
# (static/js/push.js). Both are honored through the ONE resolver below so the
# id space is identical across transports and the validation cannot drift.
#
# Character-set check rather than a regex: this runs in before_request on EVERY
# request, and a frozenset lookup says the same thing more cheaply.
_RID_ALPHABET = frozenset(
    'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-')
_RID_MAX_LEN = 64


def rid_is_safe(value) -> bool:
    """True when ``value`` is a short, log-safe correlation token.

    Client input reaches a log line and an ``X-Request-ID`` response header, so
    an unvalidated value could inject newlines (forging log records) or bloat
    every line for a request.
    """
    if not value or not isinstance(value, str) or len(value) > _RID_MAX_LEN:
        return False
    return all(ch in _RID_ALPHABET for ch in value)


def resolve_inbound_rid(header_val=None, query_val=None) -> str:
    """Resolve a request's correlation id: the client's, else a fresh one.

    The header is the primary channel; ``query_val`` is the fallback for
    transports that cannot set one (a browser WebSocket handshake).

    An unsafe id is REJECTED in favour of a server-minted one rather than
    sanitized: silently rewriting it would hand the client back an id it never
    sent, so the id it quotes in a bug report would appear nowhere in the logs
    — strictly worse than obviously ignoring it.
    """
    for candidate in (header_val, query_val):
        if rid_is_safe(candidate):
            return candidate
    return uuid.uuid4().hex[:12]


# ══════════════════════════════════════════
#  Core Logger
# ══════════════════════════════════════════

def get_logger(name: str) -> logging.Logger:
    """Get a named logger for a module.

    Args:
        name: Usually __name__, e.g. 'lib.llm' or 'routes.chat'.

    Returns:
        A logging.Logger instance that inherits root config from server.py.
    """
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, event: str,
              message: str = '', *args, **fields) -> None:
    """Emit a human message plus stable machine-readable event metadata."""
    event_name = str(event or '').strip()[:128]
    if not event_name:
        raise ValueError('event name is required')
    logging_kwargs = {
        key: fields.pop(key) for key in ('exc_info', 'stack_info', 'stacklevel')
        if key in fields
    }
    logger.log(
        level,
        message or '[event:%s]',
        *(args if message else (event_name,)),
        extra={'tofu_event_name': event_name,
               'tofu_event_fields': dict(fields)},
        **logging_kwargs,
    )


# ══════════════════════════════════════════
#  Exception Logging
# ══════════════════════════════════════════

def log_exception(logger: logging.Logger, msg: str, *args, **kwargs) -> None:
    """Convenient shorthand: log an error message with full traceback.

    Equivalent to logger.error(msg, exc_info=True) but shorter to type
    and makes exception-logging intent explicit in code reviews.

    Automatically prepends request ID if available.

    Args:
        logger: The logger instance to use.
        msg: Error message (may contain %-style format placeholders).
        *args: Format arguments for the message.
        **kwargs: Additional keyword arguments passed to logger.error().
    """
    kwargs['exc_info'] = True
    prefix = _rid_prefix()
    logger.error('%s' + msg, prefix, *args, **kwargs)


# ══════════════════════════════════════════
#  Audit Logging
# ══════════════════════════════════════════

_audit_lock = _Lock()

# ── Non-blocking audit writes ──
# audit.log lives on a FUSE/NFS mount next to the other logs. The old
# implementation opened/appended the file (plus a per-call os.makedirs)
# synchronously under _audit_lock on the CALLER's thread — including async
# handlers on the event loop, where a hung mount froze every request. The
# caller now only serialises the entry (pure CPU) and enqueues the line; a
# dedicated daemon writer thread performs the actual disk I/O, mirroring the
# QueueHandler/QueueListener setup in server.py. A FUSE hang blocks only the
# writer thread. The queue is bounded as well: otherwise a long mount outage
# turns the isolation mechanism into an unbounded-memory/OOM path. On
# overload the oldest queued line is discarded in favour of the newest one.
# Trade-off (owner-approved): entries still queued at a SIGKILL are lost; a
# bounded atexit drain preserves them on clean shutdown.
_audit_queue = None          # queue.Queue[str], created on first async write
_audit_writer_thread = None
_audit_overflow_lock = _Lock()
_audit_dropped = 0
_audit_last_drop_report_mono = 0.0


def _audit_queue_capacity() -> int:
    """Return the bounded async-audit queue capacity.

    A few thousand compact JSON records absorb normal write bursts while
    keeping the worst-case memory footprint finite during a filesystem stall.
    """
    try:
        capacity = int(os.environ.get('TOFU_AUDIT_LOG_QUEUE_MAX', '') or '4096')
    except (TypeError, ValueError) as e:
        logging.getLogger('lib.log').debug(
            'invalid TOFU_AUDIT_LOG_QUEUE_MAX; using 4096: %s', e)
        capacity = 4096
    return max(128, min(100_000, capacity))


def _audit_rotation_limits():
    """Return bounded, dependency-free audit-log rotation settings."""
    from lib.log_policy import stream_backup_count, stream_max_bytes
    return stream_max_bytes('audit'), stream_backup_count('audit')


def _rotate_audit_log_if_needed(incoming_bytes: int) -> None:
    """Size-rotate ``audit.log`` before one append; caller serializes writes."""
    max_bytes, backups = _audit_rotation_limits()
    try:
        from lib.log_retention import copytruncate_if_oversize
        copytruncate_if_oversize(
            AUDIT_LOG_FILE,
            max_bytes=max_bytes,
            trigger_bytes=max(1, max_bytes - max(0, int(incoming_bytes))),
            backup_count=backups)
    except OSError as exc:
        logging.getLogger('lib.log').debug(
            '[audit] size rotation failed: %s', exc)


def _audit_sync_writes() -> bool:
    """Keep audit writes synchronous under pytest (mirrors server.py's
    _LOG_UNDER_PYTEST): tests assert on file state right after the call and
    would race a background writer."""
    return bool(os.environ.get('PYTEST_CURRENT_TEST')) or ('pytest' in sys.modules)


def _audit_write_line(line: str) -> None:
    """Append one JSON line to the audit file (the actual disk I/O). Reads
    the module-level paths at call time so tests can monkeypatch them."""
    from lib.log_retention import (
        append_bytes_locked, ensure_private_log_directory,
    )
    ensure_private_log_directory(LOG_DIR)
    payload = line.encode('utf-8', errors='replace')
    _rotate_audit_log_if_needed(len(payload))
    append_bytes_locked(AUDIT_LOG_FILE, payload)


def _audit_writer_loop(q: '_queue_mod.Queue') -> None:
    while True:
        line = q.get()
        try:
            if line is None:  # shutdown sentinel
                return
            _audit_write_line(line)
        except Exception:
            logging.getLogger('audit').error(
                'Failed to write audit log record (%d bytes)',
                len(line.encode('utf-8', errors='replace')), exc_info=True,
                extra={
                    'tofu_event_name': 'logging.audit_write_failed',
                    'tofu_event_fields': {
                        'record_bytes': len(line.encode(
                            'utf-8', errors='replace')),
                    },
                })
        finally:
            q.task_done()


def _ensure_audit_writer():
    """Lazily start the singleton audit writer thread; returns the queue."""
    global _audit_queue, _audit_writer_thread
    if _audit_queue is not None:
        return _audit_queue
    with _audit_lock:
        if _audit_queue is None:
            q = _queue_mod.Queue(maxsize=_audit_queue_capacity())
            t = _Thread(target=_audit_writer_loop, args=(q,),
                        name='audit-log-writer', daemon=True)
            t.start()
            _audit_queue = q
            _audit_writer_thread = t
    return _audit_queue


def _enqueue_audit_line(q: '_queue_mod.Queue', line: str) -> None:
    """Enqueue without blocking and keep memory bounded under disk stalls.

    Prefer the newest audit evidence when the queue is saturated. Races with
    the consumer or another producer are intentionally tolerated: the caller
    must never wait for a slow diagnostics path.
    """
    global _audit_dropped, _audit_last_drop_report_mono
    try:
        q.put_nowait(line)
        return
    except _queue_mod.Full:
        pass

    dropped = False
    try:
        q.get_nowait()
        q.task_done()
        dropped = True
    except _queue_mod.Empty:
        # The writer drained the queue between Full and get_nowait().
        pass

    try:
        q.put_nowait(line)
    except _queue_mod.Full:
        # Another producer won the newly available slot. Keep the caller
        # non-blocking; this line becomes the discarded one.
        dropped = True

    if not dropped:
        return

    # Rate-limit the overload report without ever making the audit caller
    # wait for another producer. The main logging path has its own bounded
    # queue, so this warning cannot recreate the same memory failure mode.
    if not _audit_overflow_lock.acquire(blocking=False):
        return
    try:
        _audit_dropped += 1
        now = time.monotonic()
        if now - _audit_last_drop_report_mono >= 60.0:
            logging.getLogger('audit').warning(
                'Audit log queue saturated; dropped %d queued record(s) '
                'while preserving recent events', _audit_dropped)
            _audit_dropped = 0
            _audit_last_drop_report_mono = now
    finally:
        _audit_overflow_lock.release()


@_atexit.register
def _drain_audit_queue(timeout: float = 5.0) -> None:
    """Bounded wait for queued audit lines at interpreter exit. Bounded
    because the writer may be stuck on a hung mount — a clean exit must not
    be held hostage by the very stall this design isolates."""
    q = _audit_queue
    if q is None:
        return
    deadline = time.monotonic() + timeout
    while q.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.05)


def audit_log(event: str, **details) -> None:
    """Write a structured JSON entry to the separate audit log file.

    Use this for critical events that need to be easily grep-able and
    machine-parseable: user actions, security events, config changes, etc.

    Non-blocking: serialises the entry and enqueues it; a dedicated writer
    thread appends to the file, so concurrent requests never interleave
    partial JSON lines and a slow mount never stalls the caller.

    Args:
        event: Event name, e.g. 'user_login', 'model_switch', 'config_change'.
        **details: Arbitrary key-value pairs to include in the audit entry.
    """
    rid = req_id()
    from lib.log_redaction import redact_text, sanitize_value

    safe_details = sanitize_value(
        details, field_name='audit_details', max_items=50,
        max_string_chars=2_000)
    entry = {
        **safe_details,
        # Caller details may enrich an audit event but never forge its
        # authoritative clock or event identity.
        'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        'event': redact_text(event, max_chars=128)[:128],
    }
    if rid:
        entry['request_id'] = redact_text(rid, max_chars=128)[:64]
    # Auto-attach the authenticated principal (ENTERPRISE_READINESS_AUDIT
    # R11): explicit caller-supplied key_id/user_id always win, and empty
    # synthetic identities attach nothing.
    if 'key_id' not in entry or 'user_id' not in entry:
        p_key_id, p_user_id = principal()
        ambient = log_fields()
        if not p_key_id:
            p_key_id = _bounded_context_scalar(ambient.get('key_id') or '', 128)
        if not p_user_id:
            p_user_id = _bounded_context_scalar(ambient.get('user_id') or '', 128)
        if p_key_id and 'key_id' not in entry:
            entry['key_id'] = redact_text(
                p_key_id, max_chars=128)[:128]
        if p_user_id and 'user_id' not in entry:
            entry['user_id'] = redact_text(
                p_user_id, max_chars=128)[:128]
    line = json.dumps(entry, ensure_ascii=False, default=str,
                      separators=(',', ':')) + '\n'
    # Keep every physical JSONL row bounded and valid.  If a caller supplied a
    # giant diagnostic blob, retain identity + field names rather than slicing
    # serialized JSON into an unparsable fragment.
    max_line_bytes = 32 * 1024
    encoded_size = len(line.encode('utf-8', errors='replace'))
    if encoded_size > max_line_bytes:
        core_keys = ('timestamp', 'event', 'request_id', 'key_id', 'user_id')
        compact = {key: entry[key] for key in core_keys if key in entry}
        compact['details_truncated'] = True
        compact['detail_keys'] = [
            str(key)[:128] for key in entry if key not in core_keys][:100]
        compact['original_bytes'] = encoded_size
        line = json.dumps(compact, ensure_ascii=False, default=str,
                          separators=(',', ':')) + '\n'
    if _audit_sync_writes():
        try:
            with _audit_lock:
                _audit_write_line(line)
        except Exception:
            # Fall back to standard logging if audit file write fails
            logging.getLogger('audit').error(
                'Failed to write audit log: %s', json.dumps(entry, default=str),
                exc_info=True
            )
        return
    _enqueue_audit_line(_ensure_audit_writer(), line)


# ══════════════════════════════════════════
#  Operation Context Manager
# ══════════════════════════════════════════

@contextmanager
def log_context(operation_name: str, logger: logging.Logger = None, level: int = logging.INFO):
    """Context manager that automatically logs start/end/duration/exception of a block.

    Usage:
        with log_context('rebuild_index'):
            do_expensive_work()

    Logs:
        - INFO on entry:  "[op:rebuild_index] started"
        - INFO on exit:   "[op:rebuild_index] completed in 1.234s"
        - ERROR on error: "[op:rebuild_index] failed after 0.567s: <exception>"

    Args:
        operation_name: Human-readable name of the operation.
        logger: Logger to use. Defaults to the 'lib.log' logger.
        level: Log level for start/completion messages (default INFO).
    """
    if logger is None:
        logger = logging.getLogger('lib.log')

    prefix = _rid_prefix()
    logger.log(level, '%s[op:%s] started', prefix, operation_name)
    start = time.monotonic()
    try:
        yield
    except Exception as exc:
        elapsed = time.monotonic() - start
        logger.error(
            '%s[op:%s] FAILED after %.3fs: %s',
            prefix, operation_name, elapsed, exc,
            exc_info=True)

        raise
    else:
        elapsed = time.monotonic() - start
        logger.log(level, '%s[op:%s] completed in %.3fs', prefix, operation_name, elapsed)


# ══════════════════════════════════════════
#  Route Logging Decorator
# ══════════════════════════════════════════

def log_route(logger: logging.Logger, log_request_body: bool = False,
              log_response_body: bool = False,
              sensitive_fields: tuple = ('password', 'token', 'secret')):
    """Decorator that auto-logs route handler entry, exit, status code, and duration.

    Usage:
        @app.route('/api/foo', methods=['POST'])
        @log_route(logger)
        def foo_handler():
            ...

    Logs:
        → [Route] POST /api/foo — entry
        ← [Route] POST /api/foo — 200 OK in 0.045s

    On error:
        ✗ [Route] POST /api/foo — 500 in 0.123s: ValueError('bad input')

    Args:
        logger: Logger instance for this module.
        log_request_body: If True, log request JSON body at DEBUG level (redacted).
        log_response_body: If True, log response body at DEBUG level (truncated).
        sensitive_fields: Field names to redact from logged request bodies.
    """
    def _entry():
        """Resolve request context + log entry; return (method, path, rid_tag)."""
        try:
            from quart import request as flask_req
            method = flask_req.method
            path = flask_req.path
            rid = req_id()
            rid_tag = f'[rid:{rid}] ' if rid else ''
        except RuntimeError as e:
            # Expected when called outside Flask request context
            logger.debug('log_route outside request context: %s', e, exc_info=True)
            method, path, rid_tag = '?', '?', ''

        logger.debug('%s→ [Route] %s %s', rid_tag, method, path)

        if log_request_body:
            try:
                from quart import request as flask_req
                body = flask_req.get_json(silent=True)
                if body and isinstance(body, dict):
                    safe_body = {k: ('***' if k in sensitive_fields else v) for k, v in body.items()}
                    logger.debug('%s  Request body: %s', rid_tag, json.dumps(safe_body, ensure_ascii=False, default=str)[:2000])
            except Exception:
                logger.debug('Failed to log request body', exc_info=True)

        return method, path, rid_tag

    def _log_result(result, elapsed, method, path, rid_tag):
        status = 200
        if hasattr(result, 'status_code'):
            status = result.status_code
        elif isinstance(result, tuple) and len(result) >= 2:
            status = result[1]

        if status >= 500:
            logger.error('%s✗ [Route] %s %s — %d in %.3fs',
                        rid_tag, method, path, status, elapsed)
        elif status >= 400:
            logger.warning('%s⚠ [Route] %s %s — %d in %.3fs',
                          rid_tag, method, path, status, elapsed)
        else:
            logger.debug('%s← [Route] %s %s — %d in %.3fs',
                       rid_tag, method, path, status, elapsed)

        if log_response_body:
            try:
                resp_data = result.get_data(as_text=True) if hasattr(result, 'get_data') else str(result)
                logger.debug('%s  Response body: %.2000s', rid_tag, resp_data)
            except Exception:
                logger.debug('Failed to log response body', exc_info=True)

    def decorator(fn):
        # Dual-mode: async handlers MUST stay coroutine functions so Quart
        # awaits them; we await the result before extracting its status.
        if _asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                method, path, rid_tag = _entry()
                start = time.monotonic()
                try:
                    result = await fn(*args, **kwargs)
                    _log_result(result, time.monotonic() - start, method, path, rid_tag)
                    return result
                except Exception as exc:
                    elapsed = time.monotonic() - start
                    logger.error('%s✗ [Route] %s %s — EXCEPTION after %.3fs: %s',
                                rid_tag, method, path, elapsed, exc, exc_info=True)
                    raise
            return awrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            method, path, rid_tag = _entry()
            start = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                _log_result(result, time.monotonic() - start, method, path, rid_tag)
                return result
            except Exception as exc:
                elapsed = time.monotonic() - start
                logger.error('%s✗ [Route] %s %s — EXCEPTION after %.3fs: %s',
                            rid_tag, method, path, elapsed, exc, exc_info=True)
                raise

        return wrapper
    return decorator


# ══════════════════════════════════════════
#  External Call Context Manager
# ══════════════════════════════════════════

@contextmanager
def log_external(logger: logging.Logger, service_name: str, **context):
    """Context manager for logging external API/service calls with timing.

    Usage:
        with log_external(logger, 'eastmoney_api', symbol='000001'):
            resp = requests.get(...)

    Logs:
        [ext:eastmoney_api] calling (symbol=000001)
        [ext:eastmoney_api] OK in 0.234s
        -- or on failure --
        [ext:eastmoney_api] FAILED after 2.001s: ConnectionError(...)

    Args:
        logger: Logger instance.
        service_name: Name of the external service being called.
        **context: Key-value pairs for context (logged at entry).
    """
    prefix = _rid_prefix()
    ctx_str = ', '.join(f'{k}={v}' for k, v in context.items()) if context else ''
    ctx_display = f' ({ctx_str})' if ctx_str else ''

    logger.debug('%s[ext:%s] calling%s', prefix, service_name, ctx_display)
    start = time.monotonic()
    try:
        yield
    except Exception as exc:
        elapsed = time.monotonic() - start
        logger.error('%s[ext:%s] FAILED after %.3fs: %s',
                    prefix, service_name, elapsed, exc, exc_info=True)
        raise
    else:
        elapsed = time.monotonic() - start
        logger.debug('%s[ext:%s] OK in %.3fs', prefix, service_name, elapsed)


# ══════════════════════════════════════════
#  Safe Exception Logging for Guard Clauses
# ══════════════════════════════════════════

def log_suppressed(logger: logging.Logger, context: str, exc: Exception = None,
                   level: int = logging.WARNING):
    """Log a suppressed (swallowed) exception with context — for guard clauses.

    Use this instead of bare `except: pass` or `logger.debug('Exception caught')`.
    Provides enough info to diagnose issues without the noise of full tracebacks.

    Usage:
        try:
            risky_optional_thing()
        except Exception as e:
            log_suppressed(logger, 'optional NAV fetch', e)

    Args:
        logger: Logger instance.
        context: What was being attempted (e.g. 'NAV fetch for 000001').
        exc: The caught exception (optional, uses sys.exc_info if not provided).
        level: Log level (default WARNING). Use DEBUG for truly expected failures.
    """
    prefix = _rid_prefix()
    exc_type = type(exc).__name__ if exc else 'Unknown'
    exc_msg = str(exc)[:200] if exc else ''
    logger.log(level, '%s[suppressed] %s — %s: %s', prefix, context, exc_type, exc_msg)
