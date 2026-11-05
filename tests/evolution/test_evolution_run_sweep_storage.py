"""Run sweep work list against actual stores: guarded marker lifecycle."""
import pytest

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.api

USER = 13


def _run(run_id, *, status, updated_at):
    return dict(schema_version=1, run_id=run_id, objective='probe', status=status,
                goal_id=run_id, plan_id=run_id, episode_id='', task_id='task-1', attempt=1,
                budget={'max_tokens': 10, 'max_wall_clock_seconds': 10, 'max_tool_calls': 1},
                actuals={'tokens': 0, 'wall_clock_seconds': 0, 'tool_calls': 0},
                receipts=[], result='', review=None, error='', learning=None,
                created_at=updated_at, updated_at=updated_at)


@pytest.mark.parametrize('credential_storage', ['sqlite', 'tofudb'], indirect=True)
def test_unsettled_run_markers_backfill_settle_and_keep_newer_marks(credential_storage, monkeypatch):
    from lib.evolution import records, runs
    monkeypatch.setattr(runs, 'default_task_loader', lambda task_id, *, user_id: None)
    for run_id, status in (('run-done', 'completed'), ('run-stale', 'executing')):
        records.put_record(records.NAMESPACE_RUNS, f'{USER}:{run_id}',
                           _run(run_id, status=status, updated_at=1), kind='run')
    # The first sweep backfills the list from history and settles the run.
    assert runs.sweep_stale_runs(user_id=USER, now=10_000) == [
        {'run_id': 'run-stale', 'from': 'executing', 'to': 'needs_attention'}]
    assert records.get_record(records.NAMESPACE_RUN_SWEEP, f'{USER}:unsettled-index') == {'complete': True}
    assert records.list_records(records.NAMESPACE_UNSETTLED_RUNS, USER, kind='run') == []
    # A guarded delete keeps a marker rewritten after it was read.
    key = f'{USER}:run-stale'
    runs._mark_unsettled(_run('run-stale', status='executing', updated_at=5), USER)
    _, listed = records.get_versioned_record(records.NAMESPACE_UNSETTLED_RUNS, key)
    runs._mark_unsettled(_run('run-stale', status='executing', updated_at=7), USER)
    assert records.delete_record(records.NAMESPACE_UNSETTLED_RUNS, key,
                                 kind='run_marker', expected_version=listed) is False
    assert records.get_record(records.NAMESPACE_UNSETTLED_RUNS, key)['marked_at'] == 7
    # Backfill marks are create-only.
    runs._mark_unsettled(_run('run-stale', status='executing', updated_at=1), USER, replace=False)
    assert records.get_record(records.NAMESPACE_UNSETTLED_RUNS, key)['marked_at'] == 7
    # The indexed sweep reads the listed marker version and settles it.
    run, version = records.get_versioned_record(records.NAMESPACE_RUNS, key)
    run['status'] = 'executing'
    runs._save(run, version, USER)
    reopened = records.get_record(records.NAMESPACE_UNSETTLED_RUNS, key)['marked_at']
    assert reopened >= 7
    assert runs.sweep_stale_runs(user_id=USER, now=reopened + 3_600) == [
        {'run_id': 'run-stale', 'from': 'executing', 'to': 'needs_attention'}]
    assert records.list_records(records.NAMESPACE_UNSETTLED_RUNS, USER, kind='run') == []
