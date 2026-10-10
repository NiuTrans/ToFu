"""Swarm task sinks keep execution identity; push mirrors route by conversation."""
import json

import pytest

from lib.agent_core.push import PushHub
from lib.agent_core.push_bus import InProcPushBus
from lib.swarm.integration import _tools

pytestmark = pytest.mark.unit


def test_swarm_mirror_repairs_sink_stamped_identity_without_rewriting_task_event(
        monkeypatch, tmp_path):
    sessions = []
    monkeypatch.setattr(_tools, '_cleanup_stale_sessions', lambda: None)
    monkeypatch.setattr(_tools, '_get_session', lambda key: None)
    monkeypatch.setattr(_tools, '_set_session',
                        lambda key, session, **kw: sessions.append(session))
    monkeypatch.setattr(_tools, '_resolve_output_dir', lambda key: str(tmp_path))
    monkeypatch.setattr('lib.swarm.master.MasterOrchestrator.run_in_background',
                        lambda self: None)
    monkeypatch.setattr('lib.swarm.persistence.save_session', lambda *a, **kw: None)
    monkeypatch.setattr('lib.agent_inbox.untombstone', lambda key: None)

    class Client:
        def __init__(self, user_id):
            self.user_id = str(user_id)
            self.frames = []

        def enqueue(self, frame):
            self.frames.append(frame)

    hub = PushHub()
    hub._bus = InProcPushBus(hub._deliver_frame)
    monkeypatch.setattr('lib.agent_core.push.push_event', hub.push_event)
    exact, wildcard, other_owner = Client(7), Client(7), Client(8)
    hub.subscribe(exact, 'swarm', 'identity-conversation')
    hub.subscribe(wildcard, 'swarm', '*')
    hub.subscribe(other_owner, 'swarm', '*')
    task_events = []

    def parent_sink(event):
        # Reproduce the real task manager's correlation-envelope stamping.
        event['taskId'] = 'identity-execution'
        event['conversationId'] = 'identity-conversation'
        task_events.append(event)

    handle = json.loads(_tools.execute_swarm_tool(
        'spawn_agents', {'agents': [{'id': 'worker', 'objective': 'probe identity'}]},
        task={'id': 'identity-execution', 'convId': 'identity-conversation',
              '_userId': 7},
        on_event=parent_sink,
    ))
    assert handle['status'] == 'async_launched'
    event = {'type': 'swarm_agent_tool_call', 'agentId': 'worker',
             'callId': 'call-1', 'tool': 'run_command', 'status': 'running'}
    sessions[0].on_progress(event)

    expected_task_event = {
        'type': 'swarm_agent_tool_call', 'agentId': 'worker',
        'callId': 'call-1', 'tool': 'run_command', 'status': 'running',
        'taskId': 'identity-execution', 'conversationId': 'identity-conversation',
    }
    assert task_events == [expected_task_event]
    assert task_events[0] is event
    assert exact.frames == wildcard.frames == [{
        **expected_task_event, 'channel': 'swarm',
        'taskId': 'identity-conversation', 'executionTaskId': 'identity-execution',
        'swarmId': handle['swarm_run_id'],
    }]
    assert other_owner.frames == []
    assert event == expected_task_event
