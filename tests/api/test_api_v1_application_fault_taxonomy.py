"""Builtin faults behind application calls are redacted 500s, not client 4xx.

These routes used to classify a builtin ``ValueError``/``TypeError``/
``KeyError``/``LookupError`` (or any ``Exception``) raised behind the request
parse as the caller's fault and answer 400/404, often echoing raw exception
text. Only the typed ``BadRequest`` or a named domain error is a client fault
now; every other builtin reaches the logged, redacted 500 boundary. Each route
keeps its genuine client 400/404 with the existing message. Services are
patched at their owning module, and the real client-fault paths reject before
any durable write.
"""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]

_SECRET = 'tenant row is corrupt at /srv/private/state.db'


def _raise(exc):
    def raiser(*args, **kwargs):
        raise exc
    return raiser


def _text(response):
    return response.get_data(as_text=True)


def _assert_redacted_500(response):
    assert response.status_code == 500
    assert _SECRET not in _text(response)


def _assert_client_fault(response, status, message):
    assert response.status_code == status
    assert message in _text(response)


# ── users ────────────────────────────────────────────────────────────

def test_user_create_service_fault_is_a_redacted_500(flask_client, monkeypatch):
    import routes.api_v1.users as route
    monkeypatch.setattr(route, 'create_user', _raise(ValueError(_SECRET)))
    _assert_redacted_500(
        flask_client.post('/api/v4/users', json={'email': 'a@example.com'}))


def test_user_create_invalid_email_keeps_its_400(flask_client):
    _assert_client_fault(
        flask_client.post('/api/v4/users', json={'email': 'not-an-email'}),
        400, "Invalid email: 'not-an-email'")


@pytest.mark.parametrize(('field', 'service'), [
    ('role', 'update_user_role'), ('status', 'set_user_status')])
def test_user_patch_service_fault_is_a_redacted_500(
        flask_client, monkeypatch, field, service):
    import routes.api_v1.users as route
    monkeypatch.setattr(route, 'get_user', lambda user_id: object())
    monkeypatch.setattr(route, service, _raise(KeyError(_SECRET)))
    _assert_redacted_500(
        flask_client.patch('/api/v4/users/usr_x', json={field: 'admin'}))


@pytest.mark.parametrize(('field', 'value', 'message'), [
    ('role', 'owner', "Invalid role: 'owner'"),
    ('status', 'gone', "Invalid status: 'gone'"),
])
def test_user_patch_unknown_value_keeps_its_400(
        flask_client, monkeypatch, field, value, message):
    import routes.api_v1.users as route
    monkeypatch.setattr(route, 'get_user', lambda user_id: object())
    _assert_client_fault(
        flask_client.patch('/api/v4/users/usr_x', json={field: value}),
        400, message)


# ── working facts ────────────────────────────────────────────────────

_FACTS_URL = '/api/v4/conversations/conv-taxonomy/working-facts'
_DECISION = {'action': 'retract', 'revision': 0, 'id': 'wf_1', 'note': 'checked'}


def test_working_fact_decision_fault_is_a_redacted_500(flask_client, monkeypatch):
    from lib.memory.working_facts import service
    monkeypatch.setattr(service, 'owner_decision', _raise(ValueError(_SECRET)))
    _assert_redacted_500(flask_client.post(_FACTS_URL, json=_DECISION))


def test_working_fact_blank_decision_note_keeps_its_400(flask_client):
    _assert_client_fault(
        flask_client.post(_FACTS_URL, json={**_DECISION, 'note': ' '}),
        400, 'Describe the basis of the owner decision')


def test_working_fact_consumers_fault_is_a_redacted_500(flask_client, monkeypatch):
    from lib.memory.working_facts import consumers
    monkeypatch.setattr(consumers, 'query', _raise(ValueError(_SECRET)))
    _assert_redacted_500(flask_client.get(_FACTS_URL + '/consumers'))


@pytest.mark.parametrize(('limit', 'message'), [
    ('many', 'limit must be an integer'),
    ('0', 'Invalid working facts request'),
])
def test_working_fact_consumers_bad_limit_keeps_its_400(flask_client, limit, message):
    _assert_client_fault(
        flask_client.get(_FACTS_URL + '/consumers', query_string={'limit': limit}),
        400, message)


# ── model intelligence ───────────────────────────────────────────────

