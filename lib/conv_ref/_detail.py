"""Conversation reference — single-conversation render surface.

Holds ``get_conversation`` (fetch + format the full transcript of one
conversation) and its formatting helpers (``_extract_text``,
``_format_tool_rounds``, ``_extract_result_text``, ``_truncate``).
"""

import json

from lib.conv_ref._query import DEFAULT_USER_ID, _get_db
from lib.log import get_logger
from lib.utils import safe_json

logger = get_logger(__name__)

# Cap on the total rendered output so a huge conversation can't flood the
# model's context window. Applies to both the prose transcript and the raw dump.
MAX_CHARS = 80000

#: Default number of messages rendered by ``get_conversation`` — the TAIL of
#: the conversation (where it ended up), plus ``TRANSCRIPT_HEAD`` opening
#: messages for context. Selection happens at the MESSAGE level so a trimmed
#: read still ends on a whole message instead of mid-token.
TRANSCRIPT_HEAD = 3
TRANSCRIPT_TAIL = 60


def row_window_usable(db, conversation_id, blob_count):
    """Whether the ``conversation_messages`` row store can serve this conv.

    Guard for the row-store read cutover recorded in the project charter. The
    charter calls the cutover a "pure swap" of :func:`_select_message_window`
    for ``lib.database.messages_rows.load_message_window`` — true on the same
    data, but NOT true across the whole table:

    the row store's backfill is incomplete (measured 2026-07-26: 3,696 of
    4,160 convs). For the remainder it answers ``totalCount=0``, which is
    indistinguishable from "this conversation is empty" — so a naive swap
    would silently render a 6-message conversation as empty and look correct
    doing it. A PARTIAL backfill is worse still: the window looks plausible
    while quietly dropping the oldest history.

    ``routes/conversations.py`` already keeps a migration-flag-independent
    blob tail-slice for exactly this reason. This helper gives conv_ref the
    same posture — comparing the row count against the authoritative blob's
    and refusing the row store unless it is at least as complete.

    Fails CLOSED toward the blob: any error returns False, because the blob is
    always complete and always authoritative.

    Args:
        db: DB wrapper.
        conversation_id: conv to check.
        blob_count: number of messages in the authoritative ``messages`` array.

    Returns:
        True iff the row store holds exactly ``blob_count`` rows.  Extra rows
        are stale tail data after a failed truncation mirror and are just as
        unsafe as missing rows.
    """
    try:
        from lib.database.messages_rows import mirror_is_current
        ok = mirror_is_current(
            db, conversation_id, expected_count=int(blob_count or 0))
    except Exception as e:
        logger.debug('[conv_ref] row-store marker probe failed conv=%s: %s '
                     '— falling back to the authoritative blob',
                     (conversation_id or '')[:12], e)
        return False
    if not ok:
        logger.debug('[conv_ref] row store is incomplete, stale, or not '
                     'revision-marked for conv=%s — using the blob',
                     (conversation_id or '')[:12])
    return ok


def _select_message_window(messages, head, tail, before=None):
    """Pick a HEAD+TAIL window of messages, preserving original indices.

    Shared by the prose transcript and the raw dump so the two can never
    disagree about what a trimmed read contains. ``build_conversation_digest``
    applies the same head+tail policy for the human card — previously ONLY the
    card did, so the model got a head-only slice while the human reading the
    same row got the conclusion.

    Args:
        messages: the full ordered message list.
        head: opening messages always kept.
        tail: most-recent messages kept.
        before: cursor — when set, treat the conversation as ending just
            BEFORE this 0-based index, so a caller can walk backwards through
            history instead of being stuck with one fixed window.

    Returns:
        ``(kept, omitted, total)`` where ``kept`` is a list of
        ``(original_index, message)`` pairs in ascending order.
    """
    total = len(messages)
    end = total if before is None else max(0, min(int(before), total))
    if end <= head + tail:
        return list(enumerate(messages[:end])), 0, total
    tail_start = end - tail
    kept = list(enumerate(messages[:head]))
    kept += [(i, messages[i]) for i in range(tail_start, end)]
    return kept, tail_start - head, total

