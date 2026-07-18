"""Lazy shims: core/ownership helpers the backup cluster calls.

Resolved at call time (in-body import) so there is NO import cycle with
``_bootstrap`` / ``_pg_ownership``. ``_bootstrap`` re-imports these fns (explicit
facade) so ``_bootstrap.<name>`` / ``from lib.database._bootstrap import <name>``
keep resolving for _core.py, the scheduler, _pg_seed's own lazy shims, and the
pg_*/tier_b/seed test suites.

These delegate to _bootstrap/_pg_ownership (facade paths stay valid even as
those modules are themselves split into packages).
"""

from lib.log import get_logger

logger = get_logger(__name__)


def _find_pg_binary(*a, **k):
    from lib.database._bootstrap import _find_pg_binary as _f
    return _f(*a, **k)

def _get_username(*a, **k):
    from lib.database._bootstrap import _get_username as _f
    return _f(*a, **k)

def _tier_b_enabled(*a, **k):
    from lib.database._bootstrap import _tier_b_enabled as _f
    return _f(*a, **k)

def _ensure_managed_pg_config(*a, **k):
    from lib.database._bootstrap import _ensure_managed_pg_config as _f
    return _f(*a, **k)

def _read_our_pg_port(*a, **k):
    from lib.database._bootstrap import _read_our_pg_port as _f
    return _f(*a, **k)

def _verify_pg_after_start(*a, **k):
    from lib.database._bootstrap import _verify_pg_after_start as _f
    return _f(*a, **k)

def _ensure_database_exists(*a, **k):
    from lib.database._bootstrap import _ensure_database_exists as _f
    return _f(*a, **k)

def _write_owner_host(*a, **k):
    from lib.database._bootstrap import _write_owner_host as _f
    return _f(*a, **k)

def _mark_pg_owned_locally(*a, **k):
    from lib.database._bootstrap import _mark_pg_owned_locally as _f
    return _f(*a, **k)

def _pg_binaries_present(*a, **k):
    from lib.database._bootstrap import _pg_binaries_present as _f
    return _f(*a, **k)

def _stop_local_pg_quietly(*a, **k):
    from lib.database._bootstrap import _stop_local_pg_quietly as _f
    return _f(*a, **k)

def _bootstrap_pg(*a, **k):
    from lib.database._bootstrap import _bootstrap_pg as _f
    return _f(*a, **k)
