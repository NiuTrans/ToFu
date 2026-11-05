"""/api/v4/router: owner-scoped Global Router read, ensure, worker model and instructions."""
from __future__ import annotations

import os

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]


@pytest.fixture
def flask_app(chat_sidecar):
    import server
    server.app.config.update(TESTING=True)
    return server.app


@pytest.fixture
def home_root(tmp_path, monkeypatch):
    root = tmp_path / 'router-homes'
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(root))
    return root


def test_read_is_passive_and_ensure_is_idempotent(flask_client, chat_sidecar, home_root):
    unbound = flask_client.get('/api/v4/router')
    assert unbound.status_code == 200, unbound.get_json()
    assert unbound.get_json()['availability'] == 'unbound' and unbound.get_json()['workerModel'] == ''
    assert not home_root.exists()
    refused = flask_client.post('/api/v4/router', json={})
    assert refused.status_code == 400 and not home_root.exists()
    first = flask_client.post('/api/v4/router', json={}, headers={'Idempotency-Key': 'ensure-1'})
    assert first.status_code == 200, first.get_json()
    router = first.get_json()
    assert router['availability'] == 'available' and router['revision'] == 1
    assert os.path.isdir(os.path.join(router['projectPath'], '.git'))
    again = flask_client.post('/api/v4/router', json={}, headers={'Idempotency-Key': 'ensure-2'})
    assert again.get_json()['conversationId'] == router['conversationId']
    assert flask_client.get('/api/v4/router').get_json() == router
    assert flask_client.post('/api/v4/router', json={'unexpected': 1},
                             headers={'Idempotency-Key': 'ensure-3'}).status_code == 400


def test_worker_model_is_route_admitted_and_clearable(flask_client, chat_sidecar, home_root, monkeypatch):
    from lib.project_dispatch import delegation

    def admit(owner, model, *, tenant_id=None):
        if model != 'strong-model':
            raise ValueError('workerModel is not routable for this owner')
        return model
    monkeypatch.setattr(delegation, 'admit_worker_model', admit)
    router = flask_client.post('/api/v4/router', json={}, headers={'Idempotency-Key': 'ensure'}).get_json()
    endpoint, headers = '/api/v4/router/worker-model', {'Idempotency-Key': 'model'}
    rejected = flask_client.post(endpoint, json={'path': router['projectPath'], 'workerModel': 'made-up'},
                                 headers=headers)
    assert rejected.status_code == 400
    chosen = flask_client.post(endpoint, json={'path': router['projectPath'], 'workerModel': 'strong-model'},
                               headers=headers)
    assert chosen.status_code == 200, chosen.get_json()
    assert chosen.get_json()['workerModel'] == 'strong-model'
    assert flask_client.get('/api/v4/router').get_json()['workerModel'] == 'strong-model'
    cleared = flask_client.post(endpoint, json={'path': router['projectPath'], 'workerModel': None},
                                headers=headers)
    assert cleared.get_json()['workerModel'] == ''
    unbound = flask_client.post(endpoint, json={'path': '/no/such/coordinator', 'workerModel': None},
                                headers=headers)
    assert unbound.status_code == 403


def test_worker_model_reads_any_bound_router_without_changing_it(flask_client, chat_sidecar, home_root,
                                                                  monkeypatch, tmp_path):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.project_dispatch import delegation
    monkeypatch.setattr(delegation, 'admit_worker_model', lambda owner, model, *, tenant_id=None: model)
    project = tmp_path / 'project'
    project.mkdir()
    binding = ProjectCoordinationRepository(1).provision(
        str(project), title='Project coordinator', settings={}, command_id='route-to-project')
    endpoint = '/api/v4/router/worker-model'
    read = flask_client.get(endpoint, query_string={'path': str(project)})
    assert read.status_code == 200, read.get_json()
    assert {key: value for key, value in read.get_json().items() if key != 'ok'} == {**binding, 'workerModel': ''}
    flask_client.post(endpoint, json={'path': str(project), 'workerModel': 'fast-model'},
                      headers={'Idempotency-Key': 'project-model'})
    assert flask_client.get(endpoint, query_string={'path': str(project)}).get_json()['workerModel'] == 'fast-model'
    unbound = flask_client.get(endpoint, query_string={'path': str(tmp_path / 'unbound')}).get_json()
    assert unbound['availability'] == 'unbound' and unbound['workerModel'] == ''
    assert flask_client.get(endpoint).status_code == 400
    assert flask_client.get(endpoint, query_string={'path': str(project), 'extra': '1'}).status_code == 400


def test_owner_lists_and_cancels_standing_instructions(flask_client, chat_sidecar, home_root):
    from lib.router.instructions import RouterInstructions
    router = flask_client.post('/api/v4/router', json={}, headers={'Idempotency-Key': 'ensure'}).get_json()
    endpoint = '/api/v4/router/instructions'
    empty = flask_client.get(endpoint).get_json()
    assert empty['instructions'] == [] and empty['nextCursor'] is None
    instruction = RouterInstructions(1).create(
        router_conversation_id=router['conversationId'],
        source_turn={'turnId': 'owner-turn', 'projectionRevision': 1,
                     'projection': {'content': 'Reproduce reported bugs in the parser project.'}},
        label='Bug reports', project_path=None, thread='dx:gid:7', idempotency_key='bugs')
    listed = flask_client.get(endpoint).get_json()['instructions']
    assert [item['instructionId'] for item in listed] == [instruction['instructionId']]
    detail = flask_client.get(endpoint, query_string={'instructionId': instruction['instructionId']})
    assert detail.get_json()['applications'] == []
    assert flask_client.post(endpoint + '/cancel', json={'instructionId': instruction['instructionId']}
                             ).status_code == 400
    cancelled = flask_client.post(endpoint + '/cancel', json={'instructionId': instruction['instructionId']},
                                  headers={'Idempotency-Key': 'cancel'})
    assert cancelled.status_code == 200 and cancelled.get_json()['state'] == 'cancelled'
    assert flask_client.get(endpoint).get_json()['instructions'] == []
    everything = flask_client.get(endpoint, query_string={'includeCancelled': 'true'}).get_json()
    assert [item['state'] for item in everything['instructions']] == ['cancelled']
    assert flask_client.get(endpoint, query_string={'owner': '2'}).status_code == 400
    unknown = flask_client.post(endpoint + '/cancel', json={'instructionId': 'ri-unknown'},
                                headers={'Idempotency-Key': 'cancel-unknown'})
    assert unknown.status_code == 400


