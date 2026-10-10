"""Concurrent canonical Swarm invocations publish attributable edit presence."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import pytest

from tests.goals import test_goal_invocation_integration as goal_fixture

runtime = goal_fixture.runtime
from tests.swarm.test_swarm_invocation import reply, tool_call, agent_for
from lib.swarm.protocol import SubTaskSpec

pytestmark = pytest.mark.unit
SHARED_FILE = 'shared.py'


@pytest.mark.parametrize('announce', [True, False])
def test_concurrent_real_workers_publish_conflict_only_when_announced(runtime, monkeypatch, tmp_path, announce):
    from lib.agent_core.push import hub
    import lib.presence.registry as registry
    import lib.tasks_pkg.manager._stream as stream
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round
    from lib.swarm.agent import SubAgent
    monkeypatch.setattr(registry, '_state', {})
    monkeypatch.setattr(registry, '_sweeper_started', True)
    if not announce:
        monkeypatch.setattr(SubAgent, '_presence_announce', lambda *a, **kw: None)
    runtime.parent['convId'] = 'presence-fixture'
    tool = {'type': 'function', 'function': {'name': 'write_file', 'parameters': {
        'type': 'object', 'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
        'required': ['path', 'content']}}}
    def execute(task, tc, name, call_id, args, rn, row, *rest):
        _finalize_tool_round(task, rn, row, [{'path': args['path'], 'status': 'written'}])
        return call_id, 'Written synthetic evidence', False
    runtime.parent['_tool_env'] = SimpleNamespace(resolve=lambda name: execute)
    seen = set()
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    def provider(body, **kwargs):
        with lock:
            first = body['_task_id'] not in seen
            seen.add(body['_task_id'])
        if first:
            return reply(calls=[tool_call(name='write_file', arguments='{"path":"shared.py","content":"x"}')])
        barrier.wait(4)
        return reply('Both workers recorded their evidence. ' * 10)
    monkeypatch.setattr(stream, 'dispatch_stream', provider)
    events = []
    def listener(channel, task_id, payload):
        if channel == 'presence':
            events.append(payload)
    hub.add_listener(listener)
    try:
        agents = [agent_for(runtime, spec=SubTaskSpec(id=f'worker-{i}', role='general', objective='Write evidence'),
                            tools=[tool], project_path=str(tmp_path)) for i in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda agent: agent.run(), agents))
    finally:
        hub.remove_listener(listener)
    assert all(result.status == 'completed' for result in results), [r.error_message for r in results]
    conflicts = [event for event in events if event.get('kind') == 'conflict']
    assert bool(conflicts) == announce
    if announce:
        assert set(conflicts[-1]['conflict']['peers']) == {
            f'presence-fixture#agent-general-worker-{i}' for i in range(2)}
        assert SHARED_FILE in conflicts[-1]['conflict']['message']
