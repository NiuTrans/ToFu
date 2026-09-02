#!/usr/bin/env python3
"""One-shot deep clean for the SQLite storage authority (2026-08-20 bloat).

Measured on the live authority: 395.2 GiB file, 71% of it
``storage_attempt_events`` (281 GiB) — the v2 SSE transport log historically
stored a FULL projection copy per streaming delta.  The durable code fixes
(slim frames + rolling TTL prune + online incremental reclaim) stop the
growth and keep steady state; THIS script is the operator window that
reclaims the accumulated history in one audited pass.

Runbook (the window the owner schedules):

  1. Deploy the slim-frame/prune/reclaim build and restart — the rolling
     maintenance loop begins deleting settled streams older than
     ``TOFU_ATTEMPT_EVENT_TTL_DAYS`` (personal default 1; distributed
     default 7).
  2. Stop the server (the sidecar stops with it and checkpoints its WAL).
  3. ``python3 scripts/storage_deep_clean.py --offline --confirm``
     …which holds the project lease for the whole pass, deletes the
     TTL-eligible transport rows, recovers typed v21 task events, removes
     private usage graphs from retained round records, and compresses large
     task-event JSON in bounded transactions (checkpointing
     the WAL between them so the window never needs 300 GiB of spill),
     backfills the existing lossless codec on inactive Turn projections of at
     least 64 KiB without touching a checkpoint/patch head,
     backfills current compaction transcript archives and owner-resolves the
     exact retired generic-record archive shape into that single authority,
     backfills large historical task-result strings with the runtime field
     codec while leaving lifecycle/ownership fields queryable and versions
     unchanged,
     losslessly interns duplicated tool payloads, compresses individually
     large messages in frozen pre-Turn conversation archives under separate
     64 MiB read/write bounds while preserving bounded head/tail reads, and
     clears their obsolete rebuildable header search copies in the same write,
     rewrites the authority compactly with ``VACUUM INTO``, verifies integrity
     + row parity + auto_vacuum mode, and swaps atomically. The newest pre-clean file
     is RETAINED as ``data/tofu.db.pre-compact-<stamp>``; after publication,
     older rollback points are pruned to a bounded count (default one).
     On a constrained personal disk that cannot hold both files, use
     ``--low-space`` only after making an independent backup. It reclaims
     free pages in bounded in-place batches, checkpointing between batches,
     then runs the same integrity and authority-row checks; it deliberately
     cannot retain an on-volume rollback copy.
     Add ``--retire-legacy-conversation-mirrors`` only when the analysis report
     offers it. The optional pass deletes a frozen pre-Sidecar conversation and
     its normalized message rows only after the old array, reconstructed row
     mirror, and current Sidecar archive have identical canonical JSON. Rows
     with legacy Turns or any missing/malformed/mismatched witness survive.
  4. Start the server.  Steady state is self-maintaining from here.

After the replacement has been observed healthy, retire one exact retained
recovery point in a stopped-server window with
``--retire-rollback <basename> --confirm``. The command holds the same project
lease, checks the current authority, and never accepts a path or glob.

``--analyze`` is read-only and safe against the live authority at any time;
it prints the header facts (file/pages/freelist/mode) plus deadline-bounded,
exact logical payload bytes for the transport and legacy tables so the window
can be planned with real numbers. It also measures expired Sidecar and legacy
transport with the exact selectors used by the delete pass and emits one
capacity-gated offline-maintenance action. The scan never treats a sparse
``rowid`` span as a row count.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import sys
import time
from typing import NamedTuple

import orjson

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lib.storage_sidecar.durability import fsync_directory, fsync_file
from lib.storage_sidecar import offline_maintenance as _SQLITE_TOOLING
from lib.storage_sidecar.archived_message_codec import (
    ARCHIVED_MESSAGE_CODEC_KEY,
    decode_archived_message_sequence_from_storage,
    encode_archived_message_sequence_with_metrics,
)
from lib.storage_sidecar.offline_compaction_archive_maintenance import (
    maintain_compaction_archive_storage,
)
from lib.storage_sidecar.offline_task_result_maintenance import (
    maintain_task_result_storage,
)
from lib.storage_sidecar.backup_policy import (
    prune_retained_rollbacks,
    resolve_rollback_artifact,
    rollback_artifact_inventory,
    verified_backup_inventory,
)
from lib.storage_sidecar.preflight import ProjectLease
from lib.storage_sidecar.projection_codec import (
    ProjectionCodecError,
    STORAGE_PROJECTION_CODEC_KEY,
    decode_projection_from_storage,
    encode_projection_for_storage,
)
from lib.storage_sidecar.reclaim_policy import (
    copy_capacity_requirement,
    requires_offline_compaction,
)
from lib.storage_sidecar.schema import (
    LEGACY_TASK_EVENT_RETENTION_INDEX_NAME,
    LEGACY_TASK_EVENT_RETENTION_TYPE_LIMIT,
    OBSOLETE_DEFERRED_INDEX_NAMES,
    TASK_EVENT_RETENTION_INDEX_NAMES,
    deferred_index_statements,
)
from lib.storage_sidecar.task_event_codec import (
    COMPRESSED_TASK_EVENT_MAGIC,
    TASK_EVENT_COMPRESSION_MIN_BYTES,
    decode_task_event_payload,
    encode_task_event_payload,
)
from lib.storage_sidecar.task_result_field_codec import (
    TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES,
)
from lib.task_event_contract import (
    STRUCTURAL_EVENT_TYPES,
    TASK_EVENT_STREAMING_RETENTION_MS,
    TASK_EVENT_STRUCTURAL_RETENTION_MS,
)
from lib.storage_projection import project_event_usage_for_storage

# Tables whose row counts must match between the old and the compacted
# authority.  These are the user-visible authorities; transport tables
# (storage_attempt_events) intentionally differ — that is the point.
_PARITY_TABLES = (
    'storage_conversations', 'storage_conversation_turns',
    'storage_turn_projection_checkpoints',
    'storage_generation_attempts', 'storage_records', 'storage_events',
    'billing_ledger', 'billing_wallets', 'message_queue',
    'storage_queue_items', 'storage_command_receipts',
    'storage_command_receipts_v2',
)

# (table, payload columns) sampled by --analyze. The list covers every table
# measured above ~1 GiB during the 2026-08-20 incident, receipt authorities
# with known monotonic growth, and legacy rollback copies, so the report shows
# both live bloat and frozen rollback weight an operator may retire separately.
_ANALYZE_TABLES = (
    ('storage_attempt_events', ('payload_json',)),
    ('storage_events', ('event_json',)),
    ('storage_conversation_turns', ('projection_json', 'settlement_json')),
    ('storage_turn_projection_checkpoints', ('projection_json',)),
    ('storage_conversations', ('messages_json', 'search_text')),
    ('storage_records', ('value_json',)),
    ('storage_command_receipts', ('response_json',)),
    ('storage_command_receipts_v2', ('response_json',)),
    ('task_events', ('payload',)),
    ('attempt_events', ('payload',)),
    ('task_results', ('content', 'thinking', 'tool_rounds', 'search_results',
                      'segments', 'metadata')),
    ('conversation_messages', (
        'content', 'content_json', 'thinking', 'translated_content', 'meta',
        'meta_light', 'billing_meta', 'translation_state',
    )),
    ('conversations', ('messages', 'search_text')),
    ('conversation_turns', ('projection', 'settlement')),
    ('transcript_archive', ('messages_json', 'summary')),
)

_OPERATOR_MANAGED_LARGE_FILE_MIN_BYTES = 1024 * 1024 * 1024
_OPERATOR_MANAGED_FILE_SCAN_LIMIT = 256
_OPERATOR_MANAGED_DIRECTORY_SCAN_LIMIT = 256
_OPERATOR_MANAGED_DIRECTORY_ENTRY_SCAN_LIMIT = 256
_OPERATOR_MANAGED_DIRECTORY_LIFECYCLES = {
    'db_snapshots': 'retired_sqlite_backup_owner_review',
    'pg_backups': 'postgres_backup_owner_review',
}
_OPERATOR_MANAGED_DIRECTORY_PREFIX_LIFECYCLES = {
    'retired_migration_artifacts-': 'retired_migration_owner_review',
}
_LIVE_SQLITE_FILENAMES = frozenset({'tofu.db', 'tofu.db-wal', 'tofu.db-shm'})
_LEGACY_TRANSPORT_TABLES = frozenset({'attempt_events', 'task_events'})
_LEGACY_TRANSPORT_SELECT_ROWS = 900
_LEGACY_TRANSPORT_BATCH_PAYLOAD_BYTES = 128 * 1024 * 1024
_LEGACY_CONVERSATION_MIRROR_TABLES = frozenset({
    'conversations',
    'conversation_messages',
    'conversation_turns',
    'storage_conversations',
})
_LEGACY_CONVERSATION_MIRROR_SELECT_ROWS = 64
_LEGACY_CONVERSATION_MIRROR_BATCH_PAYLOAD_BYTES = 128 * 1024 * 1024
_LEGACY_CONVERSATION_MIRROR_DOCUMENT_BYTES = 64 * 1024 * 1024
_LEGACY_MESSAGE_MIRROR_PAYLOAD_COLUMNS = (
    'content',
    'content_json',
    'thinking',
    'translated_content',
    'meta',
    'meta_light',
    'billing_meta',
    'translation_state',
)
_LEGACY_TRANSLATION_MESSAGE_KEYS = (
    'translatedContent',
    '_showingTranslation',
    '_translateDone',
    '_translateModel',
)
_ARCHIVED_CONVERSATION_SELECT_ROWS = 64
_ARCHIVED_CONVERSATION_PAGE_PAYLOAD_BYTES = 64 * 1024 * 1024
_ARCHIVED_CONVERSATION_DOCUMENT_BYTES = 64 * 1024 * 1024
_TURN_PROJECTION_CODEC_MIN_BYTES = 64 * 1024
_TURN_PROJECTION_SELECT_ROWS = 64
_TURN_PROJECTION_PAGE_PAYLOAD_BYTES = 64 * 1024 * 1024
_TURN_PROJECTION_DOCUMENT_BYTES = 64 * 1024 * 1024
_TASK_EVENT_MAINTENANCE_SELECT_ROWS = 4096
_TASK_EVENT_MAINTENANCE_BATCH_PAYLOAD_BYTES = 64 * 1024 * 1024
_TASK_EVENT_USAGE_PROJECTION_TYPE = 'round_usage'
_TASK_EVENT_MAINTENANCE_PAGE_SQL = (
    'SELECT rowid, event_type, event_kind, created_at_ms, '
    'length(CAST(event_json AS BLOB)) AS payload_bytes '
    'FROM storage_events NOT INDEXED '
    'WHERE rowid > ? AND stream_kind = ? '
    'ORDER BY rowid LIMIT ?'
)
_ANALYZE_SQL_BUDGET_SECONDS = 60.0
_ANALYZE_SQL_PROGRESS_STEPS = 10_000
_ANALYZE_EVENT_GROUP_LIMIT = 64


class _ArchivedMessageCompaction(NamedTuple):
    """One verified message-document rewrite and its stage attribution."""

    stored_document: bytes
    already_encoded: bool
    projection_encoded_messages: int
    compressed_messages: int
    public_document_bytes: int
    projected_document_bytes: int


class _ArchivedConversationCandidate(NamedTuple):
    """One bounded archive row selected without materializing its bodies."""

    conversation_id: str
    user_id: int
    message_bytes: int
    search_text_bytes: int


class _ArchivedConversationUpdate(NamedTuple):
    """One CAS-fenced archive/search rewrite held for the current page."""

    stored_document: bytes
    conversation_id: str
    user_id: int
    input_message_bytes: int
    stored_message_bytes: int
    search_text_bytes: int
    projection_encoded_messages: int
    compressed_messages: int
    public_document_bytes: int
    projected_document_bytes: int
    messages_compacted: bool


class _TurnProjectionCandidate(NamedTuple):
    """One inactive inline Turn projection selected metadata-first."""

    turn_id: str
    conversation_id: str
    user_id: int
    projection_revision: int
    projection_bytes: int


class _TurnProjectionUpdate(NamedTuple):
    """One lossless existing-codec rewrite with its CAS witnesses."""

    stored_document: bytes
    turn_id: str
    conversation_id: str
    user_id: int
    projection_revision: int
    input_bytes: int


def _parse_header(path: Path) -> dict:
    """O(1) facts from the 100-byte SQLite header — safe on a live file."""
    with path.open('rb') as handle:
        header = handle.read(100)
    if header[:16] != b'SQLite format 3\x00':
        raise RuntimeError(f'{path} is not a SQLite 3 database')
    page_size = struct.unpack('>H', header[16:18])[0]
    if page_size == 1:
        page_size = 65536
    freelist = struct.unpack('>I', header[36:40])[0]
    incremental = struct.unpack('>I', header[64:68])[0]
    page_count = path.stat().st_size // page_size
    live_bytes = max(0, page_count - freelist) * page_size
    return {
        'bytes': path.stat().st_size,
        'page_size': page_size,
        'page_count': page_count,
        'freelist_pages': freelist,
        'freelist_bytes': freelist * page_size,
        'freelist_ratio': round(freelist / max(1, page_count), 6),
        'live_bytes': live_bytes,
        'auto_vacuum': 'incremental' if incremental else 'none-or-full',
    }


def _open_readonly(path: Path) -> sqlite3.Connection:
    return _SQLITE_TOOLING.open_sqlite_tool_connection(path, writable=False)


def _quote_identifier(value: str) -> str:
    """Quote one compile-time SQLite identifier used by the fixed inventory."""
    return '"' + str(value).replace('"', '""') + '"'


def _payload_length_expression(columns: tuple[str, ...]) -> str:
    """Return encoded byte length, not Unicode code-point length."""
    return '+'.join(
        'length(CAST(COALESCE('
        + _quote_identifier(column)
        + ",'') AS BLOB))"
        for column in columns
    )


def _fetch_measurement_rows(
    connection: sqlite3.Connection,
    statement: str,
    parameters: tuple = (),
    *,
    all_rows: bool = False,
):
    """Execute one read-only measurement query through a shared boundary.

    Analysis queries intentionally remain inside this stopped-server operator
    tool, but their direct driver capability should not be duplicated across
    every table-specific projector. Keeping execution here also makes a future
    progress/deadline hook one change rather than one per measurement.
    """
    cursor = connection.execute(statement, parameters)
    return cursor.fetchall() if all_rows else cursor.fetchone()


def _offline_maintenance_plan(compaction_plan: dict) -> dict:
    """Turn measured physical/logical work into one safe operator action."""
    physical_freelist = bool(
        compaction_plan.get('offline_compaction_recommended'))
    index_work = bool(
        compaction_plan.get('offline_index_maintenance_required'))
    retention = compaction_plan.get('transport_retention') or {}
    expired_payload_bytes = int(
        retention.get('candidate_payload_bytes') or 0)
    mirror = compaction_plan.get(
        'legacy_conversation_mirror_retirement') or {}
    mirror_payload_bytes = (
        int(mirror.get('measured_payload_bytes') or 0)
        if mirror.get('measurement_complete') else 0
    )
    header_search = compaction_plan.get(
        'conversation_search_text_retirement') or {}
    header_search_payload_bytes = (
        int(header_search.get('payload_bytes') or 0)
        if header_search.get('measurement_complete') else 0
    )

    reasons = []
    if physical_freelist:
        reasons.append('bulk_freelist')
    if expired_payload_bytes:
        reasons.append('expired_transport')
    if mirror_payload_bytes:
        reasons.append('conditional_legacy_conversation_mirrors')
    if header_search_payload_bytes:
        reasons.append('rebuildable_conversation_search_text')
    if index_work:
        reasons.append('deferred_index_maintenance')

    requires_physical_reclaim = bool(
        physical_freelist
        or expired_payload_bytes
        or mirror_payload_bytes
        or header_search_payload_bytes
    )
    command = ''
    blocked_reason = ''
    mode = 'none'
    if requires_physical_reclaim:
        mode = 'verified_copy'
        if compaction_plan.get('verified_copy_capacity_ok'):
            parts = [
                'python3 scripts/storage_deep_clean.py',
                '--offline',
            ]
            if mirror_payload_bytes:
                parts.append('--retire-legacy-conversation-mirrors')
            ttl_days = float(retention.get('ttl_days') or 1.0)
            parts.extend(('--ttl-days', f'{ttl_days:g}', '--confirm'))
            command = ' '.join(parts)
        else:
            mode = 'verified_copy_capacity_blocked'
            blocked_reason = 'insufficient_free_bytes_for_verified_copy'
    elif index_work:
        mode = 'index_only'
        command = (
            'python3 scripts/storage_deep_clean.py '
            '--offline --no-vacuum --confirm'
        )

    return {
        'recommended': bool(reasons),
        'mode': mode,
        'reasons': reasons,
        'requires_stopped_server': bool(reasons),
        'requires_physical_reclaim': requires_physical_reclaim,
        'expired_transport_payload_bytes': expired_payload_bytes,
        'conditional_legacy_mirror_payload_bytes': mirror_payload_bytes,
        'rebuildable_conversation_search_text_bytes': (
            header_search_payload_bytes),
        'recommended_command': command,
        'blocked_reason': blocked_reason,
    }


def _exact_table_measurement(
    *,
    row_count: int,
    payload_bytes: int,
    min_rowid: int | None,
    max_rowid: int | None,
) -> dict:
    """Normalize exact logical payload totals while retaining the old key."""
    rows = max(0, int(row_count or 0))
    encoded_bytes = max(0, int(payload_bytes or 0))
    minimum = int(min_rowid or 0)
    maximum = int(max_rowid or 0)
    rowid_span = maximum - minimum + 1 if rows else 0
    return {
        'measurement': 'exact_encoded_payload',
        'row_count': rows,
        'min_rowid': minimum,
        'max_rowid': maximum,
        'rowid_span': rowid_span,
        'rowid_holes': max(0, rowid_span - rows),
        'payload_bytes': encoded_bytes,
        'avg_payload_bytes': round(encoded_bytes / rows) if rows else 0,
        # Compatibility for existing report consumers. This value is exact;
        # it is no longer max(rowid) multiplied by a biased sample average.
        'estimated_bytes': encoded_bytes,
        'estimated_bytes_is_exact': True,
    }


def _measure_table(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    *,
    deadline_at: float,
) -> dict:
    """Measure one table exactly within the analyze command's shared budget."""
    if time.monotonic() >= deadline_at:
        raise TimeoutError('analysis SQL budget exhausted')
    expression = _payload_length_expression(columns)
    row = _fetch_measurement_rows(
        connection,
        'SELECT count(*), COALESCE(sum('
        + expression
        + '), 0), min(rowid), max(rowid) FROM '
        + _quote_identifier(table)
    )
    return _exact_table_measurement(
        row_count=row[0],
        payload_bytes=row[1],
        min_rowid=row[2],
        max_rowid=row[3],
    )


