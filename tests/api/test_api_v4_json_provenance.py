"""Response provenance never changes the v4 serialized representation contract."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from quart import Blueprint, Quart, Response, jsonify
from quart.json.provider import DefaultJSONProvider

from lib import api_v4
from lib.api_response import api_created, api_ok, api_payload

pytestmark = pytest.mark.unit


@pytest.fixture
def decode_calls(monkeypatch):
    calls = []

    def loads(value):
        calls.append(value)
        return json.loads(value)

    # Observe this boundary without changing Quart's provider or test decoding.
    monkeypatch.setattr(api_v4, 'json', SimpleNamespace(loads=loads, dumps=json.dumps))
    monkeypatch.setattr(api_v4, 'req_id', lambda: 'projection-request')
    return calls


async def _project(app, producer, *, headers=None, mutate=None, method='GET'):
    async with app.test_request_context('/api/v4/value', method=method, headers=headers):
        response, status = producer()
        response.status_code = status
        if mutate is not None:
            mutate(response)
        original = await response.get_data()
        response = await api_v4.translate_v4_envelope(response)
        return response, original, await response.get_data()


def _assert_envelope_matches_original(original, actual):
    meta = json.loads(actual)['meta']
    assert meta['requestId'] == 'projection-request'
    assert type(meta['serverTimeMs']) is int
    expected = api_v4.success_envelope(json.loads(original),
                                      request_id=meta['requestId'],
                                      server_time_ms=meta['serverTimeMs'])
    assert actual == json.dumps(expected, ensure_ascii=False,
                                separators=(',', ':')).encode()


@pytest.mark.parametrize('helper,status', [(api_ok, 200), (api_created, 201), (api_payload, 200)])
@pytest.mark.parametrize('sort_keys', [False, True])
def test_owned_immutable_payload_preserves_exact_encoding_without_decode(
        helper, status, sort_keys, decode_calls):
    app = Quart(__name__)
    app.json.sort_keys = sort_keys
    value = {'z': '中文\n𝄞', 'n': None, 'yes': True, 'integer': 2 ** 100,
             'negative_zero': -0.0, 'nan': float('nan'), 'infinity': float('inf'),
             'negative_infinity': float('-inf'), 'a': 'end'}
    response, original, actual = asyncio.run(_project(app, lambda: helper(value)))
    assert response.status_code == status
    _assert_envelope_matches_original(original, actual)
    assert decode_calls == []
    if status == 200:
        assert response.headers['ETag'] == 'W/"' + hashlib.sha256(original).hexdigest()[:32] + '"'


@pytest.mark.parametrize('value', [
    {'items': [{'id': 1}, {'id': 2}]},
    {'date': datetime(2026, 10, 29, tzinfo=timezone.utc),
     'uuid': UUID('00000000-0000-0000-0000-000000000001'), 'decimal': Decimal('1.20')},
    {'tuple': (1, 2, 3)},
    {1: 'first key', '1': 'same serialized key'},
])
def test_provider_normalized_or_nested_values_keep_decoding(value, decode_calls):
    app = Quart(__name__)
    app.json.sort_keys = False
    _, original, actual = asyncio.run(_project(app, lambda: api_ok(value)))
    _assert_envelope_matches_original(original, actual)
    assert decode_calls == [original]


@pytest.mark.parametrize('value,expected', [
    ({'text': '\ud834\udd1e'}, {'text': '\U0001d11e'}),
    ({'\ud834\udd1e': 'first', '\U0001d11e': 'last'}, {'\U0001d11e': 'last'}),
])
def test_explicit_surrogate_pairs_keep_json_normalization(value, expected, decode_calls):
    app = Quart(__name__)
    _, original, actual = asyncio.run(_project(app, lambda: api_ok(value)))
    _assert_envelope_matches_original(original, actual)
    assert json.loads(actual)['data'] == expected
    assert decode_calls == [original]


def test_unpaired_surrogate_keeps_final_encoding_failure(decode_calls):
    app = Quart(__name__)
    with pytest.raises(UnicodeEncodeError):
        asyncio.run(_project(app, lambda: api_ok(text='\ud834')))
    assert len(decode_calls) == 1


def test_scalar_subclasses_and_response_subclasses_use_framework_fallback(decode_calls):
    app = Quart(__name__)

    class CustomString(str):
        pass

    _, original, actual = asyncio.run(_project(app, lambda: api_ok(text=CustomString('value'))))
    _assert_envelope_matches_original(original, actual)
    assert decode_calls == [original]
    decode_calls.clear()

    class CustomResponse(Response):
        pass

    app.response_class = CustomResponse
    _, original, actual = asyncio.run(_project(app, lambda: api_ok(text='value')))
    _assert_envelope_matches_original(original, actual)
    assert decode_calls == [original]


def test_cyclic_payload_keeps_provider_failure():
    app = Quart(__name__)
    cycle = []
    cycle.append(cycle)

    async def run():
        async with app.app_context():
            with pytest.raises(ValueError, match='Circular reference'):
                api_ok(items=cycle)

    asyncio.run(run())


def test_borrowed_alias_mutation_cannot_change_already_serialized_payload(decode_calls):
    app = Quart(__name__)
    shared = [{'text': 'before'}]
    value = {'left': shared, 'right': shared}

    def mutate(_response):
        shared[0]['text'] = 'after'
        shared.append({'text': 'new'})
        value['left'] = []

    _, original, actual = asyncio.run(_project(app, lambda: api_ok(value), mutate=mutate))
    _assert_envelope_matches_original(original, actual)
    assert json.loads(actual)['data'] == {'left': [{'text': 'before'}],
                                        'right': [{'text': 'before'}]}
    assert decode_calls == [original]


@pytest.mark.parametrize('mutate_body', ['set_data', 'data_body', 'range'])
def test_changed_response_bytes_invalidate_retained_semantics(mutate_body, decode_calls):
    app = Quart(__name__)
    replacement = b'{"ok":true,"text":"changed"}'

    def mutate(response):
        if mutate_body == 'set_data':
            response.set_data(replacement)
        elif mutate_body == 'data_body':
            response.response.data = replacement
            response.response.end = len(replacement)
        else:
            response.response.data = b'[' + replacement + b']'
            response.response.begin = 1
            response.response.end = len(replacement) + 1

    _, original, actual = asyncio.run(_project(app, lambda: api_ok(text='original'), mutate=mutate))
    assert original == replacement
    _assert_envelope_matches_original(original, actual)
    assert decode_calls == [replacement]


@pytest.mark.parametrize('provider_kind', [
    'subclass', 'instance_dumps', 'instance_response', 'class_dumps', 'class_response', 'response_factory',
])
def test_custom_provider_output_remains_authoritative(provider_kind, decode_calls, monkeypatch):
    app = Quart(__name__)

    class CustomProvider(DefaultJSONProvider):
        def dumps(self, obj, **kwargs):
            return super().dumps({'rewritten': obj['text']}, **kwargs)

    if provider_kind == 'subclass':
        app.json = CustomProvider(app)
    elif provider_kind == 'instance_dumps':
        original_dumps = app.json.dumps
        app.json.dumps = lambda obj, **kwargs: original_dumps({'rewritten': obj['text']}, **kwargs)
    elif provider_kind == 'instance_response':
        app.json.response = lambda obj: Response('{"rewritten":"source"}', mimetype='application/json')
    elif provider_kind == 'class_dumps':
        original_dumps = DefaultJSONProvider.dumps
        monkeypatch.setattr(DefaultJSONProvider, 'dumps',
                            lambda self, obj, **kwargs: original_dumps(self, {'rewritten': obj['text']}, **kwargs))
    elif provider_kind == 'class_response':
        monkeypatch.setattr(DefaultJSONProvider, 'response',
                            lambda self, obj: Response('{"rewritten":"source"}', mimetype='application/json'))
    else:
        app.response_class = lambda *args, **kwargs: Response('{"rewritten":"source"}', mimetype='application/json')
    _, original, actual = asyncio.run(_project(app, lambda: api_ok(text='source')))
    _assert_envelope_matches_original(original, actual)
    assert json.loads(actual)['data'] == {'rewritten': 'source'}
    assert decode_calls == [original]


@pytest.mark.parametrize('helper_owned', [False, True])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_matching_validator_skips_only_trusted_json_decode(helper_owned, method, decode_calls):
    app = Quart(__name__)
    value = {'items': [{'id': index} for index in range(4)]}
    producer = (lambda: api_ok(value)) if helper_owned else (lambda: (jsonify(ok=True, **value), 200))
    first, _, _ = asyncio.run(_project(app, producer, method=method))
    decode_calls.clear()
    response, original, actual = asyncio.run(_project(
        app, producer, headers={'If-None-Match': first.headers['ETag']}, method=method))
    assert response.status_code == 304
    assert actual == b''
    assert response.headers['ETag'] == first.headers['ETag']
    assert response.headers['Cache-Control'] == 'private, no-cache'
    assert 'Content-Type' not in response.headers and 'Content-Length' not in response.headers
    assert decode_calls == ([] if helper_owned else [original])


@pytest.mark.parametrize('source', ['raw', 'changed_helper', 'custom_provider'])
def test_invalid_json_never_gets_conditional_headers(source, decode_calls):
    app = Quart(__name__)
    invalid = b'{invalid json'
    if source == 'raw':
        producer = lambda: (Response(invalid, mimetype='application/json'), 200)
    elif source == 'changed_helper':
        def producer():
            response, status = api_ok(text='source')
            response.set_data(invalid)
            return response, status
    else:
        app.json.response = lambda obj: Response(invalid, mimetype='application/json')
        producer = lambda: api_ok(text='source')
    tag = hashlib.sha256(invalid).hexdigest()[:32]
    response, _, actual = asyncio.run(_project(app, producer, headers={'If-None-Match': f'W/"{tag}"'}))
    assert response.status_code == 200 and actual == invalid
    assert 'ETag' not in response.headers and 'Cache-Control' not in response.headers
    assert decode_calls == [invalid]


@pytest.mark.parametrize('payload', [b'\xef\xbb\xbf{"ok":true,"text":"bom"}', b'[1,2,null]', b'null'])
def test_raw_response_json_fallback(payload, decode_calls):
    app = Quart(__name__)
    _, original, actual = asyncio.run(_project(
        app, lambda: (Response(payload, mimetype='application/json'), 200)))
    _assert_envelope_matches_original(original, actual)
    assert decode_calls == [original]


@pytest.mark.parametrize('payload', [None, {}, 'invalid'])
def test_invalid_third_party_body_returns_before_hashing(payload, decode_calls):
    app = Quart(__name__)

    async def get_data():
        return payload

    response = SimpleNamespace(status_code=200, content_type='application/json',
                               headers={}, get_data=get_data)
    returned, original, actual = asyncio.run(_project(app, lambda: (response, 200)))
    assert returned is response and actual == original == payload
    assert response.headers == {}
    assert decode_calls == [payload]


@pytest.mark.parametrize('passthrough', ['raw_handler', 'error', 'no_content', 'native', 'legacy'])
def test_real_request_preserves_undeclared_or_non_success_wire(passthrough, decode_calls):
    app = Quart(__name__)
    if passthrough == 'native':
        bp = api_v4.NativeContractBlueprint('probe', __name__, contract_id='test.native')
    else:
        bp = Blueprint('probe', __name__)
        if passthrough != 'legacy':
            api_v4.attach_v4_envelope(bp)

    async def endpoint():
        if passthrough == 'error':
            return api_payload({'error': 'test'}, status=400)
        if passthrough == 'no_content':
            return '', 204
        return api_ok(text='source')

    if passthrough == 'raw_handler':
        endpoint = api_v4.raw_success_response(endpoint)
    bp.add_url_rule('/api/v4/probe', view_func=endpoint)
    app.register_blueprint(bp)

    async def run():
        response = await app.test_client().get('/api/v4/probe')
        return response.status_code, await response.get_data()

    status, body = asyncio.run(run())
    if passthrough == 'no_content':
        assert status == 204 and body == b''
    elif passthrough == 'error':
        assert status == 400 and json.loads(body)['ok'] is False
    else:
        assert status == 200 and json.loads(body) == {'ok': True, 'text': 'source'}
    assert decode_calls == []


def test_real_request_applies_one_projection_and_request_id(decode_calls):
    app = Quart(__name__)
    bp = api_v4.attach_v4_envelope(Blueprint('probe', __name__))

    @bp.get('/api/v4/probe')
    async def endpoint():
        return api_ok(text='source')

    app.register_blueprint(bp)

    async def run():
        response = await app.test_client().get('/api/v4/probe')
        return await response.get_json()

    result = asyncio.run(run())
    assert result['data'] == {'text': 'source'}
    assert result['meta']['requestId'] == 'projection-request'
    assert decode_calls == []
