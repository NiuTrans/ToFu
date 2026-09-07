"""lib/billing/request_flow.py — Per-request reserve/settle choreography.

The HTTP completion routes (``routes/api_v1/chat.py``,
``routes/api_v1/agent_run.py``, and the OpenAI/Anthropic compat
adapters) all follow the same three-step billing dance for multi-user
(credits) installs:

1. **Pre-flight reserve** — estimate the cost from prompt size + the
   caller's ``max_tokens`` cap and hold it as a ledger reservation under
   ``ref_id=task_id`` BEFORE dispatching. If the wallet can't cover the
   estimate, the route returns 402.
2. **Pre-dispatch release** — if admission, stream setup, or ``spawn_task``
   fails, refund the hold immediately instead of waiting for the janitor sweep.
3. **Post-terminal settle** — once the task is terminal, settle the
   reservation against the actual token usage (or, when no reservation
   was placed, debit directly).

Centralising the three steps here keeps the ``_billing_reservation_micro``
task-dict contract and the usage-key fallbacks (``input_tokens`` vs
``prompt_tokens``) in ONE place so the surfaces can't drift.

**Multi-user gate**: every helper short-circuits to a no-op when
``user_id`` is empty. Personal / private / open installs (no owning
user on the API key) therefore make ZERO ledger calls — behaviour is
identical to before billing existed.
"""

from __future__ import annotations

from typing import Optional

from lib.log import get_logger

logger = get_logger(__name__)