# ── Conversation-digest (human-view card) shaping constants ──
# The digest is a bounded PROJECTION of a conversation for the frontend card,
# NOT the verbatim transcript (that stays in get_conversation / the "model
# view" button). A long conversation keeps its HEAD (what it was about) and its
# TAIL (where it ended up / the conclusion) with a "… X omitted …" marker in
# between — showing only the opening N messages is the least useful slice.
DIGEST_HEAD = 3          # opening messages always kept (the "what is this about")
DIGEST_TAIL = 100        # most-recent messages kept (the "where did it end up")
DIGEST_PREVIEW = 750     # per-message text preview length (chars)
DIGEST_FULL_CAP = 8000   # per-message expandable full-text cap (chars)
# NOTE (2026-07-23): tail/preview/full were widened (60/400/4000 → 100/750/8000)
# because L0 disk-persistence (lib/tasks_pkg/compaction) is the safety net for an
# oversized RENDERED result — the digest can afford to carry more of the
# conversation. This is a deliberate, bounded widening, NOT "unlimited": the
# digest stays a PROJECTION (the verbatim record is the model-view transcript).


def _digest_tool_desc(rnd):
    """Build a compact ``{name, arg, status}`` descriptor for one tool round.

    Reuses the same primary-argument heuristic the prose renderer
    (:func:`_format_tool_rounds`) relies on — ``query`` first, then the common
    single-value arg keys — so the card shows ``read_files → lib/foo.py`` /
    ``run_command → git status`` instead of a bare tool name. Returns ``None``
    for a non-dict round or one with no resolvable name.
    """
    if not isinstance(rnd, dict):
        return None
    name = (rnd.get('toolName') or rnd.get('tool_name') or '').strip()
    if not name:
        return None
    arg = rnd.get('query') or ''
    if not arg:
        args = rnd.get('args') or rnd.get('arguments') or {}
        if isinstance(args, dict):
            for key in ('path', 'file_path', 'command', 'pattern', 'url',
                        'query', 'conversation_id', 'title'):
                if args.get(key):
                    arg = args[key]
                    break
            else:
                # Fall back to the first scalar arg value.
                for val in args.values():
                    if isinstance(val, (str, int, float)) and str(val).strip():
                        arg = val
                        break
    arg = _truncate(str(arg), 90) if arg else ''
    return {'name': name, 'arg': arg, 'status': rnd.get('status', 'done')}


def _msg_fallback_text(msg):
    """Fallback display text for a message whose ``content`` is empty.

    A tool-only assistant round (the model called tools and emitted no visible
    prose THAT round) has empty ``content`` — so a digest row for it would
    otherwise render as a bare "(no text)". A conversation's conclusion often
    sits amid such rounds, so an empty row buries exactly what the reader
    wants. Fall back to the round's ``thinking`` first (real prose), else a
    compact summary of its tool calls (name + primary arg). Returns '' only
    when there is genuinely nothing to show.
    """
    if not isinstance(msg, dict):
        return ''
    thinking = msg.get('thinking')
    if isinstance(thinking, str) and thinking.strip():
        return thinking.strip()
    parts = []
    for r in (msg.get('toolRounds') or []):
        d = _digest_tool_desc(r)
        if d:
            parts.append(d['name'] + (f' {d["arg"]}' if d['arg'] else ''))
    return ', '.join(parts)


def _coerce_json(value, default, label=''):
    """Parse a JSON column value regardless of DB backend.

    On SQLite the ``messages`` / ``settings`` columns come back as TEXT, and on
    PostgreSQL the JSONB columns are stringified by the driver's
    ``_jsonb_as_string`` type-caster (``lib/database/_core.py``) — so both
    normally arrive as ``str`` and go through :func:`safe_json`. This helper
    additionally tolerates a driver returning an already-decoded ``dict`` /
    ``list`` (the fallback path), mirroring the defensive pattern in
    ``lib/conversations/project_peer.py``.
    """
    if isinstance(value, (dict, list)):
        return value
    return safe_json(value, default=default, label=label)


