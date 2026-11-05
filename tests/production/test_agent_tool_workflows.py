"""Fixed tool workflows use production checkpoints, gates and cancellation.

Only node bodies are synthetic. Graph admission, output validation and the
stage executor are real, including recovery from an interrupted invocation.
"""
import json

import pytest

from lib.production.stages import Stage, StageAborted, StageFailed
from lib.production.workflows import AgentToolWorkflow, WorkflowNode, run_workflow

pytestmark = pytest.mark.unit

OUTPUT = {'type': 'object', 'required': ['value'],
          'properties': {'value': {'type': 'integer'}}}


def node(name, run, *, execution='model', **kwargs):
    return WorkflowNode(Stage(name, run, **kwargs), execution, OUTPUT)


def test_declared_order_and_verified_resume(tmp_path):
    calls = []
    def generate(ctx):
        calls.append('generate')
        return {'value': 4}
    def publish(ctx):
        calls.append('publish')
        return {'value': ctx['artifacts']['generate']['value'] + 1}
    flow = AgentToolWorkflow('example.tool', '1', (
        node('generate', generate), node('publish', publish, execution='deterministic')))
    path = str(tmp_path / 'state.json')
    events = []
    assert run_workflow(flow, {}, state_path=path, emit=events.append)['publish'] == {'value': 5}
    assert run_workflow(flow, {}, state_path=path)['publish'] == {'value': 5}
    assert calls == ['generate', 'publish']
    assert [e['stage'] for e in events if e['type'] == 'stage_done'] == calls
    assert events[0]['workflow']['nodes'][0]['execution'] == 'model'


def test_invalid_model_output_never_reaches_publication(tmp_path):
    called = []
    flow = AgentToolWorkflow('example.tool', '1', (
        node('generate', lambda ctx: {'value': 'invented'}),
        node('publish', lambda ctx: called.append(True), execution='deterministic')))
    with pytest.raises(StageFailed, match='generate') as error:
        run_workflow(flow, {}, state_path=str(tmp_path / 'state.json'))
    assert 'output contract' in str(error.value.errors)
    assert called == []
    assert not (tmp_path / 'state.json').exists()


@pytest.mark.parametrize('mutation', ['duplicate', 'unknown_execution', 'remote_schema', 'invalid_retry'])
def test_invalid_definition_refused_before_any_node_runs(tmp_path, mutation):
    called = []
    first = node('draft', lambda ctx: called.append(True))
    nodes = [first]
    if mutation == 'duplicate':
        nodes.append(first)
    elif mutation == 'unknown_execution':
        nodes = [node('draft', first.stage.run, execution='spawn_anything')]
    elif mutation == 'remote_schema':
        nodes = [WorkflowNode(first.stage, 'model', {'$ref': 'https://example.com/schema'})]
    else:
        nodes = [node('draft', first.stage.run, retry=-1)]
    with pytest.raises(ValueError):
        run_workflow(AgentToolWorkflow('example.tool', '1', tuple(nodes)), {},
                     state_path=str(tmp_path / 'state.json'))
    assert called == []


def test_revision_change_invalidates_dependent_checkpoints(tmp_path):
    calls = []
    def generate(ctx):
        calls.append('generate')
        return {'value': 1}
    def finish(ctx):
        calls.append('finish')
        return {'value': 2}
    path = str(tmp_path / 'state.json')
    for version in ('1', '2'):
        run_workflow(AgentToolWorkflow('example.tool', version, (
            node('generate', generate), node('finish', finish))), {}, state_path=path)
    assert calls == ['generate', 'finish', 'generate', 'finish']


def test_schema_change_revalidates_checkpoint_before_downstream_work(tmp_path):
    calls = []
    stage = Stage('draft', lambda ctx: calls.append(True) or {'value': 3})
    path = str(tmp_path / 'state.json')
    run_workflow(AgentToolWorkflow('example.tool', '1', (
        WorkflowNode(stage, 'model', OUTPUT),)), {}, state_path=path)
    state = json.loads((tmp_path / 'state.json').read_text())
    state['stages']['draft']['artifact']['value'] = 'corrupt'
    (tmp_path / 'state.json').write_text(json.dumps(state))
    assert run_workflow(AgentToolWorkflow('example.tool', '1', (
        WorkflowNode(stage, 'model', OUTPUT),)), {}, state_path=path)['draft']['value'] == 3
    assert len(calls) == 2


