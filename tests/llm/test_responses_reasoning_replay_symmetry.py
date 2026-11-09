"""Encrypted Responses reasoning is declared, retained and replayed by one decision.

Some gateways stream ``reasoning`` items carrying ``encrypted_content`` that the
request never asked for: your-llm-gateway.example.com's Kunlun face serving
``kunlunbedrock.gpt-6.1-sol-m17`` does so whether or not ``include`` names it. A
route that does not declare ``features.responses_reasoning_replay`` can never send
that ciphertext back, and checked conversion refuses unreplayable ciphertext as
mandatory continuity. Retaining it ended every such conversation at the first
request after a reasoning round (TB2.1 ``fix-git`` headless pilot, 2026-11-09:
``protocol_conversion:mandatory_continuity_unrepresentable`` at tool round 4).

Real request preparation, sync/async HTTP shells, SSE accumulation and non-stream
conversion run; only the upstream is a scripted fake (no network, no keys).
"""
import asyncio
import copy
import json
import time

import pytest

from lib.llm.conversion import ProtocolConversionError

pytestmark = pytest.mark.unit

WIRE_MODEL = 'kunlunbedrock.gpt-6.1-sol-m17'
TOOLS = [{'type': 'function', 'function': {
    'name': 'run_command', 'description': 'Run one shell command.',
    'parameters': {'type': 'object', 'properties': {'command': {'type': 'string'}}}}}]
TASK = [{'role': 'user', 'content': 'Recover the lost commit.'}]


def _route_profile(*features):
    """Merged profile of a Kunlun-face Deployment, as dispatch stamps it."""
    return {
        'source': 'official',
        'reasoning': {'support': 'supported', 'wire_format': 'reasoning_effort',
                      'can_disable': False, 'default_effort': 'medium',
                      'supported_efforts': ['low', 'medium', 'high', 'xhigh', 'max']},
        'sampling': {'omit': ['temperature']},
        'responses': {'feature_profile': 'compatible'},
        'features': {'cache_affinity_mt_session_id': 'supported',
                     **{name: 'supported' for name in features}},
    }


# The headless pilot's Connection declared only the gateway header binding.
UNDECLARED = _route_profile()
# bundles/yourprovider/provider_templates/yourprovider.json faces.kunlun.
DECLARED = _route_profile('responses_reasoning_replay')


def _encrypted(round_num):
    return {'type': 'reasoning', 'id': f'rs_{round_num}', 'summary': [],
            'encrypted_content': f'synthetic-ciphertext-{round_num}'}


def _call(round_num):
    return {'type': 'function_call', 'id': f'fc_{round_num}', 'call_id': f'call_{round_num}',
            'name': 'run_command', 'arguments': json.dumps({'command': f'echo {round_num}'}),
            'status': 'completed'}


READABLE = {'type': 'reasoning', 'id': 'rs_readable',
            'summary': [{'type': 'summary_text', 'text': 'Checked the reflog.'}]}


def _sse(output, response_id):
    events = [{'type': 'response.created', 'response': {
        'id': response_id, 'status': 'in_progress', 'output': []}}]
    for index, item in enumerate(output):
        events.append({'type': 'response.output_item.added', 'output_index': index,
                       'item': item})
        events.append({'type': 'response.output_item.done', 'output_index': index,
                       'item': item})
    events.append({'type': 'response.completed', 'response': {
        'id': response_id, 'status': 'completed', 'output': output,
        'usage': {'input_tokens': 100, 'output_tokens': 40,
                  'output_tokens_details': {'reasoning_tokens': 28}}}})
    return [b'data: ' + json.dumps(event).encode() + b'\n\n' for event in events]


class _Upstream:
    """Kunlun-like endpoint: ciphertext in every reply, asked for or not."""

    def __init__(self):
        self.requests = []

    def reply(self, payload):
        self.requests.append(json.loads(payload))
        round_num = len(self.requests)
        return _sse([_encrypted(round_num), _call(round_num)], f'resp_{round_num}')


class _SyncResponse:
    status_code = 200
    encoding = 'utf-8'
    text = ''
    tofu_network_route = {'routeId': 'direct', 'routeMode': 'direct',
                          'decisionReason': 'fixture'}

    def __init__(self, chunks):
        self.headers = {}
        self._chunks = chunks

    def iter_content(self, chunk_size=65536):
        yield from self._chunks

    def close(self):
        pass


class _AsyncResponse:
    status_code = 200

    def __init__(self, chunks):
        self.headers = {}
        self.extensions = {'tofu_network_route': dict(_SyncResponse.tofu_network_route)}
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aread(self):
        return b''


