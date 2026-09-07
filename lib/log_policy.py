"""Declarative storage policy for every durable Tofu log stream.

This module is deliberately standard-library-only.  It is imported by the
application server, the lifecycle manager, PostgreSQL bootstrap helpers and
offline diagnostics, so those independently-started processes share one set
of bounded defaults instead of inventing retention limits at each write site.

The values below are safety ceilings, not targets.  Individual streams rotate
at their per-file limit; :mod:`lib.log_retention` additionally enforces the
whole-directory budget.  Environment overrides are parsed through the same
bounded resolver and therefore cannot accidentally turn a typo into unlimited
retention.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from runtime_guards import deployment_resource_default


MIB = 1024 * 1024
GIB = 1024 * MIB
LOG_FILE_MODE = 0o600
LOG_DIRECTORY_MODE = 0o700


@dataclass(frozen=True)
class LogStreamPolicy:
    """One stream's on-disk contract.

    ``family_budget_bytes`` covers the active file plus every rotated chunk.
    Writers that only support numbered backups use ``backup_count`` directly;
    time-based writers still use the family budget as their final hard bound.
    """

    name: str
    filename: str
    max_bytes: int
    backup_count: int
    family_budget_bytes: int
    retention_days: int
    priority: int


_POLICIES = (
    LogStreamPolicy('app', 'app.log', 32 * MIB, 14, 256 * MIB, 14, 90),
    LogStreamPolicy('access', 'access.log', 16 * MIB, 7, 96 * MIB, 7, 35),
    LogStreamPolicy('error', 'error.log', 8 * MIB, 8, 72 * MIB, 30, 100),
    LogStreamPolicy('vendor', 'vendor.log', 8 * MIB, 3, 32 * MIB, 14, 25),
    LogStreamPolicy('frontend', 'frontend.log', 16 * MIB, 4, 64 * MIB, 7, 55),
    LogStreamPolicy('desktop_client_diag', 'desktop_client_diag.log', 8 * MIB,
                    3, 32 * MIB, 30, 80),
    LogStreamPolicy('incident', 'incident.jsonl', 8 * MIB, 4, 40 * MIB, 30, 100),
    LogStreamPolicy('audit', 'audit.log', 16 * MIB, 6, 112 * MIB, 30, 100),
    LogStreamPolicy('server_console', 'server-console.log', 32 * MIB, 3,
                    128 * MIB, 14, 70),
    LogStreamPolicy('server_manager', 'server-manager.log', 8 * MIB, 3,
                    32 * MIB, 30, 85),
    LogStreamPolicy('supervisor_tofu', 'supervisor_tofu.log', 16 * MIB, 3,
                    64 * MIB, 14, 70),
    LogStreamPolicy('supervisor_watchdog', 'supervisor-watchdog.log', 4 * MIB,
                    2, 12 * MIB, 30, 85),
    LogStreamPolicy('watchdog', 'watchdog.log', 4 * MIB, 2, 12 * MIB, 30, 95),
    LogStreamPolicy('storage_postgres', 'storage-postgresql.log', 8 * MIB, 3,
                    32 * MIB, 14, 80),
    LogStreamPolicy('raw_sse_anomaly', 'raw_sse_anomaly.log', 16 * MIB, 2,
                    48 * MIB, 14, 75),
    LogStreamPolicy('raw_sse', 'raw_sse.log', 16 * MIB, 1, 32 * MIB, 3, 20),
    LogStreamPolicy('cgroup_pressure', 'cgroup_pressure.log', 4 * MIB, 1,
                    8 * MIB, 14, 90),
    LogStreamPolicy('faulthandler_legacy', 'faulthandler.log', 8 * MIB, 2,
                    24 * MIB, 30, 100),
    LogStreamPolicy('faulthandler_process', 'tofu_faulthandler_*.log',
                    16 * MIB, 8, 64 * MIB, 30, 100),
    # Desktop/supervisor child stdout lives beside platform state rather than
    # under the server's logs/ root, but it obeys the same bounded contract.
    LogStreamPolicy('desktop_console', 'desktop.log', 8 * MIB, 3,
                    32 * MIB, 14, 75),
    LogStreamPolicy('desktop_agent_console', 'desktop-agent.log', 8 * MIB, 3,
                    32 * MIB, 14, 75),
    LogStreamPolicy('desktop_adapter_console', 'adapter.log', 8 * MIB, 3,
                    32 * MIB, 14, 65),
    LogStreamPolicy('supervisor_server_console', 'supervisor-server.log',
                    16 * MIB, 3, 64 * MIB, 14, 70),
)

STREAM_POLICIES = {policy.name: policy for policy in _POLICIES}
POLICY_BY_FILENAME = {policy.filename: policy for policy in _POLICIES}


_LEGACY_ENV = {
    ('audit', 'max_bytes'): 'TOFU_AUDIT_LOG_MAX_BYTES',
    ('audit', 'backup_count'): 'TOFU_AUDIT_LOG_BACKUPS',
    ('raw_sse_anomaly', 'max_bytes'): 'TOFU_RAW_SSE_ANOMALY_MAX_BYTES',
    ('raw_sse_anomaly', 'backup_count'): 'TOFU_RAW_SSE_ANOMALY_BACKUPS',
    ('faulthandler_process', 'max_bytes'): 'TOFU_FAULT_DUMP_MAX_BYTES',
    ('faulthandler_process', 'backup_count'): 'TOFU_FAULT_DUMP_FILES',
    ('faulthandler_process', 'family_budget_bytes'):
        'TOFU_FAULT_DUMP_TOTAL_BYTES',
}


def _env_name(stream_name: str, field: str) -> str:
    suffix = 'MAX_BYTES' if field == 'max_bytes' else 'BACKUPS'
    return f'TOFU_{stream_name.upper()}_LOG_{suffix}'


def _bounded_int(raw: object, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(maximum, value))


def stream_max_bytes(name: str) -> int:
    policy = STREAM_POLICIES[name]
    env_name = _LEGACY_ENV.get((name, 'max_bytes'),
                               _env_name(name, 'max_bytes'))
    return _bounded_int(os.environ.get(env_name), policy.max_bytes,
                        1 * MIB, 1 * GIB)


def stream_backup_count(name: str) -> int:
    policy = STREAM_POLICIES[name]
    env_name = _LEGACY_ENV.get((name, 'backup_count'),
                               _env_name(name, 'backup_count'))
    return _bounded_int(os.environ.get(env_name), policy.backup_count, 1, 64)


def stream_family_budget_bytes(name: str) -> int:
    """Return one family's hard budget after a bounded environment override."""
    policy = STREAM_POLICIES[name]
    env_name = _LEGACY_ENV.get(
        (name, 'family_budget_bytes'),
        f'TOFU_{name.upper()}_LOG_FAMILY_BYTES')
    # A family budget may not be smaller than its active-file ceiling.  This
    # keeps custom max-byte overrides internally coherent instead of making
    # maintenance delete every backup forever while still remaining over cap.
    minimum = stream_max_bytes(name)
    default = max(minimum, policy.family_budget_bytes)
    return _bounded_int(os.environ.get(env_name), default, minimum, 4 * GIB)


