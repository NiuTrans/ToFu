# HOT_PATH
"""Raw SSE diagnostic dumper — ring buffer + opt-in full transcript.

Two output paths:
- ``logs/raw_sse.log`` — opt-in full transcript (``LLM_DEBUG_RAW_SSE`` env).
- ``logs/raw_sse_anomaly.log`` — always-on ring buffer flushed on anomaly.
"""

import collections
import json
import os
import threading
import time

from lib.log import LOG_DIR, get_logger
from lib.log_policy import stream_backup_count, stream_max_bytes
from lib.log_redaction import redact_text, sanitize_value
from lib.log_retention import append_bytes_locked, copytruncate_if_oversize

logger = get_logger(__name__)

# ── Raw-SSE dumper config ──
# Enable by setting LLM_DEBUG_RAW_SSE:
#   ""       → off (zero overhead, default)
#   "1"      → capture ALL requests
#   <string> → capture only when model name contains the string
_RAW_SSE_FILTER = os.environ.get('LLM_DEBUG_RAW_SSE', '').strip()

# Anomaly buffer bounds
_ANOMALY_RING_LINES = 400
_ANOMALY_RING_BYTES = 256 * 1024  # 256 KB
_ANOMALY_BLOCK_MAX_CHARS = 512 * 1024
_RAW_SSE_LINE_MAX_CHARS = 64 * 1024
_ANOMALY_WRITE_LOCK = threading.Lock()
_RAW_SSE_WRITE_LOCK = threading.Lock()


def _lock_raw_file(handle, *, acquire: bool) -> None:
    """Coordinate app-owned raw writes with retention's copy-truncate lock."""
    try:
        import fcntl
        operation = fcntl.LOCK_EX if acquire else fcntl.LOCK_UN
        fcntl.flock(handle.fileno(), operation)
    except (ImportError, OSError, ValueError):
        pass


def _anomaly_rotation_limits():
    """Bound the always-on raw anomaly trail without install-time tuning."""
    return (stream_max_bytes('raw_sse_anomaly'),
            stream_backup_count('raw_sse_anomaly'))


def _rotate_anomaly_if_needed(path, incoming_bytes):
    """Rotate one append target; caller holds ``_ANOMALY_WRITE_LOCK``."""
    max_bytes, backups = _anomaly_rotation_limits()
    try:
        copytruncate_if_oversize(
            path,
            max_bytes=max_bytes,
            trigger_bytes=max(1, max_bytes - max(0, int(incoming_bytes))),
            backup_count=backups)
    except OSError as exc:
        logger.debug('[RawSSE] anomaly-log rotation failed: %s', exc)


def _append_anomaly(path, text):
    """Atomically append one bounded anomaly block across concurrent turns."""
    text = redact_text(text, max_chars=_ANOMALY_BLOCK_MAX_CHARS)
    payload = text.encode('utf-8', errors='replace')
    encoded_size = len(payload)
    with _ANOMALY_WRITE_LOCK:
        _rotate_anomaly_if_needed(path, encoded_size)
        append_bytes_locked(path, payload)


def _raw_sse_enabled(model: str) -> bool:
    """Check whether raw SSE dumping is enabled for this model."""
    if not _RAW_SSE_FILTER:
        return False
    if _RAW_SSE_FILTER == '1':
        return True
    return _RAW_SSE_FILTER in (model or '')