def test_model_intelligence_projection_fault_is_a_redacted_500(
        flask_client, monkeypatch):
    import routes.api_v1.model_intelligence as route
    monkeypatch.setattr(route, '_read_block', _raise(KeyError(_SECRET)))
    _assert_redacted_500(flask_client.get('/api/v4/model-intelligence/aa'))


def test_model_intelligence_pricing_fault_is_a_redacted_500(
        flask_client, monkeypatch):
    import routes.api_v1.model_intelligence as route
    monkeypatch.setattr(route, '_models', lambda repository, boundary: [])
    monkeypatch.setattr(route, 'pricing_block_for_models', _raise(LookupError(_SECRET)))
    _assert_redacted_500(
        flask_client.get('/api/v4/model-intelligence/openrouter-pricing'))


@pytest.mark.parametrize('path', ['aa', 'openrouter-pricing'])
def test_model_intelligence_without_owner_keeps_its_404(
        flask_client, monkeypatch, path):
    import routes.api_v1.model_intelligence as route
    monkeypatch.setattr(route, '_boundary', lambda: None)
    _assert_client_fault(
        flask_client.get('/api/v4/model-intelligence/' + path),
        404, 'Model intelligence settings not found')


# ── response surfaces ────────────────────────────────────────────────

def _surface_request(path):
    from quart import Quart, g
    from routes.response_assets import response_assets_bp

    async def scenario():
        app = Quart(__name__)
        app.register_blueprint(response_assets_bp)

        @app.before_request
        async def identity():
            g.auth_ctx = SimpleNamespace(
                is_authenticated=True, owner_user_id=7, key_id='test',
                tenant_id='tenant', scopes=[])
        response = await app.test_client().get(path)
        return response.status_code, await response.get_data(as_text=True)
    return asyncio.run(scenario())


@pytest.mark.parametrize(('function', 'path'), [
    ('available_providers', '/api/response-surfaces/providers?runId=run'),
    ('read_resources', '/api/response-surfaces/resources?runId=run&provider=test'),
])
def test_surface_builtin_fault_is_not_a_client_fault(monkeypatch, function, path):
    from lib import response_surfaces
    monkeypatch.setattr(response_surfaces, function, _raise(KeyError(_SECRET)))
    status, body = _surface_request(path)
    assert status == 503
    assert _SECRET not in body


@pytest.mark.parametrize(('function', 'path'), [
    ('available_providers', '/api/response-surfaces/providers?runId=run'),
    ('read_resources', '/api/response-surfaces/resources?runId=run&provider=test'),
])
def test_surface_missing_turn_is_not_found(monkeypatch, function, path):
    from lib import response_surfaces
    from lib.turn_lifecycle import LifecycleNotFound
    monkeypatch.setattr(response_surfaces, function,
                        _raise(LifecycleNotFound('Turn not found')))
    assert _surface_request(path)[0] == 404


def test_surface_invalid_provider_keeps_its_400():
    status, body = _surface_request(
        '/api/response-surfaces/resources?runId=run&provider=not%20valid')
    assert status == 400
    assert 'Invalid surface context or data' in body


# ── tasks ────────────────────────────────────────────────────────────

def test_task_start_kind_parse_fault_is_a_redacted_500(flask_client, monkeypatch):
    import routes.api_v1.tasks as route
    monkeypatch.setattr(route, 'require_str', _raise(ValueError(_SECRET)))
    _assert_redacted_500(
        flask_client.post('/api/v4/tasks/start', json={'kind': 'research'}))


def test_task_start_without_kind_keeps_its_400(flask_client):
    _assert_client_fault(
        flask_client.post('/api/v4/tasks/start', json={}),
        400, 'kind is required')


# ── skills ───────────────────────────────────────────────────────────

@pytest.fixture
def installed_skill(monkeypatch):
    import lib.skills
    monkeypatch.setattr(lib.skills, 'get_skill', lambda *args, **kwargs: {'id': 'demo'})


def test_skill_env_service_fault_is_a_redacted_500(
        flask_client, monkeypatch, installed_skill):
    import lib.skills.env
    monkeypatch.setattr(lib.skills.env, 'set_skill_env', _raise(ValueError(_SECRET)))
    _assert_redacted_500(flask_client.put(
        '/api/v4/skills/demo/env', json={'name': 'API_KEY', 'value': 'v'}))


