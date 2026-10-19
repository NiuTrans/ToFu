"""Private Turn transfer receipts preserve bounded progress and verification."""
import hashlib
import json

import pytest

from lib.storage_authority_sql.migration import tofudb_migration_import as m
from lib.storage_authority_sql.migration import tofudb_migration_turn_payload as payload

pytestmark = pytest.mark.unit


def test_upload_resumes_at_verified_buffer_offset(monkeypatch):
    raw = b'payload'
    calls = []

    def command(session, operation, value):
        calls.append((operation, value))
        if operation.endswith('.begin'):
            assert value['sha256'] == hashlib.sha256(raw).hexdigest()
            return {'offset': 3}
        if operation.endswith('.append'):
            assert value == {'offset': 3, 'hex': raw[3:].hex()}
            return {'offset': len(raw)}
        return {}

    monkeypatch.setattr(m, '_command', command)
    payload.upload(object(), raw)
    assert calls[-1][0].endswith('.finish')


@pytest.mark.parametrize('corruption', ['hash', 'size', 'offset', 'final'])
def test_read_rejects_corrupt_receipts_and_releases_buffer(monkeypatch, corruption):
    raw = json.dumps({'projection': {'content': '工具结果'}}).encode()
    calls = []

    def command(session, operation, value):
        calls.append(operation)
        if operation.endswith('.begin'):
            return {'bytes': 0 if corruption == 'size' else len(raw),
                    'sha256': '0'*64 if corruption == 'hash' else hashlib.sha256(raw).hexdigest()}
        if operation.endswith('.chunk'):
            return {'offset': 1 if corruption == 'offset' else 0, 'hex': raw.hex(),
                    'final': corruption != 'final'}
        return {}

    monkeypatch.setattr(m, '_command', command)
    with pytest.raises(m.MigrationImportError):
        payload.read(object(), {'id': 'turn', 'conversation_id': 'conv'})
    assert calls[-1] == 'migration.turn.transfer.read.abort'
