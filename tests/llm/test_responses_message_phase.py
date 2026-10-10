"""Responses phase survives real stream, history, and request boundaries."""

import copy
import json
import threading
import time

import pytest

from lib.llm._sse_core import SSEAccumulator
from lib.llm.diagnostics import RawSSEDumper
from lib.llm.responses_outbound import (
    ResponsesSSETranslator, openai_body_to_responses, responses_response_to_openai,
)
from lib.tasks_pkg.assistant_messages import build_assistant_prose_message
from lib.tasks_pkg.conv_message_builder._toolcalls import build_assistant_tool_call_message
from lib.tasks_pkg.stream_handler.api import analyse_stream_result

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_event_sink(monkeypatch):
    # Synthetic tasks use an injected sink rather than production registration.
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, event: task['events'].append(event))


def _item(text, phase='commentary', **extra):
    return {'type': 'message', 'role': 'assistant', 'phase': phase,
            'content': [{'type': 'output_text', 'text': text}], **extra}


def _receive(items, streaming):
    response = {'status': 'completed', 'output': items, 'usage': {}}
    if not streaming:
        return responses_response_to_openai(response)['choices'][0]['message']
    body = {'model': 'gpt-6-astra', 'messages': []}
    acc = SSEAccumulator(body, 'phase-test',
                         RawSSEDumper('gpt-6-astra', 'phase-test', body),
                         ResponsesSSETranslator('gpt-6-astra'), time.time())
    for index, item in enumerate(items):
        for event in (
            {'type': 'response.output_item.added', 'output_index': index, 'item': item},
            {'type': 'response.output_text.delta', 'output_index': index,
             'delta': item['content'][0]['text']},
            {'type': 'response.output_item.done', 'output_index': index, 'item': item},
        ):
            acc.feed_line('data: ' + json.dumps(event))
    acc.feed_line('data: ' + json.dumps({'type': 'response.completed', 'response': response}))
    return acc.finalize()[0]


def _wire(message, profile='default'):
    body, _ = openai_body_to_responses(
        {'model': 'gpt-6-astra', 'messages': [message]}, profile=profile)
    return [item for item in body['input'] if item['type'] == 'message']


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('profile', ['default', 'codex'])
def test_each_original_phase_survives_assistant_history_and_responses_replay(streaming, profile):
    items = [_item('Investigating.'), _item('Fixed and verified.', 'final_answer')]
    message = _receive(items, streaming)
    original = copy.deepcopy(message)
    rebuilt = build_assistant_prose_message(message)
    assert _wire(rebuilt, profile) == items
    assert message == original


@pytest.mark.parametrize('streaming', [False, True])
def test_tool_round_reconstruction_preserves_preamble_phase(streaming):
    items = [_item('Inspecting the failing path.')]
    message = _receive(items, streaming)
    rebuilt = build_assistant_tool_call_message(
        tool_calls=[{'id': 'read-1', 'type': 'function',
                     'function': {'name': 'read_file', 'arguments': '{}'}}],
        content=message['content'], responses_items=message.get('_responses_items'))
    assert _wire(rebuilt) == items


def test_edited_or_compacted_prose_cannot_resurrect_original_text():
    message = _receive([_item('Original text to redact.')], False)
    message['content'] = 'Redacted summary.'
    assert _wire(message) == [{'type': 'message', 'role': 'assistant',
                              'content': [{'type': 'output_text', 'text': 'Redacted summary.'}]}]


def _analyse(message, task=None, finish='stop'):
    if task is None:
        task = {'id': 'phase-test', '_userId': 1, 'aborted': False,
                'events': [], 'events_lock': threading.Lock()}
    history = [{'role': 'user', 'content': 'Fix the failing test.'}]
    decision = analyse_stream_result(message, finish, task, 'phase-test',
                                     'gpt-6-astra', 0, 0, history, {})
    return decision, history, task


def test_commentary_only_response_continues_without_a_checklist_or_failed_tool():
    message = _receive([_item('Investigating the cause.')], False)
    decision, history, task = _analyse(message)
    assert decision['action'] == 'continue'
    assert any(row.get('_responses_items') for row in history)
    assert history[-1]['_isMeta'] is True
    assert any(event.get('type') == 'phase' for event in task['events'])


@pytest.mark.parametrize('phase', ['final_answer', None, 'unknown'])
def test_final_and_unphased_answers_are_not_reinterpreted(phase):
    message = _receive([_item('Here is the result.', phase)], False)
    decision, _, _ = _analyse(message)
    assert decision['action'] == 'break'


@pytest.mark.parametrize('state', [{'aborted': True}, {'pendingGuidanceId': 'human-1'}])
def test_commentary_continuation_respects_cancellation_and_human_wait(state):
    task = {'id': 'phase-test', '_userId': 1, 'aborted': False,
            'events': [], 'events_lock': threading.Lock(), **state}
    decision, history, _ = _analyse(_receive([_item('Waiting.')], False), task)
    assert decision['action'] == 'break'
    assert len(history) == 1


