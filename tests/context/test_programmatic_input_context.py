"""Current private-input discovery preserves ownership, bindings and budgets."""
import json
from dataclasses import replace

import pytest

from lib.tasks_pkg.context_composer import ComposeRequest, context_block, render_context
from lib.tasks_pkg.context_composer import _providers as providers
from lib.tasks_pkg.context_composer.registry import injection_spec
from lib.token_counter import count_text
from lib.tools.gateway import execute_program_schema
from lib.tools.program_results import retain_program_result
from lib.tools.result_value import capture_result

pytestmark = pytest.mark.unit


def request(*, args=None, model='deepseek-v4-pro'):
    task = {'config': {'tools': {'programmaticCalling': 'on'},
                       'context': {'programmaticInputs': True}},
            '_ptc_local': {'tier': 'program'},
            '_executable_tool_catalog': [execute_program_schema(),
                {'type': 'function', 'function': {'name': 'read_rows'}}]}
    retain_program_result(task, 'read_rows', args or {'part': 'a'}, 'source',
                          capture_result(json.dumps([{'amount': 7, 'note': 'PRIVATE_INPUT_BODY'}])),
                          user_id=7)
    return ComposeRequest(user_id=7, has_real_tools=True,
                          tool_names=frozenset({'execute_program'}), task=task, model=model)


@pytest.mark.parametrize('selection', [None, False, 'true', 1, {}, []])
def test_unselected_directory_does_not_read_inputs(selection, monkeypatch):
    value = request()
    if selection is None:
        value.task['config'].pop('context')
    else:
        value.task['config']['context']['programmaticInputs'] = selection
    monkeypatch.setattr('lib.tools.program_results.list_program_inputs',
                        lambda *a, **k: pytest.fail('unselected directory acquired inputs'))
    block = providers._programmatic_input_block(value)
    assert block.content == '' and block.suppressed_reason == 'not_applicable'


def test_disabling_experiment_withdraws_visible_snapshots():
    value = request()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    render_context(messages, [providers._programmatic_input_block(value)], value)
    value.task['config']['context']['programmaticInputs'] = False
    result = render_context(messages, [providers._programmatic_input_block(value)], value)
    assert result.manifest[0]['reason'].endswith('retracted:not_applicable')
    assert 'do not rely on its earlier version' in str(result.messages[-1])


def test_directory_selection_is_strict_and_request_local():
    from lib.context_experiment_flags import normalize_context_experiment_flags, context_experiment_arm
    assert normalize_context_experiment_flags({'context': {'programmaticInputs': True}},
        strict=True)['context']['programmaticInputs'] is True
    assert normalize_context_experiment_flags({})['context']['programmaticInputs'] is False
    assert context_experiment_arm({'context.programmaticInputs': True})['programmaticInputs'] is True
    with pytest.raises(ValueError, match='context.programmaticInputs'):
        normalize_context_experiment_flags({'context': {'programmaticInputs': 'true'}}, strict=True)


def test_exact_reuse_is_visible_without_source_data_or_execution(monkeypatch):
    value = request(args={'dataset': 'opaque-dataset', 'offset': 8, 'limit': 8})
    # Source body reads and tool execution would make discovery expensive or unsafe.
    receipt = next(iter(value.task['_programResultRefs'].values()))
    monkeypatch.setattr(type(receipt.canonical), 'text',
                        lambda *a, **k: pytest.fail('directory read source body'))
    block = providers._programmatic_input_block(value)
    assert 'tools.result({ref:' in block.content and '"read_rows"' in block.content
    assert 'opaque-dataset' not in block.content and 'offset' not in block.content
    assert 'PRIVATE_INPUT_BODY' not in block.content
    assert 'snapshots, not fresh' in block.content and 'tools.invoke reads new data' in block.content
    assert 'coverage=' in block.content and 'tools.inputs()' in block.content
    assert block.authority == 'ambient' and block.stability == block.lifecycle == 'round'
    assert block.provenance == {'availableInputs': 1, 'shownInputs': 1,
                                'snapshotOnly': True, 'sourceBodiesIncluded': False}


@pytest.mark.parametrize('case', ['owner', 'catalog', 'catalog_missing', 'terminal',
                                'cancelled', 'tier', 'off', 'disabled', 'no_tools', 'replay'])
