"""Disposable native project dispatch: real durable authority, tools and Git delivery.

Retains the artifact-local consult acceptance and exercises file publication with
one fake physical model send. Only provider/transport, notifications and the
repository's normal verification gate are substituted; grant, turns, workspace
metadata, candidate integration and delivery receipts remain native operations.
Run with TOFUDB_BIN pointing to an existing source-matched private artifact.
"""
from pathlib import Path
import subprocess
import threading
import time
import uuid

import pytest

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = [pytest.mark.api,
              pytest.mark.parametrize('semantic_authority', ['native'], indirect=True)]


@pytest.fixture
def native_dispatch_authority(semantic_authority, monkeypatch):
    factory, owner, engine = semantic_authority
    assert engine is not None, 'This specification must never run on SQLite/Postgres'
    assert engine._build_identity['source_sha256'] == engine._checkout_source_sha256
    from lib import storage, integration_state_repository as state
    # state imports the client at module load; redirect that port as well. The
    # factory follows engine._client after crash_cycle, not the dead connection.
    monkeypatch.setattr(storage, 'get_storage_client', factory)
    monkeypatch.setattr(state, 'get_storage_client', factory)

    def forbidden_provider(*args, **kwargs):
        pytest.fail('Native dispatch acceptance must not call a paid provider')

    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', forbidden_provider)
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', forbidden_provider)
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.tasks_pkg.manager import discard_task
    before = set(chat_task_runtime.task_ids())
    try:
        yield factory, owner, engine
    finally:
        for task_id in set(chat_task_runtime.task_ids()) - before:
            if task_id.startswith(('native-file-', 'fake-task-')):
                discard_task(task_id)


def test_project_dispatch_native_birth_replay_budget_and_review(native_dispatch_authority, monkeypatch):
    """Retained consult acceptance, formerly ignored under .native/fixture-engine."""
    factory, owner, engine = native_dispatch_authority
    from lib import turn_lifecycle as turns
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.project_dispatch.service import ProjectDispatchService
    from lib.project_dispatch import admission
    from lib.identity import PrincipalContext
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    from lib.agent_core.execution_budget import model_budget_admission

    project = '/native-dispatch-acceptance/' + uuid.uuid4().hex
    coordinator, recipient = 'coord-' + uuid.uuid4().hex, 'worker-' + uuid.uuid4().hex
    principal = PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner,
                                      tenant_id='native-test', scopes=['chat'])
    for conv in (coordinator, recipient):
        factory(write=True).command('conversation.create', dict(conv_id=conv, user_id=owner,
            title='Disposable native acceptance', settings={'projectPath': project},
            created_at=1760000000000, updated_at=1760000000000), uuid.uuid4().hex)
    from tests._seed import seed_coordinator_binding
    binding = seed_coordinator_binding(project, coordinator, user_id=owner, client=factory)
    assert binding['availability'] == 'available'
    human = turns.append_settled_turn(coordinator, actor='human', projection={'content': 'delegate consult'},
        user_id=owner, command_id=uuid.uuid4().hex)['turn']
    repo = ProjectDispatchRepository(owner, project, client=factory)
    monkeypatch.setattr(admission, 'repository_for', lambda _: repo)
    request = dict(projectPath=project, coordinatorId=coordinator,
        authorizationSource={'conversationId': coordinator, 'turnId': human['turnId'],
            'projectionRevision': human['projectionRevision']},
        recipientId=recipient, brief='give a bounded consult report', model='fake-native-model',
        budget={'maxTokens': 20, 'maxModelRequests': 1, 'deadline': time.time()+300})
    assignment = repo.authorize(request, principal, uuid.uuid4().hex)
    sends = []

    def fake_transport_start(conv, cfg, data, watermark, bind, **kwargs):
        task_id = 'fake-task-' + uuid.uuid4().hex
        bind(task_id)
        task = dict(id=task_id, convId=conv, config=cfg, status='running',
            _userId=owner, _principalContext=admission.principal_for(cfg[admission.KEY]).to_payload(),
            _turnId=cfg['_turnId'], _attemptId=cfg['_attemptId'], _attended=False,
            abort_event=threading.Event(), usage={})
        admission.bind_account(task)
        lease = model_budget_admission(task)(input_tokens=4, max_output_tokens=6)
        lease.prepare_input({'messages': [{'role': 'user', 'content': request['brief']}]})
        lease.mark_dispatched()
        sends.append(task_id)  # Fake physical send: never enter a provider gateway.
        lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})
        factory(write=True).command('turn.event.record', dict(user_id=owner,
            attempt_id=cfg['_attemptId'], task_id=task_id, terminal=True, status='completed',
            projection={'content': 'verified native consult report'},
            settlement={'outcome': 'completed', 'cause': 'stop', 'resumeOptions': []}), uuid.uuid4().hex)
        return task_id, None

    commands = ConversationTurnCommandService(build_user_message=lambda message, *args: message,
        was_aborted_after=lambda *args: False, start_task=fake_transport_start)
    service = ProjectDispatchService(repo, commands=commands)
    first = service.dispatch(assignment['id'], principal=principal)
    assert first['execution']['status'] == 'completed'
    assert first['execution']['result']['content'] == 'verified native consult report'
    assert len(sends) == 1
    assert len(repo.read(assignment['id'])['modelAttempts']) == 1
    replay = service.dispatch(assignment['id'], principal=principal)
    assert replay['execution']['taskId'] == sends[0]
    assert len(sends) == 1
    rows = turns.list_turns(recipient, user_id=owner)['turns']
    assert sum(row['actor'] == 'virtual_user' for row in rows) == 1
    assert sum(row['actor'] == 'assistant' for row in rows) == 1
    review = service.review(assignment['id'], principal=principal, decision='accept', note='native verified',
        result_revision=first['execution']['resultRevision'], expected_revision=1)
    assert review['review']['decision'] == 'accept'
    assert review['canonicalDelivery'] is False
    engine.crash_cycle()
    assert repo.read(assignment['id'])['review']['decision'] == 'accept'
    assert service.dispatch(assignment['id'], principal=principal)['execution']['taskId'] == sends[0]
    assert len(sends) == 1


