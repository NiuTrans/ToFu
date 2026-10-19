"""Viewport window selectors preserve query identity and legacy defaults."""
import asyncio
from unittest.mock import Mock

import pytest

from lib.conversation_sync.repository import SidecarConversationSyncRepository
from lib.conversation_sync.snapshot_query import ConversationSnapshotQuery
from routes.conversation_sync_v3 import _snapshot_turn_limit, _window_integer


pytestmark = pytest.mark.unit

def _in_request(query, read):
    from quart import Quart
    app = Quart(__name__)

    async def run():
        async with app.test_request_context('/api/v4/conversations/c/sync' + query):
            return read()
    return asyncio.run(run())


@pytest.mark.parametrize('query', [
    '?windowLimit=12', '?turnWindow=full&windowLimit=12',
    '?turnWindow=tail-96&windowLimit=0', '?turnWindow=tail-96&windowLimit=97',
    '?turnWindow=tail-96&windowLimit=12&windowLimit=24',
    '?turnWindow=tail-96&windowLimit=1.5',
])
def test_invalid_viewport_turn_selector_fails_before_storage(query):
    with pytest.raises(ValueError):
        _in_request(query, _snapshot_turn_limit)


def test_legacy_tail_and_viewport_override():
    for query, expected in [('', None), ('?turnWindow=tail-96', 96),
                            ('?turnWindow=tail-96&windowLimit=14', 14)]:
        assert _in_request(query, _snapshot_turn_limit) == expected


@pytest.mark.parametrize('value', ['0', '65535', '16777217', '-1', 'abc', '1.2', '65536&windowBytes=131072'])
def test_byte_selector_rejects_invalid_or_duplicate_values(value):
    with pytest.raises(ValueError):
        _in_request('?windowBytes=' + value, lambda: _window_integer('windowBytes', 65536, 16777216))


def test_repository_passes_byte_budget_without_changing_unopted_queries():
    client = Mock()
    client.query.return_value = {}
    repository = SidecarConversationSyncRepository(client_factory=lambda write=False: client)
    repository.snapshot('c', 7, turn_limit=14, max_projection_bytes=1048576)
    assert client.query.call_args.args == ('turn.sync.snapshot', {
        'conversation_id': 'c', 'user_id': 7, 'turn_limit': 14,
        'max_projection_bytes': 1048576,
    })
    repository.turn_page('c', 7, lane_id='main', expected_sync_sequence=11,
                         before_ordinal=50, max_projection_bytes=1048576)
    assert client.query.call_args.args[1]['max_projection_bytes'] == 1048576
    repository.snapshot('c', 7, turn_limit=96)
    assert 'max_projection_bytes' not in client.query.call_args.args[1]


def test_snapshot_query_carries_byte_budget_to_authority_loader():
    loader = Mock(return_value={'turns': [], 'pushWithheld': False})
    query = ConversationSnapshotQuery(loader, gather_seconds=0)
    query.read('c', 7, push_withheld=False, turn_limit=14, max_projection_bytes=1048576)
    loader.assert_called_once_with('c', 7, 14, include_artifact_hint=False,
                                   max_projection_bytes=1048576)
