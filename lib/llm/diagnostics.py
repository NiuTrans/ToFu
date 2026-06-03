# HOT_PATH
"""Raw SSE diagnostic dumper — ring buffer + opt-in full transcript.

Two output paths:
- ``logs/raw_sse.log`` — opt-in full transcript (``LLM_DEBUG_RAW_SSE`` env).
- ``logs/raw_sse_anomaly.log`` — always-on ring buffer flushed on anomaly.
"""

import collections
import json
import os
import time

from lib.log import get_logger

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
            log_dir = pathlib.Path('logs')
            log_dir.mkdir(parents=True, exist_ok=True)
            self._fh = open(log_dir / 'raw_sse.log', 'a', encoding='utf-8', buffering=1)
        except Exception as e:
            logger.warning('[RawSSE] Failed to open logs/raw_sse.log: %s', e)
            self.enabled = False

    def _body_snapshot(self):
        _keys = ('model', 'thinking', 'effort', 'temperature', 'top_p',
                 'top_k', 'max_tokens', 'stream', 'reasoning_split')
        snapshot = {k: self.body.get(k) for k in _keys if k in self.body}
        snapshot['_messages_count'] = len(self.body.get('messages', []))
        snapshot['_tools_count'] = len(self.body.get('tools', []) or [])
        return snapshot

    def start(self):
        if not self.enabled:
            return
        self._open()
        if not self._fh:
            return
        try:
            ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            snapshot = self._body_snapshot()
            self._fh.write(f'\n{"=" * 80}\n')
            self._fh.write(f'[{ts}] REQUEST model={self.model} trace={self.trace_id}\n')
            self._fh.write(f'body={json.dumps(snapshot, ensure_ascii=False)}\n')
            self._fh.write(f'{"-" * 80}\n')
            self.t0 = time.time()
        except Exception as e:
            logger.warning('[RawSSE] Failed to write header: %s', e)

    def line(self, sse_line: str):
        if sse_line is None:
            sse_line = ''
        try:
            self._ring.append(sse_line)
            self._ring_bytes += len(sse_line)
            while self._ring_bytes > _ANOMALY_RING_BYTES and self._ring:
                _evicted = self._ring.popleft()
                self._ring_bytes -= len(_evicted)
        except Exception as e:
            logger.debug('[RawSSE] Failed to ring-buffer line: %s', e)

        if not self.enabled or not self._fh:
            return
        try:
            self._fh.write(sse_line)
            self._fh.write('\n')
            self.chunk_count += 1
            self.byte_count += len(sse_line)
        except Exception as e:
            logger.debug('[RawSSE] Failed to write line: %s', e)

    def dump_anomaly(self, reason: str, **summary):
        """Flush the ring buffer to logs/raw_sse_anomaly.log.

        Always writes regardless of ``self.enabled``.  Idempotent.
        """
        if self._anomaly_dumped:
            return
        self._anomaly_dumped = True
        try:
            import pathlib
            log_dir = pathlib.Path('logs')
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / 'raw_sse_anomaly.log'
            ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
            elapsed = time.time() - self._t0_wall
            snapshot = self._body_snapshot()
            with open(path, 'a', encoding='utf-8') as fh:
                fh.write(f'\n{"=" * 80}\n')
                fh.write(f'[{ts}] ANOMALY reason={reason} model={self.model} '
                         f'trace={self.trace_id} elapsed={elapsed:.2f}s\n')
                fh.write(f'body={json.dumps(snapshot, ensure_ascii=False)}\n')
                fh.write(f'summary={json.dumps(summary, ensure_ascii=False, default=str)}\n')
                fh.write(f'ring_lines={len(self._ring)} ring_bytes={self._ring_bytes}\n')
                fh.write(f'{"-" * 80}\n')
                for raw_line in self._ring:
                    fh.write(raw_line)
                    fh.write('\n')
                fh.write(f'{"=" * 80}\n')
            logger.warning('[RawSSE] Anomaly dump written: reason=%s trace=%s '
                           'lines=%d bytes=%d → %s',
                           reason, self.trace_id, len(self._ring),
                           self._ring_bytes, path)
        except Exception as e:
            logger.warning('[RawSSE] Failed to dump anomaly buffer: %s', e, exc_info=True)

    def finish(self, **summary):
        if not self.enabled or not self._fh:
            return
        try:
            elapsed = time.time() - self.t0 if self.t0 else 0
            self._fh.write(f'{"-" * 80}\n')
            self._fh.write(f'SUMMARY elapsed={elapsed:.2f}s lines={self.chunk_count} '
                           f'bytes={self.byte_count} {summary}\n')
            self._fh.write(f'{"=" * 80}\n')
            self._fh.flush()
            self._fh.close()
        except Exception as e:
            logger.debug('[RawSSE] Failed to write footer: %s', e)
        finally:
            self._fh = None