def estimate_prompt_tokens(messages: list) -> int:
    """Cheap prompt-token estimate from an OpenAI-style messages array.

    Counts characters across string and multimodal ``text`` parts and
    divides by 4 (the same rough heuristic the chat route used inline).
    Returns at least 1.
    """
    prompt_chars = 0
    for m in messages or ():
        if not isinstance(m, dict):
            continue
        c = m.get('content') or ''
        if isinstance(c, str):
            prompt_chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get('type') == 'text':
                    prompt_chars += len(part.get('text') or '')
    return max(1, prompt_chars // 4)


def reserve_for_task(task: dict, *, user_id: str, model: str,
                     prompt_tokens: int,
                     max_completion_tokens: int = 1024) -> int:
    """Place a pre-flight credit reservation for ``task``.

    Returns the reserved amount in micro-credits (0 = nothing reserved,
    either because ``user_id`` is empty or the estimator returned 0).
    Stores the amount on ``task['_billing_reservation_micro']`` so
    :func:`settle_task` can find it later.

    Raises ``lib.billing.InsufficientFunds`` when the wallet can't cover
    the estimate — the caller is expected to translate that into a 402.

    No-op when ``user_id`` is empty (personal/private/open install) OR
    when the relay runs in agent-only mode (``billing_enabled=false`` —
    users bring their own model keys and are never charged).
    """
    if not user_id:
        return 0
    from lib.relay_config import billing_enabled
    if not billing_enabled():
        return 0
    from lib.billing import estimate_request_cost, reserve
    micro = estimate_request_cost(
        model or '',
        prompt_tokens=max(1, int(prompt_tokens)),
        max_completion_tokens=int(max_completion_tokens or 1024),
        headroom=1.5)
    if micro > 0:
        reserve(user_id, micro, ref_id=task['id'],
                note=f'reserve {model or "?"}')
        task['_billing_reservation_micro'] = micro
    return micro


def release_reservation(task: dict, *, user_id: str,
                        reservation_micro: int,
                        raise_on_error: bool = False) -> bool:
    """Refund a pre-flight reservation (best-effort).

    Called when a request cannot start after a reservation was placed so the
    caller isn't left holding credits until the janitor sweep. Any failure here
    is logged, never raised — the janitor is the safety net.
    """
    if not user_id or reservation_micro <= 0:
        return True
    try:
        from lib.billing import reserve_release
        reserve_release(user_id, reservation_micro, ref_id=task['id'],
                        note='request did not dispatch; reserve released')
        return True
    except Exception as e:
        logger.error('[Billing] reserve release failed: %s', e, exc_info=True)
        if raise_on_error:
            raise
        return False


def settle_task(task: dict, *, user_id: str, model: str,
                raise_on_error: bool = False) -> Optional[dict]:
    """Settle (or debit) ``task`` against its actual token usage.

    Reads ``task['usage']`` tolerating both the native
    (``input_tokens``/``output_tokens``) and OpenAI-shaped
    (``prompt_tokens``/``completion_tokens``) key spellings.

    Returns a ``{cost_micro, [reserved_micro], balance_micro,
    matched_model}`` dict suitable for attaching to the response body,
    or ``None`` when nothing was billed (empty ``user_id``, zero cost,
    or an error — all logged).

    No-op when ``user_id`` is empty OR when the relay runs in agent-only
    mode (``billing_enabled=false``) — symmetric with
    :func:`reserve_for_task` so a deployment can flip billing off without
    stranding a reservation.
    """
    if not user_id:
        return None
    from lib.relay_config import billing_enabled
    if not billing_enabled():
        # A deployment can disable billing after this request reserved funds.
        # The reservation is still a durable debt and must be released instead
        # of waiting for a janitor that may also be disabled with billing.
        reserved = int(task.get('_billing_reservation_micro') or 0)
        if reserved > 0:
            release_reservation(
                task,
                user_id=user_id,
                reservation_micro=reserved,
                raise_on_error=raise_on_error,
            )
        return None
    try:
        from lib.billing import (
            InsufficientFunds, compute_request_cost, debit, settle,
        )
        u = task.get('usage') or {}
        # Pass the FULL usage shape — including cache_read / cache_write /
        # reasoning tokens — so the wallet debit matches the displayed cost.
        # Previously only input/output were forwarded, so a cache-heavy turn
        # was silently under-debited vs the ¥ shown in the UI. normalize_usage
        # also maps vendor spellings (kimi cached_tokens, DeepSeek
        # prompt_cache_hit_tokens, nested prompt_tokens_details) onto the
        # canonical keys so those hits are billed at the read multiplier.
        cost = compute_request_cost(
            model or '', provider_id=task.get('provider_id') or None,
            raw_usage=u)
        import json
        snapshot = dict(cost.snapshot)
        task['_billing_cost_snapshot'] = snapshot
        note_payload = {
            'v': 1, 'm': model or '', 'src': snapshot.get('source'),
            'p': snapshot.get('selectedPromptTokens'),
            't': snapshot.get('tierId'), 'lim': snapshot.get('maxPromptTokens'),
            'r': snapshot.get('rates'), 'c': snapshot.get('componentsMicro'),
            'total': cost.micro,
        }
        note = json.dumps(note_payload, separators=(',', ':'),
                          ensure_ascii=True)[:900]
        reserved = int(task.get('_billing_reservation_micro') or 0)
        if reserved > 0:
            snap = settle(user_id, reserved_micro=reserved,
                          actual_micro=cost.micro, ref_id=task['id'],
                          note=note)
            return {'cost_micro': cost.micro, 'reserved_micro': reserved,
                    'balance_micro': snap.balance_micro,
                    'matched_model': cost.matched_model,
                    'cost_snapshot': snapshot}
        if cost.micro > 0:
            try:
                snap = debit(user_id, cost.micro, kind='debit',
                             ref_type='task', ref_id=task['id'],
                             note=note,
                             allow_negative=True)  # never reject after work
                return {'cost_micro': cost.micro,
                        'balance_micro': snap.balance_micro,
                        'matched_model': cost.matched_model,
                        'cost_snapshot': snapshot}
            except InsufficientFunds as e:
                logger.warning('[Billing] debit failed: %s', e)
    except Exception as e:
        logger.error('[Billing] settle error: %s', e, exc_info=True)
        if raise_on_error:
            raise
    return None


__all__ = ['estimate_prompt_tokens', 'reserve_for_task',
           'release_reservation', 'settle_task']
