"""Request ingress encodes provider JSON once; identities stay byte-identical.

The reference functions freeze the earlier algorithms: detachment by a JSON
round trip, a second encode for the provider-prefix identity and a decoded
wire body for transport diagnostics. Pinned digests guard the identity
contract independently of both implementations.
"""
from __future__ import annotations

from collections import OrderedDict
import enum
import hashlib
import json
from types import MappingProxyType

import pytest
from hypothesis import given, settings, strategies as st

import lib.llm.request as request_module
from lib.llm._sse_core import prepare_request
from lib.llm.llm_errors import LocalRequestPreparationError
from lib.llm.request import (
    AdmittedRequest, detach_input, seal_request, semantic_request_id,
    strip_internal_request_fields,
)

pytestmark = pytest.mark.unit

URL = 'https://example.invalid/v1'
PREFIX_FIELDS = ('model', 'messages', 'input', 'system', 'instructions', 'tools',
                 'prompt_cache_key')


def _reference_encode(value, *, sorted_keys=False):
    return json.dumps(value, ensure_ascii=False, sort_keys=sorted_keys,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _reference_detach(body):
    for key in ('messages', 'system', 'tools'):
        if key in body:
            body[key] = json.loads(request_module._encode(body[key]))


def _reference_semantic_id(body):
    return hashlib.sha256(_reference_encode(
        {key: body.get(key) for key in ('model', 'messages', 'system', 'tools')},
        sorted_keys=True)).hexdigest()


def _reference_seal(body, *, model, provider_id='', protocol='openai', endpoint='',
                    input_tokens, semantic_id, logical_model='', content_encoding=''):
    wire = strip_internal_request_fields(body)
    chunks, offsets, position = [b'{'], {}, 1
    for key, value in wire.items():
        header = (b',' if offsets else b'') + request_module._encode(key) + b':'
        encoded = request_module._encode(value)
        start = position + len(header)
        position = start + len(encoded)
        scalar = value is None or isinstance(value, (str, int, float, bool))
        offsets[key] = (start, position, scalar, value if scalar else None)
        chunks.extend((header, encoded))
    chunks.append(b'}')
    prefix = {key: value for key, value in wire.items() if key in PREFIX_FIELDS}
    prefix_id = hashlib.sha256(request_module._encode(
        [model, provider_id, protocol, endpoint, prefix])).hexdigest()
    return AdmittedRequest(b''.join(chunks), model, provider_id, protocol, endpoint,
                           input_tokens, semantic_id, prefix_id,
                           MappingProxyType(offsets), logical_model or model,
                           content_encoding=content_encoding)


def _image(seed):
    return 'data:image/png;base64,' + ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB' + seed) * 48


def representative_body(model='gpt-4o', *, history=1):
    """Multi-turn input with tools, images, reasoning and escaped text."""
    messages = [{'role': 'system', 'content': 'You are terse.\n规则: 不要编造。'}]
    for turn in range(history):
        messages += [
            {'role': 'user', 'content': [
                {'type': 'text', 'text': f'Turn {turn}: compare "both" images\\n\t\u2028 🙂'},
                {'type': 'image_url', 'image_url': {'url': _image(str(turn)), 'detail': 'high'}},
                {'type': 'image_url', 'image_url': {'url': _image('B')}}]},
            {'role': 'assistant', 'content': f'Checking file {turn}.',
             'reasoning_content': f'  Think {turn}: compare 上下文 widths\n',
             'tool_calls': [{'id': f'call_{turn}', 'type': 'function', 'function': {
                 'name': 'read_files',
                 'arguments': json.dumps({'path': f'lib/{turn}.py', 'limit': 200})}}]},
            {'role': 'tool', 'tool_call_id': f'call_{turn}',
             'content': f'def f{turn}():\n    return "\\x00\x07"\n' * 8},
            {'role': 'assistant', 'content': f'Done: {turn} differences.',
             'reasoning_content': 'second pass'},
        ]
    messages.append({'role': 'user', 'content': [{'type': 'text', 'text': 'continue'}]})
    return {
        'model': model, 'stream': True, 'max_tokens': 4096, 'temperature': 0.25,
        'messages': messages,
        'tools': [{'type': 'function', 'function': {
            'name': 'read_files', 'description': 'Read files. 读取文件。',
            'parameters': {'type': 'object', 'properties': {
                'path': {'type': 'string', 'description': 'Relative path'},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10 ** 18,
                          'default': 200},
                'ratio': {'type': 'number', 'default': -0.0, 'maximum': 1e-07},
                'mode': {'type': 'string', 'enum': ['head', 'tail']}},
                'required': ['path'], 'additionalProperties': False}}}],
        'tool_choice': 'auto',
        '_admitted_input_tokens': 1234,
    }