def test_repeated_commentary_is_bounded_and_cannot_settle_successfully():
    message = _receive([_item('Still investigating.')], False)
    decision, _, task = _analyse(message)
    for _ in range(4):
        decision, _, task = _analyse(message, task)
        if decision['action'] == 'break':
            break
    assert decision['action'] == 'break'
    assert decision['last_finish_reason'] == 'abnormal_stop'


def test_real_body_assembly_keeps_adjacent_phases_and_detaches_caller_data():
    from lib.llm.body import build_body

    messages = [_receive([_item('Investigating.')], False),
                _receive([_item('Fixed.', 'final_answer')], False)]
    original = copy.deepcopy(messages)
    canonical = build_body('gpt-6-astra', messages)
    wire, _ = openai_body_to_responses(canonical)
    assert [item['phase'] for item in wire['input']] == ['commentary', 'final_answer']
    assert messages == original
    wire['input'][0]['content'][0]['text'] = 'wire mutation'
    assert messages == original


@pytest.mark.parametrize('streaming', [False, True])
def test_unphased_sibling_text_is_neither_lost_nor_assigned_an_invented_phase(streaming):
    unphased = _item('Supplement.')
    unphased.pop('phase')
    items = [_item('Investigating.'), unphased, _item('Done.', 'final_answer')]
    assert _wire(_receive(items, streaming)) == items


@pytest.mark.parametrize('reason', ['awaiting_human', 'blocked', 'done'])
def test_explicit_handback_is_not_overridden_by_commentary(reason):
    message = _receive([_item(f'Status. [END_TURN: {reason}]')], False)
    assert _analyse(message)[0]['action'] == 'break'


@pytest.mark.parametrize('finish', ['length', 'error'])
def test_commentary_does_not_override_truncation_or_error(finish):
    decision, history, _ = _analyse(_receive([_item('Partial.')], False), finish=finish)
    assert decision['action'] == 'break'
    assert decision['last_finish_reason'] == finish
    assert len(history) == 1


def test_phase_sidecar_budget_is_bounded_and_preserves_display_text_on_overflow():
    from lib.llm.responses_outbound._message_phase import (
        MAX_PHASE_MESSAGES, MAX_PHASE_TEXT_BYTES,
    )

    text = 'x' * MAX_PHASE_TEXT_BYTES
    at_limit = _receive([_item(text)], False)
    assert _wire(at_limit)[0]['phase'] == 'commentary'
    assert len(json.dumps(at_limit['_responses_items']).encode()) < 512 * 1024
    for items in ([_item(text + 'x')], [_item('x')] * (MAX_PHASE_MESSAGES + 1),
                  [_item('界' * (MAX_PHASE_TEXT_BYTES // 3 + 1))]):
        message = _receive(items, False)
        assert message['content']
        assert not message.get('_responses_items')
        assert 'phase' not in _wire(message)[0]


def test_cold_transcript_rebuild_preserves_phase_without_storage_access():
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages

    items = [_item('Checked.'), _item('Verified.', 'final_answer')]
    message = _receive(items, False)
    task = {}
    build_assistant_prose_message(message, task=task)
    stored = {'role': 'assistant', 'content': message['content'],
              '_responsesItems': task['_responsesItems']}
    rebuilt = _transform_messages([stored], {}, user_id=1)
    assert _wire(rebuilt[0]) == items


def test_private_phase_state_is_not_a_chat_completions_field():
    from lib.llm.conversion import ProtocolConversionError
    from lib.llm_sanitize import _strip_non_api_fields

    message = _receive([_item('Working.')], False)
    clean = _strip_non_api_fields([message])[0]
    assert clean == {'role': 'assistant', 'content': 'Working.'}
    # Native replay state is assistant-only: `_responses_items` on a
    # non-assistant message is a protocol violation the conversion boundary
    # rejects LOUD (native_content_role_invalid) — phase state can never
    # leak onto a user message through that channel at all.
    user = {'role': 'user', 'content': message['content'],
            '_responses_items': message['_responses_items']}
    with pytest.raises(ProtocolConversionError,
                       match='native_content_role_invalid'):
        _wire(user)


def test_native_subagent_commentary_is_not_promoted_to_root_progress():
    raw = {'status': 'completed', 'output': [
        _item('Private worker update.', agent={'agent_name': '/root/worker'}),
        _item('Public answer.', 'final_answer', agent={'agent_name': '/root'}),
    ]}
    message = responses_response_to_openai(raw)['choices'][0]['message']
    assert message['content'] == 'Public answer.'
    assert not message.get('_responses_items')
    assert _analyse(message)[0]['action'] == 'break'
