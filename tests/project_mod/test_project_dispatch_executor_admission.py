"""Project-dispatch admission at the executors, independent of catalog filtering.

The serial executor and the streaming prefetch executor are the last line of
defence: every dispatch mode (consult, files, project, coordinator) is admitted
by the single ``project_dispatch.scope.admit_tool`` boundary before any soft
policy, custom handler or effect, with the catalog projection removed.
One admission validates its grant once and decides from that validated row; the
decision table per authority fault is the one a second validation gave.
"""
from collections import Counter
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.llm.llm_errors import AbortedError, ToolAdmissionRefused
from lib.project_dispatch import admission
from lib.project_dispatch.delegation import capture_worker_policy
from tests.project_mod.test_project_dispatch_runtime import runtime as consult_runtime
from tests.project_mod.test_coordinator_continuation import (
    activate, base_runtime, context, ready, run, runtime,
)
from tests.project_mod.test_coordinator_workers import _worker_task

# Keep the imported fixtures' registered aliases visible to static tooling.
__all__ = ('base_runtime', 'consult_runtime', 'context', 'run', 'runtime')

pytestmark = pytest.mark.unit

CATALOG_KEYS = ('_executable_tool_catalog', '_executableToolCatalog', '_tool_schema',
                '_toolContractDocumentsByName', '_executableToolNamespaceByName',
                '_toolNamespaceByName', '_requiredDirectToolNames', '_toolInitialVisibleNames',
                '_tool_gateway_names', '_frontendSelectedToolNames')
ARGS = {'read_files': {'path': 'src/a.txt'}, 'run_command': {'command': 'echo leaked'},
        'timer_create': {'continuation_message': 'leaked'}, 'get_project_workers': {}}


@pytest.fixture(params=('consult', 'files', 'project', 'coordinator'))
def dispatch(request, tmp_path, monkeypatch):
    """One live, account-bound dispatch task per mode, with no catalog projection."""
    mode = request.param
    project_path = None
    if mode in ('consult', 'files'):
        c = request.getfixturevalue('context')
        if mode == 'files':
            root = (tmp_path / 'owned').resolve()
            (root / 'src').mkdir(parents=True)
            (root / 'src' / 'a.txt').write_text('alpha\n')
            c.request['profile'] = {'mode': 'files', 'tools': ['read_files'], 'readPaths': ['src'],
                                    'writePaths': [], 'maxToolCalls': 4}
            # The registered isolated worktree has its own owner; its row is the port here.
            monkeypatch.setattr(admission, 'owned_workspace',
                                lambda task, binding=None: {'workspace_path': str(root)})
            project_path = str(root)
        task = request.getfixturevalue('consult_runtime').task()
        admission.bind_account(task)
        # Consultation grants nothing; any tool name stands for "the granted one".
        granted, ungranted = ('read_files' if mode == 'files' else 'run_command'), 'run_command'
    elif mode == 'project':
        task, _row = _worker_task(request.getfixturevalue('runtime'))
        granted, ungranted = 'read_files', 'timer_create'
    else:
        r = request.getfixturevalue('run')
        ready(r)
        activate(r)
        task = r.task
        granted, ungranted = 'get_project_workers', 'run_command'
    carrier = task['config'][admission.KEY]
    assigned = admission.repository_for(carrier).read(carrier['assignmentId'])
    assert admission.profile_for(assigned)['mode'] == mode
    for key in CATALOG_KEYS:
        task.pop(key, None)
        task['config'].pop(key, None)
    return SimpleNamespace(mode=mode, task=task, granted=granted, ungranted=ungranted,
                           project_path=project_path)


def _forbid_handler_dispatch(monkeypatch):
    from lib.tasks_pkg.executor import _execute
    monkeypatch.setattr(_execute, '_dispatch_admitted_tool_one',
                        lambda *a, **kw: pytest.fail('dispatch task reached handler resolution'))


