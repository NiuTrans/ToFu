"""SDK translation envelopes reach the shared executor without losing options."""
from __future__ import annotations

import asyncio
import pytest

pytestmark = pytest.mark.unit


def _exercise(monkeypatch, payload, *, major=4):
    from quart import g
    from lib.api_keys import local_admin_context
    from lib.app.app_factory import create_base_app
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from lib.http_transport.error_handlers import register_http_error_handlers
    from routes.api_v1.agents import api_v1_agents_bp
    from routes.api_v1 import translate

    submitted = []
    monkeypatch.setattr(translate, '_cleanup_translate_tasks', lambda: None)
    monkeypatch.setattr(translate._translate_runtime, 'create',
                        lambda **kwargs: {'id': kwargs['task_id']})
    def submit(*args, **kwargs):
        submitted.append((args, kwargs))
        return True
    monkeypatch.setattr(translate, 'submit_translation_task', submit)
    app = create_base_app(__name__, {'TESTING': True})
    register_http_error_handlers(app)

    @app.before_request
    async def grant():
        g.auth_ctx = local_admin_context()
        g.rate_decision = None

    bp = api_v1_agents_bp if major == 1 else attach_v4_envelope(
        copy_blueprint_to_v4(api_v1_agents_bp, name='translation_envelope_v4'))
    app.register_blueprint(bp)
    async def post():
        response = await app.test_client().post(
            f'/api/v{major}/agents/translate', json=payload)
        return response.status_code, await response.get_json()
    return (*asyncio.run(post()), submitted)


@pytest.mark.parametrize('major', [1, 4])
def test_sdk_language_fields_reach_shared_translation_worker(monkeypatch, major):
    status, body, submitted = _exercise(monkeypatch, {
        'text': 'hello', 'source_lang': 'English', 'target_lang': 'Chinese',
    }, major=major)
    assert status == 200, body
    assert len(submitted) == 1
    args, kwargs = submitted[0]
    assert args[4:7] == ('hello', 'Chinese', 'English')
    assert kwargs['user_id'] == 1


@pytest.mark.parametrize('field,value', [
    ('source_lang', 123), ('target_lang', []), ('unknown', True),
    ('model', 'explicit-model'),
])
def test_invalid_or_unsupported_options_never_start_task(monkeypatch, field, value):
    status, body, submitted = _exercise(monkeypatch, {'text': 'hello', field: value})
    assert status == 400, body
    assert submitted == []
