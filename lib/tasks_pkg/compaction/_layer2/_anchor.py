"""Layer 2 — boundary / anchor / extraction helpers (pure, no LLM).

Holds the query-aware boundary machinery shared by the automatic L2 path and
the manual ``/compact`` path:

  * ``_objective_anchor_index``       — index of the immutable OBJECTIVE ANCHOR.
  * ``_extract_current_query``        — most-recent user query text.
  * ``_find_turn_boundary``           — preservation boundary (turn-aware).
  * ``_coerce_spec_list``             — tolerant list-of-specs coercion.
  * ``_extract_recently_accessed_files`` — recent read/write file paths.
  * ``_split_cold_rounds`` / ``_apiform_tool_rounds`` / ``_fold_recent_intra_turn``
    — the SHARED intra-turn fold policy (single-giant-turn overflow).
"""

import json

from lib.log import get_logger
from lib.tasks_pkg.compaction._constants import (
    _INTRA_TURN_HOT_ROUNDS,
    _MAX_PRESERVE_TURNS,
)
from lib.tasks_pkg.compaction._tokens import _estimate_msg_tokens

logger = get_logger(__name__)


def _objective_anchor_index(messages: list) -> int | None:
    """Index of the immutable OBJECTIVE ANCHOR — the first real user message.

    This is the SAME "first real user message" the autopilot objective pin
    (``_get_or_persist_objective`` / ``_extract_objective``) is derived from —
    ONE definition of "the objective", not a parallel one.  Compaction protects
    this message so the original goal survives N successive summaries VERBATIM
    (``execute_compact_tool`` excludes it from the summarized ``old_messages``
    and re-inserts it exactly once; ``_head_truncate`` never drops it).  The
    autopilot pin is a cross-run TEXT cache of the very same message.

    Skips leading ``system`` messages, any VU directive / virtual-user turn
    (defensive — those flags are autopilot-only and absent elsewhere), and the
    synthetic ``_isMeta`` context carriers the builder prepends (CLAUDE.md /
    user-preference profile) — those are a ``user`` message at index 1, BEFORE
    the real user turn, so without this skip the anchor would protect injected
    context instead of the human's goal. Same skip as ``_extract_objective``.
    Returns ``None`` when no real user message exists (compaction then behaves
    exactly as before — no anchor to protect).
    """
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or m.get('role') != 'user':
            continue
        if m.get('_isVuDirective') or m.get('_isVirtualUser') or m.get('_isMeta'):
            continue
        content = m.get('content')
        if isinstance(content, str):
            if content.strip():
                return i
        elif isinstance(content, list):
            # A turn is real if it carries ANY substantive block — non-blank
            # text, OR a non-text block (image / document / audio). An
            # image-only opener ("fix this" + a screenshot, or just the
            # screenshot) IS the user's goal, and anchoring past it lets the
            # real request be summarized away irrecoverably.
            #
            # This differs DELIBERATELY from autopilot_state._extract_objective,
            # which shares the skip rules above but returns TEXT for the virtual
            # user: an image carries no text, so skipping it there is correct.
            # Here the return value is an INDEX whose purpose is "protect this
            # message from summarization", so an image-only turn must qualify.
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'text':
                    if (b.get('text') or '').strip():
                        return i
                else:
                    return i
        elif content:  # non-empty scalar of some other type — still real
            return i
    return None


def _extract_current_query(messages: list) -> str:
    """Extract the most recent real/current user query from messages.

    Runtime attachments are represented as ``role='user'`` for provider wire
    compatibility, but they are not a new objective.  Treating a trailing
    checklist/system reminder (or a pure swarm inbox notification) as the
    current query steers the lossy summary away from the human request.
    """
    for msg in reversed(messages):
        if msg.get('role') == 'user':
            if msg.get('_isMeta'):
                continue
            if msg.get('_isInboxInject') and not msg.get('_containsHumanSteer'):
                continue
            content = msg.get('content', '')
            if isinstance(content, list):
                text_parts = [
                    b.get('text', '')
                    for b in content
                    if isinstance(b, dict) and b.get('type') == 'text'
                ]
                return '\n'.join(text_parts)[:500]
            elif isinstance(content, str):
                return content[:500]
    return ''