def _execute(dispatch, name):
    from lib.tasks_pkg.executor._execute import _execute_tool_one
    return _execute_tool_one(dispatch.task, {}, name, 'tc', dict(ARGS[name]), 0, {}, {},
                             dispatch.project_path, False)


def test_executor_refuses_ungranted_tool_without_any_catalog(dispatch, monkeypatch):
    _forbid_handler_dispatch(monkeypatch)
    with pytest.raises(ToolAdmissionRefused) as refused:
        _execute(dispatch, dispatch.ungranted)
    assert refused.value.tool_name == dispatch.ungranted


def test_custom_tool_environment_cannot_shadow_a_dispatch_grant(dispatch, monkeypatch):
    class ShadowEnvironment:
        def resolve(self, name):
            pytest.fail('custom handler resolved for a project-dispatch task')
    dispatch.task['_tool_env'] = ShadowEnvironment()
    _forbid_handler_dispatch(monkeypatch)
    with pytest.raises(ToolAdmissionRefused) as refused:
        _execute(dispatch, dispatch.granted)
    assert refused.value.tool_name == dispatch.granted


def test_removed_grant_carrier_is_a_typed_refusal_not_a_model_visible_denial(dispatch, monkeypatch):
    dispatch.task['config'].pop(admission.KEY)
    assert admission.is_dispatch_task(dispatch.task)
    _forbid_handler_dispatch(monkeypatch)
    with pytest.raises(ToolAdmissionRefused, match='carrier') as refused:
        _execute(dispatch, dispatch.granted)
    assert refused.value.tool_name == dispatch.granted


def test_cooperative_stop_keeps_cancellation_semantics(dispatch, monkeypatch):
    dispatch.task['aborted'] = True
    _forbid_handler_dispatch(monkeypatch)
    with pytest.raises(AbortedError) as stopped:
        _execute(dispatch, dispatch.granted)
    # A Stop that races admission settles as interrupted, never as a failed refusal.
    assert not isinstance(stopped.value, ToolAdmissionRefused)


def test_streaming_prefetch_never_runs_an_unadmitted_tool(dispatch, monkeypatch):
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    fetched = []

    def fetch(url, *args, **kwargs):
        fetched.append(url)
        return {'url': url, 'page_content': 'leaked', 'is_pdf': False,
                'raw_chars': 6, 'filtered_chars': 6, 'error_msg': None}
    monkeypatch.setattr('lib.tasks_pkg.handlers.search._core._fetch_url_one', fetch)
    accumulator = StreamingToolAccumulator(dispatch.task, project_path=dispatch.project_path)
    try:
        accumulator.on_tool_call_ready({'id': 'call-1', 'type': 'function', 'function': {
            'name': 'fetch_url', 'arguments': '{"url": "https://example.invalid/leak"}'}})
    finally:
        accumulator.close(cancel_futures=False, wait=True)
    assert fetched == []


def _registered_workspace(dispatch, monkeypatch):
    """A Worker's registered isolated workspace row is its own port here, like the file grant's."""
    if dispatch.mode == 'project':
        root = dispatch.task['config']['projectPath']
        monkeypatch.setattr(admission, 'owned_workspace', lambda task, binding=None: {'workspace_path': root})


def _admit(dispatch, name):
    from lib.project_dispatch.scope import admit_tool
    try:
        admit_tool(dispatch.task, name, dict(ARGS[name]), project_path=dispatch.project_path)
    except ToolAdmissionRefused:
        outcome = 'refused'
    except AbortedError:
        outcome = 'cancelled'
    else:
        outcome = 'admitted'
    return outcome, dispatch.task.get('_abort_reason')


