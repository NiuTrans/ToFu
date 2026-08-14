"""Normalized conversation-message storage and cutover safeguards.

The conversation store is moving from a single ``conversations.messages`` JSONB
array (two writers) toward individually-addressable rows in
``conversation_messages`` (server-only writes). This module is the **migrator
layer**, landed first behind ``TOFU_MESSAGES_ROWS`` and now enabled by default
for ordinary personal-server runs after the fleet parity gate converged:

  * :func:`message_to_row` / :func:`row_to_message` — lossless split of one
    message JSON value into the row shape and back. The four columns
    :func:`lib.conversations.search_index.build_search_text` reads (``role``,
    ``content``, ``thinking``, ``translated_content``) are first-class so the
    search blob can be reconstructed from rows alone; the WHOLE original value
    is also preserved verbatim in ``meta`` so a row round-trips with zero field
    loss (including malformed historical scalar entries).
  * :func:`backfill_conv` — idempotent one-shot backfill of one conversation's
    JSONB array into rows (delete-then-insert under the conv_id).
  * :func:`dual_write_conv` — the dual-WRITE hook: mirror a JSONB write into
    rows. A no-op unless ``rows_write_enabled()``. NEVER raises into the caller
    (mirroring is best-effort; the JSONB array stays authoritative).
  * :func:`verify_search_text_parity` / :func:`verify_conv_parity` — the
    **gate**: reconstruct messages from the row round-trip and assert both the
    complete message list and ``build_search_text`` are identical to JSONB.
    Reads must NOT be flipped to rows until this passes on real data.

The explicit authority switch makes these rows canonical after verification.
At that point the legacy blob freezes, all semantic reads go through
``conversation_repository``, and a stale/missing row set fails loud instead of
resurrecting archive data.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

from lib.log import get_logger
from lib.conversations.search_index import build_search_text
from lib.database._wrappers import json_dumps_pg

logger = get_logger(__name__)

_activity_backfill_lock = threading.Lock()
_activity_backfill_thread = None


# ── Flags ──────────────────────────────────────────────────────────────
# TOFU_MESSAGES_ROWS gates the WRITE side (backfill + dual-write).
# TOFU_MESSAGES_ROWS_READ separately gates the READ cutover. Both default ON
# for an ordinary personal server now that every read also has an exact
# per-conversation revision/count/light-projection gate. Either env var may be
# set to 0 as an immediate kill switch. Pytest remains default-off so a
# developer's deployment flags and incidental mirror writes cannot leak into
# isolated fixtures; tests opt in explicitly when exercising the row store.
def _truthy(v) -> bool:
    return str(v or '').strip().lower() in ('1', 'true', 'yes', 'on')


_flag_file_engaged_logged = False


def _configured_flag(name: str) -> str | None:
    """Resolve a storage flag consistently in server and standalone tools."""
    value = os.environ.get(name)
    if value is not None:
        return value if str(value).strip() != '' else None
    # Deployment .env must never leak into isolated pytest fixtures; explicit
    # monkeypatched env variables above remain available to tests.
    if 'pytest' in sys.modules:
        return None
    from lib.env_compat import getenv_project_compat
    value = getenv_project_compat(name, default='')
    return value if str(value).strip() != '' else None


def _default_rows_enabled() -> bool:
    """Default-on only in the personal server; tooling/tests stay inert."""
    return ('pytest' not in sys.modules
            and os.environ.get('TOFU_SERVER_PROCESS') == '1')


def _flag_file_path() -> str:
    from lib.runtime_paths import data_root
    return os.path.join(data_root(), 'config', 'messages_rows_write.flag')


def _read_flag_file_path() -> str:
    from lib.runtime_paths import data_root
    return os.path.join(data_root(), 'config', 'messages_rows_read.flag')


def _authority_flag_file_path() -> str:
    from lib.runtime_paths import data_root
    return os.path.join(data_root(), 'config', 'messages_rows_authority.flag')


def _flag_file_on(path=None) -> bool:
    """Read the persistent write-flag file (pt_59140ecd ④).

    The owner-confirmed flip must survive EVERY future restart path — an
    env-var-only flip would silently revert the next time someone relaunches
    the server from a terminal that doesn't export TOFU_MESSAGES_ROWS, and
    the mirror would rot undetected. The deployment-local flag file makes
    the flip durable state, not launch-shell state. The env var stays the
    override in BOTH directions (``=0`` is the emergency kill switch even
    with the file present). Under pytest the default deployment path is
    never consulted (deployment state must not leak into the suite); tests
    exercise the file by passing an explicit ``path``.
    """
    if path is None:
        if 'pytest' in sys.modules:
            return False
        path = _flag_file_path()
    try:
        with open(path, encoding='utf-8') as f:
            return f.read().strip().lower() in ('1', 'true', 'on', 'yes')
    except OSError as _e:
        logger.debug('flag file on: unreadable (%s)', _e)
        return False


def rows_write_enabled() -> bool:
    """Whether dual-write / backfill into conversation_messages is active.

    Precedence: the env var ALWAYS wins when set (either value — ``=0`` is
    the kill switch); otherwise the persistent flag file decides.
    """
    env = _configured_flag('TOFU_MESSAGES_ROWS')
    if env is not None:
        return _truthy(env)
    on = _flag_file_on()
    if on:
        global _flag_file_engaged_logged
        if not _flag_file_engaged_logged:
            _flag_file_engaged_logged = True
            logger.info('[messages_rows] write flag engaged via persistent '
                        'flag file (%s)', _flag_file_path())
    return on or _default_rows_enabled()


def rows_read_enabled() -> bool:
    """Whether reads should be served from conversation_messages.

    Independently requested, but effective only while the write flag is also
    on — you can never read from rows that aren't being maintained. This is the
    cutover switch. It became default-on only after verify_*_parity converged
    on real data; the per-conversation marker remains the final fail-closed
    gate for every read.
    """
    env = _configured_flag('TOFU_MESSAGES_ROWS_READ')
    if env is not None:
        requested = _truthy(env)
    else:
        requested = (_flag_file_on(path=_read_flag_file_path())
                     or _default_rows_enabled())
    # Global intent is only the first gate. Every actual row read additionally
    # calls mirror_is_current(), which requires its per-conversation marker to
    # equal the DB-triggered authoritative rev and the exact row count.
    return rows_write_enabled() and requested


def rows_authority_enabled() -> bool:
    """Persistent, explicitly reversible normalized-row authority switch."""
    env = _configured_flag('TOFU_MESSAGES_ROWS_AUTHORITY')
    if env is not None:
        return _truthy(env)
    return _flag_file_on(path=_authority_flag_file_path())


def assert_rows_authority_ready(db) -> None:
    """Fail startup if an enabled row authority is structurally incomplete.

    The expensive byte-for-byte archive comparison is a one-time operator
    cutover audit. This cheap invariant gate runs on every later boot and
    protects canonical operation after the archive has intentionally stopped
    changing: revision marker, row count, all read projections, and ownership
    must be complete before the server accepts traffic.
    """
    if not rows_authority_enabled():
        return
    if not rows_read_enabled():
        raise RuntimeError(
            'message-row authority requires write and read flags to stay on')
    # SQLite has no INCLUDE columns. Explicitly select the compact covering
    # index so its unique ``id`` index cannot win the cost tie and force one
    # table lookup per 20 GB legacy-blob row. PostgreSQL's INCLUDE index is
    # selected normally and does not accept SQLite's INDEXED BY syntax.
    parent_hint = (' INDEXED BY idx_conv_rows_authority'
                   if getattr(db, 'dialect', '') == 'sqlite' else '')
    bad = db.execute(
        f'SELECT c.id FROM conversations c{parent_hint} '
        'LEFT JOIN conversation_messages cm ON cm.conv_id=c.id '
        'GROUP BY c.id, c.user_id, c.rev, c.messages_rows_rev, c.msg_count '
        'HAVING c.messages_rows_rev IS NULL '
        'OR c.messages_rows_rev<>c.rev '
        'OR COUNT(cm.seq)<>c.msg_count '
        'LIMIT 1').fetchone()
    if bad is not None:
        conv_id = bad['id'] if hasattr(bad, 'keys') else bad[0]
        raise RuntimeError(
            'message-row authority preflight failed for conversation '
            f'{conv_id}; refusing to serve a stale archive fallback')
    incomplete = db.execute(
        'SELECT conv_id FROM conversation_messages '
        'WHERE meta_light IS NULL OR message_ts IS NULL '
        'OR billing_meta IS NULL LIMIT 1').fetchone()
    if incomplete is not None:
        conv_id = (incomplete['conv_id'] if hasattr(incomplete, 'keys')
                   else incomplete[0])
        raise RuntimeError(
            'message-row authority projection is incomplete for conversation '
            f'{conv_id}; refusing to serve a lossy row projection')
    orphan = db.execute(
        'SELECT cm.conv_id FROM conversation_messages cm '
        'LEFT JOIN conversations c ON c.id=cm.conv_id '
        'WHERE c.id IS NULL LIMIT 1').fetchone()
    if orphan is not None:
        conv_id = orphan['conv_id'] if hasattr(orphan, 'keys') else orphan[0]
        raise RuntimeError(
            'message-row authority contains orphan transcript rows for '
            f'{conv_id}')


# ── Lossless message <-> row mapping ─────────────────────────────────────
# build_search_text reads exactly: role, content (str OR list-of-parts),
# thinking, translatedContent. We hoist those into typed columns. content_json
# holds the multipart-list form (as a JSON string); content holds the plain
# string form. Exactly one is non-empty per row, mirroring the str-vs-list
# branch in build_search_text so the reconstruction takes the same path.

_BILLING_PROJECTION_KEYS = (
    'timestamp', 'model', 'preset', 'effort',
    'provider_id', 'providerId',
)

_TRANSLATION_MESSAGE_KEYS = (
    'translatedContent',
    '_showingTranslation',
    '_translateDone',
    '_translateModel',
)


def translation_state_for_message(msg) -> dict:
    """Return the compact, versioned translation overlay for one message.

    Presence matters for the UI flags, so values are copied verbatim instead
    of normalized to booleans. Segment entries are keyed by their stable list
    position rather than ``llmRound``: historical data can contain duplicate
    or missing round numbers and the row round-trip must remain exact.
    """
    state = {'v': 1}
    if not isinstance(msg, dict):
        return state
    for key in _TRANSLATION_MESSAGE_KEYS:
        if key in msg:
            state[key] = msg[key]
    segments = msg.get('segments')
    if isinstance(segments, list):
        translated_segments = {}
        for index, segment in enumerate(segments):
            if isinstance(segment, dict) and 'translatedText' in segment:
                translated_segments[str(index)] = segment['translatedText']
        if translated_segments:
            state['segmentTranslatedText'] = translated_segments
    return state


def _billing_message_projection(fields) -> dict:
    """Keep only fields needed by the daily cost rollup."""
    if not isinstance(fields, dict):
        return {}
    projected = {
        key: fields[key] for key in _BILLING_PROJECTION_KEYS if key in fields
    }
    usage = fields.get('usage')
    if isinstance(usage, dict):
        # Historical usage dictionaries can carry large transport diagnostics
        # (notably ``_wire_*``). The daily rollup consumes only normalize_usage's
        # five counters, so persisting the canonical spellings keeps this
        # projection truly small without changing any cost arithmetic.
        from lib.cost import normalize_usage
        normalized = normalize_usage(usage)
        projected['usage'] = ({
            'prompt_tokens': normalized['input'],
            'completion_tokens': normalized['output'],
            'cache_write_tokens': normalized['cache_write'],
            'cache_read_tokens': normalized['cache_read'],
            'reasoning_tokens': normalized['thinking'],
        } if usage else {})
    return projected


def message_to_row(conv_id: str, seq: int, msg, *, now_ms: int = 0) -> dict:
    """Split one message JSON value into a conversation_messages row dict.

    The full original ``msg`` is stored verbatim under ``meta`` so
    :func:`row_to_message` can return the same JSON value. The hoisted
    columns are derived views used only for search reconstruction + addressing.

    ``meta`` and ``content_json`` are bound to JSONB columns, so they MUST be
    serialized with :func:`~lib.database._wrappers.json_dumps_pg` — the same
    serializer the authoritative blob writer uses. A bare ``json.dumps``
    encodes ``U+0000`` as ``\\u0000``, which PostgreSQL's JSONB parser rejects
    (``UntranslatableCharacter``); since ``dual_write_conv`` swallows write
    errors, such a row is silently dropped and the conversation is left with
    fewer rows than blob messages — the "partial backfill" shape that a
    windowed read renders as a silently truncated conversation.
    """
    # Derive indexed columns only from dict messages, but keep the original
    # JSON value in meta. Historical blobs can contain malformed/scalar list
    # entries; coercing them to {} made the supposedly lossless row cutover
    # silently alter the transcript.
    original = msg
    fields = msg if isinstance(msg, dict) else {}
    role = fields.get('role', '') or ''
    content = fields.get('content', '')
    content_str = ''
    content_json = '[]'
    if isinstance(content, list):
        content_json = json_dumps_pg(content)
    elif isinstance(content, str):
        content_str = content
    thinking = fields.get('thinking', '')
    if not isinstance(thinking, str):
        thinking = ''
    translated = fields.get('translatedContent', '')
    if not isinstance(translated, str):
        translated = ''
    try:
        message_ts = int(fields.get('timestamp', 0) or 0)
    except (TypeError, ValueError, OverflowError) as e:
        logger.debug('[MessagesRows] malformed message timestamp conv=%s '
                     'seq=%s: %s', conv_id, seq, e)
        message_ts = 0
    billing_meta = _billing_message_projection(fields)
    return {
        'conv_id': conv_id,
        'seq': seq,
        'msg_id': fields.get('_msgId', '') or '',
        'role': role,
        'content': content_str,
        'content_json': content_json,
        'thinking': thinking,
        'translated_content': translated,
        'meta': json_dumps_pg(original),
        'translation_state': json_dumps_pg(
            translation_state_for_message(original)),
        'meta_light': json_dumps_pg(light_message_for_window(original)),
        'message_ts': message_ts,
        'billing_meta': json_dumps_pg(billing_meta),
        'created_at': now_ms,
        'updated_at': now_ms,
    }


def light_message_for_window(msg):
    """Return the persistent first-paint projection for one message.

    This intentionally mirrors ``routes.conversations._trim_heavy_for_window``
    for the fields that dominate storage/network. It lives in the database
    layer so every mirror write materializes the cheap read form atomically
    beside the lossless ``meta`` value. Scalar historical entries and ordinary
    messages with neither heavy keys nor historical wire diagnostics are
    returned unchanged.

    Older authoritative blobs can contain multi-megabyte ``apiRounds[].usage``
    transport probes (the ``_wire_*`` namespace). Keeping those values in
    ``meta_light`` defeats the row window: SQLite still has to read and hand
    the payload to Python before the HTTP projection can remove it. Apply the
    same copy-on-change sanitizer as the transport path while retaining every
    UI-visible round/cost/token field. ``meta`` remains the lossless authority,
    so this derived projection never discards source data.
    """
    from lib.storage_projection import project_message_for_window
    return project_message_for_window(msg)


def row_to_message(row):
    """Reconstruct a message from its lossless base plus compact enrichments.

    Pre-v54 rows have a NULL/missing ``translation_state`` and therefore read
    byte-for-byte from ``meta``. A versioned overlay is authoritative for the
    handful of translation-only fields: this permits background translation
    to update a tiny column without rewriting a multi-megabyte base document.
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
        obj = (json.loads(meta) if isinstance(meta, str)
               else (meta if meta is not None else {}))
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug('[messages_rows] meta JSON parse failed: %s', e)
        obj = {}
    try:
        raw_state = row['translation_state']
    except (KeyError, IndexError, TypeError) as e:
        logger.debug('[messages_rows] translation overlay unavailable: %s', e)
        raw_state = None
    if isinstance(raw_state, (bytes, bytearray)):
        raw_state = raw_state.decode('utf-8', 'replace')
    try:
        state = (json.loads(raw_state) if isinstance(raw_state, str)
                 else raw_state)
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug('[messages_rows] translation overlay parse failed: %s', e)
        state = None
    if isinstance(obj, dict) and isinstance(state, dict) and state.get('v') == 1:
        # A materialized overlay is authoritative, including absence: this is
        # what lets a later normal write explicitly clear an older translation
        # that may still be present in the immutable base JSON.
        for key in _TRANSLATION_MESSAGE_KEYS:
            obj.pop(key, None)
            if key in state:
                obj[key] = state[key]
        segments = obj.get('segments')
        if isinstance(segments, list):
            for segment in segments:
                if isinstance(segment, dict):
                    segment.pop('translatedText', None)
            translated_segments = state.get('segmentTranslatedText')
            if isinstance(translated_segments, dict):
                for raw_index, value in translated_segments.items():
                    try:
                        index = int(raw_index)
                    except (TypeError, ValueError) as e:
                        logger.debug(
                            '[messages_rows] invalid translated segment index: %s',
                            e)
                        continue
                    if (0 <= index < len(segments)
                            and isinstance(segments[index], dict)):
                        segments[index]['translatedText'] = value
    return obj


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


