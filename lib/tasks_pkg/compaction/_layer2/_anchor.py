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
import posixpath

from lib.log import get_logger
from lib.tool_history_pairing import adjacent_tool_call_result_pairs
from lib.tasks_pkg.compaction._constants import (
    _INTRA_TURN_HOT_ROUNDS,
    _MAX_PRESERVE_TURNS,
    _PERSIST_DIR_BASE,
    _USER_VERBATIM_BUDGET_TOKENS,
    _USER_VERBATIM_MAX_MESSAGES,
)
from lib.tasks_pkg.compaction._tokens import _estimate_msg_tokens

logger = get_logger(__name__)


def _objective_anchor_index(messages: list) -> int | None:
    """Index of the OPENING-REQUEST ANCHOR — the first real user message.

    This is the SAME "first real user message" the autopilot objective pin is
    minted from (``_get_or_persist_objective`` / ``_extract_objective``) — ONE
    definition of the opening ask, not a parallel one.  Compaction protects
    this message so the conversation's origin survives N successive summaries
    VERBATIM (``execute_compact_tool`` excludes it from the summarized
    ``old_messages`` and re-inserts it exactly once; ``_head_truncate`` never
    drops it).  Verbatim protection is EVIDENCE preservation, not an objective
    decree: the receipt's Objective is separately model-authored from all
    user messages (see ``_ensure_summary_objective``), and the autopilot pin
    is re-pinned from accepted receipts when the human's goal is replaced
    (``_update_objective_from_receipt``).

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


def _user_message_text(msg: dict) -> str:
    """Plain-text content of a user message ('' when it carries no text)."""
    content = msg.get('content', '')
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get('text', '') for b in content
                 if isinstance(b, dict) and b.get('type') == 'text']
        return '\n'.join(p for p in parts if p).strip()
    return ''


def _extract_objective_anchor_text(
    messages: list,
    *,
    char_limit: int = 2_400,
) -> str:
    """Return a bounded text view of the opening-request anchor.

    The automatic compactor removes the anchor message from the lossy history
    before dispatch and re-inserts it verbatim afterwards, so the summary
    model would not see the opening request at all without this re-supply.
    It reaches the model as VERBATIM EVIDENCE (labeled "may already be
    completed or explicitly replaced"); the model authors the receipt's
    Objective from all user messages, with this anchor as the failure-floor
    fallback (``_ensure_summary_objective``). This helper derives both views
    from the same canonical anchor index; it does not invent a second
    objective source of truth.

    The live message remains unmodified and unbounded. Only the cheap-model
    prompt is capped, retaining both ends because long requests commonly put
    references first and the operative instruction last.
    """
    anchor_index = _objective_anchor_index(messages)
    if anchor_index is None:
        return ''
    text = _user_message_text(messages[anchor_index])
    if not text:
        return '[The primary request contains non-text attachment content.]'
    limit = max(500, int(char_limit))
    if len(text) <= limit:
        return text
    head = max(250, (limit * 3) // 5)
    tail = max(250, limit - head - 80)
    return (
        text[:head]
        + '\n...[middle of durable objective elided for summary prompt]...\n'
        + text[-tail:]
    )


def _collect_user_verbatim(
    old_messages: list,
    *,
    budget_tokens: int = _USER_VERBATIM_BUDGET_TOKENS,
    max_messages: int = _USER_VERBATIM_MAX_MESSAGES,
) -> list[str]:
    """Select real user-message texts from the to-be-summarized OLD region
    for VERBATIM retention across an L2 summary.

    Codex-inspired (codex-rs ``compact.rs`` keeps user messages intact): the
    lossy summary must not be the only place the user's literal instructions
    survive. Selection is NEWEST-first under ``budget_tokens`` /
    ``max_messages`` (the most recent instructions are the ones most likely
    to still bind the current work), returned in chronological order.

    Skips: synthetic ``_isMeta`` carriers (incl. a previous retention
    wrapper — no feedback duplication), every engine/agent-initiated turn as
    resolved by the canonical turn-initiator vocabulary, empty/non-text
    content, and exact duplicates. Human operator guidance and a swarm inbox
    carrier that contains a human steer remain eligible. The objective anchor
    is already removed from ``old_messages`` by the caller, so it is never
    duplicated here.
    """
    picked: list[str] = []
    seen: set[str] = set()
    spent = 0
    from lib.turn_initiation import (
        INITIATOR_OPERATOR,
        is_auto_initiated,
        resolve_initiator,
    )
    for msg in reversed(old_messages):
        if len(picked) >= max_messages:
            break
        if not isinstance(msg, dict) or msg.get('role') != 'user':
            continue
        if msg.get('_isMeta') or msg.get('_isVuDirective'):
            continue
        human_steer = bool(msg.get('_containsHumanSteer'))
        human_operator = resolve_initiator(msg) == INITIATOR_OPERATOR
        if is_auto_initiated(msg) and not (human_steer or human_operator):
            continue
        if msg.get('_isInboxInject') and not human_steer:
            continue
        text = _user_message_text(msg)
        if not text or text in seen:
            continue
        cost = _estimate_msg_tokens(msg)
        if picked and spent + cost > budget_tokens:
            continue
        picked.append(text)
        seen.add(text)
        spent += cost
    picked.reverse()
    return picked


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
    # ``_isMeta`` carriers (CLAUDE.md context block, token-budget reminder,
    # preference profile) are synthetic context transports, not human turns —
    # they must be transparent to the turn structure, exactly as
    # ``_extract_current_query`` / ``_objective_anchor_index`` already treat
    # them. Without this skip a trailing meta reminder would split the
    # in-flight turn at the boundary and the real current turn would lose its
    # "always preserved whole" invariant.
    user_idx = [i for i, m in enumerate(messages)
                if m.get('role') == 'user' and not m.get('_isMeta')]
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

def _split_cold_rounds(
    rounds: list,
    hot_rounds: int = _INTRA_TURN_HOT_ROUNDS,
    *,
    hot_budget_tokens: int | None = None,
    base_tokens: int = 0,
    token_cost=None,
):
    """Split a round sequence into ``(cold, hot)`` at the intra-turn fold line.

    ``rounds`` is any ordered sequence of round descriptors (RAW ``toolRounds``
    descriptors for the manual path, api-form ``(start, end)`` spans for the
    automatic path — the policy is agnostic to the element type). Without a
    token budget, keeps the last ``hot_rounds`` as HOT (verbatim). With a token
    budget, that count becomes a maximum: keep the newest contiguous suffix
    whose cost plus ``base_tokens`` fits. The newest complete round is always
    retained even if it alone exceeds the budget.

    HARD CONSTRAINT (both paths): the fold unit is a WHOLE round — a
    self-contained ``toolCallId``/``toolContent`` (raw) or a complete
    assistant(tool_calls)+tool span (api-form).  Dropping whole cold rounds can
    therefore never orphan a ``tool`` message nor split a tool_call/result pair.
    """
    hot_rounds = max(1, int(hot_rounds))
    if hot_budget_tokens is None:
        if len(rounds) <= hot_rounds:
            return [], list(rounds)
        return list(rounds[:-hot_rounds]), list(rounds[-hot_rounds:])

    if not rounds:
        return [], []
    if not callable(token_cost):
        raise TypeError('token_cost must be callable with hot_budget_tokens')
    budget = max(1, int(hot_budget_tokens))
    spent = max(0, int(base_tokens))
    keep_count = 0
    for round_descriptor in reversed(rounds[-hot_rounds:]):
        round_tokens = max(0, int(token_cost(round_descriptor)))
        if keep_count > 0 and spent + round_tokens > budget:
            break
        spent += round_tokens
        keep_count += 1
    return (list(rounds[:-keep_count]), list(rounds[-keep_count:]))


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


def _fold_recent_intra_turn(
    recent_messages: list,
    hot_rounds: int = _INTRA_TURN_HOT_ROUNDS,
    hot_budget_tokens: int | None = None,
):
    """Fold COLD tool-call rounds out of an api-form PRESERVED region.

    Used by the automatic L2 path (``execute_compact_tool``) so an in-flight
    giant turn preserved whole by ``_find_turn_boundary`` can still be shrunk.
    Keeps the leading ``user`` message(s), any plain assistant prose, and the
    most-recent ``hot_rounds`` tool-call rounds VERBATIM, subject to the
    optional token budget; older (cold) round SPANS are removed as WHOLE units
    (no orphan tool — see ``_split_cold_rounds``). The newest complete tool
    round is always kept even when it alone exceeds the budget.

    Returns ``(kept_messages, cold_round_messages)``:
      * ``kept_messages``       — the folded preserved region (hot tail intact).
      * ``cold_round_messages`` — the removed cold-round messages, IN ORDER, to
        feed the summarizer (they are NEVER re-inserted verbatim).

    Without ``hot_budget_tokens``, a region with ``<= hot_rounds`` tool-call
    rounds is a byte-identical no-op. With a budget, even a short but enormous
    hot tail is folded until its newest contiguous whole-round suffix fits.
    """
    rounds = _apiform_tool_rounds(recent_messages)
    if hot_budget_tokens is None:
        cold_spans, _hot_spans = _split_cold_rounds(rounds, hot_rounds)
    else:
        round_indices = {
            index for start, end in rounds for index in range(start, end)
        }
        base_tokens = sum(
            _estimate_msg_tokens(message)
            for index, message in enumerate(recent_messages)
            if index not in round_indices
        )
        cold_spans, _hot_spans = _split_cold_rounds(
            rounds,
            hot_rounds,
            hot_budget_tokens=hot_budget_tokens,
            base_tokens=base_tokens,
            token_cost=lambda span: sum(
                _estimate_msg_tokens(message)
                for message in recent_messages[span[0]:span[1]]
            ),
        )
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


def _normalise_recent_file_path(value) -> str:
    """Return one stable model-visible path or ``''`` for transport artifacts.

    Oversized legacy tool results are staged below ``_PERSIST_DIR_BASE`` so a
    model can selectively read them during the live turn.  They are
    reconstructible transport data, not project working state, and must not be
    promoted into a compaction summary's durable "recent files" reminder.
    """
    if not isinstance(value, str):
        return ''
    raw = value.strip().replace('\\', '/')
    if not raw:
        return ''
    normalised = posixpath.normpath(raw)
    if normalised in ('', '.'):
        return ''
    persist_root = str(_PERSIST_DIR_BASE).strip().replace('\\', '/').strip('/')
    path_with_edges = f'/{normalised.lstrip("/")}/'
    persist_parts = [part for part in persist_root.split('/') if part]
    persist_markers = {persist_root}
    if len(persist_parts) >= 2:
        persist_markers.add('/'.join(persist_parts[-2:]))
    if any(marker and f'/{marker}/' in path_with_edges
           for marker in persist_markers):
        return ''
    return normalised


def _successful_file_tool_call_objects(messages: list) -> set[int]:
    """Return object identities of calls with adjacent successful results.

    A call id is only a queue selector inside one assistant/result run.  Using
    a conversation-wide ``id -> latest result`` map loses valid earlier calls
    when positional-id providers recycle ``call_0`` and can lend a result to
    the wrong occurrence in malformed imported history.
    """
    successful: set[int] = set()
    for tool_call, message in adjacent_tool_call_result_pairs(messages):
        if (message.get('isError') or message.get('is_error')
                or str(message.get('status') or '').lower()
                in {'error', 'failed', 'failure', 'rejected'}):
            continue
        content = message.get('content')
        if isinstance(content, dict):
            if (content.get('isError') or content.get('is_error')
                    or content.get('ok') is False
                    or str(content.get('status') or '').lower()
                    in {'error', 'failed', 'failure', 'rejected'}):
                continue
            content = json.dumps(content, ensure_ascii=False, default=str)
        elif isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False, default=str)
        if not isinstance(content, str):
            continue
        stripped = content.lstrip()
        if stripped.startswith('{'):
            try:
                envelope = json.loads(content)
            except (TypeError, ValueError):
                envelope = None
            if isinstance(envelope, dict) and (
                envelope.get('isError') or envelope.get('is_error')
                or envelope.get('ok') is False
                or str(envelope.get('status') or '').lower()
                in {'error', 'failed', 'failure', 'rejected'}
            ):
                continue
        function = tool_call.get('function') or {}
        tool_name = (str(message.get('name') or '').strip()
                     or str(function.get('name') or '').strip()
                     if isinstance(function, dict) else '')
        try:
            from lib.tasks_pkg.handlers._read_gate import (
                _result_indicates_success,
            )
            if _result_indicates_success(tool_name, content):
                successful.add(id(tool_call))
        except Exception:
            # Keep the extraction helper total even in minimal/exported builds.
            if content and not content.lstrip().startswith((
                'Error:', 'ERROR:', 'Write failed', 'Diff failed',
                'Insert failed', 'Failed',
            )):
                successful.add(id(tool_call))
    return successful


def _extract_recently_accessed_files(messages: list,
                                     max_files: int = 8) -> list[str]:
    """Scan newest-first for successful, durable read/write file paths."""
    max_files = max(0, int(max_files or 0))
    if max_files == 0:
        return []
    files_seen: list[str] = []
    files_set: set[str] = set()
    successful_calls = _successful_file_tool_call_objects(messages)

    def _add_path(value) -> None:
        if len(files_seen) >= max_files:
            return
        path = _normalise_recent_file_path(value)
        if path and path not in files_set:
            files_seen.append(path)
            files_set.add(path)

    for msg in reversed(messages):
        if len(files_seen) >= max_files:
            break
        for tc in msg.get('tool_calls', []):
            if len(files_seen) >= max_files:
                break
            fn = tc.get('function', {})
            fn_name = fn.get('name', '')
            call_id = str(tc.get('id') or '').strip()

            if fn_name not in ('read_files', 'read_file',
                               'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
                               'insert_content', 'insert_contents'):
                continue

            if call_id and id(tc) not in successful_calls:
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
                    if len(files_seen) >= max_files:
                        break
                    if isinstance(spec, dict):
                        p = spec.get('path', '')
                    elif isinstance(spec, str):
                        p = spec.strip()
                    else:
                        logger.debug('[Compact] Skipping non-dict/str read spec type=%s',
                                     type(spec).__name__)
                        continue
                    _add_path(p)
            elif fn_name in ('edit_file', 'apply_diff', 'apply_diffs') and args.get('edits'):
                for edit in _coerce_spec_list(args.get('edits')):
                    if len(files_seen) >= max_files:
                        break
                    if isinstance(edit, dict):
                        _add_path(edit.get('path', ''))
            elif fn_name in ('insert_content', 'insert_contents') and args.get('edits'):
                for edit in _coerce_spec_list(args.get('edits')):
                    if len(files_seen) >= max_files:
                        break
                    if isinstance(edit, dict):
                        _add_path(edit.get('path', ''))
            else:
                _add_path(args.get('path', '') if isinstance(args, dict) else '')

    if files_seen:
        logger.debug('[Compact] Found %d recently-accessed files: %s',
                     len(files_seen),
                     ', '.join(files_seen[:4]) + ('...' if len(files_seen) > 4 else ''))

    return files_seen