def _inject(fault, dispatch, monkeypatch):
    """Apply one durable authority or task-identity change; return the tool to call."""
    task = dispatch.task
    carrier = task['config'][admission.KEY]
    repository = admission.repository_for(carrier)
    row = repository.read(carrier['assignmentId'])
    if fault == 'revoked':
        repository.revoke(row['id'], row['grant']['revision'])
    elif fault == 'durable_stop':
        from lib.project_dispatch.repository import record_stop
        record_stop(repository.owner, row['request']['recipientId'], row['grant']['createdAt'],
                    client=repository.client)
    elif fault == 'rebound':
        repository.binding_repository.value['revision'] += 1
    elif fault == 'source_changed':
        source, read_turn = row['request']['authorizationSource'], repository.turn_get

        def changed_source(conversation, turn_id, **kwargs):
            turn = read_turn(conversation, turn_id, **kwargs)
            if turn_id == source['turnId']:
                turn = {**turn, 'projectionRevision': turn['projectionRevision'] + 1}
            return turn
        monkeypatch.setattr(repository, 'turn_get', changed_source)
    elif fault == 'executor_settled':
        read_attempt = repository.attempt_get

        def settled(attempt_id, **kwargs):
            attempt = read_attempt(attempt_id, **kwargs)
            return {**attempt, 'status': 'completed'} if attempt_id == task['_attemptId'] else attempt
        monkeypatch.setattr(repository, 'attempt_get', settled)
    elif fault == 'execution_changed':
        task['_attemptId'] = 'another-attempt'
    elif fault == 'carrier_removed':
        task['config'].pop(admission.KEY)
    elif fault == 'cooperative_stop':
        task['aborted'] = True
    return dispatch.ungranted if fault == 'ungranted' else dispatch.granted


# (outcome, stop cause) of one admission per fault, in every mode.
DECISIONS = {
    'ungranted': ('refused', None),
    'revoked': ('refused', 'dispatch_grant_revoked'),
    'durable_stop': ('refused', 'dispatch_grant_revoked'),
    'rebound': ('refused', 'dispatch_authority_lost'),
    'source_changed': ('refused', 'dispatch_authority_lost'),
    'executor_settled': ('refused', 'dispatch_authority_lost'),
    'execution_changed': ('refused', 'dispatch_authority_lost'),
    'carrier_removed': ('refused', 'dispatch_authority_lost'),
    'cooperative_stop': ('cancelled', None),
}


@pytest.mark.parametrize('fault', ['live', *DECISIONS])
def test_admission_decision_per_authority_fault(dispatch, monkeypatch, fault):
    """Each fault keeps the decision the former validate-twice-and-reread admission made."""
    _registered_workspace(dispatch, monkeypatch)
    name = _inject(fault, dispatch, monkeypatch)
    # A consultation grants no tool, so even a live grant refuses without stopping the task.
    live = ('refused' if dispatch.mode == 'consult' else 'admitted', None)
    assert _admit(dispatch, name) == DECISIONS.get(fault, live)


def _port_reads(repository, monkeypatch):
    """Count live validations, row reloads and every evidence port they read."""
    counts = Counter()

    def counted(name, real):
        def call(*args, **kwargs):
            counts[name] += 1
            return real(*args, **kwargs)
        return call
    for name in ('validate', 'read', 'turn_get', 'attempt_get'):
        monkeypatch.setattr(repository, name, counted(name, getattr(repository, name)))
    monkeypatch.setattr(repository.binding_repository, 'read',
                        counted('binding', repository.binding_repository.read))
    monkeypatch.setattr(repository.conversations, 'get',
                        counted('conversation', repository.conversations.get))
    # The injected record client returns itself for reads and writes.
    monkeypatch.setattr(repository.client, 'query', counted('storage', repository.client.query))
    return counts


