"""Swarm cards project worker selection, not a fresh display-time route."""
from types import SimpleNamespace

import pytest

from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubTaskSpec


pytestmark = pytest.mark.unit

@pytest.mark.parametrize('selected', ['selected-worker-model', ''])
def test_display_model_uses_constructed_worker_without_rerouting(monkeypatch, selected):
    master = MasterOrchestrator('run', '', [], user_id=1, model='parent-model')
    spec = SubTaskSpec(id='worker', role='coder', objective='inspect')
    master._agents[spec.id] = SimpleNamespace(model=selected)

    def unexpected_route(*args, **kwargs):
        pytest.fail('presentation must not select another model for a live worker')

    monkeypatch.setattr('lib.swarm.master.resolve_model_for_tier', unexpected_route)
    assert master._resolve_spec_model(spec) == selected


def test_running_snapshot_retains_model_clock_and_tool_evidence(monkeypatch):
    from lib.swarm.protocol import SubAgentResult
    from lib.swarm.snapshot import filter_snapshot

    spec = SubTaskSpec(id='worker', role='coder', objective='inspect')
    master = MasterOrchestrator('run', '', [spec], user_id=1, model='parent-model')
    master._agents[spec.id] = SimpleNamespace(
        model='selected-worker-model', result=SubAgentResult(tool_log=[{
            'tool': 'read_files', 'args_brief': 'README.md',
            'timestamp': 1000, 'status': 'done', 'preview': 'file contents',
        }]))
    master._scheduler = SimpleNamespace(
        _running={spec.id: spec}, _pending=[],
        started_at_map=lambda: {spec.id: 1000},
    )
    monkeypatch.setattr('lib.swarm.master.resolve_model_for_tier', lambda *a, **k: '')
    snapshot = filter_snapshot(master._build_agent_snapshot(), {spec.id})
    card = snapshot['agents'][0]
    assert card['model'] == 'selected-worker-model'
    assert card['startedAt'] == 1000000
    assert card['tools'] == ['read_files']
    assert card['toolCalls'][0]['preview'] == 'file contents'
    status = master.get_status()['worker']
    assert status['model'] == 'selected-worker-model'
    assert status['startedAt'] == 1000000
    assert 'toolCalls' not in status, 'background activity stays summary-only'
    detailed = master.get_status(include_tool_timeline=True)['worker']
    assert detailed['toolCalls'][0]['preview'] == 'file contents'
    assert detailed['toolCalls'][0]['status'] == 'done'


def test_start_event_observes_factory_selected_model():
    from lib.swarm.protocol import SubAgentResult, SubAgentStatus
    from lib.swarm.scheduler import StreamingScheduler

    evidence = {}
    observed = []
    spec = SubTaskSpec(id='worker', role='coder', objective='inspect', max_retries=0)

    def factory(spec):
        evidence[spec.id] = 'selected-worker-model'
        return SimpleNamespace(run=lambda: SubAgentResult(
            status=SubAgentStatus.COMPLETED.value))

    scheduler = StreamingScheduler(
        agent_factory=factory, max_parallel=1,
        on_agent_start=lambda spec: observed.append(evidence.get(spec.id)),
    )
    try:
        scheduler.add_specs([spec])
        scheduler.run_until_idle(timeout=5)
        assert observed == ['selected-worker-model']
    finally:
        scheduler.shutdown()


@pytest.mark.parametrize('status', ['running', 'done', 'failed'])
def test_snapshot_preserves_tool_occurrence_identity_and_verdict(status):
    from lib.swarm.master import _snapshot_tool_timeline

    _, calls, _ = _snapshot_tool_timeline([{
        'tool': 'read_files', 'tool_call_id': 'occurrence-1',
        'provider_tool_call_id': 'recycled', 'status': status,
        'preview': 'result' if status == 'done' else '',
    }])
    assert calls[0]['callId'] == 'occurrence-1'
    assert calls[0]['status'] == status



