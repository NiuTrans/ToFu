# HOT_PATH — functions in this module are called per-request.
# Prefer logger.debug() over logger.info(). logger.info() is reserved
# for rare, high-signal events (e.g. content-filter injection, per-round diagnostics).
"""Orchestrator reusable turn primitives.

``drain_peer_messages_into`` — driver-loop peer-inbox drain hook.
``_run_single_turn`` — one full LLM+tool cycle on an existing task dict
(the Flow engine drives any outer work→review→revise loop).

Both delegate to ``run_task`` in ``_run.py``.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.turn_verdict import terminal_finish_reason

logger = get_logger(__name__)


from lib.tasks_pkg.orchestrator._run import run_task


#  _run_single_turn — reusable building block for Flow nodes
# ══════════════════════════════════════════════════════════

def drain_peer_messages_into(task: dict[str, Any],
                             messages: list[dict[str, Any]], *,
                             round_label: int = 0) -> int:
    """Driver-loop peer-message drain hook (Pillar #6 fast path for big tasks).

    The main ``run_task`` round loop drains the peer inbox at each round
    boundary, but Flow and VU outer loops are DRIVER loops that own their own
    iteration boundary — they must call THIS at the top
    of each iteration so a peer message reaches the model on the NEXT iteration
    (as a tool turn), not only when the whole task ends.

    Contract (mirrors the run_task hook exactly so delivery is byte-identical):
      • Respects the unmatched-tool_call guard: if the last message is an
        assistant tool_call awaiting its tool_result, DEFER (return 0) — a peer
        turn must never split a tool_call/tool_result pair.
      • Drains ONLY ``peer-msg`` items, under ``_peer_drain_key`` (VU sub-task)
        or ``swarm_key_for(task)`` (conv-scoped, matches where the twin was
        enqueued), so a cross-iteration peer message is never stranded.
      • Appends ONE coalesced user message to ``messages`` and STASHES the
        drained items under ``task['_peer_inject_pending']``. It deliberately
        does NOT emit the PEER_INBOX_INJECT chip nor delete the durable rows —
        that DEFERRED flush is owned by the run_task the driver invokes for this
        iteration (it fires right after the LLM call confirms consumption), so
        the never-zero / exactly-once invariants are preserved unchanged.

    The caller MUST set ``task['_peer_driver_owned'] = True`` so the nested
    ``run_task`` does not ALSO drain peer items (which would double-drain).

    Returns the number of peer items injected (0 when none / deferred).
    """
    try:
        from lib.agent_inbox import drain as _drain_inbox
        from lib.swarm.integration import swarm_key_for as _swarm_key_for
        _last = messages[-1] if messages else None
        if (_last and _last.get('role') == 'assistant'
                and _last.get('tool_calls')):
            return 0  # unmatched tool_call — defer to the next boundary
        _key = task.get('_peer_drain_key') or _swarm_key_for(task)
        _peer_items = _drain_inbox(_key, modes=['peer-msg'])
        _peer_items = [it for it in _peer_items if it.get('value')]
        if not _peer_items:
            return 0
        messages.append({
            'role': 'user',
            'content': '\n\n'.join(it['value'] for it in _peer_items),
        })
        task.setdefault('_peer_inject_pending', []).extend(_peer_items)
        logger.info('[Task %s] driver-loop injected %d peer message(s) at '
                    'iteration %s', task.get('id', '?')[:8], len(_peer_items),
                    round_label)
        return len(_peer_items)
    except Exception as e:
        logger.error('[Task %s] driver-loop peer drain failed (continuing): %s',
                     task.get('id', '?')[:8], e, exc_info=True)
        return 0


def _run_single_turn(
    task: dict[str, Any],
    messages_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute ONE full work turn (LLM + tool loop) and return the results.

    This wrapper:
    1. Resets per-turn accumulation fields (content, thinking, usage, etc.)
    2. Optionally replaces the messages list
    3. Delegates to the full ``run_task`` machinery
    4. Returns dict with keys: content, thinking, usage, finishReason, messages, error

    **Note:** This mutates ``task`` in place (content, thinking, status, etc.).
    It does NOT emit 'done' events — the Flow caller decides when the overall
    run is done.

    Parameters
    ----------
    task : dict
        The live task dict (from ``create_task``).  Must already be in ``tasks``.
    messages_override : list | None
        If provided, replaces ``task['messages']`` before calling.

    Returns
    -------
    dict  with keys: content, thinking, usage, finishReason, messages, error
    """
    if 'id' not in task:
        raise ValueError("_run_single_turn called with a task dict missing 'id' — did you forget to use create_task()?")
    tid = task['id'][:8]
    logger.debug('[FlowTurn] _run_single_turn %s ENTRY — messages_override=%s',
                 tid, 'yes' if messages_override is not None else 'no')

    # Override messages if supplied
    if messages_override is not None:
        task['messages'] = list(messages_override)

    # Reset per-turn accumulation fields so run_task starts clean
    with task['content_lock']:
        task['content']  = ''
        task['thinking'] = ''
    task['usage']        = {}
    task['status']       = 'running'
    task['error']        = None
    task['finishReason'] = None
    task['toolRounds'] = []    # fresh tool rounds per turn
    task['programRuns'] = []   # canonical hosted-program runs per turn

    # Flag to tell run_task NOT to emit final 'done' event
    task['_flow_managed'] = True

    try:
        run_task(task)
    finally:
        task.pop('_flow_managed', None)

    result = {
        'content':      task.get('content', ''),
        'thinking':     task.get('thinking', ''),
        'usage':        task.get('usage', {}),
        'finishReason': terminal_finish_reason(task),
        'messages':     list(task.get('messages', [])),
        'error':        task.get('error'),
    }
    if task.get('streamState'):
        result['streamState'] = task['streamState']
    # Propagate fallback info so the Flow projection can surface it.
    if task.get('_fallback_model'):
        result['fallbackModel'] = task['_fallback_model']
        result['fallbackFrom']  = task.get('_fallback_from', '')
        if task.get('_fallback_reason'):
            result['fallbackReason'] = task['_fallback_reason']
        if task.get('_fallback_kind'):
            result['fallbackKind'] = task['_fallback_kind']

    logger.debug('[FlowTurn] _run_single_turn %s → %d chars, finish=%s',
                 tid, len(result['content']), result['finishReason'])
    return result