@pytest.mark.parametrize('decide', ['tool_admission', 'file_handler_selection'])
def test_one_decision_reads_one_validation_and_its_executor(dispatch, monkeypatch, decide):
    """One validation decides; its row is exactly what a second validation or reread returns."""
    _registered_workspace(dispatch, monkeypatch)
    carrier = dispatch.task['config'][admission.KEY]
    repository = admission.repository_for(carrier)
    counts = _port_reads(repository, monkeypatch)
    repository.validate(carrier)
    validation = +counts
    counts.clear()
    decided = []
    monkeypatch.setattr(repository, 'validate', lambda binding, _real=repository.validate:
                        decided.append(_real(binding)) or decided[-1])
    if decide == 'tool_admission':
        live = ('refused' if dispatch.mode == 'consult' else 'admitted', None)
        assert _admit(dispatch, dispatch.granted) == live
    else:
        assert admission.is_file_dispatch_task(dispatch.task) is (dispatch.mode == 'files')
    # The bound physical executor is the only read beyond that single validation.
    assert counts == validation + Counter(attempt_get=1)
    assert len(decided) == 1
    assert repository.validate(carrier) == decided[0] == repository.read(carrier['assignmentId'])


def test_bound_worker_root_loop_runs_tools_in_its_registered_workspace(runtime, monkeypatch, tmp_path):
    """A bound Worker executes in its isolated checkout beside shared-checkout runtime roots.

    Binding moves ``projectPath`` to the registered checkout and keeps
    ``canonicalProjectPath`` as the project's identity; runtime roots such as
    ``.eval-runs`` stay in the shared checkout. Every tool call is admitted against
    the path the root loop resolves, so a relative read and a command without a
    working directory must both run in the Worker's own checkout.
    """
    from lib.project_mod import tools
    from lib.project_mod.config import _make_root_state, build_root_policy
    from lib.tasks_pkg.model_config import _resolve_model_config

    task, _row = _worker_task(runtime)
    config = task['config']
    canonical = config['canonicalProjectPath']
    workspace = (tmp_path / 'worker-checkout').resolve()
    workspace.mkdir()
    (workspace / 'notes.txt').write_text('worker copy\n')
    shared = build_root_policy(canonical)
    policy = {'primaryPath': str(workspace), 'roots': {
        label: {**state, 'path': str(workspace)} for label, state in shared['roots'].items()}}
    runtime_roots = [str(Path(canonical) / name) for name in ('.eval-runs', 'abtest_workdir')]
    for root in runtime_roots:
        policy['roots'][Path(root).name] = _make_root_state(root)
    config.update(projectPath=str(workspace), canonicalProjectPaths=[],
                  workspaceRuntimeRoots=runtime_roots)
    task['_workspaceRootPolicy'] = policy
    from lib.tasks_pkg.workspace_delivery import workspace_work_id
    task['_workspaceDelivery'] = {'repositories': [{
        'canonicalRoot': str(Path(canonical).resolve()), 'workspacePath': str(workspace),
        'workId': workspace_work_id(task)}]}
    monkeypatch.setattr('lib.integration_state_repository.find_workspace',
                        lambda root, task_id, user_id: {
                            'user_id': user_id, 'task_id': task_id, 'project_root': root,
                            'state': 'active', 'workspace_path': str(workspace)})

    project_path = _resolve_model_config(config, task['id'])['project_path']

    call = {'conv_id': task['convId'], 'task_id': task['id'], 'task': task}
    assert 'worker copy' in tools.execute_tool('read_files', {'path': 'notes.txt'}, project_path, **call)
    assert str(workspace) in tools.execute_tool('run_command', {'command': 'pwd'}, project_path, **call)
    assert project_path == str(workspace)
    # The shared checkout stays outside the Worker's authority, and the refusal names both roots.
    with pytest.raises(ToolAdmissionRefused) as refused:
        tools.execute_tool('read_files', {'path': 'notes.txt'}, canonical, **call)
    assert repr(canonical) in str(refused.value) and repr(str(workspace)) in str(refused.value)