def get_conversation(conversation_id, include_tool_details=True,
                     current_conv_id=None, raw=False, user_id=None,
                     limit=None, before=None):
    """Retrieve and format the content of a conversation.

    Selection is MESSAGE-level (head + tail), not a character cut: a long
    conversation keeps its opening messages AND its most-recent ones, with the
    omission stated in-band. The previous ``result[:MAX_CHARS]`` kept only the
    beginning — so on a long row the model lost the conclusion, which is
    usually the reason to open a past conversation at all.

    Args:
        conversation_id: ID of the conversation to fetch
        include_tool_details: whether to include full tool arguments/results
        current_conv_id: the current conversation's ID (to prevent self-reference loops)
        raw: when True, return the DB record as structured JSON for debugging.
            The record is WINDOWED before serialization (never cut mid-token),
            so the dump always parses.
        user_id: the OWNING principal. ``None`` falls back to
            :data:`DEFAULT_USER_ID` (single-user install behaves identically).
        limit: how many recent messages to render (default
            :data:`TRANSCRIPT_TAIL`).
        before: cursor — render the window ENDING just before this 1-based
            message number, so the caller can page backwards through a long
            history rather than being stuck at one window.

    Returns a formatted string with the selected messages, tool calls, and results.
    """
    if current_conv_id and conversation_id == current_conv_id:
        return "Error: Cannot reference the current conversation — you are already in it. Use list_conversations to find other conversations."

    db = _get_db()
    owner_id = DEFAULT_USER_ID if user_id is None else user_id

    def _blob_row():
        from lib.database.conversation_repository import load_conversation
        return load_conversation(
            db, conversation_id, user_id=owner_id,
            metadata_columns=(
                'title', 'created_at', 'updated_at', 'settings'))

    row = None
    row_backed = False
    if not raw:
        try:
            from lib.database.messages_rows import rows_read_enabled
            if rows_read_enabled():
                candidate = db.execute(
                    'SELECT id, user_id, title, created_at, updated_at, '
                    'settings, msg_count, rev FROM conversations '
                    'WHERE id=? AND user_id=?',
                    (conversation_id, owner_id),
                ).fetchone()
                if candidate and row_window_usable(
                        db, conversation_id,
                        int(candidate['msg_count'] or 0)):
                    row = candidate
                    row_backed = True
        except Exception as e:
            logger.debug('[conv_ref] metadata-only row gate failed conv=%s: %s',
                         (conversation_id or '')[:12], e)
    if row is None:
        row = _blob_row()

    if not row:
        return f"Error: Conversation '{conversation_id}' not found. Use list_conversations to find valid conversation IDs."

    if raw:
        return _render_raw_conversation(row, conversation_id,
                                        limit=limit, before=before)

    # ★ Layer 2 trigger: PAUSED. The sidebar conversation-summary feature is
    #   unstable (render location + timing issues), so we no longer REQUEST
    #   generation here. The engine (lib/conversations/project_summary) is left
    #   intact for a later revival; the post-reply trigger in
    #   lib/tasks_pkg/manager/_sync.py is likewise disabled. Revisit later.

    title = row['title'] or '(untitled)'
    _tail = TRANSCRIPT_TAIL if limit is None else max(1, int(limit))
    _before = None if before is None else max(0, int(before) - 1)
    settings = _coerce_json(row['settings'], default={},
                            label='conv-ref-settings')

    kept = None
    if row_backed:
        try:
            from lib.database.messages_rows import load_message_selection
            selected = load_message_selection(
                db, conversation_id, TRANSCRIPT_HEAD, _tail,
                before_seq=_before, known_total=int(row['msg_count'] or 0))
            kept = selected['kept']
            omitted = selected['omitted']
            total = selected['totalCount']
        except Exception as e:
            logger.debug('[conv_ref] row selection failed conv=%s: %s — '
                         'falling back to the authoritative blob',
                         (conversation_id or '')[:12], e)
            row = _blob_row()
            row_backed = False
            if row is None:
                return (f"Error: Conversation '{conversation_id}' not found. "
                        'Use list_conversations to find valid conversation IDs.')

    if not row_backed:
        messages = row.messages
        if not messages:
            return (f"Conversation '{title}' [{conversation_id}] exists but "
                    'has no messages.')
        kept, omitted, total = _select_message_window(
            messages, TRANSCRIPT_HEAD, _tail, before=_before)
    elif not kept:
        return (f"Conversation '{title}' [{conversation_id}] exists but "
                'has no messages.')

    # Build formatted output
    parts = []
    parts.append(f"{'═' * 60}")
    parts.append(f"Referenced Conversation: \"{title}\"")
    parts.append(f"   ID: {conversation_id}")
    if settings.get('preset'):
        parts.append(f"   Model preset: {settings['preset']}")
    parts.append(f"   Messages: {total}")
    if omitted or len(kept) < total:
        shown = ', '.join(str(i + 1) for i, _ in kept[:1] + kept[-1:])
        parts.append(f"   Showing {len(kept)} of {total} (around #{shown})")
    parts.append(f"{'═' * 60}")
    parts.append("")

    _prev_idx = None
    for i, msg in kept:
        if _prev_idx is not None and i - _prev_idx > 1:
            parts.append(f"… [{i - _prev_idx - 1} message(s) omitted — "
                         f"re-read with before={i + 1} to see them] …")
            parts.append("")
        _prev_idx = i
        role = msg.get('role', 'unknown')
        content = msg.get('content', '')

        if role == 'user':
            parts.append(f"── User Message #{i+1} {'─' * 40}")
            # Handle text content
            text = _extract_text(content)
            if text:
                parts.append(text)

            # Note any images/PDFs
            if msg.get('images'):
                parts.append(f"  [Contains {len(msg['images'])} image(s)]")
            if msg.get('pdfTexts'):
                for pdf in msg['pdfTexts']:
                    parts.append(f"  [PDF: {pdf.get('name', 'unknown')} — {pdf.get('pages', '?')} pages]")
                    if include_tool_details and pdf.get('text'):
                        # Truncate very long PDFs
                        pdf_text = pdf['text']
                        if len(pdf_text) > 5000:
                            pdf_text = pdf_text[:5000] + f"\n... [truncated, {len(pdf['text'])} chars total]"
                        parts.append(f"  PDF Content:\n{pdf_text}")

        elif role == 'assistant':
            parts.append(f"── Assistant Response #{i+1} {'─' * 36}")

            # Content
            if content:
                parts.append(content)

            # Thinking/reasoning
            if msg.get('thinking') and include_tool_details:
                thinking = msg['thinking']
                if len(thinking) > 3000:
                    thinking = thinking[:3000] + f"\n... [thinking truncated, {len(msg['thinking'])} chars total]"
                parts.append(f"\n  [Thinking]: {thinking}")

            # Tool rounds (toolRounds)
            tool_rounds = msg.get('toolRounds', [])
            if tool_rounds:
                parts.append(_format_tool_rounds(tool_rounds, include_tool_details))

        parts.append("")  # blank line between messages

    # Tell the model how to reach what it did not get. A truncated result with
    # no next step is a dead end — it knows content is missing but has no way
    # to ask for it.
    if omitted:
        oldest_shown = kept[-1][0] + 1 - len(kept) + TRANSCRIPT_HEAD
        parts.append("")
        parts.append(f"[{omitted} earlier message(s) not shown. Re-read with "
                     f"before={max(1, oldest_shown)} to page backwards.]")

    # Trim trailing whitespace
    result = '\n'.join(parts).rstrip()

    # Character-level backstop. Message-level selection already bounds a normal
    # read; this only fires when a SINGLE message is itself enormous. Clamp
    # HEAD+TAIL rather than head-only so the end of the record survives here
    # too — a head-only cut at this layer would reintroduce exactly the bug
    # message-level selection was added to fix.
    if len(result) > MAX_CHARS:
        head_budget = int(MAX_CHARS * 0.6)
        tail_budget = int(MAX_CHARS * 0.35)
        elided = len(result) - head_budget - tail_budget
        result = (
            result[:head_budget]
            + f"\n\n... [{elided:,} chars elided from the middle of an "
              f"oversized message \u2014 this conversation's individual messages "
              f"are too large to render in full] ...\n\n"
            + result[-tail_budget:]
        )

    return result


