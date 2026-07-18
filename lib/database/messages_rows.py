"""Phase 5 — "messages-as-rows" migrator, dual-write, and verification gate.

The conversation store is moving from a single ``conversations.messages`` JSONB
array (two writers) toward individually-addressable rows in
``conversation_messages`` (server-only writes). This module is the **migrator
layer**, landed FIRST and entirely behind the ``TOFU_MESSAGES_ROWS`` flag:

  * :func:`message_to_row` / :func:`row_to_message` — lossless split of one
    message dict into the row shape and back. The four columns
    :func:`lib.conversations.search_index.build_search_text` reads (``role``,
    ``content``, ``thinking``, ``translated_content``) are first-class so the
    search blob can be reconstructed from rows alone; the WHOLE original dict is
    also preserved verbatim in ``meta`` so a row round-trips with zero field
    loss.
  * :func:`backfill_conv` — idempotent one-shot backfill of one conversation's
    JSONB array into rows (delete-then-insert under the conv_id).
  * :func:`dual_write_conv` — the dual-WRITE hook: mirror a JSONB write into
    rows. A no-op unless ``rows_write_enabled()``. NEVER raises into the caller
    (mirroring is best-effort; the JSONB array stays authoritative).
  * :func:`verify_search_text_parity` / :func:`verify_conv_parity` — the
    **gate**: reconstruct messages from the row round-trip and assert the
    resulting ``build_search_text`` is BYTE-IDENTICAL to the JSONB-derived one.
    Reads must NOT be flipped to rows until this passes on real data.

Nothing here changes any read path. ``rows_read_enabled()`` exists so a later,
separately-gated step can flip reads — it defaults off and is independent of
the write flag, so dual-write can run and be verified for as long as needed
before any read cutover.
"""

from __future__ import annotations

import json
import os

from lib.log import get_logger
from lib.conversations.search_index import build_search_text

logger = get_logger(__name__)


# ── Flags ──────────────────────────────────────────────────────────────
# TOFU_MESSAGES_ROWS gates the WRITE side (backfill + dual-write). Default OFF.
# TOFU_MESSAGES_ROWS_READ separately gates the READ cutover. Default OFF, and
# deliberately decoupled: we keep dual-writing + verifying for as long as we
# want before ever serving reads from rows.
def _truthy(v) -> bool:
    return str(v or '').strip().lower() in ('1', 'true', 'yes', 'on')


def rows_write_enabled() -> bool:
    """Whether dual-write / backfill into conversation_messages is active."""
    return _truthy(os.environ.get('TOFU_MESSAGES_ROWS'))


def rows_read_enabled() -> bool:
    """Whether reads should be served from conversation_messages.

    Independent of the write flag AND requires the write flag too — you can
    never read from rows that aren't being written. This is the cutover switch;
    it must stay OFF until verify_*_parity is proven on real data.
    """
    return rows_write_enabled() and _truthy(os.environ.get('TOFU_MESSAGES_ROWS_READ'))


# ── Lossless message <-> row mapping ─────────────────────────────────────
# build_search_text reads exactly: role, content (str OR list-of-parts),
# thinking, translatedContent. We hoist those into typed columns. content_json
# holds the multipart-list form (as a JSON string); content holds the plain
# string form. Exactly one is non-empty per row, mirroring the str-vs-list
# branch in build_search_text so the reconstruction takes the same path.

def message_to_row(conv_id: str, seq: int, msg: dict, *, now_ms: int = 0) -> dict:
    """Split one message dict into a conversation_messages row dict.

    The full original ``msg`` is stored verbatim under ``meta`` so
    :func:`row_to_message` can return the byte-for-byte original. The hoisted
    columns are derived views used only for search reconstruction + addressing.
    """
    if not isinstance(msg, dict):
        msg = {}
    role = msg.get('role', '') or ''
    content = msg.get('content', '')
    content_str = ''
    content_json = '[]'
    if isinstance(content, list):
        content_json = json.dumps(content, ensure_ascii=False)
    elif isinstance(content, str):
        content_str = content
    thinking = msg.get('thinking', '')
    if not isinstance(thinking, str):
        thinking = ''
    translated = msg.get('translatedContent', '')
    if not isinstance(translated, str):
        translated = ''
    return {
        'conv_id': conv_id,
        'seq': seq,
        'msg_id': msg.get('_msgId', '') or '',
        'role': role,
        'content': content_str,
        'content_json': content_json,
        'thinking': thinking,
        'translated_content': translated,
        'meta': json.dumps(msg, ensure_ascii=False),
        'created_at': now_ms,
        'updated_at': now_ms,
    }


