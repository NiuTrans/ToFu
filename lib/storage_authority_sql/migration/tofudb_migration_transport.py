"""Retired transport logs and reconstructible projections need no row copy.

Legacy attempt_events/task_events have no live writers and are TTL-bounded
retention candidates; their mapped successor streams are strict supersets, so
the legacy rows carry no unique durable document. storage_projection_outbox
and storage_turn_search are transient/rebuildable projections of the durable
storage_conversation_turns. storage_turn_projection_checkpoints is admitted
here only when empty: a populated checkpoint table must still fail the import
upstream in tofudb_migration_projection.require_materialized_turns, which
carries the precise operator instruction. These are structural retired/rebuild
proofs, not byte-mirrors — legacy and successor schemas differ and independent
TTL retention means row multisets are never compared.
"""
from __future__ import annotations

RETIRED_TRANSPORT_POLICY = (
    'retired-ttl-bounded-transport-log;no-live-writer;superseded-by-mapped-successor-stream'
)
_RETIRED_TRANSPORT = {
    'attempt_events': 'storage_attempt_events',
    'task_events': 'storage_events',
}

_PROJECTION_OUTBOX_PROOF = 'transient-projection-dirty-marker-queue'
_TURN_SEARCH_PROOF = 'rebuildable-search-materialization-from-durable-turns'
_CHECKPOINT_PROOF = 'reconstructible-live-turn-projection-checkpoint'
_DURABLE_TURNS = 'storage_conversation_turns'


def _mapped_table_names(plan) -> set[str]:
    return {table['name'] for domain in plan['domains'] for table in domain['tables']}


def verified_retired_transport(source, plan) -> dict:
    """Return rebuild evidence for dead legacy logs superseded by a live stream."""
    mapped = _mapped_table_names(plan)
    result = {}
    for legacy, successor in _RETIRED_TRANSPORT.items():
        if (legacy not in source.tables or legacy in mapped
                or successor not in source.tables):
            continue
        result[legacy] = {
            'proof': RETIRED_TRANSPORT_POLICY,
            'archivedInputTables': [successor],
            'action': ('drop-after-successor-stream-verified;'
                       'ttl-retained-transport-only;no-unique-durable-rows'),
        }
    return result


def verified_reconstructible_projections(source, plan) -> dict:
    """Return rebuild evidence for projections derivable from durable turns."""
    mapped = _mapped_table_names(plan)
    result = _verified_event_read_guards(source, mapped)
    if _DURABLE_TURNS not in source.tables:
        return result
    if 'storage_projection_outbox' in source.tables and 'storage_projection_outbox' not in mapped:
        result['storage_projection_outbox'] = {
            'proof': _PROJECTION_OUTBOX_PROOF,
            'archivedInputTables': [_DURABLE_TURNS],
            'action': 'reproject-from-durable-turns;transient-markers-not-durable',
        }
    if 'storage_turn_search' in source.tables and 'storage_turn_search' not in mapped:
        result['storage_turn_search'] = {
            'proof': _TURN_SEARCH_PROOF,
            'archivedInputTables': [_DURABLE_TURNS],
            'action': 'rebuild-from-durable-turn-search-text;reset-and-reproject',
        }
    checkpoint = 'storage_turn_projection_checkpoints'
    if (checkpoint in source.tables and checkpoint not in mapped
            and not source.table_has_rows(checkpoint)):
        result[checkpoint] = {
            'proof': _CHECKPOINT_PROOF,
            'archivedInputTables': [_DURABLE_TURNS, 'storage_attempt_events'],
            'action': ('rebuild-from-turn-head-and-attempt-replay;'
                       'only-classified-when-empty;populated-state-blocks-import-upstream'),
        }
    return result


def _verified_event_read_guards(source, mapped) -> dict:
    """Admit only exact lease-scoped metadata beside mapped task events."""
    table = 'storage_event_read_guards'
    columns = ('task_id', 'source_id', 'append_version', 'prune_version')
    if (table not in source.tables or table in mapped
            or 'storage_events' not in source.tables or 'storage_events' not in mapped):
        return {}
    if (source.table_columns(table) != columns
            or source.table_definition(table)['kind'] != 'table'):
        return {}
    # The exact primary-key iterator verifies key/index shape and keeps the
    # offline admission streaming. Unknown columns, user bodies and malformed
    # witnesses are not disposable simply because the table name matches.
    for row in source.rows(table, columns, ('task_id',)):
        if (not isinstance(row['task_id'], str) or not 1 <= len(row['task_id']) <= 512
                or not isinstance(row['source_id'], str) or len(row['source_id']) != 32
                or any(char not in '0123456789abcdef' for char in row['source_id'])
                or any(type(row[key]) is not int or row[key] < 0
                       for key in ('append_version', 'prune_version'))):
            return {}
    return {table: {
        'proof': 'exact-event-read-witness-metadata;lease-scoped;no-durable-payload',
        'archivedInputTables': ['storage_events'],
        'action': 'invalidate-old-lease-stamps;rebuild-from-target-event-authority',
    }}
