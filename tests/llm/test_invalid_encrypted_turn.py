"""Rejected native reasoning state recovers through real root settlement.

Only the provider transport is synthetic. Task creation, dispatch, argument
admission, file tool execution, accounting, event log and terminal settlement
use their ordinary owners. No replay digest normalizer or fixture is weakened.
"""

from copy import deepcopy
import json
import uuid

import pytest

from lib.llm.llm_errors import InvalidEncryptedContentError
from lib.llm.responses_outbound import openai_body_to_responses_checked
from lib.llm.stream_result import ProviderStreamResult
from tests.llm.test_llm_replay_scenarios import (
    _assert_invariants_clean, _durable_events, _install_replay_route, _make_task,
)
from tests.llm.test_responses_replay_order import _call, _message, _reasoning, _receive

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


@pytest.fixture(scope='module', autouse=True)
def release_synthetic_dispatcher():
    yield
    from lib.llm_dispatch import reset_dispatcher
    reset_dispatcher()


@pytest.mark.parametrize('streaming', [False, True], ids=['snapshot', 'stream'])
def test_invalid_native_state_after_file_tool_retries_once_without_reasoning(
        tmp_path, monkeypatch, streaming):
    from lib.tasks_pkg.invocation import execute_invocation, ROOT_INVOCATION_POLICY
    from lib.turn_verdict import derive_task_verdict, TurnStatus

    _install_replay_route()
    project = tmp_path / 'project'
    project.mkdir()
    task = _make_task('cv-invalid-state-' + uuid.uuid4().hex[:10],
                      'Save the marker once, then acknowledge completion.', project)
    task['config'].update(autoTurnRetryMax=2, disableModelFallback=False)
    arguments = json.dumps({'description': 'Save the synthetic marker once',
                            'path': 'marker.txt', 'content': 'saved-once\n', 'append': True})
    output = [_reasoning('opaque-before'), _message('Saving the marker once.', 'commentary'),
              _call('save-once', arguments), _reasoning('opaque-after')]
    output[2]['name'] = 'write_file'
    received = _receive(output, streaming)
    requests = []

    def stream(body, *, on_content=None, on_tool_call_ready=None, **kwargs):
        requests.append(deepcopy(body))
        if len(requests) > 3:
            from lib.llm.llm_errors import LocalRequestPreparationError
            raise LocalRequestPreparationError(
                'Encrypted-state rejection reopened provider execution', stage='replay_validation')
        if len(requests) == 1:
            if on_content:
                on_content(received['content'])
            if on_tool_call_ready:
                for call in received['tool_calls']:
                    on_tool_call_ready(deepcopy(call))
            return ProviderStreamResult.from_legacy(
                deepcopy(received), 'tool_calls', {'prompt_tokens': 100, 'completion_tokens': 10})
        # Exercise checked native egress while inspecting the real dispatch
        # request. Completed call/result adjacency survives the recovery; only
        # the rejected ciphertext is gone from the retried request.
        wire = openai_body_to_responses_checked(body, profile='codex').require_payload()
        assert sum(item['type'] == 'function_call' for item in wire['input']) == 1
        assert sum(item['type'] == 'function_call_output' for item in wire['input']) == 1
        reasoning = [item['encrypted_content'] for item in wire['input']
                     if item['type'] == 'reasoning']
        if len(requests) == 2:
            assert reasoning == ['opaque-before', 'opaque-after']
            raise InvalidEncryptedContentError(param='input[1].encrypted_content')
        assert reasoning == []
        if on_content:
            on_content('Marker saved.')
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': 'Marker saved.'}, 'stop',
            {'prompt_tokens': 100, 'completion_tokens': 5})

    monkeypatch.setattr('lib.llm.stream_chat', stream)
    execute_invocation(task, policy=ROOT_INVOCATION_POLICY)

    assert len(requests) == 3
    assert (project / 'marker.txt').read_bytes() == b'saved-once\n'
    assert derive_task_verdict(task).status is TurnStatus.COMPLETED
    assert not task.get('error')
    assert 'Marker saved.' in task['content']
    assert not task.get('_auto_turn_retry_count')
    assert len(task['toolRounds']) == 1
    assert task['toolRounds'][0]['toolName'] == 'write_file'
    assert task['toolRounds'][0]['status'] == 'done'
    events = _durable_events(task)
    assert not any(event['type'] == 'retry_reset' for event in events)
    done = [event for event in events if event['type'] == 'done']
    assert len(done) == 1 and not done[0].get('error')
    _assert_invariants_clean(task)
