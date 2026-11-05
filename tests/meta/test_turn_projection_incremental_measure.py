"""Self-check for benchmarks/turn_projection_incremental.py (equal work gates clocks)."""
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PATH = Path(__file__).resolve().parents[2] / 'benchmarks' / 'turn_projection_incremental.py'


def _measure():
    spec = importlib.util.spec_from_file_location('turn_projection_incremental', _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_digest_is_deterministic_and_independent_of_gil_load():
    measure = _measure()
    idle = measure.run_worker(4, 1, 0, 3)
    again = measure.run_worker(4, 1, 0, 3)
    loaded = measure.run_worker(4, 1, 1, 3)
    assert idle['digest'] == again['digest'] == loaded['digest']
    assert idle['settlePoints'] is True
    assert idle['events'] == 4
    assert set(idle['medianMs']) == {'capture', 'project', 'patch', 'total'}
    assert set(idle['kindMedianMs']) == {'tool_start', 'tool_result', 'round_end', 'phase'}
    assert measure.run_worker(5, 1, 0, 3)['digest'] != idle['digest']


def test_append_worker_charges_identically_under_gil_load():
    measure = _measure()
    idle = measure.run_append_worker(4, 1, 0, 3)
    loaded = measure.run_append_worker(4, 1, 1, 3)
    # The digest covers every admitted charge and captured structure.
    assert idle['digest'] == loaded['digest']
    assert idle['admitted'] == loaded['admitted'] > len(measure._APPEND_CYCLE)
    assert idle['events'] == len(measure._APPEND_CYCLE)
    assert set(idle['medianCpuMs']) == {'append', 'structural', 'progress', 'capture'}
    assert set(idle['kindMedianCpuMs']) == set(measure._APPEND_CYCLE)
    assert measure.run_append_worker(5, 1, 0, 3)['digest'] != idle['digest']


@pytest.mark.parametrize('entry', ['projection', 'append'])
def test_driver_refuses_clocks_when_trees_do_unequal_work(monkeypatch, capsys, entry):
    measure = _measure()
    digests = iter(['a' * 64, 'b' * 64])

    def fake_arm(_root, size, events, busy, seed, entry='projection'):
        return {'size': size, 'busy': busy, 'events': events, 'seed': seed,
                'digest': next(digests), 'medianMs': {}, 'meanMs': {},
                'kindMedianMs': {}, 'settlePoints': True, 'source': '', 'python': '',
                'pid': 0}

    monkeypatch.setattr(measure, '_run_arm', fake_arm)
    with pytest.raises(SystemExit, match='different projections'):
        measure.main(['--source-root', str(_PATH.parents[1]), '--sizes', '4', '--busy', '0',
                      '--entry', entry])
    assert 'capture' not in capsys.readouterr().out
