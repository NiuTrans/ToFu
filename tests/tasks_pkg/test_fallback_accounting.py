"""Every round path bills paid responses without adopting discarded output."""

from copy import deepcopy
from types import SimpleNamespace
import threading

import pytest

from lib import llm
from lib.cost import normalize_usage
from lib.llm.stream_result import ProviderStreamResult
from lib.tasks_pkg.llm_fallback import _call, _retry, _state, _transport, _usage

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    'path,expected_model,tag',
    [
        ('primary', 'primary-model', 'R1'),
        ('configured', 'fallback-model', 'R1-FALLBACK'),
        ('rescue', 'rescue-model', 'R1-RESCUE'),
        ('reactive', 'primary-model', 'R1-REACTIVE'),
        ('refused', 'primary-model', 'R1-REFUSED'),
    ],
)
def test_each_attempt_path_bills_discarded_responses(
    monkeypatch, path, expected_model, tag
):
    usage = {
        'prompt_tokens': 100,
        'completion_tokens': 10,
        '_wire_messages': [{'role': 'user', 'content': 'attempt-local evidence'}],
        '_extra_billing_rounds': [
            {
                'model': 'discarded-model',
                'tag': 'paid-floor-attempt',
                'usage': {'prompt_tokens': 20, 'completion_tokens': 2},
            },
            {'usage': {}},
        ],
    }
    if path == 'rescue':
        usage['_dispatch'] = {'logical_model': expected_model}
    original_usage = deepcopy(usage)
    response = ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': 'provider response'},
        'content_filter' if path == 'refused' else 'stop',
        usage,
    )
    script = {
        'primary': [response],
        'configured': [llm.PermissionError_('primary unavailable'), response],
        'rescue': [
            llm.PermissionError_('primary unavailable'),
            llm.PermissionError_('fallback unavailable'),
            response,
        ],
        'reactive': [llm.PromptTooLongError('prompt too long'), response],
        'refused': [response],
    }[path]
    attempts = []

    def stream(_task, body, **kwargs):
        attempts.append((body['model'], kwargs))
        outcome = script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    events = []
    emit = lambda _task, event: events.append(event)
    monkeypatch.setattr(_transport, 'stream_llm_response', stream)
    monkeypatch.setattr(
        _retry,
        '_get_fallback_model',
        lambda _task: '' if path == 'refused' else 'fallback-model',
    )
    monkeypatch.setattr(
        _retry, '_get_pool_rescue_model', lambda _failed: 'rescue-model'
    )
    monkeypatch.setattr(
        llm,
        'build_body',
        lambda model, messages, **_kwargs: {'model': model, 'messages': messages},
    )
    monkeypatch.setattr('lib.tasks_pkg.manager._events.append_event', emit)
    monkeypatch.setattr('lib.tasks_pkg.round_retry.append_event', emit)
    monkeypatch.setattr(_usage, 'append_event', emit)
    monkeypatch.setattr('lib.log.audit_log', lambda *a, **kw: None)
    monkeypatch.setattr(
        'lib.tasks_pkg.compaction.api.reactive_compact', lambda *a, **kw: False
    )
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher',
        lambda: SimpleNamespace(has_capable_slots=lambda *_a, **_kw: True),
    )
    task = {
        'id': f'accounting-{path}',
        'convId': 'billing-fixture',
        '_userId': 1,
        'config': {},
        'events': [],
        'events_lock': threading.Lock(),
        'content_lock': threading.Lock(),
        'content': '',
        'thinking': '',
        '_round_base_content': '',
        '_round_base_thinking': '',
    }
    total, rounds = {}, []
    try:
        result = _call._llm_call_with_fallback(
            task,
            {'model': 'primary-model'},
            'primary-model',
            0,
            512,
            False,
            None,
            [{'role': 'user', 'content': 'continue'}],
            'low',
            False,
            total,
            rounds,
        )
    finally:
        _state.cleanup_reactive_compact_state(task['id'])

    assert not script
    assert (
        len(attempts)
        == {'primary': 1, 'configured': 2, 'rescue': 3, 'reactive': 2, 'refused': 1}[
            path
        ]
    )
    assert result['model'] == expected_model
    assert normalize_usage(total) == {
        'input': 120,
        'output': 12,
        'cache_read': 0,
        'cache_write': 0,
        'thinking': 0,
    }
    assert [row['model'] for row in rounds] == [
        expected_model,
        'discarded-model',
        expected_model,
    ]
    assert [row['tag'] for row in rounds] == [
        tag,
        'paid-floor-attempt',
        tag + '-DISCARDED',
    ]
    assert all(row['responseAuthoring'] is False for row in rounds[1:])
    assert (rounds[0].get('responseAuthoring') is False) == (path == 'refused')
    assert all(
        '_extra_billing_rounds' not in row['usage']
        and '_wire_messages' not in row['usage']
        for row in rounds
    )
    if path == 'refused':
        assert '_lastRoundUsage' not in task
    else:
        assert task['_lastRoundUsage']['model'] == expected_model
        assert task['_lastRoundUsage']['tokensIn'] == 100
    assert len([event for event in events if event['type'] == 'round_usage']) == 2
    assert usage == original_usage


