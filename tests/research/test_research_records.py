"""Durable research records survive registry loss without claiming live execution."""
from copy import deepcopy
import json

import pytest

pytestmark = pytest.mark.unit


def evolution_binding():
    return {'run_id': 'run-research-binding', 'goal_id': 'goal-binding',
            'plan_id': 'plan-binding', 'stage': 'executing'}


def bound_research(tmp_path):
    from lib.research import runtime
    tid = runtime._research_task_id()
    value, existing = runtime._claim_research_task((12, tid), tid,
        direction='Bound direction', workdir=str(tmp_path), lang='en', user_id=12,
        evolution=evolution_binding())
    assert not existing
    value['_research_record_enabled'] = True
    return value


def test_bound_record_and_task_meta_preserve_exact_run_identity(tmp_path, store):
    from lib.research import records, runtime
    value = bound_research(tmp_path)
    records.persist_record(value)
    assert value['meta']['evolution_run_id'] == evolution_binding()['run_id']
    record = records.load_record(value['task_id'], user_id=12)
    assert record['evolution_run_id'] == evolution_binding()['run_id']
    assert records.list_records(user_id=12)[0]['evolution_run_id'] == record['evolution_run_id']
    internal = records.load_record(value['task_id'], user_id=12, internal=True)
    assert internal['request']['evolution'] == evolution_binding()
    writes = store.writes
    value['config']['evolution']['run_id'] = 'run-another'
    with pytest.raises(ValueError, match='binding'):
        records.persist_record(value)
    assert store.writes == writes
    runtime._research_runtime.remove_owned(value['task_id'], user_id=12)


@pytest.mark.parametrize('run_status', ['executing', 'cancelled', 'needs_attention'])
def test_bound_resume_retains_authority_and_refuses_settled_runs(tmp_path, monkeypatch, store, run_status):
    from lib.research import records, runtime, engine
    from lib.evolution import runs
    value = bound_research(tmp_path)
    records.persist_record(value)
    tid = value['task_id']
    runtime._research_runtime.remove_owned(tid, user_id=12)
    authority = {**evolution_binding(), 'task_id': tid, 'domain': 'research', 'status': run_status}
    monkeypatch.setattr(runs, 'get_run', lambda rid, *, user_id: authority if user_id == 12 and rid == authority['run_id'] else None)
    monkeypatch.setattr(engine, 'research_root', lambda: str(tmp_path / 'resume'))
    spawned = []
    monkeypatch.setattr(runtime._research_runtime, 'spawn', lambda *args: spawned.append(args))
    reopened = records.load_record(tid, user_id=12)
    assert reopened['resumable'] is (run_status == 'executing')
    if run_status == 'executing':
        records.resume_record(tid, user_id=12)
        resumed = runtime._research_runtime.get_owned(tid, user_id=12)
        assert resumed['config']['evolution'] == evolution_binding()
        assert resumed['meta']['evolution_run_id'] == authority['run_id']
        assert len(spawned) == 1
        runtime._research_runtime.remove_owned(tid, user_id=12)
    else:
        with pytest.raises(ValueError, match='evolution'):
            records.resume_record(tid, user_id=12)
        assert not spawned and runtime._research_runtime.get_owned(tid, user_id=12) is None


def test_same_direction_runs_cannot_share_another_runs_worker(tmp_path, monkeypatch, store):
    from lib.research import engine, runtime
    monkeypatch.setattr(engine, 'research_root', lambda: str(tmp_path))
    spawned = []
    monkeypatch.setattr(runtime._research_runtime, 'spawn', lambda *args: spawned.append(args))
    a = evolution_binding(); b = {**a, 'run_id': 'run-second-binding'}
    first = engine.produce_research('same direction binding test', user_id=12, evolution=a)
    second = engine.produce_research('same direction binding test', user_id=12, evolution=b)
    assert first['task_id'] != second['task_id']
    again = engine.produce_research('same direction binding test', user_id=12, evolution=a)
    assert again['task_id'] == first['task_id'] and again['deduped']
    assert len(spawned) == 2
    for item, binding in ((first, a), (second, b)):
        value = runtime._research_runtime.get_owned(item['task_id'], user_id=12)
        assert value['config']['evolution'] == binding
        runtime._research_runtime.remove_owned(item['task_id'], user_id=12)


