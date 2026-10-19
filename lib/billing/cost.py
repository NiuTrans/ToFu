"""lib.billing.cost — Tokens-to-credits arithmetic (single-engine adapter).

No DB. Inputs are token counts; output is an integer micro-credit amount
including the configured relay margin.

Single source of truth (2026-06-24): the per-token RATE math lives in
ONE place — :func:`lib.cost.compute_cost` over the rich ``lib/pricing.py``
table (100+ models, Anthropic-vs-OpenAI cache-token convention detection,
per-provider cache multipliers, Qwen CNY tiers, provider-scoped overrides,
live USD→CNY rate). This module is now a THIN ADAPTER that:

  1. delegates the rate math to ``compute_cost`` so the wallet debit and the
     user-facing ¥/$ display can NEVER drift (they share the same engine);
  2. converts the resulting per-component USD into micro-credits;
  3. layers the relay profit margin on top (a billing-only concern that does
     NOT belong in the displayed price).

Before this, billing carried its OWN sparse price table in
``data/config/pricing.json`` (7 models → everything else fell to a generic
``default_model``) AND the settle path dropped ``cache_read``/``cache_write``
tokens entirely — so a cache-heavy turn was silently under-debited and the
debited amount disagreed with the displayed cost. ``pricing.json`` is now
used ONLY for ``default_margin`` (the relay markup); its per-model rate rows
are no longer consulted for cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext

from lib.billing.pricing import get_default_margin
from lib.cost import compute_cost

# Micro-credits per US dollar. EMPIRICALLY PINNED so existing wallet balances
# and the legacy pricing.json scale are preserved: the old table priced
# gpt-4o-mini input at 150_000 micro/Mtok for a real $0.15/Mtok list price →
# 1_000_000 micro per dollar. Verified by tests/test_billing.test_simple_compute
# (gpt-4o-mini 1000in+500out, margin 0 → exactly 450 µ). Do NOT change without
# migrating every stored balance.
MICRO_PER_USD = 1_000_000

# Back-compat alias (1 credit = 1e6 µ); kept for any external importer.
MICRO_PER_CREDIT = 1_000_000


@dataclass(frozen=True)
class CostBreakdown:
    """Result of :func:`compute_request_cost`."""
    micro: int                  # total micro-credits to debit
    base_micro: int             # before margin
    margin_micro: int           # added on top of base
    matched_model: str          # model id the cost engine priced
    components: dict            # {'input': µ, 'output': µ, 'cache_read': µ, ...}
    snapshot: dict              # immutable/explainable pricing decision


def _fee_row_digest(row):
    import hashlib
    import json
    return hashlib.sha256(json.dumps(row, sort_keys=True, allow_nan=False).encode()).digest()


def _transferred_child_row(row, parent_id):
    from copy import deepcopy
    result = deepcopy(row)
    result['sourceRound'] = row['round']
    result['billableTaskId'] = row.get('billableTaskId', parent_id)
    return result


@dataclass(frozen=True,slots=True,init=False)
class NativeChildFeeGrant:
    """Process-local witness over already settled child rows; never a new ledger."""
    parent_id:str
    owner:int
    child_id:str
    execution_parent_id:str
    rows:object
    projected_rows:object
    prefix_digest:bytes
    tokens:int

    def __init__(self,parent,child,account):
        from lib.agent_core.execution_budget import ExecutionBudget
        if not isinstance(account,ExecutionBudget):raise ValueError('native child fees require the owned account')
        actual_tokens=account.settled_child_invoice_tokens(parent,child)
        rows=child.get('apiRounds')
        if not isinstance(rows,list):raise ValueError('native child quote rows missing')
        detached=dict(child,_nativePricingRequired=False,_nativeFrozenCostProjection=True,_executionBudget=None)
        if rows:native_task_cost(detached,model=(child.get('config') or {}).get('model') or '')
        quoted=sum(row['nativePrice']['prompt_tokens']+(row['cost']['outputTokens'] if row['cost'] else 0) for row in rows)
        if quoted!=actual_tokens:
            raise ValueError('native child quote coverage differs from settled usage')
        prefix = child.get('_checkpointApiRounds') or []
        if prefix:
            inherited = account.recovered_actor_prefix_tokens(child, owner=parent['_userId'])
            prefix_tokens = sum(row['nativePrice']['prompt_tokens'] +
                (row['cost']['outputTokens'] if row['cost'] else 0) for row in prefix)
            if inherited is None or inherited != prefix_tokens:
                raise ValueError('native child fee prefix has no acknowledged account rebind')
        for index, row in enumerate(rows):
            payer = row.get('billableTaskId', parent['id'])
            if ('sourceRound' in row or not isinstance(row.get('taskId'), str) or not row['taskId']
                    or row['taskId'] == parent['id']
                    or not isinstance(payer, str) or not payer
                    or (index < len(prefix) and (payer in {parent['id'], child['id'], row['taskId']}
                                                or 'billableTaskId' not in row))
                    or (index >= len(prefix) and (row['taskId'] != child['id'] or payer != parent['id']))):
                raise ValueError('native child fee producer or original payer differs')
        object.__setattr__(self,'parent_id',parent['id'])
        object.__setattr__(self,'owner',parent['_userId'])
        object.__setattr__(self,'child_id',child['id'])
        object.__setattr__(self,'execution_parent_id',child['_parentTaskId'])
        from types import MappingProxyType
        object.__setattr__(self,'rows',MappingProxyType({row['round']:_fee_row_digest(row) for row in rows}))
        object.__setattr__(self,'projected_rows',MappingProxyType({
            (row['taskId'], row['round']): _fee_row_digest(_transferred_child_row(row, parent['id']))
            for row in rows}))
        object.__setattr__(self,'prefix_digest',_fee_row_digest(prefix))
        object.__setattr__(self,'tokens',actual_tokens)

    def source_rows(self, child):
        """Copy exact witnessed facts after completion; never revive the actor."""
        from copy import deepcopy
        rows = deepcopy(child.get('apiRounds'))
        prefix = deepcopy(child.get('_checkpointApiRounds') or [])
        if (child.get('id') != self.child_id or type(child.get('_userId')) is not int
                or child['_userId'] != self.owner
                or child.get('_parentTaskId') != self.execution_parent_id
                or getattr(child.get('_executionBudget'), 'root_id', None) != self.parent_id
                or not isinstance(rows, list)
                or self.rows != {row['round']: _fee_row_digest(row) for row in rows}
                or self.prefix_digest != _fee_row_digest(prefix)):
            raise ValueError('Native actor fee evidence changed after transfer')
        return rows

    def allows(self,parent,row):
        if (type(parent.get('_userId')) is not int
                or parent.get('id')!=self.parent_id or parent.get('_userId')!=self.owner):return False
        source_round=row.get('sourceRound')
        if type(source_round) is not int or source_round<1:return False
        restored=dict(row,round=source_round)
        return self.projected_rows.get((row.get('taskId'),source_round))==_fee_row_digest(restored)



@dataclass(frozen=True, slots=True, init=False)
class NativeRecoveredChildFeeGrant:
    """Exact retained child rows admitted after the owned account is restored."""
    parent_id: str
    owner: int
    rows: object

    def __init__(self, parent, rows, account_state):
        from types import MappingProxyType
        from lib.agent_core.execution_budget import ExecutionBudget
        account = parent.get('_executionBudget')
        if (not isinstance(account, ExecutionBudget)
                or not account.authorizes_model_owner(parent, parent.get('_userId'))
                or parent.get('_executionBudget') is not account):
            raise ValueError('recovered child fees require the restored owned account')
        account.validate_restored_shared_account(owner=parent['_userId'], task=parent, snapshot=account_state)
        accepted = {}
        for row in rows:
            if row.get('billableTaskId') != parent['id']:
                continue
            original = dict(row, round=row['sourceRound'])
            original.pop('sourceRound'); original.pop('billableTaskId')
            native_task_cost(dict(id=row['taskId'], apiRounds=[original],
                _nativeFrozenCostProjection=True), model=row['model'])
            key = (row['taskId'], row['sourceRound'])
            if key in accepted:
                raise ValueError('recovered child fee duplicated')
            accepted[key] = _fee_row_digest(row)
        object.__setattr__(self, 'parent_id', parent['id'])
        object.__setattr__(self, 'owner', parent['_userId'])
        object.__setattr__(self, 'rows', MappingProxyType(accepted))

    def allows(self, parent, row):
        if (parent.get('id') != self.parent_id or type(parent.get('_userId')) is not int
                or parent['_userId'] != self.owner):
            return False
        key = (row.get('taskId'), row.get('sourceRound'))
        return self.rows.get(key) == _fee_row_digest(row)


def transfer_native_child_fees(parent,child):
    """Append original quotes once to the parent's existing apiRounds projection."""
    from lib.cost import compute_api_rounds_cost
    account=parent.get('_executionBudget')
    if account is None:raise ValueError('native child fees require the shared account')
    from lib.agent_core.execution_budget import ExecutionBudget
    if not isinstance(account, ExecutionBudget):
        raise ValueError('native child fees require the owned account')
    with account.child_invoice_projection(parent, child):
        with parent.get('content_lock') or nullcontext():
            grants=parent.get('_nativeChildFeeGrants',())
            previous=next((value for value in grants if isinstance(value,NativeChildFeeGrant) and value.child_id==child['id']),None)
            if previous is not None:
                rows=child.get('apiRounds')
                if (type(parent.get('_userId')) is not int or type(child.get('_userId')) is not int
                        or parent.get('id')!=previous.parent_id or parent.get('_userId')!=previous.owner
                        or child.get('_userId')!=previous.owner or child.get('_parentTaskId')!=previous.execution_parent_id
                        or child.get('_executionBudget') is not account or not isinstance(rows,list)
                        or previous.rows!={row['round']:_fee_row_digest(row) for row in rows}
                        or previous.prefix_digest != _fee_row_digest(child.get('_checkpointApiRounds') or [])):
                    raise ValueError('native child fee retry changed its candidate')
                accepted=[row for row in parent.get('apiRounds',[])
                          if (row.get('taskId'),row.get('sourceRound')) in previous.projected_rows]
                if len(accepted)!=len(previous.rows) or not all(previous.allows(parent,row) for row in accepted):
                    raise ValueError('native parent lost its transferred child fee projection')
                child['_nativeChildFeeGrant'] = previous
                return
            grant=NativeChildFeeGrant(parent,child,account)
            rounds=parent.get('apiRounds') or []
            if not isinstance(rounds,list):raise ValueError('native parent quote rows invalid')
            updated=[*rounds]
            for original in child['apiRounds']:
                row=_transferred_child_row(original,parent['id'])
                row['round']=(updated[-1]['round'] if updated else 0)+1;updated.append(row)
            cost=compute_api_rounds_cost(updated,use_frozen_costs=True)
            parent['apiRounds']=updated;parent['cost']=cost
            parent['_nativeChildFeeGrants']=(*grants,grant)
            child['_nativeChildFeeGrant'] = grant


