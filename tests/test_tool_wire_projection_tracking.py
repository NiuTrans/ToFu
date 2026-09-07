"""Behaviour contract for final provider tool-schema tracking.

The transport boundary emits the exact-schema fingerprint through the
request-scoped diagnostic sink.  The task stream must persist that digest on a
bounded ``tool_wire_projection`` event and remove the callable sidecar after
the model request settles.  This makes round-to-round schema drift observable
without storing full provider schemas or leaking diagnostics onto the wire.
"""

from __future__ import annotations

import json
import threading

import pytest

pytestmark = pytest.mark.unit


def _task():
    return {
        'id': 'wire-projection-tracking-task',
        'convId': 'wire-projection-tracking-conv',
        '_userId': 1,
        'status': 'running',
        'content': '',
        'thinking': '',
        'config': {'userId': 1},
        'events': [],
        'toolRounds': [],
        'content_lock': threading.Lock(),
        'events_lock': threading.Lock(),
    }


def _function_tool(name: str, *, typed: bool = True) -> dict:
    tool = {
        'function': {
            'name': name,
            'description': f'Use {name}.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    }
    if typed:
        tool['type'] = 'function'
    return tool


def test_wire_projection_reuses_matching_call_local_schema_evidence(
    monkeypatch,
):
    from lib import context_telemetry
    from lib.llm._sse_core import prepare_request
    from lib.tools import gateway

    tools = [_function_tool('read_files'), _function_tool('grep_search')]
    evidence_source = list(tools)
    evidence = context_telemetry.build_tool_schema_evidence(
        evidence_source,
        1_234,
        model='gpt-5.6-sol',
    )
    expected_fingerprint = gateway.tool_schema_fingerprint(tools)
    fingerprint_calls = []
    monkeypatch.setattr(
        gateway,
        'tool_schema_tokens',
        lambda *args, **kwargs: pytest.fail(
            'unchanged final schemas must reuse admission evidence'),
    )
    monkeypatch.setattr(
        gateway,
        'tool_schema_fingerprint',
        lambda final_tools: (
            fingerprint_calls.append(final_tools) or expected_fingerprint),
    )

    projections = []
    for _ in range(2):
        diagnostics = []
        plan = prepare_request({
            'model': 'gpt-5.6-sol',
            'messages': [{'role': 'user', 'content': 'inspect'}],
            'tools': tools,
            '_tool_wire_catalog': evidence_source,
            context_telemetry.TOOL_SCHEMA_EVIDENCE_KEY: evidence,
            '_tool_search_mode': 'off',
            '_programmatic_tool_calling': 'off',
            '_multi_agent_mode': 'off',
            '_request_activity_sink': diagnostics.append,
        }, api_key='secret', base_url='https://example.invalid/v1',
           api_protocol='openai')
        projections.append(next(
            item for item in diagnostics
            if item.get('kind') == 'wire_projection'))
        assert context_telemetry.TOOL_SCHEMA_EVIDENCE_KEY not in plan.body

    assert [row['schemaTokens'] for row in projections] == [1_234, 1_234]
    assert [row['schemaFingerprint'] for row in projections] == [
        expected_fingerprint, expected_fingerprint]
    assert len(fingerprint_calls) == 1
    assert context_telemetry.tool_schema_fingerprint_from_evidence(
        evidence) == expected_fingerprint


def test_wire_projection_recounts_when_preflight_repairs_a_schema(monkeypatch):
    from lib import context_telemetry
    from lib.llm._sse_core import prepare_request
    from lib.tools import gateway

    tools = [_function_tool('read_files', typed=False)]
    evidence_source = list(tools)
    evidence = context_telemetry.build_tool_schema_evidence(
        evidence_source,
        321,
        model='gpt-5.6-sol',
        source_fingerprint='a' * 64,
    )
    diagnostics = []
    counted = []
    fingerprinted = []
    monkeypatch.setattr(
        gateway,
        'tool_schema_tokens',
        lambda final_tools, **kwargs: counted.append(final_tools) or 654,
    )
    monkeypatch.setattr(
        gateway,
        'tool_schema_fingerprint',
        lambda final_tools: fingerprinted.append(final_tools) or 'b' * 64,
    )

    plan = prepare_request({
        'model': 'gpt-5.6-sol',
        'messages': [{'role': 'user', 'content': 'inspect'}],
        'tools': tools,
        '_tool_wire_catalog': evidence_source,
        context_telemetry.TOOL_SCHEMA_EVIDENCE_KEY: evidence,
        '_tool_search_mode': 'off',
        '_programmatic_tool_calling': 'off',
        '_multi_agent_mode': 'off',
        '_request_activity_sink': diagnostics.append,
    }, api_key='secret', base_url='https://example.invalid/v1',
       api_protocol='openai')

    projection = next(
        item for item in diagnostics if item.get('kind') == 'wire_projection')
    assert projection['schemaTokens'] == 654
    assert projection['schemaFingerprint'] == 'b' * 64
    assert counted == [plan.body['tools']]
    assert fingerprinted == [plan.body['tools']]
    assert plan.body['tools'][0] is not evidence_source[0]
    assert context_telemetry.tool_schema_fingerprint_from_evidence(
        evidence) == 'a' * 64
    assert context_telemetry.TOOL_SCHEMA_EVIDENCE_KEY not in plan.body


@pytest.mark.parametrize('protocol', ['responses', 'anthropic'])
def test_schema_evidence_never_reaches_translated_provider_bodies(
    monkeypatch,
    protocol,
):
    from lib import context_telemetry
    from lib.llm._sse_core import prepare_request
    from lib.tools import gateway

    tools = [_function_tool('read_files')]
    source = list(tools)
    evidence = context_telemetry.build_tool_schema_evidence(
        source,
        321,
        model='gpt-5.6-sol',
    )
    monkeypatch.setattr(gateway, 'tool_schema_tokens', lambda *a, **k: 654)

    plan = prepare_request({
        'model': 'gpt-5.6-sol',
        'messages': [{'role': 'user', 'content': 'inspect'}],
        'tools': tools,
        '_tool_wire_catalog': source,
        context_telemetry.TOOL_SCHEMA_EVIDENCE_KEY: evidence,
        '_tool_search_mode': 'off',
        '_programmatic_tool_calling': 'off',
        '_multi_agent_mode': 'off',
        '_request_activity_sink': lambda _row: None,
    }, api_key='secret', base_url='https://example.invalid/v1',
       api_protocol=protocol)

    assert context_telemetry.TOOL_SCHEMA_EVIDENCE_KEY not in plan.body
    json.dumps(plan.body)


def test_stream_persists_bounded_final_schema_fingerprint(monkeypatch):
    import lib.tasks_pkg.manager._stream as stream_module

    events = []
    fingerprint = 'a' * 80

    def fake_dispatch(body, **_kwargs):
        sink = body.get('_request_activity_sink')
        assert callable(sink)
        sink({
            'kind': 'wire_projection',
            'model': 'kimi-k3',
            'backend': 'local',
            'toolNames': ['read_files', 'execute_tools'],
            'toolCount': 2,
            'schemaTokens': 480,
            'schemaFingerprint': fingerprint,
            'schemaBudgetTokens': 0,
            'budgetDroppedNames': [],
            'compactedNames': [],
            'executableToolCount': 12,
        })
        return (
            {'role': 'assistant', 'content': 'ok', 'reasoning_content': ''},
            'stop',
            {'prompt_tokens': 10, 'completion_tokens': 1},
        )

    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '0')
    monkeypatch.setattr(stream_module, 'dispatch_stream', fake_dispatch)
    monkeypatch.setattr(
        stream_module, 'append_event',
        lambda _task_value, event: events.append(event))
    monkeypatch.setattr(
        stream_module, 'checkpoint_task_partial', lambda _task_value: None)

    body = {
        'model': 'kimi-k3',
        'messages': [{'role': 'user', 'content': 'inspect'}],
    }
    stream_module.stream_llm_response(_task(), body, tag='R4')

    projection = next(
        event for event in events if event.get('type') == 'tool_wire_projection')
    assert projection['roundNum'] == 4
    assert projection['toolNames'] == ['read_files', 'execute_tools']
    assert projection['schemaFingerprint'] == fingerprint[:64]
    assert projection['schemaTokens'] == 480
    assert '_request_activity_sink' not in body
