"""Model-facing dataflow guidance follows admitted capabilities and context lifecycle."""
import copy
from dataclasses import replace

import pytest

from lib.tasks_pkg.context_composer import ComposeRequest, context_block, render_context
from lib.tasks_pkg.context_composer import _providers as providers
from lib.tasks_pkg.context_composer.registry import injection_spec
from lib.tools.contracts import compile_execution_contract_documents
from lib.tools.gateway import execute_program_schema
from lib.token_counter import count_text

pytestmark = pytest.mark.unit


def request(*, wire=(), admitted=False, mode='on', real=True):
    task = {'config': {'tools': {'programmaticCalling': mode}}}
    if admitted:
        task['_tool_gateway_names'] = ['execute_program']
        task['_toolContractDocumentsByName'] = compile_execution_contract_documents(
            [execute_program_schema()], namespace='gateway')
    return ComposeRequest(user_id=7, has_real_tools=real, tool_names=frozenset(wire), task=task)


@pytest.mark.parametrize('source', ['wire', 'deferred', 'admitted_gateway', 'legacy_wire'])
def test_guidance_recognizes_the_actual_or_admitted_action(source):
    value = request(wire=['execute_program'] if source == 'wire' else
                    ['execute_tools'] if source == 'legacy_wire' else [],
                    admitted=source == 'admitted_gateway')
    if source == 'deferred':
        value.task['_executable_tool_catalog'] = [execute_program_schema()]
    text = providers._programmatic_guidance(value)
    from lib.tasks_pkg.context_composer.registry import injection_spec
    from lib.token_counter import count_text
    assert text.startswith('<programmatic_execution>')
    assert text.endswith('</programmatic_execution>')
    assert count_text(text, model='gpt-4') <= injection_spec('programmatic_execution').max_tokens
    # Operations belong to the action schema; the context guide carries policy.
    action = execute_program_schema()['function']
    interface = text + action['description'] + action['parameters']['properties']['code'].get('description', '')
    for operation in ('tools.invoke', 'tools.result', 'tools.inputs', 'tools.keep', 'tools.observe'):
        assert operation in interface
    assert 'programInput.read' in interface and 'jsonType' in interface

    assert 'Prefer direct tools for single actions' in text
    assert 'execute_tools for known independent batches' in text
    assert 'statements ending in return' in text
    assert 'Return routine results' in text
    assert 'tools.observe ends the script only for unresolved judgment' in text
    assert 'Never replay completed actions' in text


@pytest.mark.parametrize('defect', ['configuration_only', 'names_only', 'document_only',
                                   'wrong_name', 'wrong_namespace', 'no_real_tools'])
def test_partial_or_absent_admission_never_advertises_program_capability(defect):
    value = request(admitted=defect != 'configuration_only', real=defect != 'no_real_tools')
    if defect == 'names_only':
        value.task.pop('_toolContractDocumentsByName')
    elif defect == 'document_only':
        value.task.pop('_tool_gateway_names')
    elif defect in {'wrong_name', 'wrong_namespace'}:
        value.task['_toolContractDocumentsByName']['execute_program'][
            'name' if defect == 'wrong_name' else 'namespace'] = 'foreign'
    assert providers._programmatic_guidance(value) == ''


@pytest.mark.parametrize('model', ['gpt-5.4', 'kimi-k3', 'deepseek-v4-pro', 'deepseek-v4-flash'])
def test_registered_guidance_fits_its_budget_without_clipping(model):
    value = replace(request(admitted=True), model=model)
    text = providers._programmatic_guidance(value)
    assert count_text(text, model=model) <= injection_spec('programmatic_execution').max_tokens
    composed = render_context([{'role':'user', 'content':'Read and aggregate data.'}],
                              [context_block('programmatic_execution', text)], value)
    receipt = composed.manifest[0]
    assert receipt['injected'] is True
    # Budget the complete model-visible carrier, including attribution/markers.
    assert receipt['tokens'] <= injection_spec('programmatic_execution').max_tokens
    assert receipt['appendedTokens'] <= injection_spec('programmatic_execution').max_tokens
    assert receipt.get('clipped') is not True
    assert receipt['authority'] == 'workflow'


def test_guidance_is_stable_across_rounds_catalog_order_and_observed_results():
    first = request(admitted=True)
    later = copy.deepcopy(first)
    later.task.update(_round=9, toolRounds=[{'output':'large source data'}])
    later = replace(later, tool_names=frozenset({'custom__read_dataset', 'custom__list_datasets'}))
    assert providers._programmatic_guidance(first) == providers._programmatic_guidance(later)


def test_explicit_off_is_authoritative_even_when_no_catalog_is_available(monkeypatch):
    monkeypatch.setattr(providers.system_prompt_base, 'build_static_prompt', lambda **kw: '')
    for name in ('_profile_block', '_memory_guidance', '_swarm_guidance'):
        monkeypatch.setattr(providers, name, lambda *a, **kw: '')
    monkeypatch.setattr(providers, '_evolution_guidance', lambda *a: ('', 'disabled'))
    monkeypatch.setattr(providers, '_project_blocks', lambda *a: [])
    monkeypatch.setattr(providers, '_skill_context', lambda *a: {
        'index':'', 'selection':{'content':'', 'receipts':[]}})
    messages = [{'role':'user', 'content':'Continue.'}]
    prior = providers._programmatic_guidance(request(admitted=True))
    render_context(messages, [context_block('programmatic_execution', prior)], request(admitted=True))
    value = request(mode='off', real=False)
    block = next(b for b in providers.collect_context_blocks(messages, value)
                 if b.id == 'programmatic_execution')
    assert block.content == ''
    assert block.suppressed_reason == 'programmatic_disabled'
    result = render_context(messages, [block], value)
    assert result.manifest[0]['reason'].endswith('retracted:programmatic_disabled')


def test_real_gateway_assembly_supplies_the_deferred_context_capability():
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import attach_gateway_contract_documents
    value = request(wire=['custom__read_dataset'])
    value.task['_toolContractDocumentsByName'] = {}
    assert attach_gateway_contract_documents(value.task, 'off') is True
    assert 'execute_program' in value.task['_tool_gateway_names']
    assert 'execute_program' not in value.tool_names
    assert providers._programmatic_guidance(value)


@pytest.mark.parametrize('wire', [[], ['execute_tools'], ['execute_program']])
def test_auto_never_injects_javascript_guidance_from_stale_capabilities(wire, monkeypatch):
    monkeypatch.delenv('TOFU_PTC_TIER', raising=False)
    assert providers._programmatic_guidance(request(mode='auto', wire=wire, admitted=True)) == ''


def test_explicit_batch_override_withdraws_program_guidance(monkeypatch):
    monkeypatch.setenv('TOFU_PTC_TIER', 'batch')
    assert providers._programmatic_guidance(request(admitted=True)) == ''
