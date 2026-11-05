"""Report publication shares the task runtime's cancellation/terminal claim."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from tests.paper.test_paper_report_body_projection import FINAL_BODY, report_case as report_case


@pytest.mark.unit
def test_spawned_report_remains_observable_through_postpasses(report_case, monkeypatch):
    run, task, worker = report_case
    runtime = worker._report_runtime
    observations = []

    def insight(*args, **kwargs):
        page = runtime.poll(task['task_id'])
        observations.append((page['status'], page.get('workerActive')))
        assert runtime.cleanup_stale(max_age=0) == 0
        runtime.append_event(task['task_id'], {'type': 'insight', 'insight': 'late insight'})

    monkeypatch.setattr(worker, '_maybe_run_insight', insight)

    def dispatch(messages, *, on_content, **kwargs):
        on_content(FINAL_BODY)
        return ({'role': 'assistant', 'content': FINAL_BODY, 'tool_calls': []}, 'stop', {})

    with ThreadPoolExecutor() as pool:
        runtime.submit_worker(
            task['task_id'], lambda _id, _owner, fn: pool.submit(fn),
            run, dispatch,
        ).result(timeout=5)
    assert observations == [('done', True)]
    page = runtime.poll(task['task_id'])
    assert page['workerActive'] is False
    assert page['status'] == 'done' and page['result'] == FINAL_BODY
    done = next(event for event in page['events'] if event['type'] == 'done')
    insight = next(event for event in page['events'] if event['type'] == 'insight')
    assert done['workerActive'] is True
    assert done['seq'] < insight['seq']


@pytest.fixture
def publication_case(monkeypatch, report_case):
    from lib.paper.report_engine import _hooks

    run, task, worker = report_case
    hooks = []
    monkeypatch.setattr(worker, '_maybe_run_insight', lambda *a, **k: hooks.append('insight'))
    monkeypatch.setattr(worker, '_maybe_run_termfill', lambda *a, **k: hooks.append('termfill'))
    monkeypatch.setattr(_hooks, '_maybe_run_checkpoints', lambda *a, **k: hooks.append('checkpoints'))

    def dispatch(messages, *, on_content, **kwargs):
        on_content(FINAL_BODY)
        return ({'role': 'assistant', 'content': FINAL_BODY, 'tool_calls': []}, 'stop', {})

    return lambda: run(dispatch), task, worker, hooks


@pytest.mark.parametrize('phase', ['enrichment', 'citation', 'terminology'])
@pytest.mark.parametrize('request_local', [False, True])
@pytest.mark.unit
def test_stop_during_report_preparation_prevents_publication_and_postpasses(
        publication_case, monkeypatch, phase, request_local):
    from lib.paper import citation_audit
    from lib.paper.terminology_audit import audit

    run, task, worker, hooks = publication_case
    if request_local:
        task['requestPolicyV1']['cacheMode'] = 'request_local'
    stops = []

    def stop(value, *args, **kwargs):
        stops.append(worker._report_runtime.abort_owned(task['task_id'], user_id=17))
        return value if phase == 'enrichment' else None

    target, name = {
        'enrichment': (worker, 'inject_images_into_report'),
        'citation': (citation_audit, 'build_citation_audit'),
        'terminology': (audit, 'build_terminology_audit'),
    }[phase]
    monkeypatch.setattr(target, name, stop)
    task, published = run()

    assert stops == [True]
    assert task['status'] == 'aborted'
    assert task['result'] is None
    assert not published
    assert not hooks
    terminal = [e for e in task['events'] if e['type'] in ('done', 'error', 'aborted')]
    assert len(terminal) == 1
    assert terminal[0]['type'] == 'aborted'
    assert terminal[0]['partial'] == FINAL_BODY
    assert not any(e['type'] == 'enriched' for e in task['events'])


@pytest.mark.parametrize('request_local', [False, True])
@pytest.mark.unit
def test_stop_at_terminal_claim_cannot_be_missed_by_a_worker_precheck(
        publication_case, monkeypatch, request_local):
    run, task, worker, hooks = publication_case
    if request_local:
        task['requestPolicyV1']['cacheMode'] = 'request_local'
    runtime = worker._report_runtime
    finish = runtime.finish

    def stop_before_claim(task_id, **kwargs):
        assert runtime.abort_owned(task_id, user_id=17)
        return finish(task_id, **kwargs)

    monkeypatch.setattr(runtime, 'finish', stop_before_claim)
    task, published = run()
    assert task['status'] == 'aborted'
    assert task['result'] is None
    assert not published and not hooks
    assert task['events'][-1]['type'] == 'aborted'
    assert task['events'][-1]['partial'] == FINAL_BODY


@pytest.mark.unit
def test_an_already_settled_report_does_not_publish_or_start_postpasses(
        publication_case, monkeypatch):
    from lib.paper import citation_audit

    run, task, worker, hooks = publication_case

    def interrupt(body):
        assert worker._report_runtime.finish(task['task_id'], error=RuntimeError('worker retired'))

    monkeypatch.setattr(citation_audit, 'build_citation_audit', interrupt)
    task, published = run()
    assert task['status'] == 'error'
    assert not published and not hooks
    assert [e['type'] for e in task['events'] if e['type'] in ('done', 'error', 'aborted')] == ['error']


@pytest.mark.parametrize('outcome', ['saved', 'refused', 'error'])
@pytest.mark.unit
def test_publication_claim_fences_stop_and_competing_finish_without_locking_reads(
        publication_case, monkeypatch, outcome):
    run, task, worker, hooks = publication_case
    entered, release = threading.Event(), threading.Event()
    repository = worker.PaperArtifactRepository

    class Repository(repository):
        def put_report(self, report, **kwargs):
            entered.set()
            assert release.wait(5), 'test did not release publication'
            if outcome == 'error':
                raise OSError('storage unavailable')
            if outcome == 'refused':
                return False
            return super().put_report(report, **kwargs)

    monkeypatch.setattr(worker, 'PaperArtifactRepository', Repository)
    runtime = worker._report_runtime
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run)
        try:
            assert entered.wait(5), 'publication did not start'
            assert runtime.get_owned(task['task_id'], user_id=17)['status'] == 'running'
            assert runtime.poll(task['task_id'])['status'] == 'running'
            assert runtime.abort_owned(task['task_id'], user_id=17) is False
            assert runtime.finish(task['task_id'], error=RuntimeError('competing finish')) is False
        finally:
            release.set()
        task, published = future.result(timeout=5)

    expected = 'done' if outcome == 'saved' else 'error'
    assert task['status'] == expected
    assert task['_executionSession'].phase.value == ('completed' if outcome == 'saved' else 'failed')
    assert len(published) == int(outcome == 'saved')
    assert hooks == (['insight', 'termfill', 'checkpoints'] if outcome == 'saved' else [])
    assert [e['type'] for e in task['events'] if e['type'] in ('done', 'error', 'aborted')] == [expected]


@pytest.mark.unit
def test_failed_execution_cleanup_cannot_publish_a_report(publication_case):
    run, task, _, hooks = publication_case

    def refuse_release(_):
        raise OSError('cleanup unavailable')

    task['_executionSession'].hold_resource('route', refuse_release)
    task, published = run()
    assert task['status'] == 'error'
    assert task['result'] is None
    assert not published and not hooks
    assert task['events'][-1]['type'] == 'error'


@pytest.mark.parametrize('title_failure', [False, True])
@pytest.mark.unit
def test_confirmed_publication_precedes_title_repair_enrichment_and_done(
        publication_case, monkeypatch, title_failure):
    run, task, worker, hooks = publication_case
    order = []
    repository = worker.PaperArtifactRepository

    class Repository(repository):
        def put_report(self, report, **kwargs):
            order.append('stored')
            return super().put_report(report, **kwargs)

    def backfill(*args, **kwargs):
        assert order == ['stored']
        order.append('title')
        if title_failure:
            raise OSError('optional title repair failed')
        return 'Recovered title'

    append = worker._append_report_event

    def emit(target, event):
        if event['type'] == 'enriched':
            assert order == ['stored', 'title']
            assert target['status'] == 'running'
            order.append('enriched')
        append(target, event)

    monkeypatch.setattr(worker, 'PaperArtifactRepository', Repository)
    monkeypatch.setattr(worker, 'extract_title_from_report', lambda body: 'Recovered title')
    monkeypatch.setattr(worker, 'backfill_library_title', backfill)
    monkeypatch.setattr(worker, 'inject_images_into_report', lambda body, *a, **k: body + '\nFigure')
    monkeypatch.setattr(worker, '_append_report_event', emit)
    task, published = run()
    assert order == ['stored', 'title', 'enriched']
    assert task['status'] == 'done'
    assert published[0].report == task['result'] == FINAL_BODY + '\nFigure'
    assert task['events'][-1]['type'] == 'done'
    assert task['events'][-1]['resolvedTitle'] == ('' if title_failure else 'Recovered title')
    assert hooks == ['insight', 'termfill', 'checkpoints']


@pytest.mark.parametrize('backend', ['sqlite', 'tofudb'])
@pytest.mark.parametrize('outcome', ['done', 'request_local', 'stopped'])
@pytest.mark.api
def test_report_publication_and_owner_scope_survive_authority_restart(
        publication_case, monkeypatch, tmp_path, backend, outcome):
    from lib.paper import citation_audit
    from lib.paper.artifact_repository import PaperArtifactRepository, PaperReport
    from lib.storage import StorageError, StorageSupervisor

    run, task, worker, hooks = publication_case
    monkeypatch.setenv('TOFUDB_OWNER_ID', '17')
    monkeypatch.setenv('TOFUDB_TENANT_ID', '1')
    monkeypatch.setenv('TOFUDB_TRANSPORT', 'daemon')
    supervisor = StorageSupervisor(project_root=tmp_path / 'authority', backend=backend)
    try:
        supervisor.start()
        def repository(owner):
            return PaperArtifactRepository(owner, client_factory=lambda **_: supervisor.client)

        assert repository(17).put_report(
            PaperReport(task['paper_hash'], 'en', 'previous report'), command_id='seed-own')
        foreign_report = PaperReport(task['paper_hash'], 'en', 'foreign report')
        if backend == 'sqlite':
            assert repository(29).put_report(foreign_report, command_id='seed-foreign')
        else:
            # Native authority authenticates one launch scope; it must refuse
            # a repository that supplies another owner, before any write.
            with pytest.raises(StorageError) as refused:
                repository(29).put_report(foreign_report, command_id='seed-foreign')
            assert refused.value.code == 'database_forbidden'
        monkeypatch.setattr(worker, 'PaperArtifactRepository', repository)
        if outcome == 'request_local':
            task['requestPolicyV1']['cacheMode'] = 'request_local'
        elif outcome == 'stopped':
            def stop(body):
                assert worker._report_runtime.abort_owned(task['task_id'], user_id=17)
            monkeypatch.setattr(citation_audit, 'build_citation_audit', stop)

        task, _ = run()
        assert task['status'] == ('aborted' if outcome == 'stopped' else 'done')
        assert hooks == (['insight', 'termfill', 'checkpoints'] if outcome == 'done' else [])
        supervisor.stop()
        supervisor.start()
        own = repository(17).get_report(task['paper_hash'], 'en')
        assert own.report == (FINAL_BODY if outcome == 'done' else 'previous report')
        if backend == 'sqlite':
            foreign = repository(29).get_report(task['paper_hash'], 'en')
            assert foreign.report == 'foreign report'
        else:
            with pytest.raises(StorageError) as refused:
                repository(29).get_report(task['paper_hash'], 'en')
            assert refused.value.code == 'database_forbidden'
    finally:
        supervisor.stop()
