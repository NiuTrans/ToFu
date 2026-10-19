"""Stable routing survives a new repository over a disposable real Sidecar."""
import json
from types import SimpleNamespace

import pytest

from lib.swarm import retained_messages
from lib.swarm.retained import complete_assignment
from lib.swarm.retained_repository import RetainedAgentRepository, scope_for

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit


def test_real_identity_claim_and_terminal_dedup_survive_repository_reconstruction(chat_sidecar, monkeypatch):
    project = '/workspace/stable-storage-spec'
    scope = scope_for(project_path=project)
    repository = RetainedAgentRepository(1)
    agent = repository.create(scope, role='coder', name='Colleague',
                              messages=[{'role': 'assistant', 'content': 'x' * (9 * 1024 * 1024)}])
    before = len(json.dumps(repository.get(scope, agent['id'])[0], ensure_ascii=False).encode())
    task = {'id': 'owned-storage-turn', 'convId': 'owned-storage-conv', '_userId': 1, 'config': {}}
    monkeypatch.setattr('lib.conversations.repository.ConversationRepository.get',
        lambda *a, **kw: SimpleNamespace(metadata={'settings': {'projectPath': project}}))
    monkeypatch.setattr('lib.swarm.integration.get_active_session', lambda key: None)
    monkeypatch.setattr('lib.swarm.persistence.load_session', lambda *a, **kw: None)
    launches = []
    def launch(name, args, parent, **kwargs):
        launches.append(args)
        run_id = args['agents'][0]['id']
        assert RetainedAgentRepository(1).get(scope, agent['id'])[0]['run_id'] == run_id
        return json.dumps({'status': 'async_launched', 'swarm_run_id': parent['id'],
                           'agents': [{'id': run_id}], 'swarm_key': parent['convId']})
    monkeypatch.setattr('lib.swarm.integration.execute_swarm_tool', launch)
    request = {'retained_agent_id': agent['id'], 'idempotency_key': 'stable-durable-key',
               'content': 'Follow up on prior evidence'}
    first = retained_messages.send(task, request, cfg={}, all_tools=[], project_path=project)
    assert first['state'] == 'turn_started'
    assert complete_assignment(user_id=1, tenant_id='', scope=scope, agent_id=agent['id'],
        run_id=first['assignment']['agent_id'], messages=[{'role': 'assistant', 'content': 'x' * (9 * 1024 * 1024)},
                  {'role': 'assistant', 'content': 'New evidence'}],
        result={'status': 'completed', 'final_answer': 'New evidence'})
    later = {**task, 'id': 'next-storage-turn'}
    assert retained_messages.send(later, request, cfg={}, all_tools=[], project_path=project) == first
    inspected = retained_messages.read(later, {
        'retained_agent_id': agent['id'], 'idempotency_key': request['idempotency_key']}, project_path=project)
    assert inspected['content'] == request['content'] and inspected['assignment'] == first['assignment']
    assert len(launches) == 1
    restored, _ = RetainedAgentRepository(1).get(scope, agent['id'])
    assert restored['state'] == 'idle'
    assert len(restored['message_dispatches']) == 1
    after = len(json.dumps(restored, ensure_ascii=False).encode())
    assert after > 8 * 1024 * 1024
    assert restored['messages'][0]['content'] == 'x' * (9 * 1024 * 1024)
    assert restored['messages'][-1]['content'] == 'New evidence'
    print(f'stable-message record bytes: before={before}, after={after}, increase={after-before}')


def test_real_roster_pages_every_identity_beyond_1000(chat_sidecar):
    scope = scope_for(conversation_id='real-roster', requested='conversation')
    repository = RetainedAgentRepository(1)
    identities = {repository.create(scope, role='coder', name=f'Agent {index}')['id']
                  for index in range(1101)}
    restored = RetainedAgentRepository(1).list(scope)
    assert len(restored) == 1101
    assert {value['id'] for value in restored} == identities
    assert RetainedAgentRepository(2).list(scope) == []
