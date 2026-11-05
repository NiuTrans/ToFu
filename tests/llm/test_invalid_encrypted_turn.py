"""Rejected native state preserves completed effects through real root settlement.

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
def test_invalid_native_state_after_file_tool_is_one_terminal_failure(
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
        if len(requests) > 2:
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
        # request. Both ciphertext occurrences and completed call/result
        # adjacency remain present when the provider rejects the next request.
        wire = openai_body_to_responses_checked(body, profile='codex').require_payload()
        assert [item['encrypted_content'] for item in wire['input']
                if item['type'] == 'reasoning'] == ['opaque-before', 'opaque-after']
        assert sum(item['type'] == 'function_call' for item in wire['input']) == 1
        assert sum(item['type'] == 'function_call_output' for item in wire['input']) == 1
        raise InvalidEncryptedContentError(param='input[1].encrypted_content')

    monkeypatch.setattr('lib.llm.stream_chat', stream)
    execute_invocation(task, policy=ROOT_INVOCATION_POLICY)

    assert len(requests) == 2
    assert (project / 'marker.txt').read_bytes() == b'saved-once\n'
    # The task carrier may be closed as `done`; only the common Turn verdict
    # defines success. It must remain FAILED with its explicit error channel.
    assert derive_task_verdict(task).status is TurnStatus.FAILED
    assert task['finishReason'] == 'error'
    assert task['error']['kind'] == 'bad_request'
    assert task['error']['retryable'] is False
    assert not task.get('_auto_turn_retry_count')
    assert len(task['toolRounds']) == 1
    assert task['toolRounds'][0]['toolName'] == 'write_file'
    assert task['toolRounds'][0]['status'] == 'done'
    assert [item['encrypted_content']
            for item in task['toolRounds'][0]['_responsesItems']
            if item['type'] == 'reasoning'] == ['opaque-before', 'opaque-after']
    events = _durable_events(task)
    assert not any(event['type'] == 'retry_reset' for event in events)
    done = [event for event in events if event['type'] == 'done']
    assert len(done) == 1 and done[0]['error']['kind'] == 'bad_request'
    assert derive_task_verdict(task, done[0]).status is TurnStatus.FAILED
    _assert_invariants_clean(task)