def _find_turn_boundary(
    messages: list,
    *,
    budget_tokens: float = float('inf'),
    max_turns: int = _MAX_PRESERVE_TURNS,
) -> int:
    """Find the preservation boundary using the turn abstraction.

    A *turn* = ``[user_msg, ...all subsequent non-user messages]``.
    Turns are atomic; the boundary always falls on a ``user`` index.

    Policy:
      • HARD INVARIANT — current (most-recent) turn always preserved.
      • BEST-EFFORT    — older turns added newest → oldest while under
        ``preserved_tokens + turn_tokens <= budget_tokens`` AND total
        preserved turn count stays ``<= max_turns``.
      • REFUSE         — if no ``user`` message exists, returns
        ``len(messages)`` so the caller short-circuits.
    """
    user_idx = [i for i, m in enumerate(messages) if m.get('role') == 'user']
    if not user_idx:
        return len(messages)

    turn_starts = user_idx
    turn_ends = user_idx[1:] + [len(messages)]

    cur_start, cur_end = turn_starts[-1], turn_ends[-1]
    boundary = cur_start
    preserved_tokens = sum(
        _estimate_msg_tokens(m) for m in messages[cur_start:cur_end]
    )
    preserved_turn_count = 1

    for k in range(len(turn_starts) - 2, -1, -1):
        if preserved_turn_count >= max_turns:
            break
        start, end = turn_starts[k], turn_ends[k]
        tt = sum(_estimate_msg_tokens(m) for m in messages[start:end])
        if preserved_tokens + tt > budget_tokens:
            break
        boundary = start
        preserved_tokens += tt
        preserved_turn_count += 1

    return boundary


# ═══════════════════════════════════════════════════════════════════════════
#  Intra-turn folding — the SHARED policy for the single-giant-turn overflow.
#
#  A single agentic turn (one user request answered with dozens of tool
#  rounds) can fill the whole window on its own.  The turn-based boundary
#  (`_find_turn_boundary` / `_raw_turn_boundary`) ALWAYS preserves the current
#  turn whole, so neither the automatic L2 path nor the manual /compact path
#  could shrink it by turn-dropping alone.  The fix is to fold the COLD tool
#  rounds INSIDE that one preserved turn: keep the most-recent `hot_rounds`
#  verbatim and summarize + drop the older ones.
#
#  Two index spaces, ONE policy:
#    * manual path  — folds RAW ``toolRounds`` dicts inside one assistant row
#      (`_manual._collect_reserve_folds`).
#    * automatic path — folds expanded api-form (assistant(tool_calls)+tool)
#      round SPANS inside the preserved region (`_fold_recent_intra_turn`).
#  Both call ``_split_cold_rounds`` so the keep-vs-fold cut can never drift
#  between the two compaction paths.
# ═══════════════════════════════════════════════════════════════════════════

def _split_cold_rounds(rounds: list, hot_rounds: int = _INTRA_TURN_HOT_ROUNDS):
    """Split a round sequence into ``(cold, hot)`` at the intra-turn fold line.

    ``rounds`` is any ordered sequence of round descriptors (RAW ``toolRounds``
    dicts for the manual path, api-form ``(start, end)`` spans for the automatic
    path — the policy is agnostic to the element type).  Keeps the last
    ``hot_rounds`` as HOT (verbatim) and returns everything older as COLD (to
    summarize + drop).  Returns ``([], list(rounds))`` when there is nothing to
    fold (``len(rounds) <= hot_rounds``), so callers can cheaply no-op.

    HARD CONSTRAINT (both paths): the fold unit is a WHOLE round — a
    self-contained ``toolCallId``/``toolContent`` (raw) or a complete
    assistant(tool_calls)+tool span (api-form).  Dropping whole cold rounds can
    therefore never orphan a ``tool`` message nor split a tool_call/result pair.
    """
    hot_rounds = max(1, int(hot_rounds))
    if len(rounds) <= hot_rounds:
        return [], list(rounds)
    return list(rounds[:-hot_rounds]), list(rounds[-hot_rounds:])


def _apiform_tool_rounds(messages: list) -> list:
    """Group api-form message indices into tool-call ROUNDS.

    A *round* = an ``assistant`` message carrying ``tool_calls`` plus every
    immediately-following ``tool`` result message.  Returns a list of
    ``(start, end)`` half-open index spans, one per round, in order.  Messages
    that are not part of any tool-call round (a leading ``user`` message, plain
    ``assistant`` prose/thinking, a ``system`` row) belong to NO span and are
    left untouched by the fold — so the leading user turn and the model's
    reasoning survive.
    """
    rounds: list = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        if isinstance(m, dict) and m.get('role') == 'assistant' and m.get('tool_calls'):
            j = i + 1
            while j < n and isinstance(messages[j], dict) \
                    and messages[j].get('role') == 'tool':
                j += 1
            rounds.append((i, j))
            i = j
        else:
            i += 1
    return rounds