def test_cancel_during_recovery_birth_cannot_dispatch_recipe(tmp_path, monkeypatch, store):
    from lib.research import records, runtime, engine
    from lib.evolution import runs
    value = bound_research(tmp_path);records.persist_record(value);tid=value['task_id']
    runtime._research_runtime.remove_owned(tid, user_id=12)
    authority = {**evolution_binding(), 'task_id': tid, 'domain': 'research', 'status': 'executing'}
    monkeypatch.setattr(runs, 'get_run', lambda rid, *, user_id: authority)
    original = runtime._new_research_task
    def cancel_after_birth(*args, **kwargs):
        task = original(*args, **kwargs);authority['status']='cancelled';return task
    monkeypatch.setattr(runtime, '_new_research_task', cancel_after_birth)
    monkeypatch.setattr(engine, 'research_root', lambda: str(tmp_path / 'resume'))
    monkeypatch.setattr(runtime._research_runtime, 'spawn', lambda *args: pytest.fail('Cancelled recovery dispatched'))
    with pytest.raises(ValueError, match='evolution'):
        records.resume_record(tid, user_id=12)
    runtime._research_runtime.remove_owned(tid, user_id=12)


def test_manifest_recovery_retains_binding_and_worker_refuses_late_cancel(tmp_path, monkeypatch, store):
    from lib.research import records, runtime, engine, recipe
    from lib.evolution import runs
    root = tmp_path / 'research';jobs = root / 'jobs';jobs.mkdir(parents=True)
    value = bound_research(jobs / 'placeholder')
    tid = value['task_id'];value['workdir']=str(jobs / tid)
    records.persist_record(value);engine._write_manifest(value, 'running')
    manifest=json.loads((jobs / tid / 'job.json').read_text())
    assert manifest['evolution']==evolution_binding()
    runtime._research_runtime.remove_owned(tid, user_id=12)
    authority={**evolution_binding(),'task_id':tid,'domain':'research','status':'executing'}
    monkeypatch.setattr(runs,'get_run',lambda rid,*,user_id:authority)
    monkeypatch.setattr(engine,'research_root',lambda:str(root))
    spawned=[];monkeypatch.setattr(runtime._research_runtime,'spawn',lambda *args:spawned.append(args))
    assert engine.resume_interrupted_research()==1
    assert len(spawned)==1
    recovered=runtime._research_runtime.get_owned(tid,user_id=12)
    assert recovered['config']['evolution']==evolution_binding()
    authority['status']='cancelled'
    monkeypatch.setattr(recipe,'build_research_from_direction',lambda *a,**k:pytest.fail('Cancelled worker executed recipe'))
    engine.run_research_task(recovered)
    assert recovered['status'] in ('error','aborted')
    runtime._research_runtime.remove_owned(tid,user_id=12)
    # The stale running manifest must also be refused before a new birth.
    manifest['state']='running';(jobs/tid/'job.json').write_text(json.dumps(manifest))
    assert engine.resume_interrupted_research()==0
    assert runtime._research_runtime.get_owned(tid,user_id=12) is None


@pytest.fixture
def store(monkeypatch):
    from lib.research import records

    class Store:
        def __init__(self):
            self.rows = {}
            self.writes = 0
            self.fail = False

        def command(self, operation, payload, command_id):
            assert operation == 'record.put'
            if self.fail:
                raise RuntimeError('write refused')
            self.writes += 1
            self.rows[payload['namespace'], payload['key']] = deepcopy(payload['value'])
            return {'version': self.writes}

        def query(self, operation, payload):
            if operation == 'record.get':
                value = self.rows.get((payload['namespace'], payload['key']))
                return {'value': deepcopy(value)} if value else None
            assert operation == 'record.list'
            return [{'value': deepcopy(value)} for (namespace, key), value in
                    sorted(self.rows.items()) if namespace == payload['namespace']
                    and key.startswith(payload['prefix'])][:payload['limit']]

    value = Store()
    monkeypatch.setattr(records, '_storage', lambda **kwargs: value)
    from lib.evolution import records as record_store
    monkeypatch.setattr(record_store, '_storage', lambda **kwargs: value)
    return value


def task(tmp_path):
    from lib.research import runtime
    value = runtime._new_research_task(runtime._research_task_id(),
        direction='Synthetic direction', workdir=str(tmp_path), lang='en', user_id=12)
    value['_research_record_enabled'] = True
    return value


