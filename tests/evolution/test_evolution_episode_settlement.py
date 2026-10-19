"""Concurrent settlement against actual stores preserves one immutable result."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tests.evolution.test_evolution_episodes import USER, _opened

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.api


@pytest.mark.parametrize('credential_storage', ['sqlite', 'tofudb'], indirect=True)
@pytest.mark.parametrize('conflicting', [False, True])
def test_concurrent_settlement_reconciles_only_identical_intent(
        credential_storage, monkeypatch, conflicting):
    from lib.evolution import episodes, records
    from lib.evolution.contracts import EvolutionStateError

    episode = _opened(None)
    barrier = Barrier(2, timeout=20)
    original = episodes.put_record

    def rendezvous(*args, **kwargs):
        barrier.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(episodes, 'put_record', rendezvous)

    def settle(index):
        try:
            return episodes.settle_episode(episode['episode_id'], user_id=USER,
                status='aborted', actuals={'tokens': 10 + (index if conflicting else 0),
                    'wall_clock_seconds': 1, 'tool_calls': 0},
                actuals_complete=False, now=1700000100 + index, reconcile=True)
        except EvolutionStateError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(settle, range(2)))
    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, EvolutionStateError)]
    assert len(successes) == (1 if conflicting else 2)
    assert len(failures) == (1 if conflicting else 0)
    assert all('conflicts' in str(error) for error in failures)
    key = records.entity_key(episode['episode_id'], USER, kind='episode')
    committed, version = records.get_versioned_record(records.NAMESPACE_EPISODES, key)
    assert version == 2 and all(result == committed for result in successes)
    assert committed['actuals_complete'] is False
    monkeypatch.setattr(episodes, 'put_record', original)
    confirmed = episodes.settle_episode(episode['episode_id'], user_id=USER,
        status='aborted', actuals=committed['actuals'], actuals_complete=False,
        reconcile=True, now=1700000200)
    assert confirmed == committed
    assert records.get_versioned_record(records.NAMESPACE_EPISODES, key) == (committed, version)