def chat_wire():
    body = representative_body()
    wire = {key: value for key, value in body.items() if not key.startswith('_')}
    wire['messages'] = [dict(message) for message in body['messages']]
    wire['messages'][-1]['content'] = 'continue'
    return wire


def anthropic_wire():
    return {
        'model': 'claude-sonnet-4-6', 'max_tokens': 8192, 'stream': True,
        'system': [{'type': 'text', 'text': 'Stable system 规则',
                    'cache_control': {'type': 'ephemeral'}}],
        'messages': [
            {'role': 'user', 'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                                             'data': _image('C')[22:]}},
                {'type': 'text', 'text': 'What changed?'}]},
            {'role': 'assistant', 'content': [
                {'type': 'thinking', 'thinking': 'compare', 'signature': 'c2lnbmF0dXJl'},
                {'type': 'tool_use', 'id': 'toolu_1', 'name': 'read_files',
                 'input': {'path': 'a.py', 'limit': 2.5}}]},
            {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'ok "quoted"'}]}],
        'tools': [{'name': 'read_files', 'description': 'Read',
                   'input_schema': {'type': 'object', 'properties': {}}}],
        'thinking': {'type': 'enabled', 'budget_tokens': 2048},
        '_conversion_report': {'losses': []},
    }


def responses_wire():
    return {
        'model': 'gpt-5.6', 'instructions': 'Be exact.\nNo guessing.',
        'input': [
            {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'hello 世界'},
                {'type': 'input_image', 'image_url': _image('D'), 'detail': 'auto'}]},
            {'type': 'reasoning', 'id': 'rs_1', 'summary': [],
             'encrypted_content': 'gAAAAB' + 'x' * 64},
            {'type': 'function_call', 'call_id': 'call_9', 'name': 'lookup',
             'arguments': '{"q":"needle"}'},
            {'type': 'function_call_output', 'call_id': 'call_9', 'output': 'found'}],
        'tools': [{'type': 'function', 'name': 'lookup', 'strict': False,
                   'parameters': {'type': 'object', 'properties': {'q': {'type': 'string'}}}}],
        'prompt_cache_key': 'task-1', 'stream': True, 'store': False,
        'reasoning': {'effort': 'high', 'summary': 'auto'},
        'include': ['reasoning.encrypted_content'], 'max_output_tokens': 32000,
    }


