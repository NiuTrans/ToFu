"""Shared request-cost snapshots and terminal display totals.

Entries stamp_round_costs, usage_with_round_costs and stamp_terminal_cost.
The existing lib.cost engine owns pricing. Request snapshots use request-bound
routes; terminal folding retains its explicit checkpoint/provider fallback.
Frozen native receipts never acquire Python price-lookup authority.
"""
from lib.log import get_logger

logger = get_logger(__name__)


def stamp_round_costs(task, api_rounds, *, model):
    """Snapshot request-local prices before checkpoint or terminal projection.

    No pricing at replay/projection time, and no repricing mixed-model aggregate
    usage under the terminal model. Missing usage stays unpriced. Compaction
    detail can be truncated; the Flow receipt reports that coverage separately.

    A round priced here never changes again: it is replaced in ``api_rounds``
    by its settled twin, which Turn projection capture shares by reference.
    """
    if task.get('_nativeFrozenCostProjection') is True:
        return

    from lib.conversation_sync.settled_rows import is_settled, settle_or_none
    from lib.cost import compute_cost
    from math import isfinite

    for index, row in enumerate(api_rounds):
        if not isinstance(row, dict) or row.get('cost'):
            continue
        usage = row.get('usage')
        if not isinstance(usage, dict) or not any(
            not isinstance(usage.get(key), bool)
            and isinstance(usage.get(key), (int, float))
            and usage[key] >= 0
            and (not isinstance(usage[key], float) or isfinite(usage[key]))
            for key in ('prompt_tokens', 'input_tokens', 'completion_tokens',
                        'output_tokens', 'cache_read_input_tokens',
                        'cache_read_tokens', 'cache_creation_input_tokens',
                        'cache_write_tokens')
        ):
            continue
        try:
            dispatch = usage.get('_dispatch')
            dispatch = dispatch if isinstance(dispatch, dict) else {}
            cost = compute_cost(
                usage, model_id=(row.get('model') or dispatch.get('logical_model')
                                 or dispatch.get('model') or model or ''),
                provider_id=(row.get('provider_id') or row.get('providerId')
                             or dispatch.get('provider_id') or None),
            )
            if cost is not None:
                if is_settled(row):
                    row = {**row, 'cost': cost}
                else:
                    row['cost'] = cost
                api_rounds[index] = settle_or_none(row) or row
        except Exception as exc:
            # Cost display is best-effort, never a reason to lose paid usage or
            # prevent child terminal delivery. Unpriced rows remain explicit.
            logger.warning('[Cost] child round snapshot failed: %s', exc)


def usage_with_round_costs(usage, api_rounds):
    """Fold request-local prices, never reprice cumulative restored tokens."""
    from math import isfinite
    result = dict(usage or {})
    priced = []
    for row in api_rounds:
        if not isinstance(row, dict):
            continue
        reported = (row.get('usage') or {}).get('cost_usd')
        cost = reported if reported is not None else (row.get('cost') or {}).get('costUsd')
        if type(cost) in (int, float) and isfinite(cost) and cost >= 0:
            priced.append(cost)
    if priced:
        result['cost_usd'] = sum(priced)
    return result


def stamp_terminal_cost(task, done_event):
    """Annotate display costs without publishing or settling a financial account."""
    # ── Stamp cost snapshot on the done event ──
    # Cost depends only on each round's usage + model + provider + the active
    # pricing table, all of which are final at this point. Price each round
    # before folding the display total so compaction/fallback models cannot be
    # silently repriced as the terminal model. Sending it on the done event eliminates the
    # per-render `/api/v1/messages/cost` round-trips on the LIVE path —
    # the persisted-cost write covers reload paths.
    try:
        from lib.conversation_sync.settled_rows import is_settled, settle
        from lib.cost import (
            compute_api_rounds_cost as _compute_api_rounds_cost,
            compute_cost as _compute_cost,
        )
        _rounds = done_event.get('apiRounds') or []
        for _index, _rd in enumerate(_rounds):
            if not isinstance(_rd, dict) or _rd.get('cost') or task.get('_nativeFrozenCostProjection') is True:
                continue
            _ru = _rd.get('usage') or {}
            if not _ru:
                continue
            _rc = _compute_cost(
                _ru,
                model_id=_rd.get('model') or done_event.get('model') or '',
                provider_id=(_rd.get('provider_id')
                              or _rd.get('providerId')
                              or task.get('provider_id') or None),
            )
            if _rc:
                if is_settled(_rd):
                    # The ledger list is shared with the task; replace the
                    # settled round copy-on-write in that same list.
                    _rounds[_index] = settle({**_rd, 'cost': _rc})
                else:
                    _rd['cost'] = _rc
        _msg_cost = _compute_api_rounds_cost(
            done_event.get('apiRounds'),
            fallback_model_id=(
                done_event.get('model') or task.get('model') or ''),
            fallback_provider_id=task.get('provider_id') or None,
            use_frozen_costs=task.get('_nativeFrozenCostProjection') is True,
        )
        if _msg_cost is None and done_event.get('usage') and task.get('_nativeFrozenCostProjection') is not True:
            _msg_cost = _compute_cost(
                done_event['usage'],
                model_id=done_event.get('model') or task.get('model') or '',
                provider_id=task.get('provider_id') or None,
            )
        if _msg_cost:
            done_event['cost'] = _msg_cost
    except Exception as _ce:
        logger.warning('[Cost] done-event stamp failed (non-fatal): %s', _ce)

