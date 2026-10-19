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

        self.conversations = None
        self.created = []
        self.receipts = {}
    def __call__(self, **kwargs):
        return self
    def query(self, operation, payload):
        assert operation == 'record.get'
        return deepcopy(self.rows.get(payload['key']))
    def command(self, operation, payload, command_id):

        if operation == 'conversation.create':
            key = (payload['user_id'], payload['conv_id'])
            assert key not in self.conversations.rows
            self.conversations.rows[key] = deepcopy(payload['settings'])
            self.created.append(deepcopy(payload))
            return {}
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

    store.conversations = conversations
    return store, conversations, ProjectCoordinationRepository(1, client=store, conversations=conversations)

def seed_legacy_binding(repository):
    # Existing durable bindings survive policy changes without being reselected.
    path, key = repository._identity('/project')
    repository.client.rows[key] = {
        'value': {'user_id': 1, 'projectPath': path, 'conversationId': 'worker'},
        'version': 1,
    }
    return repository.read(path)

def test_binding_read_restart_and_explicit_capabilities(setup):
    store, conversations, repository = setup
    assert repository.read('/project/')['availability'] == 'unbound'
    bound = seed_legacy_binding(repository)
    assert bound['revision'] == 1 and bound['availability'] == 'available'
    assert bound['capabilities'] == {'assign': False, 'sendMessage': False, 'steerSubagent': True}
    restored = ProjectCoordinationRepository(1, client=store, conversations=conversations)
    assert restored.read('/project') == bound

def test_same_command_replays_without_second_mutation(setup):
    store, _, repository = setup
    first = seed_legacy_binding(repository)
    assert repository.bind('/project', 'worker', expected_revision=0, command_id='same') == first
    assert not store.receipts

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
    seed_legacy_binding(repository)
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
    seed_legacy_binding(repository)
    next(iter(store.rows.values()))['value']['user_id'] = 2
    with pytest.raises(StorageError):
        repository.read('/project')


def test_bound_coordinator_role_is_ordinary_root_guidance(setup, monkeypatch):
    from lib.conversations import project_coordination as module
    _, _, repository = setup
    seed_legacy_binding(repository)
    monkeypatch.setattr(module, 'ProjectCoordinationRepository', lambda owner: repository)
    task = {'id': 'root', 'convId': 'worker', '_userId': 1, 'config': {}}
    role = module.prepare_coordinator_role('/project', 'worker', user_id=1, task=task)
    assert 'dispatch-only' in role and 'delegate_project_work' in role
    assert 'dispatch_project_assignment' in role and 'get_conversation' in role
    assert 'never perform concrete file reads/edits' in role
    assert 'current accepted human request' in role and 'await_project_workers' in role
    assert 'never new user authority' in role
    for changes in ({'_parentTaskId': 'parent'}, {'_userId': 2}, {'_transientRuntime': True}, {'convId': 'other'}):
        assert not module.prepare_coordinator_role('/project', 'worker', user_id=1, task={**task, **changes})
    repository.bind('/project', '', expected_revision=1, command_id='withdraw')
    assert not module.prepare_coordinator_role('/project', 'worker', user_id=1, task=task)


def test_historical_conversation_cannot_be_promoted(setup):
    store, _, repository = setup
    with pytest.raises(ValueError, match='existing conversations cannot be selected'):
        repository.bind('/project', 'worker', expected_revision=0, command_id='promote')
    assert repository.read('/project')['availability'] == 'unbound'
    assert not store.rows


def test_create_ignores_history_and_replay_preserves_current_binding(setup):
    store, _, repository = setup
    first = repository.create('/project', folder_id='folder', expected_revision=0, command_id='create')
    assert first['conversationId'] != 'worker'
    assert len(store.created) == 1
    assert store.created[0]['settings'] == {'projectPath': '/project', 'folderId': 'folder'}
    assert repository.create('/project', folder_id='folder', expected_revision=0,
                             command_id='retry') == first
    assert len(store.created) == 1
    repository.bind('/project', '', expected_revision=1, command_id='clear')
    with pytest.raises(ValueError):
        repository.bind('/project', first['conversationId'], expected_revision=2, command_id='reuse')
    second = repository.create('/project', folder_id='folder', expected_revision=2, command_id='create')
    assert second['conversationId'] != first['conversationId']
    assert len(store.created) == 2


def test_create_stale_revision_does_not_create_conversation(setup):
    store, _, repository = setup
    with pytest.raises(StorageError):
        repository.create('/project', folder_id='folder', expected_revision=1, command_id='stale')
    assert not store.created


def test_create_identity_collision_never_adopts_existing_conversation(setup, monkeypatch):
    import uuid
    store, conversations, repository = setup
    monkeypatch.setattr(uuid, 'uuid4', lambda: SimpleNamespace(hex='collision'))
    conversations.rows[(1, 'coordinator-collision')] = {'projectPath': '/project'}
    with pytest.raises(StorageError):
        repository.create('/project', folder_id='folder', expected_revision=0, command_id='collision')
    assert repository.read('/project')['availability'] == 'unbound'
    assert not store.created


def test_create_failure_never_adopts_a_concurrently_inserted_target(setup, monkeypatch):
    store, conversations, repository = setup
    command = store.command
    def fail_create(operation, payload, command_id):
        if operation == 'conversation.create':
            conversations.rows[(1, payload['conv_id'])] = {'projectPath': '/project'}
            raise StorageError('database_conflict', 'Already exists')
        return command(operation, payload, command_id)
    monkeypatch.setattr(store, 'command', fail_create)
    with pytest.raises(StorageError):
        repository.create('/project', folder_id='folder', expected_revision=0, command_id='collision')
    assert repository.read('/project')['availability'] == 'unbound'


def test_current_revision_create_replaces_legacy_binding_without_touching_old_chat(setup):
    store, conversations, repository = setup
    legacy = seed_legacy_binding(repository)
    before = deepcopy(conversations.rows[(1, 'worker')])
    replacement = repository.create('/project', folder_id='folder',
                                    expected_revision=legacy['revision'], command_id='clean')
    assert replacement['conversationId'] != 'worker'
    assert replacement['revision'] == legacy['revision'] + 1
    assert conversations.rows[(1, 'worker')] == before
    assert len(store.created) == 1
    assert repository.create('/project', folder_id='folder', expected_revision=1,
                             command_id='clean') == replacement
    assert len(store.created) == 1


def test_lost_cas_leaves_candidate_unbound_and_retry_never_adopts_it(setup, monkeypatch):
    store, conversations, repository = setup
    command = store.command
    def fail_binding(operation, payload, command_id):
        if operation == 'record.put':
            raise StorageError('database_conflict', 'Concurrent binding')
        return command(operation, payload, command_id)
    monkeypatch.setattr(store, 'command', fail_binding)
    with pytest.raises(StorageError):
        repository.create('/project', folder_id='folder', expected_revision=0, command_id='retry')
    orphan = store.created[0]['conv_id']
    assert (1, orphan) in conversations.rows
    assert repository.read('/project')['availability'] == 'unbound'
    monkeypatch.setattr(store, 'command', command)
    result = repository.create('/project', folder_id='folder', expected_revision=0, command_id='retry')
    assert result['conversationId'] != orphan
    assert len(store.created) == 2