def test_skill_env_invalid_name_keeps_its_400(flask_client, installed_skill):
    _assert_client_fault(
        flask_client.put('/api/v4/skills/demo/env',
                         json={'name': 'bad-name', 'value': 'v'}),
        400, "'bad-name' is not a valid environment variable name")


def test_skill_scope_move_fault_is_a_redacted_500(flask_client, monkeypatch):
    import lib.skills
    monkeypatch.setattr(lib.skills, 'set_skill_scope', _raise(ValueError(_SECRET)))
    _assert_redacted_500(flask_client.post(
        '/api/v4/skills/demo/scope', json={'scope': 'global'}))


def test_skill_scope_collision_keeps_its_400(flask_client, monkeypatch):
    import lib.skills
    from lib.error_envelope import BadRequest
    monkeypatch.setattr(lib.skills, 'set_skill_scope', _raise(
        BadRequest('demo already exists in global scope', field='scope')))
    _assert_client_fault(
        flask_client.post('/api/v4/skills/demo/scope', json={'scope': 'global'}),
        400, 'demo already exists in global scope')


# ── slides ───────────────────────────────────────────────────────────

@pytest.fixture
def deck_workdir(monkeypatch, tmp_path):
    import routes.api_v1.slides as route
    monkeypatch.setattr(route, '_task_workdir',
                        lambda task_id, *, user_id: str(tmp_path))
    return tmp_path


@pytest.mark.parametrize('page', ['1.html', '1/elements'])
def test_slide_page_parse_fault_is_a_redacted_500(
        flask_client, monkeypatch, deck_workdir, page):
    import lib.slides.pptd as pptd
    monkeypatch.setattr(pptd, 'parse_deck', _raise(KeyError(_SECRET)))
    _assert_redacted_500(flask_client.get(f'/api/v4/slides/deck1/pages/{page}'))


@pytest.mark.parametrize('page', ['1.html', '1/elements'])
def test_slide_page_without_deck_keeps_its_404(flask_client, deck_workdir, page):
    _assert_client_fault(
        flask_client.get(f'/api/v4/slides/deck1/pages/{page}'),
        404, 'deck_not_ready')


def _template_upload():
    from werkzeug.datastructures import FileStorage
    return FileStorage(stream=io.BytesIO(b'PK\x03\x04 template'), filename='brand.pptx',
                       content_type='application/vnd.openxmlformats-'
                                    'officedocument.presentationml.presentation')


def test_template_import_fault_is_a_redacted_500(flask_client, monkeypatch):
    import lib.slides.templates as templates
    monkeypatch.setattr(templates, 'import_template', _raise(ValueError(_SECRET)))
    _assert_redacted_500(flask_client.post(
        '/api/v4/slides/import-template', form={}, files={'file': _template_upload()}))


def test_template_import_rejection_keeps_its_400(flask_client, monkeypatch):
    import lib.slides.templates as templates
    from lib.error_envelope import BadRequest
    monkeypatch.setattr(templates, 'import_template', _raise(
        BadRequest('template file is 0 bytes; limit is 1', field='file')))
    _assert_client_fault(
        flask_client.post('/api/v4/slides/import-template', form={},
                          files={'file': _template_upload()}),
        400, 'template file is 0 bytes')


# ── project ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(('service', 'path', 'body'), [
    ('set_project', '/api/v4/project/set', {'path': '/workspace/a'}),
    ('set_project_paths', '/api/v4/project/paths', {'paths': ['/workspace/a']}),
])
def test_project_selection_fault_is_a_redacted_500(
        flask_client, monkeypatch, service, path, body):
    import lib.project_mod
    monkeypatch.setattr(lib.project_mod, service, _raise(TypeError(_SECRET)))
    method = flask_client.post if path.endswith('/set') else flask_client.put
    _assert_redacted_500(method(path, json=body))


def test_project_set_missing_directory_keeps_its_400(flask_client, tmp_path):
    missing = tmp_path / 'missing'
    _assert_client_fault(
        flask_client.post('/api/v4/project/set', json={'path': str(missing)}),
        400, f'Directory not found: {missing}')


