"""Phase 4 last-resort head truncation — drop oldest non-system messages.

Byte-target and token-target variants both honour the objective anchor (the
first real user message / north-star goal) via ``_objective_anchor_index``
from the ``_layer2`` sub-package.

TOOL-PAIRING SAFETY (2026-07): head truncation drops the oldest messages, and
the oldest message of a tool-call round is the ``assistant(tool_calls)`` row
whose ``tool`` results follow it.  Popping that assistant one message at a time
and stopping when the size target is met can leave its ``tool`` results behind
with no parent ``assistant.tool_calls`` — an orphan that the upstream API
rejects with HTTP 400 (the exact failure this LAST-RESORT net exists to
prevent).  So the drop UNIT here is a whole round: dropping an
``assistant(tool_calls)`` always drops every immediately-following ``tool``
result with it (``_round_block_end``), mirroring the manual path's discipline
of only trimming on self-contained round boundaries.  A final defensive pass
(``_strip_leading_orphan_tools``) removes any ``tool`` result left at the head
with no open ``tool_call`` — belt-and-suspenders against a pre-existing
malformed head — so this net can NEVER itself produce a 400.
"""

import json

from lib.log import audit_log, get_logger
from lib.tasks_pkg.compaction._layer2._anchor import _objective_anchor_index
from lib.tasks_pkg.compaction._reactive._measure import _estimate_wire_bytes
from lib.tasks_pkg.compaction._tokens import (
    _estimate_msg_tokens,
    _estimate_total_tokens,
    _get_context_limit,
)

logger = get_logger(__name__)


def _round_block_end(messages: list, start: int) -> int:
    """Exclusive end index of the PAIRING-SAFE drop unit starting at ``start``.

    If ``messages[start]`` is an ``assistant`` carrying ``tool_calls``, the unit
    spans it PLUS every immediately-following ``tool`` result — so a tool-call
    round is dropped as a whole and no ``tool`` result is ever orphaned from its
    ``assistant.tool_calls`` parent.  Otherwise the unit is the single message.
    """
    m = messages[start]
    if isinstance(m, dict) and m.get('role') == 'assistant' and m.get('tool_calls'):
        end = start + 1
        n = len(messages)
        while end < n and isinstance(messages[end], dict) \
                and messages[end].get('role') == 'tool':
            end += 1
        return end
    return start + 1


def _strip_leading_orphan_tools(messages: list, system_end: int) -> int:
    """Remove any ``tool`` result at the head with no open ``tool_call`` parent.

    Defensive final pass: after block-dropping, the first live message (right
    after the system block / objective anchor) must never be a ``tool`` result
    whose ``assistant(tool_calls)`` was truncated away.  Walk the head and drop
    leading ``tool`` messages until a non-``tool`` message is reached.  Returns
    the number removed (normally 0 — block-dropping already keeps rounds whole;
    this only bites on a pre-existing malformed head).
    """
    removed = 0
    # Scan from just past the system block; skip the objective anchor (a user
    # row) if it sits there, then strip any orphan tool results that follow.
    i = system_end
    n = len(messages)
    # Advance past a leading non-tool message (e.g. the preserved anchor / a
    # user row) so we only strip tool results that would actually be orphaned.
    while i < n and isinstance(messages[i], dict) \
            and messages[i].get('role') != 'tool':
        i += 1
    while i < len(messages) and isinstance(messages[i], dict) \
            and messages[i].get('role') == 'tool':
        messages.pop(i)
        removed += 1
    if removed:
        logger.warning('[HeadTruncate] Pruned %d orphan tool result(s) left at '
                       'the head after truncation (no parent tool_call)', removed)
    return removed


