"""One bounded startup budget shared by storage and the ASGI lifespan."""

from __future__ import annotations

from collections.abc import Mapping

from runtime_guards import storage_backup_timeout_seconds


ORDINARY_STORAGE_STARTUP_TIMEOUT_S = 30.0
FASTPATH_SEED_STARTUP_TIMEOUT_S = 900.0
_MAX_FASTPATH_SEED_STARTUP_TIMEOUT_S = 3600.0
_ORDINARY_LIFESPAN_STARTUP_TIMEOUT_S = 60.0
_POST_STORAGE_LIFESPAN_RESERVE_S = 60.0


def storage_startup_timeout(environ: Mapping[str, str]) -> float:
    """Return the sidecar budget for the explicitly selected topology."""
    fastpath_mode = environ.get('TOFU_STORAGE_FASTPATH', 'off').strip().lower()
    if fastpath_mode not in {'auto', 'required'}:
        return ORDINARY_STORAGE_STARTUP_TIMEOUT_S
    raw_timeout = environ.get(
        'TOFU_STORAGE_FASTPATH_STARTUP_TIMEOUT_S',
        str(FASTPATH_SEED_STARTUP_TIMEOUT_S),
    )
    try:
        configured_timeout = float(raw_timeout)
    except (TypeError, ValueError):
        configured_timeout = FASTPATH_SEED_STARTUP_TIMEOUT_S
    return min(
        _MAX_FASTPATH_SEED_STARTUP_TIMEOUT_S,
        max(ORDINARY_STORAGE_STARTUP_TIMEOUT_S, configured_timeout),
    )


def lifespan_startup_timeout(environ: Mapping[str, str]) -> float:
    """Keep Hypercorn alive through every bounded required startup phase."""
    storage_timeout = storage_startup_timeout(environ)
    backup_timeout = float(storage_backup_timeout_seconds(environ))
    required_phase_timeout = max(storage_timeout, backup_timeout)
    return max(
        _ORDINARY_LIFESPAN_STARTUP_TIMEOUT_S,
        required_phase_timeout + _POST_STORAGE_LIFESPAN_RESERVE_S,
    )


__all__ = ['lifespan_startup_timeout', 'storage_startup_timeout']
