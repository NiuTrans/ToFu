"""Project Brain legacy-table fold for the explicit native import.

The live ``project_brain.cutover`` folds durable watch intent out of the
retired legacy authorities (``storage_watch_items`` /
``storage_watch_responses``) into the ``storage_project_brain_projects``
projection, then drops the retired tables without importing board/feed/status
execution history. A native Tofu-DB target has no legacy tables, so its
``project_brain.cutover`` is a no-op and the projection must be imported
directly. This module reproduces that fold read-only over the pinned SQLite
source: it synthesizes exactly the projection documents the cutover would have
persisted, so the operator importer restores them through the same
``migration.project.brain.project.*`` operations as any settled projection.

The fold is deterministic and fail-closed. A ``legacy_migrated`` event is the
single event in each synthesized stream, so ``head_sequence`` and
``checkpoint_sequence`` are both 1 and the projection agrees with them — the
invariant the native restore adapter enforces. Watch entries keep the
cutover's per-field identity bounds and complete Watch collection.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterator, Mapping

from lib.storage_authority_sql.operations_pkg._project_brain import (
    _empty_projection,
    _fold_event,
)
from lib.storage_authority_sql.migration.tofudb_migration_source import SQLiteMigrationSource

# Retired legacy authorities the fold consumes. ``storage_watch_runs`` is part
# of the same retired family but carried no durable intent; it is listed so the
# coverage gate can recognize the complete set the cutover owns.
CONSUMED_TABLES: tuple[str, ...] = (
    'project_events',
    'project_tasks',
    'storage_board_tasks',
    'storage_watch_items',
    'storage_watch_responses',
    'storage_watch_runs',
)

# Tables that contribute projection rows (watch intent). The remainder of
# CONSUMED_TABLES is execution history the cutover intentionally drops.
_WATCH_ITEM_TABLE = 'storage_watch_items'
_WATCH_RESPONSE_TABLE = 'storage_watch_responses'

_WATCH_ITEM_COLUMNS = (
    'item_id', 'project_path', 'kind', 'text', 'status', 'created_by_conv',
    'created_at', 'updated_at', 'user_id',
)
_WATCH_RESPONSE_COLUMNS = ('item_id', 'sequence', 'response', 'trigger', 'ts')

_TRAILING_SEPARATORS = re.compile(r'[/\\]+$')


def _text(value: Any, limit: int) -> str:
    return str(value or '')[:limit]


def _nonneg(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError('boolean is not a legacy timestamp')
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        raise ValueError('legacy Project Brain timestamp is invalid') from None
    return max(parsed, 0)


def fold_legacy_projections(
    source: SQLiteMigrationSource, *, now_ms: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield one restore document per (owner, project) watch bucket.

    Buckets, ordering, field bounds and complete Watch retention mirror the
    live cutover exactly, so the synthesized projection is byte-for-byte the
    one the engine would have written had it cut over this authority.
    """
    if _WATCH_ITEM_TABLE not in source.tables:
        return
    timestamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
    projects: dict[tuple[int, str], dict[str, Any]] = {}
    for row in source.rows(
        _WATCH_ITEM_TABLE, _WATCH_ITEM_COLUMNS, ('item_id',),
        batch_rows=128,
    ):
        # Mirror the live cutover exactly: it coerces the owner with int() and
        # skips watch items whose project scope normalizes to empty, because a
        # projection is keyed by a non-empty project_key.
        owner_user_id = _nonneg(row['user_id'])
        if owner_user_id < 1:
            raise ValueError('legacy watch item has no owner')
        normalized_project = _TRAILING_SEPARATORS.sub(
            '', _text(row['project_path'], 4096))
        if not normalized_project:
            continue
        latest = None
        if _WATCH_RESPONSE_TABLE in source.tables:
            response = _latest_response(source, _text(row['item_id'], 128))
            if response is not None and _text(response['response'], 4000).strip():
                latest = {
                    'text': _text(response['response'], 4000),
                    'trigger': _text(response['trigger'], 64),
                    'timestamp': _nonneg(response['ts']),
                }
        bucket = projects.setdefault(
            (owner_user_id, normalized_project), {'watch': []})
        bucket['watch'].append({
            'id': _text(row['item_id'], 128),
            'kind': _text(row['kind'], 64) or 'concern',
            'text': _text(row['text'], 4000),
            'status': ('active' if _text(row['status'], 32) in ('', 'open')
                       else 'resolved'),
            'sourceConversationId': _text(row['created_by_conv'], 256),
            'createdAt': _nonneg(row['created_at']),
            'updatedAt': _nonneg(row['updated_at']),
            'latestResult': latest,
        })
    for (owner_user_id, project_key) in sorted(projects):
        yield _synthesize_document(
            owner_user_id, project_key,
            projects[(owner_user_id, project_key)]['watch'], timestamp)


def _latest_response(
    source: SQLiteMigrationSource, item_id: str,
) -> Mapping[str, Any] | None:
    """The newest response for one watch item, matching the cutover's probe."""
    best: Mapping[str, Any] | None = None
    for row in source.rows(
        _WATCH_RESPONSE_TABLE, _WATCH_RESPONSE_COLUMNS, ('item_id', 'sequence'),
        key_prefix=(item_id,), batch_rows=128,
    ):
        best = row  # ascending (item_id, sequence); last page holds the max
    return best


def _synthesize_document(
    owner_user_id: int, project_key: str,
    watch: list[dict[str, Any]], timestamp: int,
) -> dict[str, Any]:
    """Fold one watch bucket into the exact settled projection document."""
    projection = _empty_projection(owner_user_id, project_key)
    _fold_event(projection, {
        'projectSequence': 1,
        'kind': 'legacy_migrated',
        'timestamp': timestamp,
        'payload': {'watch': list(watch)},
    })
    canonical = json.dumps(projection, sort_keys=True, separators=(',', ':'))
    return {
        'owner_user_id': owner_user_id,
        'project_key': project_key,
        'head_sequence': int(projection['headSequence']),
        'checkpoint_sequence': int(projection['checkpointSequence']),
        'projection_json': canonical,
        'projection': projection,
        'updated_at_ms': timestamp,
    }


def consumed_populated_tables(source: SQLiteMigrationSource) -> list[str]:
    """Retired tables that hold rows the fold is responsible for.

    Only used by the coverage gate; a table absent from the source is not
    populated and needs no fold coverage.
    """
    return [name for name in CONSUMED_TABLES
            if name in source.tables and source.table_has_rows(name)]