@pytest.mark.parametrize(('body', 'message'), [
    ({'paths': ['{missing}']}, 'Directory not found: {missing}'),
    ({'paths': [7]}, 'Provide a \\"paths\\" array'),
    ({'paths': ['{root}'], 'readOnlyPaths': [7]}, 'readOnlyPaths must contain only paths'),
])
def test_project_paths_invalid_selection_keeps_its_400(
        flask_client, monkeypatch, tmp_path, body, message):
    import lib.project_mod
    calls = []
    real = lib.project_mod.set_project_paths

    def recorded(paths, readonly_paths=None):
        calls.append(paths)
        return real(paths, readonly_paths=readonly_paths)
    monkeypatch.setattr(lib.project_mod, 'set_project_paths', recorded)
    names = {'missing': str(tmp_path / 'missing'), 'root': str(tmp_path)}
    body = {key: [item.format(**names) if isinstance(item, str) else item
                  for item in value] for key, value in body.items()}
    _assert_client_fault(
        flask_client.put('/api/v4/project/paths', json=body),
        400, message.format(**names))
    assert calls in ([], [body['paths']])


def test_project_browse_parse_fault_is_a_redacted_500(flask_client, monkeypatch):
    import routes.api_v1.project as route
    monkeypatch.setattr(route, 'normalize_browse_request', _raise(TypeError(_SECRET)))
    _assert_redacted_500(flask_client.post('/api/v4/project/browse', json={}))


def test_project_browse_non_string_path_keeps_its_400(flask_client):
    _assert_client_fault(
        flask_client.post('/api/v4/project/browse', json={'path': 7}),
        400, 'path must be a string')


# ── conversation diagnostics ─────────────────────────────────────────

_DIAGNOSTIC_URL = '/api/v4/conversations/conv-taxonomy/diagnostic'


def test_diagnostic_cursor_fault_is_a_redacted_500(flask_client, monkeypatch):
    import lib.conversations.diagnostic as diagnostic
    monkeypatch.setattr(diagnostic, 'diagnostic_payload', _raise(ValueError(_SECRET)))
    _assert_redacted_500(flask_client.get(_DIAGNOSTIC_URL))


@pytest.mark.parametrize(('query', 'message'), [
    ('kind=bogus', 'kind must be header, turn or chunk'),
    ('kind=turn', 'revision and index are required for turn/chunk reads'),
    ('limit=x', 'limit must be a non-negative integer'),
])
def test_diagnostic_malformed_cursor_keeps_its_400(flask_client, query, message):
    _assert_client_fault(
        flask_client.get(_DIAGNOSTIC_URL + '?' + query), 400, message)


# ── image upload ─────────────────────────────────────────────────────

@pytest.mark.parametrize('encoded', ['not base64!!', '\u00e9\u00e9\u00e9\u00e9'])
def test_image_upload_malformed_base64_keeps_its_400(flask_client, encoded):
    _assert_client_fault(
        flask_client.post('/api/images/upload',
                          json={'base64': encoded, 'mediaType': 'image/png'}),
        400, 'Invalid base64 payload')


def test_image_upload_spool_fault_is_a_redacted_500(flask_client, monkeypatch):
    import routes.upload as route

    class BrokenSpool:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def write(self, data):
            raise ValueError(_SECRET)
    monkeypatch.setattr(route, 'tempfile', SimpleNamespace(
        TemporaryFile=lambda **kwargs: BrokenSpool()))
    _assert_redacted_500(flask_client.post(
        '/api/images/upload', json={'base64': 'aGVsbG8=', 'mediaType': 'image/png'}))


# ── server config ────────────────────────────────────────────────────

@pytest.mark.parametrize(('module', 'function', 'body'), [
    ('lib.cost_experiments.cost_experiments', 'normalize_cost_experiment_config',
     {'cost_experiment': {}}),
    ('lib.search.search_gateway', 'prepare_gateway_update',
     {'search': {'gateway': {}}}),
])
def test_server_config_section_fault_is_a_redacted_500(
        flask_client, monkeypatch, module, function, body):
    import importlib
    monkeypatch.setattr(importlib.import_module(module), function,
                        _raise(ValueError(_SECRET)))
    _assert_redacted_500(flask_client.post('/api/v4/server-config', json=body))


@pytest.mark.parametrize(('body', 'message'), [
    ({'cost_experiment': 'on'}, 'cost_experiment: cost_experiment must be a JSON object'),
    ({'cost_experiment': {'traffic_percent': 'many'}},
     'cost_experiment: traffic_percent must be an integer'),
    ({'search': {'gateway': {'sources': 'web'}}}, 'search.gateway: sources must be a list'),
])
def test_server_config_invalid_section_keeps_its_400(flask_client, body, message):
    _assert_client_fault(
        flask_client.post('/api/v4/server-config', json=body), 400, message)
