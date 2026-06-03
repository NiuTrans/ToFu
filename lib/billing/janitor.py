"""lib.billing.janitor — Release stale credit reservations.

A reservation that was placed pre-flight by the chat path becomes
"stale" if the task crashed, was aborted, or died in a way that left
no settle() call. The janitor sweeps the ledger periodically and
releases any reservation older than ``stale_reserve_ttl`` seconds.

Idempotent: posts ``reserve_release`` keyed by the same ``ref_id``,
so re-running the sweep is safe.

Wiring
------
``start_janitor()`` spawns one daemon thread; ``server.py`` calls it
once at startup. The default sweep interval is 5 min, and the default
TTL is 30 min — long enough for any legitimately slow request to
finish, short enough that the operator's quota doesn't bleed.

Environment overrides::

    TOFU_BILLING_JANITOR_INTERVAL=300   # seconds between sweeps
    TOFU_BILLING_JANITOR_TTL=1800       # seconds before a reserve
                                          is considered stale
    TOFU_BILLING_JANITOR=0              # disable entirely

The janitor only sweeps reservations attached to a finished/missing
task. A reservation whose ``ref_id`` is still in ``lib.tasks_pkg.tasks``
and has status RUNNING is NEVER released — that's a long-running call
in flight, not a leak.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

from lib.database import DOMAIN_SYSTEM, get_thread_db
from lib.log import audit_log, get_logger

logger = get_logger(__name__)


_DEFAULT_INTERVAL = 300        # 5 min
_DEFAULT_TTL = 1800            # 30 min
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _config() -> dict:
    return {
        'interval': int(os.environ.get('TOFU_BILLING_JANITOR_INTERVAL')
                         or _DEFAULT_INTERVAL),
        'ttl': int(os.environ.get('TOFU_BILLING_JANITOR_TTL')
                    or _DEFAULT_TTL),
        'enabled': (os.environ.get('TOFU_BILLING_JANITOR', '1')
                     or '1').strip() != '0',
    }


def _is_task_still_running(ref_id: str) -> bool:
    """Best-effort check: is this ref_id a running in-memory task?"""
    try:
        from lib.tasks_pkg import tasks, tasks_lock
        with tasks_lock:
            t = tasks.get(ref_id)
        if t is None:
            return False
        return t.get('status') in ('pending', 'running')
    except Exception as e:
        logger.debug('[Janitor] task-lookup failed for %s: %s', ref_id, e)
        return False


def _stale_reservations(cutoff_ts: int) -> list:
    """Return list of (user_id, ref_id, amount_micro) reservations that:

      * have a ``reserve`` ledger entry older than ``cutoff_ts``
      * have NO matching ``reserve_release`` or ``debit`` entry
        (i.e. were never settled).

    Uses one SQL query so the cost is O(stale rows), not O(ledger).
    """
    db = get_thread_db(DOMAIN_SYSTEM)
    sql = '''
      SELECT r.user_id, r.ref_id, ABS(r.amount_micro) AS amt
        FROM billing_ledger r
        WHERE r.kind = 'reserve'
          AND r.ts < ?
          AND NOT EXISTS (
            SELECT 1 FROM billing_ledger s
              WHERE s.user_id = r.user_id
                AND s.ref_id = r.ref_id
                AND s.kind IN ('reserve_release', 'debit')
          )
    '''
    rows = db.execute(sql, (cutoff_ts,)).fetchall()
    out = []
    for row in rows:
        if hasattr(row, 'keys'):
            out.append((row['user_id'], row['ref_id'], int(row['amt'])))
        else:
            out.append((row[0], row[1], int(row[2])))
    return out


def sweep_once() -> dict:
    """Run one sweep. Returns a stats dict — useful in tests."""
    cfg = _config()
    cutoff = int(time.time()) - cfg['ttl']
    candidates = _stale_reservations(cutoff)
    released = 0
    skipped_running = 0
    failed = 0
    if not candidates:
        return {'candidates': 0, 'released': 0, 'skipped_running': 0,
                'failed': 0}
    from lib.billing import reserve_release
    for user_id, ref_id, amt in candidates:
        if _is_task_still_running(ref_id):
            skipped_running += 1
            continue
        try:
            reserve_release(user_id, amt, ref_id=ref_id,
                             note='janitor: stale reserve released')
            released += 1
            audit_log('billing_janitor_released',
                      user_id=user_id, ref_id=ref_id,
                      amount_micro=amt)
        except Exception as e:
            failed += 1
            logger.error('[Janitor] release failed user=%s ref=%s: %s',
                         user_id, ref_id, e, exc_info=True)
    if released or failed:
        logger.info('[Janitor] swept: candidates=%d released=%d '
                    'skipped_running=%d failed=%d',
                    len(candidates), released, skipped_running, failed)
    return {'candidates': len(candidates), 'released': released,
            'skipped_running': skipped_running, 'failed': failed}


def _wait_for_ledger_table(timeout: float = 120.0) -> bool:
    """Poll until the ``billing_ledger`` table exists and is visible.

    ``start_janitor()`` is invoked at server-import time, before
    ``init_db()`` has created the billing tables (schema init takes ~30s
    on FUSE). Gating the first sweep here avoids an ``UndefinedTable``
    crash that would otherwise spam ``error.log`` on every boot.

    Returns:
        True once the table is queryable, False if it never appears
        within ``timeout`` seconds (e.g. an install with no billing schema).
    """
    from lib.database import db_available
    deadline = time.monotonic() + timeout
    while not _stop.is_set() and time.monotonic() < deadline:
        if db_available:
            try:
                db = get_thread_db(DOMAIN_SYSTEM)
                db.execute('SELECT 1 FROM billing_ledger LIMIT 0')
                return True
            except Exception as e:
                logger.debug('[Janitor] billing_ledger not ready yet: %s', e)
        _stop.wait(2)
    return False


def _loop():
    cfg = _config()
    logger.info('[Janitor] started — interval=%ds ttl=%ds',
                cfg['interval'], cfg['ttl'])
    # Wait for init_db() to create+commit billing_ledger before sweeping —
    # otherwise the first sweep races schema init and crashes.
    if not _wait_for_ledger_table():
        logger.debug('[Janitor] billing_ledger not available within gate — '
                     'janitor idle (no billing schema on this install)')
        return
    while not _stop.is_set():
        try:
            sweep_once()
        except Exception as e:
            # A missing table is a self-recovering startup race — the next
            # tick (or restart) sees the committed schema. Keep it quiet.
            if type(e).__name__ == 'UndefinedTable' or 'does not exist' in str(e).lower():
                logger.debug('[Janitor] sweep skipped — billing schema not ready: %s', e)
            else:
                logger.error('[Janitor] sweep crashed: %s', e, exc_info=True)
        _stop.wait(cfg['interval'])
    logger.info('[Janitor] stopped')


def start_janitor() -> None:
    """Spawn the janitor thread. Idempotent; safe to call from
    ``server.py`` and from tests."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    cfg = _config()
    if not cfg['enabled']:
        logger.info('[Janitor] disabled by env')
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name='billing-janitor',
                                daemon=True)
    _thread.start()


def stop_janitor() -> None:
    _stop.set()


__all__ = ['start_janitor', 'stop_janitor', 'sweep_once']