def test_registry_loss_restores_paper_metadata_and_explicit_checkpoint_resume(tmp_path, monkeypatch, store):
    from lib.research import records, runtime, engine
    value = task(tmp_path)
    checkpoint = {'version': 1, 'stages': {'harvest': {'done': True, 'artifact': {'folder_id': 'owned'}}}}
    (tmp_path / 'pipeline_state.json').write_text(json.dumps(checkpoint))
    records.persist_record(value, event={'type': 'stage_done'})
    runtime._append_research_event(value, {'type': 'paper_done', 'stage': 'harvest',
        'result': {'arxivId': '2401.00001', 'title': 'Captured before stage completion',
                   'paperId': 'harvest_2401.00001', 'status': 'parsed'}})
    assert records.load_record(value['task_id'], user_id=13) is None
    assert records.list_records(user_id=13) == []
    assert runtime._research_runtime.remove_owned(value['task_id'], user_id=12)
    reopened = records.load_record(value['task_id'], user_id=12)
    assert reopened['status'] == 'interrupted'
    assert reopened['resumable'] is True
    assert reopened['papers'][0]['title'] == 'Captured before stage completion'
    assert 'checkpoint' not in reopened
    from pathlib import Path
    import jsonschema
    schema_path = Path(__file__).parents[2] / 'contracts' / 'research_record_v1.schema.json'
    schema = json.loads(schema_path.read_text())
    # Resolve the sibling progress schema without network access.
    schema['properties']['research_progress']['anyOf'][1] = json.loads(
        schema_path.with_name('research_progress_v1.schema.json').read_text())
    jsonschema.validate(reopened, schema)
    monkeypatch.setattr(engine, 'research_root', lambda: str(tmp_path / 'restart'))
    spawned = []
    monkeypatch.setattr(runtime._research_runtime, 'spawn', lambda *args: spawned.append(args))
    result = records.resume_record(value['task_id'], user_id=12)
    assert result['deduped'] is False
    assert len(spawned) == 1
    restored = tmp_path / 'restart' / 'jobs' / value['task_id'] / 'pipeline_state.json'
    assert json.loads(restored.read_text()) == checkpoint


def test_write_failure_stops_worker_instead_of_clean_success(tmp_path, monkeypatch, store):
    from lib.research import records, runtime, engine, recipe
    value = task(tmp_path)
    records.persist_record(value)

    def build(*args, **kwargs):
        store.fail = True
        kwargs['emit']({'type': 'paper_done', 'result': {'arxivId': '2401.00001'}})
        return {'accepted': [], 'rejected': []}

    monkeypatch.setattr(recipe, 'build_research_from_direction', build)
    engine.run_research_task(value)
    assert value['status'] == 'error'
    assert not any(e.get('type') == 'final' for e in value['events'])
    assert value['_research_persistence_error']


def test_transient_storage_timeout_retries_instead_of_killing_run(tmp_path, monkeypatch, store):
    """Regression (2026-09-09): a maintenance prune held the single SQLite
    writer ~2.8 s, one record.put acquisition timeout killed a mid-harvest
    run, and the record blamed 'aborted in stage harvest'. A retryable
    StorageError must ride out the hold — the snapshot write is idempotent.
    """
    from lib.research import records, runtime, engine, recipe
    from lib.storage.errors import StorageError
    monkeypatch.setattr(records, '_COMMIT_RETRY_DELAYS_S', (0.01, 0.01))
    value = task(tmp_path)
    records.persist_record(value)
    remaining = {'n': 2}
    real_command = store.command

    def starved_writer(operation, payload, command_id):
        if remaining['n']:
            remaining['n'] -= 1
            raise StorageError('database_timeout',
                               'Storage writer acquisition timed out', True)
        return real_command(operation, payload, command_id)

    monkeypatch.setattr(store, 'command', starved_writer)

    def build(*args, **kwargs):
        kwargs['emit']({'type': 'paper_done', 'result': {'arxivId': '2401.00001'}})
        return {'accepted': [], 'rejected': []}

    monkeypatch.setattr(recipe, 'build_research_from_direction', build)
    engine.run_research_task(value)
    assert remaining['n'] == 0
    assert value['status'] == 'done'
    assert any(e.get('type') == 'final' for e in value['events'])
    assert not value.get('_research_persistence_error')