def _measure_storage_conversation_turns(
    connection: sqlite3.Connection,
    *,
    deadline_at: float,
) -> dict:
    """Measure Turn payload and large-codec candidates in one table scan."""
    if time.monotonic() >= deadline_at:
        raise TimeoutError('analysis SQL budget exhausted')
    row = _fetch_measurement_rows(
        connection,
        'SELECT count(*),'
        'COALESCE(sum(projection_bytes+settlement_bytes),0),'
        'min(rowid),max(rowid),'
        'COALESCE(sum(CASE WHEN projection_bytes>=? THEN 1 ELSE 0 END),0),'
        'COALESCE(sum(CASE WHEN projection_bytes>=? '
        'THEN projection_bytes ELSE 0 END),0) FROM ('
        'SELECT rowid,length(CAST(projection_json AS BLOB)) '
        'AS projection_bytes,length(CAST(settlement_json AS BLOB)) '
        'AS settlement_bytes FROM storage_conversation_turns)',
        (
            _TURN_PROJECTION_CODEC_MIN_BYTES,
            _TURN_PROJECTION_CODEC_MIN_BYTES,
        ),
    )
    measurement = _exact_table_measurement(
        row_count=row[0],
        payload_bytes=row[1],
        min_rowid=row[2],
        max_rowid=row[3],
    )
    measurement['turn_projection_codec_candidates'] = {
        'measurement': 'exact_threshold_source_payload',
        'minimum_projection_bytes': _TURN_PROJECTION_CODEC_MIN_BYTES,
        'row_count': max(0, int(row[4] or 0)),
        'projection_bytes': max(0, int(row[5] or 0)),
        'semantic_savings_require_offline_validation': True,
    }
    return measurement


def _measure_storage_records(
    connection: sqlite3.Connection,
    *,
    deadline_at: float,
) -> dict:
    """Measure record payload and large task-result candidates in one scan."""
    if time.monotonic() >= deadline_at:
        raise TimeoutError('analysis SQL budget exhausted')
    row = _fetch_measurement_rows(
        connection,
        'SELECT count(*),COALESCE(sum(payload_bytes),0),min(rowid),max(rowid),'
        'COALESCE(sum(CASE WHEN namespace=\'task_results\' '
        'AND payload_bytes>=? THEN 1 ELSE 0 END),0),'
        'COALESCE(sum(CASE WHEN namespace=\'task_results\' '
        'AND payload_bytes>=? THEN payload_bytes ELSE 0 END),0) FROM ('
        'SELECT rowid,namespace,length(CAST(value_json AS BLOB)) '
        'AS payload_bytes FROM storage_records)',
        (
            TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES,
            TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES,
        ),
    )
    measurement = _exact_table_measurement(
        row_count=row[0],
        payload_bytes=row[1],
        min_rowid=row[2],
        max_rowid=row[3],
    )
    measurement['task_result_field_codec_candidates'] = {
        'measurement': 'exact_threshold_source_payload',
        'minimum_document_bytes': TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES,
        'row_count': max(0, int(row[4] or 0)),
        'source_document_bytes': max(0, int(row[5] or 0)),
        'semantic_savings_require_offline_validation': True,
    }
    return measurement


def _measure_storage_events(
    connection: sqlite3.Connection,
    *,
    deadline_at: float,
) -> dict:
    """Measure task/project event payloads with one bounded exact breakdown."""
    if time.monotonic() >= deadline_at:
        raise TimeoutError('analysis SQL budget exhausted')
    rows = _fetch_measurement_rows(
        connection,
        'SELECT stream_kind, event_type, count(*), '
        'COALESCE(sum(length(CAST(event_json AS BLOB))), 0), '
        'min(rowid), max(rowid), min(created_at_ms), max(created_at_ms) '
        'FROM storage_events GROUP BY stream_kind, event_type '
        'ORDER BY stream_kind, event_type LIMIT ?',
        (_ANALYZE_EVENT_GROUP_LIMIT + 1,),
        all_rows=True,
    )
    groups_truncated = len(rows) > _ANALYZE_EVENT_GROUP_LIMIT
    visible_rows = rows[:_ANALYZE_EVENT_GROUP_LIMIT]
    if groups_truncated:
        measurement = _measure_table(
            connection,
            'storage_events',
            ('event_json',),
            deadline_at=deadline_at,
        )
    else:
        measurement = _exact_table_measurement(
            row_count=sum(int(row[2] or 0) for row in rows),
            payload_bytes=sum(int(row[3] or 0) for row in rows),
            min_rowid=min(
                (int(row[4]) for row in rows if row[4] is not None),
                default=0,
            ),
            max_rowid=max(
                (int(row[5]) for row in rows if row[5] is not None),
                default=0,
            ),
        )
    measurement['groups'] = [
        {
            'stream_kind': str(row[0] or ''),
            'event_type': str(row[1] or ''),
            'row_count': int(row[2] or 0),
            'payload_bytes': int(row[3] or 0),
            'avg_payload_bytes': round(
                int(row[3] or 0) / max(1, int(row[2] or 0))),
            'oldest_created_at_ms': int(row[6] or 0),
            'newest_created_at_ms': int(row[7] or 0),
        }
        for row in visible_rows
    ]
    measurement['group_limit'] = _ANALYZE_EVENT_GROUP_LIMIT
    measurement['groups_truncated'] = groups_truncated
    return measurement


def _measure_storage_conversations(
    connection: sqlite3.Connection,
    *,
    deadline_at: float,
) -> tuple[dict, dict]:
    """Measure archive payload and rebuildable search in one table scan."""
    if time.monotonic() >= deadline_at:
        raise TimeoutError('analysis SQL budget exhausted')
    row = _fetch_measurement_rows(
        connection,
        'SELECT count(*),'
        'COALESCE(sum(length(CAST(messages_json AS BLOB))),0),'
        'COALESCE(sum(length(CAST(search_text AS BLOB))),0),'
        'min(rowid),max(rowid),'
        'COALESCE(sum(CASE WHEN search_text<>\'\' THEN 1 ELSE 0 END),0) '
        'FROM storage_conversations'
    )
    message_payload_bytes = max(0, int(row[1] or 0))
    search_payload_bytes = max(0, int(row[2] or 0))
    measurement = _exact_table_measurement(
        row_count=row[0],
        payload_bytes=message_payload_bytes + search_payload_bytes,
        min_rowid=row[3],
        max_rowid=row[4],
    )
    retirement = {
        'mode': 'rebuildable_header_projection_retirement',
        'measurement': 'exact_encoded_payload',
        'measurement_complete': True,
        'candidate_rows': max(0, int(row[5] or 0)),
        'payload_bytes': search_payload_bytes,
    }
    return measurement, retirement


def _operator_managed_large_file_inventory(
    data_dir: Path,
    *,
    excluded_names: set[str],
) -> dict:
    """Bounded shallow inventory for recovery material owned outside this CLI."""
    rows = []
    scanned = 0
    capped = False
    try:
        entries = sorted(data_dir.iterdir(), key=lambda path: path.name)
    except FileNotFoundError:
        entries = []
    for path in entries:
        if scanned >= _OPERATOR_MANAGED_FILE_SCAN_LIMIT:
            capped = True
            break
        scanned += 1
        if path.name in excluded_names:
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size < _OPERATOR_MANAGED_LARGE_FILE_MIN_BYTES:
            continue
        allocated = int(getattr(stat, 'st_blocks', 0) or 0) * 512
        if allocated <= 0:
            allocated = int(stat.st_size)
        rows.append({
            'name': path.name,
            'path': str(path),
            'logical_bytes': int(stat.st_size),
            'allocated_bytes': allocated,
            'modified_at_unix_s': round(float(stat.st_mtime), 3),
            'lifecycle': 'owner_signoff_required',
        })
    return {
        'minimum_logical_bytes': _OPERATOR_MANAGED_LARGE_FILE_MIN_BYTES,
        'entry_scan_limit': _OPERATOR_MANAGED_FILE_SCAN_LIMIT,
        'scanned_entries': scanned,
        'capped': capped,
        'count': len(rows),
        'total_logical_bytes': sum(row['logical_bytes'] for row in rows),
        'total_allocated_bytes': sum(row['allocated_bytes'] for row in rows),
        'artifacts': rows,
    }