def changed_message_seqs(before, after) -> list[int]:
    """Return exact row positions that must be mirrored for a blob replace.

    Positions newly present in ``after`` are dirty; removed tail positions do
    not appear because the mirror's range DELETE handles them.  A structural
    insertion/reorder naturally marks every shifted position, preserving exact
    array semantics without forcing a rebuild when only one early message was
    edited in place.
    """
    old = _parse_messages(before)
    new = _parse_messages(after)
    common = min(len(old), len(new))
    dirty = [i for i in range(common) if old[i] != new[i]]
    dirty.extend(range(common, len(new)))
    return dirty


def backfill_conv(db, conv_id: str, messages, *, now_ms: int = 0,
                  commit: bool = True, row_authority: bool = False,
                  user_id=None) -> int:
    """Idempotently (re)write one conversation's rows from its JSONB array.

    Delete-then-insert under ``conv_id`` so re-running converges to the same
    state (idempotent). Returns the number of rows written. Caller owns the
    flag check — this does the work unconditionally so it can be used by an
    explicit backfill script even when the runtime flag is off.
    """
    from lib.database._core_schema import CONVERSATION_MESSAGES, upsert
    msgs = _parse_messages(messages)
    db.execute('DELETE FROM conversation_messages WHERE conv_id=?', (conv_id,))
    # Real blobs can carry TWO messages sharing one _msgId (the pt_97f32163
    # duplicate-reply incident shape — e.g. prod conv ms1uojtuhk9fze). The
    # partial unique index idx_conv_msgs_msgid(conv_id, msg_id) rejects the
    # second occurrence, so per conv only the FIRST keeps the SQL-side id;
    # later duplicates are written with msg_id='' (outside the index). The
    # original id is preserved verbatim in meta, so row_to_message stays
    # lossless — a dup id is not uniquely addressable by definition.
    seen_msg_ids: set = set()
    for seq, msg in enumerate(msgs):
        row = message_to_row(conv_id, seq, msg, now_ms=now_ms)
        mid = row['msg_id']
        if mid:
            if mid in seen_msg_ids:
                row['msg_id'] = ''
            else:
                seen_msg_ids.add(mid)
        upsert(db, CONVERSATION_MESSAGES, row,
               conflict_cols=['conv_id', 'seq'], commit=False)
    mark_conv_mirror_current(
        db, conv_id, msgs, allow_missing=True,
        row_authority=row_authority, user_id=user_id)
    if commit:
        db.commit()
    return len(msgs)


