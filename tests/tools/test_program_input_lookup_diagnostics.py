"""Missing retained evidence stays distinguishable from an empty source.

Exercise the real WASM bridge and complete-input bank; no source is re-executed
or promoted from a partial tool result while recovering an authoring failure.
"""
import json
from dataclasses import replace

import pytest

from lib.tools.javascript import execute_javascript_program
from lib.tools.program_contract import ProgramExecutionError
from lib.tools.program_results import (
    list_program_inputs, read_program_result, retain_program_result,
)
from lib.tools.programmatic import ProgrammaticResultBudget
from lib.tools.result_value import capture_result

pytestmark = pytest.mark.unit


@pytest.fixture
def inputs():
    task = {'_ptc_local': {'tier': 'program'}}
    effects = []
    catalog = {'get_conversation'}

    def run(source):
        budget = ProgrammaticResultBudget()
        return execute_javascript_program(
            source, search=lambda *args: [],
            call=lambda *args: effects.append(args),
            call_many=lambda *args: effects.append(args),
            input_lister=lambda query: list_program_inputs(
                task, query, user_id=7, catalog_names=catalog),
            result_reader=lambda name, args: read_program_result(
                task, name, args, user_id=7, budget=budget, catalog_names=catalog))

    return task, effects, run


def test_partial_source_is_not_retained_and_unchecked_ref_reports_empty_lookup(inputs):
    task, effects, run = inputs
    partial = replace(capture_result(json.dumps({
        'status': 'partial', 'truncated': True,
        'summary': 'PRIVATE_SOURCE', 'artifactRef': 'tool-result:historical',
    })), source_coverage='partial')
    retain_program_result(task, 'get_conversation', {}, 'source', partial, user_id=7)

    with pytest.raises(ProgramExecutionError) as caught:
        run("const page=tools.inputs({name:'get_conversation'}); "
            "return tools.result({ref:page.inputs[page.inputs.length-1].ref});")

    error = caught.value
    assert error.code == 'javascript_error'
    assert "cannot read property 'ref' of undefined" in str(error)
    assert error.detail['lastInputLookup'] == {
        'queryIndex': 1, 'name': 'get_conversation',
        'returned': 0, 'total': 0, 'nextOffset': None,
    }
    assert 'check length' in error.detail['retry_hint']
    assert 'tools.result(name,args)' in error.detail['retry_hint']
    assert 'read_tool_artifact' in error.detail['retry_hint']
    assert 'PRIVATE_SOURCE' not in json.dumps(error.as_dict())
    assert not effects
    assert error.execution_stats['tool_calls'] == error.execution_stats['result_reads'] == 0


def test_checked_required_reuse_hands_off_without_running_later_effects(inputs):
    _, effects, run = inputs
    result, stats = run(
        "try { return tools.result('get_conversation',{}); } "
        "finally { await tools.invoke('get_conversation',{}); }")
    assert result['kind'] == 'observation'
    assert result['reason'] == 'retained_result_unavailable'
    assert stats['tool_calls'] == stats['result_reads'] == 0
    assert not effects


def test_empty_directory_can_be_inspected_without_an_error_or_handoff(inputs):
    _, effects, run = inputs
    result, stats = run(
        "const {inputs}=tools.inputs({name:'get_conversation'}); "
        "if (!inputs.length) return {available:false}; "
        "return tools.result({ref:inputs[0].ref});")
    assert result == {'available': False}
    assert stats['outcome'] == 'completed'
    assert not effects


def test_page_counts_do_not_claim_an_empty_source_or_expose_private_inputs(inputs):
    task, effects, run = inputs
    retain_program_result(task, 'get_conversation', {'secret': 'PRIVATE_ARGUMENT'},
        'source', capture_result('{"body":"PRIVATE_BODY"}'), user_id=7)
    with pytest.raises(ProgramExecutionError) as caught:
        run("tools.inputs({name:'absent'}); "
            "const page=tools.inputs({name:'get_conversation',offset:16}); "
            "return page.inputs[0].ref;")
    detail = caught.value.detail
    assert detail['lastInputLookup'] == {
        'queryIndex': 2, 'name': 'get_conversation',
        'returned': 0, 'total': 1, 'nextOffset': None,
    }
    assert 'page is empty' in detail['retry_hint']
    assert all(value not in json.dumps(detail) for value in
               ('PRIVATE_ARGUMENT', 'PRIVATE_BODY', 'input_'))
    assert not effects


def test_successful_lookup_and_next_invocation_do_not_reuse_empty_page_advice(inputs):
    task, _, run = inputs
    retain_program_result(task, 'get_conversation', {}, 'source',
                          capture_result('{"count":1}'), user_id=7)
    with pytest.raises(ProgramExecutionError) as caught:
        run("tools.inputs({name:'absent'}); tools.inputs({name:'get_conversation'}); "
            "throw new Error('unrelated failure');")
    assert caught.value.detail['lastInputLookup']['returned'] == 1
    assert str(caught.value) == 'Error: unrelated failure'
    assert 'retry_hint' not in caught.value.detail
    with pytest.raises(ProgramExecutionError) as fresh:
        run("throw new Error('next invocation');")
    assert fresh.value.detail == {}