def _operator_managed_directory_inventory(data_dir: Path) -> dict:
    """Inventory known recovery directories one bounded level deep.

    These directories have distinct external/retired owners and may contain
    valid recovery points, so analysis reports them but never synthesizes a
    deletion command. Symlinks and nested directories remain outside the
    ownership proof and are ignored. Both the root discovery and each child
    scan have independent hard bounds.
    """
    selected: dict[str, tuple[Path, str]] = {}
    for name, lifecycle in _OPERATOR_MANAGED_DIRECTORY_LIFECYCLES.items():
        path = data_dir / name
        try:
            if path.is_dir() and not path.is_symlink():
                selected[name] = (path, lifecycle)
        except OSError:
            continue

    root_scanned = 0
    root_capped = False
    try:
        root_entries = os.scandir(data_dir)
    except OSError:
        root_entries = None
    if root_entries is not None:
        with root_entries:
            for entry in root_entries:
                if root_scanned >= _OPERATOR_MANAGED_DIRECTORY_SCAN_LIMIT:
                    root_capped = True
                    break
                root_scanned += 1
                lifecycle = next((
                    owner
                    for prefix, owner
                    in _OPERATOR_MANAGED_DIRECTORY_PREFIX_LIFECYCLES.items()
                    if entry.name.startswith(prefix)
                ), '')
                if not lifecycle or entry.name in selected:
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        selected[entry.name] = (
                            data_dir / entry.name, lifecycle)
                except OSError:
                    continue

    directories = []
    for name, (directory, lifecycle) in sorted(selected.items()):
        artifacts = []
        scanned = 0
        capped = False
        try:
            entries = os.scandir(directory)
        except OSError:
            entries = None
        if entries is not None:
            with entries:
                for entry in entries:
                    if scanned >= _OPERATOR_MANAGED_DIRECTORY_ENTRY_SCAN_LIMIT:
                        capped = True
                        break
                    scanned += 1
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        status = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    logical = max(0, int(status.st_size))
                    allocated = max(
                        0, int(getattr(status, 'st_blocks', 0) or 0) * 512)
                    if allocated <= 0:
                        allocated = logical
                    artifacts.append({
                        'name': entry.name,
                        'path': str(directory / entry.name),
                        'logical_bytes': logical,
                        'allocated_bytes': allocated,
                        'modified_at_unix_s': round(
                            float(status.st_mtime), 3),
                        'hard_link_count': max(
                            1, int(getattr(status, 'st_nlink', 1) or 1)),
                    })
        artifacts.sort(
            key=lambda row: (row['allocated_bytes'], row['name']),
            reverse=True,
        )
        directories.append({
            'name': name,
            'path': str(directory),
            'lifecycle': lifecycle,
            'entry_scan_limit': _OPERATOR_MANAGED_DIRECTORY_ENTRY_SCAN_LIMIT,
            'scanned_entries': scanned,
            'capped': capped,
            'count': len(artifacts),
            'total_logical_bytes': sum(
                row['logical_bytes'] for row in artifacts),
            'total_allocated_bytes': sum(
                row['allocated_bytes'] for row in artifacts),
            'artifacts': artifacts,
        })
    return {
        'root_entry_scan_limit': _OPERATOR_MANAGED_DIRECTORY_SCAN_LIMIT,
        'root_scanned_entries': root_scanned,
        'root_capped': root_capped,
        'directory_count': len(directories),
        'count': sum(row['count'] for row in directories),
        'total_logical_bytes': sum(
            row['total_logical_bytes'] for row in directories),
        'total_allocated_bytes': sum(
            row['total_allocated_bytes'] for row in directories),
        'directories': directories,
    }


def analyze(project_root: Path, *, ttl_days: float = 1.0) -> dict:
    ttl_days = float(ttl_days)
    if not math.isfinite(ttl_days) or ttl_days <= 0:
        raise ValueError('ttl_days must be a positive finite number')
    path = project_root / 'data' / 'tofu.db'
    header = _parse_header(path)
    capacity = copy_capacity_requirement(header['live_bytes'])
    available = int(shutil.disk_usage(path.parent).free)
    report = {
        'authority': str(path),
        'header': header,
        'compaction_plan': {
            **capacity,
            'available_free_bytes': available,
            'verified_copy_capacity_ok': (
                available >= capacity['required_free_bytes']),
            'offline_compaction_recommended': requires_offline_compaction(
                header['freelist_pages'], header['page_count']),
        },
        'tables': {},
    }
    rollback_inventory = rollback_artifact_inventory(path.parent)
    for artifact in rollback_inventory['artifacts']:
        artifact['retire_command'] = (
            'python3 scripts/storage_deep_clean.py --retire-rollback '
            f"{artifact['name']} --confirm")
    sqlite_backups = verified_backup_inventory(path.parent / 'backups')
    excluded_names = set(_LIVE_SQLITE_FILENAMES)
    excluded_names.update(
        artifact['name'] for artifact in rollback_inventory['artifacts'])
    operator_managed = _operator_managed_large_file_inventory(
        path.parent, excluded_names=excluded_names)
    operator_directories = _operator_managed_directory_inventory(path.parent)
    report['recovery_artifacts'] = {
        'deep_clean_rollbacks': rollback_inventory,
        'verified_sqlite_backups': sqlite_backups,
        'operator_managed_large_files': operator_managed,
        'operator_managed_directories': operator_directories,
        'total_allocated_bytes': (
            rollback_inventory['total_allocated_bytes']
            + sqlite_backups['total_allocated_bytes']
            + operator_managed['total_allocated_bytes']
            + operator_directories['total_allocated_bytes']
        ),
    }
    connection = _open_readonly(path)
    table_scan_started = 0.0
    table_scan_deadline = 0.0
    table_scan_completed = 0
    table_scan_timed_out = False
    try:
        existing = _SQLITE_TOOLING.sqlite_schema_names(connection, 'table')
        existing_indexes = _SQLITE_TOOLING.sqlite_schema_names(
            connection, 'index')
        applicable_indexes = [
            index_name
            for statement in deferred_index_statements('sqlite')
            for index_name, table_name in (
                _deferred_index_identity(statement),)
            if table_name in existing
        ]
        missing_indexes = [
            name for name in applicable_indexes if name not in existing_indexes
        ]
        obsolete_indexes = sorted(
            OBSOLETE_DEFERRED_INDEX_NAMES & existing_indexes)
        report['compaction_plan']['missing_deferred_indexes'] = missing_indexes
        report['compaction_plan']['obsolete_deferred_indexes'] = obsolete_indexes
        report['compaction_plan']['offline_index_maintenance_required'] = bool(
            missing_indexes or obsolete_indexes)
        if TASK_EVENT_RETENTION_INDEX_NAMES <= existing_indexes:
            retention_mode = 'tier_partial_v2'
        elif LEGACY_TASK_EVENT_RETENTION_INDEX_NAME in existing_indexes:
            retention_mode = 'legacy_exact_type'
        else:
            retention_mode = 'disabled_missing_index'
        report['compaction_plan']['online_event_retention'] = {
            'mode': retention_mode,
            'legacy_event_type_limit': (
                LEGACY_TASK_EVENT_RETENTION_TYPE_LIMIT
                if retention_mode == 'legacy_exact_type'
                else 0
            ),
        }
        mirror_table_presence = {
            table: table in existing
            for table in sorted(_LEGACY_CONVERSATION_MIRROR_TABLES)
        }
        mirror_available = all(mirror_table_presence.values())
        report['compaction_plan']['legacy_conversation_mirror_retirement'] = {
            'available': mirror_available,
            'table_presence': mirror_table_presence,
            'mode': (
                'explicit_offline_semantic_verification'
                if mirror_available else 'not_applicable'
            ),
            'command': (
                'python3 scripts/storage_deep_clean.py --offline '
                '--retire-legacy-conversation-mirrors --confirm'
                if mirror_available else ''
            ),
            'measured_payload_bytes': 0,
            'measurement_complete': False,
        }
        search_retirement_available = _table_has_columns(
            connection,
            'storage_conversations',
            {'search_text'},
        )
        report['compaction_plan']['conversation_search_text_retirement'] = {
            'available': search_retirement_available,
            'mode': (
                'rebuildable_header_projection_retirement'
                if search_retirement_available else 'not_applicable'
            ),
            'measurement': 'not_completed',
            'measurement_complete': False,
            'candidate_rows': 0,
            'payload_bytes': 0,
        }
        table_scan_started = time.monotonic()
        table_scan_deadline = (
            table_scan_started + _ANALYZE_SQL_BUDGET_SECONDS)
        connection.set_progress_handler(
            lambda: int(time.monotonic() >= table_scan_deadline),
            _ANALYZE_SQL_PROGRESS_STEPS,
        )
        search_retirement = report['compaction_plan'][
            'conversation_search_text_retirement']
        for table, columns in _ANALYZE_TABLES:
            if table not in existing:
                continue
            if table_scan_timed_out or time.monotonic() >= table_scan_deadline:
                table_scan_timed_out = True
                report['tables'][table] = {
                    'error': 'analysis_budget_exhausted',
                    'measurement': 'not_completed',
                }
                continue
            try:
                if table == 'storage_events':
                    measurement = _measure_storage_events(
                        connection, deadline_at=table_scan_deadline)
                elif table == 'storage_conversation_turns':
                    measurement = _measure_storage_conversation_turns(
                        connection, deadline_at=table_scan_deadline)
                elif table == 'storage_records':
                    measurement = _measure_storage_records(
                        connection, deadline_at=table_scan_deadline)
                elif table == 'storage_conversations':
                    measurement, retirement = _measure_storage_conversations(
                        connection, deadline_at=table_scan_deadline)
                    search_retirement.update(retirement)
                else:
                    measurement = _measure_table(
                        connection,
                        table,
                        columns,
                        deadline_at=table_scan_deadline,
                    )
                report['tables'][table] = measurement
                table_scan_completed += 1
            except TimeoutError:
                table_scan_timed_out = True
                report['tables'][table] = {
                    'error': 'analysis_budget_exhausted',
                    'measurement': 'not_completed',
                }
            except sqlite3.OperationalError as exc:
                interrupted = (
                    'interrupted' in str(exc).lower()
                    and time.monotonic() >= table_scan_deadline
                )
                if interrupted:
                    table_scan_timed_out = True
                    report['tables'][table] = {
                        'error': 'analysis_budget_exhausted',
                        'measurement': 'not_completed',
                    }
                else:
                    report['tables'][table] = {'error': str(exc)}
            except sqlite3.Error as exc:
                report['tables'][table] = {'error': str(exc)}
        if (
            search_retirement_available
            and not search_retirement['measurement_complete']
        ):
            conversation_measurement = report['tables'].get(
                'storage_conversations') or {}
            if conversation_measurement.get('error'):
                search_retirement['error'] = conversation_measurement['error']
        legacy_retention = (
            _SQLITE_TOOLING.measure_sqlite_transport_retention(
                connection,
                existing_tables=existing,
                ttl_days=ttl_days,
                now_ms=int(time.time() * 1000),
                deadline_at=table_scan_deadline,
            )
        )
        report['compaction_plan'][
            'transport_retention'] = legacy_retention
        table_scan_timed_out = bool(
            table_scan_timed_out or legacy_retention['timed_out'])
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()
    mirror_plan = report['compaction_plan'][
        'legacy_conversation_mirror_retirement']
    mirror_measurements = [
        report['tables'].get(table)
        for table in ('conversations', 'conversation_messages',
                      'conversation_turns')
    ]
    mirror_plan['measurement_complete'] = bool(
        mirror_plan['available']
        and all(
            isinstance(measurement, dict)
            and measurement.get('measurement') == 'exact_encoded_payload'
            for measurement in mirror_measurements
        )
    )
    if mirror_plan['measurement_complete']:
        mirror_plan['measured_payload_bytes'] = sum(
            int(measurement['payload_bytes'])
            for measurement in mirror_measurements
        )
    report['compaction_plan']['offline_maintenance'] = (
        _offline_maintenance_plan(report['compaction_plan']))
    legacy_retention = report['compaction_plan']['transport_retention']
    report['table_scan'] = {
        'method': 'exact_encoded_payload',
        'budget_seconds': _ANALYZE_SQL_BUDGET_SECONDS,
        'progress_steps': _ANALYZE_SQL_PROGRESS_STEPS,
        'completed_tables': table_scan_completed,
        'completed_retention_sources': int(
            legacy_retention['completed_sources']),
        'timed_out': table_scan_timed_out,
        'elapsed_seconds': round(
            max(0.0, time.monotonic() - table_scan_started), 3)
            if table_scan_started else 0.0,
    }
    return report


def _delete_eligible_transport_rows(
    connection,
    cutoff_ms: int,
    *,
    db_path: Path,
    lease: ProjectLease,
) -> int:
    """Bounded chunked delete; WAL is truncated between chunks.

    Offline the writer watchdog does not apply, but an unbounded single
    transaction would spill the whole deletion into one WAL (hundreds of GiB
    of extra churn).  Chunks of attempts keep every transaction small.  The
    scan walks a (settled_at, attempt_id) keyset cursor: an attempt whose
    events are already gone must never be re-selected (a plain LIMIT batch
    would return it forever — the attempt row itself is authority and stays).
    """
    total = 0
    last_settled = -1
    last_attempt = ''
    references_available = (
        _SQLITE_TOOLING.sqlite_conversation_change_references_available(
            connection
        )
    )
    while True:
        query = _SQLITE_TOOLING.sqlite_transport_retention_candidate_queries(
            attempt_cutoff_ms=cutoff_ms,
            now_ms=cutoff_ms,
            aggregate=False,
            last_settled_ms=last_settled,
            last_attempt_id=last_attempt,
            protect_conversation_change_references=references_available,
        )['storage_attempt_events']
        batch = connection.execute(
            str(query['sql']), tuple(query['params'])).fetchall()
        if not batch:
            return total
        def _delete_batch(conn) -> int:
            # Replay-pure: outer state advances only after the owned write.
            deleted = 0
            for attempt_id, _settled_at in batch:
                cursor = conn.execute(
                    'DELETE FROM storage_attempt_events WHERE attempt_id=?',
                    (attempt_id,))
                deleted += max(0, cursor.rowcount)
            return deleted

        total += _SQLITE_TOOLING.run_sqlite_tool_write(
            connection, db_path=db_path,
            lease=lease,
            purpose='storage deep clean transport retention',
            operation=_delete_batch)
        last_settled = int(batch[-1][1])
        last_attempt = str(batch[-1][0])
        # Keep the WAL bounded: checkpoint after every chunk so the window's
        # write amplification stays near the live-data size, not the bloat.
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')