_MIRROR_SAVEPOINT = 'tofu_messages_rows_mirror'


def _mirror_atomically(db, work) -> None:
    """Run one mirror mutation behind a safe write boundary.

    A multi-row upsert can fail after writing an arbitrary prefix (bad legacy
    JSON, duplicate id, connection fault, ...).  Swallowing that exception
    without rewinding the transaction leaves a plausible-looking *partial*
    mirror which a later caller may commit.

    On SQLite, a *top-level* SAVEPOINT is a deferred transaction: it can read
    a WAL snapshot without owning the physical writer slot.  A writer in a
    second process may then commit before our first mutation and make the
    read->write upgrade fail immediately with SQLITE_BUSY_SNAPSHOT
    (``database is locked``).  A process mutex cannot close that cross-process
    window, so a top-level mirror starts with BEGIN IMMEDIATE.  The caller
    still owns the successful commit.  Inside an existing transaction we use
    a savepoint, which isolates only the derived mirror work without touching
    unrelated caller state.
    """
    from lib.database import _core

    raw = getattr(db, 'raw', None)
    sqlite_top_level = (
        getattr(_core, '_BACKEND', 'sqlite') == 'sqlite'
        and raw is not None
        and not bool(getattr(raw, 'in_transaction', False))
    )
    if sqlite_top_level:
        db.begin()  # BEGIN IMMEDIATE: process + physical SQLite writer slot.
        try:
            work()
        except Exception:
            db.rollback()
            raise
        # Deliberately leave the successful transaction open.  Every public
        # caller either owns the surrounding commit or uses
        # mirror_write_and_commit().
        return

    db.execute(f'SAVEPOINT {_MIRROR_SAVEPOINT}')
    try:
        work()
    except Exception:
        try:
            db.execute(f'ROLLBACK TO SAVEPOINT {_MIRROR_SAVEPOINT}')
        finally:
            db.execute(f'RELEASE SAVEPOINT {_MIRROR_SAVEPOINT}')
        raise
    db.execute(f'RELEASE SAVEPOINT {_MIRROR_SAVEPOINT}')


