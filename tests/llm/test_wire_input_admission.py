"""No-network selected-route prompt safety regressions."""
from types import SimpleNamespace

import pytest

from lib.llm.body._admission import enforce_body_input_limit
from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot
from lib.llm_errors import PromptTooLongError
from lib.token_counter.evidence import ADMITTED_INPUT_TOKENS_KEY as COUNT

pytestmark = pytest.mark.unit


@pytest.fixture
def window(monkeypatch):
    monkeypatch.setattr(
        'lib.tasks_pkg.compaction._tokens.resolve_model_context_limit',
        lambda model, provider_id='': 1000 if provider_id == 'small' else 10000,
    )


def adapt(body, *, model='gpt-4o', provider='small', tools=None):
    slot = SimpleNamespace(model=model, provider_id=provider,
                           thinking_format='', protocol='responses')
    return _adapt_stream_body_for_slot(
        slot, body, True, tools=tools, max_tokens=128,
        temperature=1, thinking_enabled=False, preset='medium', effort=None)


def test_selected_provider_window_rejects_current_count(window, monkeypatch):
    monkeypatch.setattr('lib.token_counter.count_tokens',
                        lambda *a, **k: {'tokens': 1001})
    body = {'model': 'gpt-4o', 'messages': [], COUNT: 10}
    with pytest.raises(PromptTooLongError, match='before transport'):
        adapt(body)
    assert body[COUNT] == 10


def test_same_model_recounts_locally_despite_reuse_hint(window, monkeypatch):
    calls = []

    def count(messages, **kwargs):
        calls.append((messages, kwargs))
        return {'tokens': 12}

    monkeypatch.setattr('lib.token_counter.count_tokens', count)
    body = {'model': 'gpt-4o', 'messages': [], COUNT: 999}
    assert adapt(body)[COUNT] == 12
    assert calls == [([], {'model': 'gpt-4o', 'system': None,
                          'tools': None, 'mode': 'tiktoken'})]
    assert body[COUNT] == 999


@pytest.mark.parametrize('drift', ['messages_replaced', 'message_edited',
                                  'system_replaced', 'tools_edited'])
def test_same_model_stale_admission_cannot_bypass_final_limit(window, drift):
    tools = [{'type': 'function', 'function': {
        'name': 'lookup', 'parameters': {'type': 'object', 'properties': {
            'query': {'type': 'string', 'description': 'short'},
        }},
    }}]
    body = {'model': 'gpt-4o',
            'messages': [{'role': 'user', 'content': 'short'}],
            'system': 'short', 'tools': tools}
    admitted = adapt(body, tools=tools)
    assert 0 < admitted[COUNT] < 1000
    text = 'hello world ' * 3000
    if drift == 'messages_replaced':
        admitted['messages'] = [{'role': 'user', 'content': text}]
    elif drift == 'message_edited':
        admitted['messages'][0]['content'] = text
    elif drift == 'system_replaced':
        admitted['system'] = text
    else:
        tools[0]['function']['parameters']['properties']['query']['description'] = text
    previous_count = admitted[COUNT]
    with pytest.raises(PromptTooLongError, match='before transport'):
        adapt(admitted, tools=tools)
    assert admitted[COUNT] == previous_count


def test_model_swap_recounts_final_tools_and_ignores_stale_hint(window, monkeypatch):
    tools = [{'type': 'function', 'function': {'name': 'large'}}]
    def count(messages, **kwargs):
        assert kwargs['model'] == 'gpt-4o'
        assert kwargs['tools'] is tools
        assert kwargs['mode'] == 'tiktoken'
        return {'tokens': 1001}
    monkeypatch.setattr('lib.token_counter.count_tokens', count)
    with pytest.raises(PromptTooLongError):
        adapt({'model': 'gpt-4.1', 'messages': [], COUNT: 1}, tools=tools)


def test_replaced_tools_invalidate_same_model_evidence(window, monkeypatch):
    monkeypatch.setattr('lib.token_counter.count_tokens',
                        lambda *a, **k: {'tokens': 1001})
    with pytest.raises(PromptTooLongError):
        adapt({'model': 'gpt-4o', 'messages': [], COUNT: 1}, tools=[{}])


def test_opaque_reasoning_is_not_tokenized_as_ciphertext(window):
    body = {'messages': [{'role': 'assistant', 'content': 'ok',
                         '_responses_output': [{'type': 'reasoning',
                                                'encrypted_content': 'x' * 100000}]}]}
    tokens = enforce_body_input_limit(body, model='gpt-4o', provider_id='small')
    assert tokens < 1000


def test_actual_large_text_and_tools_refused_without_truncation(window):
    text = 'hello world ' * 3000
    body = {'messages': [{'role': 'user', 'content': text}]}
    with pytest.raises(PromptTooLongError):
        enforce_body_input_limit(body, model='gpt-4o', provider_id='small')
    assert body['messages'][0]['content'] is text
    with pytest.raises(PromptTooLongError):
        enforce_body_input_limit({'messages': [], 'tools': [{'description': text}]},
                                 model='gpt-4o', provider_id='small')
