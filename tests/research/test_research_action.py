"""Least-authority and evidence gates for agentic research actions."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _schema(name):
    return {'type': 'function', 'function': {
        'name': name, 'description': name,
        'parameters': {'type': 'object', 'properties': {}},
    }}


def _epoch(names):
    from lib.paper.tools import PaperToolEpochV2

    schemas = tuple(_schema(name) for name in names)
    return PaperToolEpochV2(
        wire_schemas=schemas, executable_schemas=schemas,
        contract_documents_by_name={name: {'schema_hash': name} for name in names},
        discovery_policy_by_name={name: 'eager' for name in names},
        namespace_by_name={name: 'test' for name in names},
        search_text_by_name={name: name for name in names},
        script_safe_by_name={name: False for name in names},
        schema_tokens=1, gateway_schema_tokens=0, schema_budget_tokens=100,
        result_envelope='v2', epoch_hash='full',
    )


def test_action_epoch_exposes_only_native_allowlist_and_exact_binding(monkeypatch):
    import lib.research.action as action

    bound = 'mcp__any-provider__launch'
    unbound = 'mcp__any-provider__delete_all'
    monkeypatch.setattr(action, 'build_paper_full_tool_epoch', lambda **_kwargs: _epoch([
        'web_search', 'fetch_url', 'run_command', 'create_memory', bound, unbound,
    ]))
    workspace = {'capability_bindings': [{
        'capability': 'experiment.execute', 'provider': 'mcp',
        'tool': bound, 'schema_hash': 'one', 'enabled': True,
        'argument_defaults': {}, 'notes': '',
    }]}
    catalog = {'tools': [
        {'name': bound, 'read_only': False, 'schema_hash': 'one'},
        {'name': unbound, 'read_only': False, 'schema_hash': 'two'},
    ]}
    epoch, bindings, problems = action.build_action_tool_epoch(
        action='experiment', user_id=1, workspace=workspace,
        confirm_external_writes=True, catalog=catalog)
    names = {row['function']['name'] for row in epoch.executable_schemas}
    assert names == {'web_search', 'fetch_url', 'run_command', bound}
    assert bindings[0]['tool'] == bound and not problems


def test_write_actions_require_explicit_confirmation():
    from lib.research.action import build_action_tool_epoch

    with pytest.raises(ValueError, match='confirm_external_writes'):
        build_action_tool_epoch(
            action='experiment', user_id=1, workspace={},
            confirm_external_writes=False, catalog={'tools': []})


def test_research_context_preserves_abort_signal(monkeypatch):
    import lib.research.action as action
    import lib.research.persistence as persistence
    from lib.llm.llm_errors import AbortedError

    def abort(*_args, **_kwargs):
        raise AbortedError('owner stopped the task')

    monkeypatch.setattr(persistence, 'load_research_artifacts', abort)
    with pytest.raises(AbortedError, match='owner stopped'):
        action._research_context('Causal compression', 'en', 1)


def test_bound_argument_defaults_are_applied_but_call_arguments_win():
    from lib.research.action import _merge_bound_arguments

    got = _merge_bound_arguments(
        'mcp__provider__run', {'seed': 7}, [{
            'tool': 'mcp__provider__run',
            'argument_defaults': {'queue': 'research', 'seed': 3},
        }])
    assert got == {'queue': 'research', 'seed': 7}


def test_experiment_cannot_pass_without_a_successful_tool_receipt():
    from lib.research.action import apply_action_result
    from lib.research.workspace import empty_workspace

    workspace = empty_workspace('Causal compression')
    updated = apply_action_result(
        action='experiment', workspace=workspace,
        payload={'run': {'id': 'r1', 'status': 'passed', 'metric': '99'}},
        receipts=[], task_id='task-1', bindings=[])
    assert updated['runs'][0]['status'] == 'planned'
    assert updated['runs'][0]['task_id'] == 'task-1'


def test_experiment_ignores_search_receipts_and_invented_artifact_refs():
    from lib.research.action import apply_action_result
    from lib.research.workspace import empty_workspace

    updated = apply_action_result(
        action='experiment', workspace=empty_workspace('Evidence contracts'),
        payload={'run': {
            'id': 'r1', 'status': 'passed',
            'artifact_refs': ['artifact://invented'],
        }},
        receipts=[{
            'tool': 'web_search', 'status': 'done',
            'artifact_ref': 'evidence:search', 'result_excerpt': 'papers only',
        }],
        task_id='task-1', bindings=[])
    assert updated['runs'][0]['status'] == 'planned'
    assert updated['runs'][0]['artifact_refs'] == []


def test_analysis_downgrades_unobserved_supported_claim_and_visual():
    from lib.research.action import apply_action_result
    from lib.research.workspace import empty_workspace

    updated = apply_action_result(
        action='analyze', workspace=empty_workspace('Evidence contracts'),
        payload={
            'claims': [{
                'id': 'c1', 'text': 'Large gain', 'status': 'supported',
                'evidence_refs': ['artifact://invented'],
            }],
            'figures': [{
                'id': 'f1', 'status': 'verified',
                'data_ref': 'data://invented', 'script_ref': 'code://invented',
                'output_ref': 'figure://invented',
            }],
        },
        receipts=[], task_id='task-1', bindings=[])
    assert updated['claims'][0]['status'] == 'draft'
    assert updated['claims'][0]['evidence_refs'] == []
    assert updated['figures'][0]['status'] == 'planned'
    assert updated['figures'][0]['output_ref'] == ''


def test_publish_cannot_claim_success_from_model_text_alone():
    from lib.research.action import apply_action_result
    from lib.research.workspace import empty_workspace

    workspace = empty_workspace('Evidence contracts')
    workspace['source_files'] = [{'path': 'main.tex', 'content': 'paper'}]
    binding = {'capability': 'publication.push', 'tool': 'mcp__vendor__push'}
    rejected = apply_action_result(
        action='publish', workspace=workspace,
        payload={'status': 'published', 'project_url': 'https://example.test/p'},
        receipts=[], task_id='task-1', bindings=[binding])
    assert rejected['publication']['status'] == 'failed'

    accepted = apply_action_result(
        action='publish', workspace=workspace,
        payload={'status': 'published', 'project_url': 'https://example.test/p'},
        receipts=[{'tool': binding['tool'], 'status': 'done'}],
        task_id='task-2', bindings=[binding])
    assert accepted['publication']['status'] == 'published'
    assert accepted['publication']['source_digest']
    assert accepted['publication']['provider'] == 'vendor'
    assert accepted['publication']['project_url'] == ''


@pytest.mark.parametrize('status,expected', [
    (None, 'pending'), ('', 'pending'), ('pending', 'pending'),
    ('searching', 'pending'), ('running', 'pending'),
    ('error', 'error'), ('failed', 'error'), ('rejected', 'rejected'),
    ('done', 'done'),
])
def test_receipt_requires_explicit_terminal_success(status, expected):
    from lib.research.action import _tool_receipts, apply_action_result
    from lib.research.workspace import empty_workspace

    row = {'toolName': 'run_command',
           'toolContent': '{"scientific_outcome":"supported"}'}
    if status is not None:
        row['status'] = status
    receipts = _tool_receipts([row])
    assert receipts[0]['status'] == expected
    assert bool(receipts[0]['artifact_ref']) == (expected == 'done')
    updated = apply_action_result(
        action='experiment', workspace=empty_workspace('Receipt authority'),
        payload={'run': {'id': 'r1', 'status': 'passed'}},
        receipts=receipts, task_id='t1', bindings=[])
    run = updated['runs'][0]
    assert run['scientific_outcome'] == (
        'supported' if expected == 'done' else 'inconclusive')
    if expected != 'done':
        assert run['execution_status'] != 'completed'


@pytest.mark.parametrize('capability', ['compute.submit', 'compute.status'])
@pytest.mark.parametrize('remote_status,expected', [
    (None, 'planned'), ('success', 'planned'), ('queued', 'queued'),
    ('pending', 'queued'), ('running', 'running'), ('failed', 'failed'),
    ('completed', 'completed'),
])
def test_compute_api_success_is_not_experiment_completion(capability, remote_status, expected):
    import json
    from lib.research.action import _tool_receipts, apply_action_result
    from lib.research.workspace import empty_workspace

    observed = {'ok': True, 'scientific_outcome': 'supported', 'job_id': 'j1'}
    if remote_status is not None:
        observed['job_status'] = remote_status
    tool = 'mcp__compute__job'
    receipts = _tool_receipts([{
        'toolName': tool, 'status': 'done', 'toolContent': json.dumps(observed)}])
    updated = apply_action_result(
        action='experiment', workspace=empty_workspace('Remote jobs'),
        payload={'run': {'id': 'r1', 'status': 'passed', 'remote_job_id': 'j1'},
                 'claims': [{'id': 'c1', 'text': 'Works', 'status': 'supported',
                             'evidence_refs': ['r1']}]},
        receipts=receipts, task_id='t1',
        bindings=[{'capability': capability, 'tool': tool}])
    run = updated['runs'][0]
    assert run['execution_status'] == expected
    assert run['remote_job_id'] == 'j1'
    assert run['scientific_outcome'] == (
        'supported' if expected == 'completed' else 'inconclusive')
    assert bool(run['finished_at']) == (expected in {'failed', 'completed'})
    if expected != 'completed':
        assert not run['artifact_refs']
        assert updated['claims'][0]['status'] != 'supported'


def test_compute_status_poll_can_complete_queued_submission():
    from lib.research.action import _tool_receipts, apply_action_result
    from lib.research.workspace import empty_workspace

    receipts = _tool_receipts([
        {'toolName': 'submit', 'status': 'done',
         'toolContent': '{"job":{"status":"queued"}}'},
        {'toolName': 'poll', 'status': 'done',
         'toolContent': '{"job":{"status":"completed"},"scientific_outcome":"refuted"}'},
    ])
    updated = apply_action_result(
        action='experiment', workspace=empty_workspace('Remote jobs'),
        payload={'run': {'id': 'r1'}}, receipts=receipts, task_id='t1',
        bindings=[{'capability': 'compute.submit', 'tool': 'submit'},
                  {'capability': 'compute.status', 'tool': 'poll'}])
    run = updated['runs'][0]
    assert run['execution_status'] == 'completed'
    assert run['scientific_outcome'] == 'refuted'
    assert run['artifact_refs'] == [receipts[1]['artifact_ref']]


@pytest.mark.parametrize('abort_during', ['agent', 'apply'])
def test_abort_after_agent_never_saves_workspace(monkeypatch, abort_during):
    import threading
    from types import SimpleNamespace
    from unittest.mock import Mock
    import lib.research.action as action
    import lib.research.action_runtime as runtime
    import lib.research.workspace as workspace

    abort = threading.Event()
    task = {'task_id': 'abort-test', 'action': 'experiment', 'user_id': 1,
            'direction': 'Stop safely', 'lang': 'en', 'expected_revision': 0,
            'confirm_external_writes': True, 'abort_event': abort}
    owner = SimpleNamespace(mark_running=Mock(), abort=Mock(), finish=Mock())
    monkeypatch.setattr(runtime, '_research_action_runtime', owner)
    monkeypatch.setattr(runtime, 'append_action_event', Mock())
    monkeypatch.setattr(workspace, 'load_workspace', lambda *a, **k: {'revision': 0, 'protocol': {'decision_rule': {
        'direction':'higher','minimum_effect':0,'uncertainty_multiplier':2},'random_seeds':[1,2,3],'dataset':'held-out','primary_metric':'accuracy','baseline':'reference'}})
    save = Mock()
    monkeypatch.setattr(workspace, 'save_workspace', save)
    monkeypatch.setattr(action, 'build_action_tool_epoch',
                        lambda **k: (_epoch([]), [], []))

    def agent(*args):
        if abort_during == 'agent':
            abort.set()
        return {}, []

    def apply(**kwargs):
        abort.set()
        return {}

    apply_mock = Mock(side_effect=apply)
    monkeypatch.setattr(action, '_run_agent', agent)
    monkeypatch.setattr(action, 'apply_action_result', apply_mock)
    action.run_research_action(task)
    save.assert_not_called()
    owner.abort.assert_called_once_with('abort-test')
    assert owner.finish.call_args.kwargs['terminal_event_fields']['type'] == 'aborted'
    assert apply_mock.call_count == (0 if abort_during == 'agent' else 1)


def test_experiment_prompt_carries_protocol_specific_generation_contract(monkeypatch):
    from lib.research import action
    import json
    from copy import deepcopy
    monkeypatch.setattr(action,'_research_context',lambda *args:{})
    workspace={'protocol':{'measurement_scope':{'candidate_digest':'a'*64,'baseline_digest':'b'*64,
        'dataset_digest':'d'*64,'split_digest':'c'*64,'budget_unit':'training_steps','budget':100}},
        'hypothesis':'Frozen central hypothesis','runs':[]}
    before=deepcopy(workspace)
    messages=action._messages({'action':'experiment','direction':'Study','lang':'en','user_id':7},workspace,[])
    payload=json.loads(messages[-1]['content'])
    assert payload['workspace']['protocol']==workspace['protocol'] and workspace==before
    for system in ('method','baseline'):
        assert 'system_digest' in payload['scientific_observation_contract']['properties']['comparison']['properties'][system]['required']
    assert '"protocol":object' not in messages[0]['content']


def test_research_repeated_calls_leave_progress_decision_to_shared_loop(monkeypatch):
    import threading
    import lib.research.action as action

    monkeypatch.setattr(action, '_messages', lambda *args: [])
    monkeypatch.setattr(action, 'make_paper_exec_shim', lambda **kwargs: None)
    class InspectedLoop(Exception):
        pass
    def inspect_loop(*, usage_meter, **kwargs):
        call = {'tool_calls': [{'function': {'name': 'run_command',
                'arguments': '{"command":"inspect experiment progress"}'}}]}
        for round_index in range(1, 30):
            usage_meter.begin_dispatch()
            usage_meter.observe_agent_round(None, call)
        assert usage_meter.force_final_reason == ''
        assert usage_meter.agent_dispatches == 29
        raise InspectedLoop
    monkeypatch.setattr(action, 'run_guarded_paper_agent_loop', inspect_loop)
    with pytest.raises(InspectedLoop):
        action._run_agent({'abort_event': threading.Event(), 'model': 'test',
            'user_id': 1, 'task_id': 'progress-policy', 'tool_rounds': [],
            'action': 'experiment'}, {}, _epoch([]), [])


@pytest.mark.parametrize('excerpt,reference,accepted', [
    ('{"artifact_ref":"artifact:123"}', 'artifact:123', True),
    ('{"job":{"id":"job-42"}}', 'job-42', True),
    ('{"artifacts":["artifact:123"]}', 'artifact:123', True),
    ('{"artifact_ref":"artifact:1234"}', 'artifact:123', False),
    ('{"detail":"artifact:123 was not produced"}', 'artifact:123', False),
    ('{"artifact:123":false}', 'artifact:123', False),
    ('log prefix {"artifact_ref":"artifact:123"}', 'artifact:123', False),
])
def test_proposed_reference_requires_complete_observed_value(excerpt, reference, accepted):
    from lib.research.action import _observed_reference
    receipt = {'status': 'done', 'result_excerpt': excerpt}
    assert _observed_reference(reference, [receipt]) == (reference if accepted else '')
    receipt['status'] = 'error'
    assert _observed_reference(reference, [receipt]) == ''


def test_registered_reference_does_not_require_visible_excerpt():
    from lib.research.action import _observed_reference
    assert _observed_reference('artifact:123', [{'status': 'done',
        'artifact_ref': 'artifact:123', 'result_excerpt': 'truncated'}]) == 'artifact:123'
    assert _observed_reference('run-1', [], {'run-1'}) == 'run-1'


@pytest.mark.parametrize('action_name', ['experiment', 'analyze', 'manuscript', 'compile', 'publish'])
def test_stale_action_refuses_before_task_birth(monkeypatch, action_name):
    from lib.research import action, action_runtime, workspace
    monkeypatch.setattr(action_runtime._production, 'cleanup_stale', lambda: None)
    monkeypatch.setattr(action_runtime._production, 'index_get', lambda key: None)
    reads = []
    def load(direction, lang, *, user_id):
        reads.append((direction, lang, user_id))
        return {'revision': 4}
    def forbidden(*args, **kwargs):
        pytest.fail('stale action must not allocate or spawn a task')
    monkeypatch.setattr(workspace, 'load_workspace', load)
    monkeypatch.setattr(action_runtime._production, 'new_task_id', forbidden)
    monkeypatch.setattr(action_runtime, 'create_action_task', forbidden)
    monkeypatch.setattr(action_runtime._research_action_runtime, 'spawn', forbidden)
    with pytest.raises(ValueError, match='research_workspace_stale'):
        action.start_research_action('Owned question', action=action_name,
            expected_revision=3, lang='zh', confirm_external_writes=True, user_id=7)
    assert reads == [('Owned question', 'zh', 7)]


@pytest.mark.parametrize('directions', [
    ('Same owned hypothesis', 'Same owned hypothesis'),
    ('Same owned hypothesis', 'same  owned   HYPOTHESIS'),
    ('Same owned hypothesis', 'SAME\towned\nhypothesis'),
])
def test_simultaneous_action_starts_join_one_real_runtime_task(monkeypatch, directions):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from lib.production.runtime import ProductionRuntime
    from lib.research import action, action_runtime, workspace
    production = ProductionRuntime('research-action-claim-test', id_prefix='claim')
    monkeypatch.setattr(action_runtime, '_production', production)
    monkeypatch.setattr(action_runtime, '_research_action_runtime', production.runtime)
    barrier = threading.Barrier(2)
    def load(direction, lang, *, user_id):
        assert user_id == 7
        barrier.wait(timeout=10)
        return {'revision': 3}
    spawned = []
    monkeypatch.setattr(workspace, 'load_workspace', load)
    monkeypatch.setattr(production.runtime, 'spawn', lambda *args: spawned.append(args))
    def start(direction):
        return action.start_research_action(direction,
            action='manuscript', expected_revision=3, user_id=7)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, directions))
    assert results[0]['task_id'] == results[1]['task_id']
    assert sorted(row['deduped'] for row in results) == [False, True]
    assert len(spawned) == 1
    task = production.runtime.get_owned(results[0]['task_id'], user_id=7)
    assert task['direction'] in directions
    assert task['expected_revision'] == 3 and task['action'] == 'manuscript'
    assert production.runtime.get_owned(results[0]['task_id'], user_id=8) is None


@pytest.mark.parametrize('action_name', ['experiment', 'manuscript', 'analyze', 'publish', 'compile'])
def test_action_model_choice_is_preserved_in_live_task_identity(monkeypatch, action_name):
    from lib.production.runtime import ProductionRuntime
    from lib.research import action, action_runtime, workspace
    production = ProductionRuntime('research-model-identity-test', id_prefix='model')
    monkeypatch.setattr(action_runtime, '_production', production)
    monkeypatch.setattr(action_runtime, '_research_action_runtime', production.runtime)
    frozen = {'revision': 3, 'protocol': {
        'decision_rule': {'direction': 'higher', 'minimum_effect': 0, 'uncertainty_multiplier': 2},
        'random_seeds': [1, 2], 'dataset': 'held-out', 'primary_metric': 'accuracy', 'baseline': 'reference'}}
    monkeypatch.setattr(workspace, 'load_workspace', lambda *args, **kwargs: frozen)
    spawned = []
    monkeypatch.setattr(production.runtime, 'spawn', lambda *args: spawned.append(args))
    def start(model):
        return action.start_research_action('Owned comparison', action=action_name,
            expected_revision=3, confirm_external_writes=True, model=model, user_id=7)
    first, second = start('model-a'), start('model-b')
    repeated = start('model-a')
    assert repeated == {'task_id': first['task_id'], 'deduped': True}
    if action_name == 'compile':
        assert second == repeated and len(spawned) == 1
    else:
        assert first['task_id'] != second['task_id'] and not second['deduped']
        assert len(spawned) == 2
        for result, model in [(first, 'model-a'), (second, 'model-b')]:
            assert production.runtime.get_owned(result['task_id'], user_id=7)['model'] == model


def test_research_command_keeps_successful_shared_execution_receipt(monkeypatch):
    import threading
    from types import SimpleNamespace
    from lib.research import action
    from lib.research.workspace import empty_workspace
    from tests.paper.test_paper_full_tools import _shim
    monkeypatch.setattr(action, '_messages', lambda *args: [])
    monkeypatch.setattr(action, 'make_paper_exec_shim', lambda **kwargs: _shim())
    def loop(**kwargs):
        result = kwargs['execute_tool'](0, {'id': 'actual-command', 'function': {
            'name': 'run_command', 'arguments': '{"command":"echo research-command-receipt-ok"}'}})
        assert 'research-command-receipt-ok' in str(result)
        assert 'KeyError' not in str(result)
        kwargs['on_round_result'](0, {'content': '{}'}, 'stop', {})
        return SimpleNamespace(completed=True)
    monkeypatch.setattr(action, 'run_guarded_paper_agent_loop', loop)
    task = {'user_id': 1, 'model': 'controlled-model', 'task_id': 'actual-research-command',
            'action': 'analyze', 'direction': 'Receipt boundary',
            'abort_event': threading.Event(), 'tool_rounds': []}
    epoch = SimpleNamespace(result_envelope='v2', wire_schemas=[], contract_documents_by_name=None)
    _, receipts = action._run_agent(task, empty_workspace('Receipt boundary'), epoch, [])
    assert len(receipts) == 1 and receipts[0]['status'] == 'done'
    assert receipts[0]['execution_observation']['state'] == 'completed'
    display = task['tool_rounds'][0]['results'][0]
    assert str(display['exitCode']) == '0'


@pytest.mark.parametrize('window', [32000, 128000, 1000000])
def test_research_default_has_no_cumulative_cutoff_and_restores_frozen_policy(monkeypatch, window):
    import threading
    from lib.research import action, context
    from lib.paper.agent_usage import PaperAgentUsageMeter
    monkeypatch.setattr(context, 'action_window', lambda model: window)
    monkeypatch.setattr(action, '_messages', lambda *args: [])
    monkeypatch.setattr(action, 'make_paper_exec_shim', lambda **kwargs: None)
    class InspectedLoop(Exception):
        pass
    def inspect(*, usage_meter, **kwargs):
        assert usage_meter.token_budget == 0
        assert usage_meter.dispatch_budget == 0
        if window >= 128000:
            for _ in range(4):
                usage_meter.observe_agent_round({'prompt_tokens': 100000, 'completion_tokens': 0},
                                               {'content': 'controlled logical usage'})
            assert usage_meter.agent_tokens == 400000
            assert usage_meter.force_final_reason == ''
        snapshot = usage_meter.snapshot()
        monkeypatch.setattr(context, 'action_window', lambda model: window * 2)
        restored = PaperAgentUsageMeter.from_snapshot(snapshot, fallback_model='test')
        assert restored.token_budget == 0
        assert restored.dispatch_budget == 0
        raise InspectedLoop
    monkeypatch.setattr(action, 'run_guarded_paper_agent_loop', inspect)
    with pytest.raises(InspectedLoop):
        action._run_agent({'abort_event': threading.Event(), 'model': 'test', 'user_id': 1,
            'task_id': 'capacity-policy', 'tool_rounds': [], 'action': 'experiment'}, {}, _epoch([]), [])


def test_loop_failure_retains_usage_for_terminal_persistence(monkeypatch):
    import threading
    from lib.research import action
    monkeypatch.setattr(action, '_messages', lambda *args: [])
    monkeypatch.setattr(action, 'make_paper_exec_shim', lambda **kwargs: None)
    task = {'abort_event': threading.Event(), 'model': 'test', 'user_id': 1,
            'task_id': 'terminal-loop-account', 'tool_rounds': [], 'action': 'experiment',
            'logical_budget': {'dispatch_budget': 10}}
    def stopped(*, usage_meter, **kwargs):
        for _number in range(10):
            usage_meter.begin_dispatch()
        raise RuntimeError('controlled loop interruption')
    monkeypatch.setattr(action, 'run_guarded_paper_agent_loop', stopped)
    with pytest.raises(RuntimeError, match='loop interruption'):
        action._run_agent(task, {}, _epoch([]), [])
    snapshot = task['_researchUsageSnapshot']
    assert snapshot['agent_dispatches'] == 10
    assert snapshot['forced_final_reason'] == ''
    assert snapshot['calls'] == 0


@pytest.mark.parametrize('budget', [False, [], {'token_budget': -1}, {'dispatch_budget': True},
                                   {'dispatch_budget': 1.5}, {'unexpected': 10}])
def test_invalid_logical_budget_refuses_before_task_birth(monkeypatch, budget):
    from lib.research import action, action_runtime
    monkeypatch.setattr(action_runtime._production, 'cleanup_stale',
        lambda: pytest.fail('invalid budget must refuse before runtime effects'))
    with pytest.raises(ValueError):
        action.start_research_action('Owned action', action='analyze', expected_revision=0,
            user_id=1, confirm_external_writes=True, logical_budget=budget)


def test_explicit_logical_budget_is_frozen_in_task_identity(monkeypatch):
    from lib.production.runtime import ProductionRuntime
    from lib.research import action, action_runtime, workspace
    production = ProductionRuntime('research-budget-identity-test', id_prefix='budget')
    monkeypatch.setattr(action_runtime, '_production', production)
    monkeypatch.setattr(action_runtime, '_research_action_runtime', production.runtime)
    monkeypatch.setattr(workspace, 'load_workspace', lambda *args, **kwargs: {'revision': 3})
    monkeypatch.setattr(production.runtime, 'spawn', lambda *args: None)
    def start(budget):
        return action.start_research_action('Owned analysis', action='analyze', expected_revision=3,
            user_id=1, confirm_external_writes=True, logical_budget=budget)
    supplied = {'dispatch_budget': 30}
    first = start(supplied)
    supplied['dispatch_budget'] = 40
    repeated = start({'token_budget': 0, 'dispatch_budget': 30})
    assert repeated == {'task_id': first['task_id'], 'deduped': True}
    second = start(supplied)
    assert second['task_id'] != first['task_id']
    assert production.runtime.get_owned(first['task_id'], user_id=1)['logical_budget'] == {
        'token_budget': 0, 'dispatch_budget': 30}