def test_persistent_write_failure_reports_storage_cause_not_stage_abort(tmp_path, monkeypatch, store):
    """When a write failure does sacrifice the run, the record must name the
    storage cause — not whichever stage noticed the abort flag first.
    """
    from lib.research import records, runtime, engine, recipe
    value = task(tmp_path)
    records.persist_record(value)

    def build(*args, **kwargs):
        store.fail = True
        kwargs['emit']({'type': 'paper_done', 'result': {'arxivId': '2401.00001'}})
        raise RuntimeError("aborted in stage 'harvest'")

    monkeypatch.setattr(recipe, 'build_research_from_direction', build)
    engine.run_research_task(value)
    assert value['status'] == 'error'
    rendered = json.dumps(value['error'])
    assert 'write refused' in rendered
    assert "aborted in stage" not in rendered


def test_token_traffic_has_no_durable_write_and_papers_are_bounded(tmp_path, store):
    from lib.research import records, runtime
    value = task(tmp_path)
    records.persist_record(value)
    writes = store.writes
    for _ in range(1000):
        runtime._append_research_event(value, {'type': 'text_delta', 'text': 'x'})
    assert store.writes == writes
    for index in range(200):
        records.persist_record(value, event={'type': 'paper_done', 'result': {
            'arxivId': str(index), 'title': 'x' * 1000}})
    assert len(value['_durable_research_record']['papers']) == 200
    assert len(value['_durable_research_record']['papers'][0]['title']) == 500
    assert len(store.rows) == 2  # replace-in-place snapshot + compact index
    with pytest.raises(ValueError, match='ceiling'):
        records.persist_record(value, event={'type': 'paper_done', 'result': {'arxivId': 'overflow'}})


def test_run_result_is_frozen_independently_of_same_direction(tmp_path, store):
    from lib.research import records
    first = task(tmp_path)
    second = task(tmp_path)
    result = {'survey_md': 'first run evidence', 'accepted': []}
    records.persist_record(first, status='done', result=result)
    result['survey_md'] = 'mutated caller'
    records.persist_record(second, status='done', result={'survey_md': 'second run'})
    assert records.load_record(first['task_id'], user_id=12)['result']['survey_md'] == 'first run evidence'
    assert records.load_record(second['task_id'], user_id=12)['result']['survey_md'] == 'second run'


def test_start_refusal_does_not_spawn(tmp_path, monkeypatch, store):
    from lib.research import engine, runtime
    store.fail = True
    monkeypatch.setattr(engine, 'research_root', lambda: str(tmp_path))
    monkeypatch.setattr(runtime._research_runtime, 'spawn',
                        lambda *args: pytest.fail('unpersisted task spawned'))
    with pytest.raises(RuntimeError, match='write refused'):
        engine.produce_research('cannot persist', user_id=12)


def test_literature_commits_before_live_and_terminal_error_is_bounded(tmp_path, monkeypatch, store):
    from lib.research import records, runtime
    value = task(tmp_path)
    event = {'type': 'literature_discovered', 'papers': [
        {'paperId': 'doi:10.1234/a', 'doi': '10.1234/a', 'citationCount': 0,
         'citationCountSource': 'crossref', 'year': None, 'venue': ''}],
        'citation_edges': [], 'source_statuses': [{'source': 'semantic_scholar', 'status': 'unavailable'}]}
    original = runtime._production.append_event
    def append(task, receipt):
        assert store.rows[records.NAMESPACE, records._key(value['task_id'], 12)]['papers'][0]['citationCount'] == 0
        return original(task, receipt)
    monkeypatch.setattr(runtime._production, 'append_event', append)
    runtime._append_research_event(value, event)
    records.persist_record(value, event={'type': 'stage_failed', 'stage': 'survey'})
    value['error'] = {'message': 'Canonical failure', 'detail': 'x' * 3000, 'raw': 'private raw'}
    records.persist_record(value, status='error')
    runtime._research_runtime.remove_owned(value['task_id'], user_id=12)
    reopened = records.load_record(value['task_id'], user_id=12)
    assert len(reopened['error']) == 2000
    assert reopened['error'].startswith('Canonical failure: ')
    assert 'private raw' not in reopened['error']
    assert reopened['failed_stage'] == 'survey'
    assert records.list_records(user_id=12)[0]['error'] == reopened['error']
    from pathlib import Path
    import jsonschema
    path = Path(__file__).parents[2] / 'contracts' / 'research_record_v1.schema.json'
    schema = json.loads(path.read_text())
    schema['properties']['research_progress']['anyOf'][1] = json.loads(path.with_name('research_progress_v1.schema.json').read_text())
    jsonschema.validate(reopened, schema)