class RawSSEDumper:
    """Captures raw SSE traffic for diagnostics.

    Usage:
        dumper = RawSSEDumper(model, trace_id, body)
        dumper.start()
        dumper.line(sse_line)
        dumper.finish(summary)
        dumper.dump_anomaly('empty_stop', chunks=N, ...)
    """

    def __init__(self, model: str, trace_id: str, body: dict):
        self.enabled = _raw_sse_enabled(model)
        self.model = model
        self.trace_id = trace_id
        self.body = body
        self.t0 = 0.0
        self.chunk_count = 0
        self.byte_count = 0
        self._fh = None
        self._ring = collections.deque(maxlen=_ANOMALY_RING_LINES)
        self._ring_bytes = 0
        self._anomaly_dumped = False
        self._t0_wall = time.time()

    def _open(self):
        if self._fh is not None:
            return
        try:
            import pathlib
            log_dir = pathlib.Path(LOG_DIR)
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / 'raw_sse.log'
            copytruncate_if_oversize(
                path, max_bytes=stream_max_bytes('raw_sse'),
                backup_count=stream_backup_count('raw_sse'))
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            flags |= getattr(os, 'O_NOFOLLOW', 0)
            descriptor = os.open(path, flags, 0o600)
            self._fh = os.fdopen(
                descriptor, 'a', encoding='utf-8', buffering=1)
        except Exception as e:
            logger.warning('[RawSSE] Failed to open logs/raw_sse.log: %s', e)
            self.enabled = False

    def _body_snapshot(self):
        _keys = ('model', 'thinking', 'effort', 'temperature', 'top_p',
                 'top_k', 'max_tokens', 'stream', 'reasoning_split')
        snapshot = {k: self.body.get(k) for k in _keys if k in self.body}
        snapshot['_messages_count'] = len(self.body.get('messages', []))
        snapshot['_tools_count'] = len(self.body.get('tools', []) or [])
        safe = sanitize_value(
            snapshot, field_name='raw_sse_request', max_items=20,
            max_string_chars=1_000)
        return safe if isinstance(safe, dict) else {}

    def start(self):
        if not self.enabled:
            return
        with _RAW_SSE_WRITE_LOCK:
            self._open()
            if not self._fh:
                return
            _lock_raw_file(self._fh, acquire=True)
            try:
                ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                snapshot = self._body_snapshot()
                model = redact_text(self.model, max_chars=256)
                trace_id = redact_text(self.trace_id, max_chars=256)
                self._fh.write(f'\n{"=" * 80}\n')
                self._fh.write(
                    f'[{ts}] REQUEST model={model} trace={trace_id}\n')
                self._fh.write(
                    f'body={json.dumps(snapshot, ensure_ascii=False)}\n')
                self._fh.write(f'{"-" * 80}\n')
                self.t0 = time.time()
            except Exception as e:
                logger.warning('[RawSSE] Failed to write header: %s', e)
            finally:
                _lock_raw_file(self._fh, acquire=False)

    def line(self, sse_line: str):
        if sse_line is None:
            sse_line = ''
        safe_line = redact_text(sse_line, max_chars=_RAW_SSE_LINE_MAX_CHARS)
        try:
            self._ring.append(safe_line)
            self._ring_bytes += len(safe_line.encode('utf-8', errors='replace'))
            while self._ring_bytes > _ANOMALY_RING_BYTES and self._ring:
                _evicted = self._ring.popleft()
                self._ring_bytes -= len(
                    _evicted.encode('utf-8', errors='replace'))
        except Exception as e:
            logger.debug('[RawSSE] Failed to ring-buffer line: %s', e)

        if not self.enabled or not self._fh:
            return
        with _RAW_SSE_WRITE_LOCK:
            if not self._fh:
                return
            _lock_raw_file(self._fh, acquire=True)
            try:
                self._fh.write(safe_line)
                self._fh.write('\n')
                self.chunk_count += 1
                self.byte_count += len(
                    safe_line.encode('utf-8', errors='replace'))
            except Exception as e:
                logger.debug('[RawSSE] Failed to write line: %s', e)
            finally:
                _lock_raw_file(self._fh, acquire=False)

    def dump_anomaly(self, reason: str, **summary):
        """Flush the ring buffer to logs/raw_sse_anomaly.log.

        Always writes regardless of ``self.enabled``.  Idempotent.
        """
        if self._anomaly_dumped:
            return
        self._anomaly_dumped = True
        try:
            import pathlib
            log_dir = pathlib.Path(LOG_DIR)
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / 'raw_sse_anomaly.log'
            ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            elapsed = time.time() - self._t0_wall
            snapshot = self._body_snapshot()
            safe_summary = sanitize_value(
                summary, field_name='raw_sse_summary', max_items=30,
                max_string_chars=2_000)
            model = redact_text(self.model, max_chars=256)
            trace_id = redact_text(self.trace_id, max_chars=256)
            safe_reason = redact_text(reason, max_chars=128)
            parts = [
                f'\n{"=" * 80}\n',
                f'[{ts}] ANOMALY reason={safe_reason} model={model} '
                f'trace={trace_id} elapsed={elapsed:.2f}s\n',
                f'body={json.dumps(snapshot, ensure_ascii=False)}\n',
                f'summary={json.dumps(safe_summary, ensure_ascii=False)}\n',
                f'ring_lines={len(self._ring)} ring_bytes={self._ring_bytes}\n',
                f'{"-" * 80}\n',
            ]
            parts.extend(raw_line + '\n' for raw_line in self._ring)
            parts.append(f'{"=" * 80}\n')
            _append_anomaly(path, ''.join(parts))
            logger.warning('[RawSSE] Anomaly dump written: reason=%s trace=%s '
                           'lines=%d bytes=%d → %s',
                           safe_reason, trace_id, len(self._ring),
                           self._ring_bytes, path)
        except Exception as e:
            logger.warning('[RawSSE] Failed to dump anomaly buffer: %s', e, exc_info=True)

    def finish(self, **summary):
        if not self.enabled or not self._fh:
            return
        with _RAW_SSE_WRITE_LOCK:
            handle = self._fh
            _lock_raw_file(handle, acquire=True)
            try:
                elapsed = time.time() - self.t0 if self.t0 else 0
                safe_summary = sanitize_value(
                    summary, field_name='raw_sse_summary', max_items=30,
                    max_string_chars=2_000)
                handle.write(f'{"-" * 80}\n')
                handle.write(
                    f'SUMMARY elapsed={elapsed:.2f}s lines={self.chunk_count} '
                    f'bytes={self.byte_count} '
                    f'{json.dumps(safe_summary, ensure_ascii=False)}\n')
                handle.write(f'{"=" * 80}\n')
                handle.flush()
            except Exception as e:
                logger.debug('[RawSSE] Failed to write footer: %s', e)
            finally:
                _lock_raw_file(handle, acquire=False)
                try:
                    handle.close()
                except Exception as exc:
                    logger.debug('[RawSSE] close failed: %s', exc)
                self._fh = None
                try:
                    path = os.path.join(LOG_DIR, 'raw_sse.log')
                    copytruncate_if_oversize(
                        path, max_bytes=stream_max_bytes('raw_sse'),
                        backup_count=stream_backup_count('raw_sse'))
                except Exception as e:
                    logger.debug('[RawSSE] final retention pass failed: %s', e)