def _table_has_columns(
    connection: sqlite3.Connection,
    table: str,
    required: set[str],
) -> bool:
    if table not in _SQLITE_TOOLING.sqlite_schema_names(connection, 'table'):
        return False
    columns = {
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    }
    return required <= columns


def _bounded_payload_batch(
    rows,
    *,
    payload_budget_bytes: int,
    purpose: str,
) -> tuple[list[int], int]:
    """Select one non-empty row prefix within an encoded-payload budget."""
    rowids: list[int] = []
    payload_bytes = 0
    for row in rows:
        rowid = int(row[0])
        size = max(0, int(row[1] or 0))
        if size > payload_budget_bytes:
            raise RuntimeError(
                f'{purpose} row exceeds the offline cleanup byte budget')
        if rowids and payload_bytes + size > payload_budget_bytes:
            break
        rowids.append(rowid)
        payload_bytes += size
        if payload_bytes >= payload_budget_bytes:
            break
    return rowids, payload_bytes


def _bounded_legacy_transport_batch(rows) -> tuple[list[int], int]:
    return _bounded_payload_batch(
        rows,
        payload_budget_bytes=_LEGACY_TRANSPORT_BATCH_PAYLOAD_BYTES,
        purpose='legacy transport',
    )


def _sqlite_payload_bytes(value) -> bytes:
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode('utf-8')
    raise RuntimeError('task-event payload has an invalid SQLite value type')


def _drain_legacy_transport_rows(
    connection: sqlite3.Connection,
    *,
    table: str,
    candidate_sql: str,
    candidate_params: tuple,
    db_path: Path,
    lease: ProjectLease,
    purpose: str,
) -> dict:
    """Delete candidate rowids in 128-MiB payload / 900-row transactions."""
    if table not in _LEGACY_TRANSPORT_TABLES:
        raise RuntimeError(f'unsupported legacy transport table: {table}')
    total_rows = 0
    total_payload_bytes = 0
    batches = 0
    max_batch_payload_bytes = 0
    while True:
        candidates = connection.execute(
            candidate_sql,
            (*candidate_params, _LEGACY_TRANSPORT_SELECT_ROWS),
        ).fetchall()
        rowids, payload_bytes = _bounded_legacy_transport_batch(candidates)
        if not rowids:
            return {
                'deleted_rows': total_rows,
                'deleted_payload_bytes': total_payload_bytes,
                'batches': batches,
                'max_batch_payload_bytes': max_batch_payload_bytes,
                'batch_payload_budget_bytes': (
                    _LEGACY_TRANSPORT_BATCH_PAYLOAD_BYTES),
                'selection_row_limit': _LEGACY_TRANSPORT_SELECT_ROWS,
            }
        placeholders = ','.join('?' for _ in rowids)

        def _delete_batch(conn) -> int:
            cursor = conn.execute(
                f'DELETE FROM "{table}" WHERE rowid IN ({placeholders})',
                tuple(rowids),
            )
            deleted = max(0, int(cursor.rowcount))
            if deleted != len(rowids):
                raise RuntimeError(
                    f'{purpose} selected {len(rowids)} rows but deleted '
                    f'{deleted}')
            return deleted

        deleted = _SQLITE_TOOLING.run_sqlite_tool_write(
            connection,
            db_path=db_path,
            lease=lease,
            purpose=purpose,
            operation=_delete_batch,
        )
        total_rows += int(deleted)
        total_payload_bytes += payload_bytes
        batches += 1
        max_batch_payload_bytes = max(max_batch_payload_bytes, payload_bytes)
        _SQLITE_TOOLING.checkpoint_sqlite_wal(connection)


def _maintain_task_event_rows(
    connection: sqlite3.Connection,
    *,
    now_ms: int,
    db_path: Path,
    lease: ProjectLease,
) -> dict:
    """Bounded TTL, type recovery, usage projection, and codec backfill."""
    required_columns = {
        'stream_kind', 'event_type', 'event_kind', 'event_json',
        'created_at_ms',
    }
    if not _table_has_columns(connection, 'storage_events', required_columns):
        return {'available': False, 'mode': 'missing_storage_events'}

    stream_cutoff = now_ms - TASK_EVENT_STREAMING_RETENTION_MS
    structural_cutoff = now_ms - TASK_EVENT_STRUCTURAL_RETENTION_MS
    connection.execute(
        'CREATE TEMP TABLE IF NOT EXISTS '
        '_deep_clean_task_event_deletes(rowid INTEGER PRIMARY KEY)')
    cursor_rowid = 0
    report = {
        'available': True,
        'mode': 'bounded_ttl_type_recovery_usage_projection_codec_backfill',
        'scanned_rows': 0,
        'scanned_payload_bytes': 0,
        'scan_batches': 0,
        'write_batches': 0,
        'deleted_rows': 0,
        'deleted_streaming_rows': 0,
        'deleted_structural_rows': 0,
        'deleted_payload_bytes': 0,
        'reclassified_blank_rows': 0,
        'opaque_blank_rows': 0,
        'invalid_blank_rows': 0,
        'updated_rows': 0,
        'compressed_rows': 0,
        'compression_saved_bytes': 0,
        'already_compressed_rows': 0,
        'usage_projection_candidates': 0,
        'usage_projected_rows': 0,
        'usage_projection_input_bytes': 0,
        'usage_projection_output_bytes': 0,
        'usage_projection_removed_bytes': 0,
        'invalid_usage_rows': 0,
        'non_object_usage_rows': 0,
        'max_batch_payload_bytes': 0,
        'batch_payload_budget_bytes': (
            _TASK_EVENT_MAINTENANCE_BATCH_PAYLOAD_BYTES),
        'selection_row_limit': _TASK_EVENT_MAINTENANCE_SELECT_ROWS,
    }
    while True:
        candidates = connection.execute(
            _TASK_EVENT_MAINTENANCE_PAGE_SQL,
            (cursor_rowid, 'task', _TASK_EVENT_MAINTENANCE_SELECT_ROWS),
        ).fetchall()
        bounded_rows = [
            (int(row['rowid']), int(row['payload_bytes'] or 0))
            for row in candidates
        ]
        rowids, batch_payload_bytes = _bounded_payload_batch(
            bounded_rows,
            payload_budget_bytes=_TASK_EVENT_MAINTENANCE_BATCH_PAYLOAD_BYTES,
            purpose='task event',
        )
        if not rowids:
            break
        metadata_rows = candidates[:len(rowids)]
        cursor_rowid = rowids[-1]
        report['scanned_rows'] += len(metadata_rows)
        report['scanned_payload_bytes'] += batch_payload_bytes
        report['scan_batches'] += 1
        report['max_batch_payload_bytes'] = max(
            report['max_batch_payload_bytes'], batch_payload_bytes)

        payload_rows = connection.execute(
            'SELECT rowid, event_json FROM storage_events NOT INDEXED '
            'WHERE rowid BETWEEN ? AND ? AND stream_kind = ? AND ('
            'event_type = \'\' OR event_type = ? '
            'OR length(CAST(event_json AS BLOB)) >= ?) '
            'ORDER BY rowid',
            (
                rowids[0], rowids[-1], 'task',
                _TASK_EVENT_USAGE_PROJECTION_TYPE,
                TASK_EVENT_COMPRESSION_MIN_BYTES,
            ),
        ).fetchall()
        payloads = {
            int(row['rowid']): _sqlite_payload_bytes(row['event_json'])
            for row in payload_rows
        }
        delete_rowids: list[int] = []
        updates: list[tuple[str, str, bytes, int]] = []

        for row in metadata_rows:
            rowid = int(row['rowid'])
            current_type = str(row['event_type'] or '')
            current_kind = str(row['event_kind'] or '')
            created_at_ms = int(row['created_at_ms'] or 0)
            payload_bytes = int(row['payload_bytes'] or 0)
            recovered_type = current_type
            recovered_kind = current_kind
            stored_payload = payloads.get(rowid)
            decoded_payload: bytes | None = None
            decoded_event = None
            decoded_event_loaded = False

            if not current_type:
                if stored_payload is None:
                    raise RuntimeError(
                        'blank task-event row was omitted from payload page')
                decoded_payload = decode_task_event_payload(stored_payload)
                try:
                    decoded_event = orjson.loads(decoded_payload)
                    decoded_event_loaded = True
                except orjson.JSONDecodeError:
                    decoded_event = None
                    decoded_event_loaded = True
                    report['invalid_blank_rows'] += 1
                if isinstance(decoded_event, dict):
                    recovered_type = str(
                        decoded_event.get('type') or '')[:128]
                    recovered_kind = str(
                        decoded_event.get('kind') or '')[:128]
                if not recovered_type:
                    report['opaque_blank_rows'] += 1

            is_structural = (
                not recovered_type
                or recovered_type in STRUCTURAL_EVENT_TYPES
            )
            cutoff = structural_cutoff if is_structural else stream_cutoff
            if created_at_ms < cutoff:
                delete_rowids.append(rowid)
                report['deleted_rows'] += 1
                report['deleted_payload_bytes'] += payload_bytes
                tier_key = (
                    'deleted_structural_rows'
                    if is_structural
                    else 'deleted_streaming_rows'
                )
                report[tier_key] += 1
                continue

            if (
                decoded_payload is None
                and (
                    payload_bytes >= TASK_EVENT_COMPRESSION_MIN_BYTES
                    or recovered_type == _TASK_EVENT_USAGE_PROJECTION_TYPE
                )
            ):
                if stored_payload is None:
                    raise RuntimeError(
                        'selected task-event row was omitted from payload page')
                decoded_payload = decode_task_event_payload(stored_payload)
            if decoded_payload is None:
                if recovered_type != current_type or recovered_kind != current_kind:
                    stored_payload = payloads.get(rowid)
                    if stored_payload is None:
                        raise RuntimeError(
                            'reclassified task event has no payload bytes')
                    decoded_payload = decode_task_event_payload(stored_payload)
                else:
                    continue

            if recovered_type == _TASK_EVENT_USAGE_PROJECTION_TYPE:
                report['usage_projection_candidates'] += 1
                if not decoded_event_loaded:
                    try:
                        decoded_event = orjson.loads(decoded_payload)
                    except orjson.JSONDecodeError:
                        decoded_event = None
                        report['invalid_usage_rows'] += 1
                    decoded_event_loaded = True
                if decoded_event is not None and not isinstance(
                        decoded_event, dict):
                    report['non_object_usage_rows'] += 1
                if isinstance(decoded_event, dict):
                    projected_event = project_event_usage_for_storage(
                        decoded_event)
                    if projected_event is not decoded_event:
                        projected_payload = orjson.dumps(projected_event)
                        if projected_payload != decoded_payload:
                            projection_input_bytes = len(decoded_payload)
                            projection_output_bytes = len(projected_payload)
                            decoded_payload = projected_payload
                            report['usage_projected_rows'] += 1
                            report['usage_projection_input_bytes'] += (
                                projection_input_bytes)
                            report['usage_projection_output_bytes'] += (
                                projection_output_bytes)
                            report['usage_projection_removed_bytes'] += max(
                                0,
                                projection_input_bytes
                                - projection_output_bytes,
                            )

            encoded_payload = encode_task_event_payload(decoded_payload)
            if stored_payload is None:
                raise RuntimeError('task-event codec update has no stored payload')
            was_compressed = stored_payload.startswith(COMPRESSED_TASK_EVENT_MAGIC)
            is_compressed = encoded_payload.startswith(COMPRESSED_TASK_EVENT_MAGIC)
            if was_compressed:
                report['already_compressed_rows'] += 1
            if is_compressed and not was_compressed:
                report['compressed_rows'] += 1
                report['compression_saved_bytes'] += (
                    len(stored_payload) - len(encoded_payload))
            if (
                recovered_type != current_type
                or recovered_kind != current_kind
                or encoded_payload != stored_payload
            ):
                updates.append((
                    recovered_type, recovered_kind, encoded_payload, rowid))
                report['updated_rows'] += 1
                if not current_type and recovered_type:
                    report['reclassified_blank_rows'] += 1

        if not delete_rowids and not updates:
            continue

        def _write_batch(conn) -> tuple[int, int]:
            conn.execute('DELETE FROM _deep_clean_task_event_deletes')
            deleted = 0
            if delete_rowids:
                conn.executemany(
                    'INSERT INTO _deep_clean_task_event_deletes(rowid) VALUES (?)',
                    ((rowid,) for rowid in delete_rowids),
                )
                delete_cursor = conn.execute(
                    'DELETE FROM storage_events WHERE rowid IN ('
                    'SELECT rowid FROM _deep_clean_task_event_deletes)')
                deleted = max(0, int(delete_cursor.rowcount))
                if deleted != len(delete_rowids):
                    raise RuntimeError(
                        'task-event cleanup selected '
                        f'{len(delete_rowids)} rows but deleted {deleted}')
            updated = 0
            if updates:
                update_cursor = conn.executemany(
                    'UPDATE storage_events SET event_type=?, event_kind=?, '
                    'event_json=? WHERE rowid=?',
                    updates,
                )
                updated = max(0, int(update_cursor.rowcount))
                if updated != len(updates):
                    raise RuntimeError(
                        'task-event cleanup selected '
                        f'{len(updates)} updates but applied {updated}')
            return deleted, updated

        _SQLITE_TOOLING.run_sqlite_tool_write(
            connection,
            db_path=db_path,
            lease=lease,
            purpose='task event retention and codec maintenance',
            operation=_write_batch,
        )
        report['write_batches'] += 1
        _SQLITE_TOOLING.checkpoint_sqlite_wal(connection)

    report['retained_rows'] = int(connection.execute(
        'SELECT count(*) FROM storage_events WHERE stream_kind=?',
        ('task',),
    ).fetchone()[0])
    report['retained_blank_rows'] = int(connection.execute(
        'SELECT count(*) FROM storage_events '
        'WHERE stream_kind=? AND event_type=\'\'',
        ('task',),
    ).fetchone()[0])
    return report


