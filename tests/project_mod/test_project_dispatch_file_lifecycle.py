"""Actual file executor + temporary Git delivery; fake transport/grant ports only."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from lib.identity import PrincipalContext
from lib.project_dispatch import admission
from lib.project_dispatch.repository import ProjectDispatchRepository
from lib.project_dispatch.service import ProjectDispatchService
from lib import integration_control as ic, integration_state_repository as state
from tests.project_mod.test_project_dispatch_repository import context as context_fixture
from tests.storage.test_integration_delivery import repo as repo_fixture, automatic_settle, git

context = context_fixture
repo = repo_fixture
pytestmark = pytest.mark.unit


def file_context(context, repo, monkeypatch):
    monkeypatch.delenv('TOFU_WORKSPACE_MODE', raising=False)
    c = context; root, _, owner = repo
    for item in c.conversations.rows.values():
        item['user_id'] = owner; item['settings']['projectPath'] = str(root)
    request = deepcopy(c.request)
    request.update(projectPath=str(root), brief='Create allowed/result.txt inside the owned checkout')
    request['budget'].update(deadline=time.time()+300, maxTokens=10000)
    request['profile'] = {'mode': 'files', 'tools': ['read_files', 'write_file', 'edit_file'],
                          'readPaths': ['allowed'], 'writePaths': ['allowed'], 'maxToolCalls': 8}
    binding = SimpleNamespace(read=lambda path: {**c.binding.value, 'projectPath': str(root)})
    principal = PrincipalContext.user(subject_id='user:1', owner_user_id=owner,
                                      tenant_id='tenant-a', scopes={'chat'})
    output = {'turnId': 'worker-turn', 'conversationId': 'recipient', 'actor': 'assistant',
              'projectionRevision': 1, 'projection': {'content': ''}}
    attempt = {'attemptId': 'worker-attempt', 'turnId': 'worker-turn',
               'conversationId': 'recipient', 'taskId': 'automatic', 'status': 'running'}
    def turns(conv, tid, **kw):
        return deepcopy(c.turn if conv == 'coordinator' else output)
    repository = ProjectDispatchRepository(owner, str(root), client=c.client, storage_version=1,
        conversations=c.conversations, binding_repository=binding, turn_get=turns,
        attempt_get=lambda *a, **kw: deepcopy(attempt), clock=lambda: c.now[0])
    monkeypatch.setattr(admission, 'repository_for', lambda _: repository)
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(root.parent/'managed'))
    monkeypatch.setattr(ic, '_gate_commands', lambda *a, **kw: (True, 'deterministic fixture gate'))
    from lib.project_mod import modifications
    from lib import file_history
    monkeypatch.setattr(modifications, 'SESSIONS_DIR', str(root.parent/'sessions'))
    monkeypatch.setattr(modifications, '_live_change_listeners', [])
    monkeypatch.setattr(file_history, 'track_edit', lambda *a, **kw: None)
    return SimpleNamespace(c=c, root=root, owner=owner, request=request, principal=principal,
        repository=repository, output=output, attempt=attempt, turns=turns)


@pytest.mark.parametrize('direct_source', ['default', 'process', 'repository'])
def test_file_worker_report_accept_publish_receipt_and_checkpoint_pin(
        context, repo, monkeypatch, direct_source):
    from lib.tasks_pkg.workspace_delivery import bind_worker_workspace
    from lib.tasks_pkg.executor._execute import _execute_tool_one
    from lib.agent_core.execution_budget import model_budget_admission
    f = file_context(context, repo, monkeypatch)

    if direct_source == 'process':
        monkeypatch.setenv('TOFU_WORKSPACE_MODE', 'direct')
    elif direct_source == 'repository':
        git(f.root, 'config', '--local', 'tofu.workspaceMode', 'direct')
    row = f.repository.authorize(f.request, f.principal, 'file-command')
    f.repository.bind_execution(row['id'], 'worker-turn', 'worker-attempt')
    trusted = admission.TrustedDispatch(f.repository.admission(row['id']))
    cfg = trusted.capture({}, f.owner, trusted.principal, 'recipient')
    cfg.update(_turnOwnerUserId=f.owner, _turnId='worker-turn', _attemptId='worker-attempt')
    task = {'id': 'automatic', 'convId': 'recipient', 'config': cfg, 'status': 'running',
        'messages': [], 'usage': {}, 'toolRounds': [], 'events': [], '_userId': f.owner,
        '_principalContext': trusted.principal.to_payload(), '_attended': False,
        '_turnId': 'worker-turn', '_attemptId': 'worker-attempt', 'abort_event': threading.Event()}
    admission.bind_account(task)
    bind_worker_workspace(task)
    work = Path(task['config']['projectPath'])
    assert work != f.root and admission.owned_workspace(task)['task_id'] == 'automatic'
    effects = []
    def acknowledge_effect(_task, event):
        if event['type'] == 'tool_effect':
            assert not (work/'allowed/result.txt').exists()
            effects.append(deepcopy(event))
        return True
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', acknowledge_effect)
    lease = model_budget_admission(task)(input_tokens=4, max_output_tokens=6)
    lease.mark_dispatched(); lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})
    round_entry = {'name': 'write_file', 'query': 'Write the authorized result', 'arguments': {'path': 'allowed/result.txt', 'content': 'actual scoped bytes\n'}}
    result = _execute_tool_one(task, {}, 'write_file', 'tool-1', round_entry['arguments'], 0,
        round_entry, task['config'], str(work), True)
    assert 'Error:' not in str(result[1]), result
    assert effects and effects[0]['toolCallId'] == 'tool-1'
    assert (work/'allowed/result.txt').exists(), (result, round_entry)
    assert (work/'allowed/result.txt').read_text() == 'actual scoped bytes\n'
    assert not (f.root/'allowed/result.txt').exists()
    charged = f.repository.read(row['id'])
    assert len(charged['modelAttempts']) == 1
    assert charged['toolAttempts'][0]['phase'] == 'executor_returned'
    f.attempt['status'] = 'completed'
    f.output.update(projectionRevision=2, projection={'content': 'Files ready for review'})
    held = automatic_settle(repo)
    assert held['state'] == 'blocked'

    dependent_request = deepcopy(f.request)
    dependent_request.pop('profile')
    dependent_request['brief'] = 'Consult after this exact file result is accepted and delivered'
    dependent_request['dependencies'] = [{'assignmentId': row['id'], 'grantRevision': 1,
                                           'resultRevision': 2, 'checkpointSha': held['checkpointSha']}]
    dependent = f.repository.authorize(dependent_request, f.principal, 'dependent-command')
    dependent_binding = f.repository.admission(dependent['id'])
    assert f.repository.dependency_states(dependent)[0]['status'] == 'waiting_review'
    with pytest.raises(ValueError, match='dependencies'):
        f.repository.validate(dependent_binding)
    assert dependent['execution'] is None and dependent['modelAttempts'] == []
    service = ProjectDispatchService(f.repository, commands=SimpleNamespace(), turn_get=f.turns,
        attempt_get=lambda *a, **kw: deepcopy(f.attempt), workspace_get=state.find_workspace,
        publication_lock=lambda root: nullcontext(), publication_wake=lambda: None)
    accepted = service.review(row['id'], principal=f.principal, decision='accept', note='Checked bytes',
                              result_revision=2, expected_revision=1)
    assert accepted['canonicalDelivery'] is False

    assert f.repository.dependency_states(dependent)[0]['status'] == 'waiting_delivery'
    with pytest.raises(ValueError, match='dependencies'):
        f.repository.validate(dependent_binding)
    published = service.publish(row['id'], principal=f.principal, expected_revision=1,
                                result_revision=2, expected_checkpoint_sha=held['checkpointSha'])
    assert published['canonicalDelivery'] is False
    assert service.publish(row['id'], principal=f.principal, expected_revision=1,
        result_revision=2, expected_checkpoint_sha=held['checkpointSha'])['publication'] == published['publication']
    from lib.project_mod import delivery
    delivery.resume_pending_workspace_deliveries()
    assert (f.root/'allowed/result.txt').read_text() == 'actual scoped bytes\n'
    final = service.view(row['id'], principal=f.principal)
    assert final['canonicalDelivery'] is True and final['delivery']['status'] == 'delivered'

    assert f.repository.dependency_states(dependent)[0]['status'] == 'ready'
    assert f.repository.validate(dependent_binding)['execution'] is None
    assert f.repository.read(dependent['id'])['modelAttempts'] == []
    with pytest.raises(ValueError):
        service.publish(row['id'], principal=f.principal, expected_revision=1,
                        result_revision=2, expected_checkpoint_sha='different-checkpoint')


    f.repository.revoke(row['id'], expected_revision=1)
    assert f.repository.dependency_states(dependent)[0]['reason'] == 'revoked'
    with pytest.raises(ValueError, match='dependencies'):
        f.repository.validate(dependent_binding)


def test_linked_rework_requires_current_review_and_fresh_human_grant(context):
    c = context
    c.request['profile'] = {'mode':'files','tools':['read_files'], 'readPaths':['src'],
                           'writePaths':[], 'maxToolCalls':2}
    first = c.repo.authorize(c.request, c.principal, 'first')
    c.repo.bind_execution(first['id'], 'result-turn', 'result-attempt')
    c.repo.record_review(first['id'], 'rework', 'Revise', 5, 1)
    second = deepcopy(c.request)
    second['reworkOf'] = {'assignmentId':first['id'], 'grantRevision':1, 'resultRevision':5}
    second['brief'] = 'Explicit newly budgeted revision'
    born = c.repo.authorize(second, c.principal, 'second')
    assert born['id'] != first['id'] and born['execution'] is None and born['modelAttempts'] == []
    assert c.repo.authorize(second, c.principal, 'second') == born
    second['reworkOf']['resultRevision'] = 6
    with pytest.raises(ValueError): c.repo.authorize(second, c.principal, 'invalid')


@pytest.mark.parametrize('spelling', ['canonical', 'symlink', 'ancestor'])
def test_file_worker_in_plain_folder_runs_isolated_and_publishes_back(context, repo, monkeypatch, spelling):
    """The RSI Exam shape: nobody ran Git, the folder holds notes and bulky data."""
    import os
    from lib.project_mod import delivery
    from lib.project_mod.managed_repository import LARGE_FILE_BYTES
    from lib.tasks_pkg.workspace_delivery import bind_worker_workspace
    from lib.tasks_pkg.executor._execute import _execute_tool_one
    from lib.agent_core.execution_budget import model_budget_admission
    root, _, owner = repo
    plain = (root.parent / 'plain-project').resolve()
    (plain / 'allowed').mkdir(parents=True)
    (plain / 'allowed' / 'notes.md').write_text('person notes\n')
    panel = plain / 'allowed' / 'panel.parquet'
    with open(panel, 'wb') as stream:
        stream.truncate(LARGE_FILE_BYTES)
    project = plain
    if spelling != 'canonical':
        alias = root.parent / 'project-alias'
        alias.symlink_to(plain if spelling == 'symlink' else plain.parent, target_is_directory=True)
        project = alias if spelling == 'symlink' else alias / plain.name
    f = file_context(context, (project, None, owner), monkeypatch)
    row = f.repository.authorize(f.request, f.principal, 'plain-command')
    f.repository.bind_execution(row['id'], 'worker-turn', 'worker-attempt')
    trusted = admission.TrustedDispatch(f.repository.admission(row['id']))
    cfg = trusted.capture({}, f.owner, trusted.principal, 'recipient')
    cfg.update(_turnOwnerUserId=f.owner, _turnId='worker-turn', _attemptId='worker-attempt')
    task = {'id': 'automatic', 'convId': 'recipient', 'config': cfg, 'status': 'running',
        'messages': [], 'usage': {}, 'toolRounds': [], 'events': [], '_userId': f.owner,
        '_principalContext': trusted.principal.to_payload(), '_attended': False,
        '_turnId': 'worker-turn', '_attemptId': 'worker-attempt', 'abort_event': threading.Event()}
    admission.bind_account(task)
    bind_worker_workspace(task)

    work = Path(task['config']['projectPath'])
    assert work != plain and admission.owned_workspace(task)['task_id'] == 'automatic'
    assert task['convId'] == 'recipient'
    assert task['config']['canonicalProjectPath'] == str(project)
    assert task['config'][admission.KEY]['projectPath'] == str(project)
    assert f.c.conversations.rows['recipient']['settings']['projectPath'] == str(project)
    assert state.get_workspace(str(plain), 'automatic', user_id=owner)['origin'][admission.KEY]['projectPath'] == str(project)
    assert (plain / '.git').is_dir() and git(plain, 'status', '--porcelain') == ''
    assert (work / 'AGENTS.md').read_text() == (plain / 'AGENTS.md').read_text()
    assert (work / 'allowed' / 'notes.md').read_text() == 'person notes\n'
    assert os.readlink(work / 'allowed' / 'panel.parquet') == str(panel)
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda _task, event: True)
    lease = model_budget_admission(task)(input_tokens=4, max_output_tokens=6)
    lease.mark_dispatched(); lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})
    round_entry = {'name': 'write_file', 'query': 'Write the result',
                   'arguments': {'path': 'allowed/result.txt', 'content': 'worker bytes\n'}}
    result = _execute_tool_one(task, {}, 'write_file', 'tool-1', round_entry['arguments'], 0,
        round_entry, task['config'], str(work), True)
    assert 'Error:' not in str(result[1]), result
    assert not (plain / 'allowed' / 'result.txt').exists()

    f.attempt['status'] = 'completed'
    f.output.update(projectionRevision=2, projection={'content': 'Files ready for review'})
    held = automatic_settle((plain, None, owner))
    assert held['state'] == 'blocked'
    if spelling != 'canonical':
        from lib.llm.llm_errors import ToolAdmissionRefused
        from lib.project_dispatch.publication import delivery_view, publication_reason
        registered = state.get_workspace(str(plain), 'automatic', user_id=owner)
        assigned = f.repository.read(row['id'])
        assert delivery_view(assigned, f.attempt, state.find_workspace)['checkpointSha'] == held['checkpointSha']
        target = alias.readlink()
        alias.unlink()
        alias.symlink_to(root, target_is_directory=True)
        try:
            with pytest.raises(ToolAdmissionRefused, match='registered owned isolated worktree'):
                admission.owned_workspace(task)
            assert publication_reason(registered, held) == 'project_publication_identity_changed'
            assert delivery_view(assigned, f.attempt, state.find_workspace) is None
            assert not (root / 'allowed' / 'result.txt').exists()
        finally:
            alias.unlink()
            alias.symlink_to(target, target_is_directory=True)
    service = ProjectDispatchService(f.repository, commands=SimpleNamespace(), turn_get=f.turns,
        attempt_get=lambda *a, **kw: deepcopy(f.attempt), workspace_get=state.find_workspace,
        publication_lock=lambda root: nullcontext(), publication_wake=lambda: None)
    service.review(row['id'], principal=f.principal, decision='accept', note='Checked bytes',
                   result_revision=2, expected_revision=1)
    service.publish(row['id'], principal=f.principal, expected_revision=1,
                    result_revision=2, expected_checkpoint_sha=held['checkpointSha'])
    delivery.resume_pending_workspace_deliveries()
    settled = state.get_workspace(str(plain), 'automatic', user_id=owner)['origin']['delivery']
    assert settled['state'] == 'delivered', settled.get('reason')
    assert (plain / 'allowed' / 'result.txt').read_text() == 'worker bytes\n'
    assert service.view(row['id'], principal=f.principal)['canonicalDelivery'] is True
    assert (project / 'allowed' / 'result.txt').read_text() == 'worker bytes\n'
    if spelling != 'canonical':
        assert alias.is_symlink()
    assert panel.stat().st_size == LARGE_FILE_BYTES and not panel.is_symlink()
    assert 'allowed/panel.parquet' not in git(plain, 'ls-tree', '-r', '--name-only', held['checkpointSha'])


def test_file_worker_in_a_repository_subdirectory_fails_binding_before_any_model_request(
        context, repo, monkeypatch):
    from lib.tasks_pkg.workspace_delivery import WorkspaceBindingError, bind_worker_workspace
    root, _, owner = repo
    plain = root / 'nested-project'
    plain.mkdir()
    f = file_context(context, (plain, None, owner), monkeypatch)
    row = f.repository.authorize(f.request, f.principal, 'plain-command')
    f.repository.bind_execution(row['id'], 'worker-turn', 'worker-attempt')
    trusted = admission.TrustedDispatch(f.repository.admission(row['id']))
    cfg = trusted.capture({}, f.owner, trusted.principal, 'recipient')
    cfg.update(_turnOwnerUserId=f.owner, _turnId='worker-turn', _attemptId='worker-attempt')
    task = {'id': 'automatic', 'convId': 'recipient', 'config': cfg, 'status': 'running',
        'messages': [], 'usage': {}, 'toolRounds': [], 'events': [], '_userId': f.owner,
        '_principalContext': trusted.principal.to_payload(), '_attended': False,
        '_turnId': 'worker-turn', '_attemptId': 'worker-attempt', 'abort_event': threading.Event()}
    admission.bind_account(task)
    with pytest.raises(WorkspaceBindingError, match='subdirectory of the Git repository') as refused:
        bind_worker_workspace(task)
    assert refused.value.user_hint.startswith('解决办法 / How to fix:')
    assert f'Open {root}' in refused.value.user_hint and 'git ' not in refused.value.user_hint
    assert task['config']['projectPath'] == str(plain) and '_workspaceDelivery' not in task
    assert state.find_workspace(str(plain), 'automatic', user_id=owner) is None
    assert f.repository.read(row['id'])['modelAttempts'] == []
    assert not (plain / '.git').exists() and not (plain / 'AGENTS.md').exists()


@pytest.mark.parametrize('predecessor', ['interrupted_by_restart', 'failed_before_entry'])
def test_successor_continues_the_assignment_checkout_as_its_own_task(context, repo, monkeypatch, predecessor):
    """Every durable-worker attempt is its own task; the assignment's checkout and ledger continue."""
    from lib.project_mod import delivery
    from lib.tasks_pkg.workspace_delivery import bind_worker_workspace, settle_task_workspaces
    from lib.tasks_pkg.executor._execute import _execute_tool_one
    from lib.agent_core.execution_budget import model_budget_admission
    f = file_context(context, repo, monkeypatch)
    # A restart restores ordinals into uncapped tool accounts only (a finite cap refuses).
    f.request['profile'].pop('maxToolCalls')
    row = f.repository.authorize(f.request, f.principal, 'resumed-file-command')
    trusted = admission.TrustedDispatch(f.repository.admission(row['id']))
    trusted.bind_execution('worker-turn', 'worker-attempt')
    work_id = 'project-worker:' + row['id']
    attempts = {'worker-attempt': {**f.attempt, 'taskId': 'conversation-attempt:worker-attempt'}}
    f.repository.attempt_get = lambda attempt_id, **kw: deepcopy(attempts.get(attempt_id))
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda _task, _event: True)

    def carrier(attempt_id, resume=None, operation='continue'):
        cfg = trusted.capture({}, f.owner, trusted.principal, 'recipient')
        cfg.update(_turnOwnerUserId=f.owner, _turnId='worker-turn', _attemptId=attempt_id)
        if resume is not None:
            cfg.update(_turnOperation=operation, _resumeSource=resume)
        task = {'id': attempts[attempt_id]['taskId'], 'convId': 'recipient', 'config': cfg,
            'status': 'running', 'messages': [], 'usage': {}, 'toolRounds': [], 'events': [],
            '_userId': f.owner, '_principalContext': trusted.principal.to_payload(), '_attended': False,
            '_turnId': 'worker-turn', '_attemptId': attempt_id, 'abort_event': threading.Event()}
        admission.bind_account(task)
        bind_worker_workspace(task)
        lease = model_budget_admission(task)(input_tokens=4, max_output_tokens=6)
        lease.mark_dispatched(); lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})
        return task, lease.ordinal

    def write(task, call_id, content):
        arguments = {'path': 'allowed/result.txt', 'content': content}
        entry = {'name': 'write_file', 'query': 'Write the authorized result', 'arguments': arguments}
        result = _execute_tool_one(task, {}, 'write_file', call_id, arguments, 0, entry,
                                   task['config'], task['config']['projectPath'], True)
        assert 'Error:' not in str(result[1]), result

    first_task = attempts['worker-attempt']['taskId']
    if predecessor == 'interrupted_by_restart':
        first, first_ordinal = carrier('worker-attempt')
        work = Path(first['config']['projectPath'])
        assert work != f.root and admission.owned_workspace(first)['task_id'] == work_id
        write(first, 'before-restart', 'draft from the first attempt\n')
        # A restart interrupts the first attempt; its checkout waits for the resumed one.
        first['status'] = 'interrupted'
        attempts['worker-attempt']['status'] = 'interrupted'
        settle_task_workspaces(first)
        resume = {'attemptId': 'worker-attempt', 'taskId': first_task, 'status': 'interrupted',
                  'cause': 'server_restart'}
    else:
        # The registration failure of 2026-11-07: the attempt settled failed before entry.
        attempts['worker-attempt']['status'] = 'failed'
        resume = {'attemptId': 'worker-attempt', 'taskId': first_task, 'status': 'failed',
                  'cause': 'task_start_failed'}

    attempts['resumed-attempt'] = {**attempts['worker-attempt'], 'attemptId': 'resumed-attempt',
                                   'taskId': '', 'status': 'pending'}
    f.repository.continue_execution(row['id'], f.repository.read(row['id'])['execution'],
                                    'worker-turn', 'resumed-attempt')
    attempts['resumed-attempt'].update(taskId='conversation-attempt:resumed-attempt', status='running')
    second, second_ordinal = carrier('resumed-attempt', resume=resume,
        operation='continue' if predecessor == 'interrupted_by_restart' else 'regenerate')
    assert second['id'] != first_task and admission.owned_workspace(second)['task_id'] == work_id
    ledger_before = f.repository.read(row['id'])
    if predecessor == 'interrupted_by_restart':
        assert Path(second['config']['projectPath']) == work
        assert (work/'allowed/result.txt').read_text() == 'draft from the first attempt\n'
        expected_models, expected_tools = [1, 2], [(1, 'before-restart'), (2, 'after-restart')]
    else:
        assert ledger_before.get('toolAttempts', []) == []
        expected_models, expected_tools = [1], [(1, 'after-restart')]
    write(second, 'after-restart', 'finished by the resumed attempt\n')
    ledger = f.repository.read(row['id'])
    assert second_ordinal == expected_models[-1]
    assert [(item['root_task_id'], item['attempt_ordinal']) for item in ledger['modelAttempts']] == [
        (work_id, ordinal) for ordinal in expected_models]
    assert [(item['root_task_id'], item['tool_ordinal'], item['tool_call_id'])
            for item in ledger['toolAttempts']] == [(work_id, *tool) for tool in expected_tools]

    attempts['resumed-attempt']['status'] = 'completed'
    f.output.update(projectionRevision=2, projection={'content': 'Files ready for review'})
    second.update(status='done', finishReason='stop')
    settle_task_workspaces(second)
    delivery.resume_pending_workspace_deliveries()
    registered = state.get_workspace(str(f.root), work_id, user_id=f.owner)
    held = registered['origin']['delivery']
    assert held['state'] == 'blocked' and registered['origin']['executorTaskId'] == second['id']
    assert state.find_workspace(str(f.root), second['id'], user_id=f.owner) is None
    service = ProjectDispatchService(f.repository, commands=SimpleNamespace(), turn_get=f.turns,
        attempt_get=f.repository.attempt_get, workspace_get=state.find_workspace,
        publication_lock=lambda root: nullcontext(), publication_wake=lambda: None)
    service.review(row['id'], principal=f.principal, decision='accept', note='Checked bytes',
                   result_revision=2, expected_revision=1)
    service.publish(row['id'], principal=f.principal, expected_revision=1,
                    result_revision=2, expected_checkpoint_sha=held['checkpointSha'])
    delivery.resume_pending_workspace_deliveries()
    final = service.view(row['id'], principal=f.principal)
    assert final['canonicalDelivery'] is True and final['delivery']['taskId'] == work_id
    assert final['execution']['taskId'] == second['id']
    assert (f.root/'allowed/result.txt').read_text() == 'finished by the resumed attempt\n'
