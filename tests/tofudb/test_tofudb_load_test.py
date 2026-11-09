"""Safety and accounting contract of the production-shaped TofuDB load test.

The harness must never read or write production state: its measuring child
gets a scrubbed environment rooted in the private working directory, and a
root inside a live Tofu data directory is refused. Latency percentiles are
exact over recorded samples, and engine accounting is an interval delta.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'tofudb_load_test.py'


@pytest.fixture(scope='module')
def harness():
    spec = importlib.util.spec_from_file_location('tofudb_load_test', _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_child_environment_inherits_no_production_settings(harness, tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_PROJECT_PATH', '/live/tofu')
    monkeypatch.setenv('TOFU_STORAGE_CONNECTION_FILE', '/live/tofu/data/native-storage-connection.json')
    monkeypatch.setenv('TOFU_DATA_DIR', '/live/tofu/data')
    monkeypatch.setenv('PYTHONPATH', '/live/tofu')
    engine = tmp_path / 'engine' / 'tofu-db'
    env = harness._child_environment(tmp_path, engine, 'a' * 64, 'normal')

    assert 'TOFU_STORAGE_CONNECTION_FILE' not in env
    for key in ('HOME', 'TMPDIR', 'XDG_CACHE_HOME', 'TOFU_DATA_DIR', 'TOFU_PROJECT_PATH'):
        assert Path(env[key]).is_relative_to(tmp_path), key
    assert env['PYTHONPATH'] == str(harness.PROJECT_ROOT)
    assert env['TOFUDB_BIN'] == str(engine)
    assert env['TOFU_DB_DURABILITY'] == 'normal'
    assert 'TOFU_DB_DURABILITY' not in harness._child_environment(tmp_path, engine, 'a' * 64, 'auto')


def test_root_inside_a_live_checkout_is_refused(harness, tmp_path):
    checkout = tmp_path / 'tofu'
    (checkout / 'data').mkdir(parents=True)
    (checkout / 'data' / 'native-storage-connection.json').write_text('{}')
    with pytest.raises(SystemExit, match='inside a live Tofu checkout'):
        harness._refuse_live_root(checkout / 'data' / 'scratch')
    with pytest.raises(SystemExit, match='live Tofu data root'):
        harness._refuse_live_root(checkout / 'data')
    harness._refuse_live_root(tmp_path / 'private')


def test_recorder_reports_exact_percentiles_and_error_codes(harness):
    recorder = harness.Recorder()
    for value in range(1, 101):
        recorder.samples['op'].append(value / 1000)

    class Refused(Exception):
        code = 'database_timeout'

    def refuse():
        raise Refused()

    with pytest.raises(Refused):
        recorder.timed('op', refuse)
    summary = recorder.summary(10.0)
    assert summary['op']['n'] == 100
    assert summary['op']['p50_ms'] == pytest.approx(51.0)
    assert summary['op']['p99_ms'] == pytest.approx(99.0)
    assert summary['op']['errors'] == {'database_timeout': 1}
    assert summary['op:error']['n'] == 1


def test_the_phase_stop_settlement_herd_is_reported_apart_from_turn_settlement(harness, tmp_path, monkeypatch):
    """Stopping a phase settles every running Turn at the same moment; that herd
    queues on the one client process and must not read as one Turn's settlement."""
    import lib.durable_worker as durable_worker
    import lib.turn_lifecycle as turn_lifecycle

    run = harness.Harness(SimpleNamespace(random_seed=7), tmp_path)
    run.conversations = {kind: [f'{kind}-1'] for kind in harness.PRODUCTION_PROFILE['active_kind_weights']}
    run.bookkeeping = lambda state, rng: None
    monkeypatch.setitem(harness.PRODUCTION_PROFILE, 'events_per_turn', (2, 3))
    monkeypatch.setitem(harness.PRODUCTION_PROFILE, 'events_per_attempt_per_s', 1e9)
    turns = []

    def create_turn_pair(conv_id, **kwargs):
        turns.append(conv_id)
        return {'attempt': {'attemptId': f'attempt-{len(turns)}'}}

    class Store:
        def claim_next(self, **kwargs):
            return {'payload': {'attemptId': f'attempt-{len(turns)}', 'conversationId': turns[-1],
                                'turnId': f'turn-{len(turns)}'}}

        def heartbeat(self, *args, **kwargs):
            pass

        def complete(self, *args, **kwargs):
            pass

    def record_task_event(task, wire, task_event=None):
        if len(turns) == 2 and wire['type'] != 'done':
            run.stop.set()  # the phase ends while the second Turn streams
        return 'carried'

    monkeypatch.setattr(durable_worker, 'StorageWorkerJobStore', Store)
    monkeypatch.setattr(durable_worker, 'WorkerJobClaim', SimpleNamespace(
        from_document=lambda document, expected_worker_id: SimpleNamespace(task_id='task', fencing_token=1)))
    monkeypatch.setattr(durable_worker, 'WorkerExecutionContext', lambda claim: None)
    monkeypatch.setattr(turn_lifecycle, 'create_turn_pair', create_turn_pair)
    monkeypatch.setattr(turn_lifecycle, 'dispatch_attempt_to_worker', lambda principal, attempt_id: None)
    monkeypatch.setattr(turn_lifecycle, 'mark_task_started', lambda *args, **kwargs: None)
    monkeypatch.setattr(turn_lifecycle, 'record_task_event', record_task_event)

    run.agent(0, 1.0)
    summary = run.recorder.summary(1.0)
    assert len(turns) == 2
    assert summary['turn.event.record:terminal']['n'] == 1
    assert summary['turn.event.record:terminal:phase_stop']['n'] == 1
    assert run.counters['turns_completed'] == 2