def test_rediscovery_rewrites_edges_to_retained_ids_before_live_and_reopen(tmp_path, store):
    from lib.research import records, runtime
    value = task(tmp_path)
    runtime._append_research_event(value, {'type': 'literature_discovered', 'papers': [
        {'paperId': 'arxiv:2401.00001', 'arxivId': '2401.00001'},
        {'paperId': 'arxiv:2401.00002', 'arxivId': '2401.00002'}]})
    runtime._append_research_event(value, {'type': 'paper_done', 'result': {
        'arxivId': '2401.00001', 'paperId': 'parser-hash', 'status': 'parsed'}})
    incoming = {'type': 'literature_discovered', 'papers': [
        {'paperId': 'doi:10.1234/a', 'doi': '10.1234/a', 'arxivId': '2401.00001'},
        {'paperId': 'doi:10.1234/b', 'doi': '10.1234/b', 'arxivId': '2401.00002'}],
        'citation_edges': [{'from': 'doi:10.1234/a', 'to': 'doi:10.1234/b', 'relation': 'cites'}]}
    runtime._append_research_event(value, incoming)
    expected = [{'from': 'arxiv:2401.00001', 'to': 'arxiv:2401.00002', 'relation': 'cites'}]
    assert value['events'][-1]['citation_edges'] == expected
    assert value['events'][-1]['papers'][0]['status'] == 'parsed'
    # Recovery from a newer DOI-based checkpoint cannot strand saved edges.
    checkpoint = {'stages': {'harvest': {'artifact': {'discovery': incoming}}}}
    (tmp_path / 'pipeline_state.json').write_text(json.dumps(checkpoint))
    records.persist_record(value, event={'type': 'stage_done'}, status='error')
    runtime._research_runtime.remove_owned(value['task_id'], user_id=12)
    reopened = records.load_record(value['task_id'], user_id=12)
    assert reopened['citation_edges'] == expected
    ids = {paper['paperId'] for paper in reopened['papers']}
    assert all(edge['from'] in ids and edge['to'] in ids for edge in reopened['citation_edges'])
    assert reopened['papers'][0]['status'] == 'parsed'


def test_recipe_map_failure_persists_paper_receipts_without_success(tmp_path, monkeypatch, store):
    from lib.research import records, runtime, recipe
    value = task(tmp_path)
    failures = [{'batch': 0, 'paper_ids': ['2401.00001', '2401.00002'], 'error': 'ValueError: offline provider'}]
    monkeypatch.setattr(recipe, '_build_survey', lambda *a, **k: {
        'ok': False, 'error': 'no successful survey map batches', 'batch_failures': failures})
    events = []
    def emit(event):
        events.append(event)
        runtime._append_research_event(value, event)
    with pytest.raises(RuntimeError, match='survey failed'):
        recipe._run_survey({'user_id': 12, 'direction': 'test', 'emit': emit,
            'artifacts': {'harvest': {'folder_id': 'owned', 'arxiv_ids': failures[0]['paper_ids']}}})
    assert not any(event['type'] == 'stage_done' for event in events)
    records.persist_record(value, event={'type': 'stage_failed', 'stage': 'survey'}, status='error')
    runtime._research_runtime.remove_owned(value['task_id'], user_id=12)
    reopened = records.load_record(value['task_id'], user_id=12)
    assert reopened['survey_batch_failures'] == failures
    assert reopened['failed_stage'] == 'survey'
    assert 'survey_batch_failures' not in records.list_records(user_id=12)[0]
    from pathlib import Path
    import jsonschema
    path = Path(__file__).parents[2] / 'contracts' / 'research_record_v1.schema.json'
    schema = json.loads(path.read_text())
    schema['properties']['research_progress']['anyOf'][1] = json.loads(path.with_name('research_progress_v1.schema.json').read_text())
    jsonschema.validate(reopened, schema)
    runtime._append_research_event(value, {'type': 'stage_started', 'stage': 'survey'})
    assert 'survey_batch_failures' not in value['_durable_research_record']