# sha256(payload), provider_prefix_id — produced before single-encode sealing.
PINNED_SEALS = {
    'chat': ('b0cae4e8de3b581f7b2d75db2e1d44a4e691b3a0f948895bf0b347f6940bc900',
             '214edcdf4d409d1715d48e8156395df9a5cd983d5a9bc026c8cccdeac6575f7b'),
    'anthropic': ('45f65770a59138babb46b95c8330d7ef8f6ae755c84963d94c8caaab44bed962',
                  'c225088ae978a9fba03a6cc34a156ea8b1876b46b32460b4449a528dfe53336e'),
    'responses': ('811ed1b6e15ef18ea68d8ee0d1548bcb752e0df00617c1cc1f90e07a94e62d40',
                  '6d6de46343616af2b23630f0710d23c0a274784cb648a82330d758240dad3452'),
}
PINNED_SEMANTIC_ID = '1a46ae659cc72dae9e2b1b4a378e08f60963713bc7c50b9bfc9afb0023877ad9'
SEAL_CASES = {
    'chat': (chat_wire, dict(model='gpt-4o', provider_id='openai-main',
                             protocol='openai', endpoint=URL + '/chat/completions')),
    'anthropic': (anthropic_wire, dict(model='claude-sonnet-4-6', provider_id='anthropic',
                                       protocol='anthropic', endpoint=URL + '/messages')),
    'responses': (responses_wire, dict(model='gpt-5.6', provider_id='',
                                       protocol='responses', endpoint=URL + '/responses')),
}


@pytest.mark.parametrize('case', sorted(SEAL_CASES))
def test_sealed_payload_and_prefix_identity_are_pinned(case):
    build, route = SEAL_CASES[case]
    sealed = seal_request(build(), input_tokens=7, semantic_id='s', **route)
    reference = _reference_seal(build(), input_tokens=7, semantic_id='s', **route)
    assert sealed.payload == reference.payload
    assert sealed.provider_prefix_id == reference.provider_prefix_id
    assert dict(sealed._fields) == dict(reference._fields)
    assert (hashlib.sha256(sealed.payload).hexdigest(),
            sealed.provider_prefix_id) == PINNED_SEALS[case]


def test_semantic_identity_is_pinned():
    body = representative_body()
    detach_input(body)
    assert semantic_request_id(body) == PINNED_SEMANTIC_ID
    assert semantic_request_id(body) == _reference_semantic_id(representative_body())


def _shape(value):
    """Type-exact structural view: 1, 1.0, True and tuples never compare equal."""
    kind = type(value)
    if kind is list:
        return ('list', [_shape(item) for item in value])
    if kind is dict:
        return ('dict', [(_shape(key), _shape(item)) for key, item in value.items()])
    if kind is float:
        return ('float', repr(value))
    return (kind.__name__, value)


def _containers(value):
    if isinstance(value, (list, dict)):
        yield id(value)
        for item in (value.values() if isinstance(value, dict) else value):
            yield from _containers(item)


json_values = st.recursive(
    st.none() | st.booleans() | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False) | st.text(),
    lambda values: st.lists(values, max_size=6)
    | st.dictionaries(st.text(), values, max_size=6),
    max_leaves=40)


@settings(max_examples=150, deadline=None)
@given(json_values, json_values)
def test_detach_equals_json_round_trip_and_owns_every_container(messages, tools):
    source = {'messages': messages, 'tools': tools, 'system': 'shared'}
    detached, reference = dict(source), dict(source)
    detach_input(detached)
    _reference_detach(reference)
    for key in ('messages', 'tools', 'system'):
        assert _shape(detached[key]) == _shape(reference[key])
    assert not set(_containers(detached)) & set(_containers(source))


class Color(enum.StrEnum):
    RED = 'red'


class Level(enum.IntEnum):
    HIGH = 3


class Ratio(float):
    pass


class Tagged(dict):
    pass


@pytest.mark.parametrize('carrier', [
    ('tuple', ['x', 1.5]),
    [Color.RED, Level.HIGH, Ratio(0.25)],
    {1: 'int key', False: 'bool key', None: 'none', 2.5: 'float key'},
    {'nested': OrderedDict([('b', 1), ('a', (2, 3))])},
    [Tagged(z=1, a=[Tagged()])],
    {'mixed': {1: 'a', 'b': 2}},
], ids=['tuple', 'scalar-subclasses', 'non-string-keys', 'ordered', 'dict-subclass',
        'mixed-keys'])
