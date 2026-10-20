"""Agent reports preserve evidence while only recognized native text is unwrapped."""
import json

import pytest

from lib.tools.result_surfaces import project_model_source

pytestmark = pytest.mark.unit

NUMBERS = ('1.2345678901234567890123456789', '1e400', '1e-400',
           '-0', '9007199254740993', '1.2300e+05')


def deliver(name, raw):
    if name == 'poll':
        source = '== build (command) ==\nrunning\n== worker (swarm agent) ==\n' + raw + '\n'
        return source, project_model_source(name, source)
    return raw, project_model_source(name, raw)


@pytest.mark.parametrize('name', ['get_agent_result', 'poll'])
@pytest.mark.parametrize('number', NUMBERS)
def test_agent_report_preserves_numeric_tokens_and_nested_types(name, number):
    raw = ('{"status":"ok","agent_status":"completed",'
           '"final_answer":"Evidence\\nComplete","signal":' + number +
           ',"metrics":{"amount":' + number + ',"text":"' + number + '"}}')
    source, view = deliver(name, raw)

    assert view != source
    assert 'Evidence\nComplete' in view
    assert f'signal: {number}\n' in view
    assert f'metrics: {{"amount":{number},"text":"{number}"}}' in view
    if name == 'poll':
        assert view.startswith('== build (command) ==\nrunning\n== worker (swarm agent) ==\n')


@pytest.mark.parametrize('name', ['get_agent_result', 'poll'])
@pytest.mark.parametrize('field', ['final_answer', 'result', 'partial_output'])
def test_numeric_report_field_does_not_become_a_text_report(name, field):
    raw = '{"status":"ok","' + field + '":1e400}'
    source, view = deliver(name, raw)
    assert view == source


@pytest.mark.parametrize('name', ['get_agent_result', 'poll'])
def test_mixed_report_keeps_numeric_section_and_partial_text_distinct(name):
    raw = '{"status":"ok","final_answer":-0,"partial_output":"Exact partial evidence"}'
    _, view = deliver(name, raw)
    assert '## final_answer\n-0' in view
    assert '## partial_output\nExact partial evidence' in view


@pytest.mark.parametrize('name', ['get_agent_result', 'poll'])
def test_report_batch_preserves_unknown_and_failed_evidence(name):
    raw = ('{"status":"ok","results":['
           '{"agent_id":"done","final_answer":"Exact\\nReport","metric":1e-400},'
           '{"agent_id":"unknown","result":1e400,"state":"running"},'
           '{"agent_id":"failed","status":"error","details":{"value":-0}}]}')
    _, view = deliver(name, raw)
    assert 'Exact\nReport' in view and 'metric: 1e-400' in view
    assert '{"agent_id":"unknown","result":1e400,"state":"running"}' in view
    assert '{"agent_id":"failed","status":"error","details":{"value":-0}}' in view
    assert all(f'results[{index}]' in view for index in range(3))


@pytest.mark.parametrize('name', ['get_agent_result', 'poll'])
@pytest.mark.parametrize('metadata', [
    '"evidence":"first","evidence":"second"',
    '"details":{"value":1,"value":2}',
    '"value":NaN', '"value":Infinity', '"value":-Infinity',
])
def test_ambiguous_or_non_json_report_retains_the_exact_original(name, metadata):
    raw = '{"status":"ok","final_answer":"Exact report",' + metadata + '}'
    source, view = deliver(name, raw)
    assert view == source


@pytest.mark.parametrize('raw', [
    '{"status":"error","details":[\n {"final_answer":"partial evidence"}\n]}',
    '[\n {"final_answer":"unrecognized array"}\n]',
    '{"results":[\n {"final_answer":"ambiguous evidence"}\n],"results":[]}',
])
def test_poll_does_not_rewrite_fragments_of_an_unprojected_json_document(raw):
    assert project_model_source('poll', raw) == raw


def test_deep_unrecognized_metadata_never_turns_projection_into_execution_failure():
    raw = '{"final_answer":"Exact report","metadata":' + '[' * 400 + '1e400' + ']' * 400 + '}'
    view = project_model_source('get_agent_result', raw)
    assert '1e400' in view
    assert 'Infinity' not in view


@pytest.mark.parametrize('name', ['get_agent_result', 'poll'])
@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
def test_exact_report_evidence_survives_canonical_budget_and_provider_preparation(name, protocol, monkeypatch):
    from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
    from lib.tools.result_envelope import split_tool_result_delivery
    from lib.llm._sse_core import prepare_request

    raw = '{"status":"ok","final_answer":"Exact\\nReport","evidence":{"value":1e400,"zero":-0}}'
    source = '== worker (swarm agent) ==\n' + raw if name == 'poll' else raw
    retained = []
    def retain(canonical, *, user_id):
        retained.append((user_id, canonical.text()))
        return 'tool-result:' + 'a' * 64
    monkeypatch.setattr('lib.tools.result_value.persist_result', retain)
    result = budget_tool_result_v2(name, source, user_id=73)
    delivery = split_tool_result_delivery(result)
    assert retained == [(73, source)] and result.canonical.text() == source
    assert not delivery.evidence['truncated']
    assert delivery.evidence['viewCoverage'] == 'complete'
    assert 'evidence: {"value":1e400,"zero":-0}' in delivery.model_text

    messages = [
        {'role': 'user', 'content': 'Read the exact report evidence.'},
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'report-call', 'type': 'function',
             'function': {'name': name, 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'report-call', 'content': delivery.model_text},
    ]
    plan = prepare_request({'model': 'fixture-model', 'messages': messages},
        api_key='fixture-key', base_url='https://provider.invalid/v1', api_protocol=protocol)
    wire = json.loads(plan.body.payload)
    if protocol == 'openai':
        tool_message = next(item for item in wire['messages'] if item['role'] == 'tool')
        content = tool_message['content']
    elif protocol == 'responses':
        content = next(item['output'] for item in wire['input']
                       if item.get('type') == 'function_call_output')
    else:
        content = next(block['content'] for item in wire['messages']
                       for block in item['content'] if block.get('type') == 'tool_result')
    assert content == delivery.model_text
    assert b'Infinity' not in plan.body.payload
