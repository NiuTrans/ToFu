"""lib/boot_profile.py — persist the LAST boot's per-phase timings.

The in-app restart progress bar (``frontend/src/runtime/sections/update.js``)
cannot stream the booting process's lifespan over the wire, so it estimates
progress from a per-phase time budget.  A hardcoded budget is fiction on any
host whose storage/filesystem profile differs from the author's (the bar
pinned at 96% while the backend was still at phase 3/9 on a network FS).
This module closes the loop: the production lifespan MEASURES each required
phase, the restart client reads the previous boot's profile from
``/api/health`` (``bootProfile``) and budgets the bar from the host's own
observed boot shape instead of a universal guess.

The profile lives in the process data dir (``data_root()``), which the
re-exec keeps, so a fresh process serves the profile its predecessor wrote.
The file is small, append-free, and rewritten once per boot.
"""

from __future__ import annotations

import time
from typing import Any

from lib.log import get_logger
from lib.runtime.runtime_paths import data_root

logger = get_logger(__name__)

PROFILE_NAME = 'boot_profile.json'
_SCHEMA = 1


def record_boot_phases(
    phases: list[dict[str, Any]],
    *,
    phase_total: int,
    boot_to_ready_s: float,
) -> dict[str, Any] | None:
    """Persist one boot's measured phase timings; best-effort, never raises.

    ``phases`` is a list of ``{key,label,index,duration_s}`` in run order.
    Returns the written payload (also exposed to callers for tests) or None
    when persistence failed — a boot must never fail over telemetry.
    """
    try:
        from lib.json_store import write_json_atomic

        clean = []
        for entry in phases:
            try:
                clean.append({
                    'key': str(entry.get('key') or ''),
                    'label': str(entry.get('label') or ''),
                    'index': int(entry.get('index') or 0),
                    'duration_s': round(
                        max(0.0, float(entry.get('duration_s') or 0.0)), 3),
                })
            except (TypeError, ValueError):
                continue
        payload = {
            'schema': _SCHEMA,
            'booted_at': time.time(),
            'phase_total': int(phase_total),
            'boot_to_ready_s': round(max(0.0, float(boot_to_ready_s)), 3),
            'phases': clean,
        }
        write_json_atomic(_profile_path(), payload)
        return payload
    except Exception as exc:  # telemetry must not gate readiness
        logger.debug('[BootProfile] persist failed: %s', exc)
        return None


def load_boot_profile() -> dict[str, Any] | None:
    """Return the last persisted profile, or None when absent/corrupt."""
    try:
        from lib.json_store import read_json

        payload = read_json(_profile_path(), default=None)
        if not isinstance(payload, dict):
            return None
        if payload.get('schema') != _SCHEMA:
            return None
        if not isinstance(payload.get('phases'), list):
            return None
        return payload
    except Exception as exc:
        logger.debug('[BootProfile] load failed: %s', exc)
        return None


def _profile_path() -> str:
    import os

    return os.path.join(data_root(), PROFILE_NAME)


__all__ = ['PROFILE_NAME', 'load_boot_profile', 'record_boot_phases']
