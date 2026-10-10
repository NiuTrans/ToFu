"""Response media and raw downloads share an explicit runtime/docs boundary."""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap

import pytest
from quart import Blueprint, Response

from lib.api_response import api_bad_request, sse_response
from lib.api_v4 import attach_v4_envelope, raw_success_response
from lib.app_factory import create_base_app
from lib.openapi import api_meta, build_spec, media_responses
from lib.openapi._paths import _flask_to_openapi_path
from scripts.gen_api_endpoint_index import route_app

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def delivery_surface():
    app = route_app()
    return app, build_spec(app)


def test_registered_stream_and_file_handlers_declare_their_media(delivery_surface):
    """A new direct stream/file producer cannot silently become JSON docs."""
    app, spec = delivery_surface
    file_calls = {'send_file', 'send_file_conditional', 'send_leased_artifact',
                  'send_from_directory'}
    for rule in app.url_map.iter_rules():
        path = _flask_to_openapi_path(rule.rule)
        if path not in spec['paths']:
            continue
        view = app.view_functions[rule.endpoint]
        source = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(view))))
        calls = {n.func.id for n in ast.walk(source)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        for method in set(rule.methods or ()) - {'HEAD', 'OPTIONS'}:
            op = spec['paths'][path][method.lower()]
            content = op['responses'].get('200', {}).get('content', {})
            if 'sse_response' in calls:
                assert 'text/event-stream' in content, rule.endpoint
            if calls & file_calls:
                assert content and set(content) != {'application/json'}, rule.endpoint
                assert getattr(view, 'tofu_raw_success_response', False), rule.endpoint


@pytest.mark.parametrize('path', ['/api/v4/agent/run', '/v1/chat/completions', '/v1/messages'])
def test_mixed_stream_and_json_successes_remain_distinct(delivery_surface, path):
    _, spec = delivery_surface
    responses = spec['paths'][path]['post']['responses']
    assert set(responses['200']['content']) == {'application/json', 'text/event-stream'}
    if path.startswith('/api/v4/'):
        assert responses['200']['content']['application/json']['schema']['required'] == ['data', 'meta']
        assert '202' in responses
    assert responses['200']['content']['text/event-stream']['schema'] == {'type': 'string'}


def test_json_file_schema_is_raw_and_range_responses_have_their_own_status(delivery_surface):
    _, spec = delivery_surface
    for path in [
        '/api/v4/paper/builds/{document_id}/{build_id}/artifacts/{name}',
        '/api/v4/motion/videos/{task_id}/file',
    ]:
        op = spec['paths'][path]['get']
        assert op['x-tofu-raw-success-response'] is True
        for status in ('200', '206'):
            assert op['responses'][status]['content']['application/json']['schema'] == {}
        assert 'content' not in op['responses']['304']
        assert set(op['responses']['416']['content']) == {'application/problem+json'}


@pytest.mark.parametrize('status', [200, 206])
def test_raw_json_download_preserves_exact_bytes_headers_and_never_buffers(status):
    app = create_base_app('raw-json-download', {'TESTING': True})
    bp = Blueprint('download', __name__)
    body = b'{ "ok": true, "unicode": "\\u4e2d" }\n'

    @bp.get('/api/v4/download')
    @api_meta(responses=media_responses('application/json', conditional=True))
    @raw_success_response
    async def download():
        response = Response(body, status=status, content_type='application/json', headers={
            'Content-Disposition': 'attachment; filename="sources.json"',
            'Content-Length': str(len(body)),
            'Content-Range': f'bytes 0-{len(body)-1}/{len(body)}',
        })
        async def forbidden_buffer(*_args, **_kwargs):
            raise AssertionError('The envelope boundary buffered a download')
        response.get_data = forbidden_buffer
        return response

    app.register_blueprint(attach_v4_envelope(bp))
    async def check():
        response = await app.test_client().get('/api/v4/download')
        assert response.status_code == status
        assert await response.get_data() == body
        assert response.headers['Content-Length'] == str(len(body))
        assert response.headers['Content-Range'] == f'bytes 0-{len(body)-1}/{len(body)}'
        assert response.headers['Content-Disposition'].endswith('"sources.json"')
    asyncio.run(check())


def test_raw_success_declaration_preserves_problem_failures():
    app = create_base_app('raw-download-error', {'TESTING': True})
    bp = Blueprint('download_error', __name__)

    @bp.get('/api/v4/download')
    @raw_success_response
    async def download():
        return api_bad_request('invalid artifact')

    app.register_blueprint(attach_v4_envelope(bp))
    async def check():
        response = await app.test_client().get('/api/v4/download')
        assert response.status_code == 400
        assert response.mimetype == 'application/problem+json'
        assert (await response.get_json())['status'] == 400
    asyncio.run(check())


def test_sse_bytes_proxy_headers_and_timeout_survive_release_projection():
    app = create_base_app('sse-media', {'TESTING': True})
    bp = Blueprint('events', __name__)

    @bp.get('/api/v4/events')
    @api_meta(responses=media_responses('text/event-stream'))
    async def events():
        async def generate():
            yield b'data: {"stage":"done","ok":true}\n\n'
        response = sse_response(generate(), extra_headers={'Content-Encoding': 'identity'})
        assert response.timeout is not None
        return response

    app.register_blueprint(attach_v4_envelope(bp))
    async def check():
        response = await app.test_client().get('/api/v4/events')
        assert response.mimetype == 'text/event-stream'
        assert await response.get_data() == b'data: {"stage":"done","ok":true}\n\n'
        assert response.headers['Cache-Control'] == 'no-cache, no-transform'
        assert response.headers['X-Accel-Buffering'] == 'no'
        assert response.headers['Content-Encoding'] == 'identity'
    asyncio.run(check())


def test_public_shell_documents_html_without_bearer_auth(delivery_surface):
    _, spec = delivery_surface
    for path in ('/login', '/signup', '/dashboard', '/admin', '/favicon.svg'):
        op = spec['paths'][path]['get']
        assert op['security'] == []
        expected = 'image/svg+xml' if path.endswith('.svg') else 'text/html'
        assert set(op['responses']['200']['content']) == {expected}


@pytest.mark.parametrize('status, body', [(200, 'success'), (400, 'fail')])
def test_alipay_notification_keeps_provider_literals_and_documents_both_error_media(monkeypatch, status, body):
    from lib.api_v4 import copy_blueprint_to_v4
    from routes.api_v1 import billing
    from lib.billing import payments
    submitted = []
    def notify(form):
        submitted.append(form)
        return status, body
    monkeypatch.setattr(payments, 'handle_alipay_notify', notify)
    app = create_base_app('alipay-wire', {'TESTING': True})
    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(billing.api_v1_billing_bp, name='billing_wire_v4')))
    url = '/api/v4/billing/webhooks/alipay'
    spec = build_spec(app)
    operation = spec['paths'][url]['post']
    assert operation['security'] == []
    assert set(operation['requestBody']['content']) == {'application/x-www-form-urlencoded'}
    assert operation['responses']['200']['content']['text/plain']['schema']['const'] == 'success'
    failure = operation['responses']['400']['content']
    assert set(failure) == {'text/plain', 'application/problem+json'}
    assert failure['text/plain']['schema']['const'] == 'fail'
    async def check():
        response = await app.test_client().post(url, form={'sign': 'signature', 'out_trade_no': 'order'})
        assert response.status_code == status
        assert response.mimetype == 'text/plain'
        assert await response.get_data() == body.encode()
    asyncio.run(check())
    assert submitted == [{'sign': 'signature', 'out_trade_no': 'order'}]