def native_task_cost(task: dict, *, model: str) -> CostBreakdown:
    """Fold settled Rust credits for the existing wallet; never reprice them."""
    if task.get('_nativePricingRequired') is not True and task.get('_nativeFrozenCostProjection') is not True:
        raise ValueError('native wallet cost requires frozen task projection')
    budget = task.get('_executionBudget')
    if task.get('_nativePricingRequired') is True and budget is None:
        raise ValueError('native wallet cost requires the owned financial account')
    account = None if budget is None else budget.consumption_snapshot()
    if account is not None and account[2] is not True:
        raise ValueError('native wallet cost has unknown financial usage')
    rounds = task.get('apiRounds', [])
    if not isinstance(rounds, list) or (not rounds and (account is None or account[0] != 0)):
        raise ValueError('native wallet cost requires settled round evidence')
    prefix = task.get('_checkpointApiRounds')
    prefix = [] if prefix is None else prefix
    if not isinstance(prefix, list) or len(prefix) > len(rounds):
        raise ValueError('native wallet checkpoint prefix differs from accepted round evidence')
    task_id = task.get('id')
    keys = {'input', 'output', 'cache_read', 'cache_write', 'base', 'margin', 'total'}
    totals = dict.fromkeys(keys, 0)
    previous = 0
    quoted_tokens = 0
    billable_rounds = 0
    transferred=set()
    for position, row in enumerate(rounds):
        if (not isinstance(row, dict) or type(row.get('round')) is not int
                or row['round'] <= previous or 'cost' not in row
                or not isinstance(row.get('nativePrice'), dict)):
            raise ValueError('native wallet round evidence invalid')
        credits = row.get('nativeCredits')
        if (not isinstance(credits, dict) or set(credits) != keys
                or any(type(value) is not int or not 0 <= value <= 9223372036854775807
                       for value in credits.values())
                or credits['base'] != sum(credits[key] for key in ('input', 'output', 'cache_read', 'cache_write'))
                or credits['total'] != credits['base'] + credits['margin']):
            raise ValueError('native wallet credits invalid')
        previous = row['round']
        price, amounts = row['nativePrice'], row['cost']
        prompt = price.get('prompt_tokens')
        if type(prompt) is not int or prompt < 0:
            raise ValueError('native wallet prompt evidence invalid')
        if amounts is None:
            if prompt or any(credits.values()):
                raise ValueError('native empty quote cannot carry a charge')
        elif (not isinstance(amounts, dict) or amounts.get('pricingSource') != 'native_resolved_price'
                or amounts.get('totalInputTokens') != prompt
                or type(amounts.get('outputTokens')) is not int or amounts['outputTokens'] < 0):
            raise ValueError('native wallet amount evidence invalid')
        quoted_tokens += prompt + (0 if amounts is None else amounts['outputTokens'])
        if position < len(prefix):
            if row != prefix[position]:
                raise ValueError('native wallet checkpoint prefix differs from accepted round evidence')
            source = row.get('taskId')
            if not isinstance(source, str) or not source or source == task_id:
                raise ValueError('native wallet resume requires distinct recorded executor provenance')
            # Old execution still consumes the shared token budget. Its wallet
            # debt belongs to its original task id, including lost-ACK recovery.
            continue
        if row.get('taskId', task_id) != task_id:
            grants=task.get('_nativeChildFeeGrants',())
            if not isinstance(row.get('taskId'),str) or type(row.get('sourceRound')) is not int:
                raise ValueError('native child quote provenance invalid')
            key=(row['taskId'],row['sourceRound'])
            if (key in transferred or not any(isinstance(grant,(NativeChildFeeGrant,NativeRecoveredChildFeeGrant)) and grant.allows(task,row) for grant in grants)):
                raise ValueError('native wallet fresh quote belongs to another executor')
            transferred.add(key)
            if row.get('billableTaskId') != task_id:
                # An exact witnessed historical row still consumes tokens, but
                # its debt remains with the original root across actor handoffs.
                continue
        billable_rounds += 1
        for key in keys:
            totals[key] += credits[key]
            if totals[key] > 9223372036854775807:
                raise ValueError('native wallet total exceeds storage integer range')
    if account is not None and account[0] != quoted_tokens:
        raise ValueError('native wallet quotes do not cover settled financial usage')
    components = {key: totals[key] for key in ('input', 'output', 'cache_read', 'cache_write')}
    snapshot = {'source': 'native_resolved_price', 'roundCount': billable_rounds,
                'componentsMicro': dict(components, baseMicro=totals['base'],
                    marginMicro=totals['margin'], totalMicro=totals['total'])}
    return CostBreakdown(totals['total'], totals['base'], totals['margin'], model, components, snapshot)