def _git(root, *args):
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(root), *args],
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_native_file_dispatch_checkpoint_publish_delivery_crash_replay(
        native_dispatch_authority, tmp_path, monkeypatch):
    from lib import integration_control as ic, integration_state_repository as state
    from lib import turn_lifecycle as turns
    from lib.agent_core.execution_budget import model_budget_admission
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.identity import PrincipalContext
    from lib.project_dispatch import admission
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.project_dispatch.service import ProjectDispatchService
    from lib.project_mod import delivery, modifications
    from lib.tasks_pkg.executor._execute import _execute_tool_one
    from lib.tasks_pkg.workspace_delivery import bind_worker_workspace

    factory, owner, engine = native_dispatch_authority
    root = tmp_path / 'canonical'
    root.mkdir()
    _git(root, 'init', '-b', 'native-dispatch-fixture')
    _git(root, 'config', 'user.name', 'Native dispatch fixture')
    _git(root, 'config', 'user.email', 'native-dispatch@example.invalid')
    (root / 'base.txt').write_text('unchanged canonical base\n', encoding='utf-8')
    _git(root, 'add', 'base.txt')
    _git(root, 'commit', '-m', 'disposable base')
    original_head = _git(root, 'rev-parse', 'HEAD')
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'managed'))
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', '')
    monkeypatch.setattr(modifications, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(modifications, '_live_change_listeners', [])
    monkeypatch.setattr(ic, '_start_or_wake_worker', lambda: None)
    monkeypatch.setattr(ic, '_push', lambda *args, **kwargs: None)
    monkeypatch.setattr(ic, '_gate_commands', lambda *args, **kwargs: (True, 'fixture gate'))
    monkeypatch.setattr(delivery, '_canonical_in_use', lambda *args: False)
    monkeypatch.setattr(delivery, '_RECOVERY_CURSOR', 0)

    project = str(root)
    coordinator, recipient = 'coord-' + uuid.uuid4().hex, 'worker-' + uuid.uuid4().hex
    principal = PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner,
                                      tenant_id='native-test', scopes=['chat'])
    for conv in (coordinator, recipient):
        factory(write=True).command('conversation.create', dict(conv_id=conv, user_id=owner,
            title='Native file dispatch fixture', settings={'projectPath': project},
            created_at=1760000000000, updated_at=1760000000000), uuid.uuid4().hex)
    binding_repo = ProjectCoordinationRepository(owner, client=factory)
    from tests._seed import seed_coordinator_binding
    binding = seed_coordinator_binding(project, coordinator, user_id=owner, client=factory)
    assert binding['availability'] == 'available'
    human = turns.append_settled_turn(coordinator, actor='human',
        projection={'content': 'Authorize writing allowed/result.txt; hold for explicit publication'},
        user_id=owner, command_id=uuid.uuid4().hex)['turn']
    repo = ProjectDispatchRepository(owner, project, client=factory)
    monkeypatch.setattr(admission, 'repository_for', lambda _: repo)
    request = dict(projectPath=project, coordinatorId=coordinator,
        authorizationSource={'conversationId': coordinator, 'turnId': human['turnId'],
                             'projectionRevision': human['projectionRevision']},
        recipientId=recipient, brief='Write allowed/result.txt in the owned bound workspace',
        model='fake-native-model',
        budget={'maxTokens': 10000, 'maxModelRequests': 1, 'deadline': time.time()+300},
        profile={'mode': 'files', 'tools': ['read_files', 'write_file', 'edit_file'],
                 'readPaths': ['allowed'], 'writePaths': ['allowed'], 'maxToolCalls': 8})
    command_id = 'native-files-' + uuid.uuid4().hex
    assignment = repo.authorize(request, principal, command_id)
    sends, tasks, workspaces = [], [], []
    expected_bytes = 'native executor bytes: café\n'.encode('utf-8')

    def fake_transport_start(conv, cfg, data, watermark, bind, **kwargs):
        task_id = 'native-file-task-' + uuid.uuid4().hex
        bind(task_id)  # Actual command birth binds native attempt/task before execution.
        task = dict(id=task_id, convId=conv, config=cfg, status='running',
            messages=[], usage={}, toolRounds=[], events=[], _userId=owner,
            _principalContext=admission.principal_for(cfg[admission.KEY]).to_payload(),
            _attended=False, _turnId=cfg['_turnId'], _attemptId=cfg['_attemptId'],
            abort_event=threading.Event())
        admission.bind_account(task)
        bind_worker_workspace(task)
        work = Path(task['config']['projectPath'])
        assert work != root and work.is_relative_to(tmp_path)
        assert admission.owned_workspace(task)['task_id'] == task_id
        tasks.append(task)
        workspaces.append(work)
        lease = model_budget_admission(task)(input_tokens=4, max_output_tokens=6)
        lease.prepare_input({'messages': [{'role': 'user', 'content': request['brief']}]})
        lease.mark_dispatched()
        sends.append(task_id)
        lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})
        arguments = {'path': 'allowed/result.txt', 'content': expected_bytes.decode('utf-8')}
        entry = {'name': 'write_file', 'query': 'Write authorized native result', 'arguments': arguments}
        result = _execute_tool_one(task, {}, 'write_file', 'native-tool-1', arguments, 0,
                                  entry, task['config'], str(work), True)
        assert 'Error:' not in str(result[1]), result
        assert (work / 'allowed/result.txt').read_bytes() == expected_bytes
        assert not (root / 'allowed/result.txt').exists()
        factory(write=True).command('turn.event.record', dict(user_id=owner,
            attempt_id=cfg['_attemptId'], task_id=task_id, terminal=True, status='completed',
            projection={'content': 'Native files ready for human review'},
            settlement={'outcome': 'completed', 'cause': 'stop', 'resumeOptions': []}), uuid.uuid4().hex)
        return task_id, None

    commands = ConversationTurnCommandService(build_user_message=lambda message, *args: message,
        was_aborted_after=lambda *args: False, start_task=fake_transport_start)
    service = ProjectDispatchService(repo, commands=commands)
    first = service.dispatch(assignment['id'], principal=principal)
    execution = first['execution']
    assert execution['status'] == 'completed'
    assert execution['taskId'] == sends[0]
    assert execution['result']['content'] == 'Native files ready for human review'
    task_id, result_revision = execution['taskId'], execution['resultRevision']
    charged = repo.read(assignment['id'])
    assert len(charged['modelAttempts']) == len(charged['toolAttempts']) == 1
    assert charged['modelAttempts'][0]['phase'] == 'settled'
    assert charged['modelAttempts'][0]['usage_complete'] is True
    assert charged['modelAttempts'][0]['reported_tokens'] == 10
    assert charged['toolAttempts'][0]['phase'] == 'executor_returned'
    assert charged['toolAttempts'][0]['tool_call_id'] == 'native-tool-1'

    delivery.settle_task_workspace(project, task_id, user_id=owner, outcome='completed')
    delivery.resume_pending_workspace_deliveries()
    row = state.get_workspace(project, task_id, user_id=owner)
    held = row['origin']['delivery']
    checkpoint = row['checkpoint_sha']
    assert held['state'] == 'blocked' and held['checkpointSha'] == checkpoint
    assert checkpoint and _git(root, 'show', checkpoint + ':allowed/result.txt') == expected_bytes.decode().strip()
    assert _git(root, 'rev-parse', 'HEAD') == original_head
    assert not (root / 'allowed/result.txt').exists()
    accepted = service.review(assignment['id'], principal=principal, decision='accept',
        note='Verified exact native workspace bytes', result_revision=result_revision, expected_revision=1)
    assert accepted['review']['decision'] == 'accept'
    assert accepted['publication'] is None and accepted['canonicalDelivery'] is False
    delivery.resume_pending_workspace_deliveries()
    assert not (root / 'allowed/result.txt').exists(), 'Review alone must not publish'
    with pytest.raises(ValueError):
        service.publish(assignment['id'], principal=principal, expected_revision=1,
                        result_revision=result_revision, expected_checkpoint_sha='different-checkpoint')
    published = service.publish(assignment['id'], principal=principal, expected_revision=1,
                               result_revision=result_revision, expected_checkpoint_sha=checkpoint)
    assert published['publication']['checkpointSha'] == checkpoint
    assert published['canonicalDelivery'] is False
    delivery.resume_pending_workspace_deliveries()
    final = service.view(assignment['id'], principal=principal)
    assert final['canonicalDelivery'] is True and final['delivery']['status'] == 'delivered'
    assert (root / 'allowed/result.txt').read_bytes() == expected_bytes
    delivered_row = state.get_workspace(project, task_id, user_id=owner)
    assert delivered_row['state'] == 'checkpointed'
    assert not delivered_row['candidate_sha'], 'Task delta delivery must not invent candidate promotion'
    assert _git(root, 'diff', '--cached', '--name-only') == ''
    receipt = delivered_row['origin']['delivery']
    assert receipt['state'] == 'delivered' and receipt['checkpointSha'] == checkpoint
    delivered_head = _git(root, 'rev-parse', 'HEAD')
    assert final['delivery']['targetSha'] == receipt['targetSha']
    assert _git(root, 'show', receipt['targetSha'] + ':allowed/result.txt') == expected_bytes.decode().strip()
    durable = repo.read(assignment['id'])

    # Model lost ACK after successful publication/delivery, then SIGKILL/reopen
    # native storage. Explicit service replays must not purchase another task.
    engine.crash_cycle()
    assert repo.read(assignment['id']) == durable
    assert binding_repo.read(project) == binding
    assert state.get_workspace(project, task_id, user_id=owner)['origin']['delivery'] == receipt
    assert repo.authorize(request, principal, command_id) == durable
    for _ in range(2):
        replay = service.dispatch(assignment['id'], principal=principal)
        assert replay['execution'] == execution
        assert replay['canonicalDelivery'] is True
        assert service.publish(assignment['id'], principal=principal, expected_revision=1,
            result_revision=result_revision, expected_checkpoint_sha=checkpoint)['publication'] == published['publication']
        delivery.resume_pending_workspace_deliveries()
    assert sends == [task_id] and len(tasks) == len(workspaces) == 1
    assert repo.read(assignment['id']) == durable
    assert state.get_workspace(project, task_id, user_id=owner)['origin']['delivery'] == receipt
    rows = turns.list_turns(recipient, user_id=owner)['turns']
    assert sum(row['actor'] == 'virtual_user' for row in rows) == 1
    assert sum(row['actor'] == 'assistant' for row in rows) == 1
    assert _git(root, 'rev-parse', 'HEAD') == delivered_head
    assert delivered_head == original_head
    assert _git(root, 'diff', '--cached', '--name-only') == ''
    assert _git(root, 'status', '--porcelain') == '?? allowed/'
    assert (root / 'allowed/result.txt').read_bytes() == expected_bytes
