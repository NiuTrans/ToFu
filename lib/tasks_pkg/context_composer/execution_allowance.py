"""Bounded chronological observations of the existing execution allowance.

The account owns all accounting and clocks. This producer holds only an
injection watermark, never a parallel budget, dispatch grant or acceptance.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._models import ComposeRequest

from lib.agent_core.execution_budget import ModelBudgetExceeded, execution_allowance_snapshot


def _number(value: int) -> str:
    return str(value) if value < 10**12 else 'at least 1 trillion'


def render_allowance(snapshot: dict, round_num: int, *, prompt_input_tokens=None) -> str:
    """Pure, bounded rendering; no identities, paths, credentials or transcripts."""
    if not snapshot['token_bounded']:
        tokens = 'No finite token cap.'
    elif not snapshot['usage_known']:
        tokens = 'Token headroom unknown: reported consumption is incomplete.'
    else:
        tokens = f"Unreserved token headroom: {_number(snapshot['tokens_available'])}."
    tools = ('No finite tool cap.' if snapshot['tool_calls_available'] is None else
             f"Tool admission slots left: {_number(snapshot['tool_calls_available'])}.")
    clock = ('No execution deadline.' if snapshot['seconds_left'] is None else
             f"Whole seconds before the effective deadline: {_number(snapshot['seconds_left'])}.")
    forecast = _input_forecast(snapshot, prompt_input_tokens)
    return (
        f'Execution allowance observed at start of model round {_number(round_num)}; '
        'historical after that round; may change before dispatch.\n'
        f'{tokens} Shared observed consumption: {_number(snapshot["tokens_observed"])} '
        f'(lower bound). In-flight token reservations: {_number(snapshot["tokens_reserved"])} '
        '(not actual cost). Input and output both spend the token allowance.\n'
        f'{tools} Slots are never refunded. {clock}\n'
        f'{forecast}'
        'Choose the next work to preserve a reviewable deliverable and evidence before '
        'exhaustion. State incomplete checks honestly; keep all original acceptance '
        'requirements and read-only/tool permissions. This observation grants no '
        'dispatch: the runtime rechecks every model request and tool call. '
        'Perform required authorized archive/review handoffs before exhaustion; '
        'a working file or self-PASS does not prove completion.'
    )


def _input_forecast(snapshot: dict, prompt_input_tokens, *, compact=False) -> str:
    if type(prompt_input_tokens) is not int or prompt_input_tokens <= 0:
        return ''
    available = snapshot['tokens_available']
    if available is None:
        fit = 'unknown' if snapshot['token_bounded'] else 'unbounded'
    else:
        peers = available // prompt_input_tokens
        fit = f'<= {peers}' if peers < 10**12 else 'upper bound >=1e12 (exact count omitted)'
    if compact:
        return (f'Pre-note input {_number(prompt_input_tokens)}; comparable-input fit {fit} '
                '(exclude output/note/growth/adaptation/others). ')
    return (f'Pre-notice locally measured input, including messages/tools: {_number(prompt_input_tokens)} tokens. '
            f'Comparable-input fit: {fit}; excludes output, this notice, '
            'later context growth/provider adaptation and other work. No dispatch guarantee.\n')



def _compact_allowance(snapshot: dict, round_num: int, *, prompt_input_tokens=None) -> str:
    def number(value):
        return 'unlimited' if value is None else str(value) if value < 10**12 else '>=1e12'
    headroom = ('unknown' if snapshot['token_bounded'] and not snapshot['usage_known']
                else number(snapshot['tokens_available']))
    return (f'Round {number(round_num)} budget snapshot; may be stale: '
        f'unreserved tokens {headroom}, shared lower bound {number(snapshot["tokens_observed"])}, '
        f'reserved {number(snapshot["tokens_reserved"])} (not spend); '
        f'tool slots {number(snapshot["tool_calls_available"])}, whole seconds {number(snapshot["seconds_left"])}. '
        'Input/output share tokens. Preserve deliverable/evidence before exhaustion. '
        'Original checks/permissions stay. Runtime admits each call. '
        + _input_forecast(snapshot, prompt_input_tokens, compact=True))


def notice_decision(task: dict, messages: list[dict], round_num: int, *,
                    prompt_input_tokens=None) -> dict | None:
    """Compute the pending allowance notice WITHOUT appending it.

    Pure observation of the injection watermark: it never mutates ``task``,
    ``messages``, or the budget account. Prompt admission calls this BEFORE
    measuring so the appended notice is counted inside the single measured
    prompt instead of triggering a second full-prompt re-measurement. A None
    return is exact: ``inject_execution_allowance`` with the same inputs would
    append nothing.

    The ``'text'`` payload is byte-final whenever ``prompt_input_tokens`` is
    the admitted measurement, because ``render_allowance`` is pure. The
    watermark stamp stays owned by ``inject_execution_allowance``: admission
    MUST inject via the returned entrypoint (never ``append_injection`` with
    this text) so a revoked milestone cannot silently advance the watermark.
    """
    from . import can_append_injection
    if (task.get('aborted')
            or (task.get('abort_event') and task['abort_event'].is_set())
            or not can_append_injection(messages)):
        return None
    try:
        snapshot = execution_allowance_snapshot(task)
    except ModelBudgetExceeded:
        # Observation must not steal the existing cancellation/refusal owner.
        # The ordinary admission port still refuses this closed/foreign ID.
        return None
    if snapshot is None:
        return None
    prior = task.get('_executionAllowanceNotice') or {}
    initial_seconds = prior.get('initial_seconds', snapshot['seconds_left'])
    # Pending leases can refund or finish cheaply. They reduce the displayed
    # headroom, but cannot irreversibly advance a consumption milestone.
    percentages = [snapshot['token_percent_remaining'], snapshot['tool_percent_available']]
    if initial_seconds is not None and snapshot['seconds_left'] is not None:
        percentages.append(snapshot['seconds_left'] * 100 // initial_seconds if initial_seconds else 0)
    level = max((3 if p <= 5 else 2 if p <= 20 else 1 if p <= 50 else 0)
                for p in percentages if p is not None) if any(p is not None for p in percentages) else 0
    # Input growth can consume comparable-request capacity before the percentage
    # milestone. Use settled headroom here: pending leases may refund, so they
    # affect displayed fit but never advance this irreversible watermark.
    remaining = snapshot['tokens_remaining']
    if type(prompt_input_tokens) is int and prompt_input_tokens > 0 and remaining is not None:
        peers = remaining // prompt_input_tokens
        level = max(level, 3 if peers <= 1 else 2 if peers <= 3 else 1 if peers <= 6 else 0)
    if not snapshot['usage_known']:
        level = 4
    # Monotone watermark: at most four finite observations plus one unknown.
    # Compaction does not reset this cap or manufacture consumption evidence.
    if level <= prior.get('level', -1):
        return None
    return {'snapshot': snapshot, 'level': level, 'initial_seconds': initial_seconds,
            'text': render_allowance(snapshot, round_num, prompt_input_tokens=prompt_input_tokens)}


def inject_execution_allowance(task: dict, messages: list[dict], round_num: int, *,
                               prompt_input_tokens=None, decision: dict | None = None) -> None:
    if decision is None:
        decision = notice_decision(task, messages, round_num,
                                   prompt_input_tokens=prompt_input_tokens)
        if decision is None:
            return
    elif not isinstance(decision, dict):
        raise TypeError('decision must come from notice_decision')
    snapshot = decision['snapshot']
    level = decision['level']
    initial_seconds = decision['initial_seconds']
    from ._render import _count_tokens
    from .registry import injection_spec
    spec = injection_spec('execution_allowance')
    ceiling = spec.max_tokens
    model = str(task.get('model') or (task.get('config') or {}).get('model') or '')
    text = decision['text']
    if _count_tokens(text, model) > ceiling:
        text = _compact_allowance(snapshot, round_num, prompt_input_tokens=prompt_input_tokens)
    cost = _count_tokens(text, model)
    if cost > ceiling:
        from . import record_manifest_rows
        record_manifest_rows(task, [{'id': spec.id, 'source': spec.source,
            'authority': spec.authority, 'placement': 'tail',
            'stability': spec.stability, 'lifecycle': spec.lifecycle, 'appended': False,
            'injected': False, 'reason': 'notice_token_ceiling', 'tokens': cost,
            'chars': len(text), 'appendedTokens': 0, 'round': round_num}])
        return
    commit_allowance_notice(task, messages, text, level=level,
        initial_seconds=initial_seconds, round_num=round_num)


def _allowance_compose_request(task: dict) -> 'ComposeRequest':
    """Minimal ComposeRequest for the execution_allowance round block.

    The notice is a self-contained runtime observation; it carries no provider
    acquisition, so the request needs only the task (for model token counting
    and manifest recording) plus the model name.
    """
    from ._models import ComposeRequest
    model = str(task.get('model') or (task.get('config') or {}).get('model') or '')
    return ComposeRequest(task=task, model=model)


def commit_allowance_notice(task: dict, messages: list[dict], text: str, *,
                            level: int, initial_seconds,
                            round_num: int) -> dict | None:
    """Append one decided allowance notice and stamp its watermark.

    Only ``inject_execution_allowance`` and the single-measure admission seam
    call this; the decision must come from ``notice_decision`` so a revoked
    milestone can never silently advance the watermark without a fresh pure
    computation.

    The carrier is the registry's round-lifecycle block: each milestone is
    emitted through the renderer's content-addressed reuse, which supersedes
    the prior execution_allowance carrier instead of stacking a permanent
    ``_isMeta`` message per milestone. After the block commit the carrier is
    stamped with the injection identity/receipt the single-measure admission
    seam and the Inspector rely on (``_contextInjectionId`` /
    ``_contextInjection``).

    Returns the carrier message when the notice was committed this call, else
    None (an identical notice already present is content-addressed and reused
    without a new carrier).
    """
    from . import append_context_blocks
    from .registry import context_block, injection_spec
    from ._render import _count_tokens
    spec = injection_spec('execution_allowance')
    model = str(task.get('model') or (task.get('config') or {}).get('model') or '')
    result = append_context_blocks(
        messages, [context_block('execution_allowance', text)],
        _allowance_compose_request(task))
    row = next((r for r in result.manifest
                if r.get('id') == 'execution_allowance'), None)
    carrier = None
    if row is not None and row.get('appended'):
        # The renderer just appended this notice as a round-lifecycle block
        # carrier. Stamp the chronological injection identity and receipt the
        # single-measure admission seam and Inspector receipts expect.
        import hashlib
        import uuid
        for message in reversed(messages):
            blocks = message.get('_contextBlocks') if isinstance(message, dict) else None
            if blocks and any(b.get('id') == 'execution_allowance' for b in blocks):
                carrier = message
                break
        if carrier is not None:
            tokens = _count_tokens(text, model)
            receipt = {
                'id': spec.id, 'source': spec.source, 'authority': spec.authority,
                'placement': 'tail', 'stability': spec.stability,
                'lifecycle': spec.lifecycle, 'injected': True, 'appended': True,
                'delivery': 'appended_not_acknowledged', 'chars': len(text),
                'tokens': tokens,
                'hash': hashlib.sha256(text.encode('utf-8')).hexdigest()[:16],
                'reason': spec.trigger, 'round': round_num,
                'eventId': uuid.uuid4().hex, 'appendedTokens': tokens,
            }
            carrier['_contextInjectionId'] = 'execution_allowance'
            carrier['_contextInjection'] = receipt
    if carrier is not None:
        task['_executionAllowanceNotice'] = {'level': level,
                                             'initial_seconds': initial_seconds}
    return carrier


def _rerender_allowance_block(messages: list[dict], text: str, *,
                              model: str) -> dict | None:
    """Re-render the committed allowance block carrier in place with new text.

    The forecast level can only rise once the measured prompt input is known,
    so the single-measure admission seam re-renders the JUST-committed carrier
    with the measured input instead of appending a second notice. Rebuilding
    the full context envelope keeps the content-addressed hash honest (the
    renderer hashes the canonical envelope section, never raw text), so a later
    pass still supersedes this carrier instead of seeing a stale/foreign hash.

    Only the most recent allowance carrier is rewritten; returns it or None.
    """
    from ._render import _count_tokens, _envelope
    from .registry import injection_spec
    spec = injection_spec('execution_allowance')
    for message in reversed(messages):
        blocks = message.get('_contextBlocks') if isinstance(message, dict) else None
        if not (blocks and any(b.get('id') == 'execution_allowance' for b in blocks)):
            continue
        _block = next(b for b in blocks if b.get('id') == 'execution_allowance')
        # Rebuild the canonical envelope so the carrier's on-wire bytes match
        # what the renderer would emit for this exact notice text.
        from ._models import ContextBlock
        envelope_block = ContextBlock(
            id='execution_allowance', source=spec.source, content=text,
            authority=spec.authority, placement=spec.placement,
            stability=spec.stability, lifecycle=spec.lifecycle,
            priority=spec.priority, max_tokens=spec.max_tokens,
            layer=spec.layer, required=spec.required)
        rendered = _envelope(envelope_block, text)
        message['content'] = rendered
        tokens = _count_tokens(text, model)
        import hashlib
        message['_contextInjection'] = {
            **(message.get('_contextInjection') or {}),
            'chars': len(text), 'tokens': tokens, 'appendedTokens': tokens,
            'hash': hashlib.sha256(text.encode('utf-8')).hexdigest()[:16],
        }
        return message
    return None

def _rollback_allowance_notice(task: dict, messages: list[dict], *,
                               notice_len_before: int,
                               watermark_before,
                               budget_before,
                               lock_before,
                               manifest_before,
                               notes_before) -> None:
    """Roll a pre-measure notice commit back to its exact pre-state.

    The provider never saw this notice (final admission refused), so the
    milestone, account, receipts and injected notes stay unspent. Only the
    single-measure admission seam calls this after a refused
    ``enforce_dispatch_prompt_limit``.
    """
    del messages[notice_len_before:]
    task.pop('_executionAllowanceNotice', None)
    if watermark_before is not None:
        task['_executionAllowanceNotice'] = watermark_before
    task.pop('_executionBudget', None)
    if budget_before is not None:
        task['_executionBudget'] = budget_before
    task.pop('_executionBudgetLock', None)
    if lock_before is not None:
        task['_executionBudgetLock'] = lock_before
    task['_contextManifest'] = manifest_before
    task['_injectedNotes'] = notes_before
