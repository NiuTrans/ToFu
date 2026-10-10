"""Independent review final-wire tools consume complete original pages directly.

Real run/stage records, tool registry, policy resolver and provider serializer;
no transport/model calls. Ordinary executor calculation helpers stay available.
"""
from copy import deepcopy

import pytest

from tests.evolution.test_evolution_runs import runtime as runtime, finish, review, OWNER
from tests.evolution.test_evolution_source_tools import large_review
from tests.evolution.test_evolution_artifact_evidence import artifact_review

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_external_reindex(monkeypatch):
    monkeypatch.setattr('lib.evolution.maintenance.reindex_maintenance', lambda **kw: [])


def projected_wire(task, *, protocol):
    from lib.context_experiment_flags import normalize_context_experiment_flags
    from lib.tasks_pkg.model_config import _assemble_tool_list
    from lib.tasks_pkg.tool_orchestration_policy import resolve_tool_orchestration
    from lib.llm._sse_core import prepare_request
    config = deepcopy(task['config'])
    config['userId'] = OWNER
    tools, _ = _assemble_tool_list(config, config.get('projectPath'),
        config.get('projectEnabled', False), task['id'],
        config.get('searchMode', 'off'), config.get('searchMode') != 'off',
        config.get('fetchEnabled', False), config.get('codeExecEnabled', False),
        False, False, human_guidance_enabled=config.get('humanGuidanceEnabled', False))
    messages = [{'role': 'user', 'content': 'Inspect all original evidence and return the independent judgment.'}]
    from lib.model_info._openai_gpt56 import GPT56_ALIAS_TARGET
    model = {'openai': 'gpt-4o', 'anthropic': 'claude-opus-4-6',
             'responses': GPT56_ALIAS_TARGET}[protocol]
    catalog = config['_executableToolCatalog']
    authority_names = {tool.get('function', {}).get('name') or tool.get('name')
                       for tool in catalog}
    if config['evolution']['stage'] == 'reviewing':
        expected = {'evolution_source', 'evolution_review'} if config['toolSpecAllowlist'] else set()
        assert authority_names == expected
    flags = normalize_context_experiment_flags(config)
    decision = resolve_tool_orchestration(
        requested_programmatic=flags['tools']['programmaticCalling'],
        requested_programmatic_exposure=flags['tools']['programmaticExposure'],
        requested_multi_agent='off', messages=messages, tools=tools,
        round_num=1, model=model)
    body = {'model': model, 'messages': messages, 'tools': tools,
        '_executable_tool_catalog': catalog, '_tool_wire_catalog': tools,
        '_tool_discovery_policy_by_name': config['_toolDiscoveryPolicyByName'],
        '_tool_search_catalog_size': config['_toolSearchCatalogSize'],
        '_tool_searchable_count': config['_toolSearchableCount'],
        '_programmatic_tool_calling': decision['programmaticCalling'],
        '_programmatic_tier': decision['programmaticTier'],
        '_programmatic_eligible_tools': decision['programmaticEligibleTools']}
    if protocol == 'responses':
        body['_responses_features_profile'] = 'openai'
    plan = prepare_request(body, api_key='unit-placeholder-not-a-credential',
        base_url='https://unit.invalid', api_protocol=protocol)
    return {x.get('function', {}).get('name') or x.get('name')
            for x in plan.body.get('tools') or []}


@pytest.mark.parametrize('source_kind', ['deliverable', 'tool_evidence', 'artifact_evidence'])
@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
def test_source_review_final_wire_has_direct_reader_and_submission(runtime, tmp_path, source_kind, protocol):
    if source_kind == 'deliverable':
        run, executor, reviewer = large_review(runtime)
    elif source_kind == 'artifact_evidence':
        run, executor, reviewer, _, _ = artifact_review(runtime, tmp_path)
    else:
        runtime[0].submit_run({'objective': 'Assess the observed tool result.',
            'max_attempts': 1}, user_id=OWNER)
        executor = runtime[2][-1]
        executor['messages'] = [{'role': 'tool', 'tool_call_id': 'full-observation',
            'content': 'Observed result 🧪\n' * 1000}]
        run = finish(runtime, executor, 'Inspect the complete retained tool evidence.')
        reviewer = runtime[2][-1]
    assert run['status'] == 'reviewing'
    ref_key = 'result_source' if source_kind == 'deliverable' else source_kind + '_source'
    assert run[ref_key]
    assert projected_wire(reviewer, protocol=protocol) == {'evolution_source', 'evolution_review'}
    # The serialized policy has not performed a read or granted coverage.
    final = finish(runtime, reviewer, review())
    assert final['status'] == 'needs_attention'
    assert next(x for x in final['review']['criterion_results']
                if x['criterion_id'] == 'objective')['outcome'] == 'unknown'


def test_executor_keeps_ordinary_computation_helpers(runtime):
    runtime[0].submit_run({'objective': 'Analyze and implement a causal alternative.'}, user_id=OWNER)
    names = projected_wire(runtime[2][-1], protocol='openai')
    assert names.intersection({'execute_program', 'execute_tools'})


def test_inline_review_still_settles_without_tool_or_coverage_requirement(runtime):
    runtime[0].submit_run({'objective': 'Calculate 8 times 9.'}, user_id=OWNER)
    run = finish(runtime, runtime[2][-1], '8 times 9 is 72.')
    reviewer = runtime[2][-1]
    assert not projected_wire(reviewer, protocol='openai')
    completed = finish(runtime, reviewer, review())
    assert completed['status'] == 'completed'
    assert completed['actuals']['tokens'] == 30