@pytest.fixture
def upstream(monkeypatch):
    import lib.desktop.egress as egress
    import lib.llm.astream as astream
    import lib.llm.stream as stream

    fake = _Upstream()

    class Session:
        @staticmethod
        def post(url, **kwargs):
            return _SyncResponse(fake.reply(kwargs['data']))

    class Client:
        @staticmethod
        def stream(method, url, **kwargs):
            return _AsyncResponse(fake.reply(kwargs['content']))

    monkeypatch.setattr(egress, 'route_request', lambda url, **kwargs: 'direct')
    monkeypatch.setattr(stream, 'get_sync_session', lambda: Session())
    monkeypatch.setattr(astream, 'get_async_client', lambda *args, **kwargs: Client())
    return fake


def _round(messages, profile, *, asynchronous=False):
    """One physical attempt through the real transport shell."""
    from lib.llm import astream, stream
    from lib.llm.stream_result import ensure_provider_stream_result
    body = {'model': WIRE_MODEL, 'messages': copy.deepcopy(messages), 'tools': TOOLS,
            'max_tokens': 1000, 'reasoning_effort': 'medium',
            '_responses_feature_profile': 'compatible',
            '_route_inference_profile': copy.deepcopy(profile)}
    options = {'base_url': 'https://gateway.invalid/v1', 'api_key': 'synthetic-key',
               'api_protocol': 'responses', 'log_prefix': '[replay-symmetry]'}
    if asynchronous:
        result = asyncio.run(astream._async_stream_chat_once(body, **options))
    else:
        result = stream._stream_chat_once(body, **options)
    return ensure_provider_stream_result(result).message


def _converse(profile, *, rounds, asynchronous=False):
    messages = list(TASK)
    for _ in range(rounds):
        message = _round(messages, profile, asynchronous=asynchronous)
        messages.append(message)
        messages.extend({'role': 'tool', 'tool_call_id': call['id'], 'content': 'ok'}
                        for call in message['tool_calls'])
    return messages


def _ciphertexts(message):
    return [item['encrypted_content'] for item in message.get('_responses_items') or ()
            if item.get('type') == 'reasoning' and item.get('encrypted_content')]


def _replayed(request):
    return [item.get('encrypted_content') for item in request['input']
            if item.get('type') == 'reasoning']


# ── Declaration: the wire names exactly the state the reply side keeps ──

@pytest.mark.parametrize('profile,replays', [(UNDECLARED, False), (DECLARED, True)],
                         ids=['route-without-replay', 'route-with-replay'])
def test_wire_declaration_and_retained_state_follow_one_replay_decision(
        upstream, profile, replays):
    message = _round(TASK, profile)
    request = upstream.requests[0]
    assert ('reasoning.encrypted_content' in request.get('include', ())) is replays
    assert (request['reasoning'].get('context') == 'all_turns') is replays
    assert _ciphertexts(message) == (['synthetic-ciphertext-1'] if replays else [])
    assert [call['function']['name'] for call in message['tool_calls']] == ['run_command']


@pytest.mark.parametrize('body,profile,replays', [
    ({'model': WIRE_MODEL, '_responses_feature_profile': 'compatible',
      '_route_inference_profile': UNDECLARED}, 'default', False),
    ({'model': WIRE_MODEL, '_responses_feature_profile': 'compatible',
      '_route_inference_profile': DECLARED}, 'default', True),
    # Legacy callers without a route contract keep the GPT-5.6 stateful gate.
    ({'model': 'gpt-5.6-sol'}, 'default', True),
    ({'model': 'gpt-5.6-sol', '_responses_feature_profile': 'compatible'}, 'default', False),
    # Codex's installed binding replays; an explicit owner narrowing does not.
    ({'model': 'gpt-6-astra', '_route_inference_profile': {
        'reasoning': {'support': 'supported'}}}, 'codex', True),
    ({'model': 'gpt-6-astra', '_route_inference_profile': {
        'features': {'responses_reasoning_replay': 'unsupported'}}}, 'codex', False),
], ids=['compatible-undeclared', 'compatible-declared', 'legacy-gpt56', 'legacy-compatible',
        'codex-installed', 'codex-narrowed'])
