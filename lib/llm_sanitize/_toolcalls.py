"""lib/llm_sanitize/_toolcalls.py — Anthropic-strict tool-call/result repair.

Fixes orphaned tool_use/tool_result blocks and enforces the Anthropic
adjacency requirement (tool results must immediately follow their tool_use).
"""

from lib.log import get_logger

from lib.llm_sanitize._fields import _strip_tool_calls

logger = get_logger(__name__)

import hashlib
import json
import re
from collections import defaultdict, deque


# ══════════════════════════════════════════════════════════
#  Wire-shape protocol healer (any-model)
# ══════════════════════════════════════════════════════════

#: Placeholder stamped on tool_calls whose function name is empty/missing.
#: Kimi hard-400s the WHOLE request on it ("Invalid request: tokenization
#: failed" — live-verified 2026-08-07: task 9a8196f3 R4 was rejected on both
#: gateway keys; probe matrix B/L). Matches Anthropic's ^[a-zA-Z0-9_-]{1,64}$
#: as well, and the paired tool receipt still tells the model the call never
#: ran, so no information is lost.
_UNNAMED_TOOL_NAME = 'unnamed_tool_call'

#: Strictest vendor name contract (Anthropic): ^[a-zA-Z0-9_-]{1,64}$.
_TOOL_NAME_VALID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')
_TOOL_NAME_INVALID_RE = re.compile(r'[^a-zA-Z0-9_-]')
_TOOL_NAME_MAX = 64