def _apply_margin(base_micro: int, margin: float) -> int:
    return int(base_micro * margin)


def _usd_to_micro(usd: float) -> int:
    """Convert a USD sub-cost to integer micro-credits.

    Rounds at the final step only. The caller MUST pass a per-component USD
    figure (``inputCostUsd`` etc., 9-dp precise) — NOT the top-level
    ``costUsd`` (rounded to 4 dp, which would lose sub-cent components, e.g.
    $0.00045 → $0.0004 → 400µ instead of 450µ).
    """
    return int(round((usd or 0.0) * MICRO_PER_USD))


def compute_request_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    provider_id: str | None = None,
    margin: float = -1.0,
    raw_usage: dict | None = None,
) -> CostBreakdown:
    """Compute the cost of a single completed LLM request, in micro-credits.

    Delegates rate math to :func:`lib.cost.compute_cost` (the single cost
    engine) so the debited amount and the displayed ¥/$ are computed by the
    SAME code over the SAME price table — they can never drift. Cache-read and
    cache-write tokens ARE billed (the engine knows each model's cache
    multipliers and detects the Anthropic-vs-OpenAI cache-token convention).

    Args:
        model: Model name; priced via the rich ``lib/pricing.py`` table.
        input_tokens: Prompt tokens (uncached portion for Anthropic, total for
            OpenAI — the engine auto-detects and never double-charges cache).
        output_tokens: Completion tokens.
        cache_read_tokens: Cache-hit tokens (billed at the model's read mul).
        cache_write_tokens: Cache-creation tokens (billed at the write mul).
        reasoning_tokens: Thinking tokens; billed as output when the model
            reports no separate completion tokens.
        provider_id: Optional provider for provider-scoped price overrides.
        margin: Override the relay margin. -1 = use pricing.json default;
            0.0 = no margin.

    Returns:
        :class:`CostBreakdown` with the integer micro-credit total.
    """
    if margin < 0:
        margin = get_default_margin()

    # Spell the scalars so the cost engine reads them under the RIGHT
    #   convention. Hardcoding the Anthropic key names here used to force an
    #   OpenAI TOTAL to be read as an uncached RESIDUAL, so the cache was
    #   added on top of a figure that already contained it and the wallet
    #   over-charged (gpt-4o 10000 total / 6000 cached billed 10000 uncached
    #   instead of 4000 — 52500µ vs the displayed 37500µ). synthesize_usage
    #   picks the spelling that preserves the scalars' meaning.
    from lib.cost import synthesize_usage
    usage = dict(raw_usage) if isinstance(raw_usage, dict) else synthesize_usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
    )
    cc = compute_cost(usage, model_id=model or '', provider_id=provider_id)

    if not cc:
        components = {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0}
    else:
        components = {
            'input':       _usd_to_micro(cc.get('inputCostUsd')),
            'output':      _usd_to_micro(cc.get('outputCostUsd')),
            'cache_read':  _usd_to_micro(cc.get('cacheReadCostUsd')),
            'cache_write': _usd_to_micro(cc.get('cacheWriteCostUsd')),
        }

    base = sum(components.values())
    margin_micro = _apply_margin(base, margin)
    return CostBreakdown(
        micro=base + margin_micro,
        base_micro=base,
        margin_micro=margin_micro,
        matched_model=model or '',
        components=components,
        snapshot={
            **dict((cc or {}).get('pricingSnapshot') or {}),
            'componentsMicro': dict(components),
            'baseMicro': base, 'marginMicro': margin_micro,
            'totalMicro': base + margin_micro,
        },
    )


