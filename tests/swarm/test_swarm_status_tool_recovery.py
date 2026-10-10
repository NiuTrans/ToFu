"""Panel recovery reads bounded worker evidence; the chip remains summary-only.

No worker/model is started. A guarded synthetic log rejects full iteration and
unlocked tail reads, so a large logical history cannot hide an O(history) scan.
"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubAgentResult, SubTaskSpec

pytestmark = pytest.mark.unit


class _LockProbe:
    held = False
    entered = 0

    def __enter__(self):
        assert not self.held
        self.held = True
        self.entered += 1
        return self

    def __exit__(self, *args):
        self.held = False


class _TailOnlyLog(list):
    def __init__(self, entries, lock):
        super().__init__(entries)
        self.lock = lock
        self.reads = 0

    def __len__(self):
        self.reads += 1
        return 100_000

    def __iter__(self):
        pytest.fail('status recovery must never scan the whole worker log')

    def __getitem__(self, key):
        self.reads += 1
        assert key == slice(-30, None)
        assert self.lock.held, 'copy live log entries under the worker lock'
        return super().__getitem__(key)


def _master():
    spec = SubTaskSpec(id='rust_port', role='coder', objective='Inspect safely')
    master = MasterOrchestrator('recovery-run', 'recovery-conv', [spec], user_id=1)
    lock = _LockProbe()
    entries = [{
        'tool': 'read_files', 'tool_call_id': f'occurrence-{n}',
        'provider_tool_call_id': 'recycled', 'status': 'done',
        'preview': '🧪' * 5000, 'args_brief': 'artifact.rs',
    } for n in range(99_970, 100_000)]
    log = _TailOnlyLog(entries, lock)
    result = SubAgentResult(status='running', rounds_used=24, tool_log=log,
                            final_answer='not part of status recovery')
    master._agents[spec.id] = SimpleNamespace(
        model='selected-model', result=result, _tool_log_lock=lock)
    master._scheduler = SimpleNamespace(
        _running={spec.id: spec}, pending_count=0, running_count=1,
        started_at_map=lambda: {spec.id: 1_789_025_737.999})
    return master, spec, log, lock, result


def test_recovery_copies_only_locked_tail_and_preserves_worker_evidence():
    master, _, log, lock, _ = _master()
    original = deepcopy(list(list.__iter__(log)))
    summary = master.get_status()['rust_port']
    assert summary['status'] == 'running'
    assert summary['model'] == 'selected-model'
    assert summary['round'] == 24
    assert summary['startedAt'] == 1_789_025_737_999
    assert 'toolCalls' not in summary and log.reads == 0

    detailed = master.get_status(include_tool_timeline=True)['rust_port']
    calls = detailed['toolCalls']
    assert lock.entered == 1 and not lock.held
    assert log.reads <= 3
    assert len(calls) <= 30
    assert len(json.dumps(calls, ensure_ascii=True, separators=(',', ':'))) <= 32_768
    assert detailed['toolCallsOmitted'] + len(calls) == 100_000
    assert calls[-1]['callId'] == 'occurrence-99999'
    assert calls[-1]['preview'] == '🧪' * 2000
    assert calls[-1]['previewFullChars'] == 5000
    assert calls[-1]['previewTruncated'] is True
    assert 'final_answer' not in detailed and 'preview' not in detailed
    assert list(list.__iter__(log)) == original, 'projection cannot truncate worker data'


def test_completed_recovery_uses_result_after_worker_is_released():
    master, spec, _, _, _ = _master()
    result = SubAgentResult(status='completed', rounds_used=25, tool_log=[{
        'tool': 'run_command', 'tool_call_id': 'final-tool',
        'status': 'done', 'preview': 'exit code: 0',
    }])
    master._results_by_id[spec.id] = (spec, result)
    master._agents.clear()
    master._resolve_spec_model = lambda _: 'recorded-model'
    detailed = master.get_status(include_tool_timeline=True)[spec.id]
    assert detailed['status'] == 'completed'
    assert detailed['toolCalls'][0]['preview'] == 'exit code: 0'
    assert detailed['toolCalls'][0]['callId'] == 'final-tool'


def test_status_and_activity_apply_owner_fence_before_reading_tool_evidence(monkeypatch):
    from lib.swarm.integration import _state

    master, _, log, lock, _ = _master()
    monkeypatch.setattr(_state, '_get_session', lambda _: master)
    monkeypatch.setattr(_state, '_resolve_key', lambda key: key)
    monkeypatch.setattr(_state, '_active_sessions', {'recovery-conv': master})
    monkeypatch.setattr(_state, '_session_timestamps', {'recovery-conv': 1_789_025_737})

    assert _state.get_swarm_status('recovery-conv', user_id=2) is None
    assert _state.list_active_swarms(user_id=2) == []
    assert log.reads == 0
    activities = _state.list_active_swarms(user_id=1)
    assert activities[0]['agents'][0]['id'] == 'rust_port'
    assert activities[0]['agents'][0]['status'] == 'running'
    assert 'toolCalls' not in activities[0]['agents'][0]
    assert log.reads == 0, 'frequent chip polls do not touch the log'
    status = _state.get_swarm_status('recovery-conv', user_id=1)
    assert status['active'] is True and status['known'] is True
    assert status['agents'][0]['id'] == 'rust_port'
    assert status['agents'][0]['toolCalls'][-1]['callId'] == 'occurrence-99999'
    assert lock.entered == 1