def _mint_tool_call_id(msg_idx: int, call_idx: int, tc: dict) -> str:
    """Deterministic id for an id-less tool_call: ``call_<sha1[:12]>`` over
    (message index, call index, id-less canonical JSON of the call).

    ``_strip_non_api_fields`` DEEP-copies, so this heal never reaches the
    source messages — a random mint would give the same persisted call a
    DIFFERENT wire id on every round, breaking the prompt-cache prefix and
    tripping cache_tracking ``body_identical=false`` for exactly the
    non-parse producers (Flow nodes, compat shims, legacy history) this
    healer exists to protect (owner review 2026-08-07: two build_body runs
    over one id-less call minted call_d35b67054ae1 vs call_426a3d3ea09a).
    Deriving from (position, content) makes the mint idempotent across
    rounds; identical id-less twins in one message still get distinct ids
    via the call index.
    """
    payload = json.dumps({k: v for k, v in tc.items() if k != 'id'},
                         sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha1(
        f'{msg_idx}:{call_idx}:{payload}'.encode('utf-8')).hexdigest()
    return f'call_{digest[:12]}'


def _mint_unique_tool_call_id(
    msg_idx: int, call_idx: int, tc: dict, claimed_ids: set[str],
) -> str:
    """Mint one deterministic call id that cannot collide on this wire."""
    base = _mint_tool_call_id(msg_idx, call_idx, tc)
    candidate = base
    occurrence = 1
    while candidate in claimed_ids:
        occurrence += 1
        candidate = f'{base}_{occurrence}'
    return candidate


def _fix_tool_call_wire_shape(messages: list) -> list:
    """Heal OpenAI-style tool_call protocol violations on the wire.

    Single chokepoint — runs in ``build_body`` for EVERY model and EVERY
    producer (fresh stream rounds, persisted history, Flow nodes, swarm,
    compat shims). Every rule below is backed by a live probe against
    kimi-k3 (2026-08-07 matrix, ``max_tokens=1``):

    Healed (proven HTTP 400 on the strict vendor):
      * ``function.name`` empty/missing → ``_UNNAMED_TOOL_NAME``
        (probes B/L — "tokenization failed" / "name can't be blank")
      * ``type`` missing or ≠ ``'function'`` → ``'function'`` (probe H —
        "tokenization failed")
      * ``function.arguments`` dict/list → JSON string; None/non-str scalar
        → ``'{}'`` (probe K — "expected type string")
      * tool message ``tool_call_id`` → occurrence-paired only within its
        immediately preceding assistant/tool run; blank IDs use the next
        unclaimed occurrence, and all unpairable/duplicate receipts are
        dropped (probe M — "tool_call_id is not found")

    Normalised for cross-vendor safety (tolerated by kimi, rejected by
    Anthropic's name pattern):
      * name invalid chars → ``'_'``, clamped to 64 chars (probe I showed
        kimi accepts ``antml:thinking``; Anthropic does not)
      * tool_call ``id`` empty/missing → minted ``call_<sha1[:12]>``
        DETERMINISTICALLY from (message idx, call idx, content) — kimi
        tolerates ``''`` (probe J), but the orphan/adjacency fixer pairs
        BY id; and a random mint would break the cache prefix every round
        because the deep-copy seam never writes the heal back
      * duplicate truthy ids → later occurrences are deterministically
        reminted and their adjacent tool receipts are rewritten positionally.
        Provider ids are correlation tokens, not unique execution authority;
        legacy positional-id histories may reuse them across many rounds.

    Deliberately NOT touched (live-probed accepted — healing them would
    change behaviour on a guess and destroy evidence):
      * ``arguments=''`` (probe E), invalid-JSON argument strings (probe G —
        the round-level ``sanitize_malformed_tool_call_args`` already heals
        fresh rounds and keeps the raw-args evidence), scalar JSON (probe F),
        lone surrogates in content (probe C).

    Mutates nested dicts in place (same self-healing-history semantics as
    ``sanitize_malformed_tool_call_args``); returns a NEW list because
    unpairable tool messages may be dropped. Runs BEFORE
    ``_fix_orphaned_tool_calls`` so pairing there sees the healed ids.
    """
    if not messages:
        return messages

    fixed_name = fixed_type = fixed_args = fixed_id = 0
    paired_tid = dropped_entry = dropped_message = dropped_tool = 0
    name_locations = []

    out = []
    # (provider/original id, effective unique id) for the current adjacent run.
    unclaimed: list[tuple[str, str]] = []
    claimed_ids: set[str] = set()
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            # A malformed persisted carrier must not crash later ``.get``
            # passes or reach a strict provider as a non-message JSON value.
            dropped_message += 1
            unclaimed = []
            continue
        role = msg.get('role')

        if role == 'assistant' and 'tool_calls' in msg:
            tcs = msg.get('tool_calls')
            if not isinstance(tcs, list) or not tcs:
                # Non-list / empty tool_calls is never protocol-valid; the
                # message stands on its content alone.
                msg.pop('tool_calls', None)
                unclaimed = []
                out.append(msg)
                continue
            kept = []
            kept_claims: list[tuple[str, str]] = []
            for tc_i, tc in enumerate(tcs):
                if not isinstance(tc, dict):
                    dropped_entry += 1
                    continue
                tcid = tc.get('id')
                if tcid is not None and not isinstance(tcid, str):
                    tc['id'] = str(tcid)
                    fixed_id += 1
                original_id = str(tc.get('id') or '')
                if not original_id or original_id in claimed_ids:
                    tc['id'] = _mint_unique_tool_call_id(
                        idx, tc_i, tc, claimed_ids)
                    fixed_id += 1
                effective_id = str(tc['id'])
                claimed_ids.add(effective_id)
                if tc.get('type') != 'function':
                    tc['type'] = 'function'
                    fixed_type += 1
                fn = tc.get('function')
                if not isinstance(fn, dict):
                    fn = {}
                    tc['function'] = fn
                name = fn.get('name')
                if not isinstance(name, str) or not name.strip():
                    fn['name'] = _UNNAMED_TOOL_NAME
                    fixed_name += 1
                    if len(name_locations) < 6:
                        name_locations.append(f'#{idx}')
                elif not _TOOL_NAME_VALID_RE.match(name):
                    fn['name'] = _TOOL_NAME_INVALID_RE.sub(
                        '_', name)[:_TOOL_NAME_MAX]
                    fixed_name += 1
                    if len(name_locations) < 6:
                        name_locations.append(f'#{idx}')
                args = fn.get('arguments')
                if isinstance(args, (dict, list)):
                    fn['arguments'] = json.dumps(args, ensure_ascii=False)
                    fixed_args += 1
                elif args is None or not isinstance(args, str):
                    fn['arguments'] = '{}'
                    fixed_args += 1
                kept.append(tc)
                kept_claims.append((original_id, effective_id))
            if kept:
                msg['tool_calls'] = kept
                unclaimed = kept_claims
            else:
                msg.pop('tool_calls', None)
                unclaimed = []
            out.append(msg)
            continue

        if role == 'tool':
            tcid = msg.get('tool_call_id')
            if tcid is not None and not isinstance(tcid, str):
                tcid = str(tcid)
                msg['tool_call_id'] = tcid
                fixed_id += 1
            if not tcid:
                if unclaimed:
                    _original_id, effective_id = unclaimed.pop(0)
                    msg['tool_call_id'] = effective_id
                    paired_tid += 1
                else:
                    # Protocol-dead: no vendor accepts a tool message with an
                    # unresolvable id (probe M), and the orphan fixer only
                    # drops truthy ids — so the drop must happen here.
                    dropped_tool += 1
                    continue
            else:
                matching_index = next((
                    claim_index
                    for claim_index, (original_id, effective_id)
                    in enumerate(unclaimed)
                    if tcid in {original_id, effective_id}
                ), None)
                if matching_index is not None:
                    _original_id, effective_id = unclaimed.pop(matching_index)
                    if tcid != effective_id:
                        msg['tool_call_id'] = effective_id
                        fixed_id += 1
                else:
                    # Never borrow a globally equal id from another round. A
                    # second receipt for one occurrence or a non-adjacent
                    # result is protocol-dead and must fail closed here.
                    dropped_tool += 1
                    continue
            out.append(msg)
            continue

        # Any other message breaks an assistant → tool adjacency run.
        unclaimed = []
        out.append(msg)

    total = (fixed_name + fixed_type + fixed_args + fixed_id
             + paired_tid + dropped_entry + dropped_message + dropped_tool)
    if total:
        logger.warning(
            '[build_body] Healed tool_call wire shape: name=%d type=%d '
            'args=%d id=%d paired_tool_id=%d dropped_entries=%d '
            'dropped_messages=%d dropped_tool_msgs=%d%s — strict vendors '
            'hard-400 the whole '
            'request on these (Kimi "tokenization failed", 2026-08-07)',
            fixed_name, fixed_type, fixed_args, fixed_id, paired_tid,
            dropped_entry, dropped_message, dropped_tool,
            (f' (name fixes at msg {", ".join(name_locations)})'
             if name_locations else ''))
    return out


# ══════════════════════════════════════════════════════════
#  Tool-call/result repair (Anthropic-strict)
# ══════════════════════════════════════════════════════════

def _message_survives_tool_call_strip(message: dict) -> bool:
    """Whether an assistant still carries model-visible protocol payload."""
    if message.get('content'):
        return True
    return any(message.get(field) for field in (
        'function_call', 'reasoning_content', 'reasoning_details',
        'thinking_signature', '_responses_items', '_anthropic_content_blocks',
    ))


def _repair_adjacent_tool_runs(messages: list) -> list:
    """Occurrence-pair assistant calls with their adjacent result run.

    A provider call ID is a selector only inside one contiguous
    ``assistant(tool_calls) -> tool*`` run. Duplicate IDs pair FIFO by
    occurrence. Results after an intervening user/assistant/system message are
    never moved backwards: crossing that semantic boundary can make an old
    result settle the wrong action. Missing calls and unmatched/duplicate
    results are removed, while valid siblings and reasoning sidecars survive.
    """
    if not messages:
        return messages

    repaired: list[dict] = []
    stripped_calls = 0
    dropped_results = 0
    dropped_messages = 0
    position = 0
    while position < len(messages):
        message = messages[position]
        if not isinstance(message, dict):
            dropped_messages += 1
            position += 1
            continue

        role = message.get('role')
        if role == 'tool':
            # A tool message is valid only when consumed with the immediately
            # preceding assistant run; reaching it here proves it is orphaned.
            dropped_results += 1
            position += 1
            continue

        raw_calls = message.get('tool_calls')
        if role != 'assistant' or not raw_calls:
            repaired.append(message)
            position += 1
            continue
        if not isinstance(raw_calls, list):
            stripped_calls += 1
            stripped = _strip_tool_calls(message)
            if _message_survives_tool_call_strip(stripped):
                repaired.append(stripped)
            position += 1
            continue

        result_end = position + 1
        adjacent_results: list[dict] = []
        while result_end < len(messages):
            result = messages[result_end]
            if not isinstance(result, dict) or result.get('role') != 'tool':
                break
            adjacent_results.append(result)
            result_end += 1

        call_positions_by_id: dict[str, deque[int]] = defaultdict(deque)
        for call_position, call in enumerate(raw_calls):
            if not isinstance(call, dict):
                continue
            call_id = str(call.get('id') or '').strip()
            if call_id:
                call_positions_by_id[call_id].append(call_position)

        matched_call_positions: set[int] = set()
        matched_results: list[dict] = []
        for result in adjacent_results:
            result_id = str(result.get('tool_call_id') or '').strip()
            call_queue = call_positions_by_id.get(result_id)
            if not result_id or not call_queue:
                dropped_results += 1
                continue
            matched_call_positions.add(call_queue.popleft())
            matched_results.append(result)

        kept_calls = [
            call for call_position, call in enumerate(raw_calls)
            if call_position in matched_call_positions
        ]
        stripped_calls += len(raw_calls) - len(kept_calls)
        if kept_calls:
            if len(kept_calls) == len(raw_calls):
                repaired.append(message)
            else:
                kept_message = dict(message)
                kept_message['tool_calls'] = kept_calls
                repaired.append(kept_message)
            repaired.extend(matched_results)
        else:
            stripped = _strip_tool_calls(message)
            if _message_survives_tool_call_strip(stripped):
                repaired.append(stripped)

        position = result_end

    if stripped_calls:
        logger.warning(
            '[build_body] Stripped %d orphaned/non-adjacent tool_call(s); '
            'correlation is occurrence-local', stripped_calls)
    if dropped_results:
        logger.warning(
            '[build_body] Removed %d orphaned/duplicate tool_result(s); '
            'no cross-round ID borrowing', dropped_results)
    if dropped_messages:
        logger.warning(
            '[build_body] Removed %d malformed non-object message carrier(s)',
            dropped_messages)
    return repaired


def _fix_orphaned_tool_calls(messages: list) -> list:
    """Remove orphaned calls/results without guessing across message runs."""
    return _repair_adjacent_tool_runs(messages)


def _fix_tool_call_adjacency(messages: list) -> list:
    """Compatibility entry point for occurrence-local adjacency repair."""
    return _repair_adjacent_tool_runs(messages)
