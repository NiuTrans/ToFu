"""lib/database/_core_schema/_tables.py — the COMPLETE dual-backend schema.

Every table the project creates is defined ONCE here as a SQLAlchemy Core
``Table`` on the shared private ``MetaData`` from ``_helpers.py``, using the
dialect-variant column factories defined there. A table appears here only
after its parity test in ``tests/test_core_schema_parity.py`` is green on BOTH
backends, proving the Core-generated DDL is byte-equivalent to the live
hand-DDL. The DDL is unchanged, so NO ``_SCHEMA_VERSION`` bump is required.

Every ``define_table`` call registers on the ONE shared ``MetaData`` — so this
module must be imported exactly once (via the package ``__init__``); importing
it twice would raise 'table already defined in MetaData'.
"""

from __future__ import annotations

import sqlalchemy as sa

from lib.log import get_logger

from ._helpers import (
    autoincrement_pk,
    bigint_autoincrement_pk,
    bigint_column,
    bool_column,
    define_table,
    double_column,
    epoch_now,
    jsonb_column,
    now_timestamp,
    timestamptz_column,
)

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════
#  Registered tables — the COMPLETE dual-backend schema (migration done 2026-06).
#
#  Every table the project creates lives here; _schema_pg.py / _schema_sqlite.py
#  no longer hand-author CREATE TABLE. A table appears here only after its
#  parity test in tests/test_core_schema_parity.py is green on BOTH backends,
#  proving the Core-generated DDL is byte-equivalent to the live hand-DDL. The
#  DDL is unchanged, so NO _SCHEMA_VERSION bump is required.
# ═══════════════════════════════════════════════════════════════════════

