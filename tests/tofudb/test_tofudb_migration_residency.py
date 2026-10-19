"""Large-domain resume verification retains only the current source document."""
from types import SimpleNamespace
import hashlib
import tracemalloc

import pytest

from lib.storage_authority_sql.migration import tofudb_migration_import as importer

pytestmark = pytest.mark.unit


def test_completed_document_domain_resume_has_constant_residency(monkeypatch):
    count = 4096
    seed = 'large-domain'

    def rows():
        for index in range(count):
            yield {'id': str(index), 'body': str(index).ljust(4096, '.')}

    digest = hashlib.sha256(seed.encode()).hexdigest()
    for document in rows():
        digest = importer._record_chain(digest, document)
    checkpoint = SimpleNamespace(record_verified_prefix=lambda *args, **kwargs: None, domain=lambda _: {
        'complete': True, 'rows': count, 'sha256': digest, 'cursor': [],
    })
    monkeypatch.setattr(importer, '_scan', lambda *args, **kwargs: rows())
    def query(session, operation, payload):
        if operation == 'get':
            return dict(payload)
        start = int((payload['cursor'] or 'doc:0').split(':')[1])
        end = min(start + 128, count)
        return {'rows': [{'id': str(i)} for i in range(start, end)],
                'cursor': f'doc:{end}' if end < count else None}

    def point(table, columns, keys, identity):
        return {'id': identity[0], 'body': identity[0].ljust(4096, '.')}

    monkeypatch.setattr(importer, '_query', query)
    tracemalloc.start()
    try:
        importer._import_document_domain(
            SimpleNamespace(tables=('documents',), point=point), object(), checkpoint,
            'documents', batch_rows=16, after_verified_prefix=None,
            table={'name': 'documents', 'columns': ['id', 'body'], 'keys': ['id']}, build_document=lambda row: row,
            restore_operation='restore', get_operation='get',
            get_identity=lambda document: document, scan_operation='scan',
            cursor_label='doc:', identity_of_row=lambda row: row,
            point_key=lambda identity: (identity['id'],), chain_seed=seed,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024