def build_conversation_digest(conversation_id, current_conv_id=None,
                              head=DIGEST_HEAD, tail=DIGEST_TAIL, raw=False,
                              user_id=None):
    """Build a STRUCTURED digest of a conversation for the human-view card.

    This is the display sibling of :func:`get_conversation` (which returns the
    verbatim prose transcript the MODEL reads). The frontend renders this dict
    as a clean, scannable card instead of dumping the raw ``═══`` / ``── User
    Message #`` ASCII separators as Markdown.

    Never re-parses the prose result — reads the same DB row and emits a
    typed structure (mirrors the ``boardSnapshot`` / ``peerStatus`` pattern in
    ``lib/tasks_pkg/handlers/misc/_brain.py``).

    HEAD+TAIL policy: a long conversation keeps its opening ``head`` messages
    (what it is about) AND its most-recent ``tail`` messages (where it ended
    up), with a structured ``omitted`` marker row between them — showing only
    the first N messages is the least useful slice. Each message carries a
    truncated ``text`` preview plus the ``full`` text (capped) so the frontend
    can expand a single message in place instead of forcing a jump to the
    "model view". Assistant messages carry per-round ``tools`` descriptors
    (name + primary arg + status), not just tool names.

    Args:
        conversation_id: the conversation to summarize.
        current_conv_id: the active conversation (self-reference is a no-op).
        head: opening messages always kept.
        tail: most-recent messages kept.
        raw: when True, mark the digest ``raw: true`` + carry the row-level
            ``rev``, and attach per-message low-level metadata
            (``model`` / ``usage`` / ``finishReason`` / ``msgId``) so the human
            card visibly reflects the debug read. Non-raw omits all of these.

    Returns:
        A dict ``{convId, title, preset, msgCount, createdAt, updatedAt,
        messages: [...], truncated, omitted}`` or ``None`` when the
        conversation can't be read (self-ref / missing / empty) so the caller
        falls back to the prose dump. Each message row is either a content row
        (``role``/``text``/``full``/``ts``/``tools``/…) or an omission marker
        (``{omitted: X}``).
    """
    if current_conv_id and conversation_id == current_conv_id:
        return None
    try:
        db = _get_db()
        from lib.database.conversation_repository import load_conversation
        row = load_conversation(
            db, conversation_id,
            user_id=DEFAULT_USER_ID if user_id is None else user_id,
            metadata_columns=('title', 'settings', 'created_at', 'updated_at'))
    except Exception as e:
        logger.debug('[conv_ref] digest DB read failed for %s: %s',
                     conversation_id, e)
        return None
    if not row:
        return None

    messages = row.messages
    if not isinstance(messages, list) or not messages:
        return None
    settings = _coerce_json(row['settings'], default={}, label='conv-digest-settings')

    def _preview(text, limit=DIGEST_PREVIEW):
        s = ' '.join(str(text or '').split())
        return (s[:limit] + '…') if len(s) > limit else s

    def _full(text):
        s = str(text or '').strip()
        return (s[:DIGEST_FULL_CAP] + '…') if len(s) > DIGEST_FULL_CAP else s

    n = len(messages)

    # ── TAIL ANCHORING ──
    # The tail must END on the conversation's CONCLUSION, not on a trailing
    # run of tool-only rounds. Find the last message that carries SUBSTANTIVE
    # prose — real ``content`` OR ``thinking`` (a round's model reasoning is
    # substantive; a bare cleanup tool call with neither is the "empty closer"
    # we drop) — and anchor the tail there, dropping the rounds after it.
    # Without this a tool-heavy ending fills the tail with blank rows and the
    # "where did it end up" half of head+tail shows nothing.
    def _is_anchor_worthy(m):
        if not isinstance(m, dict):
            return False
        if _extract_text(m.get('content', '')).strip():
            return True
        th = m.get('thinking')
        return isinstance(th, str) and bool(th.strip())

    last_content_idx = None
    for idx in range(n - 1, -1, -1):
        if _is_anchor_worthy(messages[idx]):
            last_content_idx = idx
            break
    tail_end = last_content_idx if last_content_idx is not None else n - 1
    trailing_dropped = (n - 1) - tail_end

    # HEAD+TAIL selection with 1-based original indices preserved, so a message
    # row always reports its true position in the conversation.
    if tail_end + 1 <= head + tail:
        # Head and tail windows meet/overlap — no middle gap.
        kept = list(enumerate(messages[:tail_end + 1]))
        omitted = 0
    else:
        tail_start = tail_end - tail + 1
        kept = list(enumerate(messages[:head]))
        kept += [(i, messages[i]) for i in range(tail_start, tail_end + 1)]
        omitted = tail_start - head

    def _row(i, msg):
        role = msg.get('role', 'unknown')
        full_text = _extract_text(msg.get('content', ''))
        is_fallback = False
        if not full_text.strip():
            fb = _msg_fallback_text(msg)
            if fb:
                full_text, is_fallback = fb, True
        preview = _preview(full_text)
        entry = {
            'index': i + 1,
            'role': role,
            'text': preview,
        }
        # A row whose text is a thinking/tool summary (not the message's own
        # visible content) is flagged so the frontend can style it as a
        # summary rather than pass it off as the real message.
        if is_fallback:
            entry['textFallback'] = True
        full = _full(full_text)
        # Only carry `full` when it adds something beyond the preview, so the
        # frontend knows whether an "expand" affordance is meaningful.
        if full and full != preview:
            entry['full'] = full
        ts = msg.get('timestamp') or msg.get('ts')
        if isinstance(ts, (int, float)) and ts > 0:
            entry['ts'] = int(ts)
        imgs = msg.get('images')
        if imgs:
            entry['images'] = len(imgs)
        pdfs = msg.get('pdfTexts')
        if pdfs:
            entry['pdfs'] = len(pdfs)
        if role == 'assistant':
            tools = []
            for r in (msg.get('toolRounds') or []):
                desc = _digest_tool_desc(r)
                if desc:
                    tools.append(desc)
            if tools:
                entry['tools'] = tools
        # ── RAW-mode per-message metadata (debug view) ──
        # Only in raw mode do we surface the low-level fields the prose/normal
        # card drops — a few compact chips per row (model / token usage /
        # finish reason / message id), NOT the whole message. This is what
        # makes a raw read visibly RICHER than a normal read in the human card
        # (previously identical). The full verbatim JSON still lives on the
        # "model view" channel.
        if raw:
            mdl = msg.get('model')
            if isinstance(mdl, str) and mdl.strip():
                entry['model'] = mdl.strip()
            fr = msg.get('finishReason')
            if isinstance(fr, str) and fr.strip():
                entry['finishReason'] = fr.strip()
            mid = msg.get('_msgId')
            if isinstance(mid, str) and mid.strip():
                entry['msgId'] = mid.strip()
            usage = msg.get('usage')
            if isinstance(usage, dict):
                inp = usage.get('input_tokens')
                out = usage.get('output_tokens')
                u = {}
                if isinstance(inp, (int, float)):
                    u['in'] = int(inp)
                if isinstance(out, (int, float)):
                    u['out'] = int(out)
                if u:
                    entry['usage'] = u
        return entry

    rows = []
    inserted_marker = False
    prev_idx = None
    for i, msg in kept:
        if not isinstance(msg, dict):
            continue
        # Insert the omission marker at the head/tail seam (first index jump).
        if (omitted and not inserted_marker and prev_idx is not None
                and i - prev_idx > 1):
            rows.append({'omitted': omitted})
            inserted_marker = True
        rows.append(_row(i, msg))
        prev_idx = i

    result = {
        'convId': conversation_id,
        'title': row['title'] or '(untitled)',
        'preset': settings.get('preset', ''),
        'msgCount': n,
        'createdAt': row['created_at'] or 0,
        'updatedAt': row['updated_at'] or 0,
        'messages': rows,
        'truncated': bool(omitted or trailing_dropped),
        'omitted': omitted,
        'trailingDropped': trailing_dropped,
    }
    if raw:
        # Mark the digest as a RAW/debug view + carry the row-level revision so
        # the frontend can render a distinct "RAW · debug" badge. Non-raw reads
        # get NONE of these keys (byte-identical to the prior behaviour).
        result['raw'] = True
        rev = row['rev']
        if isinstance(rev, (int, float)):
            result['rev'] = int(rev)
    return result


