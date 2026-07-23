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
                     current_conv_id=None, raw=False):
    """Retrieve and format the full content of a conversation.

    Args:
        conversation_id: ID of the conversation to fetch
        include_tool_details: whether to include full tool arguments/results
        current_conv_id: the current conversation's ID (to prevent self-reference loops)
        raw: when True, return the full DB record (all row columns + settings +
            the complete messages array with every field preserved) as a
            structured JSON dump for debugging, instead of the readable prose
            transcript.

    Returns a formatted string with all messages, tool calls, and results.
    """
    if current_conv_id and conversation_id == current_conv_id:
        return "Error: Cannot reference the current conversation — you are already in it. Use list_conversations to find other conversations."

    db = _get_db()
    row = db.execute(
        'SELECT id, user_id, title, messages, created_at, updated_at, '
        'settings, msg_count, rev FROM conversations WHERE id=? AND user_id=?',
        (conversation_id, DEFAULT_USER_ID)
    ).fetchone()

    if not row:
        return f"Error: Conversation '{conversation_id}' not found. Use list_conversations to find valid conversation IDs."

    if raw:
        return _render_raw_conversation(row, conversation_id)

    # ★ Layer 2 trigger: lazily (re)generate this conversation's project
    #   summary in the background when the model first reads it. Non-blocking —
    #   the summary engine self-gates on staleness, so this is a cheap no-op
    #   for an already-summarized, unchanged conversation.
    try:
        from lib.conversations.project_summary import ensure_summary
        ensure_summary(conversation_id, blocking=False)
    except Exception as e:
        logger.debug('[conv_ref] summary trigger skipped for %s: %s',
                     conversation_id, e)

    title = row['title'] or '(untitled)'
    messages = _coerce_json(row['messages'], default=[], label='conv-ref-messages')

    if not messages:
        return f"Conversation '{title}' [{conversation_id}] exists but has no messages."

    # Parse settings for model info
    settings = _coerce_json(row['settings'], default={}, label='conv-ref-settings')

    # Build formatted output
    parts = []
    parts.append(f"{'═' * 60}")
    parts.append(f"Referenced Conversation: \"{title}\"")
    parts.append(f"   ID: {conversation_id}")
    if settings.get('preset'):
        parts.append(f"   Model preset: {settings['preset']}")
    parts.append(f"   Messages: {len(messages)}")
    parts.append(f"{'═' * 60}")
    parts.append("")

    for i, msg in enumerate(messages):
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

    # Trim trailing whitespace
    result = '\n'.join(parts).rstrip()

    # Safety: cap total length to avoid flooding context
    if len(result) > MAX_CHARS:
        result = result[:MAX_CHARS] + f"\n\n... [output truncated at {MAX_CHARS} chars — conversation has more content]"

    return result


def build_conversation_digest(conversation_id, current_conv_id=None,
                              head=DIGEST_HEAD, tail=DIGEST_TAIL, raw=False):
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
        row = db.execute(
            'SELECT id, title, messages, settings, created_at, updated_at, rev '
            'FROM conversations WHERE id=? AND user_id=?',
            (conversation_id, DEFAULT_USER_ID)
        ).fetchone()
    except Exception as e:
        logger.debug('[conv_ref] digest DB read failed for %s: %s',
                     conversation_id, e)
        return None
    if not row:
        return None

    messages = _coerce_json(row['messages'], default=[], label='conv-digest-messages')
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


def _render_raw_conversation(row, conversation_id):
    """Render the full DB record of a conversation as a structured JSON dump.

    Used for debugging: preserves every field of every message (``_msgId``,
    ``timestamp``, ``finishReason``, ``usage``, ``model``, ``modifiedFileList``,
    the complete ``toolRounds``, …) plus the row-level metadata columns
    (``created_at``, ``updated_at``, ``msg_count``, ``rev``) and the raw
    ``settings``. Nothing is truncated per-field; the whole payload is capped
    at :data:`MAX_CHARS` so it cannot flood the context window.
    """
    messages = _coerce_json(row['messages'], default=[], label='conv-ref-raw-messages')
    settings = _coerce_json(row['settings'], default={}, label='conv-ref-raw-settings')

    record = {
        'id': row['id'],
        'user_id': row['user_id'],
        'title': row['title'] or '(untitled)',
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
        'msg_count': row['msg_count'],
        'rev': row['rev'],
        'settings': settings,
        'messages': messages,
    }

    header = (
        f"{'═' * 60}\n"
        f"Raw Conversation Record: \"{record['title']}\"\n"
        f"   ID: {conversation_id}\n"
        f"   Messages: {len(messages) if isinstance(messages, list) else '?'}"
        f"  (msg_count column: {row['msg_count']}, rev: {row['rev']})\n"
        f"{'═' * 60}\n"
    )

    try:
        dump = json.dumps(record, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError) as e:
        logger.warning('[conv_ref] raw dump JSON serialization failed for %s: %s',
                       conversation_id, e, exc_info=True)
        dump = str(record)

    result = f"{header}```json\n{dump}\n```"

    if len(result) > MAX_CHARS:
        result = result[:MAX_CHARS] + (
            f"\n... [raw record truncated at {MAX_CHARS} chars — "
            f"conversation has more content]")
    return result


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