def row_to_message(row) -> dict:
    """Reconstruct the original message dict from a row.

    ``meta`` is the authoritative copy of the original dict, so this is just a
    parse of ``meta`` — guaranteeing field-for-field fidelity (the hoisted
    columns are never read back; they exist for SQL-side search/addressing).
    """
    meta = row['meta'] if not isinstance(row, (tuple, list)) else None
    if meta is None:
        # positional row: meta is at a known index only if caller used SELECT *
        # — callers should pass dict-like rows. Defensive fallthrough.
        try:
            meta = row[8]
        except (IndexError, TypeError) as e:
            logger.debug('[messages_rows] positional meta extract failed: %s', e)
            meta = '{}'
    if isinstance(meta, (bytes, bytearray)):
        meta = meta.decode('utf-8', 'replace')
    try:
        obj = json.loads(meta) if isinstance(meta, str) else (meta or {})
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug('[messages_rows] meta JSON parse failed: %s', e)
        obj = {}
    return obj if isinstance(obj, dict) else {}


def rows_to_messages(rows) -> list:
    """Reconstruct the ordered messages list from conversation_messages rows.

    Rows MUST be supplied ordered by ``seq`` (the caller's SELECT does the
    ORDER BY); this preserves the original array order.
    """
    return [row_to_message(r) for r in rows]


# ── Backfill / dual-write ────────────────────────────────────────────────

def _parse_messages(messages):
    if isinstance(messages, str):
        try:
            messages = json.loads(messages)
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[messages_rows] messages JSON parse failed: %s', e)
            return []
    return messages if isinstance(messages, list) else []


def backfill_conv(db, conv_id: str, messages, *, now_ms: int = 0, commit: bool = True) -> int:
    """Idempotently (re)write one conversation's rows from its JSONB array.

    Delete-then-insert under ``conv_id`` so re-running converges to the same
    state (idempotent). Returns the number of rows written. Caller owns the
    flag check — this does the work unconditionally so it can be used by an
    explicit backfill script even when the runtime flag is off.
    """
    from lib.database._core_schema import CONVERSATION_MESSAGES, upsert
    msgs = _parse_messages(messages)
    db.execute('DELETE FROM conversation_messages WHERE conv_id=?', (conv_id,))
    for seq, msg in enumerate(msgs):
        row = message_to_row(conv_id, seq, msg, now_ms=now_ms)
        upsert(db, CONVERSATION_MESSAGES, row,
               conflict_cols=['conv_id', 'seq'], commit=False)
    if commit:
        db.commit()
    return len(msgs)


def dual_write_conv(db, conv_id: str, messages, *, now_ms: int = 0) -> None:
    """Mirror a JSONB ``messages`` write into conversation_messages rows.

    Best-effort: a no-op when the flag is off, and swallows every exception so
    a mirroring failure can NEVER break the authoritative JSONB write path.
    """
    if not rows_write_enabled():
        return
    try:
        backfill_conv(db, conv_id, messages, now_ms=now_ms, commit=False)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning('[messages_rows] dual-write mirror failed conv=%s (non-fatal): %s',
                       (conv_id or '')[:12], e)


# ── Windowed read (tail window + page-up) ─────────────────────────────────

