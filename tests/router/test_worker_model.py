"""Workers run on the coordinator's workerModel, else the owner's default (R5)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib.project_dispatch import delegation
from lib.project_dispatch.delegation import (
    capture_worker_policy, issued_worker_policy, policy_for_task, resolve_worker_model,
)
from lib.project_dispatch.repository import CONTRACT

pytestmark = pytest.mark.unit
OWNER = 7
CATALOG = [{'function': {'name': name}} for name in
           ('run_command', 'read_files', *CONTRACT['continuationTools'])]


class Bindings:
    def __init__(self, worker_model=''):
        self.model = worker_model

    def worker_model(self, path):
        assert path == '/work/project'
        return self.model


@pytest.fixture
def task(monkeypatch):
    from lib.conversations import project_coordination
    import lib
    bindings = Bindings()
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository', lambda owner: bindings)
    monkeypatch.setattr(lib, 'LLM_MODEL', 'owner-default-model')
    value = {'id': 'coordinator-task', 'convId': 'coordinator', '_userId': OWNER,
             'config': {'codeExecEnabled': True},
             '_coordinatorDispatchScope': {'project_path': '/work/project', 'binding_revision': 1,
                                           'conversation_id': 'coordinator', 'user_id': OWNER}}
    return SimpleNamespace(value=value, bindings=bindings)


def _issue(task):
    capture_worker_policy(task.value, task.value['config'], CATALOG, 'router-light-model')
    return task.value['_coordinatorWorkerPolicy']


def test_tool_assembly_never_reads_the_setting_and_workers_skip_the_coordinator_model(task):
    task.bindings.model = 'worker-strong-model'
    policy = _issue(task)
    assert policy.model is None and policy.coordinator_model == 'router-light-model'
    issued = issued_worker_policy(SimpleNamespace(), task.value)
    assert issued.model == 'worker-strong-model'
    continuation = policy_for_task(SimpleNamespace(), task.value, None)['continuation']
    assert continuation['model'] == 'router-light-model'
    assert continuation['workerModel'] == 'worker-strong-model'


def test_absent_setting_uses_the_owner_default_not_the_coordinator_model(task):
    _issue(task)
    assert issued_worker_policy(SimpleNamespace(), task.value).model == 'owner-default-model'
    assert task.value['_coordinatorWorkerModel'] == 'owner-default-model'


def test_no_model_at_all_is_an_explicit_refusal(task, monkeypatch):
    import lib
    monkeypatch.setattr(lib, 'LLM_MODEL', '')
    _issue(task)
    with pytest.raises(PermissionError, match='No Worker model'):
        resolve_worker_model(task.value)


@pytest.mark.parametrize('controls, expected', [
    ({'model': 'coordinator-model'}, 'coordinator-model'),
    ({'model': 'coordinator-model', 'workerModel': 'worker-model'}, 'worker-model'),
])
def test_continuations_keep_the_issued_worker_model(task, monkeypatch, controls, expected):
    from lib.project_dispatch import continuation
    row = {'request': {'delegation': {'policy': {'config': {}, 'continuation': {
        'version': 1, 'workerTools': ['run_command'], 'tools': ['report_to_user'], **controls}}}}}
    monkeypatch.setattr(continuation, 'current_continuation', lambda task, **kwargs: row)
    capture_worker_policy(task.value, {}, CATALOG, 'ignored-assembly-model')
    policy = task.value['_coordinatorWorkerPolicy']
    assert policy.model == expected and policy.coordinator_model == 'coordinator-model'


def test_owner_choice_passes_route_admission_before_it_is_stored(monkeypatch):
    import lib.model_routing as routing
    resolved = []

    class Repository:
        def get(self, boundary):
            resolved.append(boundary)
            return SimpleNamespace(document={'owner': boundary.owner_user_id})

    def resolve(document, model):
        if model != 'known-model':
            raise routing.ModelRoutingError('unknown model', field='model')
        return 'selection'
    monkeypatch.setattr(routing, 'ModelRoutingRepository', Repository)
    monkeypatch.setattr(routing, 'resolve_compatible_model', resolve)
    assert delegation.admit_worker_model(OWNER, ' known-model ', tenant_id='tenant') == 'known-model'
    assert resolved[0].owner_user_id == OWNER and resolved[0].tenant_id == 'tenant'
    with pytest.raises(ValueError, match='not routable'):
        delegation.admit_worker_model(OWNER, 'made-up-model')
    with pytest.raises(ValueError):
        delegation.admit_worker_model(OWNER, '   ')


def test_set_worker_model_writes_and_clears_the_coordinator_setting(monkeypatch):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations import settings_store
    settings = {'projectPath': '/work/project'}

    class Store:
        def __call__(self, **kwargs):
            return self

        def query(self, operation, payload):
            return {'value': {'user_id': OWNER, 'projectPath': '/work/project',
                              'conversationId': 'coordinator'}, 'version': 1}

    class Conversations:
        def get(self, conversation_id, *, user_id, include_messages):
            return SimpleNamespace(metadata={'settings': dict(settings)})

    def update(conversation_id, mutate, *, user_id):
        assert (conversation_id, user_id) == ('coordinator', OWNER)
        mutate(settings)
        return settings
    monkeypatch.setattr(settings_store, 'update_conversation_settings', update)
    monkeypatch.setattr(delegation, 'admit_worker_model', lambda owner, model, tenant_id=None: model.strip())
    repository = ProjectCoordinationRepository(OWNER, client=Store(), conversations=Conversations())
    assert repository.set_worker_model('/work/project', 'strong-model')['workerModel'] == 'strong-model'
    assert settings['workerModel'] == 'strong-model'
    assert repository.worker_model('/work/project') == 'strong-model'
    assert repository.set_worker_model('/work/project', None)['workerModel'] == ''
    assert 'workerModel' not in settings


def test_settings_vocabulary_declares_the_server_owned_worker_model():
    from lib.conversation_sync.settings_policy import CONVERSATION_SETTINGS_CONTRACT
    declaration = CONVERSATION_SETTINGS_CONTRACT['keys']['workerModel']
    assert declaration['author'] == 'server'
    assert 'lib/conversations/project_coordination.py' in declaration['owners']