def test_engine_delta_is_an_interval_and_charges_bytes_per_change(harness):
    def metrics(calls, nanos, wal, frames):
        return {'commitStages': [{'stage': 'authority_wait', 'calls': calls, 'totalNanos': nanos}],
                'durability': {'walEncodedBytes': wal, 'projectionCheckpointBytes': 0},
                'listener': {'requestFrames': frames, 'acceptedConnections': 1, 'peakActiveConnections': 3}}

    delta = harness._engine_delta(
        metrics(10, 10_000_000, 1_000, 5), metrics(30, 50_000_000, 21_000, 105),
        {'cpu_s': 1.0, 'write_bytes': 100, 'wchar': 100},
        {'cpu_s': 3.0, 'write_bytes': 40_100, 'wchar': 50_100}, 10.0, changes=20)
    assert delta['stages']['authority_wait'] == {'calls': 20, 'avg_ms': 2.0, 'thread_eq': 0.004}
    assert delta['durability']['walEncodedBytes'] == 20_000
    assert delta['listener']['requestFrames'] == 100
    assert delta['process']['write_bytes_per_change'] == 2_000
    assert delta['process']['wal_bytes_per_change'] == 1_000
    assert delta['cpu_cores'] == 0.2


def test_engine_delta_reports_growth_reclamation_and_packing(harness):
    def metrics(reclaimed, compactions, retained, packed):
        return {'onlineGarbageCollector': {'total_reclaimed_blocks': reclaimed, 'epochs_completed': 1,
                                           'epoch_error': None},
                'maintenance': {'historyCompactions': compactions, 'retainedHistorySegments': retained},
                'backgroundPacker': {'packedSegments': packed}}

    growth = harness._engine_delta(metrics(5, 2, 900, 1), metrics(105, 12, 64, 4), {}, {}, 10.0,
                                   changes=0)['growth']
    assert growth['onlineGarbageCollector'] == {'total_reclaimed_blocks': 100, 'epochs_completed': 0,
                                                'epoch_error': None}
    assert growth['maintenance'] == {'historyCompactions': 10, 'retainedHistorySegments': 64}
    assert growth['backgroundPacker'] == {'packedSegments': 3}
    assert growth['historyPromotion'] == {}
