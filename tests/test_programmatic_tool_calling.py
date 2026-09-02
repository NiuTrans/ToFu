"""End-to-end contracts for Responses Programmatic Tool Calling."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


def _caller():
    return {'type': 'program', 'caller_id': 'prog-1'}


def test_stateless_replay_keeps_order_caller_and_structured_program_output():
    from lib.llm.responses_outbound import openai_body_to_responses

    body, _ = openai_body_to_responses({
        'model': 'gpt-5.6-sol',
        '_responses_feature_profile': 'openai',
        '_programmatic_tool_calling': 'auto',
        'messages': [
            {'role': 'assistant', 'content': '', '_responses_items': [{
                'type': 'program', 'call_id': 'prog-1',
                'code': 'const x = await tools.read_files({path:"a"});',
            }], 'tool_calls': [{
                'id': 'child-1', 'type': 'function', 'caller': _caller(),
                'function': {'name': 'read_files', 'arguments': '{"path":"a"}'},
            }]},
            {'role': 'tool', 'tool_call_id': 'child-1', 'caller': _caller(),
             'content': 'file body'},
            {'role': 'assistant', 'content': '', '_responses_items': [{
                'type': 'program_output', 'call_id': 'prog-1',
                'status': 'completed', 'result': '{"selected":1}',
            }]},
        ],
    })

    dynamic = [item for item in body['input']
               if item.get('type') != 'message']
    assert [item['type'] for item in dynamic] == [
        'program', 'function_call', 'function_call_output', 'program_output']
    assert dynamic[1]['caller'] == _caller()
    assert dynamic[2]['caller'] == _caller()
    assert json.loads(dynamic[2]['output']) == {
        'content': 'file body', 'truncated': False}


def test_direct_and_program_calls_share_exact_output_envelope():
    from lib.llm.responses_outbound import openai_body_to_responses

    body, _ = openai_body_to_responses({
        'model': 'gpt-5.6-sol',
        '_responses_feature_profile': 'openai',
        '_programmatic_tool_calling': 'auto',
        'messages': [{
            'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'direct-1', 'type': 'function',
                'function': {'name': 'read_files', 'arguments': '{}'},
            }]}, {
            'role': 'tool', 'tool_call_id': 'direct-1', 'content': 'body',
        }],
    })
    item = next(x for x in body['input']
                if x.get('type') == 'function_call_output')
    assert json.loads(item['output']) == {
        'content': 'body', 'truncated': False}


def test_program_output_budget_is_cumulative_and_utf8_safe(monkeypatch):
    from lib.llm.responses_outbound import openai_body_to_responses
    import lib.tools.programmatic as contract

    monkeypatch.setattr(contract, 'PROGRAMMATIC_MAX_OUTPUT_BYTES', 5)
    caller = _caller()
    messages = []
    for call_id, content in (('child-1', 'ééé'), ('child-2', 'zz')):
        messages.extend([{
            'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': call_id, 'type': 'function', 'caller': caller,
                'function': {'name': 'read_files', 'arguments': '{}'},
            }]}, {
            'role': 'tool', 'tool_call_id': call_id, 'caller': caller,
            'content': content,
        }])
    body, _ = openai_body_to_responses({
        'model': 'gpt-5.6-sol',
        '_responses_feature_profile': 'openai',
        '_programmatic_tool_calling': 'auto',
        'messages': messages,
    })
    outputs = [json.loads(x['output']) for x in body['input']
               if x.get('type') == 'function_call_output']
    assert outputs == [
        {'content': 'éé', 'truncated': True},
        {'content': 'z', 'truncated': True},
    ]


def test_ptc_eligibility_is_explicit_not_the_idempotent_partition():
    from lib.tools.registry import all_specs
    from lib.tools.programmatic import eligible_programmatic_tool_names

    names = eligible_programmatic_tool_names()
    assert {'read_files', 'grep_search', 'fetch_url'} <= names
    # Retry-safe is insufficient: these routes lose citations/native artifacts
    # or mutate the active instruction surface when hidden in a program.
    assert {'web_search', 'inspect_image', 'load_skill'}.isdisjoint(names)
    assert all(spec.programmatic_tools <= spec.provides for spec in all_specs())


def test_program_parent_is_persisted_before_children_and_settled(monkeypatch):
    import lib.tasks_pkg.orchestrator._programmatic as _programmatic
    emitted = []
    monkeypatch.setattr(_programmatic, 'append_event',
                        lambda _task, event: emitted.append(event))
    task = {'toolRounds': [{
        'roundNum': 1, 'llmRound': 2, 'toolCallId': 'child-1',
        'toolName': 'read_files', 'status': 'searching',
    }]}
    assistant = {
        '_responses_items': [{
            'type': 'program', 'call_id': 'prog-1', 'fingerprint': 'opaque',
            'code': 'const a = await tools.read_files({path:"a"});\n'
                    'const b = await tools.grep_search({query:a.content});',
        }],
        'tool_calls': [{
            'id': 'child-1', 'caller': _caller(),
            'function': {'name': 'read_files', 'arguments': '{}'},
        }, {
            'id': 'child-2', 'caller': _caller(),
            'function': {'name': 'grep_search', 'arguments': '{}'},
        }],
    }
    assert _programmatic.reconcile_programmatic_items(
        task, assistant, llm_round=2) == 1
    parent = task['toolRounds'][0]
    assert parent['_programSynthetic'] is True
    assert parent['childCallIds'] == ['child-1', 'child-2']
    assert emitted[0]['type'] == 'program_start'
    run = task['programRuns'][0]
    assert run['callId'] == 'prog-1'
    assert run['source'] == 'openai_ptc'
    assert run['limits']['maxCalls'] == 16
    assert run['limits']['maxConcurrentCalls'] == 8
    assert [c['id'] for c in run['childCalls']] == ['child-1', 'child-2']

    output = {'_responses_items': [{
        'type': 'program_output', 'call_id': 'prog-1',
        'status': 'completed', 'result': '{"matches":2}',
    }]}
    _programmatic.reconcile_programmatic_items(task, output, llm_round=3)
    assert parent['status'] == 'done'
    assert parent['programResult'] == '{"matches":2}'
    assert run['status'] == 'completed'
    assert run['result'] == '{"matches":2}'
    assert emitted[-1]['type'] == 'program_output'
    # A reconnect/replay is idempotent.
    before = len(emitted)
    _programmatic.reconcile_programmatic_items(task, output, llm_round=3)
    assert len(emitted) == before


def test_cold_tool_round_reconstruction_preserves_program_caller():
    from lib.tasks_pkg.conv_message_builder._toolcalls import (
        _reconstruct_tool_call_messages,
    )

    messages = _reconstruct_tool_call_messages([{
        'roundNum': 1, 'llmRound': 0,
        'toolCallId': 'child-1', 'toolName': 'read_files',
        'toolArgs': '{}', 'toolContent': 'body', 'caller': _caller(),
    }])
    assert messages is not None
    assert messages[0]['tool_calls'][0]['caller'] == _caller()
    assert messages[1]['caller'] == _caller()


def test_program_parent_is_never_reconstructed_as_a_tool_call():
    from lib.tasks_pkg.conv_message_builder._toolcalls import (
        _reconstruct_tool_call_messages,
    )

    assert _reconstruct_tool_call_messages([{
        'roundNum': 8_800_000, '_programSynthetic': True,
        '_programCallId': 'prog-1', 'programCode': 'return 1',
        'programResult': '1', 'status': 'done',
    }]) is None


def test_nonstream_program_output_requests_final_message_followup():
    from lib.llm.responses_outbound import responses_response_to_openai

    translated = responses_response_to_openai({
        'id': 'resp', 'status': 'completed', 'model': 'gpt-5.6-sol',
        'output': [{
            'type': 'program_output', 'call_id': 'prog-1',
            'status': 'completed', 'result': '{"ok":true}',
        }],
        'usage': {'input_tokens': 4, 'output_tokens': 2},
    })
    assert translated['usage']['_program_pending'] is True


def test_program_call_budget_and_direct_only_boundary_are_hard():
    from lib.tasks_pkg.orchestrator._programmatic import (
        reject_programmatic_call,
    )
    from lib.tools.programmatic import PROGRAMMATIC_MAX_CALLS

    task = {}
    direct_only = {
        'id': 'search-1', 'caller': _caller(),
        'function': {'name': 'web_search'},
    }
    rejected = reject_programmatic_call(task, direct_only, 'web_search')
    assert rejected and rejected[1]['kind'] == 'programmatic_direct_only'

    missing_id = {
        'caller': _caller(),
        'function': {'name': 'read_files'},
    }
    rejected = reject_programmatic_call(task, missing_id, 'read_files')
    assert rejected and rejected[1]['kind'] == 'programmatic_invalid_call_id'

    for index in range(PROGRAMMATIC_MAX_CALLS):
        tc = {
            'id': f'child-{index}', 'caller': _caller(),
            'function': {'name': 'read_files'},
        }
        assert reject_programmatic_call(task, tc, 'read_files') is None
        if index == 0:
            repeated = reject_programmatic_call(task, tc, 'read_files')
            assert repeated and repeated[1]['kind'] == (
                'programmatic_duplicate_call_id')
    overflow = {
        'id': 'overflow', 'caller': _caller(),
        'function': {'name': 'read_files'},
    }
    rejected = reject_programmatic_call(task, overflow, 'read_files')
    assert rejected and rejected[1]['kind'] == 'programmatic_budget'
    run = task['programRuns'][0]
    assert len(run['admittedCallIds']) == PROGRAMMATIC_MAX_CALLS
    assert run['rejectedCallIds'] == ['search-1', 'overflow']
    assert run['duplicateRejectedCallCount'] == 1


def test_program_caller_identity_and_parallelism_fail_closed(monkeypatch):
    from lib.tasks_pkg.orchestrator._programmatic import (
        reject_programmatic_call,
        settle_programmatic_call,
    )
    from lib.tasks_pkg.tool_dispatch._pipeline import _parallel_worker_limit

    malformed = {
        'id': 'child-1', 'caller': {'type': 'program'},
        'function': {'name': 'read_files'},
    }
    task = {}
    rejected = reject_programmatic_call(task, malformed, 'read_files')
    assert rejected and rejected[1]['kind'] == 'programmatic_invalid_caller'
    settle_programmatic_call(task, malformed, 'rejected')
    assert task.get('programRuns') is None

    monkeypatch.setenv('TOOL_MAX_PARALLEL_WORKERS', '32')
    assert _parallel_worker_limit(32) == 32
    assert _parallel_worker_limit(32, programmatic=True) == 8


def test_program_output_telemetry_matches_cumulative_utf8_budget(monkeypatch):
    import lib.tasks_pkg.orchestrator._programmatic as _programmatic
    monkeypatch.setattr(_programmatic, 'PROGRAMMATIC_MAX_OUTPUT_BYTES', 5)
    task = {}
    first = {
        'id': 'child-1', 'caller': _caller(),
        'function': {'name': 'read_files'},
    }
    second = {
        'id': 'child-2', 'caller': _caller(),
        'function': {'name': 'read_files'},
    }
    empty = {
        'id': 'child-3', 'caller': _caller(),
        'function': {'name': 'read_files'},
    }
    _programmatic.settle_programmatic_call(task, first, 'done', 'ééé')
    _programmatic.settle_programmatic_call(task, second, 'done', 'zz')
    _programmatic.settle_programmatic_call(task, empty, 'done', None)

    run = task['programRuns'][0]
    assert (run['rawOutputBytes'], run['outputBytes']) == (10, 5)
    assert run['outputTruncated'] is True
    assert [(
        child['rawOutputBytes'], child['outputBytes'],
        child['outputTruncated'], child['status'],
    ) for child in run['childCalls']] == [
        (6, 4, True, 'done'),
        (2, 1, True, 'done'),
        (2, 0, True, 'done'),
    ]
    assert all(child['tEnd'] >= child['tStart']
               for child in run['childCalls'])

    # Cached settlement and reconnect replay must not double-count bytes.
    _programmatic.settle_programmatic_call(task, first, 'done', 'different')
    assert (run['rawOutputBytes'], run['outputBytes']) == (10, 5)


def test_streaming_prefetch_defers_program_children_until_parent_exists():
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator

    task = {'id': 'task-program', 'toolRounds': []}
    acc = StreamingToolAccumulator(task, None)
    try:
        acc.on_tool_call_ready({
            'id': 'child-1', 'caller': _caller(),
            'function': {'name': 'read_files', 'arguments': '{"path":"a"}'},
        })
        assert task['toolRounds'] == []
        assert acc.announced_tc_map == {}
        assert acc.submitted_count == 0
    finally:
        acc._pool.shutdown(wait=True)


def test_program_continuation_budget_is_per_run():
    from lib.tasks_pkg.orchestrator._programmatic import (
        admit_program_continuation,
    )
    from lib.tools.programmatic import PROGRAMMATIC_MAX_CONTINUATIONS

    task = {}
    msg = {'_responses_items': [{
        'type': 'program_output', 'call_id': 'prog-1',
        'status': 'completed', 'result': '{}',
    }]}
    for expected in range(1, PROGRAMMATIC_MAX_CONTINUATIONS + 1):
        allowed, count, limit = admit_program_continuation(task, msg)
        assert allowed is True
        assert (count, limit) == (expected, PROGRAMMATIC_MAX_CONTINUATIONS)
    allowed, count, _ = admit_program_continuation(task, msg)
    assert allowed is False
    assert count == PROGRAMMATIC_MAX_CONTINUATIONS + 1

    malformed_task = {}
    assert admit_program_continuation(malformed_task, {
        '_responses_items': [{'type': 'program_output'}],
    }) == (False, 0, PROGRAMMATIC_MAX_CONTINUATIONS)
    assert malformed_task.get('programRuns') is None


def test_program_runs_enter_headless_result_metadata():
    from lib.tasks_pkg.manager import build_result_meta

    run = {'callId': 'prog-1', 'status': 'completed'}
    assert build_result_meta({'programRuns': [run]})['programRuns'] == [run]