def _worker_swarm(runtime, monkeypatch, tmp_path, catalog=None, environment=None):
    """A bound Worker, its registered checkout and the swarm a ``spawn_agents`` call starts."""
    from lib.swarm.master import MasterOrchestrator
    from lib.swarm.protocol import SubTaskSpec
    from lib.tools.project import READ_FILES_TOOL
    if catalog is not None:
        capture_worker_policy(runtime.task, runtime.task['config'],
                              [{'function': {'name': name}} for name in catalog], 'model-a',
                              worker_model='model-a')
    worker, row = _worker_task(runtime)
    worker['_tenant_id'] = admission.principal_for(worker['config'][admission.KEY]).tenant_id
    if environment is not None:
        worker['_tool_env'] = environment
    workspace = (tmp_path / 'worker-checkout').resolve()
    workspace.mkdir()
    (workspace / 'notes.txt').write_text('worker copy\n')
    worker['config']['projectPath'] = str(workspace)
    monkeypatch.setattr(admission, 'owned_workspace',
                        lambda task, binding=None: {'workspace_path': str(workspace)})
    spec = SubTaskSpec(role='general', objective='Inspect notes.txt and share the finding')
    # A storage-free run keeps swarm checkpoints out of this fixture's record store.
    master = MasterOrchestrator(worker['id'], worker['convId'], [spec], user_id=7,
                                project_path=str(workspace), model='model-a', all_tools=[READ_FILES_TOOL],
                                parent_config={**deepcopy(worker['config']), '_transientRuntime': True},
                                parent_task=worker)
    return SimpleNamespace(worker=worker, row=row, workspace=str(workspace), master=master,
                           agent=master._make_agent(spec))


def _scripted_child_rounds(monkeypatch, calls):
    """The child invocation's real pipeline with only context hooks and transport scripted."""
    import json
    import lib.tasks_pkg.manager._stream as stream
    import lib.tasks_pkg.orchestrator._ports as ports
    import lib.tasks_pkg.orchestrator._root_agent_loop as loop
    import lib.tasks_pkg.orchestrator._run as runner
    from lib.llm.stream_result import ProviderStreamResult
    for name in ('start_prefetches', 'stop_prefetches', 'maybe_run_memory_prefetch',
                 'inject_context_and_emit_chips', 'setup_project_context'):
        monkeypatch.setattr(runner, name, lambda *a, **kw: None)
    monkeypatch.setattr(runner, 'run_turn_prelude', lambda task, cfg, tid: cfg)
    monkeypatch.setattr(runner, 'check_autopilot_kick', lambda task: False)
    monkeypatch.setattr(loop, 'run_round_message_hygiene', lambda *a, **kw: None)
    monkeypatch.setattr(loop, 'drain_and_inject_inbox', lambda **kw: None)
    monkeypatch.setattr(ports, 'build_request_body', lambda model, messages, **kw: {
        'model': model, 'messages': deepcopy(messages), 'tools': deepcopy(kw['tools'])})
    # The Worker's human Turn configures no requirements.
    monkeypatch.setattr('lib.acceptance.turns.read_execution_requirements', lambda *a, **kw: None)
    requests = []

    def provider(body, **kwargs):
        requests.append(deepcopy(body))
        assert len(requests) <= 2, 'unexpected extra provider dispatch'
        message = {'role': 'assistant', 'content': 'Shared the finding.'}
        if len(requests) == 1:
            message = {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': f'child-{name}', 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps(args)}}
                for name, args in calls]}
        return ProviderStreamResult.from_legacy(
            message, 'tool_calls' if len(requests) == 1 else 'stop',
            {'prompt_tokens': 20, 'completion_tokens': 3, 'total_tokens': 23})
    monkeypatch.setattr(stream, 'dispatch_stream', provider)
    return requests


CHILD_CALLS = (('read_files', {'path': 'notes.txt'}),
               ('store_artifact', {'key': 'finding', 'content': 'notes.txt says: worker copy'}))