def test_owner_pauses_and_resumes_standing_instructions(flask_client, chat_sidecar, home_root):
    from lib.router.instructions import RouterInstructions
    router = flask_client.post('/api/v4/router', json={}, headers={'Idempotency-Key': 'ensure'}).get_json()
    endpoint = '/api/v4/router/instructions'
    instruction = RouterInstructions(1).create(
        router_conversation_id=router['conversationId'],
        source_turn={'turnId': 'owner-turn', 'projectionRevision': 1,
                     'projection': {'content': 'Reproduce reported bugs in the parser project.'}},
        label='Bug reports', project_path=None, thread=None, idempotency_key='bugs')
    body = {'instructionId': instruction['instructionId']}
    assert flask_client.post(endpoint + '/pause', json=body).status_code == 400
    assert flask_client.post(endpoint + '/pause', json={**body, 'state': 'paused'},
                             headers={'Idempotency-Key': 'pause-extra'}).status_code == 400
    paused = flask_client.post(endpoint + '/pause', json=body, headers={'Idempotency-Key': 'pause'})
    assert paused.status_code == 200, paused.get_json()
    assert paused.get_json()['state'] == 'paused' and isinstance(paused.get_json()['pausedAt'], int)
    [listed] = flask_client.get(endpoint).get_json()['instructions']
    assert listed['state'] == 'paused'
    again = flask_client.post(endpoint + '/pause', json=body, headers={'Idempotency-Key': 'pause-again'})
    assert again.get_json() == paused.get_json()
    resumed = flask_client.post(endpoint + '/resume', json=body, headers={'Idempotency-Key': 'resume'})
    assert resumed.status_code == 200 and resumed.get_json()['state'] == 'active'
    assert resumed.get_json()['pausedAt'] is None
    flask_client.post(endpoint + '/cancel', json=body, headers={'Idempotency-Key': 'cancel'})
    refused = flask_client.post(endpoint + '/resume', json=body, headers={'Idempotency-Key': 'resume-cancelled'})
    assert refused.status_code == 400 and 'cannot be resumed' in str(refused.get_json())
    unknown = flask_client.post(endpoint + '/pause', json={'instructionId': 'ri-unknown'},
                                headers={'Idempotency-Key': 'pause-unknown'})
    assert unknown.status_code == 400


def test_watchdog_read_is_passive_ensure_is_idempotent_and_review_is_owner_controlled(
        flask_client, chat_sidecar, home_root):
    endpoint = '/api/v4/router/watchdog'
    unbound = flask_client.get(endpoint)
    assert unbound.status_code == 200, unbound.get_json()
    assert unbound.get_json()['availability'] == 'unbound'
    assert unbound.get_json()['review'] == {'enabled': False, 'schedule': '7,37 * * * *', 'taskId': None}
    assert not home_root.exists()
    assert flask_client.post(endpoint, json={}).status_code == 400
    early = flask_client.post(endpoint + '/review', json={'enabled': True}, headers={'Idempotency-Key': 'early'})
    assert early.status_code == 403
    first = flask_client.post(endpoint, json={}, headers={'Idempotency-Key': 'watchdog-1'})
    assert first.status_code == 200, first.get_json()
    watchdog = first.get_json()
    assert watchdog['availability'] == 'available' and watchdog['projectPath'].endswith('/.tofu-watchdog')
    assert os.path.isdir(watchdog['projectPath'])
    again = flask_client.post(endpoint, json={}, headers={'Idempotency-Key': 'watchdog-2'}).get_json()
    assert again['conversationId'] == watchdog['conversationId']
    review = endpoint + '/review'
    assert flask_client.post(review, json={'enabled': True}).status_code == 400
    enabled = flask_client.post(review, json={'enabled': True}, headers={'Idempotency-Key': 'review-1'})
    assert enabled.status_code == 200, enabled.get_json()
    state = enabled.get_json()['review']
    assert state['enabled'] is True and state['schedule'] == '7,37 * * * *' and state['taskId']
    custom = flask_client.post(review, json={'enabled': True, 'schedule': '*/20 * * * *'},
                               headers={'Idempotency-Key': 'review-2'}).get_json()['review']
    assert custom == {**state, 'schedule': '*/20 * * * *'}
    assert flask_client.post(review, json={'enabled': True, 'schedule': 'whenever'},
                             headers={'Idempotency-Key': 'review-3'}).status_code == 400
    off = flask_client.post(review, json={'enabled': False}, headers={'Idempotency-Key': 'review-4'})
    assert off.get_json()['review'] == {**custom, 'enabled': False}
    assert flask_client.get(endpoint).get_json() == off.get_json()
