"""Unavailable account recovery never replaces acknowledged interrupted state."""
import pytest
from lib.research import records, runtime, engine, budget_binding as binding
from tests.research.test_research_budget_binding import bound_source as bound_source, LIMITS
from tests.research.test_research_record_ack import research_task
from tests.research.test_research_workspace_evidence_guard import storage
pytestmark = pytest.mark.unit


def retired(bound_source):
    task, client = bound_source
    runtime._research_runtime.remove_owned(task['task_id'], user_id=12)
    return task, client


def test_bound_record_does_not_advertise_or_create_legacy_resume(bound_source, monkeypatch):
    task, client = bound_source
    binding.bind_model_budget(task['task_id'], user_id=12, execution_limits=LIMITS)
    before = records.load_record(task['task_id'], user_id=12, internal=True)
    retired(bound_source)
    projected = records.load_record(task['task_id'], user_id=12)
    assert projected['status'] == 'interrupted'
    assert projected['resumable'] is False
    monkeypatch.setattr(runtime, '_new_research_task', lambda *args, **kwargs: pytest.fail('no replacement birth'))
    monkeypatch.setattr(records, 'restore_pipeline_checkpoint', lambda *args: pytest.fail('no checkpoint rewrite'))
    with pytest.raises(ValueError, match='explicit model account recovery'):
        records.resume_record(task['task_id'], user_id=12)
    assert records.load_record(task['task_id'], user_id=12, internal=True) == before
    assert runtime._research_runtime.get(task['task_id']) is None


def test_unbound_record_keeps_legacy_resume_visibility(bound_source):
    task, _ = retired(bound_source)
    assert records.load_record(task['task_id'], user_id=12)['resumable'] is True


def test_startup_bound_record_is_preserved_before_worker_birth(bound_source, monkeypatch, tmp_path):
    task, _ = bound_source
    task['_research_record_enabled'] = True
    task['workdir'] = str(tmp_path / 'research' / 'jobs' / task['task_id'])
    task['status'] = 'running'
    engine._write_manifest(task, 'running')
    binding.bind_model_budget(task['task_id'], user_id=12, execution_limits=LIMITS)
    before = records.load_record(task['task_id'], user_id=12, internal=True)
    retired(bound_source)
    monkeypatch.setattr(engine, 'research_root', lambda: str(tmp_path / 'research'))
    monkeypatch.setattr(runtime, '_new_research_task', lambda *args, **kwargs: pytest.fail('no replacement birth'))
    assert engine.resume_interrupted_research() == 0
    assert records.load_record(task['task_id'], user_id=12, internal=True) == before
    assert runtime._research_runtime.get(task['task_id']) is None
