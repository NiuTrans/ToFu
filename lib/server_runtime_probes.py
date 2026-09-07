"""Cheap host-pressure probes used by the event-loop watchdog.

Failures are expected on restricted containers and non-Linux hosts. They are
aggregated into bounded metrics instead of being logged on every watchdog
sample, keeping the boundary visible without creating log noise.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from lib.observability import record_runtime_probe_failure


def _read_loadavg() -> str:
    with open('/proc/loadavg', encoding='ascii') as handle:
        return handle.read().split()[0]


def _read_cgroup_pressure() -> Mapping[str, Any] | None:
    from lib.cgroup_guard import pressure

    return pressure()


def stall_pressure_context(
    *,
    loadavg_reader: Callable[[], str] | None = None,
    pressure_reader: Callable[[], Mapping[str, Any] | None] | None = None,
) -> str:
    """Return a compact, non-raising host-pressure snapshot.

    Readers are injectable so failure accounting remains deterministic in
    tests. Production uses only local ``/proc`` and cgroupfs reads.
    """
    parts: list[str] = []
    try:
        load1 = (loadavg_reader or _read_loadavg)()
        if load1:
            parts.append(f'load1={load1}')
    except Exception:
        record_runtime_probe_failure('loadavg')

    try:
        pressure = (pressure_reader or _read_cgroup_pressure)()
        if pressure:
            parts.append('cgmem=%.1f%%' % float(pressure['pct']))
    except Exception:
        record_runtime_probe_failure('cgroup_memory')

    return ' '.join(parts)


__all__ = ['stall_pressure_context']