def _clamp_message_fields(msg, budget, max_items=None):
    """Return a copy of ``msg`` with over-long strings and arrays cut down.

    Used only by the raw dump's last-resort guard, when dropping whole messages
    still leaves the payload over :data:`MAX_CHARS` because an INDIVIDUAL
    message is enormous. Clamping happens on the parsed structure (never on the
    serialized text) so the dump stays valid JSON, and every clamped value
    carries an explicit marker — a silently shortened field would look like the
    complete value.

    ``max_items`` caps EVERY long array (keeping a head and a tail slice with a
    count marker between). Capping only ``toolRounds`` was not enough: on real
    rows the size was dominated by an ``images`` array holding base64 blobs
    (829 KB in one message) and a ``segments`` array of hundreds of small dicts
    (687 KB) — string clamping cannot shrink either, because their weight is
    the ITEM COUNT, not any single long value.
    """
    if not isinstance(msg, dict):
        return msg

    def _clamp(v):
        if isinstance(v, str) and len(v) > budget:
            head, tail = int(budget * 0.6), int(budget * 0.3)
            return (v[:head]
                    + f'\n… [{len(v) - head - tail:,} chars clamped] …\n'
                    + v[-tail:])
        if isinstance(v, list):
            if max_items is not None and len(v) > max_items:
                keep_head = max(1, max_items // 2)
                keep_tail = max(1, max_items - keep_head)
                return ([_clamp(x) for x in v[:keep_head]]
                        + [{'omittedItems': len(v) - keep_head - keep_tail}]
                        + [_clamp(x) for x in v[-keep_tail:]])
            return [_clamp(x) for x in v]
        if isinstance(v, dict):
            return {k: _clamp(x) for k, x in v.items()}
        return v

    return {k: _clamp(v) for k, v in msg.items()}


def _render_raw_conversation(row, conversation_id, limit=None, before=None):
    """Render the DB record of a conversation as a structured JSON dump.

    Used for debugging: preserves every field of every RENDERED message
    (``_msgId``, ``timestamp``, ``finishReason``, ``usage``, ``model``,
    ``modifiedFileList``, the complete ``toolRounds``, …) plus the row-level
    metadata columns and the raw ``settings``.

    The message list is WINDOWED (head + tail) BEFORE serialization rather
    than the JSON text being cut afterwards. The window is FITTED TO THE
    SERIALIZED BUDGET, not sized by the prose-tuned ``TRANSCRIPT_TAIL``
    constant: a raw record carries every message's full ``toolRounds``, so an
    unfitted ask of 60 was demolished by the over-budget guard (measured: 2 of
    205 messages delivered, field-clamped, under a header claiming nothing was
    summarized away). The fit is measured on the actual dump — pretty-print
    cost depends on nesting depth, so an estimate drifts — and the recent
    block is ALWAYS contiguous: when something has to give, the OLDEST tail
    candidate is dropped, never a middle message (a middle eviction reads as
    consecutive history with silent holes).

    The header states what was delivered (``DELIVERED N of M``), how to reach
    the rest (``before=`` / ``limit=``), and any clamping — a read that had to
    shrink fields must not look like a faithful one.
    """
    messages = _coerce_json(row['messages'], default=[], label='conv-ref-raw-messages')
    settings = _coerce_json(row['settings'], default={}, label='conv-ref-raw-settings')

    all_msgs = messages if isinstance(messages, list) else []
    total = len(all_msgs)
    _before = None if before is None else max(0, min(int(before) - 1, total))
    end = total if _before is None else _before

    head_n = min(TRANSCRIPT_HEAD, end)
    head_block = [(i, all_msgs[i]) for i in range(head_n)]

    base = {
        'id': row['id'],
        'user_id': row['user_id'],
        'title': row['title'] or '(untitled)',
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
        'msg_count': row['msg_count'],
        'rev': row['rev'],
        'settings': settings,
        'messageCount': total,
    }

    def _serialize(kept_pairs, omitted):
        rec = dict(base)
        rec.update({
            'truncated': bool(omitted),
            'omitted': omitted,
            'messageIndices': [i + 1 for i, _ in kept_pairs],
            'messages': [m for _, m in kept_pairs],
        })
        return rec, json.dumps(rec, ensure_ascii=False, indent=2, default=str)

    # Fit the tail greedily backwards from `end`, stopping when the next
    # (older) message would push the serialized record over the budget. The
    # ending message is always kept — a debugging read is usually about how
    # the conversation ENDED; if that one message alone overflows, the clamp
    # path below handles it honestly.
    budget = MAX_CHARS - 2048  # header + fence margin
    tail_cap = end - head_n
    if limit is not None:
        tail_cap = min(tail_cap, max(1, int(limit)))
    tail = []
    record, dump = None, ''
    for i in range(end - 1, head_n - 1, -1):
        if len(tail) >= tail_cap:
            break
        cand = [(i, all_msgs[i])] + tail
        rec, d = _serialize(head_block + cand,
                            end - head_n - len(cand))
        if len(d) > budget and tail:
            break
        tail, record, dump = cand, rec, d
    if record is None:  # end == head_n: no tail slot at all
        record, dump = _serialize(head_block, 0)
        tail = []
    omitted = end - head_n - len(tail)

    header_lines = [
        f"{'═' * 60}",
        f"Raw Conversation Record: \"{record['title']}\"",
        f"   ID: {conversation_id}",
        f"   DELIVERED {len(record['messages'])} of {total} messages"
        f"  (msg_count column: {row['msg_count']}, rev: {row['rev']})",
    ]
    if omitted:
        oldest_tail_1based = (end - len(tail)) + 1
        header_lines.append(
            f"   … {omitted} earlier message(s) not delivered — page back with "
            f"before={oldest_tail_1based}; widen the window with limit=N")
    header_lines.append(f"{'═' * 60}")
    header = '\n'.join(header_lines) + '\n'

    # Last-resort clamp: only reachable when the MINIMAL window itself
    # overflows (one enormous message — measured 436 KB on production). The
    # fitted path above never needs it. Everything here operates on the
    # STRUCTURE and re-serializes, so the payload is never cut mid-token, and
    # every degradation is disclosed in the header.
    if len(dump) > MAX_CHARS:
        clamp_budget = max(1000, MAX_CHARS // max(1, len(record['messages'])) // 2)
        items_cap = 12
        original = record['messages']
        for _ in range(8):
            record['messages'] = [
                _clamp_message_fields(m, clamp_budget, max_items=items_cap)
                for m in original]
            record['truncated'] = True
            record['fieldsClamped'] = True
            dump = json.dumps(record, ensure_ascii=False, indent=2, default=str)
            if len(dump) <= MAX_CHARS:
                break
            clamp_budget = max(200, clamp_budget // 2)
            items_cap = max(2, items_cap // 2)
        else:
            # Pathological: many small strings (a huge toolRounds array) that
            # per-field clamping can't shrink enough. Keep the LAST message —
            # the conclusion is the single most useful row — under a hard
            # clamp, rather than dropping everything and returning bare
            # metadata.
            logger.warning('[conv_ref] raw dump for %s still %s chars after '
                           'clamping — keeping only the final message',
                           conversation_id, len(dump))
            tail_msg = original[-1] if original else None
            record['messages'] = (
                [_clamp_message_fields(tail_msg, 400, max_items=2)]
                if tail_msg else [])
            record['messageIndices'] = record['messageIndices'][-1:]
            record['omitted'] = total - len(record['messages'])
            record['reducedToFinalMessage'] = True
            dump = json.dumps(record, ensure_ascii=False, indent=2, default=str)
            # Even that can overflow on a single pathological message; drop to
            # metadata only as the last honest resort.
            if len(dump) > MAX_CHARS:
                record['messages'] = []
                record['messagesDropped'] = True
                dump = json.dumps(record, ensure_ascii=False, indent=2,
                                  default=str)
        if record.get('fieldsClamped') or record.get('reducedToFinalMessage'):
            border = '═' * 60 + '\n'
            note = ('   CLAMPED: fields were cut to fit the budget '
                    '(see fieldsClamped/reducedToFinalMessage)\n')
            idx = header.rfind(border)
            header = (header[:idx] + note + header[idx:]) if idx >= 0 \
                else header + note

    return f"{header}```json\n{dump}\n```"


def _extract_text(content):
    """Extract text from a message content field (string or multimodal array)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict):
                if part.get('type') == 'text':
                    texts.append(part.get('text', ''))
                elif part.get('type') == 'image_url':
                    texts.append('[image]')
            elif isinstance(part, str):
                texts.append(part)
        return '\n'.join(texts)
    return str(content) if content else ''


def _format_tool_rounds(rounds, include_details=True):
    """Format tool call rounds from toolRounds data."""
    if not rounds:
        return ""

    parts = ["\n  Tool Calls:"]
    for j, rnd in enumerate(rounds):
        tool_name = rnd.get('toolName', rnd.get('tool_name', 'unknown'))
        status = rnd.get('status', 'done')

        # Build call signature
        call_desc = f"    {j+1}. {tool_name}"

        # Add key arguments based on tool type
        query = rnd.get('query', '')
        if query:
            call_desc += f"({_truncate(query, 120)})"

        call_desc += f"  [{status}]"
        parts.append(call_desc)

        if include_details:
            # Show arguments if present
            args = rnd.get('args', rnd.get('arguments', {}))
            if args and isinstance(args, dict):
                for key, val in args.items():
                    val_str = str(val)
                    if len(val_str) > 500:
                        val_str = val_str[:500] + '...'
                    parts.append(f"       {key}: {val_str}")

            # Show results
            results = rnd.get('results', rnd.get('result', []))
            if results:
                if isinstance(results, list):
                    for res in results:
                        res_text = _extract_result_text(res)
                        if res_text:
                            if len(res_text) > 3000:
                                res_text = res_text[:3000] + f'\n       ... [result truncated, {len(res_text)} chars total]'
                            parts.append(f"       → {res_text}")
                elif isinstance(results, str):
                    if len(results) > 3000:
                        results = results[:3000] + '\n       ... [result truncated]'
                    parts.append(f"       → {results}")

    return '\n'.join(parts)


def _extract_result_text(result):
    """Extract readable text from a tool result entry."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        # Common patterns in toolRounds results
        if 'text' in result:
            return result['text']
        if 'content' in result:
            return result['content']
        if 'title' in result and 'snippet' in result:
            return f"{result['title']}: {result['snippet']}"
        if 'title' in result and 'url' in result:
            return f"{result['title']} — {result['url']}"
        # Fallback: compact JSON
        try:
            return json.dumps(result, ensure_ascii=False)[:2000]
        except (TypeError, ValueError):
            logger.debug('JSON serialization failed for tool result, falling back to str()', exc_info=True)
            return str(result)[:2000]
    return str(result)[:2000] if result else ''


def _truncate(text, max_len=120):
    """Truncate text with ellipsis."""
    text = str(text).replace('\n', ' ').strip()
    if len(text) > max_len:
        return text[:max_len] + '...'
    return text