def test_worker_swarm_child_runs_ordinary_and_artifact_tools(runtime, monkeypatch, tmp_path):
    """A Worker's ``spawn_agents`` child runs its tools under the Worker's live grant.

    The child holds no grant of its own (``share_dispatch_root``). Its artifact
    port serves only its run's in-process store, so it is not a custom tool
    environment, and the artifact tools ride the Worker's swarm capability.
    The first child call used to fail the child with "cannot be shadowed by a
    custom tool environment".
    """
    swarm = _worker_swarm(runtime, monkeypatch, tmp_path)
    requests = _scripted_child_rounds(monkeypatch, CHILD_CALLS)
    result = swarm.agent.run()
    assert result.status == 'completed', result.error_message
    assert result.tool_calls_made == 2
    assert swarm.master.artifact_store.get('finding') == 'notes.txt says: worker copy'
    replies = {message.get('tool_call_id'): message.get('content') for message in requests[1]['messages']
               if message.get('role') == 'tool'}
    assert 'worker copy' in str(replies['child-read_files'])
    assert 'Stored artifact' in str(replies['child-store_artifact'])
    assert not swarm.worker.get('aborted')
    # The Worker's durable tool ledger settles the child's calls under its own grant.
    from lib.project_dispatch.repository import CONTRACT
    attempts = admission.repository_for(swarm.worker['config'][admission.KEY]).read(swarm.row['id'])['toolAttempts']
    assert sorted(row['tool_name'] for row in attempts) == ['read_files', 'store_artifact']
    assert all(row['phase'] in CONTRACT['terminalToolPhases'] and row['task_id'] != swarm.worker['id']
               for row in attempts)


class _CustomEnvironment:
    def __init__(self):
        self.resolved = []

    def resolve(self, name):
        self.resolved.append(name)
        return lambda *args: ('shadow', 'custom handler ran', False)


# (Worker catalog override, Worker holds a custom environment, decision per child call)
SWARM_CHILD_DECISIONS = {
    'worker swarm': (None, False, {'read_files': 'admitted', 'store_artifact': 'admitted'}),
    # Artifact tools come with the swarm capability, never on their own.
    'no swarm capability': (('run_command', 'read_files', 'write_file'), False,
                            {'read_files': 'admitted', 'store_artifact': 'capability ceiling'}),
    # Tools the Worker's own task-local environment would resolve stay refused.
    'inherited custom environment': (None, True,
                                     {'read_files': 'cannot be shadowed', 'store_artifact': 'cannot be shadowed'}),
}


@pytest.mark.parametrize('case', SWARM_CHILD_DECISIONS)
def test_swarm_child_admission_follows_the_workers_grant(runtime, monkeypatch, tmp_path, case):
    """The child the swarm actually builds is admitted exactly as its Worker's grant allows."""
    from lib.project_dispatch.scope import admit_tool
    from lib.tasks_pkg.child_invocation import close_unstarted_child_invocation
    catalog, inherits, expected = SWARM_CHILD_DECISIONS[case]
    environment = _CustomEnvironment() if inherits else None
    swarm = _worker_swarm(runtime, monkeypatch, tmp_path, catalog=catalog, environment=environment)
    decisions = {}

    def admit_unstarted(child, **kwargs):
        try:
            for name, args in CHILD_CALLS:
                try:
                    admit_tool(child, name, dict(args), project_path=child['config']['projectPath'])
                    decisions[name] = 'admitted'
                except ToolAdmissionRefused as refused:
                    assert refused.tool_name == name
                    decisions[name] = next(phrase for phrase in ('capability ceiling', 'cannot be shadowed')
                                           if phrase in str(refused))
        finally:
            close_unstarted_child_invocation(child, reason='Admission spec complete')
    monkeypatch.setattr('lib.tasks_pkg.child_invocation.run_child_invocation', admit_unstarted)
    swarm.agent.run()
    assert decisions == expected
    assert not swarm.worker.get('aborted')
    if environment is not None:
        assert environment.resolved == []