# daily_cost_cache — pre-aggregated per-day LLM cost. Composite PK
# (user_id, date 'YYYY-MM-DD'). cost is DOUBLE PRECISION/REAL, conversations_json
# is JSONB/TEXT. Exercises double_column + composite-PK upsert (queries-on-Core).
DAILY_COST_CACHE = define_table(
    'daily_cost_cache',
    sa.Column('user_id', sa.Integer, nullable=False),
    sa.Column('date', sa.Text, nullable=False),
    sa.Column('cost', double_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('conversations_json', jsonb_column(), nullable=False, server_default=sa.text("'{}'")),
    sa.Column('computed_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.PrimaryKeyConstraint('user_id', 'date'),
)

# schema_meta — core-owned key/value store for bootstrap metadata
# (schema version + active-domain set). Same shape as trading_config; lives in
# core so the fast-startup version cache survives when the trading domain is
# disabled or extracted into its own package. See lib/database/schema_registry.py.
SCHEMA_META = define_table(
    'schema_meta',
    sa.Column('key', sa.Text, primary_key=True),
    sa.Column('value', sa.Text, nullable=False, server_default=''),
)

# users — account table. Auto-increment PK (SERIAL/INTEGER AUTOINCREMENT),
# unique username, and a human-readable created_at (TIMESTAMPTZ/TEXT) defaulting
# to NOW()/datetime('now'). FK target for conversations.user_id.
USERS = define_table(
    'users',
    autoincrement_pk(),
    sa.Column('username', sa.Text, nullable=False, unique=True),
    sa.Column('display_name', sa.Text, nullable=False, server_default=''),
    sa.Column('password_hash', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', timestamptz_column(), server_default=now_timestamp()),
    sqlite_autoincrement=True,
)

# conversations — chat transcripts. OPTION B migration: Core owns the shared
# base columns (incl. search_text, which SQLite's base CREATE carries and which
# PG adds via a _column_exists-guarded ALTER that becomes a no-op once Core
# emits the column). The PG-ONLY full-text infrastructure — the search_tsv
# tsvector column, pg_trgm EXTENSION, idx_conv_search_trgm / idx_conv_search_tsv
# GIN indexes, and the conversations_search_tsv_trg trigger + function — CANNOT
# be expressed by create_if_absent's single ddl_for and remain as explicit,
# guarded post-create DDL in _schema_pg.py (exactly as before). messages /
# settings are JSONB on PG, TEXT on SQLite. Composite PK (id, user_id); FK to
# users(id) ON DELETE CASCADE.
CONVERSATIONS = define_table(
    'conversations',
    sa.Column('id', sa.Text, nullable=False),
    sa.Column('user_id', sa.Integer, nullable=False),
    sa.Column('title', sa.Text, nullable=False, server_default='New Chat'),
    sa.Column('messages', jsonb_column(), nullable=False, server_default=sa.text("'[]'")),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.Column('updated_at', bigint_column(), nullable=False),
    sa.Column('settings', jsonb_column(), nullable=False, server_default=sa.text("'{}'")),
    sa.Column('msg_count', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('search_text', sa.Text, nullable=False, server_default=''),
    # rev — server-issued monotonic message-version. Bumped by a DB trigger
    # (NOT by any application writer) whenever the messages column actually
    # changes, so it is impossible for a new writer to forget. Powers the
    # compare-and-swap PUT + rev-based reconcile winner (a stale client copy
    # carries an older rev and can never clobber fresh server truth). Starts at
    # 0 on every existing row and pre-CAS client, so a client that sends no
    # baseRev falls back to the legacy count-regression guard (fail-open).
    sa.Column('rev', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.PrimaryKeyConstraint('id', 'user_id'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
)

# conversation_messages — Phase 5 "messages-as-rows". The per-message row
# store that the conversations.messages JSONB array migrates INTO. Landing
# migrator-first behind the TOFU_MESSAGES_ROWS flag (lib/database/messages_rows.py):
# a one-shot idempotent backfill + dual-write, with reads gated on a proven
# byte-identical build_search_text reconstruction BEFORE any read cutover.
#
# Column split rationale: the four columns build_search_text() actually reads —
# role, content, thinking, translated_content — are first-class so the search
# blob can be reconstructed from rows alone (the verification invariant). The
# whole original message dict (incl. _msgId, timestamp, finishReason, usage,
# toolRounds, model, modifiedFileList, …) is preserved verbatim in meta JSONB,
# so a row round-trips back to the exact JSONB element with no field loss.
# content_json holds multipart content (list of text/image parts) as a JSON
# string; content holds the plain-string form. Exactly one is populated per row
# (mirrors the str-vs-list branch in build_search_text). Composite PK
# (conv_id, seq) preserves order; (conv_id, msg_id) is separately UNIQUE for
# index-free addressing. FK to conversations(id) is intentionally OMITTED —
# conversations has a COMPOSITE PK (id, user_id), so a single-column FK can't
# target it; the migrator/dual-writer scope rows by conv_id within the owning
# user's write path.
CONVERSATION_MESSAGES = define_table(
    'conversation_messages',
    sa.Column('conv_id', sa.Text, nullable=False),
    sa.Column('seq', sa.Integer, nullable=False),
    sa.Column('msg_id', sa.Text, nullable=False, server_default=''),
    sa.Column('role', sa.Text, nullable=False, server_default=''),
    sa.Column('content', sa.Text, nullable=False, server_default=''),
    sa.Column('content_json', jsonb_column(), nullable=False, server_default=sa.text("'[]'")),
    sa.Column('thinking', sa.Text, nullable=False, server_default=''),
    sa.Column('translated_content', sa.Text, nullable=False, server_default=''),
    sa.Column('meta', jsonb_column(), nullable=False, server_default=sa.text("'{}'")),
    sa.Column('created_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('updated_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.PrimaryKeyConstraint('conv_id', 'seq'),
)

# trading_config — key/value store; identical shape on PG + SQLite.
TRADING_CONFIG = define_table(
    'trading_config',
    sa.Column('key', sa.Text, primary_key=True),
    sa.Column('value', sa.Text, nullable=False, server_default=''),
)

# pricing_cache — system key/value cache with an epoch-ms timestamp.
PRICING_CACHE = define_table(
    'pricing_cache',
    sa.Column('key', sa.Text, primary_key=True),
    sa.Column('value', sa.Text, nullable=False),
    sa.Column('updated_at', bigint_column(), nullable=False),
)

# recent_projects — MRU list keyed by filesystem path.
RECENT_PROJECTS = define_table(
    'recent_projects',
    sa.Column('path', sa.Text, primary_key=True),
    sa.Column('count', sa.Integer, nullable=False, server_default=sa.text('1')),
    sa.Column('last_used', bigint_column(), nullable=False),
)

# paper_reports — persistent cache for paper analysis reports; composite PK
# (paper_hash, lang). All TEXT + bigint created_at. `meta` is a JSON blob
# holding the resolved generation model + token usage + cost (rendered as a
# "finish tag" badge under the report); '' on legacy rows.
PAPER_REPORTS = define_table(
    'paper_reports',
    sa.Column('paper_hash', sa.Text, nullable=False),
    sa.Column('lang', sa.Text, nullable=False, server_default='en'),
    sa.Column('report', sa.Text, nullable=False, server_default=''),
    sa.Column('model', sa.Text, nullable=False, server_default=''),
    sa.Column('meta', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.PrimaryKeyConstraint('paper_hash', 'lang'),
)

# paper_library — server-side bookshelf; composite PK (id, user_id), FK to
# users. qa_history/images/babel_cache are plain TEXT (json.dumps strings, NOT
# JSONB) on both backends — matches the live DDL.
PAPER_LIBRARY = define_table(
    'paper_library',
    sa.Column('id', sa.Text, nullable=False),
    sa.Column('user_id', sa.Integer, nullable=False),
    sa.Column('title', sa.Text, nullable=False, server_default=''),
    sa.Column('pdf_url', sa.Text, nullable=False, server_default=''),
    sa.Column('pdf_filename', sa.Text, nullable=False, server_default=''),
    sa.Column('arxiv_id', sa.Text, nullable=False, server_default=''),
    sa.Column('paper_hash', sa.Text, nullable=False, server_default=''),
    sa.Column('parsed_text', sa.Text, nullable=False, server_default=''),
    # parser_version — the extractor+version key that produced parsed_text
    # (e.g. 'pymupdf4llm-1.27.2.3'; raw fallback 'pymupdf-raw-…'; '' = legacy/
    # unknown). The harvest parse-once probe requires an exact match with the
    # environment's expected version, so a parser upgrade or a degraded write
    # invalidates naturally instead of serving stale text forever.
    sa.Column('parser_version', sa.Text, nullable=False, server_default=''),
    sa.Column('qa_history', sa.Text, nullable=False, server_default='[]'),
    sa.Column('images', sa.Text, nullable=False, server_default='[]'),
    sa.Column('babel_cache', sa.Text, nullable=False, server_default='{}'),
    sa.Column('page_count', sa.Integer, nullable=False, server_default=sa.text('0')),
    # folder_id — optional organizational grouping (mirrors conversation
    # folders). Empty string = unfiled. Folder metadata lives in a separate
    # JSON store (paper_folders.json); this is just the membership link.
    sa.Column('folder_id', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.Column('updated_at', bigint_column(), nullable=False),
    sa.PrimaryKeyConstraint('id', 'user_id'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
)

# paper_notes — reader margin notes (reading-xp P4). One row per note;
# anchor is a JSON blob {heading_idx, char_offset, quote} — heading_idx+offset
# addresses the spot, quote is the fuzzy re-anchor fallback after a report
# regeneration (an unmatchable note degrades to an "orphan", never vanishes).
PAPER_NOTES = define_table(
    'paper_notes',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('paper_hash', sa.Text, nullable=False, server_default=''),
    sa.Column('lang', sa.Text, nullable=False, server_default=''),
    sa.Column('anchor', sa.Text, nullable=False, server_default='{}'),
    sa.Column('note', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.Column('updated_at', bigint_column(), nullable=False),
)

# paper_translations — Babel-mode whole-paper translation cache; composite PK
# (paper_hash, lang). created_at is epoch-ms (bigint on PG, integer on SQLite).
PAPER_TRANSLATIONS = define_table(
    'paper_translations',
    sa.Column('paper_hash', sa.Text, nullable=False),
    sa.Column('lang', sa.Text, nullable=False),
    sa.Column('text', sa.Text, nullable=False, server_default=''),
    sa.Column('model', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.PrimaryKeyConstraint('paper_hash', 'lang'),
)

# paper_podcasts — generated paper-podcast cache; composite PK
# (paper_hash, mode, lang, voice). script_json is the segmented spoken
# script (JSON); file_path points at the on-disk audio under
# uploads/papers/podcast/ (DB holds text/metadata, disk holds binaries —
# same convention as paper PDFs/images). status: running | done |
# script_only (no TTS slot configured — script+transcript only) | error.
# duration_sec is REAL (0 when unknown/estimated-only).
PAPER_PODCASTS = define_table(
    'paper_podcasts',
    sa.Column('paper_hash', sa.Text, nullable=False),
    sa.Column('mode', sa.Text, nullable=False, server_default='short'),
    sa.Column('lang', sa.Text, nullable=False, server_default='zh'),
    sa.Column('voice', sa.Text, nullable=False, server_default=''),
    sa.Column('status', sa.Text, nullable=False, server_default=''),
    sa.Column('script_json', sa.Text, nullable=False, server_default=''),
    sa.Column('file_path', sa.Text, nullable=False, server_default=''),
    sa.Column('duration_sec', sa.Float, nullable=False, server_default=sa.text('0')),
    sa.Column('model', sa.Text, nullable=False, server_default=''),
    sa.Column('tts_model', sa.Text, nullable=False, server_default=''),
    sa.Column('meta', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.Column('updated_at', bigint_column(), nullable=False),
    sa.PrimaryKeyConstraint('paper_hash', 'mode', 'lang', 'voice'),
)

# task_results — persisted chat/task output. Single-col PK; several nullable
# TEXT columns (no default) and a nullable completed_at timestamp.
TASK_RESULTS = define_table(
    'task_results',
    sa.Column('task_id', sa.Text, primary_key=True),
    sa.Column('conv_id', sa.Text, nullable=False),
    sa.Column('content', sa.Text, nullable=False, server_default=''),
    sa.Column('thinking', sa.Text, nullable=False, server_default=''),
    sa.Column('error', sa.Text),
    sa.Column('status', sa.Text, nullable=False, server_default='done'),
    sa.Column('tool_rounds', sa.Text),
    sa.Column('search_results', sa.Text),
    sa.Column('metadata', sa.Text),
    # segments — the ordered typed-segment timeline (epic pt_cb8f98b0cb9b47fb).
    # TEXT holding a JSON string (the thin form; see segments.segments_to_json),
    # NOT JSONB — matches the sibling tool_rounds/search_results/metadata cols
    # so the same json.dumps(ensure_ascii=False) write path + parity DDL apply.
    # Read wholesale, never queried, so JSONB buys nothing.
    sa.Column('segments', sa.Text),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.Column('completed_at', bigint_column()),
)

# task_events — persisted SSE event log; composite PK (task_id, event_id).
# payload is JSONB on PG / TEXT on SQLite.
TASK_EVENTS = define_table(
    'task_events',
    sa.Column('task_id', sa.Text, nullable=False),
    sa.Column('event_id', bigint_column(), nullable=False),
    sa.Column('ts_ms', bigint_column(), nullable=False),
    sa.Column('type', sa.Text, nullable=False),
    sa.Column('payload', jsonb_column(), nullable=False),
    sa.PrimaryKeyConstraint('task_id', 'event_id'),
)

# chat_artifacts — renderable reports promoted out of chat (md/html/svg).
# Single-col PK; two JSONB columns with '{}' default, a BOOLEAN pinned flag,
# and bigint timestamps. Exercises jsonb_column + bool_column + bigint_column.
CHAT_ARTIFACTS = define_table(
    'chat_artifacts',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('conv_id', sa.Text, nullable=False),
    sa.Column('task_id', sa.Text, nullable=False, server_default=''),
    sa.Column('msg_id', sa.Text, nullable=False, server_default=''),
    sa.Column('source', sa.Text, nullable=False),
    sa.Column('source_ref', jsonb_column(), nullable=False, server_default=sa.text("'{}'")),
    sa.Column('format', sa.Text, nullable=False),
    sa.Column('title', sa.Text, nullable=False, server_default=''),
    sa.Column('content', sa.Text, nullable=False),
    sa.Column('content_sha256', sa.Text, nullable=False),
    sa.Column('size_bytes', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('version', sa.Integer, nullable=False, server_default=sa.text('1')),
    sa.Column('parent_id', sa.Text, nullable=False, server_default=''),
    sa.Column('pinned', bool_column(), nullable=False, server_default=sa.false()),
    sa.Column('meta', jsonb_column(), nullable=False, server_default=sa.text("'{}'")),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.Column('deleted_at', bigint_column(), nullable=False, server_default=sa.text('0')),
)

# transcript_archive — pre-compaction snapshots + metadata. Auto-increment PK
# (SERIAL on PG / INTEGER AUTOINCREMENT on SQLite); created_at uses a
# per-dialect epoch_now() default. All metadata columns are in the base
# CREATE on both backends (the ALTERs in _schema_*.py are upgrade-only).
TRANSCRIPT_ARCHIVE = define_table(
    'transcript_archive',
    autoincrement_pk(),
    sa.Column('conv_id', sa.Text, nullable=False),
    sa.Column('messages_json', sa.Text, nullable=False),
    sa.Column('summary', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', bigint_column(), nullable=False, server_default=epoch_now()),
    sa.Column('trigger', sa.Text, nullable=False, server_default='force'),
    sa.Column('task_id', sa.Text, nullable=False, server_default=''),
    sa.Column('round_num', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('model', sa.Text, nullable=False, server_default=''),
    sa.Column('tokens_before', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('tokens_after', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('msgs_before', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('msgs_after', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('reason', sa.Text, nullable=False, server_default=''),
    sqlite_autoincrement=True,
)


# ── Wave 2 (2026-06): the remaining hand-DDL tables, migrated onto Core. ──
# Same parity-gated workflow as the tables above — each has a byte-equivalence
# test in tests/test_core_schema_parity.py that is green on BOTH backends.

# message_queue — unified priority turn-source queue. Single TEXT PK; payload /
# config are plain TEXT (json strings, not JSONB) with '{}' defaults.
#   kind     — turn source: 'real' (human), 'workflow_step', or 'autopilot'
#              (a persistent armed-marker sentinel that is NOT dispatched as a
#              task; the autopilot hook consults it). See lib/message_queue.py.
#   priority — lower = higher priority. real=10, workflow_step=50, autopilot=90.
#              Rows dispatch in (priority ASC, position ASC) order so a human
#              message always pre-empts an autopilot sentinel.
MESSAGE_QUEUE = define_table(
    'message_queue',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('conv_id', sa.Text, nullable=False),
    sa.Column('payload', sa.Text, nullable=False, server_default="{}"),
    sa.Column('config', sa.Text, nullable=False, server_default="{}"),
    sa.Column('position', sa.Integer, nullable=False, server_default=sa.text('1')),
    sa.Column('kind', sa.Text, nullable=False, server_default="real"),
    sa.Column('priority', sa.Integer, nullable=False, server_default=sa.text('100')),
    sa.Column('created_at', bigint_column(), nullable=False),
    # Dispatch lease (pt_4ab943fa): dequeue LEASES the row instead of deleting
    # it; the delete lands only after spawn_task succeeds. NULL leased_until =
    # not leased; '' lease_task_id = dispatch in flight, task not yet created.
    sa.Column('leased_until', bigint_column(), nullable=True),
    sa.Column('lease_task_id', sa.Text, nullable=False, server_default=''),
)

# scheduled_tasks — cron/agent task registry. Single TEXT PK. Mixes NOT-NULL
# columns, nullable TEXT columns with no default (last_run/last_result),
# nullable-with-default columns (description and the proactive-agent fields),
# and BOOLEAN flags (enabled/notify_*). The post-create ALTERs in _schema_*.py
# stay (upgrade-only); Core's create only fires on a fresh install.
SCHEDULED_TASKS = define_table(
    'scheduled_tasks',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('name', sa.Text, nullable=False),
    sa.Column('schedule', sa.Text, nullable=False),
    sa.Column('task_type', sa.Text, nullable=False, server_default='command'),
    sa.Column('command', sa.Text, nullable=False),
    sa.Column('description', sa.Text, server_default=''),
    sa.Column('enabled', bool_column(), nullable=False, server_default=sa.true()),
    sa.Column('notify_on_failure', bool_column(), nullable=False, server_default=sa.true()),
    sa.Column('notify_on_success', bool_column(), nullable=False, server_default=sa.false()),
    sa.Column('max_runtime', sa.Integer, nullable=False, server_default=sa.text('300')),
    sa.Column('last_run', sa.Text),
    sa.Column('last_result', sa.Text),
    sa.Column('last_status', sa.Text, server_default='never'),
    sa.Column('run_count', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('fail_count', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('created_at', sa.Text, nullable=False, server_default=''),
    sa.Column('updated_at', sa.Text, nullable=False, server_default=''),
    sa.Column('target_conv_id', sa.Text, server_default=''),
    sa.Column('source_conv_id', sa.Text, server_default=''),
    sa.Column('tools_config', sa.Text, server_default="{}"),
    sa.Column('poll_count', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('last_poll_at', sa.Text, server_default=''),
    sa.Column('last_poll_decision', sa.Text, server_default=''),
    sa.Column('last_poll_reason', sa.Text, server_default=''),
    sa.Column('last_execution_at', sa.Text, server_default=''),
    sa.Column('last_execution_task_id', sa.Text, server_default=''),
    sa.Column('last_execution_status', sa.Text, server_default=''),
    sa.Column('execution_count', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('max_executions', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('expires_at', sa.Text, server_default=''),
    # ── Predicate-promotion columns (scheduler condition paradigm) ──
    # condition_kind: 'llm' (default, unchanged behaviour) | 'code' (pure
    # predicate, zero-LLM) | 'hybrid' (LLM authoritative + reconcile a predicate
    # each poll, auto-promote to 'code' after promotion_streak agreements).
    sa.Column('condition_kind', sa.Text, nullable=False, server_default='llm'),
    sa.Column('condition_command', sa.Text, nullable=False, server_default=''),
    sa.Column('condition_regex', sa.Text, nullable=False, server_default=''),
    sa.Column('promotion_streak', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('fallback_streak', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('promoted_at', sa.Text, server_default=''),
)

# proactive_poll_log — append-only poll decisions. Auto-increment PK
# (SERIAL/INTEGER AUTOINCREMENT).
PROACTIVE_POLL_LOG = define_table(
    'proactive_poll_log',
    autoincrement_pk(),
    sa.Column('task_id', sa.Text, nullable=False),
    sa.Column('poll_time', sa.Text, nullable=False),
    sa.Column('decision', sa.Text, nullable=False, server_default='skip'),
    sa.Column('reason', sa.Text, nullable=False, server_default=''),
    sa.Column('status_snapshot', sa.Text, nullable=False, server_default=''),
    sa.Column('model', sa.Text, nullable=False, server_default=''),
    sa.Column('tokens_used', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('execution_task_id', sa.Text, server_default=''),
    # ── Predicate-promotion audit columns (machine-queryable, not free text) ──
    # tier: which mechanism decided this poll — 'llm'|'code'|'predicate'.
    # predicate_matched: 1/0 predicate outcome, -1 = not run or ambiguous.
    # llm_agreed: 1/0 hybrid reconciliation, -1 = not applicable. The current
    # promotion streak is reconstructable by counting trailing llm_agreed=1.
    sa.Column('tier', sa.Text, nullable=False, server_default='llm'),
    sa.Column('predicate_matched', sa.Integer, nullable=False, server_default=sa.text('-1')),
    sa.Column('llm_agreed', sa.Integer, nullable=False, server_default=sa.text('-1')),
    sqlite_autoincrement=True,
)

# timer_watchers — durable timer/condition watchers. Single TEXT PK; all TEXT +
# integer poll fields, nullable-with-default trailing columns.
TIMER_WATCHERS = define_table(
    'timer_watchers',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('conv_id', sa.Text, nullable=False),
    sa.Column('source_task_id', sa.Text, nullable=False, server_default=''),
    sa.Column('check_instruction', sa.Text, nullable=False),
    sa.Column('check_command', sa.Text, nullable=False, server_default=''),
    sa.Column('continuation_message', sa.Text, nullable=False),
    sa.Column('poll_interval', sa.Integer, nullable=False, server_default=sa.text('60')),
    sa.Column('max_polls', sa.Integer, nullable=False, server_default=sa.text('120')),
    sa.Column('poll_count', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('status', sa.Text, nullable=False, server_default='active'),
    sa.Column('tools_config', sa.Text, nullable=False, server_default="{}"),
    sa.Column('created_at', sa.Text, nullable=False, server_default=''),
    sa.Column('updated_at', sa.Text, nullable=False, server_default=''),
    sa.Column('triggered_at', sa.Text, server_default=''),
    sa.Column('cancelled_at', sa.Text, server_default=''),
    sa.Column('execution_task_id', sa.Text, server_default=''),
    sa.Column('last_poll_at', sa.Text, server_default=''),
    sa.Column('last_poll_decision', sa.Text, server_default=''),
    sa.Column('last_poll_reason', sa.Text, server_default=''),
    # ── Predicate-promotion columns (scheduler condition paradigm) ──
    # See SCHEDULED_TASKS for the condition_kind semantics — identical here.
    sa.Column('condition_kind', sa.Text, nullable=False, server_default='llm'),
    sa.Column('condition_command', sa.Text, nullable=False, server_default=''),
    sa.Column('condition_regex', sa.Text, nullable=False, server_default=''),
    sa.Column('promotion_streak', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('fallback_streak', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('promoted_at', sa.Text, server_default=''),
    # ── Provenance marker ──────────────────────────────────────────────
    # 'inline'     — created by the timer_create tool, which BLOCKS its
    #                parent task and polls inline. This is the ONLY way a
    #                timer is created today, so every existing row is inline.
    # 'background' — a self-driving background injector (future / proactive).
    # The resume-on-restart path uses this to tell a parent-blocking inline
    # timer (whose parent task died with the process → an orphan) apart from a
    # genuine background timer, so a resumed orphan is retired instead of
    # silently injecting a follow-up turn into an abandoned conversation.
    sa.Column('origin', sa.Text, nullable=False, server_default='inline'),
)

# timer_poll_log — append-only timer poll decisions. Auto-increment PK.
TIMER_POLL_LOG = define_table(
    'timer_poll_log',
    autoincrement_pk(),
    sa.Column('timer_id', sa.Text, nullable=False),
    sa.Column('poll_time', sa.Text, nullable=False),
    sa.Column('decision', sa.Text, nullable=False, server_default='wait'),
    sa.Column('reason', sa.Text, nullable=False, server_default=''),
    sa.Column('check_output', sa.Text, nullable=False, server_default=''),
    sa.Column('tokens_used', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('model', sa.Text, nullable=False, server_default=''),
    sa.Column('poll_id', sa.Text, nullable=False, server_default=''),
    sa.Column('raw_output', sa.Text, nullable=False, server_default=''),
    # ── Predicate-promotion audit columns (machine-queryable, not free text) ──
    # See PROACTIVE_POLL_LOG for the tier/predicate_matched/llm_agreed semantics.
    sa.Column('tier', sa.Text, nullable=False, server_default='llm'),
    sa.Column('predicate_matched', sa.Integer, nullable=False, server_default=sa.text('-1')),
    sa.Column('llm_agreed', sa.Integer, nullable=False, server_default=sa.text('-1')),
    sqlite_autoincrement=True,
)

# swarm_sessions — durable swarm session state. Single TEXT PK; TEXT json
# columns + bigint timestamps defaulting to 0.
SWARM_SESSIONS = define_table(
    'swarm_sessions',
    sa.Column('swarm_key', sa.Text, primary_key=True),
    sa.Column('conv_id', sa.Text, nullable=False, server_default=''),
    sa.Column('task_id', sa.Text, nullable=False, server_default=''),
    sa.Column('status', sa.Text, nullable=False, server_default='running'),
    sa.Column('specs_json', sa.Text, nullable=False, server_default="[]"),
    sa.Column('config_json', sa.Text, nullable=False, server_default="{}"),
    sa.Column('created_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('updated_at', bigint_column(), nullable=False, server_default=sa.text('0')),
)

# swarm_agents — per-agent message checkpoints. Composite PK (swarm_key,
# agent_id); delivered is an INTEGER flag (0/1) on both backends in the live
# DDL — kept as plain Integer, NOT bool_column.
SWARM_AGENTS = define_table(
    'swarm_agents',
    sa.Column('swarm_key', sa.Text, nullable=False),
    sa.Column('agent_id', sa.Text, nullable=False),
    sa.Column('role', sa.Text, nullable=False, server_default=''),
    sa.Column('objective', sa.Text, nullable=False, server_default=''),
    sa.Column('status', sa.Text, nullable=False, server_default='pending'),
    sa.Column('messages_json', sa.Text, nullable=False, server_default="[]"),
    sa.Column('result_json', sa.Text, nullable=False, server_default="{}"),
    sa.Column('rounds_used', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('delivered', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('updated_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.PrimaryKeyConstraint('swarm_key', 'agent_id'),
)

# orchestration_runs — durable flow-run instances. Single TEXT PK.
ORCHESTRATION_RUNS = define_table(
    'orchestration_runs',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('orch_id', sa.Text, nullable=False, server_default=''),
    sa.Column('name', sa.Text, nullable=False, server_default=''),
    sa.Column('definition', sa.Text, nullable=False, server_default="{}"),
    sa.Column('input', sa.Text, nullable=False, server_default=''),
    sa.Column('status', sa.Text, nullable=False, server_default='pending'),
    sa.Column('final', sa.Text, nullable=False, server_default=''),
    sa.Column('error', sa.Text, nullable=False, server_default=''),
    sa.Column('created_by', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('updated_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('finished_at', bigint_column(), nullable=False, server_default=sa.text('0')),
)

# orchestration_run_events — append-only durable event log; composite PK
# (run_id, seq).
ORCHESTRATION_RUN_EVENTS = define_table(
    'orchestration_run_events',
    sa.Column('run_id', sa.Text, nullable=False),
    sa.Column('seq', sa.Integer, nullable=False),
    sa.Column('type', sa.Text, nullable=False, server_default=''),
    sa.Column('node_id', sa.Text, nullable=False, server_default=''),
    sa.Column('payload', sa.Text, nullable=False, server_default="{}"),
    sa.Column('ts', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.PrimaryKeyConstraint('run_id', 'seq'),
)

# project_events — append-only cross-conversation activity feed ("project
# brain" pulse), keyed on project_path. Composite PK (project_path, seq): seq
# is a per-project monotonic counter so the frontend can do Last-Event-ID
# style incremental fetch without a global sequence. No FK to conversations —
# a project_path is a string key, not a row (mirrors recent_projects). payload
# is kind-specific extra json (TEXT). See lib/conversations/project_feed.py.
PROJECT_EVENTS = define_table(
    'project_events',
    sa.Column('project_path', sa.Text, nullable=False),
    sa.Column('seq', sa.Integer, nullable=False),
    sa.Column('event_id', sa.Text, nullable=False, server_default=''),
    sa.Column('conv_id', sa.Text, nullable=False, server_default=''),
    sa.Column('task_id', sa.Text, nullable=False, server_default=''),
    sa.Column('kind', sa.Text, nullable=False, server_default='note'),
    sa.Column('title', sa.Text, nullable=False, server_default=''),
    sa.Column('summary', sa.Text, nullable=False, server_default=''),
    sa.Column('payload', sa.Text, nullable=False, server_default="{}"),
    sa.Column('ts', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.PrimaryKeyConstraint('project_path', 'seq'),
)

# project_charter — the "north star" per project (Pillar #2 of the project
# brain). ONE row per project_path (single TEXT PK, upsert semantics): the
# living goal/north-star (`content`) + the COMMITTED key decisions
# (`decisions`, a JSON array). Agents may only PROPOSE amendments (which land
# in project_events as kind='proposed_decision'); the actual commit is
# human-gated and bumps `version` (optimistic lock) so two concurrent commits
# can't silently clobber. See lib/conversations/project_charter.py.
PROJECT_CHARTER = define_table(
    'project_charter',
    sa.Column('project_path', sa.Text, primary_key=True),
    sa.Column('content', sa.Text, nullable=False, server_default=''),
    sa.Column('decisions', sa.Text, nullable=False, server_default="[]"),
    sa.Column('updated_by_conv', sa.Text, nullable=False, server_default=''),
    sa.Column('updated_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('version', sa.Integer, nullable=False, server_default=sa.text('0')),
)

# project_tasks — the coordination BOARD (Pillar #3 of the project brain).
# Coarse, human-meaningful epics per project_path: conversations POST work they
# discover, CLAIM an epic (a SOFT, TTL-expiring lease — advisory, never a hard
# lock, so a crashed/abandoned conversation can never deadlock the board), and
# COMPLETE it. status ∈ {open, claimed, done}; lease_expires_at is checked
# at-READ-time (an expired claim reads as open — no background reaper).
# depends_on is a JSON array of task ids (intra-board dependency — NOT a second
# namespace). See lib/conversations/project_board.py.
PROJECT_TASKS = define_table(
    'project_tasks',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('project_path', sa.Text, nullable=False, server_default=''),
    sa.Column('title', sa.Text, nullable=False, server_default=''),
    sa.Column('status', sa.Text, nullable=False, server_default='open'),
    sa.Column('owner_conv_id', sa.Text, nullable=False, server_default=''),
    sa.Column('lease_expires_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('created_by_conv', sa.Text, nullable=False, server_default=''),
    sa.Column('depends_on', sa.Text, nullable=False, server_default="[]"),
    # kind: 'epic' (default — a coordination work-item, dispatchable) or
    # 'lease' (a durational resource/path RESERVATION — "a sibling is actively
    # editing these paths, hold off"). A lease reuses the SAME soft TTL-lease +
    # at-read-time expiry as an epic claim, but is EXCLUDED from
    # select_dispatchable (never auto-dispatched as work) and rendered in its
    # own "Held" section. See lib/conversations/project_board.py::claim_lease.
    sa.Column('kind', sa.Text, nullable=False, server_default='epic'),
    # dispatched: 1 when the CURRENT claim was minted by brain-driven dispatch
    # (the heartbeat/completion sweep) rather than a human/agent claim — surfaced
    # as a "brain-dispatched" badge on the board card. Reset to 0 on complete.
    sa.Column('dispatched', sa.Integer, nullable=False, server_default=sa.text('0')),
    # blocked_until / block_count / block_reason: the BLOCK COOLDOWN (a
    # self-expiring escalating backoff, NOT the removed park shelf). When an
    # epic hits a genuine external gate, block_task stamps blocked_until = now +
    # an ESCALATING cooldown (capped) and records why; select_dispatchable skips
    # a row whose blocked_until is still in the future (at-READ-time expiry, no
    # reaper, no human un-block gate — so it can never deadlock). block_count
    # drives the escalation; both reset to 0 on complete / reopen so a human
    # reopen forces an immediate retry. See project_board.py::block_task.
    sa.Column('blocked_until', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('block_count', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('block_reason', sa.Text, nullable=False, server_default=''),
    # wait_paths: the wait-on-path commit-dependency (Pillar #3). A JSON array
    # of path/resource strings this epic must wait on — resolved as the INVERSE
    # READ of the path-lease: select_dispatchable holds the epic while any
    # listed path is under a LIVE lease held by a DIFFERENT conversation, and
    # releases automatically when that lease expires (at read time, no reaper).
    # NOT a new lock namespace — it reads the SAME kind='lease' rows. Reset to
    # '[]' on complete/reopen. See docs/PROJECT_BRAIN_WAIT_ON_PATH.md.
    sa.Column('wait_paths', sa.Text, nullable=False, server_default="[]"),
    # dispatch_target: MUTABLE routing override for idle-sibling migration
    # (Pillar #5). Dispatch routes to dispatch_target or created_by_conv. When
    # the originating conv is genuinely stuck (no live task + kickoff undrained
    # past the lease TTL, and NOT held by cooldown/wait), the sweep migrates the
    # epic to a genuinely-idle sibling by setting this field — WITHOUT touching
    # created_by_conv (immutable authorship/provenance). Reset to '' on
    # complete/reopen. See docs/PROJECT_BRAIN_MIGRATION.md.
    sa.Column('dispatch_target', sa.Text, nullable=False, server_default=''),
    # write_set: dispatch-time file-ownership partitioning (Pillar #3 / worktree
    # isolation §4). A JSON array of path/glob/subsystem-tag strings this epic
    # intends to WRITE. select_dispatchable PREFERS an epic whose write_set is
    # DISJOINT from every live-claimed epic's write_set, shifting collision
    # detection LEFT from land-time to dispatch-time so two convs don't get
    # handed epics that will fight over the same files. A SOFT preference, never
    # a hard filter: an empty write_set (the default) means "unknown footprint"
    # and is treated as non-conflicting so a pre-migration / undeclared epic is
    # NEVER stranded. Reset to '[]' on complete/reopen. See
    # docs/PROJECT_BRAIN_WORKTREE_ISOLATION.md §4.
    sa.Column('write_set', sa.Text, nullable=False, server_default="[]"),
    # block_question / human_answer: the STRUCTURED human gate on a block
    # (Pillar #3). block_task may carry a question for the human (JSON
    # {"q", "options": [{"label", "description"?}]}); an epic with a
    # PENDING question (block_question set, human_answer empty) is EXCLUDED
    # from select_dispatchable regardless of cooldown — re-running before
    # the answer can only re-discover the same gate (the billed-turn loop).
    # answer_task records the human's answer, clears the cooldown + question,
    # and the next dispatch injects the Q&A into the kickoff. Both reset on
    # complete/reopen; a fresh block supersedes a stale answer. See
    # project_board.py::answer_task. Added 2026-07.
    sa.Column('block_question', sa.Text, nullable=False, server_default=''),
    sa.Column('human_answer', sa.Text, nullable=False, server_default=''),
    # blocked_by: PROVENANCE of the last block — the conversation that called
    # block_task. The operator's first question on a halted-epic card is
    # "which chat asked me this?" and before this column the answer existed
    # only in the feed/audit trail, not on the row (owner_conv_id projects ''
    # for a blocked epic because it is not claimed). Written on EVERY block
    # (superseded like block_reason); deliberately NOT cleared by
    # answer/complete/reopen — it records who blocked LAST, nothing reads it
    # once the block state is gone. Added 2026-08.
    sa.Column('blocked_by', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('updated_at', bigint_column(), nullable=False, server_default=sa.text('0')),
)

# project_status_snapshots — the human↔brain status lane (Pillar #7 of the
# project brain). Append-only, keyed on project_path; seq is a per-project
# monotonic counter (composite PK, mirrors project_events) so the trail can be
# read newest-first / incrementally. Each row is ONE synthesized "where is the
# project / are we drifting from the charter" narrative + the pillar_state JSON
# evidence it was generated from + the trigger that minted it. Retention is
# bounded (pruned on insert). HUMAN-FACING ONLY — never injected into sibling
# agent prompts. See lib/conversations/project_status.py.
PROJECT_STATUS_SNAPSHOTS = define_table(
    'project_status_snapshots',
    sa.Column('project_path', sa.Text, nullable=False),
    sa.Column('seq', sa.Integer, nullable=False),
    sa.Column('snapshot_id', sa.Text, nullable=False, server_default=''),
    sa.Column('narrative', sa.Text, nullable=False, server_default=''),
    sa.Column('pillar_state', sa.Text, nullable=False, server_default="{}"),
    sa.Column('trigger', sa.Text, nullable=False, server_default='manual'),
    sa.Column('ts', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.PrimaryKeyConstraint('project_path', 'seq'),
)


# project_watch_items — the human's standing "things I care about" list (Pillar
# #7 watch lane). The HUMAN authors each item (kind: concern|question|goal +
# free text); the brain addresses it on a recurring basis. Single TEXT PK
# (item_id). status: open|resolved.
#
# Promotion columns — AUDIT TRAIL ONLY, never the UI's source of truth:
#   `promoted`      — a one-shot "a promotion was performed" marker for a
#                     concern/question. It is NOT a live answer to "is this
#                     reaching agents right now": the charter can be deleted or
#                     a committed decision FIFO-evicted, and this boolean would
#                     still read 1. (Measured: promoted=1 while read_charter()
#                     reported exists=False.) The live answer is COMPUTED at
#                     read time by project_watch.promotion_state().
#
#                     A GOAL has no promotion at all: every OPEN goal is
#                     injected into every sibling conversation's prompt by
#                     project_watch.render_goals_injection_block(), so its
#                     presence is decided by (kind, status) and nothing else.
#                     The former `promoted_text` / `promoted_at` receipt columns
#                     were DROPPED with that design (2026-07-30): they existed
#                     solely to say WHICH SIDE moved when a goal's text and the
#                     charter's copy of it diverged, and a goal is no longer
#                     copied anywhere, so divergence is not a reachable state.
#
# HUMAN-FACING ONLY — never injected into sibling agent prompts. See
# lib/conversations/project_watch.py.
PROJECT_WATCH_ITEMS = define_table(
    'project_watch_items',
    sa.Column('item_id', sa.Text, primary_key=True),
    sa.Column('project_path', sa.Text, nullable=False, server_default=''),
    sa.Column('kind', sa.Text, nullable=False, server_default='concern'),
    sa.Column('text', sa.Text, nullable=False, server_default=''),
    sa.Column('status', sa.Text, nullable=False, server_default='open'),
    sa.Column('promoted', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('response_fingerprint', sa.Text, nullable=False, server_default=''),
    sa.Column('created_by_conv', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('updated_at', bigint_column(), nullable=False, server_default=sa.text('0')),
)

# project_watch_responses — append-only trail of the brain's recurring
# responses to a watch item, keyed on item_id with a monotonic per-item seq
# (composite PK, mirrors project_events). Bounded retention per item (pruned on
# insert). A concern whose answer CHANGES over time is the drift signal, so we
# keep the trail, not latest-only. HUMAN-FACING ONLY.
PROJECT_WATCH_RESPONSES = define_table(
    'project_watch_responses',
    sa.Column('item_id', sa.Text, nullable=False),
    sa.Column('seq', sa.Integer, nullable=False),
    sa.Column('project_path', sa.Text, nullable=False, server_default=''),
    sa.Column('response', sa.Text, nullable=False, server_default=''),
    sa.Column('pillar_state', sa.Text, nullable=False, server_default="{}"),
    sa.Column('trigger', sa.Text, nullable=False, server_default='manual'),
    sa.Column('ts', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.PrimaryKeyConstraint('item_id', 'seq'),
)

# optimizer_proposals — nightly self-tuning proposals. Single TEXT PK;
# confidence is DOUBLE PRECISION/REAL.
OPTIMIZER_PROPOSALS = define_table(
    'optimizer_proposals',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('created_at', sa.Text, nullable=False),
    sa.Column('title', sa.Text, nullable=False),
    sa.Column('rationale', sa.Text, nullable=False),
    sa.Column('action_type', sa.Text, nullable=False),
    sa.Column('action_args', sa.Text, nullable=False),
    sa.Column('severity', sa.Text, nullable=False, server_default='low'),
    sa.Column('confidence', double_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('evidence', sa.Text, nullable=False, server_default=''),
    sa.Column('status', sa.Text, nullable=False, server_default='pending_review'),
    sa.Column('status_reason', sa.Text, nullable=False, server_default=''),
)

# optimizer_action_log — applied-action audit + revert tracking. Single TEXT PK.
OPTIMIZER_ACTION_LOG = define_table(
    'optimizer_action_log',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('proposal_id', sa.Text, nullable=False),
    sa.Column('applied_at', sa.Text, nullable=False),
    sa.Column('expires_at', sa.Text, nullable=False, server_default=''),
    sa.Column('pre_metric', sa.Text, nullable=False, server_default=''),
    sa.Column('outcome_metric', sa.Text, nullable=False, server_default=''),
    sa.Column('outcome_recorded_at', sa.Text, nullable=False, server_default=''),
    sa.Column('reverted_at', sa.Text, nullable=False, server_default=''),
    sa.Column('revert_reason', sa.Text, nullable=False, server_default=''),
)

# rate_limit_events — per-request gate log. BIGSERIAL/INTEGER AUTOINCREMENT PK
# (high-churn, id can exceed 32 bits). ts_ms is epoch-ms.
RATE_LIMIT_EVENTS = define_table(
    'rate_limit_events',
    bigint_autoincrement_pk(),
    sa.Column('endpoint', sa.Text, nullable=False),
    sa.Column('ip', sa.Text, nullable=False),
    sa.Column('ts_ms', bigint_column(), nullable=False),
    sqlite_autoincrement=True,
)

# error_resolutions — operator error-triage notes. Single TEXT PK. NOTE: this
# table is created ONLY on PostgreSQL in the live bootstrap (it has no SQLite
# CREATE), so only a PG parity test + PG-path wiring exist for it.
ERROR_RESOLUTIONS = define_table(
    'error_resolutions',
    sa.Column('fingerprint', sa.Text, primary_key=True),
    sa.Column('logger_name', sa.Text, nullable=False, server_default=''),
    sa.Column('sample_message', sa.Text, nullable=False, server_default=''),
    sa.Column('resolved_by', sa.Text, nullable=False, server_default=''),
    sa.Column('ticket', sa.Text, nullable=False, server_default=''),
    sa.Column('notes', sa.Text, nullable=False, server_default=''),
    sa.Column('resolved_at', bigint_column(), nullable=False),
    sa.Column('updated_at', bigint_column(), nullable=False),
)

# tenant_users — multi-tenant relay user table (distinct from chat `users`).
# email is inline UNIQUE; email_verified is a plain INTEGER (0/1) on BOTH
# backends in the live DDL — NOT bool_column. metadata is TEXT json.
TENANT_USERS = define_table(
    'tenant_users',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('email', sa.Text, nullable=False, unique=True),
    sa.Column('password_hash', sa.Text, nullable=False, server_default=''),
    sa.Column('display_name', sa.Text, nullable=False, server_default=''),
    sa.Column('role', sa.Text, nullable=False, server_default='user'),
    sa.Column('status', sa.Text, nullable=False, server_default='active'),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.Column('last_login_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('email_verified', sa.Integer, nullable=False, server_default=sa.text('0')),
    sa.Column('metadata', sa.Text, nullable=False, server_default="{}"),
)

# billing_ledger — append-only source-of-truth for credit movements. Single
# TEXT PK; all amounts are BIGINT micro-credits.
BILLING_LEDGER = define_table(
    'billing_ledger',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('user_id', sa.Text, nullable=False),
    sa.Column('ts', bigint_column(), nullable=False),
    sa.Column('amount_micro', bigint_column(), nullable=False),
    sa.Column('kind', sa.Text, nullable=False),
    sa.Column('ref_type', sa.Text, nullable=False, server_default=''),
    sa.Column('ref_id', sa.Text, nullable=False, server_default=''),
    sa.Column('balance_after_micro', bigint_column(), nullable=False),
    sa.Column('note', sa.Text, nullable=False, server_default=''),
)

# billing_wallets — denormalized balance cache. Single TEXT PK (user_id).
BILLING_WALLETS = define_table(
    'billing_wallets',
    sa.Column('user_id', sa.Text, primary_key=True),
    sa.Column('balance_micro', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('currency', sa.Text, nullable=False, server_default='CREDIT'),
    sa.Column('low_balance_alert_micro', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('updated_at', bigint_column(), nullable=False),
)

# billing_redeem_codes — prepaid redeem codes. Single TEXT PK (code).
BILLING_REDEEM_CODES = define_table(
    'billing_redeem_codes',
    sa.Column('code', sa.Text, primary_key=True),
    sa.Column('amount_micro', bigint_column(), nullable=False),
    sa.Column('batch', sa.Text, nullable=False, server_default=''),
    sa.Column('created_by', sa.Text, nullable=False, server_default=''),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.Column('expires_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('redeemed_by', sa.Text, nullable=False, server_default=''),
    sa.Column('redeemed_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('note', sa.Text, nullable=False, server_default=''),
)

# billing_payments — external payment records. Single TEXT PK. amount_minor is
# minor-currency units; credit_micro is the granted micro-credits. raw is TEXT json.
BILLING_PAYMENTS = define_table(
    'billing_payments',
    sa.Column('id', sa.Text, primary_key=True),
    sa.Column('user_id', sa.Text, nullable=False),
    sa.Column('provider', sa.Text, nullable=False),
    sa.Column('provider_id', sa.Text, nullable=False, server_default=''),
    sa.Column('amount_minor', bigint_column(), nullable=False),
    sa.Column('currency', sa.Text, nullable=False, server_default='USD'),
    sa.Column('credit_micro', bigint_column(), nullable=False),
    sa.Column('status', sa.Text, nullable=False, server_default='pending'),
    sa.Column('created_at', bigint_column(), nullable=False),
    sa.Column('settled_at', bigint_column(), nullable=False, server_default=sa.text('0')),
    sa.Column('raw', sa.Text, nullable=False, server_default="{}"),
)
