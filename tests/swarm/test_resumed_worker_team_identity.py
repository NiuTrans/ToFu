"""Later-wave and restart-restored workers keep their exact team identity.

A follow-up wave spawned by a later turn executes under that turn's task while
its mailbox binding names the original launch run; startup recovery restores
the same assignment parent. The live session's exact child invocation proves
such a worker, so copied, nested, superseded and foreign bindings stay refused.
"""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.swarm.integration import _rehydrate, _tools
from lib.swarm.run_messages import RunMessageError
from lib.swarm.team_messages import worker_binding

pytestmark = pytest.mark.unit

LAUNCH = {'id': 'launch-task', 'convId': 'conv-team', '_userId': 7}
LATER = {'id': 'later-task', 'convId': 'conv-team', '_userId': 7}


@pytest.fixture
def team(monkeypatch, tmp_path):
    """Real spawn, follow-up and startup-recovery owners; I/O ports stubbed."""
    from tests.swarm.test_retained_agents import RecordClient

    live, saved = {}, []
    for owner in (_tools, _rehydrate):
        monkeypatch.setattr(owner, '_get_session', lambda key: live.get(key))
        monkeypatch.setattr(owner, '_set_session', lambda key, value, **kw: live.__setitem__(key, value))
    monkeypatch.setattr('lib.swarm.integration._get_session', lambda key: live.get(key))
    monkeypatch.setattr(_tools, '_resolve_output_dir', lambda key: str(tmp_path))
    monkeypatch.setattr(_rehydrate, '_resolve_output_dir', lambda key: '')
    monkeypatch.setattr(_rehydrate, '_rebuild_tool_list', lambda config: [])
    monkeypatch.setattr(_tools, 'add_session_alias', lambda *a: None)
    monkeypatch.setattr('lib.swarm.master.MasterOrchestrator.run_in_background', lambda self: None)
    monkeypatch.setattr('lib.swarm.master.MasterOrchestrator.rehydrate_in_background',
                        lambda self, agents, **kw: None)
    monkeypatch.setattr('lib.swarm.persistence.save_session',
                        lambda key, **kw: saved.append(deepcopy(dict(swarm_key=key, **kw))))
    monkeypatch.setattr('lib.swarm.persistence.load_session', lambda *a, **kw: {
        'task_id': 'launch-task', 'status': 'running', 'agents': []})
    monkeypatch.setattr('lib.swarm.restart_recovery.read_recovery_turns',
                        lambda *a, **kw: {'turns': [{'status': 'completed'}]})
    monkeypatch.setattr('lib.agent_inbox.untombstone', lambda key: None)
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **kw: None)
    monkeypatch.setattr('lib.tasks_pkg.invocation_registration.register_child_invocation',
                        lambda *a, **kw: None)
    client = RecordClient()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda write=False: client)
    monkeypatch.setattr('lib.conversations.repository.ConversationRepository.get',
        lambda _self, conv_id, *, user_id, include_messages: SimpleNamespace(
            metadata={'settings': {}}) if (conv_id, user_id) == ('conv-team', 7) else None)

    def spawn(task, agent_id):
        return json.loads(_tools.execute_swarm_tool('spawn_agents', {
            'agents': [{'id': agent_id, 'objective': 'work ' + agent_id}]}, task=task,
            model='fixture-model'))

    assert spawn(LAUNCH, 'first')['swarm_run_id'] == 'launch-task'
    launched = live['conv-team']
    launched._scheduler = Mock()
    launched._scheduler.add_specs.side_effect = lambda specs: specs
    # A later turn adds work to the run that the completed launch turn left running.
    later = spawn(LATER, 'later')
    assert later['is_followup'] and later['swarm_run_id'] == 'launch-task'
    launched._scheduler = None  # Admission is stubbed; no driver ever started.

    def session(recovered):
        if recovered:
            live.clear()  # Process restart: only the durable row survives.
            assert _rehydrate._rehydrate_one(saved[-1])
            assert live['conv-team'] is not launched
        return live['conv-team']

    def run_worker(current, agent_id, during):
        """Run the real SubAgent; ``during`` executes inside its live invocation."""
        def invocation(child):
            during(child)
            child.update(content='done', finishReason='stop', status='done')
        monkeypatch.setattr('lib.tasks_pkg.child_invocation.run_child_invocation', invocation)
        agent = current._make_agent(next(spec for spec in current.specs if spec.id == agent_id))
        agent._checkpoint = lambda **kw: True
        return agent, agent.run()

    return SimpleNamespace(session=session, run_worker=run_worker, client=client, live=live)