def _delete_expired_legacy_transport_rows(
    connection: sqlite3.Connection,
    *,
    attempt_cutoff_ms: int,
    now_ms: int,
    db_path: Path,
    lease: ProjectLease,
) -> dict:
    """Reclaim retired transport tables without deleting their latest state."""
    report: dict[str, object] = {'mode': 'bounded_expired_transport'}
    queries = _SQLITE_TOOLING.sqlite_transport_retention_candidate_queries(
        attempt_cutoff_ms=attempt_cutoff_ms,
        now_ms=now_ms,
        aggregate=False,
    )
    if _table_has_columns(
            connection, 'attempt_events',
            {'attempt_id', 'seq', 'payload', 'created_at'}):
        attempt_query = queries['attempt_events']
        attempt = _drain_legacy_transport_rows(
            connection,
            table='attempt_events',
            candidate_sql=str(attempt_query['sql']),
            candidate_params=tuple(attempt_query['params']),
            db_path=db_path,
            lease=lease,
            purpose='legacy attempt transport retention',
        )
        attempt['retained_rows'] = int(connection.execute(
            'SELECT count(*) FROM attempt_events').fetchone()[0])
        report['attempt_events'] = attempt
    else:
        report['attempt_events'] = {'available': False, 'deleted_rows': 0}

    if _table_has_columns(
            connection, 'task_events', {'type', 'payload', 'ts_ms'}):
        streaming_query = queries['task_events_streaming']
        streaming = _drain_legacy_transport_rows(
            connection,
            table='task_events',
            candidate_sql=str(streaming_query['sql']),
            candidate_params=tuple(streaming_query['params']),
            db_path=db_path,
            lease=lease,
            purpose='legacy streaming task-event retention',
        )
        structural_query = queries['task_events_structural']
        structural = _drain_legacy_transport_rows(
            connection,
            table='task_events',
            candidate_sql=str(structural_query['sql']),
            candidate_params=tuple(structural_query['params']),
            db_path=db_path,
            lease=lease,
            purpose='legacy structural task-event retention',
        )
        report['task_events'] = {
            'streaming': streaming,
            'structural': structural,
            'retained_rows': int(connection.execute(
                'SELECT count(*) FROM task_events').fetchone()[0]),
        }
    else:
        report['task_events'] = {'available': False, 'deleted_rows': 0}
    return report


def _compact_archived_message_document(
    raw: bytes,
) -> _ArchivedMessageCompaction:
    """Return a verified compact archive while releasing parsed state locally."""
    stored_messages = orjson.loads(raw)
    already_encoded = (
        isinstance(stored_messages, list)
        and any(
            isinstance(message, dict)
            and (
                STORAGE_PROJECTION_CODEC_KEY in message
                or ARCHIVED_MESSAGE_CODEC_KEY in message
            )
            for message in stored_messages
        )
    )
    public_messages = decode_archived_message_sequence_from_storage(
        stored_messages
    )
    del stored_messages
    public_bytes = orjson.dumps(
        public_messages, option=orjson.OPT_SORT_KEYS)
    encoding = encode_archived_message_sequence_with_metrics(public_messages)
    del public_messages
    encoded = encoding.stored_document
    hydrated = decode_archived_message_sequence_from_storage(
        orjson.loads(encoded)
    )
    if orjson.dumps(hydrated, option=orjson.OPT_SORT_KEYS) != public_bytes:
        raise ProjectionCodecError('archive projection round-trip mismatched')
    return _ArchivedMessageCompaction(
        stored_document=encoded,
        already_encoded=already_encoded,
        projection_encoded_messages=encoding.projection_encoded_messages,
        compressed_messages=encoding.compressed_messages,
        public_document_bytes=len(public_bytes),
        projected_document_bytes=encoding.projected_document_bytes,
    )


def _compact_turn_projection_document(raw: bytes) -> tuple[bytes, bool]:
    """Apply the production projection codec and prove exact public parity."""
    stored_projection = orjson.loads(raw)
    already_encoded = (
        isinstance(stored_projection, dict)
        and STORAGE_PROJECTION_CODEC_KEY in stored_projection
    )
    public_projection = decode_projection_from_storage(stored_projection)
    if not isinstance(public_projection, dict):
        raise ProjectionCodecError('stored Turn projection is not an object')
    del stored_projection
    public_document = orjson.dumps(
        public_projection, option=orjson.OPT_SORT_KEYS)
    encoded_document = orjson.dumps(
        encode_projection_for_storage(public_projection),
        option=orjson.OPT_SORT_KEYS,
    )
    del public_projection
    hydrated = decode_projection_from_storage(orjson.loads(encoded_document))
    if orjson.dumps(hydrated, option=orjson.OPT_SORT_KEYS) != public_document:
        raise ProjectionCodecError('Turn projection round-trip mismatched')
    return encoded_document, already_encoded


def _turn_projection_chain_guards(columns: set[str]) -> list[str]:
    """Return conservative predicates for every installed live-head column."""
    guards = []
    for column in (
        'projection_checkpoint_revision',
        'projection_materialized_revision',
    ):
        if column in columns:
            guards.append(f'{column} IS NULL')
    for column in ('projection_patch_count', 'projection_patch_bytes'):
        if column in columns:
            guards.append(f'{column}=0')
    return guards