def dual_write_conv(db, conv_id: str, messages, *, now_ms: int = 0,
                    changed_seqs=None) -> bool:
    """Mirror a JSONB ``messages`` write into conversation_messages rows.

    Incremental (2026-07-27, pt_59140ecd): the previous shape delegated to
    ``backfill_conv`` — a DELETE-all + per-row re-upsert of the WHOLE history
    on every write, so a 1163-message conversation paid 1163 row round-trips
    per appended message, strictly worse than the blob write being mirrored
    (docs/MESSAGES_ROWS_WRITE_FLIP_EVIDENCE.md §4.2).

    ``changed_seqs``: callers that KNOW which positions they edited in place
    (translate commit, patch-by-id, …) pass them — only those rows are
    re-mirrored (plus truncation repair). When ``None`` the mirror infers the
    change from the row count: tail appends write only the new rows plus a
    re-write of the previous tip row, which also covers the dominant
    same-count mutation (a streaming task finalizing its LAST message).
    A same-count edit NOT at the tip is invisible to the count heuristic —
    edit-capable callers MUST pass ``changed_seqs`` (the fleet parity gate
    is the backstop for anything that slips through).

    Best-effort: returns ``False`` when the flag is off or mirroring fails, and
    swallows every exception so a mirroring failure can NEVER break the
    authoritative JSONB write path.  A failed multi-row mutation is rolled
    back to its savepoint, so callers can never commit a partial mirror.
    """
    if not rows_write_enabled():
        return False
    try:
        write_conv_rows(
            db, conv_id, messages, now_ms=now_ms,
            changed_seqs=changed_seqs)
        return True
    except Exception as e:  # pragma: no cover - defensive
        logger.warning('[messages_rows] dual-write mirror failed conv=%s (non-fatal): %s',
                       (conv_id or '')[:12], e)
        return False


def write_conv_rows(db, conv_id: str, messages, *, now_ms: int = 0,
                    changed_seqs=None, full: bool = False,
                    row_authority: bool = False, user_id=None) -> None:
    """Write the canonical row representation or raise.

    Unlike the legacy :func:`dual_write_conv` compatibility hook, this is a
    strong data-layer operation: it never swallows failures.  Repository
    writers call it inside the same :func:`write_transaction` as the
    transitional ``conversations.messages`` blob, so either both
    representations advance or neither does.
    """
    if not rows_write_enabled():
        raise RuntimeError(
            'conversation message rows are disabled; refusing a strong row write')
    if full:
        _mirror_atomically(
            db,
            lambda: backfill_conv(
                db, conv_id, messages, now_ms=now_ms, commit=False,
                row_authority=row_authority, user_id=user_id),
        )
    else:
        _mirror_atomically(
            db,
            lambda: _mirror_conv_rows(
                db, conv_id, messages, now_ms=now_ms,
                changed_seqs=changed_seqs, row_authority=row_authority,
                user_id=user_id),
        )


def _mirror_conv_rows(db, conv_id: str, messages, *, now_ms: int = 0,
                      changed_seqs=None, row_authority: bool = False,
                      user_id=None) -> None:
    """Incremental mirror of the authoritative blob into rows.

    Cost per write on the dominant append path: one index-only COUNT plus
    ≤2 row upserts (tip refresh + the new row), versus history-length
    DELETE+reinsert before. Never commits — the caller owns the transaction
    boundary (pt_7e4afe73).
    """
    from lib.database._core_schema import CONVERSATION_MESSAGES, upsert
    msgs = _parse_messages(messages)
    n = len(msgs)
    if changed_seqs is not None:
        seqs = sorted({s for s in changed_seqs if isinstance(s, int) and 0 <= s < n})
    else:
        cnt_row = db.execute(
            'SELECT COUNT(*) AS n FROM conversation_messages WHERE conv_id=?',
            (conv_id,)).fetchone()
        old = int(cnt_row['n'] if hasattr(cnt_row, 'keys') else cnt_row[0]) if cnt_row else 0
        # Re-write from the previous tip onward: pure appends mirror the new
        # rows + refresh the tip (streaming writers mutate the last message
        # in place with the count unchanged); a fresh conv (old=0) falls out
        # as a full insert.
        start = max(0, min(old, n) - 1)
        seqs = list(range(start, n))
    for seq in seqs:
        row = message_to_row(conv_id, seq, msgs[seq], now_ms=now_ms)
        mid = row['msg_id']
        if mid:
            # A DIFFERENT seq may already own this msg_id (real blobs carry
            # duplicate _msgIds — the pt_97f32163 shape). The partial unique
            # index would reject the write AND every later mirror for this
            # conv; degrade the later duplicate to msg_id='' instead (the
            # original id survives verbatim in meta). Index-only probe,
            # skipped entirely for id-less rows.
            owner = db.execute(
                'SELECT seq FROM conversation_messages WHERE conv_id=? '
                'AND msg_id=? AND seq<>? LIMIT 1', (conv_id, mid, seq)).fetchone()
            if owner is not None:
                row['msg_id'] = ''
        upsert(db, CONVERSATION_MESSAGES, row,
               conflict_cols=['conv_id', 'seq'], commit=False)
    # Truncation repair: the blob is authoritative, so any row beyond its tail
    # is stale (branch delete / regen). Index-range delete — cheap no-op when
    # nothing is there.
    db.execute('DELETE FROM conversation_messages WHERE conv_id=? AND seq>=?',
               (conv_id, n))
    # Mark only after every row mutation succeeded. The conditional UPDATE and
    # row changes commit together; a concurrent authority write advances rev
    # and makes the CAS fail, rolling this savepoint back instead of blessing a
    # mixed/stale mirror.
    mark_conv_mirror_current(
        db, conv_id, msgs, allow_missing=True,
        row_authority=row_authority, user_id=user_id)


