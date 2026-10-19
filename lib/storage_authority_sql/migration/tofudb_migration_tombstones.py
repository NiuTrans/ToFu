"""Exact Turn deletion markers through the native offline migration boundary.

The source row is the ownership authority even after its parent is gone. Native
claim, delta and retention indexes publish atomically. Enumeration independently
checks identities against source primary keys and exact deletion timestamps.
"""
from lib.storage_authority_sql.migration import tofudb_migration_import as m


def import_tombstones(source, session, checkpoint, *, batch_rows, after_verified_prefix):
    table = next(domain for domain in m.PLAN['domains']
                 if domain['name'] == 'turn_tombstones')['tables'][0]

    def document(row):
        if m._int(row['user_id']) != session.owner_id:
            raise m.MigrationImportError('tombstone source owner differs from target')
        value = {key: m._text(row[key]) for key in ('conversation_id', 'turn_id')}
        if any(not value[key] for key in value):
            raise m.MigrationImportError('tombstone source identity is empty')
        return {**value, 'user_id': session.owner_id,
                'deleted_at': m._nonneg_int(row['deleted_at'])}

    m._import_document_domain(
        source, session, checkpoint, 'turn_tombstones', batch_rows=batch_rows,
        after_verified_prefix=after_verified_prefix, table=table,
        build_document=document, restore_operation='migration.turn.tombstone.restore',
        get_operation='migration.turn.tombstone.get', get_identity=lambda value: value,
        scan_operation='migration.turn.tombstone.scan', cursor_label='tombstones:',
        identity_of_row=lambda value: value,
        point_key=lambda value: (value['conversation_id'], value['turn_id']),
        chain_seed='tofu-db.turn-tombstones.v1',
    )
