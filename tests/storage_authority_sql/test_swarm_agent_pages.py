"""SQL swarm agent pages pack whole checkpoints below the stream ceiling."""
from uuid import uuid4

import pytest

from lib.storage_authority_sql.operations_pkg import _runs
from tests.storage_authority_sql.test_native_checkpoint_storage import backend as backend
from tests.storage_authority_sql.test_native_checkpoint_handoff import TransactionClient

pytestmark = pytest.mark.unit


def _agent_ids(page):
    return [agent['agent_id'] for agent in page['agents']]


def test_agent_pages_follow_the_byte_budget_and_always_progress(backend, monkeypatch):
    client = TransactionClient(backend)
    client.command('swarm.session.save', dict(swarm_key='swarm', task_id='task', specs=[],
        config={'user_id': 7}, now_ms=1), str(uuid4()))
    for agent_id in ('a', 'b', 'c'):
        client.command('swarm.agent.save', dict(swarm_key='swarm', agent_id=agent_id,
            status='running', messages=[{'role': 'tool', 'content': agent_id * 1000}],
            result={}, now_ms=2), str(uuid4()))
    monkeypatch.setattr(_runs, '_SWARM_AGENT_PAGE_BYTES', 3000)

    pages, payload = [], {'swarm_key': 'swarm'}
    while True:
        page = client.query('swarm.agents.page', payload)
        pages.append(_agent_ids(page))
        if page['next_after_agent_id'] is None:
            break
        payload = {'swarm_key': 'swarm', 'after_agent_id': page['next_after_agent_id']}
    assert pages == [['a', 'b'], ['c']]

    monkeypatch.setattr(_runs, '_SWARM_AGENT_PAGE_BYTES', 1)
    single = client.query('swarm.agents.page', {'swarm_key': 'swarm'})
    assert _agent_ids(single) == ['a']
    assert single['next_after_agent_id'] == 'a'
    assert client.query('swarm.agents.page', {'swarm_key': 'missing'}) == {
        'agents': [], 'next_after_agent_id': None}


def test_an_append_that_would_outgrow_one_agent_page_is_refused_unchanged(backend, monkeypatch):
    from lib.storage.errors import StorageError
    client = TransactionClient(backend)
    client.command('swarm.session.save', dict(swarm_key='swarm', task_id='task', specs=[],
        config={'user_id': 7}, now_ms=1), str(uuid4()))
    client.command('swarm.agent.save', dict(swarm_key='swarm', agent_id='a', status='running',
        messages=[{'role': 'tool', 'content': 'x' * 1000}], result={}, checkpoint_id='c0',
        now_ms=2), str(uuid4()))
    monkeypatch.setattr(_runs, '_SWARM_AGENT_PAGE_BYTES', 3000)

    def append(content):
        return client.command('swarm.agent.append', dict(
            swarm_key='swarm', agent_id='a', base_checkpoint_id='c0', base_message_count=1,
            checkpoint_id='c1', messages=[{'role': 'tool', 'content': content}], result={},
            status='running', now_ms=3), str(uuid4()))

    # The same bound a full save of that transcript would meet as one frame.
    with pytest.raises(StorageError, match='exceeds the size limit') as refused:
        append('y' * 3000)
    assert refused.value.code == 'database_protocol_error'
    assert append('y' * 100) == {'appended': True}
    page = client.query('swarm.agents.page', {'swarm_key': 'swarm'})
    assert [message['content'] for message in page['agents'][0]['messages']] == [
        'x' * 1000, 'y' * 100]
