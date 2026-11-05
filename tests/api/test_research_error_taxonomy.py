"""Research HTTP error taxonomy.

Research page parameters and draft/workspace input are client faults; a
frozen-basis, accounting or persistence invariant is a server fault. The
route must not turn the latter into a 400 merely because it is a ValueError.
A typed missing resource (``LifecycleNotFound``) is a 404 with its message.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]


@pytest.mark.parametrize(('module', 'function', 'path'), [
    ('lib.research.action_observations', 'load_action_observation_page',
     '/api/v4/research/actions/task-a/observations'),
    ('lib.research.action_observations', 'load_action_effect_page',
     '/api/v4/research/actions/task-a/effects'),
    ('lib.research.model_accounting', 'load_model_calls_page',
     '/api/v4/research/actions/task-a/model-calls'),
    ('lib.research.action_terminal', 'load_action_terminal',
     '/api/v4/research/actions/task-a/terminal'),
    ('lib.research.action_discovery', 'discover_unfinished_actions',
     '/api/v4/research/actions/unfinished'),
])
def test_research_internal_fault_is_redacted_500(
    flask_client, monkeypatch, module, function, path,
):
    import importlib

    service = importlib.import_module(module)
    monkeypatch.setattr(service, function,
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            ValueError('frozen basis invariant failed')))
    response = flask_client.get(path)
    assert response.status_code == 500
    assert 'frozen basis invariant failed' not in response.get_data(as_text=True)


@pytest.mark.parametrize(('module', 'function', 'path', 'message', 'field'), [
    ('lib.research.action_observations', 'load_action_observation_page',
     '/api/v4/research/actions/task-a/observations',
     'Invalid Research observation page cursor', 'cursor'),
    ('lib.research.action_observations', 'load_action_effect_page',
     '/api/v4/research/actions/task-a/effects',
     'Invalid Research observation page cursor', 'cursor'),
    ('lib.research.model_accounting', 'load_model_calls_page',
     '/api/v4/research/actions/task-a/model-calls',
     'Invalid Research observation page cursor', 'cursor'),
    ('lib.research.action_discovery', 'discover_unfinished_actions',
     '/api/v4/research/actions/unfinished', 'lang must be en or zh', 'lang'),
])
def test_research_client_fault_keeps_400_and_field(
    flask_client, monkeypatch, module, function, path, message, field,
):
    import importlib
    from lib.error_envelope import BadRequest

    service = importlib.import_module(module)
    monkeypatch.setattr(service, function,
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            BadRequest(message, field=field)))
    response = flask_client.get(path)
    assert response.status_code == 400
    body = response.get_json()
    assert message in response.get_data(as_text=True)
    assert body['error']['kind'] == 'bad_request'
    if 'field' in body['error']:
        assert body['error']['field'] == field


@pytest.mark.parametrize(('module', 'function', 'method', 'path', 'body'), [
    ('lib.research.workspace', 'save_workspace', 'put', '/api/v4/research/workspace',
     {'direction': 'Study', 'workspace': {}, 'expected_revision': 0}),
    ('lib.research.workspace', 'save_workspace', 'post',
     '/api/v4/research/manuscript/scaffold',
     {'direction': 'Study', 'workspace': {}, 'expected_revision': 0}),
    ('lib.research.draft', 'draft_protocol', 'post',
     '/api/v4/research/workspace/draft-protocol',
     {'direction': 'Study', 'idea_id': 'idea-1', 'source_task_id': 'deleted-source'}),
    ('lib.research.action_continuation', 'resume_action', 'post',
     '/api/v4/research/actions/task-a/resume', {}),
])
def test_research_missing_resource_is_a_typed_404(
    flask_client, monkeypatch, module, function, method, path, body,
):
    import importlib
    from lib.turn_lifecycle import LifecycleNotFound

    service = importlib.import_module(module)
    monkeypatch.setattr(service, function,
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            LifecycleNotFound('Saved research source not found')))
    response = getattr(flask_client, method)(path, json=body)
    assert response.status_code == 404
    error = response.get_json()['error']
    assert error['kind'] == 'not_found'
    assert error['message'] == 'Saved research source not found'


def test_research_numeric_limit_remains_a_client_400(flask_client):
    response = flask_client.get('/api/v4/research/actions/task-a/observations?limit=abc')
    assert response.status_code == 400
    body = response.get_json()
    assert body['error']['kind'] == 'bad_request'
    assert body['error']['message'] == 'limit must be an integer'
    assert 'limit must be an integer' in response.get_data(as_text=True)
