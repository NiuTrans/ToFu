"""One bounded startup budget shared by storage and the ASGI lifespan."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from runtime_guards import storage_backup_timeout_seconds


ORDINARY_STORAGE_STARTUP_TIMEOUT_S = 30.0
FASTPATH_SEED_STARTUP_TIMEOUT_S = 900.0
_MAX_FASTPATH_SEED_STARTUP_TIMEOUT_S = 3600.0
_MAX_ORDINARY_STARTUP_TIMEOUT_S = 3600.0
_ORDINARY_LIFESPAN_STARTUP_TIMEOUT_S = 60.0
_POST_STORAGE_LIFESPAN_RESERVE_S = 60.0


def storage_startup_timeout(environ: Mapping[str, str]) -> float:
    """Return the sidecar budget for the explicitly selected topology."""
    fastpath_mode = environ.get('TOFU_STORAGE_FASTPATH', 'off').strip().lower()
    if fastpath_mode not in {'auto', 'required'} and not environ.get('TOFU_DB_FASTPATH_DIR', '').strip():
        raw_timeout = environ.get('TOFU_STORAGE_STARTUP_TIMEOUT_S', '').strip()
        if not raw_timeout:
            return ORDINARY_STORAGE_STARTUP_TIMEOUT_S
        try:
            configured_timeout = float(raw_timeout)
        except (TypeError, ValueError):
            configured_timeout = ORDINARY_STORAGE_STARTUP_TIMEOUT_S
        return min(
            _MAX_ORDINARY_STARTUP_TIMEOUT_S,
            max(ORDINARY_STORAGE_STARTUP_TIMEOUT_S, configured_timeout),
        )
    return fastpath_seed_startup_timeout(environ)


def fastpath_seed_startup_timeout(environ: Mapping[str, str]) -> float:
    """Bounded budget for one fastpath seed, restore, resume or planned exit."""
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


def _automatic_native_fastpath_may_seed(environ: Mapping[str, str]) -> bool:
    """Whether automatic native selection could seed or exit during this boot.

    Only a measured remote authority can be nominated, so ordinary deployments
    keep the short outer budget. Observation failures never extend it.
    """
    if environ.get('TOFU_DB_FASTPATH_DIR', '').strip():
        # An explicit selection already owns the seed budget and never exits.
        return False
    try:
        from lib.runtime.runtime_paths import data_root
        from lib.storage_authority_sql.native_fastpath_auto import may_seed

        return may_seed(Path(data_root()), environ)
    except Exception:
        from lib.log import get_logger

        get_logger(__name__).debug(
            'Automatic native fastpath observation failed; retaining ordinary startup budget',
            exc_info=True)
        return False


def lifespan_startup_timeout(environ: Mapping[str, str]) -> float:
    """Keep Hypercorn alive through every bounded required startup phase."""
    storage_timeout = storage_startup_timeout(environ)
    if _automatic_native_fastpath_may_seed(environ):
        # Either a planned exit followed by the ordinary launch, or one seed,
        # restore or resume launch: never both in one boot.
        storage_timeout += fastpath_seed_startup_timeout(environ)
    backup_timeout = float(storage_backup_timeout_seconds(environ))
    required_phase_timeout = max(storage_timeout, backup_timeout)
    return max(
        _ORDINARY_LIFESPAN_STARTUP_TIMEOUT_S,
        required_phase_timeout + _POST_STORAGE_LIFESPAN_RESERVE_S,
    )


__all__ = ['fastpath_seed_startup_timeout', 'lifespan_startup_timeout', 'storage_startup_timeout']