def send(child, agent_id, key):
    return json.loads(_tools.execute_swarm_tool('send_agent_message', {
        'agent_id': agent_id, 'swarm_run_id': 'launch-task', 'idempotency_key': key,
        'content': 'Another session already owns this fix; stop duplicating it.'}, task=child))


@pytest.mark.parametrize('recovered', [False, True], ids=['live', 'startup_recovery'])
def test_later_wave_worker_messages_its_team_through_real_tool_entry(team, recovered):
    session = team.session(recovered)
    replies, identities, senders = [], [], []

    def during(child):
        identities.append((child['_parentTaskId'], worker_binding(child)['swarmRunId']))
        replies.append(send(child, 'first', 'warn-teammate'))
        replies.append(send(child, 'launch-task', 'warn-root'))
        # The run snapshot attributes the sent message to this assignment.
        snapshot = session._build_agent_snapshot()
        sent = [row['senderTaskId'] for row in snapshot['messageReceipts']
                if row['agentId'] == 'first']
        senders.append((sent, [row.get('taskId') for row in snapshot['messageParticipants']
                               if row['id'] == 'later'], child['id']))

    _, result = team.run_worker(session, 'later', during)
    assert result.status == 'completed', result.error_message
    # The worker executes for the later caller while its mailbox names the run.
    assert identities == [('later-task', 'launch-task')]
    teammate, root = replies
    assert teammate['state'] == 'submitted' and teammate['execution_started'] is False
    [(sent, participant, child_id)] = senders
    assert sent == participant == [child_id]
    # The launch root ended before the later turn began; messages cannot wake it.
    assert root['code'] == 'message_target_not_active', root
    assert 'final result' in root['next_action']


@pytest.mark.parametrize('mutation', [
    'copied', 'nested', 'superseded', 'foreign_run', 'foreign_key', 'forged_owner', 'no_session'])
def test_only_the_runs_exact_live_invocation_keeps_identity(team, mutation):
    session = team.session(True)
    outcomes = []

    def during(child):
        worker = child
        # Live tasks hold runtime handles; equal fields still name another object.
        if mutation == 'copied':
            worker = dict(child)
        elif mutation == 'nested':
            worker = {**child, 'id': 'nested-child', '_parentTaskId': child['id']}
        elif mutation == 'superseded':
            session._agents['later']._child = dict(child)
        elif mutation == 'foreign_run':
            child['_swarmMessageBinding']['swarmRunId'] = 'other-run'
        elif mutation == 'foreign_key':
            child['_swarmMessageBinding']['swarmKey'] = 'other-conv'
        elif mutation == 'forged_owner':
            child['_swarmMessageBinding']['owner'] = 8
        elif mutation == 'no_session':
            team.live.clear()  # The invocation outlived its registered session.
        with pytest.raises(RunMessageError, match='original owned team identity'):
            worker_binding(worker)
        outcomes.append(json.loads(_tools.execute_swarm_tool('send_agent_message', {
            'agent_id': 'first', 'swarm_run_id': 'launch-task', 'idempotency_key': 'forged',
            'content': 'impersonated'}, task=worker))['code'])

    team.run_worker(session, 'later', during)
    assert outcomes == ['message_forbidden']
    assert not team.client.rows