def test_one_decision_selects_ciphertext_on_the_wire_and_in_the_reply(body, profile, replays):
    from lib.llm.responses_outbound import (
        ResponsesSSETranslator, openai_body_to_responses, responses_reasoning_replay,
        responses_response_to_openai,
    )
    body = {**body, 'messages': [*TASK, {'role': 'assistant', 'content': 'Looking.',
                                         '_responses_items': [_encrypted(0)]}]}
    if replays:
        payload, _reverse = openai_body_to_responses(body, profile=profile)
        assert _replayed(payload) == ['synthetic-ciphertext-0']
        assert 'reasoning.encrypted_content' in payload['include']
    else:
        with pytest.raises(ProtocolConversionError, match='mandatory_continuity_unrepresentable'):
            openai_body_to_responses(body, profile=profile)
    assert responses_reasoning_replay(body, profile=profile) is replays
    translator = ResponsesSSETranslator(WIRE_MODEL, reasoning_replay=replays)
    for chunk in _sse([_encrypted(1)], 'resp_1'):
        translator.translate(chunk.decode().strip()[len('data: '):])
    nonstream = responses_response_to_openai(
        {'id': 'resp_1', 'status': 'completed', 'output': [_encrypted(1)]},
        reasoning_replay=replays)['choices'][0]['message']
    kept = [_encrypted(1)] if replays else []
    assert translator.response_items == kept
    assert nonstream.get('_responses_items', []) == kept


# ── Storage: stream and non-stream replies keep the same replayable state ──

def _streamed(output, *, replays):
    from lib.llm._sse_core import SSEAccumulator
    from lib.llm.diagnostics import RawSSEDumper
    from lib.llm.responses_outbound import ResponsesSSETranslator
    body = {'model': WIRE_MODEL, 'messages': [], 'tools': TOOLS}
    accumulator = SSEAccumulator(
        body, 'trace-replay-symmetry', RawSSEDumper(WIRE_MODEL, 'trace-replay-symmetry', body),
        ResponsesSSETranslator(WIRE_MODEL, reasoning_replay=replays), time.time())
    for chunk in _sse(output, 'resp_1'):
        accumulator.feed_line(chunk.decode().strip())
    return accumulator.finalize()[0]


@pytest.mark.parametrize('replays', [False, True], ids=['no-replay', 'replay'])
def test_stream_and_nonstream_replies_keep_only_replayable_ciphertext(replays):
    from lib.llm.responses_outbound import responses_response_to_openai
    output = [READABLE, _encrypted(1), _call(1)]
    streamed = _streamed(copy.deepcopy(output), replays=replays)
    direct = responses_response_to_openai(
        {'id': 'resp_1', 'status': 'completed', 'output': copy.deepcopy(output)},
        reasoning_replay=replays)['choices'][0]['message']
    # Summary-only reasoning is metadata, never mandatory continuity: kept as before.
    expected = [READABLE, _encrypted(1)] if replays else [READABLE]
    for message in (streamed, direct):
        assert message['_responses_items'] == expected
        assert message['reasoning_content'] == 'Checked the reflog.'
        assert [call['id'] for call in message['tool_calls']] == ['call_1']


# ── Replay: a multi-tool-call conversation continues on its own route ──

@pytest.mark.parametrize('asynchronous', [False, True], ids=['sync', 'async'])
def test_route_without_replay_completes_a_multi_tool_call_conversation(
        upstream, asynchronous):
    messages = _converse(UNDECLARED, rounds=6, asynchronous=asynchronous)
    assert len(upstream.requests) == 6
    for request in upstream.requests:
        assert 'include' not in request
        assert 'context' not in request['reasoning']
        assert _replayed(request) == []
    assert not any(_ciphertexts(message) for message in messages
                   if message['role'] == 'assistant')


@pytest.mark.parametrize('asynchronous', [False, True], ids=['sync', 'async'])
def test_route_with_replay_round_trips_every_reasoning_item(upstream, asynchronous):
    _converse(DECLARED, rounds=6, asynchronous=asynchronous)
    assert len(upstream.requests) == 6
    for round_num, request in enumerate(upstream.requests, start=1):
        assert request['include'] == ['reasoning.encrypted_content']
        assert request['reasoning']['context'] == 'all_turns'
        assert _replayed(request) == [
            f'synthetic-ciphertext-{prior}' for prior in range(1, round_num)]


def test_unreplayable_state_already_in_history_still_fails_closed(upstream):
    """The fix is capture symmetry; foreign ciphertext still cannot be dropped."""
    history = _converse(DECLARED, rounds=1)
    with pytest.raises(ProtocolConversionError, match='mandatory_continuity_unrepresentable'):
        _round(history, UNDECLARED)
    assert len(upstream.requests) == 1
