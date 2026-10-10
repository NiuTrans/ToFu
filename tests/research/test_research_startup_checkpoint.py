"""Startup recovery restores acknowledged stages and never reopens terminal runs."""
import json
import pytest
from lib.research import engine, records, runtime
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


@pytest.fixture
def interrupted(tmp_path, monkeypatch, storage):
    supervisor, _ = storage
    monkeypatch.setattr(records, '_storage', lambda **kwargs: supervisor.client)
    from lib.evolution import records as record_store
    monkeypatch.setattr(record_store, '_storage', lambda **kwargs: supervisor.client)
    root = tmp_path / 'research'
    task_id = runtime._research_task_id()
    workdir = root / 'jobs' / task_id
    task = runtime._new_research_task(task_id, user_id=12, direction='Durable restart',
                                     lang='en', workdir=str(workdir))
    task['_research_record_enabled'] = True
    task['status'] = 'running'
    workdir.mkdir(parents=True)
    checkpoint = {'stages': {'harvest': {'status': 'done', 'artifact': {'papers': []}}}}
    (workdir / 'pipeline_state.json').write_text(json.dumps(checkpoint))
    records.persist_record(task, event={'type': 'stage_done', 'stage': 'harvest'})
    engine._write_manifest(task, 'running')
    runtime._research_runtime.remove_owned(task_id, user_id=12)
    monkeypatch.setattr(engine, 'research_root', lambda: str(root))
    spawned = []
    monkeypatch.setattr(runtime._research_runtime, 'spawn', lambda *args: spawned.append(args))
    try:
        yield task, workdir, checkpoint, spawned
    finally:
        runtime._research_runtime.remove_owned(task_id, user_id=12)


@pytest.mark.parametrize('local_state', ['missing', 'stale'])
def test_startup_restores_saved_checkpoint_before_worker_birth(interrupted, monkeypatch, local_state):
    task, workdir, checkpoint, spawned = interrupted
    local = workdir / 'pipeline_state.json'
    if local_state == 'missing':
        local.unlink()
    else:
        local.write_text(json.dumps({'stages': {}}))
    create = runtime._new_research_task
    def checked_birth(*args, **kwargs):
        assert json.loads(local.read_text()) == checkpoint
        return create(*args, **kwargs)
    monkeypatch.setattr(runtime, '_new_research_task', checked_birth)
    assert engine.resume_interrupted_research() == 1
    assert len(spawned) == 1
    assert engine.resume_interrupted_research() == 0


@pytest.mark.parametrize('status', ['done', 'degraded', 'error', 'aborted'])
def test_stale_running_manifest_cannot_reopen_terminal_record(interrupted, status):
    task, workdir, checkpoint, spawned = interrupted
    records.persist_record(task, status=status)
    assert json.loads((workdir / 'job.json').read_text())['state'] == 'running'
    assert engine.resume_interrupted_research() == 0
    assert spawned == []
    assert runtime._research_runtime.get(task['task_id']) is None


def test_restore_failure_has_no_carrier_or_dispatch(interrupted, monkeypatch):
    task, workdir, checkpoint, spawned = interrupted
    from lib import json_store
    def fail(*args, **kwargs):
        raise OSError('checkpoint volume unavailable')
    monkeypatch.setattr(json_store, 'write_json_atomic', fail)
    assert engine.resume_interrupted_research() == 0
    assert spawned == []
    assert runtime._research_runtime.get(task['task_id']) is None
    assert records.load_record(task['task_id'], user_id=12, internal=True)['checkpoint'] == checkpoint


def test_storage_restart_recovers_acknowledged_stage_material(interrupted, storage):
    task, workdir, checkpoint, spawned = interrupted
    supervisor, _ = storage
    supervisor.stop()
    supervisor.start()
    (workdir / 'pipeline_state.json').unlink()
    assert engine.resume_interrupted_research() == 1
    assert json.loads((workdir / 'pipeline_state.json').read_text()) == checkpoint
    assert len(spawned) == 1
