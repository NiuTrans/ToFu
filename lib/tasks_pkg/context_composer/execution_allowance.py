"""Bounded chronological observations of the existing execution allowance.

The account owns all accounting and clocks. This producer holds only an
injection watermark, never a parallel budget, dispatch grant or acceptance.
"""
from __future__ import annotations

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


def inject_execution_allowance(task: dict, messages: list[dict], round_num: int, *, prompt_input_tokens=None) -> None:
    from . import append_injection, can_append_injection
    if (task.get('aborted')
            or (task.get('abort_event') and task['abort_event'].is_set())
            or not can_append_injection(messages)):
        return
    try:
        snapshot = execution_allowance_snapshot(task)
    except ModelBudgetExceeded:
        # Observation must not steal the existing cancellation/refusal owner.
        # The ordinary admission port still refuses this closed/foreign ID.
        return
    if snapshot is None:
        return
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
        return
    from ._render import _count_tokens
    from .registry import injection_spec
    spec = injection_spec('execution_allowance')
    ceiling = spec.max_tokens
    model = str(task.get('model') or (task.get('config') or {}).get('model') or '')
    text = render_allowance(snapshot, round_num, prompt_input_tokens=prompt_input_tokens)
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
    message = append_injection(messages, 'execution_allowance',
        text, task=task, round_num=round_num)
    if message is not None:
        task['_executionAllowanceNotice'] = {'level': level, 'initial_seconds': initial_seconds}
