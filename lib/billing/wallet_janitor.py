"""lib.billing.wallet_janitor — Reclaim orphaned reservations.

A billable request reserves credits up front (``reserve(-estimate)``) and
releases the hold at the end inside :func:`lib.billing.wallet.settle`
(``reserve_release(+estimate)`` + ``debit(-actual)``). If the process
crashes — or the request aborts — between the reserve and the settle, the
hold is never released: the credits stay subtracted from the user's
*usable* balance forever, with nothing to ever reclaim them.

This module is the janitor the wallet docstring promised. It finds
``reserve`` ledger rows with no matching ``reserve_release`` for the same
``ref_id`` that are older than a TTL, and releases each one through the
existing idempotent :func:`lib.billing.wallet.reserve_release` path. Because
that path is keyed on ``ref_id``, the sweep is safe to run repeatedly: once a
hold is released the next sweep no longer sees it as orphaned.

Wiring: one durably claimed scheduler task calls :func:`sweep_stale_reserves`
every few minutes (see
``lib.scheduler.manager._ensure_default_reserve_reclaim_task``). The task is
enabled only for a multi-user relay with billing active; personal/private and
BYO-only installs retain no periodic billing worker.
"""

from __future__ import annotations

import os
import time

from lib.log import audit_log, get_logger, log_context
from lib.storage import get_storage_client

from . import wallet as _wallet

logger = get_logger(__name__)

# Default time-to-live for an un-released reserve before the janitor reclaims
# it. A hold that has lived this long with no matching reserve_release belongs
# to a request that crashed or aborted mid-flight. Overridable per deployment
# via the env var (kept out of code per CLAUDE.md §3.5).
_DEFAULT_RESERVE_TTL_S = 1800  # 30 minutes


def _resolve_ttl(ttl_seconds) -> int:
    """Resolve the reserve TTL, preferring the explicit arg, then env."""
    if ttl_seconds is not None:
        return int(ttl_seconds)
    raw = (os.environ.get('TOFU_BILLING_RESERVE_TTL', '')
           or os.environ.get('TOFU_BILLING_JANITOR_TTL', ''))
    if raw:
        try:
            return int(raw)
        except (ValueError, TypeError) as e:
            logger.debug('[Billing] Invalid TOFU_BILLING_RESERVE_TTL=%r, '
                         'defaulting to %ds: %s', raw, _DEFAULT_RESERVE_TTL_S, e)
    return _DEFAULT_RESERVE_TTL_S


def reserve_reclaim_enabled() -> bool:
    """Return whether this deployment may schedule automatic money recovery."""
    raw = (os.environ.get('TOFU_BILLING_JANITOR', '1') or '1').strip().lower()
    if raw in {'0', 'false', 'no', 'off'}:
        return False
    from lib.auth_mode import is_multi_user
    from lib.relay_config import billing_enabled

    return is_multi_user() and billing_enabled()


def _is_task_still_running(ref_id: str, *, user_id) -> bool:
    """Best-effort personal-process guard for unusually long requests."""
    try:
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        task = chat_task_runtime.get_owned(ref_id, user_id=int(user_id))
        return bool(task and task.get('status') in ('pending', 'running'))
    except Exception as exc:
        logger.debug('[Billing] running-task check failed for %s: %s',
                     ref_id, exc)
        return False


def find_stale_reserves(cutoff_ts: int):
    """Return ``[(user_id, ref_id, held_micro), ...]`` for orphaned reserves.

    A reservation is orphaned when, for a given ``(user_id, ref_id)``, the
    summed ``reserve`` holds are not fully cancelled by ``reserve_release``
    rows AND the most recent ``reserve`` row is older than ``cutoff_ts``.
    ``held_micro`` is the positive amount still held (to be refunded).

    Grouping by ``(user_id, ref_id)`` makes the result correct even if a
    single ref accumulated multiple reserve rows.
    """
    rows = get_storage_client().query(
        'billing.reserve.stale', {'cutoff_ts': int(cutoff_ts), 'limit': 10_000},
        deadline=10.0)
    return [
        (row['user_id'], row['ref_id'], int(row['held_micro']))
        for row in rows
    ]


def sweep_stale_reserves(ttl_seconds=None) -> dict:
    """Reclaim reservations orphaned by a crash/abort before settle.

    Finds ``reserve`` holds with no matching ``reserve_release`` older than
    the TTL and releases each via the idempotent
    :func:`lib.billing.wallet.reserve_release`. Idempotent across runs: a
    released hold is no longer orphaned, so a second sweep is a no-op.

    Args:
        ttl_seconds: Age threshold in seconds. Defaults to
            ``TOFU_BILLING_RESERVE_TTL`` env, else 1800 (30 min).

    Returns:
        Summary dict: ``{ok, reclaimed, reclaimed_micro, candidates,
        skipped_running, errors, ttl_seconds}``.
    """
    ttl = _resolve_ttl(ttl_seconds)
    cutoff = int(time.time()) - ttl
    summary = {
        'ok': True, 'reclaimed': 0, 'reclaimed_micro': 0,
        'candidates': 0, 'skipped_running': 0,
        'errors': 0, 'ttl_seconds': ttl,
    }

    with log_context('billing_reserve_sweep', logger=logger):
        try:
            stale = find_stale_reserves(cutoff)
        except Exception as e:
            # A missing billing_ledger table (SQLite install with billing
            # never activated) or transient DB error is non-fatal — the sweep
            # simply has nothing to do this tick.
            logger.warning('[Billing] reserve sweep query failed '
                           '(nothing reclaimed this run): %s', e)
            summary['ok'] = False
            return summary

        summary['candidates'] = len(stale)
        if not stale:
            logger.debug('[Billing] reserve sweep: no stale reserves '
                         '(ttl=%ds)', ttl)
            return summary

        for user_id, ref_id, held_micro in stale:
            if _is_task_still_running(ref_id, user_id=user_id):
                summary['skipped_running'] += 1
                continue
            try:
                _wallet.reserve_release(
                    user_id, held_micro, ref_id=ref_id,
                    note=f'janitor: stale reserve reclaimed after {ttl}s')
                summary['reclaimed'] += 1
                summary['reclaimed_micro'] += held_micro
                audit_log('billing_reserve_reclaim', user_id=user_id,
                          ref_id=ref_id, amount_micro=held_micro,
                          ttl_seconds=ttl)
            except Exception as e:
                summary['errors'] += 1
                logger.error('[Billing] failed to reclaim stale reserve '
                             'user=%s ref=%s amount=%dµ: %s',
                             user_id, ref_id, held_micro, e, exc_info=True)

        logger.info('[Billing] reserve sweep reclaimed %d/%d orphaned '
                    'hold(s), %dµ total (ttl=%ds)',
                    summary['reclaimed'], summary['candidates'],
                    summary['reclaimed_micro'], ttl)
    return summary


__all__ = [
    'sweep_stale_reserves', 'find_stale_reserves',
    'reserve_reclaim_enabled',
]