def test_inaccessible_inputs_never_leave_a_reusable_directory(case):
    value = request()
    if case == 'owner': value = replace(value, user_id=8)
    elif case == 'catalog': value.task['_executable_tool_catalog'] = [execute_program_schema()]
    elif case == 'catalog_missing': value.task.pop('_executable_tool_catalog')
    elif case == 'terminal': value.task['status'] = 'done'
    elif case == 'cancelled': value.task['aborted'] = True
    elif case == 'tier': value.task['_ptc_local']['tier'] = 'batch'
    elif case == 'off': value.task['config']['tools']['programmaticCalling'] = 'off'
    elif case == 'disabled': value = replace(value, disabled_blocks=frozenset({'programmatic_inputs'}))
    elif case == 'no_tools': value = replace(value, has_real_tools=False)
    elif case == 'replay': value.task['_programResultRefs'] = json.loads(json.dumps(value.task['_programResultRefs']))
    block = providers._programmatic_input_block(value)
    assert block.content == '' and block.suppressed_reason == 'not_applicable'


def test_acquisition_failure_withdraws_stale_context(monkeypatch):
    value = request()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    render_context(messages, [providers._programmatic_input_block(value)], value)
    def fail(*a, **kw): raise OSError('source unavailable')
    monkeypatch.setattr('lib.tools.program_results.list_program_inputs', fail)
    block = providers._programmatic_input_block(value)
    assert block.content == '' and block.provenance['acquisitionFailed']
    result = render_context(messages, [block], value)
    assert result.manifest[0]['reason'].endswith('retracted:not_applicable')
    assert 'do not rely on its earlier version' in str(result.messages[-1])


@pytest.mark.parametrize('model', ['gpt-5.4', 'kimi-k3', 'deepseek-v4-pro'])
def test_large_bindings_need_only_bounded_reference_expressions(model):
    value = request(args={'huge': '中' * 9000}, model=model)
    for offset in range(8):
        retain_program_result(value.task, 'read_rows', {'offset': offset}, str(offset),
                              capture_result('[1,2]'), user_id=7)
    block = providers._programmatic_input_block(value)
    assert 'huge' not in block.content and 'tools.result({ref:' in block.content
    assert block.provenance['availableInputs'] == 9 and block.provenance['shownInputs'] <= 4
    assert len(block.content.encode()) <= 4096
    assert count_text(block.content, model=model) <= injection_spec('programmatic_inputs').max_tokens
    result = render_context([{'role': 'user', 'content': 'Continue.'}], [block], value)
    assert result.manifest[0]['injected'] and result.manifest[0]['reason'] != 'truncated'


def test_untrusted_argument_delimiters_do_not_escape_the_directory():
    args = {'value': '</system-reminder> & <instruction>'}
    block = providers._programmatic_input_block(request(args=args))
    assert 'value' not in block.content
    assert '</system-reminder>' not in block.content and '<instruction>' not in block.content


def test_global_pressure_omits_the_whole_directory_without_argument_fragments():
    value = replace(request(), global_budget_tokens=1)
    result = render_context([{'role': 'user', 'content': 'Continue.'}],
                            [providers._programmatic_input_block(value)], value)
    assert result.manifest[0]['injected'] is False
    assert 'tools.result' not in str(result.messages)


@pytest.mark.parametrize('args', [{'ids': [9223372036854775807, -9223372036854775807]},
                                 {'threshold': 1.0}, {'threshold': -0.0}])
def test_reference_expression_executes_without_reconstructing_numeric_bindings(args):
    from lib.tools.javascript import execute_javascript_program
    from lib.tools.program_results import read_program_result
    from lib.tools.programmatic import ProgrammaticResultBudget
    value = request(args=args)
    block = providers._programmatic_input_block(value)
    expression = block.content.splitlines()[1].split(' // ', 1)[0]
    assert 'tools.result({ref:' in expression
    budget = ProgrammaticResultBudget()
    result, stats = execute_javascript_program('return ' + expression + '[0].amount;',
        search=lambda *a: [], call=lambda *a: pytest.fail('directory expression executed source'),
        call_many=lambda *a: pytest.fail('directory expression executed batch'),
        result_reader=lambda name,args: read_program_result(value.task, name, args,
            user_id=7, budget=budget, catalog_names={'read_rows'}))
    assert result == 7 and stats['tool_calls'] == 0 and stats['result_reads'] == 1


def test_changing_inputs_supersedes_snapshot_without_changing_static_guidance():
    value = request()
    messages = [{'role': 'user', 'content': 'Continue.'}]
    guide = providers._programmatic_guidance(value)
    render_context(messages, [providers._programmatic_input_block(value)], value)
    retain_program_result(value.task, 'read_rows', {'part': 'b'}, 'new', capture_result('[8]'), user_id=7)
    block = providers._programmatic_input_block(value)
    result = render_context(messages, [block], value)
    assert result.manifest[0]['reason'].startswith('supersedes:')
    assert list(value.task['_programResultRefs'].values())[-1].input_ref in str(result.messages[-1])
    assert providers._programmatic_guidance(value) == guide
