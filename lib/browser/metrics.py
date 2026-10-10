"""Request-local, content-free browser timings; no persistent metric registry.

Queue dispatch records monotonic durations. Workflows explicitly propagate a
collector to their bounded workers. At most 128 samples exist per invocation.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading

_current = ContextVar('browser_timings', default=None)


@dataclass
class BrowserTimings:
    samples: list = field(default_factory=list)
    dropped: int = 0
    _lock: object = field(default_factory=threading.Lock, repr=False)

    def record(self, *, queue_ms, total_ms, execution_ms=None, failed=False, phase='other'):
        with self._lock:
            if len(self.samples) >= 128:
                self.dropped += 1
                return
            row = {'queue_ms': max(0, round(queue_ms, 2)),
                   'total_ms': max(0, round(total_ms, 2)), 'failed': bool(failed),
                   'phase': phase if phase in ('navigation', 'read', 'interaction', 'cleanup') else 'other'}
            if execution_ms is not None:
                row['execution_ms'] = max(0, min(300000, round(execution_ms, 2)))
                row['transport_and_settlement_ms'] = max(
                    0, round(total_ms - queue_ms - row['execution_ms'], 2))
            self.samples.append(row)

    def summary(self):
        with self._lock:
            rows = list(self.samples)
            dropped = self.dropped
        out = {'command_count': len(rows), 'dropped_samples': dropped,
               'failed_commands': sum(row['failed'] for row in rows)}
        phases = {}
        for row in rows:
            phase = phases.setdefault(row['phase'], {'commands': 0, 'total_ms': 0})
            phase['commands'] += 1
            phase['total_ms'] = round(phase['total_ms'] + row['total_ms'], 2)
        out['by_phase'] = phases
        for name in ('queue_ms', 'total_ms', 'execution_ms', 'transport_and_settlement_ms'):
            values = sorted(row[name] for row in rows if name in row)
            if values:
                out[name] = {'sum': round(sum(values), 2),
                             'p50': values[(len(values) - 1) // 2],
                             'p95': values[min(len(values) - 1, int(len(values) * .95))]}
        return out


@contextmanager
def measure_browser(collector=None):
    collector = collector or _current.get() or BrowserTimings()
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)


def record_command(cmd, *, failed=False):
    import time
    collector = _current.get()
    if collector is None:
        return
    started = cmd.get('started_monotonic', time.monotonic())
    finished = time.monotonic()
    claimed = cmd.get('claimed_monotonic', finished)
    command = cmd.get('type')
    phase = ('navigation' if command in ('navigate', 'create_tab') else
             'read' if command in ('read_tab', 'read_page_bundle', 'page_state', 'page_snapshot', 'fetch_url', 'research_url') else
             'cleanup' if command in ('close_tab', 'network_capture_stop') else
             'interaction' if command in ('page_click', 'page_fill', 'page_press', 'execute_js') else 'other')
    collector.record(queue_ms=(claimed - started) * 1000,
                     total_ms=(finished - started) * 1000,
                     execution_ms=cmd.get('execution_ms'), failed=failed, phase=phase)