def mark_conv_mirror_current(db, conv_id: str, messages, *,
                             allow_missing: bool = False,
                             row_authority: bool = False, user_id=None):
    """CAS-mark the row mirror as exact for the current authoritative rev.

    The authority is re-read *after* row mutation. Equal message content is a
    hard precondition; then ``UPDATE ... WHERE rev=?`` locks/marks that exact
    revision in the same transaction as the mirror. If another writer wins at
    any point, either content differs or the CAS affects zero rows. No stale
    writer can stamp a newer rev onto older rows.

    Returns the marked rev, or ``None`` only for an explicitly allowed missing
    authority (useful for isolated row-mapper tests/backfill staging).
    """
    msgs = _parse_messages(messages)
    projection = 'rev' if row_authority else 'messages, rev'
    rows = db.execute(
        f'SELECT {projection} FROM conversations WHERE id=? '
        'ORDER BY user_id LIMIT 2', (conv_id,)).fetchall()
    if not rows:
        if allow_missing:
            return None
        raise RuntimeError('conversation authority missing')
    if len(rows) != 1:
        raise RuntimeError('conversation id is not globally unique; row mirror unsafe')
    row = rows[0]
    if row_authority:
        if user_id is None:
            raise RuntimeError('row authority requires an owning user_id')
        owner = db.execute(
            'SELECT rev FROM conversations WHERE id=? AND user_id=?',
            (conv_id, user_id)).fetchone()
        if owner is None:
            raise RuntimeError('conversation row authority owner missing')
        rev = int(owner['rev'] if hasattr(owner, 'keys') else owner[0])
    else:
        authoritative = _parse_messages(
            row['messages'] if hasattr(row, 'keys') else row[0])
        rev = int(row['rev'] if hasattr(row, 'keys') else row[1])
        if authoritative != msgs:
            raise RuntimeError('conversation authority changed during row mirror')
    if user_id is None:
        cur = db.execute(
            'UPDATE conversations SET messages_rows_rev=? '
            'WHERE id=? AND rev=?', (rev, conv_id, rev))
    else:
        cur = db.execute(
            'UPDATE conversations SET messages_rows_rev=? '
            'WHERE id=? AND user_id=? AND rev=?',
            (rev, conv_id, user_id, rev))
    affected = getattr(cur, 'rowcount', None)
    if affected != 1:
        raise RuntimeError(
            'conversation authority advanced before mirror marker CAS')
    return rev


def mirror_is_current(db, conv_id: str, *, expected_count=None,
                      expected_rev=None) -> bool:
    """Fail-closed read gate for one normalized conversation mirror."""
    try:
        rows = db.execute(
            'SELECT c.rev, c.messages_rows_rev, c.msg_count, '
            'COUNT(cm.seq) AS row_count, '
            'COUNT(cm.meta_light) AS light_count '
            'FROM conversations c LEFT JOIN conversation_messages cm '
            'ON cm.conv_id=c.id WHERE c.id=? '
            'GROUP BY c.user_id, c.rev, c.messages_rows_rev, c.msg_count '
            'ORDER BY c.user_id LIMIT 2',
            (conv_id,)).fetchall()
        if len(rows) != 1:
            return False
        row = rows[0]
        get = (lambda key, pos: row[key] if hasattr(row, 'keys') else row[pos])
        rev = int(get('rev', 0) or 0)
        raw_mirror_rev = get('messages_rows_rev', 1)
        mirror_rev = -1 if raw_mirror_rev is None else int(raw_mirror_rev)
        count = int(get('msg_count', 2) or 0)
        if mirror_rev != rev:
            return False
        if expected_rev is not None and rev != int(expected_rev):
            return False
        if expected_count is not None and count != int(expected_count or 0):
            return False
        # Compatibility for minimal DB/test adapters that project only the
        # three historical parent columns. Real SQLite/PG rows include the two
        # correlated counts and stay on the one-round-trip path.
        keys = row.keys() if hasattr(row, 'keys') else ()
        if keys and ('row_count' not in keys or 'light_count' not in keys):
            count_row = db.execute(
                'SELECT COUNT(*) AS n, COUNT(meta_light) AS light_n '
                'FROM conversation_messages WHERE conv_id=?',
                (conv_id,)).fetchone()
            row_count = int(count_row['n'] if hasattr(count_row, 'keys')
                            else count_row[0]) if count_row else 0
            light_count = int(count_row['light_n'] if hasattr(count_row, 'keys')
                              else count_row[1]) if count_row else 0
        else:
            row_count = int(get('row_count', 3) or 0)
            light_count = int(get('light_count', 4) or 0)
        return row_count == count and light_count == count
    except Exception as e:
        logger.debug('[messages_rows] current-marker probe failed conv=%s: %s',
                     (conv_id or '')[:12], e)
        return False


# ── Windowed read (tail window + page-up) ─────────────────────────────────

def _window_meta_expr(backend: str, source_sql: str = 'meta') -> str:
    """SQL expression for the transport-light form of a JSON message.

    Windowed conversation GETs never send the raw tool timeline on first
    paint, so pulling multi-megabyte ``toolRounds``/``segments`` values out of
    PostgreSQL only to discard them in Python is pure memory and CPU waste.
    Project those keys in the database and retain the one fact reconcile needs
    for ghost safety: whether/how many tool rounds existed.

    ``source_sql`` is an internal SQL expression (never request data). Keeping
    it parameterized lets both the normalized ``meta`` row and an element of
    the authoritative JSON array use exactly the same projection. Non-object
    historical values pass through byte-for-byte; stored values remain the
    lossless authority used by parity checks and full hydration.
    """
    source = f'({source_sql})'
    if backend == 'pg':
        return (
            # Do not use PostgreSQL's ``?|`` operator here: the project's
            # qmark-to-%s compatibility wrapper would parse its question mark
            # as a bind placeholder.  ``-> ... IS NOT NULL`` distinguishes an
            # absent key from a present JSON null and survives translation.
            f"CASE WHEN jsonb_typeof({source})='object' AND "
            f"jsonb_exists_any({source}, ARRAY['segments', 'toolRounds', "
            "'_continueToolRounds', 'toolSummary']) THEN "
            f"({source} - 'segments' - 'toolRounds' - '_continueToolRounds' "
            "          - 'toolSummary') "
            "|| jsonb_build_object('_trimmed', true) "
            f"|| CASE WHEN jsonb_typeof({source}->'toolRounds')='array' "
            f"             AND jsonb_array_length({source}->'toolRounds') > 0 "
            "        THEN jsonb_build_object('_trimmedToolRoundCount', "
            f"                                jsonb_array_length({source}->'toolRounds')) "
            "        ELSE '{}'::jsonb END "
            f"ELSE {source} END"
        )
    return (
        f"CASE WHEN json_type({source})='object' AND ("
        f"json_type({source}, '$.segments') IS NOT NULL OR "
        f"json_type({source}, '$.toolRounds') IS NOT NULL OR "
        f"json_type({source}, '$._continueToolRounds') IS NOT NULL OR "
        f"json_type({source}, '$.toolSummary') IS NOT NULL) THEN "
        f"json_set(json_remove({source}, '$.segments', '$.toolRounds', "
        "                         '$._continueToolRounds', '$.toolSummary'), "
        "         '$._trimmed', json('true'), "
        "         '$._trimmedToolRoundCount', "
        f"         CASE WHEN json_type({source}, '$.toolRounds')='array' "
        f"              THEN json_array_length({source}, '$.toolRounds') ELSE 0 END) "
        f"ELSE {source} END"
    )