def test_detach_normalizes_foreign_carriers_exactly_like_json(carrier):
    detached = {'messages': [{'role': 'user', 'content': carrier}]}
    reference = {'messages': [{'role': 'user', 'content': carrier}]}
    detach_input(detached)
    _reference_detach(reference)
    assert _shape(detached['messages']) == _shape(reference['messages'])
    assert semantic_request_id(detached) == _reference_semantic_id(reference)


def test_detach_shares_exact_settled_rows_and_round_trips_the_rest():
    from lib.conversation_sync.settled_rows import is_settled, settle
    exact = settle({'role': 'user', 'content': [{'type': 'text', 'text': 'é 1.5'}],
                    'weight': 1.5})
    inexact = settle({'role': 'user', 'content': ('tuple', 1.5), 'level': Level.HIGH})
    tools = settle([{'type': 'function'}])
    body = {'messages': [exact, inexact], 'tools': tools}
    reference = {'messages': [exact, inexact], 'tools': tools}
    detach_input(body)
    _reference_detach(reference)
    assert body['messages'][0] is exact and exact == reference['messages'][0]
    assert body['tools'] is tools
    assert not is_settled(body['messages'][1])
    assert _shape(body['messages'][1]) == _shape(reference['messages'][1])


@pytest.mark.parametrize('shape', ['all-settled', 'mixed', 'repeated-row', 'empty'])
def test_semantic_identity_over_settled_messages_equals_one_encode(shape):
    from lib.conversation_sync.settled_rows import settle
    rows = settle(representative_body(history=2)['messages'])
    messages = {'all-settled': list(rows),
                'mixed': [rows[0], {'role': 'user', 'content': 'plain'}, *rows[1:]],
                'repeated-row': [rows[1], rows[1], rows[2], rows[1]],
                'empty': []}[shape]
    body = {'model': 'gpt-4o', 'messages': messages, 'system': None,
            'tools': representative_body()['tools']}
    # Twice: the second walk resumes from digest states memoized on the rows.
    assert semantic_request_id(body) == _reference_semantic_id(body)
    assert semantic_request_id(body) == _reference_semantic_id(body)
    assert semantic_request_id(dict(body, model='other')) == _reference_semantic_id(
        dict(body, model='other'))


def _cycle():
    value = []
    value.append(value)
    return value


@pytest.mark.parametrize('bad', [
    float('nan'), float('inf'), {'set'}, object(), '\ud800', 10 ** 5000, _cycle(),
], ids=['nan', 'inf', 'set', 'object', 'surrogate', 'huge-int', 'cycle'])
def test_invalid_json_is_rejected_before_counting(bad):
    body = {'model': 'm', 'messages': [{'role': 'user', 'content': 'ok'}],
            'tools': [{'parameters': {'x-extension': bad}}]}
    with pytest.raises(LocalRequestPreparationError) as raised:
        detach_input(body)
        semantic_request_id(body)
    assert raised.value.stage == 'request_serialization'
    assert 'set' not in str(raised.value)


@pytest.mark.parametrize('depth', [50, 1200, 5000])
def test_deep_nesting_matches_reference_outcome(depth):
    def nested():
        value = 'leaf'
        for _ in range(depth):
            value = [value]
        return value

    outcomes = []
    for detach in (detach_input, _reference_detach):
        body = {'messages': nested()}
        try:
            detach(body)
            outcomes.append(('ok', _reference_encode(body['messages'])))
        except LocalRequestPreparationError as error:
            outcomes.append(('error', error.stage))
    assert outcomes[0] == outcomes[1]


wire_keys = st.sampled_from(PREFIX_FIELDS + ('stream', 'max_tokens', 'temperature',
                                             'metadata', 'reasoning', '引用'))


@settings(max_examples=150, deadline=None)
@given(st.dictionaries(wire_keys, json_values, max_size=8),
       st.text(), st.text(), st.sampled_from(['openai', 'anthropic', 'responses']),
       st.text())