def test_abort_after_model_output_keeps_delivery_unfinished(tmp_path):
    cancelled = []
    def produce(ctx):
        cancelled.append(True)
        return {'value': 1}
    flow = AgentToolWorkflow('example.tool', '1', (node('draft', produce),))
    with pytest.raises(StageAborted):
        run_workflow(flow, {}, state_path=str(tmp_path / 'state.json'),
                     abort_check=lambda: bool(cancelled))
    assert not (tmp_path / 'state.json').exists()


def test_descriptions_do_not_grant_execution_or_depend_on_callback_identity():
    first = AgentToolWorkflow('example.tool', '1', (node('draft', lambda ctx: {}),))
    second = AgentToolWorkflow('example.tool', '1', (node('draft', lambda ctx: {}),))
    assert first.describe() == second.describe()
    assert first.digest == second.digest
    assert 'run' not in first.describe()['nodes'][0]


def test_existing_validated_checkpoints_adopt_contract_without_replaying(tmp_path):
    from lib.production.stages import run_stages
    calls = []
    stage = Stage('draft', lambda ctx: calls.append(True) or {'value': 1},
                  checkpoint_version='original-v1')
    path = str(tmp_path / 'state.json')
    run_stages([stage], {}, state_path=path)
    before = json.loads((tmp_path / 'state.json').read_text())
    flow = AgentToolWorkflow('example.tool', '1', (WorkflowNode(stage, 'model', OUTPUT),))
    run_workflow(flow, {}, state_path=path)
    after = json.loads((tmp_path / 'state.json').read_text())
    assert calls == [True]
    assert after['stages']['draft']['at'] == before['stages']['draft']['at']
    assert after['stages']['draft']['checkpoint_contract']


def test_independent_nodes_start_together_and_join(tmp_path):
    from threading import Barrier
    barrier = Barrier(3, timeout=5)
    def work(ctx):
        barrier.wait()
        return {'value': 1}
    flow = AgentToolWorkflow('example.tool', '1', tuple(
        node(str(i), work) for i in range(3)), mode='independent')
    assert len(run_workflow(flow, {}, state_path=str(tmp_path / 'state.json'))) == 3


def test_changed_node_preserves_unaffected_prefix(tmp_path):
    calls = []
    def make(name, version):
        return node(name, lambda ctx: calls.append(name) or {'value': 1},
                    checkpoint_version=version)
    path = str(tmp_path / 'state.json')
    for revision in ('1', '2'):
        run_workflow(AgentToolWorkflow('example.tool', '1', (
            make('research', '1'), make('write', revision), make('deliver', '1'))),
            {}, state_path=path)
    assert calls == ['research', 'write', 'deliver', 'write', 'deliver']


def test_contract_adoption_failure_preserves_checkpoint_and_never_replays(tmp_path, monkeypatch):
    from lib import json_store
    from lib.production.stages import run_stages
    calls = []
    stage = Stage('draft', lambda ctx: calls.append(True) or {'value': 1})
    path = tmp_path / 'state.json'
    run_stages([stage], {}, state_path=str(path))
    original = path.read_bytes()
    def unavailable(*args, **kwargs):
        raise OSError('write unavailable')
    monkeypatch.setattr(json_store, 'write_json_atomic', unavailable)
    events = []
    with pytest.raises(StageFailed, match='adoption failed'):
        run_workflow(AgentToolWorkflow('example.tool', '1', (
            WorkflowNode(stage, 'model', OUTPUT),)), {},
            state_path=str(path), emit=events.append)
    assert calls == [True]
    assert path.read_bytes() == original
    assert events[-1]['type'] == 'stage_failed'


def test_cancellation_during_adoption_does_not_report_completion(tmp_path, monkeypatch):
    from lib import json_store
    from lib.production.stages import run_stages
    stage = Stage('draft', lambda ctx: {'value': 1})
    path = str(tmp_path / 'state.json')
    run_stages([stage], {}, state_path=path)
    cancelled = []
    write = json_store.write_json_atomic
    def cancel_after_write(*args, **kwargs):
        write(*args, **kwargs)
        cancelled.append(True)
    monkeypatch.setattr(json_store, 'write_json_atomic', cancel_after_write)
    with pytest.raises(StageAborted):
        run_workflow(AgentToolWorkflow('example.tool', '1', (
            WorkflowNode(stage, 'model', OUTPUT),)), {}, state_path=path,
            abort_check=lambda: bool(cancelled))