def estimate_request_cost(
    model: str,
    *,
    prompt_tokens: int,
    max_completion_tokens: int = 1024,
    headroom: float = 1.5,
) -> int:
    """Pessimistic estimate for pre-flight reservation.

    The relay reserves ``estimate_request_cost(...)`` micro-credits BEFORE
    sending the upstream request; the gap between estimate and actual is
    refunded in :func:`lib.billing.wallet.settle`. ``headroom`` widens the
    estimate so a request whose completion is longer than expected still
    succeeds — at the cost of temporarily holding more of the user's
    balance during the call.

    Args:
        model: Model name.
        prompt_tokens: Counted prompt tokens (relay should compute these
                       cheaply via the local tokenizer before dispatch).
        max_completion_tokens: Caller's ``max_tokens`` cap, or a sane
                               default.
        headroom: Multiplier applied to the WHOLE estimate (1.5 = 50%
                  cushion). Set to 1.0 for "exact" reservation.
    """
    breakdown = compute_request_cost(
        model,
        input_tokens=prompt_tokens,
        output_tokens=max_completion_tokens,
    )
    return int(breakdown.micro * headroom)


# ── Conversion helpers (display only) ─────────────────────────────────

def micro_to_credits(micro: int) -> float:
    """Convert micro-credits → human-readable credit float."""
    return micro / MICRO_PER_CREDIT


def credits_to_micro(credits: float) -> int:
    """Convert credit float → micro-credits (rounded to integer)."""
    return int(round(credits * MICRO_PER_CREDIT))


def format_credits(micro: int, *, precision: int = 4) -> str:
    """Pretty-print a micro-credit amount as a credit string."""
    return f'{micro / MICRO_PER_CREDIT:.{precision}f}'


__all__ = [
    'CostBreakdown',
    'MICRO_PER_CREDIT',
    'compute_request_cost', 'estimate_request_cost',
    'micro_to_credits', 'credits_to_micro', 'format_credits',
]
