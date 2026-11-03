"""Project-dispatch admission at the executors, independent of catalog filtering.

The serial executor and the streaming prefetch executor are the last line of
defence: every dispatch mode (consult, files, project, coordinator) is admitted
by the single ``project_dispatch.scope.admit_tool`` boundary before any soft
policy, custom handler or effect, with the catalog projection removed.
One admission validates its grant once and decides from that validated row; the
decision table per authority fault is the one a second validation gave.
"""
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.llm.llm_errors import AbortedError, ToolAdmissionRefused
from lib.project_dispatch import admission
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
    task['_workspaceDelivery'] = {'repositories': [{
        'canonicalRoot': str(Path(canonical).resolve()), 'workspacePath': str(workspace),
        'workId': task['id']}]}
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
