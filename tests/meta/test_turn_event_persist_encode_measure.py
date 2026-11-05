"""Self-check for benchmarks/turn_event_persist_encode.py (equal work gates clocks)."""
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PATH = Path(__file__).resolve().parents[2] / 'benchmarks' / 'turn_event_persist_encode.py'


def _measure():
    spec = importlib.util.spec_from_file_location('turn_event_persist_encode', _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated_env(monkeypatch):
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
    monkeypatch.setenv('TOFU_TURN_STRUCTURAL_RECORD_MS', '0')


def test_worker_digests_are_deterministic_and_independent_of_gil_load(isolated_env):
    from lib import turn_lifecycle
    from lib.tasks_pkg.manager import _sync
    seams = (turn_lifecycle._turn_client, turn_lifecycle._now_ms, _sync._upsert_task_row)
    measure = _measure()
    idle = measure.run_worker(4, 0, 3)
    again = measure.run_worker(4, 0, 3)
    loaded = measure.run_worker(4, 1, 3)
    keys = ('wireSha256', 'rowsSha256', 'checkpointSha256', 'wireBytes')
    assert [idle[key] for key in keys] == [again[key] for key in keys] == [loaded[key] for key in keys]
    assert idle['sharesSettled'] is True
    assert idle['snapshot']['events'] == 8 and idle['checkpoint']['events'] == 8
    assert idle['structural']['events'] == 16 and idle['progress']['events'] == 12
    assert measure.run_worker(5, 0, 3)['rowsSha256'] != idle['rowsSha256']
    # The worker restores every production seam it replaced.
    assert (turn_lifecycle._turn_client, turn_lifecycle._now_ms, _sync._upsert_task_row) == seams


def test_driver_refuses_clocks_when_trees_persist_different_bytes(monkeypatch, capsys):
    measure = _measure()
    digests = iter(['a' * 64, 'b' * 64])

    def fake_arm(_root, rounds, busy, seed):
        return {'rounds': rounds, 'busy': busy, 'seed': seed, 'wireSha256': next(digests),
                'rowsSha256': 'r', 'checkpointSha256': 'c'}

    monkeypatch.setattr(measure, '_run_arm', fake_arm)
    with pytest.raises(SystemExit, match='different bytes'):
        measure.main(['--source-root', str(_PATH.parents[1]), '--rounds', '2', '--busy', '0'])
    assert 'calling-thread CPU' not in capsys.readouterr().out