def backfill_light_projection(db, conv_id: str, *, commit: bool = True) -> int:
    """Populate missing ``meta_light`` values for one conversation online.

    The update is derived wholly inside the database, so full tool/image
    payloads never cross into Python.  It only touches NULL rows, making it
    resumable and compatible with an older still-running server: any row that
    process inserts without the new column remains NULL and automatically
    fails the read gate until a later pass fills it.
    """
    from lib.database import _BACKEND
    cur = db.execute(
        'UPDATE conversation_messages SET meta_light=' +
        _window_meta_expr(_BACKEND) +
        ' WHERE conv_id=? AND meta_light IS NULL',
        (conv_id,),
    )
    affected = getattr(cur, 'rowcount', None)
    if commit:
        db.commit()
    return max(0, int(affected or 0))


def backfill_activity_projection(db, conv_id: str, *, commit: bool = True) -> int:
    """Populate missing activity/billing projections for one conversation.

    ``meta_light`` avoids transferring tool/segment payloads. COALESCE to full
    ``meta`` only protects an older row whose light projection has not landed
    yet. This updates derived data only; authority and mirror revision remain
    untouched, and the ``IS NULL`` fence makes retries idempotent.
    """
    rows = db.execute(
        'SELECT seq, COALESCE(meta_light, meta) AS projected '
        'FROM conversation_messages WHERE conv_id=? '
        'AND (message_ts IS NULL OR billing_meta IS NULL) '
        'ORDER BY seq', (conv_id,)).fetchall()
    updates = []
    for row in rows:
        raw = row['projected'] if hasattr(row, 'keys') else row[1]
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode('utf-8', 'replace')
        try:
            msg = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[MessagesRows] malformed projected message conv=%s '
                         'seq=%r: %s', conv_id,
                         row['seq'] if hasattr(row, 'keys') else row[0], e)
            msg = {}
        fields = msg if isinstance(msg, dict) else {}
        try:
            message_ts = int(fields.get('timestamp', 0) or 0)
        except (TypeError, ValueError, OverflowError) as e:
            logger.debug('[MessagesRows] malformed projected timestamp '
                         'conv=%s seq=%r: %s', conv_id,
                         row['seq'] if hasattr(row, 'keys') else row[0], e)
            message_ts = 0
        billing_meta = json_dumps_pg(_billing_message_projection(fields))
        seq = int(row['seq'] if hasattr(row, 'keys') else row[0])
        updates.append((message_ts, billing_meta, conv_id, seq))
    affected = 0
    if updates:
        cur = db.executemany(
            'UPDATE conversation_messages SET '
            'message_ts=COALESCE(message_ts,?), '
            'billing_meta=COALESCE(billing_meta,?) '
            'WHERE conv_id=? AND seq=? '
            'AND (message_ts IS NULL OR billing_meta IS NULL)', updates)
        rowcount = getattr(cur, 'rowcount', -1)
        affected = (len(updates) if rowcount is None or int(rowcount) < 0
                    else int(rowcount))
    if commit:
        db.commit()
    return affected


def _bounded_env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, '') or str(default))
    except (TypeError, ValueError) as e:
        logger.debug('[MessagesRows] invalid %s=%r; using %d: %s',
                     name, os.environ.get(name), default, e)
        value = default
    return max(low, min(high, value))


def _activity_projection_candidates(db, after_conv_id: str, limit: int):
    """Return one bounded page from the partial incomplete-projection index."""
    limit = max(1, min(1000, int(limit)))
    index_hint = (
        ' INDEXED BY idx_conv_msgs_incomplete_projection'
        if getattr(db, 'dialect', '') == 'sqlite' else '')
    rows = db.execute(
        'SELECT DISTINCT conv_id FROM conversation_messages' + index_hint +
        ' WHERE (message_ts IS NULL OR billing_meta IS NULL) '
        'AND conv_id > ? ORDER BY conv_id LIMIT ?',
        (after_conv_id, limit)).fetchall()
    return [str(row['conv_id'] if hasattr(row, 'keys') else row[0])
            for row in rows]


def _activity_projection_backfill_worker() -> None:
    """Converge NULL activity/billing projections without delaying startup."""
    initial_ms = _bounded_env_int(
        'TOFU_MESSAGES_ACTIVITY_BACKFILL_INITIAL_MS', 10_000, 0, 300_000)
    sleep_ms = _bounded_env_int(
        'TOFU_MESSAGES_ACTIVITY_BACKFILL_SLEEP_MS', 100, 0, 5_000)
    page_size = _bounded_env_int(
        'TOFU_MESSAGES_ACTIVITY_BACKFILL_PAGE_SIZE', 100, 10, 1000)
    if initial_ms:
        time.sleep(initial_ms / 1000.0)

    from lib.database import DOMAIN_CHAT, pooled_db
    after_conv_id = ''
    scanned = filled = failed = 0
    while True:
        try:
            with pooled_db(DOMAIN_CHAT) as db:
                conv_ids = _activity_projection_candidates(
                    db, after_conv_id, page_size)
        except Exception as e:
            logger.warning('[messages_rows] activity projection inventory failed: %s', e)
            return
        if not conv_ids:
            break

        for conv_id in conv_ids:
            try:
                with pooled_db(DOMAIN_CHAT) as db:
                    filled += backfill_activity_projection(
                        db, conv_id, commit=True)
            except Exception as e:
                failed += 1
                logger.warning(
                    '[messages_rows] activity projection failed conv=%s: %s',
                    conv_id[:12], e)
            scanned += 1
            if scanned % 250 == 0:
                logger.info('[messages_rows] activity projection progress: '
                            'convs=%d rows=%d failed=%d',
                            scanned, filled, failed)
            if sleep_ms:
                time.sleep(sleep_ms / 1000.0)
        after_conv_id = conv_ids[-1]

    if scanned:
        logger.info('[messages_rows] activity projection backfill complete: '
                    'convs=%d rows=%d failed=%d', scanned, filled, failed)


