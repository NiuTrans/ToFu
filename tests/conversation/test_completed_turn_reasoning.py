"""Settled history can cross protocols without weakening active continuity."""
from copy import deepcopy

import pytest

from lib.llm.anthropic_outbound import openai_body_to_anthropic_checked
from lib.llm.openai_outbound import openai_body_to_chat
from lib.llm.responses_outbound import openai_body_to_responses_checked

pytestmark = pytest.mark.unit


def _history(monkeypatch, status, *, native_blocks=None):
    from lib import turn_lifecycle
    from lib.storage.model_continuation import ModelContinuationStore

    projection = {
        'content': 'Finished answer', 'model': 'source-messages-model',
        # Persisted projection metadata cannot override the settled status.
        '_turnStatus': 'completed',
        'toolRounds': [{
            'roundNum': 0, 'llmRound': 0, 'status': 'done',
            'toolName': 'read_file', 'toolCallId': 'call-read',
            'toolArgs': '{"path":"source.py"}', 'toolContent': 'Exact result',
            'assistantContent': 'Reading source',
            'thinking': 'Prior reasoning', 'thinkingSignature': 'opaque-signature',
            **({'_anthropicContentBlocks': native_blocks} if native_blocks else {}),
        }],
    }
    rows = [
        {'turnId': 'user-old', 'actor': 'human', 'laneId': 'main', 'ordinal': 1,
         'status': 'completed', 'projection': {'content': 'Original request'}},
        {'turnId': 'assistant-old', 'actor': 'assistant', 'laneId': 'main',
         'ordinal': 2, 'status': status, 'projection': projection},
        {'turnId': 'user-new', 'actor': 'human', 'laneId': 'main', 'ordinal': 3,
         'status': 'completed', 'projection': {'content': 'Follow up'}},
        {'turnId': 'assistant-new', 'actor': 'assistant', 'laneId': 'main',
         'ordinal': 4, 'status': 'running', 'projection': {'content': ''}},
    ]
    original = deepcopy(rows)

    class Client:
        def query(self, operation, payload, **kwargs):
            assert operation == 'turn.list'
            assert payload == {'conversation_id': 'conversation', 'user_id': 7}
            return deepcopy(rows)

    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda: Client())
    monkeypatch.setattr(ModelContinuationStore, 'read_context',
                        lambda self, **kwargs: {'supported': False})
    monkeypatch.setattr(ModelContinuationStore, 'get', lambda self, **kwargs: None)
    messages = turn_lifecycle.build_api_messages(
        'conversation', 'assistant-new', {'model': 'destination-responses-model'}, user_id=7)
    assert rows == original
    return messages


def test_completed_signed_history_preserves_work_across_protocol_switch(monkeypatch):
    messages = _history(monkeypatch, 'completed')
    original = deepcopy(messages)
    result = openai_body_to_responses_checked({'messages': messages}, profile='codex')
    payload = result.require_payload()
    assert messages == original
    assert result.permitted_conversion_losses['historical_thinking_signature_omitted'] == 1
    assert {'type': 'function_call', 'call_id': 'call-read', 'name': 'read_file',
            'arguments': '{"path":"source.py"}'} in payload['input']
    assert {'type': 'function_call_output', 'call_id': 'call-read',
            'output': 'Exact result'} in payload['input']
    assert any(block.get('text') == 'Finished answer'
               for item in payload['input'] if item.get('type') == 'message'
               for block in item['content'])
    assert 'opaque-signature' not in repr(payload)
    assert '_reasoning_continuity' not in repr(payload)
    same_protocol = openai_body_to_anthropic_checked({'messages': messages}).require_payload()
    assert any(block.get('signature') == 'opaque-signature'
               for message in same_protocol['messages'] for block in message['content'])


@pytest.mark.parametrize('status', ['running', 'failed', 'aborted', None])
def test_unsettled_history_cannot_discard_its_signature(monkeypatch, status):
    messages = _history(monkeypatch, status)
    result = openai_body_to_responses_checked({'messages': messages}, profile='codex')
    assert result.validation_issues == ('mandatory_continuity_unrepresentable',)


def test_completed_native_thinking_is_optional_but_visible_text_survives(monkeypatch):
    blocks = [{'type': 'thinking', 'thinking': 'Prior reasoning',
               'signature': 'opaque-signature'},
              {'type': 'redacted_thinking', 'data': 'opaque-redaction'},
              {'type': 'text', 'text': 'Visible native text'}]
    messages = _history(monkeypatch, 'completed', native_blocks=blocks)
    result = openai_body_to_responses_checked({'messages': messages}, profile='codex')
    payload = result.require_payload()
    assert result.permitted_conversion_losses['historical_thinking_block_omitted'] == 2
    assert any(block.get('text') == 'Visible native text'
               for item in payload['input'] if item.get('type') == 'message'
               for block in item['content'])
    assert 'opaque-redaction' not in repr(payload)