def _fold_recent_intra_turn(recent_messages: list,
                            hot_rounds: int = _INTRA_TURN_HOT_ROUNDS):
    """Fold COLD tool-call rounds out of an api-form PRESERVED region.

    Used by the automatic L2 path (``execute_compact_tool``) so an in-flight
    giant turn preserved whole by ``_find_turn_boundary`` can still be shrunk.
    Keeps the leading ``user`` message(s), any plain assistant prose, and the
    most-recent ``hot_rounds`` tool-call rounds VERBATIM; the older (cold) round
    SPANS are removed as WHOLE units (no orphan tool — see ``_split_cold_rounds``).

    Returns ``(kept_messages, cold_round_messages)``:
      * ``kept_messages``       — the folded preserved region (hot tail intact).
      * ``cold_round_messages`` — the removed cold-round messages, IN ORDER, to
        feed the summarizer (they are NEVER re-inserted verbatim).

    A no-op (``recent_messages`` returned unchanged, ``[]``) when the region has
    ``<= hot_rounds`` tool-call rounds — so a normal multi-turn chat near the
    window is byte-identical to the pre-fold behaviour.
    """
    rounds = _apiform_tool_rounds(recent_messages)
    cold_spans, _hot_spans = _split_cold_rounds(rounds, hot_rounds)
    if not cold_spans:
        return list(recent_messages), []

    cold_idx: set[int] = set()
    for (s, e) in cold_spans:
        cold_idx.update(range(s, e))

    kept = [m for k, m in enumerate(recent_messages) if k not in cold_idx]
    cold_msgs = [recent_messages[k] for k in sorted(cold_idx)]
    return kept, cold_msgs


def _coerce_spec_list(value) -> list:
    """Coerce a tool arg that should be a list-of-specs into a real list.

    Tolerates the observed-in-the-wild case where a streamed / partial
    tool-call recorded the array as a JSON *string* (sometimes truncated)
    instead of a list — e.g. ``reads='[{"path": "a.py", "end_line": 4]'``.
    Iterating such a raw string char-by-char is what produced the notorious
    "one letter per line" modified-files reminder (conv mr4e8pnxbv440z).

    If the string decodes to a list, return it; otherwise return ``[]`` so the
    caller skips it rather than iterating characters and emitting garbage.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError) as e:
            logger.debug('[Compact] _coerce_spec_list: unparseable spec '
                         'string (%s) — dropping', e)
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _extract_recently_accessed_files(messages: list,
                                     max_files: int = 8) -> list[str]:
    """Scan messages newest-first for file paths from read/write tools."""
    files_seen: list[str] = []
    files_set: set[str] = set()

    for msg in reversed(messages):
        for tc in msg.get('tool_calls', []):
            fn = tc.get('function', {})
            fn_name = fn.get('name', '')

            if fn_name not in ('read_files', 'read_file',
                               'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
                               'insert_content', 'insert_contents'):
                continue

            try:
                args = json.loads(fn.get('arguments', '{}'))
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug('[Compaction] Skipping unparseable tool_call args for %s: %s',
                             fn_name, exc, exc_info=True)
                continue

            if not isinstance(args, dict):
                logger.debug('[Compact] Skipping non-dict tool_call args for %s (type=%s)',
                             fn_name, type(args).__name__)
                continue

            if fn_name == 'read_files':
                # After _coerce_spec_list the container is guaranteed a LIST,
                # so a string ELEMENT is a genuine full path (a documented
                # Claude-Opus shape: reads=["a.py","b.py"]) — NOT a stray char
                # from iterating a string container. Keep both element shapes.
                for spec in _coerce_spec_list(args.get('reads')):
                    if isinstance(spec, dict):
                        p = spec.get('path', '')
                    elif isinstance(spec, str):
                        p = spec.strip()
                    else:
                        logger.debug('[Compact] Skipping non-dict/str read spec type=%s',
                                     type(spec).__name__)
                        continue
                    if p and p not in files_set:
                        files_seen.append(p)
                        files_set.add(p)
            elif fn_name in ('edit_file', 'apply_diff', 'apply_diffs') and args.get('edits'):
                for edit in _coerce_spec_list(args.get('edits')):
                    if isinstance(edit, dict):
                        p = edit.get('path', '')
                        if p and p not in files_set:
                            files_seen.append(p)
                            files_set.add(p)
            elif fn_name in ('insert_content', 'insert_contents') and args.get('edits'):
                for edit in _coerce_spec_list(args.get('edits')):
                    if isinstance(edit, dict):
                        p = edit.get('path', '')
                        if p and p not in files_set:
                            files_seen.append(p)
                            files_set.add(p)
            else:
                p = args.get('path', '') if isinstance(args, dict) else ''
                if p and p not in files_set:
                    files_seen.append(p)
                    files_set.add(p)

            if len(files_seen) >= max_files:
                break

    if files_seen:
        logger.debug('[Compact] Found %d recently-accessed files: %s',
                     len(files_seen),
                     ', '.join(files_seen[:4]) + ('...' if len(files_seen) > 4 else ''))

    return files_seen