def _head_truncate(messages: list, task: dict | None = None,
                   byte_target: int | None = None,
                   reported_token_count: int | None = None,
                   *, token_target: int | None = None,
                   event_name: str = 'reactive_head_truncate') -> int:
    """Last-resort head truncation: drop the oldest non-system messages.

    Drops in PAIRING-SAFE units (a whole ``assistant(tool_calls)+tool`` round at
    a time — see ``_round_block_end``) so it can never orphan a tool result and
    trigger the very HTTP 400 it is meant to avert.

    ``event_name`` labels the ``audit_log`` entry. It defaults to
    ``reactive_head_truncate`` so every existing (reactive) call site is
    byte-identical; the proactive-pipeline fallback passes
    ``proactive_head_truncate`` so the two escape hatches are
    distinguishable in audit.log.

    Returns the number of messages dropped (0 if nothing could be shed —
    e.g. fewer than ``system_end + 4`` messages remain).
    """
    system_end = 0
    for i, msg in enumerate(messages):
        if msg.get('role') == 'system':
            system_end = i + 1
        else:
            break

    # OBJECTIVE ANCHOR — the first real user message (the north-star goal)
    #   must survive even a last-resort head-truncate.  Compute the drop
    #   position that SKIPS it: normally we pop the oldest non-system message
    #   (``system_end``); if that is the anchor, pop the one AFTER it instead so
    #   the goal is never discarded.  A tiny helper keeps both trim loops
    #   (byte-target and token-target) honouring the anchor identically.
    def _drop_pos() -> int:
        anchor = _objective_anchor_index(messages)
        if anchor == system_end and len(messages) > system_end + 1:
            return system_end + 1  # protect the anchor; drop the next-oldest
        return system_end

    def _pop_block(pos: int) -> list:
        """Pop the pairing-safe unit at ``pos`` and return the popped messages
        (a whole tool-call round when ``pos`` starts on assistant.tool_calls,
        else the single message)."""
        end = _round_block_end(messages, pos)
        block = messages[pos:end]
        del messages[pos:end]
        return block

    if byte_target is not None:
        dropped = 0
        # Running total: re-serializing the ENTIRE list every iteration is
        # O(n^2) on the exact (large, MB-scale) payloads this path fires on.
        # Subtract each popped message's own wire bytes instead; recompute the
        # true whole-list size ONCE after the loop for the log/audit value.
        cur_bytes = _estimate_wire_bytes(messages)
        while cur_bytes > byte_target and len(messages) > system_end + 4:
            block = _pop_block(_drop_pos())
            if not block:
                break
            try:
                for popped in block:
                    cur_bytes -= len(json.dumps(
                        popped, ensure_ascii=False).encode('utf-8')) + 2  # ', ' sep
            except Exception as _e:
                logger.debug('[HeadTruncate] per-msg wire estimate failed '
                             '(%s) — recomputing whole list', _e)
                cur_bytes = _estimate_wire_bytes(messages)
            dropped += len(block)
        # Belt-and-suspenders: never leave an orphan tool result at the head.
        dropped += _strip_leading_orphan_tools(messages, system_end)
        if dropped:
            wire_now = _estimate_wire_bytes(messages)
            logger.warning('[HeadTruncate] Dropped %d oldest messages by byte target '
                           '(wire now %.1fMB, target %.1fMB)',
                           dropped, wire_now / 1048576, byte_target / 1048576)
            # Last-resort truncation permanently discards conversation context;
            # record what was lost so it's queryable per-conv in audit.log
            # rather than only inferable from a transient WARNING.
            audit_log(event_name,
                      conv=(task.get('convId', '') if task else ''),
                      dropped_msgs=dropped, mode='byte_target',
                      wire_mb=round(wire_now / 1048576, 2))
        return dropped

    context_limit = _get_context_limit(task)
    target = (max(1, int(token_target)) if token_target is not None
              else int(context_limit * 0.60))

    if token_target is not None:
        target_measure = target
    elif reported_token_count and reported_token_count > target:
        est_before = max(1, _estimate_total_tokens(messages))
        frac_to_drop = (reported_token_count - target) / reported_token_count
        heuristic_target = int(est_before * (1 - frac_to_drop))
        logger.warning('[HeadTruncate] Using upstream-reported count: '
                       'reported=%d target=%d → shed %.0f%% '
                       '(heuristic %d → %d)',
                       reported_token_count, target, frac_to_drop * 100,
                       est_before, heuristic_target)
        target_measure = heuristic_target
    else:
        target_measure = target

    dropped = 0
    # Exact running total: _estimate_total_tokens == sum(_estimate_msg_tokens),
    # so subtracting each popped message keeps cur_tokens bit-identical to a
    # fresh full walk — without the O(n^2) re-sum every iteration.
    cur_tokens = _estimate_total_tokens(messages)
    while cur_tokens > target_measure and len(messages) > system_end + 4:
        block = _pop_block(_drop_pos())
        if not block:
            break
        for popped in block:
            cur_tokens -= _estimate_msg_tokens(popped)
        dropped += len(block)
    # Belt-and-suspenders: never leave an orphan tool result at the head.
    dropped += _strip_leading_orphan_tools(messages, system_end)

    if dropped:
        logger.warning('[HeadTruncate] Dropped %d oldest messages to fit context '
                       '(tokens now ~%d, target ~%d, reported_api=%s)',
                       dropped, cur_tokens, target_measure,
                       f'{reported_token_count:,}' if reported_token_count else 'n/a')
        audit_log(event_name,
                  conv=(task.get('convId', '') if task else ''),
                  dropped_msgs=dropped, mode='token_target',
                  tokens_after=cur_tokens,
                  target=target_measure)
    return dropped
