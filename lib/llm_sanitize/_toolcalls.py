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
      * tool message with empty/missing ``tool_call_id`` → positionally
        paired with the preceding assistant message's next unclaimed id;
        unpairable → dropped (probe M — "tool_call_id is not found")

    Normalised for cross-vendor safety (tolerated by kimi, rejected by
    Anthropic's name pattern):
      * name invalid chars → ``'_'``, clamped to 64 chars (probe I showed
        kimi accepts ``antml:thinking``; Anthropic does not)
      * tool_call ``id`` empty/missing → minted ``call_<sha1[:12]>``
        DETERMINISTICALLY from (message idx, call idx, content) — kimi
        tolerates ``''`` (probe J), but the orphan/adjacency fixer pairs
        BY id; and a random mint would break the cache prefix every round
        because the deep-copy seam never writes the heal back

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
    paired_tid = dropped_entry = dropped_tool = 0
    name_locations = []

    out = []
    unclaimed = []  # ids of the current assistant → tool* run, not yet matched
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            out.append(msg)
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
            for tc_i, tc in enumerate(tcs):
                if not isinstance(tc, dict):
                    dropped_entry += 1
                    continue
                tcid = tc.get('id')
                if tcid is not None and not isinstance(tcid, str):
                    tc['id'] = str(tcid)
                    fixed_id += 1
                elif not tcid:
                    tc['id'] = _mint_tool_call_id(idx, tc_i, tc)
                    fixed_id += 1
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
            if kept:
                msg['tool_calls'] = kept
                unclaimed = [tc.get('id') for tc in kept if tc.get('id')]
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
                    msg['tool_call_id'] = unclaimed.pop(0)
                    paired_tid += 1
                else:
                    # Protocol-dead: no vendor accepts a tool message with an
                    # unresolvable id (probe M), and the orphan fixer only
                    # drops truthy ids — so the drop must happen here.
                    dropped_tool += 1
                    continue
            elif tcid in unclaimed:
                unclaimed.remove(tcid)
            out.append(msg)
            continue

        # Any other message breaks an assistant → tool adjacency run.
        unclaimed = []
        out.append(msg)

    total = (fixed_name + fixed_type + fixed_args + fixed_id
             + paired_tid + dropped_entry + dropped_tool)
    if total:
        logger.warning(
            '[build_body] Healed tool_call wire shape: name=%d type=%d '
            'args=%d id=%d paired_tool_id=%d dropped_entries=%d '
            'dropped_tool_msgs=%d%s — strict vendors hard-400 the whole '
            'request on these (Kimi "tokenization failed", 2026-08-07)',
            fixed_name, fixed_type, fixed_args, fixed_id, paired_tid,
            dropped_entry, dropped_tool,
            (f' (name fixes at msg {", ".join(name_locations)})'
             if name_locations else ''))
    return out


# ══════════════════════════════════════════════════════════
#  Tool-call/result repair (Anthropic-strict)
# ══════════════════════════════════════════════════════════

def _fix_orphaned_tool_calls(messages: list) -> list:
    """Remove or fix assistant messages with tool_calls that lack matching tool_results.

    Claude/Anthropic API requires every tool_use block to have a corresponding
    tool_result in the immediately following message.  If a task was aborted
    mid-tool-call, the stored/persisted messages may contain orphaned tool_use
    blocks.  This causes HTTP 400:
      "tool_use ids were found without tool_result blocks immediately after"

    Strategy:
      1. Collect all tool_call IDs from assistant messages
      2. Collect all tool_call_ids from tool-role messages
      3. For any assistant message whose tool_calls ALL lack matching tool_results,
         strip the tool_calls (keep content if any, else remove the message)
      4. Remove any tool-role messages that reference non-existent tool_calls
      5. Validate adjacency: tool results must immediately follow their tool_calls
         (Anthropic requires this, even if matching IDs exist elsewhere)

    Returns a new list (non-mutating).
    """
    if not messages:
        return messages

    # ── Pass 1: Collect all tool_call IDs and tool_result IDs ──
    tool_call_ids = set()
    tool_result_ids = set()
    for msg in messages:
        if msg.get('role') == 'tool' and msg.get('tool_call_id'):
            tool_result_ids.add(msg['tool_call_id'])
        tcs = msg.get('tool_calls')
        if tcs and msg.get('role') == 'assistant':
            for tc in tcs:
                if tc.get('id'):
                    tool_call_ids.add(tc['id'])

    # ── Pass 2: Strip orphaned tool_calls and orphaned tool_results ──
    fixed = []
    orphan_tc_count = 0
    orphan_tr_count = 0
    for msg in messages:
        # Remove orphaned tool results (role=tool without matching tool_call)
        if msg.get('role') == 'tool':
            tcid = msg.get('tool_call_id')
            if tcid and tcid not in tool_call_ids:
                orphan_tr_count += 1
                logger.debug('[build_body] Dropping orphaned tool_result tc_id=%.16s '
                             '(no matching tool_call)', tcid)
                continue
            fixed.append(msg)
            continue

        tcs = msg.get('tool_calls')
        if not tcs or msg.get('role') != 'assistant':
            fixed.append(msg)
            continue

        # Separate matched vs orphaned tool_calls
        matched_tcs = [tc for tc in tcs if tc.get('id') in tool_result_ids]
        orphaned_tcs = [tc for tc in tcs if tc.get('id') not in tool_result_ids]

        if not orphaned_tcs:
            # All tool_calls have results — keep as-is
            fixed.append(msg)
        elif matched_tcs:
            # Some matched, some orphaned — keep only matched
            new_msg = dict(msg)
            new_msg['tool_calls'] = matched_tcs
            fixed.append(new_msg)
            orphan_tc_count += len(orphaned_tcs)
        else:
            # ALL tool_calls are orphaned — strip tool_calls but keep content
            # AND reasoning fields (thinking replay needs them).
            content = msg.get('content')
            if content:
                fixed.append(_strip_tool_calls(msg))
            # If no content either, we drop the message entirely
            orphan_tc_count += len(orphaned_tcs)

    if orphan_tc_count:
        logger.warning(
            '[build_body] Fixed %d orphaned tool_call(s) without matching tool_result '
            '— stripped to prevent Claude HTTP 400', orphan_tc_count)
    if orphan_tr_count:
        logger.warning(
            '[build_body] Removed %d orphaned tool_result(s) without matching tool_call',
            orphan_tr_count)

    # ── Pass 3: Validate adjacency ──
    # Anthropic requires tool_result blocks to be immediately after the
    # assistant message containing the corresponding tool_use.  If an
    # assistant message with tool_calls is NOT immediately followed by
    # tool-role messages with matching IDs, fix by reordering or stripping.
    fixed = _fix_tool_call_adjacency(fixed)

    return fixed


def _fix_tool_call_adjacency(messages: list) -> list:
    """Ensure tool results immediately follow their assistant tool_calls.

    Anthropic requires tool_result blocks in the message immediately after
    the tool_use.  OpenAI is more lenient (results can be anywhere after).
    This function validates and fixes adjacency:
      - For each assistant message with tool_calls, check that the next N
        messages (where N = number of tool_calls) are role=tool with matching IDs.
      - If tool results are present but out of order, reorder them.
      - If tool results are missing from the immediately following position,
        strip the tool_calls from the assistant message.

    Returns a new list.
    """
    if not messages:
        return messages

    result = list(messages)
    fix_count = 0

    i = 0
    while i < len(result):
        msg = result[i]
        tcs = msg.get('tool_calls')
        if not tcs or msg.get('role') != 'assistant':
            i += 1
            continue

        # Collect expected tool_call IDs
        expected_ids = {tc.get('id') for tc in tcs if tc.get('id')}
        if not expected_ids:
            i += 1
            continue

        # Check the next N messages are tool results with matching IDs.
        # Bound the scan by the number of id-bearing tool_calls (NOT the
        # deduplicated id set): duplicate/empty ids would shrink the set and
        # stop the scan before all genuinely-adjacent tool results are
        # consumed, causing already-adjacent results to be re-moved.
        n_expected = sum(1 for tc in tcs if tc.get('id'))
        following_tool_ids = set()
        j = i + 1
        while j < len(result) and j - i - 1 < n_expected:
            fmsg = result[j]
            if fmsg.get('role') != 'tool':
                break
            tcid = fmsg.get('tool_call_id')
            if tcid in expected_ids:
                following_tool_ids.add(tcid)
            j += 1

        missing_ids = expected_ids - following_tool_ids
        if not missing_ids:
            # All tool results are adjacent — good
            i = j
            continue

        # Some tool results are not adjacent — search for them elsewhere
        found_elsewhere = {}
        for k in range(j, len(result)):
            if result[k].get('role') == 'tool':
                tcid = result[k].get('tool_call_id')
                if tcid in missing_ids:
                    found_elsewhere[tcid] = k

        if found_elsewhere:
            # Move misplaced tool results to the correct position
            # Remove from original positions (in reverse order to preserve indices)
            moved_msgs = []
            for _idx in sorted(found_elsewhere.values(), reverse=True):
                moved_msgs.insert(0, result.pop(_idx))
            # Insert them right after the assistant message (after existing adjacent tools)
            insert_pos = i + 1 + len(following_tool_ids)
            for m in moved_msgs:
                result.insert(insert_pos, m)
                insert_pos += 1
            fix_count += len(moved_msgs)
            logger.warning(
                '[build_body] Reordered %d tool_result(s) to be adjacent to '
                'their tool_calls (Anthropic adjacency fix)',
                len(moved_msgs))
        else:
            # Tool results genuinely missing — strip orphaned tool_calls
            still_matched = [tc for tc in tcs if tc.get('id') not in missing_ids]
            if still_matched:
                result[i] = dict(msg)
                result[i]['tool_calls'] = still_matched
            else:
                content = msg.get('content')
                if content:
                    result[i] = _strip_tool_calls(msg)
                else:
                    result.pop(i)
                    continue  # Don't increment i
            fix_count += len(missing_ids)
            logger.warning(
                '[build_body] Stripped %d tool_call(s) with non-adjacent results '
                '(Anthropic adjacency requirement)', len(missing_ids))

        i += 1

    if fix_count:
        logger.info('[build_body] Tool adjacency fixes applied: %d total', fix_count)

    return result