def _maintain_turn_projection_rows(
    connection: sqlite3.Connection,
    *,
    db_path: Path,
    lease: ProjectLease,
) -> dict:
    """Backfill the existing lossless codec on inactive large Turn rows."""
    table = 'storage_conversation_turns'
    required = {
        'turn_id', 'conversation_id', 'user_id', 'projection_revision',
        'projection_json',
    }
    if table not in _SQLITE_TOOLING.sqlite_schema_names(connection, 'table'):
        return {'mode': 'unsupported_schema', 'updated_rows': 0}
    columns = {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }
    if not required <= columns:
        return {'mode': 'unsupported_schema', 'updated_rows': 0}
    chain_guards = _turn_projection_chain_guards(columns)
    guard_sql = ''.join(f' AND {guard}' for guard in chain_guards)
    report = {
        'mode': 'lossless_existing_turn_projection_codec',
        'minimum_projection_bytes': _TURN_PROJECTION_CODEC_MIN_BYTES,
        'scanned_rows': 0,
        'scanned_payload_bytes': 0,
        'already_encoded_rows': 0,
        'unchanged_rows': 0,
        'invalid_rows': 0,
        'oversize_rows': 0,
        'updated_rows': 0,
        'updated_input_bytes': 0,
        'updated_stored_bytes': 0,
        'saved_bytes': 0,
        'write_batches': 0,
        'max_page_payload_bytes': 0,
        'selection_row_limit': _TURN_PROJECTION_SELECT_ROWS,
        'page_payload_budget_bytes': _TURN_PROJECTION_PAGE_PAYLOAD_BYTES,
        'document_budget_bytes': _TURN_PROJECTION_DOCUMENT_BYTES,
        'chain_guard_columns': [
            guard.split(' ', 1)[0].split('=', 1)[0]
            for guard in chain_guards
        ],
    }
    last_turn_id = ''
    while True:
        candidates = connection.execute(
            'SELECT turn_id,conversation_id,user_id,projection_revision,'
            'length(CAST(projection_json AS BLOB)) '
            f'FROM {table} WHERE turn_id>? '
            'AND length(CAST(projection_json AS BLOB))>=?'
            f'{guard_sql} ORDER BY turn_id LIMIT ?',
            (
                last_turn_id,
                _TURN_PROJECTION_CODEC_MIN_BYTES,
                _TURN_PROJECTION_SELECT_ROWS,
            ),
        ).fetchall()
        if not candidates:
            break
        selected: list[_TurnProjectionCandidate] = []
        page_payload_bytes = 0
        for row in candidates:
            candidate = _TurnProjectionCandidate(
                turn_id=str(row[0]),
                conversation_id=str(row[1]),
                user_id=int(row[2]),
                projection_revision=int(row[3]),
                projection_bytes=max(0, int(row[4] or 0)),
            )
            if candidate.projection_bytes > _TURN_PROJECTION_DOCUMENT_BYTES:
                last_turn_id = candidate.turn_id
                report['oversize_rows'] += 1
                continue
            if (
                selected
                and page_payload_bytes + candidate.projection_bytes
                > _TURN_PROJECTION_PAGE_PAYLOAD_BYTES
            ):
                break
            selected.append(candidate)
            page_payload_bytes += candidate.projection_bytes
            last_turn_id = candidate.turn_id
            if page_payload_bytes >= _TURN_PROJECTION_PAGE_PAYLOAD_BYTES:
                break
        if not selected:
            continue
        report['scanned_rows'] += len(selected)
        report['scanned_payload_bytes'] += page_payload_bytes
        report['max_page_payload_bytes'] = max(
            report['max_page_payload_bytes'], page_payload_bytes)
        updates: list[_TurnProjectionUpdate] = []
        for candidate in selected:
            row = connection.execute(
                'SELECT CAST(projection_json AS BLOB) '
                f'FROM {table} WHERE turn_id=? AND conversation_id=? '
                'AND user_id=? AND projection_revision=?'
                f'{guard_sql}',
                (
                    candidate.turn_id,
                    candidate.conversation_id,
                    candidate.user_id,
                    candidate.projection_revision,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    'Turn projection disappeared during maintenance')
            raw = _sqlite_payload_bytes(row[0])
            if len(raw) != candidate.projection_bytes:
                raise RuntimeError(
                    'Turn projection length changed during maintenance')
            try:
                stored_document, already_encoded = (
                    _compact_turn_projection_document(raw)
                )
            except (
                ProjectionCodecError,
                orjson.JSONDecodeError,
                orjson.JSONEncodeError,
                TypeError,
                ValueError,
            ):
                report['invalid_rows'] += 1
                continue
            if already_encoded:
                report['already_encoded_rows'] += 1
            if len(stored_document) >= len(raw):
                report['unchanged_rows'] += 1
                continue
            updates.append(_TurnProjectionUpdate(
                stored_document=stored_document,
                turn_id=candidate.turn_id,
                conversation_id=candidate.conversation_id,
                user_id=candidate.user_id,
                projection_revision=candidate.projection_revision,
                input_bytes=len(raw),
            ))
        if not updates:
            continue

        def _write_batch(conn) -> int:
            updated = 0
            for update in updates:
                cursor = conn.execute(
                    f'UPDATE {table} SET projection_json=? '
                    'WHERE turn_id=? AND conversation_id=? AND user_id=? '
                    'AND projection_revision=? '
                    'AND length(CAST(projection_json AS BLOB))=?'
                    f'{guard_sql}',
                    (
                        update.stored_document,
                        update.turn_id,
                        update.conversation_id,
                        update.user_id,
                        update.projection_revision,
                        update.input_bytes,
                    ),
                )
                changed = max(0, int(cursor.rowcount))
                if changed != 1:
                    raise RuntimeError(
                        'Turn projection codec update count mismatched')
                updated += changed
            return updated

        updated = _SQLITE_TOOLING.run_sqlite_tool_write(
            connection,
            db_path=db_path,
            lease=lease,
            purpose='historical Turn projection codec backfill',
            operation=_write_batch,
        )
        input_bytes = sum(update.input_bytes for update in updates)
        stored_bytes = sum(len(update.stored_document) for update in updates)
        report['updated_rows'] += int(updated)
        report['updated_input_bytes'] += input_bytes
        report['updated_stored_bytes'] += stored_bytes
        report['saved_bytes'] += input_bytes - stored_bytes
        report['write_batches'] += 1
        _SQLITE_TOOLING.checkpoint_sqlite_wal(connection)
    return report


def _maintain_archived_conversation_rows(
    connection: sqlite3.Connection,
    *,
    db_path: Path,
    lease: ProjectLease,
) -> dict:
    """Compact valid frozen messages and retire their derived search copy."""
    required = {
        'id', 'user_id', 'messages_json', 'msg_count', 'search_text',
    }
    if not _table_has_columns(connection, 'storage_conversations', required):
        return {'mode': 'unsupported_schema', 'updated_rows': 0}

    report = {
        'mode': 'lossless_archive_codec_and_search_retirement',
        'scanned_rows': 0,
        'scanned_payload_bytes': 0,
        'scanned_search_text_bytes': 0,
        'already_encoded_rows': 0,
        'unchanged_rows': 0,
        'invalid_rows': 0,
        'oversize_rows': 0,
        'updated_rows': 0,
        'projection_encoded_messages': 0,
        'compressed_messages': 0,
        'updated_input_bytes': 0,
        'updated_public_bytes': 0,
        'updated_projected_bytes': 0,
        'updated_stored_bytes': 0,
        'compression_saved_bytes': 0,
        'compacted_message_rows': 0,
        'cleared_search_text_rows': 0,
        'cleared_search_text_bytes': 0,
        'message_saved_bytes': 0,
        'saved_bytes': 0,
        'write_batches': 0,
        'max_page_payload_bytes': 0,
        'selection_row_limit': _ARCHIVED_CONVERSATION_SELECT_ROWS,
        'page_payload_budget_bytes': (
            _ARCHIVED_CONVERSATION_PAGE_PAYLOAD_BYTES),
        'document_budget_bytes': _ARCHIVED_CONVERSATION_DOCUMENT_BYTES,
    }
    last_id: str | None = None
    last_user_id = -1
    while True:
        candidates = connection.execute(
            'SELECT id,user_id,length(CAST(messages_json AS BLOB)),'
            'length(CAST(search_text AS BLOB)) '
            'FROM storage_conversations '
            'WHERE (msg_count>0 OR search_text<>\'\') '
            'AND (? IS NULL OR id>? '
            'OR (id=? AND user_id>?)) '
            'ORDER BY id,user_id LIMIT ?',
            (last_id, last_id, last_id, last_user_id,
             _ARCHIVED_CONVERSATION_SELECT_ROWS),
        ).fetchall()
        if not candidates:
            break

        selected: list[_ArchivedConversationCandidate] = []
        page_payload_bytes = 0
        for candidate in candidates:
            conversation_id = str(candidate[0])
            user_id = int(candidate[1])
            message_bytes = max(0, int(candidate[2] or 0))
            search_text_bytes = max(0, int(candidate[3] or 0))
            if message_bytes > _ARCHIVED_CONVERSATION_DOCUMENT_BYTES:
                last_id = conversation_id
                last_user_id = user_id
                report['oversize_rows'] += 1
                continue
            if (selected and page_payload_bytes + message_bytes
                    > _ARCHIVED_CONVERSATION_PAGE_PAYLOAD_BYTES):
                break
            selected.append(_ArchivedConversationCandidate(
                conversation_id=conversation_id,
                user_id=user_id,
                message_bytes=message_bytes,
                search_text_bytes=search_text_bytes,
            ))
            page_payload_bytes += message_bytes
            last_id = conversation_id
            last_user_id = user_id
            if page_payload_bytes >= _ARCHIVED_CONVERSATION_PAGE_PAYLOAD_BYTES:
                break
        if not selected:
            continue

        report['scanned_rows'] += len(selected)
        report['scanned_payload_bytes'] += page_payload_bytes
        report['scanned_search_text_bytes'] += sum(
            candidate.search_text_bytes for candidate in selected
        )
        report['max_page_payload_bytes'] = max(
            report['max_page_payload_bytes'], page_payload_bytes)
        updates: list[_ArchivedConversationUpdate] = []
        for candidate in selected:
            row = connection.execute(
                'SELECT CAST(messages_json AS BLOB) '
                'FROM storage_conversations WHERE id=? AND user_id=?',
                (candidate.conversation_id, candidate.user_id),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    'conversation archive disappeared during maintenance')
            raw = _sqlite_payload_bytes(row[0])
            if len(raw) != candidate.message_bytes:
                raise RuntimeError(
                    'conversation archive length changed during maintenance')
            try:
                compaction = _compact_archived_message_document(raw)
            except (ProjectionCodecError, orjson.JSONDecodeError,
                    orjson.JSONEncodeError, TypeError, ValueError):
                report['invalid_rows'] += 1
                continue
            if compaction.already_encoded:
                report['already_encoded_rows'] += 1
            messages_compacted = len(compaction.stored_document) < len(raw)
            if not messages_compacted and not candidate.search_text_bytes:
                report['unchanged_rows'] += 1
                continue
            stored_document = (
                compaction.stored_document if messages_compacted else raw
            )
            updates.append(_ArchivedConversationUpdate(
                stored_document=stored_document,
                conversation_id=candidate.conversation_id,
                user_id=candidate.user_id,
                input_message_bytes=len(raw),
                stored_message_bytes=len(stored_document),
                search_text_bytes=candidate.search_text_bytes,
                projection_encoded_messages=(
                    compaction.projection_encoded_messages
                    if messages_compacted else 0
                ),
                compressed_messages=(
                    compaction.compressed_messages
                    if messages_compacted else 0
                ),
                public_document_bytes=(
                    compaction.public_document_bytes
                    if messages_compacted else len(raw)
                ),
                projected_document_bytes=(
                    compaction.projected_document_bytes
                    if messages_compacted else len(raw)
                ),
                messages_compacted=messages_compacted,
            ))

        if not updates:
            continue

        def _write_batch(conn) -> int:
            updated = 0
            for update in updates:
                cursor = conn.execute(
                    'UPDATE storage_conversations '
                    'SET messages_json=?,search_text=\'\' '
                    'WHERE id=? AND user_id=? '
                    'AND length(CAST(messages_json AS BLOB))=? '
                    'AND length(CAST(search_text AS BLOB))=?',
                    (
                        update.stored_document,
                        update.conversation_id,
                        update.user_id,
                        update.input_message_bytes,
                        update.search_text_bytes,
                    ),
                )
                changed = max(0, int(cursor.rowcount))
                if changed != 1:
                    raise RuntimeError(
                        'conversation archive codec update count mismatched')
                updated += changed
            return updated

        updated = _SQLITE_TOOLING.run_sqlite_tool_write(
            connection,
            db_path=db_path,
            lease=lease,
            purpose='archived conversation compaction and search retirement',
            operation=_write_batch,
        )
        report['updated_rows'] += int(updated)
        report['projection_encoded_messages'] += sum(
            update.projection_encoded_messages for update in updates)
        report['compressed_messages'] += sum(
            update.compressed_messages for update in updates)
        input_bytes = sum(update.input_message_bytes for update in updates)
        stored_bytes = sum(update.stored_message_bytes for update in updates)
        public_bytes = sum(update.public_document_bytes for update in updates)
        projected_bytes = sum(
            update.projected_document_bytes for update in updates)
        search_text_bytes = sum(update.search_text_bytes for update in updates)
        report['updated_input_bytes'] += input_bytes
        report['updated_public_bytes'] += public_bytes
        report['updated_projected_bytes'] += projected_bytes
        report['updated_stored_bytes'] += stored_bytes
        report['compression_saved_bytes'] += projected_bytes - stored_bytes
        report['compacted_message_rows'] += sum(
            update.messages_compacted for update in updates)
        report['cleared_search_text_rows'] += sum(
            update.search_text_bytes > 0 for update in updates)
        report['cleared_search_text_bytes'] += search_text_bytes
        message_saved_bytes = input_bytes - stored_bytes
        report['message_saved_bytes'] += message_saved_bytes
        report['saved_bytes'] += message_saved_bytes + search_text_bytes
        report['write_batches'] += 1
        _SQLITE_TOOLING.checkpoint_sqlite_wal(connection)

    report['retained_archive_rows'] = int(connection.execute(
        'SELECT count(*) FROM storage_conversations WHERE msg_count>0'
    ).fetchone()[0])
    report['retained_search_text_rows'] = int(connection.execute(
        'SELECT count(*) FROM storage_conversations WHERE search_text<>\'\''
    ).fetchone()[0])
    return report


def _legacy_conversation_mirror_schema(
    connection: sqlite3.Connection,
) -> tuple[str, dict[str, list[str]]]:
    """Return whether the exact frozen-mirror schema is safe to inspect."""
    existing = _SQLITE_TOOLING.sqlite_schema_names(connection, 'table')
    legacy_tables = _LEGACY_CONVERSATION_MIRROR_TABLES - {
        'storage_conversations'}
    if not (legacy_tables & existing):
        return 'not_present', {}
    required = {
        'conversations': {'id', 'user_id', 'messages'},
        'conversation_messages': {
            'conv_id', *_LEGACY_MESSAGE_MIRROR_PAYLOAD_COLUMNS,
        },
        'conversation_turns': {'conversation_id', 'user_id'},
        'storage_conversations': {'id', 'user_id', 'messages_json'},
    }
    missing: dict[str, list[str]] = {}
    for table, columns in required.items():
        if table not in existing:
            missing[table] = sorted(columns)
            continue
        actual = {
            str(row[1])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        absent = sorted(columns - actual)
        if absent:
            missing[table] = absent
    return ('ready' if not missing else 'unsupported_schema'), missing


def _canonical_message_document_digest(value) -> bytes:
    """Hash one valid message array after insignificant JSON formatting."""
    raw = _sqlite_payload_bytes(value)
    document = orjson.loads(raw)
    public = decode_archived_message_sequence_from_storage(document)
    canonical = orjson.dumps(public, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(canonical).digest()


def _apply_legacy_translation_overlay(message, raw_state):
    """Reconstruct the sole authoritative overlay from the retired row codec."""
    if raw_state is None:
        return message
    state = orjson.loads(_sqlite_payload_bytes(raw_state))
    if not (
        isinstance(message, dict)
        and isinstance(state, dict)
        and state.get('v') == 1
    ):
        return message
    for key in _LEGACY_TRANSLATION_MESSAGE_KEYS:
        message.pop(key, None)
        if key in state:
            message[key] = state[key]
    segments = message.get('segments')
    if isinstance(segments, list):
        for segment in segments:
            if isinstance(segment, dict):
                segment.pop('translatedText', None)
        translated_segments = state.get('segmentTranslatedText')
        if isinstance(translated_segments, dict):
            for raw_index, value in translated_segments.items():
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    continue
                if (0 <= index < len(segments)
                        and isinstance(segments[index], dict)):
                    segments[index]['translatedText'] = value
    return message


def _legacy_message_rows_digest(
    connection: sqlite3.Connection,
    conversation_id: str,
    *,
    expected_rows: int,
) -> bytes:
    """Rebuild the retired row authority without materializing the full list."""
    digest = hashlib.sha256()
    digest.update(b'[')
    seen = 0
    for row in connection.execute(
        'SELECT seq,CAST(meta AS BLOB),CAST(translation_state AS BLOB) '
        'FROM conversation_messages WHERE conv_id=? ORDER BY seq',
        (conversation_id,),
    ):
        sequence = int(row[0])
        if sequence != seen:
            raise ValueError('legacy conversation-message sequence has a gap')
        message = orjson.loads(_sqlite_payload_bytes(row[1]))
        message = _apply_legacy_translation_overlay(message, row[2])
        if seen:
            digest.update(b',')
        digest.update(orjson.dumps(message, option=orjson.OPT_SORT_KEYS))
        seen += 1
    if seen != expected_rows:
        raise ValueError('legacy conversation-message count mismatched')
    digest.update(b']')
    return digest.digest()


def _retire_legacy_conversation_mirrors(
    connection: sqlite3.Connection,
    *,
    db_path: Path,
    lease: ProjectLease,
) -> dict:
    """Delete only per-conversation mirrors proven equal to current authority.

    The legacy tables are not runtime authorities, but they may be the last
    forensic copy of a failed historical import. Every candidate therefore
    needs an owner-scoped current header, no legacy Turn foreign key, and an
    equal canonical message array. Anything unverifiable remains untouched.
    """
    schema_state, missing_columns = _legacy_conversation_mirror_schema(
        connection)
    if schema_state != 'ready':
        return {
            'mode': schema_state,
            'deleted_conversations': 0,
            'deleted_message_rows': 0,
            'missing_columns': missing_columns,
        }

    mirror_payload_expression = '+'.join(
        'length(CAST(COALESCE(m.' + _quote_identifier(column)
        + ",'') AS BLOB))"
        for column in _LEGACY_MESSAGE_MIRROR_PAYLOAD_COLUMNS
    )
    legacy_rows = int(connection.execute(
        'SELECT count(*) FROM conversations').fetchone()[0])
    message_rows = int(connection.execute(
        'SELECT count(*) FROM conversation_messages').fetchone()[0])
    ambiguous_global_ids = int(connection.execute(
        'SELECT count(*) FROM (SELECT id FROM conversations '
        'GROUP BY id HAVING count(*)<>1)'
    ).fetchone()[0])
    if ambiguous_global_ids:
        # The retired row mirror owns only conv_id, not user_id. Without the
        # historical global-id invariant one owner's rows cannot be proved
        # apart from another's, so no candidate is safe to delete.
        return {
            'mode': 'ambiguous_legacy_conversation_ids',
            'initial_conversations': legacy_rows,
            'initial_message_rows': message_rows,
            'ambiguous_global_ids': ambiguous_global_ids,
            'deleted_conversations': 0,
            'deleted_message_rows': 0,
        }
    legacy_turn_conversations = int(connection.execute(
        'SELECT count(DISTINCT conversation_id) FROM conversation_turns'
    ).fetchone()[0])
    missing_current_authority = int(connection.execute(
        'SELECT count(*) FROM conversations AS l WHERE NOT EXISTS ('
        'SELECT 1 FROM storage_conversations AS s '
        'WHERE s.id=l.id AND s.user_id=l.user_id)'
    ).fetchone()[0])
    orphan_message_conversations = int(connection.execute(
        'SELECT count(*) FROM (SELECT DISTINCT m.conv_id '
        'FROM conversation_messages AS m WHERE NOT EXISTS ('
        'SELECT 1 FROM conversations AS l WHERE l.id=m.conv_id))'
    ).fetchone()[0])

    report = {
        'mode': 'verified_semantic_mirror_retirement',
        'initial_conversations': legacy_rows,
        'initial_message_rows': message_rows,
        'ambiguous_global_ids': 0,
        'legacy_turn_conversations': legacy_turn_conversations,
        'missing_current_authority': missing_current_authority,
        'orphan_message_conversations': orphan_message_conversations,
        'checked_conversations': 0,
        'verified_conversations': 0,
        'semantic_mismatches': 0,
        'mirror_mismatches': 0,
        'invalid_legacy_documents': 0,
        'invalid_current_documents': 0,
        'invalid_mirror_documents': 0,
        'oversize_documents': 0,
        'oversize_retire_candidates': 0,
        'deleted_conversations': 0,
        'deleted_message_rows': 0,
        'deleted_payload_bytes': 0,
        'batches': 0,
        'max_batch_payload_bytes': 0,
        'selection_row_limit': _LEGACY_CONVERSATION_MIRROR_SELECT_ROWS,
        'batch_payload_budget_bytes': (
            _LEGACY_CONVERSATION_MIRROR_BATCH_PAYLOAD_BYTES),
        'document_budget_bytes': _LEGACY_CONVERSATION_MIRROR_DOCUMENT_BYTES,
        'semantic_witness': 'sha256_canonical_json',
    }
    last_id: str | None = None
    last_user_id = -1
    while True:
        candidates = connection.execute(
            'SELECT l.id,l.user_id,'
            'length(CAST(l.messages AS BLOB)) AS legacy_bytes,'
            'length(CAST(s.messages_json AS BLOB)) AS current_bytes,'
            '(SELECT count(*) FROM conversation_messages AS m '
            'WHERE m.conv_id=l.id) AS mirror_rows,'
            '(SELECT COALESCE(SUM(' + mirror_payload_expression + '),0) '
            'FROM conversation_messages AS m WHERE m.conv_id=l.id) '
            'AS mirror_bytes '
            'FROM conversations AS l JOIN storage_conversations AS s '
            'ON s.id=l.id AND s.user_id=l.user_id '
            'WHERE (? IS NULL OR l.id>? OR (l.id=? AND l.user_id>?)) '
            'AND NOT EXISTS (SELECT 1 FROM conversation_turns AS t '
            'WHERE t.conversation_id=l.id AND t.user_id=l.user_id) '
            'ORDER BY l.id,l.user_id LIMIT ?',
            (last_id, last_id, last_id, last_user_id,
             _LEGACY_CONVERSATION_MIRROR_SELECT_ROWS),
        ).fetchall()
        if not candidates:
            break

        selected: list[tuple[str, int, int, int]] = []
        batch_payload_bytes = 0
        for candidate in candidates:
            conversation_id = str(candidate[0])
            user_id = int(candidate[1])
            legacy_bytes = max(0, int(candidate[2] or 0))
            current_bytes = max(0, int(candidate[3] or 0))
            candidate_message_rows = max(0, int(candidate[4] or 0))
            mirror_bytes = max(0, int(candidate[5] or 0))
            retire_bytes = legacy_bytes + mirror_bytes
            if (selected and batch_payload_bytes + retire_bytes
                    > _LEGACY_CONVERSATION_MIRROR_BATCH_PAYLOAD_BYTES):
                break
            last_id = conversation_id
            last_user_id = user_id
            if retire_bytes > _LEGACY_CONVERSATION_MIRROR_BATCH_PAYLOAD_BYTES:
                report['oversize_retire_candidates'] += 1
                continue
            if max(legacy_bytes, current_bytes) \
                    > _LEGACY_CONVERSATION_MIRROR_DOCUMENT_BYTES:
                report['oversize_documents'] += 1
                continue

            report['checked_conversations'] += 1
            legacy_payload = connection.execute(
                'SELECT CAST(messages AS BLOB) FROM conversations '
                'WHERE id=? AND user_id=?',
                (conversation_id, user_id),
            ).fetchone()
            if legacy_payload is None:
                raise RuntimeError(
                    'legacy conversation disappeared during mirror retirement')
            try:
                legacy_digest = _canonical_message_document_digest(
                    legacy_payload[0])
            except (orjson.JSONDecodeError, orjson.JSONEncodeError,
                    TypeError, ValueError):
                report['invalid_legacy_documents'] += 1
                continue

            current_payload = connection.execute(
                'SELECT CAST(messages_json AS BLOB) '
                'FROM storage_conversations WHERE id=? AND user_id=?',
                (conversation_id, user_id),
            ).fetchone()
            if current_payload is None:
                raise RuntimeError(
                    'current conversation disappeared during mirror retirement')
            try:
                current_digest = _canonical_message_document_digest(
                    current_payload[0])
            except (orjson.JSONDecodeError, orjson.JSONEncodeError,
                    TypeError, ValueError):
                report['invalid_current_documents'] += 1
                continue
            if legacy_digest != current_digest:
                report['semantic_mismatches'] += 1
                continue
            try:
                mirror_digest = _legacy_message_rows_digest(
                    connection,
                    conversation_id,
                    expected_rows=candidate_message_rows,
                )
            except (orjson.JSONDecodeError, orjson.JSONEncodeError,
                    TypeError, ValueError):
                report['invalid_mirror_documents'] += 1
                continue
            if mirror_digest != current_digest:
                report['mirror_mismatches'] += 1
                continue
            selected.append((
                conversation_id,
                user_id,
                candidate_message_rows,
                retire_bytes,
            ))
            batch_payload_bytes += retire_bytes
            if (batch_payload_bytes
                    >= _LEGACY_CONVERSATION_MIRROR_BATCH_PAYLOAD_BYTES):
                break

        if not selected:
            continue

        def _delete_batch(conn) -> tuple[int, int]:
            deleted_conversations = 0
            deleted_messages = 0
            for (conversation_id, user_id, expected_message_rows,
                 _retire_bytes) in selected:
                message_cursor = conn.execute(
                    'DELETE FROM conversation_messages WHERE conv_id=?',
                    (conversation_id,),
                )
                removed_messages = max(0, int(message_cursor.rowcount))
                if removed_messages != expected_message_rows:
                    raise RuntimeError(
                        'legacy conversation-message delete count mismatched')
                conversation_cursor = conn.execute(
                    'DELETE FROM conversations WHERE id=? AND user_id=? '
                    'AND NOT EXISTS (SELECT 1 FROM conversation_turns AS t '
                    'WHERE t.conversation_id=? AND t.user_id=?)',
                    (conversation_id, user_id, conversation_id, user_id),
                )
                removed_conversations = max(
                    0, int(conversation_cursor.rowcount))
                if removed_conversations != 1:
                    raise RuntimeError(
                        'legacy conversation mirror delete count mismatched')
                deleted_messages += removed_messages
                deleted_conversations += removed_conversations
            return deleted_conversations, deleted_messages

        deleted_conversations, deleted_messages = (
            _SQLITE_TOOLING.run_sqlite_tool_write(
                connection,
                db_path=db_path,
                lease=lease,
                purpose='legacy conversation mirror retirement',
                operation=_delete_batch,
            )
        )
        report['verified_conversations'] += int(deleted_conversations)
        report['deleted_conversations'] += int(deleted_conversations)
        report['deleted_message_rows'] += int(deleted_messages)
        report['deleted_payload_bytes'] += batch_payload_bytes
        report['batches'] += 1
        report['max_batch_payload_bytes'] = max(
            report['max_batch_payload_bytes'], batch_payload_bytes)
        _SQLITE_TOOLING.checkpoint_sqlite_wal(connection)

    report['retained_conversations'] = int(connection.execute(
        'SELECT count(*) FROM conversations').fetchone()[0])
    report['retained_message_rows'] = int(connection.execute(
        'SELECT count(*) FROM conversation_messages').fetchone()[0])
    return report


def _table_count(connection, table: str) -> int | None:
    row = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
        (table,)).fetchone()
    if row is None:
        return None
    return int(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])


def _deferred_index_identity(statement: str) -> tuple[str, str]:
    """Return index/table names from the schema's constrained DDL grammar."""
    tokens = statement.split()
    if (len(tokens) < 8
            or [token.upper() for token in tokens[:5]]
            != ['CREATE', 'INDEX', 'IF', 'NOT', 'EXISTS']
            or tokens[6].upper() != 'ON'):
        raise RuntimeError('invalid deferred SQLite index statement')
    return tokens[5], tokens[7].split('(', 1)[0]


def _install_deferred_indexes(
    connection: sqlite3.Connection,
    *,
    db_path: Path,
    lease: ProjectLease | None,
) -> list[str]:
    """Build missing performance indexes only inside this offline window."""
    existing_tables = _SQLITE_TOOLING.sqlite_schema_names(connection, 'table')
    installed = []
    for statement in deferred_index_statements('sqlite'):
        index_name, table_name = _deferred_index_identity(statement)
        if table_name not in existing_tables:
            continue
        if _SQLITE_TOOLING.sqlite_index_exists(connection, index_name):
            continue
        if lease is None:
            with _SQLITE_TOOLING.write_transaction(connection):
                connection.execute(statement)
        else:
            def _create_index(conn, sql=statement):
                conn.execute(sql)

            _SQLITE_TOOLING.run_sqlite_tool_write(
                connection,
                db_path=db_path,
                lease=lease,
                purpose=f'install deferred index {index_name}',
                operation=_create_index,
            )
            _SQLITE_TOOLING.checkpoint_sqlite_wal(connection)
        installed.append(index_name)
    return installed


def _retire_obsolete_deferred_indexes(
    connection: sqlite3.Connection,
    *,
    db_path: Path,
    lease: ProjectLease | None,
) -> list[str]:
    """Drop superseded performance indexes only inside an offline window."""
    retired = []
    for index_name in sorted(OBSOLETE_DEFERRED_INDEX_NAMES):
        if not _SQLITE_TOOLING.sqlite_index_exists(connection, index_name):
            continue
        if lease is None:
            with _SQLITE_TOOLING.write_transaction(connection):
                _SQLITE_TOOLING.drop_sqlite_index(connection, index_name)
        else:
            def _drop_index(conn, name=index_name):
                _SQLITE_TOOLING.drop_sqlite_index(conn, name)

            _SQLITE_TOOLING.run_sqlite_tool_write(
                connection,
                db_path=db_path,
                lease=lease,
                purpose=f'retire deferred index {index_name}',
                operation=_drop_index,
            )
            _SQLITE_TOOLING.checkpoint_sqlite_wal(connection)
        retired.append(index_name)
    return retired


def _incremental_reclaim_all(
    connection: sqlite3.Connection,
    *,
    batch_pages: int = 32_768,
) -> dict:
    """Return every free page to the filesystem without a second DB copy.

    Each batch can move at most 128 MiB with the standard 4 KiB page size,
    then truncates the WAL. This is slower than ``VACUUM INTO`` but keeps the
    temporary storage envelope small enough for a nearly-full personal disk.
    The database must already use ``auto_vacuum=INCREMENTAL``.
    """
    mode = int(connection.execute('PRAGMA auto_vacuum').fetchone()[0])
    if mode != 2:
        raise RuntimeError(
            'low-space reclaim requires auto_vacuum=INCREMENTAL; '
            f'authority mode is {mode}')
    page_size = int(connection.execute('PRAGMA page_size').fetchone()[0])
    initial = int(connection.execute('PRAGMA freelist_count').fetchone()[0])
    remaining = initial
    reclaimed = 0
    while remaining > 0:
        requested = min(max(1, int(batch_pages)), remaining)
        connection.execute(f'PRAGMA incremental_vacuum({requested})')
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        current = int(
            connection.execute('PRAGMA freelist_count').fetchone()[0])
        if current >= remaining:
            raise RuntimeError(
                'incremental_vacuum made no progress; refusing an unbounded '
                f'cleanup loop with {remaining} free pages remaining')
        reclaimed += remaining - current
        remaining = current
    return {
        'initial_free_pages': initial,
        'reclaimed_pages': reclaimed,
        'reclaimed_bytes': reclaimed * page_size,
        'batch_pages': int(batch_pages),
    }


def _verify_authority(
    candidate: Path,
    *,
    canonical_path: Path,
    parity_before: dict[str, int | None],
) -> dict[str, int | None]:
    """Verify integrity, storage mode, and every user-authority row count."""
    if candidate.resolve() == canonical_path.resolve():
        # Low-space mode verifies the canonical authority itself under the
        # already-held project lease. The candidate facade correctly rejects
        # that path, so use the tooling facade's bounded read-only handle.
        check = _open_readonly(candidate)
    else:
        check = _SQLITE_TOOLING.open_sqlite_candidate_connection(
            candidate, canonical_path=canonical_path, writable=False)
    try:
        integrity = check.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            raise RuntimeError(
                f'compacted authority failed integrity_check: {integrity}')
        mode = int(check.execute('PRAGMA auto_vacuum').fetchone()[0])
        if mode != 2:
            raise RuntimeError(
                'compacted authority lost auto_vacuum=INCREMENTAL '
                f'(mode={mode}); refusing publication')
        parity = {
            table: (_table_count(check, table), before_count)
            for table, before_count in parity_before.items()}
    finally:
        check.close()
    mismatches = {
        table: counts for table, counts in parity.items()
        if counts[0] != counts[1]}
    if mismatches:
        raise RuntimeError(
            f'row-count parity mismatch on {sorted(mismatches)}; '
            'refusing publication')
    return {table: counts[0] for table, counts in parity.items()}


def offline_compact(
    project_root: Path,
    *,
    ttl_days: float,
    vacuum: bool = True,
    low_space: bool = False,
    retire_legacy_conversation_mirrors: bool = False,
) -> dict:
    """Windowed retention + compaction pass over a STOPPED authority.

    Caller contract: the web server (and therefore the sidecar) is stopped.
    The project lease is acquired for the whole pass, which both proves the
    authority is idle and blocks any sidecar from starting mid-pass.
    """
    data_dir = project_root / 'data'
    live = data_dir / 'tofu.db'
    if not live.is_file():
        raise RuntimeError(f'no SQLite authority at {live}')
    if low_space and not vacuum:
        raise RuntimeError('low-space reclaim cannot be combined with no-vacuum')
    if retire_legacy_conversation_mirrors and not vacuum:
        raise RuntimeError(
            'legacy conversation mirror retirement requires physical reclaim')
    lease = ProjectLease(
        data_dir,
        owner_kind='offline_maintenance',
        owner_label='SQLite deep clean',
    )
    lease.acquire()  # raises while any owner is alive; held until release
    compact: Path | None = None
    compact_owned = False
    installed_indexes: list[str] = []
    retired_indexes: list[str] = []
    try:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        compact = data_dir / f'.tofu.db.compact-{stamp}'
        retained = data_dir / f'tofu.db.pre-compact-{stamp}'
        maintenance_artifacts = (
            compact,
            Path(str(compact) + '-wal'),
            Path(str(compact) + '-shm'),
            retained,
            Path(str(retained) + '-wal'),
            Path(str(retained) + '-shm'),
        )
        collisions = [
            path.name
            for path in maintenance_artifacts
            if path.exists() or path.is_symlink()
        ]
        if collisions:
            raise RuntimeError(
                'refusing offline-maintenance artifact collision: '
                + ', '.join(collisions))
        # The project lease makes these freshly proved-absent names exclusive
        # to this invocation. Failure cleanup may remove only this owned set.
        compact_owned = True
        now_s = time.time()
        now_ms = int(now_s * 1000)
        cutoff_ms = int((now_s - ttl_days * 86400) * 1000)
        before = _parse_header(live)

        connection = _SQLITE_TOOLING.open_sqlite_tool_connection(
            live, writable=True, lease=lease)
        try:
            t0 = time.monotonic()
            deleted = _delete_eligible_transport_rows(
                connection, cutoff_ms, db_path=live, lease=lease)
            task_event_maintenance = (
                _maintain_task_event_rows(
                    connection,
                    now_ms=now_ms,
                    db_path=live,
                    lease=lease,
                )
                if vacuum
                else {'mode': 'skipped_without_physical_reclaim'}
            )
            legacy_transport = (
                _delete_expired_legacy_transport_rows(
                    connection,
                    attempt_cutoff_ms=cutoff_ms,
                    now_ms=now_ms,
                    db_path=live,
                    lease=lease,
                )
                if vacuum
                else {'mode': 'skipped_without_physical_reclaim'}
            )
            legacy_conversation_mirrors = (
                _retire_legacy_conversation_mirrors(
                    connection,
                    db_path=live,
                    lease=lease,
                )
                if retire_legacy_conversation_mirrors
                else {'mode': 'not_requested'}
            )
            compaction_archive_maintenance = (
                maintain_compaction_archive_storage(
                    connection,
                    db_path=live,
                    lease=lease,
                )
                if vacuum
                else {'mode': 'skipped_without_physical_reclaim'}
            )
            task_result_maintenance = (
                maintain_task_result_storage(
                    connection,
                    db_path=live,
                    lease=lease,
                )
                if vacuum
                else {'mode': 'skipped_without_physical_reclaim'}
            )
            turn_projection_maintenance = (
                _maintain_turn_projection_rows(
                    connection,
                    db_path=live,
                    lease=lease,
                )
                if vacuum
                else {'mode': 'skipped_without_physical_reclaim'}
            )
            archived_conversation_maintenance = (
                _maintain_archived_conversation_rows(
                    connection,
                    db_path=live,
                    lease=lease,
                )
                if vacuum
                else {'mode': 'skipped_without_physical_reclaim'}
            )
            parity_before = {
                table: _table_count(connection, table)
                for table in _PARITY_TABLES}
            incremental = None
            if not vacuum:
                # This explicit offline mode is the lightest safe index
                # transition for a large authority that needs no page rewrite.
                # Retire first so CREATE INDEX can reuse v1's freed pages.
                retired_indexes = _retire_obsolete_deferred_indexes(
                    connection, db_path=live, lease=lease)
                installed_indexes = _install_deferred_indexes(
                    connection, db_path=live, lease=lease)
            elif low_space:
                incremental = _incremental_reclaim_all(connection)
                retired_indexes = _retire_obsolete_deferred_indexes(
                    connection, db_path=live, lease=lease)
                installed_indexes = _install_deferred_indexes(
                    connection, db_path=live, lease=lease)
            elif vacuum:
                page_size = int(
                    connection.execute('PRAGMA page_size').fetchone()[0])
                pages = int(
                    connection.execute('PRAGMA page_count').fetchone()[0])
                free_pages = int(
                    connection.execute('PRAGMA freelist_count').fetchone()[0])
                estimated_copy = max(0, pages - free_pages) * page_size
                capacity = copy_capacity_requirement(estimated_copy)
                available = int(shutil.disk_usage(data_dir).free)
                if available < capacity['required_free_bytes']:
                    raise RuntimeError(
                        'insufficient free space for verified copy compaction: '
                        f"need {capacity['required_free_bytes']} bytes, have "
                        f'{available}; make an independent backup and rerun '
                        'with --low-space')
                connection.execute(f"VACUUM INTO '{compact.as_posix()}'")
        finally:
            connection.close()

        if vacuum and not low_space:
            candidate = _SQLITE_TOOLING.open_sqlite_candidate_connection(
                compact, canonical_path=live, writable=True)
            try:
                # Publication renames only the main candidate file. Force a
                # rollback-journal mode so an index commit can never remain in
                # a candidate-named WAL that the atomic swap would orphan.
                _SQLITE_TOOLING.prepare_sqlite_compaction_candidate(candidate)
                # Drop v1 before building v2 so SQLite can reuse its pages and
                # the compact authority never carries both large indexes.
                retired_indexes = _retire_obsolete_deferred_indexes(
                    candidate, db_path=compact, lease=None)
                installed_indexes = _install_deferred_indexes(
                    candidate, db_path=compact, lease=None)
            finally:
                candidate.close()
        elapsed = time.monotonic() - t0

        if not vacuum:
            return {
                'ok': True, 'vacuum': False, 'deleted_rows': deleted,
                'elapsed_s': round(elapsed, 1), 'before': before,
                'task_event_maintenance': task_event_maintenance,
                'legacy_transport': legacy_transport,
                'legacy_conversation_mirrors': legacy_conversation_mirrors,
                'compaction_archive_maintenance': (
                    compaction_archive_maintenance),
                'task_result_maintenance': task_result_maintenance,
                'turn_projection_maintenance': turn_projection_maintenance,
                'archived_conversation_maintenance': (
                    archived_conversation_maintenance),
                'installed_indexes': installed_indexes,
                'retired_indexes': retired_indexes,
            }

        if low_space:
            fsync_file(live)
            fsync_directory(data_dir)
            parity = _verify_authority(
                live, canonical_path=live, parity_before=parity_before)
            after = _parse_header(live)
            return {
                'ok': True, 'vacuum': True,
                'reclaim_mode': 'incremental-low-space',
                'deleted_rows': deleted,
                'elapsed_s': round(elapsed, 1),
                'before': before, 'after': after,
                'incremental': incremental,
                'task_event_maintenance': task_event_maintenance,
                'legacy_transport': legacy_transport,
                'legacy_conversation_mirrors': legacy_conversation_mirrors,
                'compaction_archive_maintenance': (
                    compaction_archive_maintenance),
                'task_result_maintenance': task_result_maintenance,
                'turn_projection_maintenance': turn_projection_maintenance,
                'archived_conversation_maintenance': (
                    archived_conversation_maintenance),
                'installed_indexes': installed_indexes,
                'retired_indexes': retired_indexes,
                'parity': parity,
                'note': 'no on-volume rollback copy was retained; start the '
                        'server and verify the independent backup remains usable',
            }

        # ── Verification gates: integrity, mode, row parity. ──
        parity = _verify_authority(
            compact, canonical_path=live, parity_before=parity_before)

        # ── Atomic swap with the pre-clean authority retained. ──
        fsync_file(compact)
        os.replace(live, retained)
        try:
            os.replace(compact, live)
        except BaseException:
            # Never leave the project without an authority: restore.
            os.replace(retained, live)
            raise
        fsync_directory(data_dir)
        after = _parse_header(live)
        rollback_retention = prune_retained_rollbacks(
            data_dir, preserve=retained)
        return {
            'ok': True, 'vacuum': True, 'deleted_rows': deleted,
            'elapsed_s': round(elapsed, 1),
            'before': before, 'after': after,
            'retained': str(retained),
            'task_event_maintenance': task_event_maintenance,
            'legacy_transport': legacy_transport,
            'legacy_conversation_mirrors': legacy_conversation_mirrors,
            'compaction_archive_maintenance': (
                compaction_archive_maintenance),
            'task_result_maintenance': task_result_maintenance,
            'turn_projection_maintenance': turn_projection_maintenance,
            'archived_conversation_maintenance': (
                archived_conversation_maintenance),
            'rollback_retention': rollback_retention,
            'reclaim_mode': 'verified-copy',
            'installed_indexes': installed_indexes,
            'retired_indexes': retired_indexes,
            'parity': parity,
            'note': 'start the server; after the deployment is verified '
                    'healthy, retire the exact retained basename with the '
                    '--retire-rollback confirmation command',
        }
    finally:
        # A failed VACUUM INTO must not turn ENOSPC into a second persistent
        # storage leak. These names are unique products of this invocation;
        # the live authority and retained rollback file are never targets.
        if compact is not None and compact_owned:
            for artifact in (
                compact,
                Path(f'{compact}-wal'),
                Path(f'{compact}-shm'),
            ):
                try:
                    artifact.unlink(missing_ok=True)
                except OSError:
                    pass
        lease.release()


def retire_rollback(project_root: Path, basename: str) -> dict:
    """Retire one exact deep-clean recovery point in an offline lease window."""
    data_dir = project_root / 'data'
    live = data_dir / 'tofu.db'
    if not live.is_file() or live.is_symlink():
        raise RuntimeError(f'no safe SQLite authority at {live}')
    lease = ProjectLease(
        data_dir,
        owner_kind='offline_maintenance',
        owner_label='SQLite rollback retirement',
    )
    lease.acquire()
    try:
        target = resolve_rollback_artifact(data_dir, basename)
        if os.path.samestat(live.stat(), target.stat()):
            raise RuntimeError('rollback target aliases the live authority')
        companions = [Path(f'{target}-wal'), Path(f'{target}-shm')]
        if any(path.exists() for path in companions):
            raise RuntimeError(
                'rollback target has an unexpected WAL/SHM companion')

        # Both names must still be SQLite authorities. The current authority
        # additionally receives a full quick_check before its only local
        # recovery point can be removed.
        _parse_header(live)
        _parse_header(target)
        connection = _open_readonly(live)
        try:
            check = connection.execute('PRAGMA quick_check').fetchone()
        finally:
            connection.close()
        if not check or str(check[0]) != 'ok':
            detail = str(check[0]) if check else 'missing result'
            raise RuntimeError(
                f'live authority quick_check failed: {detail}')

        stat = target.stat()
        allocated = int(getattr(stat, 'st_blocks', 0) or 0) * 512
        if allocated <= 0:
            allocated = int(stat.st_size)
        target.unlink()
        fsync_directory(data_dir)
        if target.exists():
            raise RuntimeError('rollback retirement postcondition failed')
        return {
            'ok': True,
            'retired': target.name,
            'reclaimed_logical_bytes': int(stat.st_size),
            'reclaimed_allocated_bytes': allocated,
            'remaining': rollback_artifact_inventory(data_dir),
        }
    finally:
        lease.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--project-root', type=Path, default=_PROJECT_ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--analyze', action='store_true',
                      help='read-only size report (safe on the live authority)')
    mode.add_argument('--offline', action='store_true',
                      help='retention + compaction pass; authority must be stopped')
    mode.add_argument(
        '--retire-rollback', metavar='BASENAME',
        help='delete one exact pre-compact rollback; authority must be stopped')
    parser.add_argument('--ttl-days', type=float, default=1.0,
                        help='settled-attempt transport retention horizon '
                             'for the offline pass (default: 1 day)')
    reclaim = parser.add_mutually_exclusive_group()
    reclaim.add_argument('--no-vacuum', action='store_true',
                         help='delete + migrate deferred indexes in place; '
                              'skip physical page compaction')
    reclaim.add_argument(
        '--low-space', action='store_true',
        help='bounded in-place incremental reclaim; requires an independent '
             'backup and retains no on-volume rollback copy')
    parser.add_argument(
        '--retire-legacy-conversation-mirrors', action='store_true',
        help='after per-conversation semantic verification, retire frozen '
             'pre-Sidecar mirrors during physical reclaim')
    parser.add_argument('--confirm', action='store_true',
                        help='required for --offline and --retire-rollback')
    args = parser.parse_args(argv)

    try:
        if args.analyze:
            if args.retire_legacy_conversation_mirrors:
                raise RuntimeError(
                    'legacy mirror retirement is valid only with --offline')
            report = analyze(args.project_root, ttl_days=args.ttl_days)
        elif args.retire_rollback:
            if not args.confirm:
                raise RuntimeError('--retire-rollback requires --confirm')
            if args.no_vacuum or args.low_space:
                raise RuntimeError(
                    'rollback retirement does not accept reclaim-mode flags')
            if args.retire_legacy_conversation_mirrors:
                raise RuntimeError(
                    'rollback retirement does not accept legacy mirror flags')
            report = retire_rollback(args.project_root, args.retire_rollback)
        else:
            if not args.confirm:
                raise RuntimeError('--offline requires --confirm')
            report = offline_compact(
                args.project_root, ttl_days=args.ttl_days,
                vacuum=not args.no_vacuum, low_space=args.low_space,
                retire_legacy_conversation_mirrors=(
                    args.retire_legacy_conversation_mirrors),
            )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except BaseException as exc:
        print(json.dumps({'ok': False, 'error': type(exc).__name__,
                          'message': str(exc)}, sort_keys=True),
              file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
