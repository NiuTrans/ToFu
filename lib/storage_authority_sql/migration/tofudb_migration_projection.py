"""Admission for immutable Turn snapshots before native source binding.

Offline Turn import currently accepts materialized projections only. Checkpoints
and patch heads belong to the running source's bounded recovery/settlement path;
never discard or synthesize them in an importer that only reads inline bodies.
"""
from lib.storage.errors import StorageError
from lib.storage_authority_sql.turn_projection_head import projection_head_state


def require_materialized_turns(source):
    from lib.storage_authority_sql.migration.tofudb_migration_import import MigrationImportError

    checkpoint_table = 'storage_turn_projection_checkpoints'
    if checkpoint_table in source.tables and source.table_has_rows(checkpoint_table):
        raise MigrationImportError(
            'source retains Turn projection checkpoints; settle or recover source attempts before offline import')
    table = 'storage_conversation_turns'
    if table not in source.tables:
        return
    columns = source.table_columns(table)
    metadata = [name for name in (
        'projection_checkpoint_revision', 'projection_materialized_revision',
        'projection_patch_count', 'projection_patch_bytes',
    ) if name in columns]
    if not metadata:
        return
    for row in source.rows(table, ['turn_id', 'projection_revision', *metadata], ['turn_id']):
        try:
            state = projection_head_state(row)
        except StorageError as exc:
            raise MigrationImportError('source Turn projection head metadata is invalid') from exc
        if state.active or state.checkpoint_active:
            raise MigrationImportError(
                'source retains a Turn projection patch head; settle or recover source attempts before offline import')