@pytest.mark.parametrize('block', [
    {'type': 'server_tool_use', 'id': 'server-call', 'name': 'search', 'input': {}},
    {'type': 'future_native_block', 'data': 'visible-work'},
])
def test_completed_history_does_not_erase_native_tool_or_unknown_work(monkeypatch, block):
    messages = _history(monkeypatch, 'completed', native_blocks=[block])
    result = openai_body_to_responses_checked({'messages': messages}, profile='codex')
    assert result.validation_issues == ('mandatory_continuity_unrepresentable',)


def _restored_history(monkeypatch):
    """A new Turn whose completed Claude prefix comes from its checkpoint.

    The configuration names no model, so the dispatcher chooses the protocol
    only after restoration; the checkpoint holds the live Messages output of
    the completed Turn (signature-only thinking, ``display: omitted``).
    """
    from lib import turn_lifecycle
    from lib.storage.model_continuation import ModelContinuationStore
    from lib.tasks_pkg import model_replay

    rows = [
        {'turnId': 'user-old', 'actor': 'human', 'laneId': 'main', 'ordinal': 1,
         'status': 'completed', 'projection': {'content': 'Original request'}},
        {'turnId': 'assistant-old', 'actor': 'assistant', 'laneId': 'main',
         'ordinal': 2, 'status': 'completed', 'projection': {'content': 'Finished answer'}},
        {'turnId': 'user-new', 'actor': 'human', 'laneId': 'main', 'ordinal': 3,
         'status': 'completed', 'projection': {'content': 'Follow up'}},
        {'turnId': 'assistant-new', 'actor': 'assistant', 'laneId': 'main',
         'ordinal': 4, 'status': 'running', 'projection': {'content': ''}},
    ]
    config = {'model': ''}
    view = [
        {'role': 'user', 'content': 'Original request', '_turnId': 'user-old'},
        {'role': 'assistant', 'content': '', '_anthropic_content_blocks': [
            {'type': 'thinking', 'thinking': '', 'signature': 'tool-signature'},
            {'type': 'tool_use', 'id': 'call-read', 'name': 'read_file',
             'input': {'path': 'source.py'}}],
         'tool_calls': [{'id': 'call-read', 'type': 'function', 'function': {
             'name': 'read_file', 'arguments': '{"path":"source.py"}'}}]},
        {'role': 'tool', 'tool_call_id': 'call-read', 'content': 'Exact result'},
        {'role': 'assistant', 'content': 'Finished answer', '_anthropic_content_blocks': [
            {'type': 'thinking', 'thinking': '', 'signature': 'final-signature'},
            {'type': 'text', 'text': 'Finished answer'}]},
    ]
    checkpoint = {'policy': model_replay.policy_witness(config),
                  'turns': [model_replay.turn_witness(row) for row in rows[:2]],
                  'messages': view}
    stored = deepcopy(checkpoint)

    class Client:
        def query(self, operation, payload, **kwargs):
            assert operation == 'turn.list'
            return deepcopy(rows)

    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda: Client())
    monkeypatch.setattr(ModelContinuationStore, 'read_context',
                        lambda self, **kwargs: {'supported': False})
    monkeypatch.setattr(ModelContinuationStore, 'get', lambda self, **kwargs: checkpoint)
    messages = turn_lifecycle.build_api_messages(
        'conversation', 'assistant-new', config, user_id=7)
    assert config['_modelReplayStatus'] == 'restored'
    assert checkpoint == stored
    return messages


def test_restored_completed_prefix_crosses_protocols_like_a_cold_rebuild(monkeypatch):
    messages = _restored_history(monkeypatch)
    original = deepcopy(messages)

    chat = openai_body_to_chat({'model': 'chat-model', 'messages': messages})
    assert chat['_conversion_report'] == {'historical_thinking_block_omitted': 2}
    assert {'id': 'call-read', 'type': 'function', 'function': {
        'name': 'read_file', 'arguments': '{"path":"source.py"}'}} in [
        call for message in chat['messages'] for call in message.get('tool_calls') or []]
    assert 'signature' not in repr(chat['messages'])

    responses = openai_body_to_responses_checked({'messages': messages}, profile='codex')
    payload = responses.require_payload()
    assert responses.permitted_conversion_losses['historical_thinking_block_omitted'] == 2
    assert {'type': 'function_call_output', 'call_id': 'call-read',
            'output': 'Exact result'} in payload['input']
    assert 'signature' not in repr(payload)
    assert messages == original

    same_protocol = openai_body_to_anthropic_checked({'messages': messages}).require_payload()
    signatures = [block.get('signature') for message in same_protocol['messages']
                  for block in message['content'] if block.get('type') == 'thinking']
    assert signatures == ['tool-signature', 'final-signature']
    assert '_reasoning_continuity' not in repr(same_protocol)


@pytest.mark.parametrize('block,issue', [
    ({'type': 'thinking', 'thinking': 'Prior reasoning', 'signature': ''},
     'native_signature_required'),
    ({'type': 'redacted_thinking', 'data': 123}, 'native_content_invalid'),
])
def test_completed_history_still_validates_native_thinking(monkeypatch, block, issue):
    messages = _history(monkeypatch, 'completed', native_blocks=[block])
    result = openai_body_to_responses_checked({'messages': messages}, profile='codex')
    assert result.validation_issues == (issue,)
