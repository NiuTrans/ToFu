"""Production task scanner resolution, independent resource ownership and SDK close."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import threading

import pytest
from test_native_extraction_stream import stream as stream, observe_children, assert_joined
from test_native_extraction_owned import blocked_sources, observe_first_ast

pytestmark = pytest.mark.unit
OWNER = 900001


def runtime_for(scanner):
    from lib.repo_map.native_runtime import NativeRepositoryRuntime
    return NativeRepositoryRuntime(scanner.grammar_directory, scanner.limits)


def task_for(root, case='python_extraction'):
    from native_domain_worker import Workload
    work = Workload(root, case); work.start()
    return work


def observe_installed_images(monkeypatch):
    import tofu_repo_kernel
    original = tofu_repo_kernel.open_stream_process
    images = []
    def open_image():
        value = original(); images.append(value); return value
    monkeypatch.setattr(tofu_repo_kernel, 'open_stream_process', open_image)
    return images


def test_registered_task_scope_coexists_with_custom_environment(stream, monkeypatch):
    from lib.repo_map.native_runtime import scanner_for_task
    from lib.repo_map import outline
    from lib.agent_core.execution_session import ExecutionPhase
    scanner, root = stream
    work = task_for(root); runtime = runtime_for(scanner)
    images = observe_installed_images(monkeypatch)
    work.task['_repository_runtime'] = runtime
    task = work.task
    try:
        port = scanner_for_task(task, root)
        assert runtime.retained_image_bytes() == 0 and not images
        assert scanner_for_task(task, root) is port
        assert asdict(port(root)) == asdict(outline.scan(str(root)))
        assert len(images) == 1 and runtime.retained_image_bytes() > 0
        work.stop()
        receipt = task['_executionSession'].settle(ExecutionPhase.COMPLETED)
        assert receipt.invariants_satisfied
        assert {'tool_environment', 'repository_scanner'} <= {name for name,_ in receipt.resource_dispositions}
        assert '_repository_scanner_binding' not in task
        with pytest.raises(RuntimeError, match='closed'): scanner_for_task(task, root)
        assert not images[0]._image.closed
    finally:
        work.close(); runtime.close()
    assert images[0]._image.closed and runtime.retained_image_bytes() == 0


def test_task_root_owner_and_execution_session_are_all_fenced(stream):
    from lib.repo_map.native_runtime import scanner_for_task
    scanner, root = stream
    work, other = task_for(root), task_for(root)
    runtime = runtime_for(scanner)
    task = work.task; task['_repository_runtime'] = runtime
    try:
        port = scanner_for_task(task, root)
        with pytest.raises(PermissionError): scanner_for_task(task, root.parent)
        with pytest.raises(PermissionError): port(root.parent)
        session = task['_executionSession']; task['_executionSession'] = other.task['_executionSession']
        try:
            with pytest.raises(PermissionError): scanner_for_task(task, root)
        finally:task['_executionSession'] = session
        owner = task['_userId']; task['_userId'] = owner+1
        try:
            with pytest.raises(ValueError, match='principal owner'): scanner_for_task(task, root)
        finally:task['_userId'] = owner
    finally:
        work.close(); other.close(); runtime.close()


def test_concurrent_first_binding_registers_one_task_resource(stream, monkeypatch):
    from lib.repo_map.native_runtime import scanner_for_task
    scanner, root = stream
    work = task_for(root); runtime = runtime_for(scanner)
    task = work.task; task['_repository_runtime'] = runtime
    images = observe_installed_images(monkeypatch)
    children = observe_children(monkeypatch)
    barrier = threading.Barrier(8)
    def run(_):
        barrier.wait(timeout=5)
        port = scanner_for_task(task, root)
        return id(port), asdict(port(root))
    try:
        with ThreadPoolExecutor(max_workers=8) as pool: rows = list(pool.map(run, range(8)))
        assert len({row[0] for row in rows}) == 1 and all(row[1] == rows[0][1] for row in rows)
        assert len(images) == 1
    finally:
        work.close(); runtime.close()
    assert_joined(children)


def test_failed_registration_releases_the_staged_binding(stream, monkeypatch):
    from lib.repo_map.native_runtime import scanner_for_task
    scanner, root = stream
    work = task_for(root); runtime = runtime_for(scanner)
    task = work.task; task['_repository_runtime'] = runtime
    primary = LookupError('registration refused')
    def refuse(*args, **kwargs):raise primary
    monkeypatch.setattr(task['_executionSession'], 'hold_resource', refuse)
    try:
        with pytest.raises(LookupError) as caught:scanner_for_task(task, root)
        assert caught.value is primary
        assert '_repository_scanner_binding' not in task and task['_repository_scanner_closed']
        assert runtime.retained_image_bytes() == 0
    finally:
        work.close(); runtime.close()


def test_real_handler_uses_task_selection_and_retains_error_publication_on_abort(stream, monkeypatch):
    from lib.tasks_pkg.handlers import project
    from lib.tasks_pkg.manager.cancellation import cancel_task
    scanner, root = stream
    work = task_for(root); runtime = runtime_for(scanner)
    task = work.task; task['_repository_runtime'] = runtime
    blocked_sources(root)
    entered = observe_first_ast(monkeypatch); children = observe_children(monkeypatch)
    result = []; entry = {'query':'cancel repository scan','toolName':'repo_map','toolCallId':'application-abort'}
    def run():
        result.append(project._handle_project_tool(task,{},'repo_map','application-abort',{},1,entry,{},str(root),True))
    thread = threading.Thread(target=run); thread.start()
    try:
        assert entered.wait(timeout=5)
        assert not cancel_task(task['id'], user_id=OWNER+1).get('signalled')
        assert cancel_task(task['id'], user_id=OWNER).get('signalled')
        thread.join(timeout=5)
        assert not thread.is_alive() and result and result[0][2]
        assert entry['status'] == 'error'
    finally:
        work.close(True); runtime.close(); thread.join(timeout=5)
    assert_joined(children)


@pytest.mark.parametrize('case', ['python_extraction','multilingual_extraction','ranking','incremental_edit','cold_scan'])
def test_public_handler_selects_native_task_owner_without_benchmark_port(stream, case):
    from native_domain_worker import Workload
    from lib.tasks_pkg.handlers import project
    scanner, root = stream
    reference = Workload(root, case); candidate = Workload(root, case)
    runtime = runtime_for(scanner)
    start = candidate.start
    def start_native():
        start(); candidate.task['_repository_runtime'] = runtime
        class ProductionPort:
            def invoke(self, task, arguments, finalize):
                entry = {'query':'repository publication benchmark','toolName':'repo_map','toolCallId':'repository-port-call-1'}
                value = project._handle_project_tool(task,{},'repo_map','repository-port-call-1',arguments,1,entry,{},str(root),True)
                finalize(task,1,entry,entry['results'],status=entry['status'])
                return ('repository-port-call-1', value[1], value[2])
        candidate.port = ProductionPort()
    candidate.start = start_native
    try:
        assert candidate.operation(0) == reference.operation(0)
        candidate.stop(); reference.stop()
        assert candidate.operation(1) == reference.operation(1)
        assert runtime.retained_image_bytes() > 0
    finally:
        candidate.close(); reference.close(); runtime.close()
    assert runtime.retained_image_bytes() == 0


def test_terminal_racing_first_binding_rolls_back_without_image(stream, monkeypatch):
    from lib.repo_map.native_runtime import scanner_for_task
    from lib.agent_core.execution_session import ExecutionPhase
    scanner, root = stream
    work = task_for(root); runtime = runtime_for(scanner)
    task = work.task; task['_repository_runtime'] = runtime
    session = task['_executionSession']
    original = session.hold_resource
    entered, resume = threading.Event(), threading.Event()
    errors = []
    def hold(*args, **kwargs):
        entered.set()
        assert resume.wait(timeout=5)
        return original(*args, **kwargs)
    monkeypatch.setattr(session, 'hold_resource', hold)
    def bind():
        try: scanner_for_task(task, root)
        except BaseException as error: errors.append(error)
    worker = threading.Thread(target=bind); worker.start()
    try:
        assert entered.wait(timeout=5)
        session.settle(ExecutionPhase.COMPLETED)
        resume.set(); worker.join(timeout=5)
        assert not worker.is_alive() and len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert task['_repository_scanner_closed'] and '_repository_scanner_binding' not in task
        assert runtime.retained_image_bytes() == 0
    finally:
        resume.set(); worker.join(timeout=5)
        work.close(); runtime.close()


def test_selected_artifact_failure_is_published_without_reference_fallback(stream, monkeypatch):
    import tofu_repo_kernel
    from lib.repo_map import outline
    from lib.tasks_pkg.handlers import project
    scanner, root = stream
    work = task_for(root); runtime = runtime_for(scanner)
    task = work.task; task['_repository_runtime'] = runtime
    def refuse(): raise RuntimeError('installed stream admission refused')
    def forbidden(*args, **kwargs): pytest.fail('selected native failure fell back to reference')
    monkeypatch.setattr(tofu_repo_kernel, 'open_stream_process', refuse)
    monkeypatch.setattr(outline, 'scan', forbidden)
    entry = {'query':'map project','toolName':'repo_map','toolCallId':'admission-refusal'}
    try:
        result = project._handle_project_tool(task,{},'repo_map','admission-refusal',{},1,entry,{},str(root),True)
        assert result[2] and entry['status'] == 'error'
        assert 'installed stream admission refused' in result[1]
        assert runtime.retained_image_bytes() == 0
    finally:
        work.close(); runtime.close()


def test_missing_grammar_preserves_other_files_references_and_publication(stream, monkeypatch, tmp_path):
    from lib.repo_map import outline
    from lib.repo_map.native_runtime import NativeRepositoryRuntime, scanner_for_task
    from lib.tasks_pkg.handlers import project
    import grep_ast.tsl
    scanner, root = stream
    grammars = tmp_path / 'empty-grammars'; grammars.mkdir()
    work = task_for(root)
    (root / 'missing.js').write_text('function missingGrammar() { return symbol_0(); }\n')
    runtime = NativeRepositoryRuntime(grammars, scanner.limits)
    task = work.task; task['_repository_runtime'] = runtime
    def missing(_language):
        raise LookupError('matching missing reference grammar')
    monkeypatch.setattr(grep_ast.tsl, 'get_parser', missing)
    try:
        reference = outline.scan(str(root))
        assert 'missing.js' in reference.files and 'missing.js' not in reference.tags
        assert 'missing.js' in reference.references['symbol_0']
        children = observe_children(monkeypatch)
        port = scanner_for_task(task, root)
        assert asdict(port(root)) == asdict(reference)
        def forbidden(*args, **kwargs):
            pytest.fail('file-local native grammar error invoked reference scanner')
        monkeypatch.setattr(outline, 'scan', forbidden)
        entry = {'query':'map project','toolName':'repo_map','toolCallId':'missing-grammar'}
        result = project._handle_project_tool(task,{},'repo_map','missing-grammar',{},1,entry,{},str(root),True)
        assert not result[2] and entry['status'] == 'done'
        assert port.scope.discovery_calls == 2
    finally:
        work.close(); runtime.close()
    assert runtime.retained_image_bytes() == 0
    assert_joined(children)


def test_owned_measurement_default_still_refuses_missing_grammar(stream, tmp_path, monkeypatch):
    from lib.repo_map._native_owned import OwnedStreamScope
    scanner, root = stream
    (root / 'missing.js').write_text('function missingGrammar() {}\n')
    grammars = tmp_path / 'empty-grammars'; grammars.mkdir()
    children = observe_children(monkeypatch)
    with OwnedStreamScope(root, OWNER, scanner.binary, grammars,
                          expected_sha256=scanner.expected_sha256, limits=scanner.limits) as scope:
        with pytest.raises(RuntimeError, match='grammar_unavailable'):
            scope.scan(OWNER)
    assert_joined(children)
