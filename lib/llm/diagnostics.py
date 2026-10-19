# HOT_PATH
"""Bounded SSE diagnostics — metadata ring + opt-in bounded payloads.

Two output paths (malformed and over-budget payloads retain metadata only):
- ``logs/raw_sse.log`` — opt-in payload evidence (``LLM_DEBUG_RAW_SSE`` env).
- ``logs/raw_sse_anomaly.log`` — ring flushed on anomaly; metadata by default.
"""

import collections
import json
import os
import threading
import time

from lib.log import LOG_DIR, get_logger
from lib.logging_pkg.log_policy import stream_backup_count, stream_max_bytes
from lib.logging_pkg.log_record_values import (
    text_value, sanitize_value,
)
from lib.logging_pkg.log_retention import append_bytes_locked, copytruncate_if_oversize

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
    text = text_value(text, max_chars=_ANOMALY_BLOCK_MAX_CHARS)
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


def _safe_payload_value(value, *, depth=0, budget=None):
    """Bound one debug payload before serialization, never log tool arguments.

    Values retain their original credential content. Node, depth and string
    budgets keep this diagnostic projection finite.
    """
    if budget is None:
        budget = [512]
    budget[0] -= 1
    if depth >= 10 or budget[0] < 0:
        return '<diagnostic limit>'
    if isinstance(value, str):
        return text_value(value, max_chars=_RAW_SSE_LINE_MAX_CHARS)
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            if budget[0] <= 0:
                out['<omitted>'] = 'diagnostic limit'
                break
            out[text_value(key, max_chars=128)] = _safe_payload_value(
                child,
                depth=depth + 1, budget=budget)
        return out
    if isinstance(value, list):
        out = []
        for child in value:
            if budget[0] <= 0:
                out.append('<diagnostic limit>')
                break
            out.append(_safe_payload_value(
                child, depth=depth + 1, budget=budget))
        return out
    return sanitize_value(value)


def _safe_sse_payload(payload: str, *, capture_payload: bool) -> str:
    """Sanitize decoded data once, measuring original characters, not bytes."""
    if payload == '[DONE]':
        return 'data: [DONE]'
    if not capture_payload or len(payload) > _RAW_SSE_LINE_MAX_CHARS:
        return f'data: <omitted payload chars={len(payload)}>'
    try:
        value = json.loads(payload)
    except (ValueError, RecursionError):
        return f'data: <malformed payload chars={len(payload)}>'
    line = 'data: ' + json.dumps(_safe_payload_value(value), ensure_ascii=False)
    return text_value(line, max_chars=_RAW_SSE_LINE_MAX_CHARS)


def _safe_sse_line(line: str, *, capture_payload: bool) -> str:
    if line.startswith('data:'):
        return _safe_sse_payload(line[5:].lstrip(' '), capture_payload=capture_payload)
    return text_value(line, max_chars=_RAW_SSE_LINE_MAX_CHARS)


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
        # The native Messages adapter moves effort out of the canonical
        # top-level field. Keep just that scalar: output schemas may contain
        # user data and do not belong in request diagnostics.
        output_config = self.body.get('output_config')
        if isinstance(output_config, dict) and output_config.get('effort') in (
                'low', 'medium', 'high', 'xhigh', 'max'):
            snapshot['output_config'] = {'effort': output_config['effort']}
        snapshot['_messages_count'] = len(self.body.get('messages', []))
        snapshot['_tools_count'] = len(self.body.get('tools', []) or [])
        safe = sanitize_value(
            snapshot, max_items=20,
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
                model = text_value(self.model, max_chars=256)
                trace_id = text_value(self.trace_id, max_chars=256)
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
        self._retain_line(_safe_sse_line(sse_line, capture_payload=self.enabled))

    def payload(self, data: str):
        """Record decoded data without retaining raw content or synthetic framing."""
        self._retain_line(_safe_sse_payload(data, capture_payload=self.enabled))

    def _retain_line(self, safe_line: str):
        try:
            if len(self._ring) == self._ring.maxlen:
                evicted = self._ring.popleft()
                self._ring_bytes -= len(evicted.encode('utf-8', errors='replace'))
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
            # A single long debug-enabled request must not exceed the log
            # lifecycle just because no later request arrives to rotate it.
            # Rotate before taking the write flock (copytruncate takes its own
            # lock and preserves this open O_APPEND descriptor).
            try:
                max_bytes = stream_max_bytes('raw_sse')
                incoming = len(safe_line.encode('utf-8', errors='replace')) + 1
                if os.fstat(self._fh.fileno()).st_size + incoming > max_bytes:
                    copytruncate_if_oversize(
                        self._fh.name if isinstance(self._fh.name, str) else
                        os.path.join(LOG_DIR, 'raw_sse.log'),
                        max_bytes=max_bytes,
                        trigger_bytes=max(1, max_bytes - incoming),
                        backup_count=stream_backup_count('raw_sse'))
            except (OSError, ValueError) as error:
                logger.debug('[RawSSE] rotation check failed: %s', error)
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
                summary, max_items=30,
                max_string_chars=2_000)
            model = text_value(self.model, max_chars=256)
            trace_id = text_value(self.trace_id, max_chars=256)
            safe_reason = text_value(reason, max_chars=128)
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
                    summary, max_items=30,
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
