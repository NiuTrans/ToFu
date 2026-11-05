"""Complete-text callers retain Codex routes and verify streamed completion."""
import importlib
import json
import time
from types import SimpleNamespace

import pytest

from lib.llm.llm_errors import AbortedError, RetryableAPIError
from lib.llm.stream_result import UnverifiedProviderStreamError
from tests.sse.test_sse_core_parity import _FakeRequestsResp

pytestmark = pytest.mark.unit


@pytest.fixture
def subscription_wire(monkeypatch):
    chat = importlib.import_module('lib.llm.chat')
    stream = importlib.import_module('lib.llm.stream')
    calls = []
    events = [
        {'type': 'response.output_text.delta', 'delta': '论文大纲'},
        {'type': 'response.completed', 'response': {
            'status': 'completed', 'output': [],
            'usage': {'input_tokens': 17, 'output_tokens': 4}}},
    ]

    def post(url, **kwargs):
        data = kwargs['data']
        # Codex subscription bodies use Codex CLI's zstd request encoding.
        assert kwargs['headers']['Content-Encoding'] == 'zstd'
        import zstandard
        calls.append((url, json.loads(
            zstandard.ZstdDecompressor().decompress(data))))
        wire = b''.join(b'data: ' + json.dumps(e).encode() + b'\n\n'
                        for e in events)
        return _FakeRequestsResp([], raw_chunks=[wire])

    def resolve(provider, body, headers, user_id):
        assert provider == 'codex' and user_id == '7'
        return 'synthetic-token', headers, body

    monkeypatch.setattr(chat, 'http_post', lambda *a, **kw: pytest.fail(
        'subscription complete-text call sent a nonstream request'))
    monkeypatch.setattr(stream, 'get_sync_session',
                        lambda: SimpleNamespace(post=post))
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **kw: 'direct')
    monkeypatch.setattr('lib.oauth.outbound.resolve_oauth_request', resolve)
    return calls, events


def complete(**kwargs):
    from lib.llm import chat
    return chat([{'role': 'user', 'content': 'Make an outline'}],
                model='fixture-model', api_key='synthetic',
                base_url='https://subscription.example.invalid',
                api_protocol='responses', oauth='codex', owner_user_id=7,
                max_tokens=128, max_retries=kwargs.pop('max_retries', 0), **kwargs)


def test_complete_text_uses_subscription_stream_and_preserves_usage(subscription_wire):
    calls, _ = subscription_wire
    text, usage = complete()
    assert text == '论文大纲'
    assert usage['prompt_tokens'] == 17
    assert usage['completion_tokens'] == 4
    assert usage['finish_reason'] == 'stop'
    assert len(calls) == 1
    assert calls[0][0] == 'https://subscription.example.invalid/responses'
    assert calls[0][1]['stream'] is True
    assert calls[0][1]['model'] == 'fixture-model'
    assert calls[0][1]['input'][0]['content'][0]['text'] == 'Make an outline'


def test_partial_subscription_response_is_never_a_complete_text(subscription_wire):
    _, events = subscription_wire
    events.pop()
    with pytest.raises(UnverifiedProviderStreamError):
        complete()


def test_subscription_completion_preserves_abort(subscription_wire):
    calls, _ = subscription_wire
    with pytest.raises(AbortedError):
        complete(abort_check=lambda: True)
    assert calls == []


def test_collected_completion_retains_tool_reasoning_and_call_controls(monkeypatch):
    stream = importlib.import_module('lib.llm.stream')
    tool_calls = [{'id': 'call-1', 'type': 'function', 'function': {
        'name': 'cite_source', 'arguments': '{"source":"paper"}'}}]

    def collected(body, **kwargs):
        assert kwargs['owner_user_id'] == 7 and kwargs['oauth'] == 'codex'
        assert kwargs['attempt_timeout'] == kwargs['first_byte_timeout'] == 12
        assert kwargs['max_retries'] == 0
        return ({'role': 'assistant', 'content': '', 'tool_calls': tool_calls,
                 'reasoning_content': 'synthetic reasoning'}, 'tool_calls',
                {'prompt_tokens': 17, 'completion_tokens': 4})

    monkeypatch.setattr(stream, 'stream_chat', collected)
    text, usage = complete(timeout=12)
    assert text == ''
    assert usage['finish_reason'] == 'tool_calls'
    assert usage['_tool_calls'] == tool_calls
    assert usage['_reasoning_content'] == 'synthetic reasoning'


@pytest.mark.parametrize('retries', [0, 1, 5])
def test_subscription_completion_honors_retry_budget(monkeypatch, subscription_wire, retries):
    stream = importlib.import_module('lib.llm.stream')
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(1)
        raise RetryableAPIError('synthetic network failure')

    monkeypatch.setattr(stream, '_stream_chat_once', fail)
    monkeypatch.setattr(stream, 'abortable_sleep', lambda *args: None)
    with pytest.raises(RetryableAPIError):
        complete(max_retries=retries)
    assert len(attempts) == 1 + retries


def test_dispatch_retains_a_known_stream_only_subscription(monkeypatch, subscription_wire):
    from lib.llm_dispatch import api
    from lib.llm_dispatch.slot import Slot

    slot = Slot(key_name='synthetic', api_key='synthetic', model='fixture-model',
                capabilities={'text'}, protocol='responses', oauth='codex',
                base_url='https://subscription.example.invalid', stream_only=True)
    selections = []

    def pick(**kwargs):
        selections.append(kwargs)
        assert slot.model not in set(kwargs.get('exclude_models') or ())
        slot.record_request()
        return slot

    monkeypatch.setattr(api, 'get_dispatcher', lambda: SimpleNamespace(
        slots=[slot], pick_and_reserve=pick))
    text, usage = api.dispatch_chat(
        [{'role': 'user', 'content': 'Make an outline'}],
        prefer_model=slot.model, strict_model=True, owner_user_id=7, max_retries=1)
    assert text == '论文大纲'
    assert len(selections) == 1
    assert usage['_dispatch']['model'] == 'fixture-model'
    assert slot.inflight == 0


@pytest.mark.parametrize('cap', [0, 10000, None])
def test_subscription_completion_uses_one_existing_budget_lease(subscription_wire, cap):
    from lib.agent_core.execution_budget import model_budget_admission, ModelBudgetAdmissionDenied

    calls, _ = subscription_wire
    task = {'id': 'subscription-budget', 'config': {'executionLimits': {
        'max_tokens': cap, 'max_tool_calls': 0, 'deadline': time.time() + 60}}}
    admission = model_budget_admission(task)
    if cap is not None:
        # The subscription dialect omits output-token caps. A finite budget
        # must retain the existing refusal instead of admitting uncapped work.
        with pytest.raises(ModelBudgetAdmissionDenied):
            complete(model_budget_admit=admission)
        assert calls == []
        assert task['_executionBudget'].consumption_snapshot() == (0, 0, True)
    else:
        text, _ = complete(model_budget_admit=admission)
        assert text == '论文大纲' and len(calls) == 1
        assert task['_executionBudget'].consumption_snapshot() == (21, 0, True)
        assert '_model_budget_admit' not in json.dumps(calls)
