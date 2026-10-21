"""One accepted Coordinator request delegates via actual native Turn admission.

No paid provider: substitute transport only, retain native authorization, budgets,
ordinary tool resolution, isolated Git workspaces and report/unread persistence.
"""
from pathlib import Path
import threading
import uuid

import pytest

from tests.storage.test_native_project_dispatch import native_dispatch_authority, _git

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = [pytest.mark.api,
              pytest.mark.parametrize('semantic_authority', ['native'], indirect=True)]


@pytest.mark.parametrize('continue_after_restart', [False, True])
def test_native_delegate_execute_await_report_and_restart(native_dispatch_authority, tmp_path, monkeypatch, continue_after_restart):
    from lib import turn_lifecycle as turns, integration_control as ic
    from lib.project_mod import modifications, delivery
    from lib.conversations.coordinator_tools import execute_coordinator_tool
    from lib.conversations import coordinator_workers
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.coordinator_reports import CoordinatorReports
    from lib.conversations.repository import ConversationRepository
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    from lib.project_dispatch import admission
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.project_dispatch.service import ProjectDispatchService
    from lib.project_dispatch.delegation import capture_worker_policy
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.workspace_delivery import bind_worker_workspace
    from lib.tasks_pkg.executor._execute import _execute_tool_one
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    from lib.agent_core.execution_budget import model_budget_admission
    import json

    factory, owner, engine = native_dispatch_authority
    root = tmp_path / 'canonical'; root.mkdir()
    _git(root, 'init', '-b', 'coordinator-workers')
    _git(root, 'config', 'user.name', 'Test')
    _git(root, 'config', 'user.email', 'test@example.invalid')
    (root / 'base.txt').write_text('base\n')
    _git(root, 'add', 'base.txt'); _git(root, 'commit', '-m', 'base')
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'workspaces'))
    monkeypatch.setattr(modifications, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(modifications, '_live_change_listeners', [])
    monkeypatch.setattr(ic, '_start_or_wake_worker', lambda: None)
    monkeypatch.setattr(ic, '_push', lambda *a, **kw: None)
    monkeypatch.setattr(ic, '_gate_commands', lambda *a, **kw: (True, 'fixture verification'))
    monkeypatch.setattr(delivery, '_canonical_in_use', lambda *a: False)
    monkeypatch.setattr(delivery, '_RECOVERY_CURSOR', 0)
    binding = ProjectCoordinationRepository(owner).create(str(root), folder_id='native-folder',
        expected_revision=0, command_id='coordinator')
    conv = binding['conversationId']
    pair = turns.create_turn_pair(conv, user_id=owner, command_id='original-request',
        input_projection={'content': 'Implement both parts and verify the result'}, config={})
    root_id = 'native-file-coordinator-' + uuid.uuid4().hex
    turns.bind_task(pair['attempt']['attemptId'], root_id, user_id=owner)
    turns.mark_task_started(pair['attempt']['attemptId'], root_id, user_id=owner)
    task = {'id': root_id, 'convId': conv, '_userId': owner, '_attended': True,
            '_turnId': pair['attempt']['turnId'], '_attemptId': pair['attempt']['attemptId'],
            '_principalContext': PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner,
                tenant_id='native-test', scopes=[]).to_payload(),
            'config': {'projectPath': str(root), 'codeExecEnabled': True, 'autoApply': True}}
    from lib.project_dispatch.repository import CONTRACT
    names = ['read_files', 'write_file', 'run_command', 'spawn_agents', 'timer_create']
    if continue_after_restart:
        names += CONTRACT['continuationTools']
    capture_worker_policy(task, task['config'], [{'function': {'name': n}} for n in names], 'fixture-model')
    if continue_after_restart:
        # Capture the original ceiling through actual root assembly, then later
        # recover it from a planning-only synthetic Coordinator catalog.
        assemble_round_tools(task['config'], task, {'model': 'fixture-model',
            'project_path': str(root), 'project_enabled': True, 'search_mode': 'off', 'search_enabled': False,
            'fetch_enabled': False, 'code_exec_enabled': True, 'browser_enabled': False,
            'desktop_enabled': False, 'image_gen_enabled': False})
        assert 'get_project_workers' in task['_coordinatorWorkerPolicy'].coordinator_tools
    repo = ProjectDispatchRepository(owner, str(root))
    from lib.project_dispatch.request_budget import bind_source_account, allowance
    source_budget = bind_source_account(task, repository=repo)
    source_lease = task['_executionBudget'].reserve_model(task, input_tokens=5, max_output_tokens=5)
    source_lease.mark_dispatched(); source_lease.settle({'prompt_tokens': 5, 'completion_tokens': 5})
    sends, workspaces = [], []

    def transport(conv, cfg, data, watermark, bind, **kwargs):
        identity = 'native-file-worker-' + uuid.uuid4().hex
        bind(identity)
        worker = {'id': identity, 'convId': conv, 'config': cfg, 'status': 'running',
            'messages': [], 'usage': {}, 'toolRounds': [], 'events': [], '_userId': owner,
            '_principalContext': admission.principal_for(cfg[admission.KEY]).to_payload(),
            '_attended': False, '_turnId': cfg['_turnId'], '_attemptId': cfg['_attemptId'],
            'abort_event': threading.Event()}
        admission.bind_account(worker)
        if conv == binding['conversationId']:
            assert not admission.requires_isolated_workspace(worker)
            schemas, available = assemble_round_tools(cfg, worker, {'model': 'fixture-model',
                'project_path': str(root), 'project_enabled': True, 'search_mode': 'off', 'search_enabled': False,
                'fetch_enabled': False, 'code_exec_enabled': True, 'browser_enabled': False,
                'desktop_enabled': False, 'image_gen_enabled': False})
            assert available
            catalog = {s['function']['name'] for s in worker['_executable_tool_catalog']}
            assert {'delegate_project_work', 'get_project_workers', 'finish_project_request'} <= catalog
            assert not {'write_file', 'run_command', 'spawn_agents'} & catalog
            worker['_testCoordinatorSchemas'] = schemas
            lease = model_budget_admission(worker)(input_tokens=4, max_output_tokens=6)
            lease.mark_dispatched(); lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})
            continuations.append(worker)
            return identity, None
        bind_worker_workspace(worker)
        work = Path(worker['config']['projectPath']); workspaces.append(work)
        assert work != root and admission.owned_workspace(worker)['task_id'] == identity
        schemas, available = assemble_round_tools(cfg, worker, {'model': 'fixture-model',
            'project_path': str(work), 'project_enabled': True, 'search_mode': 'off', 'search_enabled': False,
            'fetch_enabled': False, 'code_exec_enabled': True, 'browser_enabled': False,
            'desktop_enabled': False, 'image_gen_enabled': False})
        assert available
        catalog = {s['function']['name'] for s in worker['_executable_tool_catalog']}
        assert {'read_files', 'write_file', 'run_command'} <= catalog
        assert not {'spawn_agents', 'timer_create', 'delegate_project_work'} & catalog
        lease = model_budget_admission(worker)(input_tokens=4, max_output_tokens=6)
        lease.mark_dispatched(); sends.append(identity)
        lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})
        args = {'path': 'result.txt', 'content': 'native independent worker result\n'}
        entry = {'name': 'write_file', 'arguments': args, 'query': 'Implement the assigned result'}
        result = _execute_tool_one(worker, {}, 'write_file', 'native-write', args, 0, entry,
                                   cfg, str(work), True, schemas)
        assert 'Error:' not in str(result[1]), result
        assert (work / 'result.txt').read_text() == args['content']
        assert not (root / 'result.txt').exists()
        command = {'command': 'python3 -c "from pathlib import Path; assert Path(\'result.txt\').read_text().startswith(\'native independent\')"'}
        checked = _execute_tool_one(worker, {}, 'run_command', 'native-check', command, 1,
            {'name': 'run_command', 'arguments': command, 'query': 'Verify written result'}, cfg, str(work), True, schemas)
        assert 'Error:' not in str(checked[1]), checked
        factory(write=True).command('turn.event.record', dict(user_id=owner,
            attempt_id=cfg['_attemptId'], task_id=identity, terminal=True, status='completed',
            projection={'content': 'Verified independent worker bytes'},
            settlement={'outcome': 'completed', 'cause': 'stop', 'resumeOptions': []}), uuid.uuid4().hex)
        return identity, None

    continuations = []
    commands = ConversationTurnCommandService(build_user_message=lambda message, *args: message,
        was_aborted_after=lambda *args: False, start_task=transport)
    service = ProjectDispatchService(repo, commands=commands)
    monkeypatch.setattr(coordinator_workers, 'ProjectDispatchService', lambda repository: service)
    arguments = {'title': 'Independent implementation', 'brief': 'Write and verify result.txt', 'idempotency_key': 'implementation'}
    first = json.loads(execute_coordinator_tool(task, 'delegate_project_work', arguments))
    assert first.get('status') == 'completed', first
    second = json.loads(execute_coordinator_tool(task, 'delegate_project_work', arguments))
    assert second == first and len(sends) == 1
    worker = ConversationRepository().get(first['conversation_id'], user_id=owner, include_messages=False)
    assert worker.metadata['settings']['folderId'] == 'native-folder'
    evidence = json.loads(execute_coordinator_tool(task, 'await_project_workers', {'assignment_ids': [first['assignment_id']]}))
    assert evidence['wait'] == 'settled' and evidence['workers'][0]['result_excerpt'] == 'Verified independent worker bytes'
    row = repo.read(first['assignment_id'])
    assert row['modelAttempts'][0]['reported_tokens'] == 10
    assert allowance(repo, repo._load()[0], source_budget['id'])['observedTokens'] == 20
    assert row['toolAttempts'][0]['phase'] == 'executor_returned'
    assert row['toolAttempts'][1]['phase'] == 'executor_returned'
    delivery.settle_task_workspace(str(root), sends[0], user_id=owner, outcome='completed')
    delivery.resume_pending_workspace_deliveries()
    view = service.view_for_coordinator(first['assignment_id'], scope={'user_id': owner,
        'project_path': str(root), 'conversation_id': conv, 'binding_revision': binding['revision']})
    assert view['delivery']['status'] == 'blocked' and not view['canonicalDelivery']
    if continue_after_restart:
        factory(write=True).command('turn.event.record', dict(user_id=owner,
            attempt_id=task['_attemptId'], task_id=task['id'], terminal=True, status='completed',
            projection={'content': 'Delegated independent implementation'},
            settlement={'outcome': 'completed', 'cause': 'stop', 'resumeOptions': []}), uuid.uuid4().hex)
        engine.crash_cycle()
        from lib.project_dispatch.recovery import recover_persisted_dispatch
        from lib.project_dispatch.continuation import recover_coordinator
        monkeypatch.setattr('lib.project_dispatch.recovery.ProjectDispatchService', lambda repository: service)
        recover_persisted_dispatch(client=factory())
        assert len(continuations) == 1
        task = continuations[0]
        assert task['_principalContext']['kind'] == 'system'
        assert recover_coordinator(repo, service=service) == []
        page = json.loads(execute_coordinator_tool(task, 'get_project_workers', {}))
        assert page['workers'][0]['assignment_id'] == first['assignment_id'], page
        assert page['request_allowance']['id'] == source_budget['id']
        assert page['request_allowance']['consumption']['observedTokens'] >= 20
        result = _execute_tool_one(task, {}, 'get_project_workers', 'native-control-read', {}, 0,
            {'name': 'get_project_workers', 'arguments': {}, 'query': 'Follow Worker results'},
            task['config'], str(root), True, task['_testCoordinatorSchemas'])
        assert first['assignment_id'] in str(result[1]) and 'Error:' not in str(result[1]), result
    reviewed = json.loads(execute_coordinator_tool(task, 'review_project_work', {
        'assignment_id': first['assignment_id'], 'result_revision': view['execution']['resultRevision'],
        'decision': 'accept', 'note': 'Worker file bytes and command verification confirmed.',
        'checkpoint_sha': view['delivery']['checkpointSha']}))
    assert reviewed.get('review', {}).get('actor') == 'coordinator', reviewed
    assert reviewed['canonicalDelivery'] is False
    delivery.resume_pending_workspace_deliveries()
    delivered = json.loads(execute_coordinator_tool(task, 'await_project_workers', {
        'assignment_ids': [first['assignment_id']], 'wait_for': 'delivery', 'timeout_seconds': 0}))
    assert delivered['wait'] == 'settled' and delivered['workers'][0]['canonical_delivery'], delivered
    assert (root / 'result.txt').read_text() == 'native independent worker result\n'
    report = json.loads(execute_coordinator_tool(task, 'report_to_user', {
        'title': 'Implementation delivered', 'message': 'Worker verification passed and canonical delivery is confirmed.',
        'kind': 'delivery', 'idempotency_key': 'verified'}))
    assert report.get('turnId'), report
    from lib.conversations.coordinator_budget import CoordinatorBudgetSettings, read_state
    defaults = CoordinatorBudgetSettings(owner, str(root))
    defaults.update({'path': str(root), 'conversationId': conv, 'bindingRevision': binding['revision'],
        'expectedRevision': 0, 'settings': {**defaults.read()['settings'], 'maxTokens': 1234}})
    engine.crash_cycle()
    persisted = read_state(owner, str(root))
    assert persisted['settings']['maxTokens'] == 1234
    assert persisted['request']['policy'] == source_budget['policy']
    assert persisted['request']['consumption']['observedTokens'] >= 20
    assert CoordinatorReports(owner, str(root)).read()['unread'] is True
    assert json.loads(execute_coordinator_tool(task, 'delegate_project_work', arguments)) == first
    assert len(sends) == 1 and not workspaces[0].exists()
    assert (root / 'result.txt').read_text() == 'native independent worker result\n'
    if continue_after_restart:
        receipt = json.loads(execute_coordinator_tool(task, 'finish_project_request', {
            'report_turn_id': report['turnId'], 'outcome': 'complete'}))
        assert receipt.get('reason') == 'complete', receipt
        engine.crash_cycle()
        assert recover_coordinator(repo, service=service) == []
        assert len(continuations) == 1
