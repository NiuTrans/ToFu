"""Optional event projection preserves the real SQL wire/replay boundary."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.storage.errors import StorageError
from lib.task_replay.generated_request_inspector_projection import PROJECTION
from tests.storage.test_storage_authority_sql_contract import storage as storage

pytestmark = pytest.mark.unit
CASES = json.loads((Path(__file__).resolve().parents[1] /
                   'fixtures/request_inspector_projection.json').read_text())


def test_request_list_view_preserves_page_order_and_full_event_replay(storage):
    client = storage.client
    for sequence, case in enumerate(CASES):
        client.command('event.append', {'task_id': 'inspector', 'sequence': sequence,
                                       'event': case['event']}, f'inspector-{sequence}')
    def read(**options):
        return client.query('event.list', {'task_id': 'inspector', **options})

    original = read()
    projected = read(payload_projection=PROJECTION)
    assert len(projected) == len(original)
    for before, after, case in zip(original, projected, CASES):
        assert after['sequence'] == before['sequence']
        assert after['created_at_ms'] == before['created_at_ms']
        if case['event'] == case['expected']:
            assert after == before
        else:
            assert after['event'] == case['expected']
    assert read(payload_projection=PROJECTION, after_sequence=0, limit=1) == projected[1:2]
    omitted = read(payload_projection=PROJECTION, types=['messages_snapshot'],
                   payload_types=[], limit=1)
    assert omitted[0]['event'] == {'type': 'messages_snapshot'}
    assert read() == original
    assert read(payload_projection='full') == original
    for invalid in (None, True, 0, [], {}, 'unknown'):
        with pytest.raises(StorageError) as caught:
            read(payload_projection=invalid)
        assert caught.value.code == 'database_protocol_error'
