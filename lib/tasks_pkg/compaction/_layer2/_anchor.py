"""Layer 2 — boundary / anchor / extraction helpers (pure, no LLM).

Holds the query-aware boundary machinery shared by the automatic L2 path and
the manual ``/compact`` path:

  * ``_objective_anchor_index``       — index of the immutable OBJECTIVE ANCHOR.
  * ``_extract_current_query``        — most-recent user query text.
  * ``_find_turn_boundary``           — preservation boundary (turn-aware).
  * ``_coerce_spec_list``             — tolerant list-of-specs coercion.
  * ``_extract_recently_accessed_files`` — recent read/write file paths.
"""

import json

from lib.log import get_logger
from lib.tasks_pkg.compaction._constants import _MAX_PRESERVE_TURNS
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
            if any(isinstance(b, dict) and b.get('type') == 'text'
                   and (b.get('text') or '').strip() for b in content):
                return i
        elif content:  # non-empty non-text (e.g. image-only) — still real
            return i
    return None


def _extract_current_query(messages: list) -> str:
    """Extract the most recent user query from messages."""
    for msg in reversed(messages):
        if msg.get('role') == 'user':
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
                               'write_file', 'apply_diff', 'apply_diffs',
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
            elif fn_name in ('apply_diff', 'apply_diffs') and args.get('edits'):
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