def load_message_window(db, conv_id: str, limit: int, before_seq=None) -> dict:
    """Load a WINDOW of a conversation's messages from the row store.

    The root-cause fix for slow first-open of long conversations: instead of
    detoasting + parsing the whole ``conversations.messages`` blob (cost linear
    in history length), read only ``limit`` rows via the ``idx_conv_msgs_conv``
    ``(conv_id, seq)`` index (cost constant in the window).

    Args:
        db: DB wrapper.
        conv_id: conversation id.
        limit: max messages to return (the window size N). ``<=0`` disables the
            window and returns the whole conversation.
        before_seq: when set, page UPWARD — return the window of messages with
            ``seq < before_seq`` (the ``limit`` messages ending just before it).
            When ``None``, return the TAIL window (the newest ``limit``).

    Returns a dict::

        {
          'messages':      [msg, ...],   # ascending seq order
          'totalCount':    int,          # total rows for this conv
          'firstLoadedSeq': int|None,    # seq of the first (oldest) returned msg
          'lastLoadedSeq':  int|None,    # seq of the last (newest) returned msg
          'hasMore':       bool,         # True iff older messages exist above the window
        }

    Pure read: no writes, never mutates. Callers gate on ``rows_read_enabled()``
    and fail open to the single-blob path on any error.
    """
    # total count is authoritative from the row store (COUNT on the covering
    # index); callers that already know msg_count may skip this, but keeping it
    # here makes the function self-contained.
    try:
        cnt_row = db.execute(
            'SELECT COUNT(*) AS n FROM conversation_messages WHERE conv_id=?',
            (conv_id,)).fetchone()
        total = int(cnt_row['n'] if hasattr(cnt_row, 'keys') else cnt_row[0]) if cnt_row else 0
    except Exception as e:
        logger.debug('[messages_rows] window count failed conv=%s: %s',
                     (conv_id or '')[:12], e)
        total = 0

    if limit is None or limit <= 0:
        rows = db.execute(
            'SELECT meta, seq FROM conversation_messages WHERE conv_id=? ORDER BY seq',
            (conv_id,)).fetchall()
    elif before_seq is not None:
        # page upward: the `limit` messages with seq < before_seq (newest of the
        # older block first, then reversed to ascending).
        rows = db.execute(
            'SELECT meta, seq FROM conversation_messages WHERE conv_id=? AND seq<? '
            'ORDER BY seq DESC LIMIT ?', (conv_id, int(before_seq), int(limit))).fetchall()
        rows = list(reversed(rows))
    else:
        # tail window: the newest `limit` messages, reversed to ascending.
        rows = db.execute(
            'SELECT meta, seq FROM conversation_messages WHERE conv_id=? '
            'ORDER BY seq DESC LIMIT ?', (conv_id, int(limit))).fetchall()
        rows = list(reversed(rows))

    def _seq(r):
        try:
            return int(r['seq'] if hasattr(r, 'keys') else r[1])
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    msgs = rows_to_messages(rows)
    first_seq = _seq(rows[0]) if rows else None
    last_seq = _seq(rows[-1]) if rows else None
    has_more = bool(first_seq is not None and first_seq > 0)
    return {
        'messages': msgs,
        'totalCount': total,
        'firstLoadedSeq': first_seq,
        'lastLoadedSeq': last_seq,
        'hasMore': has_more,
    }


# ── Verification gate ─────────────────────────────────────────────────────

def verify_search_text_parity(messages) -> bool:
    """Return True iff a row round-trip preserves ``build_search_text`` exactly.

    Reconstructs messages from the in-memory row round-trip
    (``message_to_row`` → ``row_to_message``) and asserts the resulting search
    blob is BYTE-IDENTICAL to the one built directly from the input. This is
    the read-cutover gate: it proves the row representation loses no
    search-relevant text. Pure / connection-free so it can run on real data
    pulled from the DB without a write.
    """
    msgs = _parse_messages(messages)
    expected = build_search_text(msgs)
    rows = [message_to_row('verify', i, m) for i, m in enumerate(msgs)]
    reconstructed = rows_to_messages(rows)
    got = build_search_text(reconstructed)
    if got != expected:
        logger.error('[messages_rows] search_text parity MISMATCH: '
                     'expected %d chars, got %d chars', len(expected), len(got))
        return False
    return True


def verify_conv_parity(db, conv_id: str) -> dict:
    """Verify ONE conversation already-backfilled in rows reproduces its JSONB
    array's ``build_search_text`` byte-for-byte.

    Reads the authoritative JSONB messages AND the conversation_messages rows
    independently, then compares both ``build_search_text`` outputs. Returns a
    verdict dict ``{ok, conv_id, jsonb_len, rows_len, jsonb_msgs, rows_msgs}``.
    Used by the verification harness BEFORE flipping reads.
    """
    jr = db.execute('SELECT messages FROM conversations WHERE id=?', (conv_id,)).fetchone()
    jsonb_msgs = _parse_messages(jr['messages'] if jr else [])
    rows = db.execute(
        'SELECT meta FROM conversation_messages WHERE conv_id=? ORDER BY seq', (conv_id,)
    ).fetchall()
    rows_msgs = rows_to_messages(rows)
    a = build_search_text(jsonb_msgs)
    b = build_search_text(rows_msgs)
    return {
        'ok': a == b,
        'conv_id': conv_id,
        'jsonb_len': len(a),
        'rows_len': len(b),
        'jsonb_msgs': len(jsonb_msgs),
        'rows_msgs': len(rows_msgs),
    }


__all__ = [
    'rows_write_enabled', 'rows_read_enabled',
    'message_to_row', 'row_to_message', 'rows_to_messages',
    'backfill_conv', 'dual_write_conv', 'load_message_window',
    'verify_search_text_parity', 'verify_conv_parity',
]
