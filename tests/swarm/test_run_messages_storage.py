"""Run-message CAS against a disposable real SQLite Sidecar, not app data."""
import uuid
import pytest
from lib.storage.errors import StorageError
from lib.swarm.run_messages import RunMessageRepository

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit


def test_durable_receipts_and_commit_before_lost_ack(chat_sidecar):
    from lib.storage import get_storage_client
    binding = {'owner': 1, 'tenant': '', 'conversationId': '', 'projectPath': '',
               'swarmKey': 'isolated', 'swarmRunId': uuid.uuid4().hex, 'agentId': 'child',
               'retainedAgentId': '', 'retainedRunId': '', 'retainedScope': ''}
    class LoseOneAck:
        lost = False
        def query(self, *args, **kw):
            return get_storage_client().query(*args, **kw)
        def command(self, *args, **kw):
            result = get_storage_client(write=True).command(*args, **kw)
            if not self.lost:
                self.lost = True
                raise StorageError('database_busy', 'Synthetic lost response after real commit', retryable=True)
            return result
    transport = LoseOneAck()
    repo = RunMessageRepository(binding, client=lambda **kw: transport)
    first = repo.submit(content='Durable evidence', kind='reference', key='one', sender_task_id='parent')
    assert first['state'] == 'submitted' and transport.lost
    restored = RunMessageRepository(binding)
    assert restored.load()[1] == 1
    assert restored.submit(content='Durable evidence', kind='reference', key='one', sender_task_id='parent')['id'] == first['id']
    assert len(restored.load()[0]['messages']) == 1
    restored.confirm([first['id']], child_task_id='physical-child', round_num=2)
    restored.close()
    value, revision = RunMessageRepository(binding).load()
    assert revision == 3 and value['closed']
    assert value['messages'][0]['state'] == 'input_confirmed'
    assert value['messages'][0]['inputRound'] == 3
    # The same visible IDs under another owner address a different mailbox.
    assert RunMessageRepository({**binding, 'owner': 2}).load()[1] == 0


def test_real_empty_terminal_tombstone_fences_late_first_send(chat_sidecar):
    from lib.swarm.run_messages import RunMessageError
    binding = {'owner': 1, 'tenant': '', 'conversationId': '', 'projectPath': '',
               'swarmKey': 'isolated', 'swarmRunId': uuid.uuid4().hex, 'agentId': 'child',
               'retainedAgentId': '', 'retainedRunId': '', 'retainedScope': ''}
    RunMessageRepository(binding).close()
    with pytest.raises(RunMessageError, match='ended'):
        RunMessageRepository(binding).submit(content='Late', kind='steer', key='late', sender_task_id='parent')
    assert RunMessageRepository(binding).load()[0] == {'binding': binding, 'closed': True, 'messages': []}