def test_prefix_identity_equals_second_encode(wire, model, provider_id, protocol, endpoint):
    route = dict(model=model, provider_id=provider_id, protocol=protocol, endpoint=endpoint)
    sealed = seal_request(dict(wire), input_tokens=1, semantic_id='s', **route)
    reference = _reference_seal(dict(wire), input_tokens=1, semantic_id='s', **route)
    assert sealed.payload == reference.payload
    assert sealed.provider_prefix_id == reference.provider_prefix_id
    assert dict(sealed) == json.loads(reference.payload)


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr('lib.token_counter.count_tokens', lambda *a, **k: {'tokens': 1234})
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens.resolve_model_context_limit',
                        lambda *a, **k: 1_000_000)


CASES = [('openai', 'gpt-4o'), ('responses', 'gpt-4o'), ('anthropic', 'gpt-4o'),
         ('openai', 'claude-sonnet-4-6'), ('anthropic', 'claude-sonnet-4-6')]


def _decoded_diagnostics(wire):
    from lib.tasks_pkg.wire_fingerprint import (
        capture_wire_message_evidence, marker_signature, static_prefix_hash,
        system_fingerprint, wire_byte_region,
    )
    items = wire.get('messages') or wire.get('input') or []
    evidence = capture_wire_message_evidence(items)
    return (evidence.canonical, evidence.message_bytes, evidence.field_bytes,
            static_prefix_hash(items), wire_byte_region(wire.get('system'), wire.get('tools')),
            marker_signature(wire), system_fingerprint(wire.get('system'), wire.get('tools')))


def _plan_diagnostics(dumper):
    return (dumper.wire_fp, dumper.wire_bytes, dumper.wire_field_bytes, dumper.wire_static,
            dumper.wire_region, dumper.wire_markers, dumper.wire_system)


@pytest.mark.parametrize('protocol,model', CASES)
def test_prepare_request_matches_reference_pipeline(offline, monkeypatch, protocol, model):
    plan = prepare_request(representative_body(model, history=3), base_url=URL,
                           api_protocol=protocol)
    with monkeypatch.context() as patch:
        patch.setattr(request_module, 'detach_input', _reference_detach)
        patch.setattr(request_module, 'semantic_request_id', _reference_semantic_id)
        patch.setattr(request_module, 'seal_request', _reference_seal)
        reference = prepare_request(representative_body(model, history=3), base_url=URL,
                                    api_protocol=protocol)
    sealed = plan.body
    assert sealed.payload == reference.body.payload
    assert sealed.semantic_id == reference.body.semantic_id
    assert sealed.provider_prefix_id == reference.body.provider_prefix_id
    assert sealed.input_tokens == reference.body.input_tokens
    assert sealed.diagnostics() == plan.raw_dumper.request_identity
    decoded = json.loads(sealed.payload)
    assert plan.raw_dumper.body == decoded
    assert _plan_diagnostics(plan.raw_dumper) == _decoded_diagnostics(decoded)


def test_request_encodes_reuse_configured_encoders(offline):
    """Per-value encodes (one per tool_use input on Messages) build no encoder."""
    import sys
    constructor = json.JSONEncoder.__init__.__code__
    built = []

    def profile(frame, event, _arg):
        if event == 'call' and frame.f_code is constructor:
            caller = frame.f_back
            while caller is not None and caller.f_code.co_filename == json.__file__:
                caller = caller.f_back
            if caller is not None and caller.f_code.co_filename == request_module.__file__:
                built.append(caller.f_code.co_name)

    sys.setprofile(profile)
    try:
        plan = prepare_request(representative_body('claude-sonnet-4-6', history=6),
                               base_url=URL, api_protocol='anthropic')
    finally:
        sys.setprofile(None)
    assert b'"tool_use"' in plan.body.payload
    assert built == []


def _compact(value):
    return _reference_encode(value)


