"""lib/llm_sanitize/_toolcalls.py — Anthropic-strict tool-call/result repair.

Fixes orphaned tool_use/tool_result blocks and enforces the Anthropic
adjacency requirement (tool results must immediately follow their tool_use).
"""

from lib.log import get_logger

from lib.llm_sanitize._fields import _strip_tool_calls

logger = get_logger(__name__)


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
