"""Coordinator bindings are metadata, never task execution or authorization."""
from types import SimpleNamespace
from copy import deepcopy
import pytest
from lib.conversations.project_coordination import ProjectCoordinationRepository
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit

class Store:
    def __init__(self):
        self.rows = {}
        self.receipts = {}
    def __call__(self, **kwargs):
        return self
    def query(self, operation, payload):
        assert operation == 'record.get'
        return deepcopy(self.rows.get(payload['key']))
    def command(self, operation, payload, command_id):
        assert operation == 'record.put'
        if command_id in self.receipts:
            assert self.receipts[command_id] == payload
            return {}
        current = self.rows.get(payload['key'])
        version = current['version'] if current else 0
        if version != payload['expected_version']:
            raise StorageError('database_conflict', 'Concurrent binding')
        self.rows[payload['key']] = {'value': deepcopy(payload['value']), 'version': version + 1}
        self.receipts[command_id] = deepcopy(payload)
        return {'version': version + 1}

class Conversations:
    def __init__(self):
        self.rows = {(1, 'worker'): {'projectPath': '/project'}}
    def get(self, conversation_id, *, user_id, include_messages):
        assert include_messages is False
        settings = self.rows.get((user_id, conversation_id))
        return SimpleNamespace(metadata={'settings': settings}) if settings is not None else None

@pytest.fixture
def setup():
    store, conversations = Store(), Conversations()
    return store, conversations, ProjectCoordinationRepository(1, client=store, conversations=conversations)

def test_binding_read_restart_and_explicit_capabilities(setup):
    store, conversations, repository = setup
    assert repository.read('/project/')['availability'] == 'unbound'
    bound = repository.bind('/project/', 'worker', expected_revision=0, command_id='bind-1')
    assert bound['revision'] == 1 and bound['availability'] == 'available'
    assert bound['capabilities'] == {'assign': False, 'sendMessage': False, 'steerSubagent': True}
    restored = ProjectCoordinationRepository(1, client=store, conversations=conversations)
    assert restored.read('/project') == bound

def test_same_command_replays_without_second_mutation(setup):
    store, _, repository = setup
    first = repository.bind('/project', 'worker', expected_revision=0, command_id='same')
    assert repository.bind('/project', 'worker', expected_revision=0, command_id='same') == first
    assert len(store.receipts) == 1

def test_cross_owner_and_project_are_rejected(setup):
    store, conversations, repository = setup
    for path, conv in [('/other', 'worker'), ('/project', 'missing')]:
        with pytest.raises(ValueError):
            repository.bind(path, conv, expected_revision=0, command_id='bad')
    foreign = ProjectCoordinationRepository(2, client=store, conversations=conversations)
    with pytest.raises(ValueError):
        foreign.bind('/project', 'worker', expected_revision=0, command_id='bad')
    assert not store.rows

def test_stale_cas_and_deleted_or_rebound_targets(setup):
    _, conversations, repository = setup
    repository.bind('/project', 'worker', expected_revision=0, command_id='first')
    with pytest.raises(StorageError):
        repository.bind('/project', '', expected_revision=0, command_id='stale')
    conversations.rows[(1, 'worker')]['projectPath'] = '/other'
    unavailable = repository.read('/project')
    assert unavailable['availability'] == 'unavailable'
    assert unavailable['conversationId'] == 'worker'
    assert repository.bind('/project', '', expected_revision=1, command_id='unbind')['availability'] == 'unbound'

@pytest.mark.parametrize('revision', [True, -1, '1'])
def test_revision_validation(setup, revision):
    with pytest.raises(ValueError):
        setup[2].bind('/project', 'worker', expected_revision=revision, command_id='invalid')

def test_corrupt_owner_record_fails_closed(setup):
    store, _, repository = setup
    repository.bind('/project', 'worker', expected_revision=0, command_id='first')
    next(iter(store.rows.values()))['value']['user_id'] = 2
    with pytest.raises(StorageError):
        repository.read('/project')