class _EncodeLedger:
    """Request encodes/decodes and every value diagnostics serialize again."""

    def __init__(self, monkeypatch):
        import lib.tasks_pkg.wire_fingerprint as fingerprint_module
        self.encodes, self.decodes, self.diagnostics = [], [], []
        encode, loads = request_module._encode, json.loads
        dump = fingerprint_module._dump_wire_json_value

        def counted_encode(value, *, sorted_keys=False):
            result = encode(value, sorted_keys=sorted_keys)
            self.encodes.append((sorted_keys, len(result)))
            return result

        def counted_loads(value, *args, **kwargs):
            self.decodes.append(len(value))
            return loads(value, *args, **kwargs)

        def counted_dump(value):
            result = dump(value)
            self.diagnostics.append(len(result.encode('utf-8')))
            return result

        monkeypatch.setattr(request_module, '_encode', counted_encode)
        monkeypatch.setattr(json, 'loads', counted_loads)
        monkeypatch.setattr(fingerprint_module, '_dump_wire_json_value', counted_dump)

    def assert_single_encodes(self, payload, *, route, beside_wire=0, reencoded=0):
        """The canonical input is encoded once, sorted, for its identity; the
        wire once, item by item (never whole), beside only the route header
        and ``beside_wire`` bytes; diagnostics serialize only ``reencoded``
        bytes; nothing payload-sized is decoded."""
        half = len(payload) // 2
        sorted_sizes = [size for sorted_keys, size in self.encodes if sorted_keys]
        wire_sizes = [size for sorted_keys, size in self.encodes if not sorted_keys]
        assert len(sorted_sizes) == 1 and sorted_sizes[0] >= half
        assert max(wire_sizes) < half
        route_bytes = sum(len(_compact(part)) for part in route)
        assert sum(wire_sizes) <= len(payload) + route_bytes + beside_wire
        assert sum(self.diagnostics) <= reencoded
        assert [size for size in self.decodes if size >= half] == []


@pytest.mark.parametrize('protocol,model', CASES)
def test_prepare_request_encodes_each_item_once_and_never_decodes_it(
        offline, monkeypatch, protocol, model):
    loads = json.loads
    source = representative_body(model, history=12)
    ledger = _EncodeLedger(monkeypatch)
    plan = prepare_request(source, base_url=URL, api_protocol=protocol)
    sealed = plan.body
    wire = loads(sealed.payload)
    items = wire.get('messages') or wire.get('input')
    # The Messages recount encodes each tool_use input once more; diagnostics
    # serialize only marker-bearing items (detect, then strip).
    tool_inputs = sum(
        len(_compact(block['input'])) for item in items
        for block in (item.get('content') if isinstance(item.get('content'), list) else ())
        if protocol == 'anthropic' and block.get('type') == 'tool_use')
    marked = [item for item in items if b'"cache_control":' in _compact(item)]
    ledger.assert_single_encodes(
        sealed.payload, route=(sealed.model, sealed.provider_id, sealed.protocol,
                               sealed.endpoint),
        beside_wire=tool_inputs,
        reencoded=2 * sum(len(_compact(item)) for item in marked))
    if model == 'gpt-4o':
        assert marked == [] and ledger.diagnostics == []


def test_nonstream_chat_encodes_wire_once(offline, monkeypatch):
    import importlib
    chat_module = importlib.import_module('lib.llm.chat')
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **k: 'direct')
    sent = []

    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}

    def post(url, **kwargs):
        sent.append(kwargs.get('data', kwargs.get('content')))
        return Response()

    monkeypatch.setattr(chat_module, 'http_post', post)
    body = representative_body(history=12)
    ledger = _EncodeLedger(monkeypatch)
    text, _usage = chat_module.chat(body['messages'], 'gpt-4o',
                                    extra={'tools': body['tools']},
                                    base_url=URL, api_key='synthetic',
                                    api_protocol='openai', max_retries=0)
    assert text == 'ok'
    ledger.assert_single_encodes(
        sent[0], route=('gpt-4o', '', 'openai', URL + '/chat/completions'))