def start_activity_projection_backfill() -> bool:
    """Start the singleton, daemonized activity projection backfill.

    Returns whether a worker was started. The default personal server enables
    it together with row reads; tests/tools and explicit kill-switch installs
    remain inert.
    """
    global _activity_backfill_thread
    enabled = (os.environ.get('TOFU_MESSAGES_ACTIVITY_BACKFILL', '1')
               or '1').strip() != '0'
    if not enabled or not rows_read_enabled():
        return False
    with _activity_backfill_lock:
        if (_activity_backfill_thread is not None
                and _activity_backfill_thread.is_alive()):
            return False
        thread = threading.Thread(
            target=_activity_projection_backfill_worker,
            name='message-activity-backfill', daemon=True)
        thread.start()
        _activity_backfill_thread = thread
        return True


def load_message_window(db, conv_id: str, limit: int, before_seq=None, *,
                        project_heavy: bool = False,
                        known_total=None) -> dict:
    """Load a WINDOW of a conversation's messages from the row store.

    The root-cause fix for slow first-open of long conversations: instead of
    detoasting + parsing the whole ``conversations.messages`` blob (cost linear
    in history length), read only ``limit`` rows via the ``idx_conv_msgs_conv``
    ``(conv_id, seq)`` primary-key index (cost constant in the window).

    Args:
        db: DB wrapper.
        conv_id: conversation id.
        limit: max messages to return (the window size N). ``<=0`` disables the
            window and returns the whole conversation.
        before_seq: when set, page UPWARD — return the window of messages with
            ``seq < before_seq`` (the ``limit`` messages ending just before it).
            When ``None``, return the TAIL window (the newest ``limit``).
        project_heavy: strip first-paint-only heavy fields inside SQL and add
            ``_trimmed``/``_trimmedToolRoundCount`` summary markers.  The
            stored rows are untouched.  Reconcile treats the count as positive
            evidence of a real tool round and declines duplicate folding when
            full payloads were projected, so this optimization cannot create a
            false ghost or lossy deduplication.
        known_total: authoritative row count already verified by the caller.
            Supplying it avoids a duplicate ``COUNT(*)`` on every windowed
            open. Direct callers may leave it unset for the self-contained
            legacy behaviour.

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
    # The read gate has normally just proved ``msg_count == row_count``. Reuse
    # that value instead of scanning the same covering index a second time.
    if known_total is not None:
        total = max(0, int(known_total or 0))
    else:
        try:
            cnt_row = db.execute(
                'SELECT COUNT(*) AS n FROM conversation_messages WHERE conv_id=?',
                (conv_id,)).fetchone()
            total = int(cnt_row['n'] if hasattr(cnt_row, 'keys') else cnt_row[0]) if cnt_row else 0
        except Exception as e:
            logger.debug('[messages_rows] window count failed conv=%s: %s',
                         (conv_id or '')[:12], e)
            total = 0

    meta_expr = 'meta'
    if project_heavy:
        # ``mirror_is_current`` proves every row has this materialized value.
        # COALESCE remains a defensive direct-caller fallback during a rolling
        # migration; it detoasts only the exceptional NULL row, not every GET.
        from lib.database import _BACKEND
        meta_expr = 'COALESCE(meta_light, %s)' % _window_meta_expr(_BACKEND)

    if limit is None or limit <= 0:
        rows = db.execute(
            f'SELECT {meta_expr} AS meta, translation_state, seq '
            'FROM conversation_messages '
            'WHERE conv_id=? ORDER BY seq',
            (conv_id,)).fetchall()
    elif before_seq is not None:
        # page upward: the `limit` messages with seq < before_seq (newest of the
        # older block first, then reversed to ascending).
        rows = db.execute(
            f'SELECT {meta_expr} AS meta, translation_state, seq '
            'FROM conversation_messages '
            'WHERE conv_id=? AND seq<? '
            'ORDER BY seq DESC LIMIT ?', (conv_id, int(before_seq), int(limit))).fetchall()
        rows = list(reversed(rows))
    else:
        # tail window: the newest `limit` messages, reversed to ascending.
        rows = db.execute(
            f'SELECT {meta_expr} AS meta, translation_state, seq '
            'FROM conversation_messages WHERE conv_id=? '
            'ORDER BY seq DESC LIMIT ?', (conv_id, int(limit))).fetchall()
        rows = list(reversed(rows))

    def _seq(r):
        try:
            return int(r['seq'] if hasattr(r, 'keys') else r[2])
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


def load_message_selection(db, conv_id: str, head: int, tail: int,
                           before_seq=None, *, known_total=None) -> dict:
    """Load the exact HEAD+TAIL selection used by conversation references.

    Unlike composing two :func:`load_message_window` calls, this performs one
    indexed query and never repeats the row-count scan.  ``before_seq`` is an
    exclusive zero-based upper bound, matching ``_select_message_window``.
    The returned ``kept`` values are ``(absolute_seq, message)`` pairs.
    """
    if known_total is None:
        count_row = db.execute(
            'SELECT COUNT(*) AS n FROM conversation_messages WHERE conv_id=?',
            (conv_id,)).fetchone()
        total = int(count_row['n'] if hasattr(count_row, 'keys')
                    else count_row[0]) if count_row else 0
    else:
        total = max(0, int(known_total or 0))

    head = max(0, int(head or 0))
    tail = max(0, int(tail or 0))
    end = (total if before_seq is None
           else max(0, min(int(before_seq), total)))
    if end <= head + tail:
        rows = db.execute(
            'SELECT meta, translation_state, seq FROM conversation_messages '
            'WHERE conv_id=? AND seq<? ORDER BY seq',
            (conv_id, end)).fetchall()
        omitted = 0
    else:
        tail_start = end - tail
        # The branches are disjoint because this path requires
        # end > head+tail. UNION ALL therefore needs no duplicate elimination,
        # and both halves are narrow primary-key ranges.
        rows = db.execute(
            'SELECT meta, translation_state, seq FROM conversation_messages '
            'WHERE conv_id=? AND seq<? UNION ALL '
            'SELECT meta, translation_state, seq FROM conversation_messages '
            'WHERE conv_id=? AND seq>=? AND seq<? ORDER BY seq',
            (conv_id, head, conv_id, tail_start, end)).fetchall()
        omitted = tail_start - head

    kept = []
    for row in rows:
        seq = int(row['seq'] if hasattr(row, 'keys') else row[2])
        kept.append((seq, row_to_message(row)))
    return {'kept': kept, 'omitted': omitted, 'totalCount': total}


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
    """Verify ONE row-backed conversation reproduces its complete JSON blob.

    Reads the authoritative JSONB messages AND the conversation_messages rows
    independently, then compares the complete parsed message list *and* both
    ``build_search_text`` outputs. Search-only parity is not enough: losing a
    tool result, usage field, attachment, or message id could leave search text
    unchanged while corrupting the conversation. Used by the verification
    harness BEFORE flipping reads.
    """
    from lib.database._access_policy import allow_transcript_archive_access
    with allow_transcript_archive_access():
        jr = db.execute(
            'SELECT messages FROM conversations WHERE id=?',
            (conv_id,)).fetchone()
    jsonb_msgs = _parse_messages(jr['messages'] if jr else [])
    rows = db.execute(
        'SELECT meta, translation_state FROM conversation_messages '
        'WHERE conv_id=? ORDER BY seq', (conv_id,)
    ).fetchall()
    rows_msgs = rows_to_messages(rows)
    a = build_search_text(jsonb_msgs)
    b = build_search_text(rows_msgs)
    content_ok = jsonb_msgs == rows_msgs
    search_text_ok = a == b
    return {
        'ok': content_ok and search_text_ok,
        'content_ok': content_ok,
        'search_text_ok': search_text_ok,
        'conv_id': conv_id,
        'jsonb_len': len(a),
        'rows_len': len(b),
        'jsonb_msgs': len(jsonb_msgs),
        'rows_msgs': len(rows_msgs),
        'light_ready': _light_projection_ready(db, conv_id, len(rows_msgs)),
        'activity_ready': _activity_projection_ready(
            db, conv_id, len(rows_msgs)),
        'billing_ready': _billing_projection_ready(
            db, conv_id, len(rows_msgs)),
        'mirror_current': mirror_is_current(
            db, conv_id, expected_count=len(jsonb_msgs)),
    }


def _light_projection_ready(db, conv_id: str, expected_count: int) -> bool:
    """Exact NULL-free gate for the materialized first-paint projection."""
    try:
        row = db.execute(
            'SELECT COUNT(meta_light) AS n FROM conversation_messages '
            'WHERE conv_id=?', (conv_id,)).fetchone()
        n = int(row['n'] if hasattr(row, 'keys') else row[0]) if row else 0
        return n == int(expected_count or 0)
    except Exception as e:
        logger.debug('[messages_rows] light readiness probe failed conv=%s: %s',
                     (conv_id or '')[:12], e)
        return False


def _activity_projection_ready(db, conv_id: str, expected_count: int) -> bool:
    """Exact NULL-free gate for the materialized activity timestamp."""
    try:
        row = db.execute(
            'SELECT COUNT(message_ts) AS n FROM conversation_messages '
            'WHERE conv_id=?', (conv_id,)).fetchone()
        n = int(row['n'] if hasattr(row, 'keys') else row[0]) if row else 0
        return n == int(expected_count or 0)
    except Exception as e:
        logger.debug('[messages_rows] activity readiness probe failed conv=%s: %s',
                     (conv_id or '')[:12], e)
        return False


def _billing_projection_ready(db, conv_id: str, expected_count: int) -> bool:
    """Exact NULL-free gate for the materialized billing projection."""
    try:
        row = db.execute(
            'SELECT COUNT(billing_meta) AS n FROM conversation_messages '
            'WHERE conv_id=?', (conv_id,)).fetchone()
        n = int(row['n'] if hasattr(row, 'keys') else row[0]) if row else 0
        return n == int(expected_count or 0)
    except Exception as e:
        logger.debug('[messages_rows] billing readiness probe failed conv=%s: %s',
                     (conv_id or '')[:12], e)
        return False


def mirror_write_and_commit(db, conv_id: str, messages, *, now_ms: int = 0,
                            changed_seqs=None, full: bool = False) -> None:
    """The one-line dual-write hook for full-blob writers (pt_59140ecd ②).

    Call this AFTER the authoritative ``conversations.messages`` write has
    committed (or at the end of a function whose teardown commits): mirrors
    into ``conversation_messages`` rows via :func:`dual_write_conv` and —
    only when the write flag is on — commits the mirror rows immediately so
    they never hang uncommitted on a pooled connection (the pt_7e4afe73
    durability gap). The flag-off path is a pure no-op, byte-identical to
    not calling at all, so fanning this out to every blob writer is
    behaviour-neutral until the flag flips.

    ``full=True`` forces a complete rebuild (DELETE + re-insert of every row)
    — use it for REWRITE-class writers that re-sequence or surgically rewrite
    the array (reconcile / killed-recovery), where the count heuristic and
    seq hints cannot express the change. These paths are rare, so the
    O(history) cost is acceptable.

    ``lib/chat/persistence.py::persist_conv_messages`` keeps its annotated
    inline version (it interleaves the commit with the rev read-back).

    **NEVER RAISES.** The whole body is defended because the mirror is
    best-effort by contract: the authoritative blob write has ALREADY
    committed by the time callers reach this hook, so an exception escaping
    here would abort a caller mid-way through work that is already durable.
    That is not hypothetical — a missing ``full`` parameter made this hook
    raise ``NameError`` on 2026-07-27 and silently killed the autopilot baton
    hand-off, the scheduler's task spawn, and swarm auto-continue, each of
    which had already committed its messages. Callers additionally guard the
    call itself, because a signature-level ``TypeError`` cannot be caught from
    inside the callee.
    """
    try:
        if not rows_write_enabled():
            return
        if full:
            _mirror_atomically(
                db,
                lambda: backfill_conv(
                    db, conv_id, messages, now_ms=now_ms, commit=False),
            )
        else:
            if not dual_write_conv(db, conv_id, messages, now_ms=now_ms,
                                   changed_seqs=changed_seqs):
                return
        db.commit()
    except Exception as e:
        # The helper contract requires the JSONB authority to be durable before
        # entry.  Roll back only the failed mirror transaction so a pooled
        # connection never carries an aborted/partial transaction forward.
        try:
            db.rollback()
        except Exception as rollback_error:
            logger.debug('[messages_rows] mirror rollback failed conv=%s: %s',
                         (conv_id or '')[:12], rollback_error)
        logger.warning('[messages_rows] mirror failed conv=%s (non-fatal, '
                       'JSONB truth already durable): %s',
                       (conv_id or '')[:12], e, exc_info=True)


__all__ = [
    'rows_write_enabled', 'rows_read_enabled', 'rows_authority_enabled',
    'assert_rows_authority_ready',
    'message_to_row', 'translation_state_for_message',
    'light_message_for_window', 'row_to_message', 'rows_to_messages',
    'changed_message_seqs',
    'backfill_conv', 'backfill_light_projection', 'backfill_activity_projection',
    'start_activity_projection_backfill',
    'dual_write_conv', 'write_conv_rows', 'mirror_write_and_commit',
    'mark_conv_mirror_current', 'mirror_is_current',
    'load_message_window', 'load_message_selection',
    'verify_search_text_parity', 'verify_conv_parity',
]
