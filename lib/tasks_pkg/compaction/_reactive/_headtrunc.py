"""Phase 4 last-resort head truncation — drop oldest non-system messages.

Byte-target and token-target variants both honour the objective anchor (the
first real user message / north-star goal) via ``_objective_anchor_index``
from the ``_layer2`` sub-package.
"""

import json

from lib.log import audit_log, get_logger
from lib.tasks_pkg.compaction._reactive._measure import _estimate_wire_bytes
from lib.tasks_pkg.compaction._tokens import (
    _estimate_msg_tokens,
    _estimate_total_tokens,
    _get_context_limit,
)

logger = get_logger(__name__)


def _head_truncate(messages: list, task: dict | None = None,
                   byte_target: int | None = None,
                   reported_token_count: int | None = None,
                   *, event_name: str = 'reactive_head_truncate') -> int:
    """Last-resort head truncation: drop the oldest non-system messages.

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

    # ★ OBJECTIVE ANCHOR — the first real user message (the north-star goal)
    #   must survive even a last-resort head-truncate.  Compute the drop
    #   position that SKIPS it: normally we pop the oldest non-system message
    #   (``system_end``); if that is the anchor, pop the one AFTER it instead so
    #   the goal is never discarded.  A tiny helper keeps both trim loops
    #   (byte-target and token-target) honouring the anchor identically.
    from lib.tasks_pkg.compaction._layer2 import _objective_anchor_index

    def _drop_pos() -> int:
        anchor = _objective_anchor_index(messages)
        if anchor == system_end and len(messages) > system_end + 1:
            return system_end + 1  # protect the anchor; drop the next-oldest
        return system_end

    if byte_target is not None:
        dropped = 0
        # Running total: re-serializing the ENTIRE list every iteration is
        # O(n^2) on the exact (large, MB-scale) payloads this path fires on.
        # Subtract each popped message's own wire bytes instead; recompute the
        # true whole-list size ONCE after the loop for the log/audit value.
        cur_bytes = _estimate_wire_bytes(messages)
        while cur_bytes > byte_target and len(messages) > system_end + 4:
            popped = messages.pop(_drop_pos())
            try:
                cur_bytes -= len(json.dumps(
                    popped, ensure_ascii=False).encode('utf-8')) + 2  # ', ' sep
            except Exception as _e:
                logger.debug('[HeadTruncate] per-msg wire estimate failed '
                             '(%s) — recomputing whole list', _e)
                cur_bytes = _estimate_wire_bytes(messages)
            dropped += 1
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
    target = int(context_limit * 0.60)

    if reported_token_count and reported_token_count > target:
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
        cur_tokens -= _estimate_msg_tokens(messages.pop(_drop_pos()))
        dropped += 1

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
