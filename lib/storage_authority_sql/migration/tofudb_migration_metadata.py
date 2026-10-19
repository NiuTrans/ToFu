"""Admission semantics for source-engine metadata retained as migration history.

Native identity/schema are independent and native archive usage is recomputed.
A completed legacy Project Brain marker is historical evidence; an incomplete
cutover cannot be treated as native-ready data. Unknown metadata keys may own
replay or projection lifecycle and must not be silently archived and ignored.
"""
from uuid import UUID

TABLE = {'name': 'storage_meta', 'keys': ['meta_key'], 'columns': ['meta_key', 'meta_value']}
POLICY = {
    'authority_uuid': 'validate-source-uuid;archive-only;keep-target-authority',
    'schema_version': 'require-current-source-schema;archive-only;keep-native-schema',
    'raw_archive_stored_bytes': 'validate-u64;archive-original;recompute-native-from-imported-archives',
    'project_brain_cutover_v1': 'require-complete-if-present;archive-only;import-current-project-brain-domains',
}


def validate_metadata(row):
    key, value = row['meta_key'], row['meta_value']
    if key not in POLICY:
        raise ValueError('source metadata key has no native migration semantics')
    if not isinstance(value, str):
        raise ValueError('source metadata value must be text')
    if key == 'authority_uuid':
        try:
            UUID(value)
        except ValueError:
            raise ValueError('source authority UUID is invalid') from None
    elif key == 'project_brain_cutover_v1':
        if value != 'complete':
            raise ValueError('source Project Brain cutover is incomplete')
    elif key == 'raw_archive_stored_bytes':
        if not value.isascii() or not value.isdigit() or len(value) > 20 or int(value) > 2**64 - 1:
            raise ValueError('source raw archive counter is invalid')
    else:
        from lib.storage_authority_sql.schema import SCHEMA_VERSION
        try:
            valid = int(value) == SCHEMA_VERSION
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('source schema version is invalid')