def test_scope_denial_bills_known_usage_without_publishing_context(monkeypatch):
    def forbidden_emit(*_args, **_kwargs):
        pytest.fail('scope denial cannot publish round usage')

    monkeypatch.setattr(_usage, '_emit_round_usage', forbidden_emit)
    total, rounds = {}, []
    _usage.record_scope_denied_usage(
        {
            'prompt_tokens': 5,
            '_extra_billing_rounds': [{'usage': {'prompt_tokens': 3}}],
        },
        'denied-model',
        2,
        total,
        rounds,
    )

    assert normalize_usage(total)['input'] == 8
    assert len(rounds) == 2
    assert all(
        row['model'] == 'denied-model'
        and row['responseAuthoring'] is False
        and row['tag'] == 'R3-SCOPE-DISCARDED'
        for row in rounds
    )


def test_post_loop_synthesis_retains_named_round_and_bills_discards(monkeypatch):
    from lib.tasks_pkg.orchestrator import _finalize

    usage = {
        'prompt_tokens': 10,
        '_extra_billing_rounds': [
            {'model': 'discarded-model', 'usage': {'prompt_tokens': 3}}
        ],
    }
    events = []
    emit = lambda _task, event: events.append(event)
    monkeypatch.setattr(_finalize, 'append_event', emit)
    monkeypatch.setattr(_usage, 'append_event', emit)
    monkeypatch.setattr(
        _finalize.orchestrator_ports,
        'build_request_body',
        lambda model, messages, **_kwargs: {'model': model, 'messages': messages},
    )
    monkeypatch.setattr(
        _finalize,
        'stream_llm_response',
        lambda *_a, **_kw: (
            {'role': 'assistant', 'content': 'synthesized answer'},
            'stop',
            usage,
        ),
    )
    task = {'id': 'post-loop-billing', 'config': {}, '_userId': 1}
    total, rounds = {}, []

    finish, returned_usage, result = _finalize._run_post_loop_fallback(
        task,
        model='author-model',
        preset='medium',
        thinking_depth='medium',
        cfg={},
        original_messages=[{'role': 'user', 'content': 'continue'}],
        all_search_results_text=['observed result'],
        max_tokens=512,
        thinking_enabled=True,
        temperature=1.0,
        last_finish_reason=None,
        last_usage=None,
        last_stream_result=None,
        accumulated_usage=total,
        api_rounds=rounds,
    )

    assert finish == 'stop' and returned_usage is result.usage
    assert returned_usage['prompt_tokens'] == usage['prompt_tokens']
    assert result.message['content'] == 'synthesized answer'
    assert normalize_usage(total)['input'] == 13
    assert [row['round'] for row in rounds] == ['fallback', 'fallback']
    assert [row['model'] for row in rounds] == ['author-model', 'discarded-model']
    assert rounds[1]['responseAuthoring'] is False
    assert [row['tag'] for row in rounds] == ['FALLBACK', 'FALLBACK-DISCARDED']
    assert task['_lastRoundUsage']['model'] == 'author-model'
    assert len([event for event in events if event['type'] == 'round_usage']) == 2
