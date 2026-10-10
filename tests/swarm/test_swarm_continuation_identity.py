"""Continuation commands distinguish sessions and retain identity across restart."""
from copy import deepcopy
from unittest.mock import Mock
import json

import pytest

from lib.swarm.integration import _tools, _rehydrate

pytestmark = pytest.mark.unit


def test_sequential_waves_same_parent_have_distinct_restart_safe_sources(monkeypatch, tmp_path):
    sessions = []
    saved = []
    wake = Mock()
    monkeypatch.setattr('lib.swarm.integration._maybe_autocontinue', wake)
    monkeypatch.setattr(_tools, '_get_session', lambda key: None)
    monkeypatch.setattr(_tools, '_set_session', lambda key, session, **kw: sessions.append(session))
    monkeypatch.setattr(_tools, '_resolve_output_dir', lambda key: str(tmp_path))
    monkeypatch.setattr('lib.swarm.master.MasterOrchestrator.run_in_background', lambda self: None)
    monkeypatch.setattr('lib.swarm.persistence.save_session',
                        lambda key, **kw: saved.append(deepcopy(dict(swarm_key=key, **kw))))
    monkeypatch.setattr('lib.agent_inbox.untombstone', lambda key: None)
    task = {'id': 'identity-parent', 'convId': 'identity-conv', '_userId': 7}
    for _ in range(2):
        result = json.loads(_tools.execute_swarm_tool(
            'spawn_agents', {'agents': [{'id': 'same-child', 'objective': 'work'}]},
            task=task))
        assert result['status'] == 'async_launched'
        sessions[-1].on_settled()
    sources = [call.kwargs['source_id'] for call in wake.call_args_list]
    assert sources[0] != sources[1]
    assert all(call.args == ('identity-conv', 7) for call in wake.call_args_list)


    # Adding work to a live session is still one settlement, not a new source.
    live = sessions[-1]
    live._scheduler = Mock()
    live._scheduler.add_specs.side_effect = lambda specs: specs
    monkeypatch.setattr(_tools, '_get_session', lambda key: live)
    monkeypatch.setattr(_tools, 'add_session_alias', lambda *a: None)
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **kw: None)
    followup = json.loads(_tools.execute_swarm_tool(
        'spawn_agents', {'agents': [{'id': 'followup', 'objective': 'more work'}]},
        task=task))
    assert followup['is_followup']
    live.on_settled()
    assert wake.call_args.kwargs['source_id'] == sources[-1]
    sources.append(sources[-1])
    monkeypatch.setattr(_rehydrate, '_get_session', lambda key: None)
    monkeypatch.setattr(_rehydrate, '_set_session', lambda key, session, **kw: sessions.append(session))
    monkeypatch.setattr(_rehydrate, '_resolve_output_dir', lambda key: str(tmp_path))
    monkeypatch.setattr(_rehydrate, '_rebuild_tool_list', lambda config: [])
    monkeypatch.setattr('lib.swarm.master.MasterOrchestrator.rehydrate_in_background', lambda self, agents: None)
    monkeypatch.setattr('lib.swarm.restart_recovery.read_recovery_turns', lambda *a, **kw: {
        'turns': [{'status': 'completed'}]})
    for row, expected in zip(saved, sources):
        assert _rehydrate._rehydrate_one(row)
        sessions[-1].on_settled()
        sessions[-1].on_settled()
        assert wake.call_args.kwargs['source_id'] == expected


def test_legacy_rehydrate_keeps_original_task_identity(monkeypatch, tmp_path):
    factory = Mock()
    monkeypatch.setattr(_rehydrate, 'MasterOrchestrator', factory)
    monkeypatch.setattr(_rehydrate, '_get_session', lambda key: None)
    monkeypatch.setattr(_rehydrate, '_set_session', lambda *a, **kw: None)
    monkeypatch.setattr(_rehydrate, '_resolve_output_dir', lambda key: str(tmp_path))
    monkeypatch.setattr(_rehydrate, '_rebuild_tool_list', lambda config: [])
    monkeypatch.setattr('lib.agent_inbox.untombstone', lambda key: None)
    wake = Mock()
    monkeypatch.setattr('lib.swarm.integration._maybe_autocontinue', wake)
    assert _rehydrate._rehydrate_one({
        'swarm_key': 'identity-legacy', 'task_id': 'legacy-task',
        'config': {'user_id': 7},
        'specs': [{'id': 'child', 'role': 'coder', 'objective': 'work'}],
    })
    factory.call_args.kwargs['on_settled']()
    wake.assert_called_once_with('identity-legacy', 7, source_id='legacy-task')
