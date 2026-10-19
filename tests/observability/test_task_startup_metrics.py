"""Startup diagnostics preserve failures and never admit identity labels."""
import pytest

from lib import observability

pytestmark = pytest.mark.unit


def test_startup_stage_metrics_are_closed_and_record_failure(monkeypatch):
    samples = []
    monkeypatch.setattr(
        observability._STORE, 'observe',
        lambda name, duration, buckets, **labels: samples.append((name, duration, labels)),
    )
    with observability.measure_task_startup('registration'):
        pass
    with pytest.raises(RuntimeError, match='original startup failure'):
        with observability.measure_task_startup('private-conversation-id'):
            raise RuntimeError('original startup failure')
    assert [labels for _, _, labels in samples] == [
        {'stage': 'registration', 'outcome': 'success'},
        {'stage': 'other', 'outcome': 'error'},
    ]
    assert all(duration >= 0 for _, duration, _ in samples)


def test_failed_diagnostic_observer_cannot_break_worker_entry(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError('diagnostic observer unavailable')
    monkeypatch.setattr(observability._STORE, 'observe', unavailable)
    with observability.measure_task_startup('worker_entry'):
        pass
    with pytest.raises(ValueError, match='worker rejected'):
        with observability.measure_task_startup('worker_entry'):
            raise ValueError('worker rejected')


@pytest.mark.parametrize('stage', sorted(
    stage for stage in observability._TASK_STARTUP_STAGES
    if stage.startswith('workspace_')))
def test_workspace_stages_emit_paired_content_free_anchors(monkeypatch, stage):
    import json
    records, samples = [], []
    monkeypatch.setattr(observability.logger, 'info',
                        lambda message, payload: records.append(json.loads(payload)))
    monkeypatch.setattr(observability._STORE, 'observe',
                        lambda name, duration, buckets, **labels: samples.append(labels))
    with observability.measure_task_startup(stage, identity={
            'task_id': 'owned-task', 'user_id': 7, 'secret': 'must-not-log'}):
        assert records[0]['boundary'] == 'start'
    assert [record['boundary'] for record in records] == ['start', 'complete']
    assert all(record['stage'] == stage and record['task_id'] == 'owned-task'
               and 'secret' not in record for record in records)
    assert records[1]['durationMs'] >= 0
    assert samples == [{'stage': stage, 'outcome': 'success'}]


def test_nested_startup_identity_is_scoped_and_restored(monkeypatch):
    import json
    from concurrent.futures import ThreadPoolExecutor
    records = []
    monkeypatch.setattr(observability.logger, 'info',
                        lambda message, payload: records.append(json.loads(payload)))
    def measure(stage, identity=None):
        with observability.measure_task_startup(stage, identity=identity):
            pass
    with observability.measure_task_startup('workspace_binding', identity={'task_id': 'outer'}):
        measure('workspace_index_seed')
        with pytest.raises(ValueError):
            with observability.measure_task_startup('workspace_snapshot', identity={'task_id': 'inner'}):
                raise ValueError('failure')
        measure('workspace_index_add')
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(measure, 'workspace_index_tree').result()
    measure('workspace_index_tree')
    starts = [r for r in records if r['boundary'] == 'start']
    assert [(r['stage'], r['task_id']) for r in starts] == [
        ('workspace_binding', 'outer'), ('workspace_index_seed', 'outer'),
        ('workspace_snapshot', 'inner'), ('workspace_index_add', 'outer')]
    assert observability._STARTUP_IDENTITY.get() is None