def total_log_budget_bytes() -> int:
    """Return the profile-aware global direct ``logs/`` budget."""
    default_mb = deployment_resource_default(
        'TOFU_LOG_TOTAL_BUDGET_MB', os.environ)
    raw_bytes = os.environ.get('TOFU_LOG_TOTAL_BUDGET_BYTES')
    if raw_bytes not in (None, ''):
        return _bounded_int(
            raw_bytes, default_mb * MIB, 64 * MIB, 16 * GIB)
    raw_mb = os.environ.get('TOFU_LOG_TOTAL_BUDGET_MB')
    return _bounded_int(raw_mb, default_mb, 64, 16 * 1024) * MIB


def maintenance_interval_seconds() -> float:
    try:
        value = float(os.environ.get('TOFU_LOG_MAINTENANCE_SEC', '') or 900)
    except (TypeError, ValueError, OverflowError):
        value = 900.0
    return max(60.0, min(86_400.0, value))


def policy_manifest() -> list[dict]:
    """Return a machine-readable, environment-resolved policy inventory."""
    return [{
        'name': policy.name,
        'filename': policy.filename,
        'file_mode': format(LOG_FILE_MODE, '04o'),
        'max_bytes': stream_max_bytes(policy.name),
        'backup_count': stream_backup_count(policy.name),
        'family_budget_bytes': stream_family_budget_bytes(policy.name),
        'retention_days': policy.retention_days,
        'priority': policy.priority,
    } for policy in _POLICIES]


__all__ = [
    'GIB', 'LOG_DIRECTORY_MODE', 'LOG_FILE_MODE', 'MIB', 'LogStreamPolicy',
    'POLICY_BY_FILENAME', 'STREAM_POLICIES', 'maintenance_interval_seconds',
    'policy_manifest', 'stream_backup_count', 'stream_family_budget_bytes',
    'stream_max_bytes', 'total_log_budget_bytes',
]
