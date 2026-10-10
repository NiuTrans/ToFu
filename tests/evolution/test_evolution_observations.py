"""Observation ledger: durable noticings, zero learning rights, bounded."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _obs(obs_id, *, kind='premise_objection', subject=None, status='open',
         statement='grep source recovery is not addressable'):
    return {
        'schema_version': 1,
        'observation_id': obs_id,
        'kind': kind,
        'statement': statement,
        'subject': subject or {'type': 'tool', 'id': 'grep_search'},
        'lineage': ['e1', 't1'],
        'source': {'actor': 'executor', 'task_id': 't1'},
        'status': status,
        'created_at': 1,
        'updated_at': 1,
    }


def test_record_is_validated_and_idempotent(store):
    from lib import evolution
    record = evolution.record_observation(_obs('obs-a'), user_id=USER, now=5)
    assert record['observation_id'] == 'obs-a'
    assert record['updated_at'] == 5
    again = evolution.record_observation(_obs('obs-a'), user_id=USER, now=9)
    assert again['created_at'] == 1  # the original record, untouched
    assert len(evolution.list_observations(user_id=USER)) == 1


def test_conflicting_id_is_refused(store):
    from lib import evolution
    evolution.record_observation(_obs('obs-a'), user_id=USER)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.record_observation(
            _obs('obs-a', statement='different content'), user_id=USER)


def test_contract_rejects_bad_kind_and_missing_fields(store):
    from lib import evolution
    with pytest.raises(evolution.EvolutionContractError):
        evolution.record_observation(_obs('obs-bad', kind='hunch'),
                                   user_id=USER)
    broken = _obs('obs-broken')
    del broken['lineage']
    with pytest.raises(evolution.EvolutionContractError):
        evolution.record_observation(broken, user_id=USER)


def test_capacity_refuses_never_evicts(store, monkeypatch):
    from lib import evolution
    from lib.evolution import observations
    monkeypatch.setattr(observations, 'MAX_OBSERVATIONS', 2)
    evolution.record_observation(_obs('obs-1'), user_id=USER)
    evolution.record_observation(_obs('obs-2'), user_id=USER)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.record_observation(_obs('obs-3'), user_id=USER)
    assert len(evolution.list_observations(user_id=USER)) == 2


def test_clusters_group_by_kind_and_subject(store):
    from lib import evolution
    evolution.record_observation(_obs('obs-1'), user_id=USER, now=5)
    evolution.record_observation(
        _obs('obs-2', statement='same tool, second noticing'), user_id=USER,
        now=9)
    evolution.record_observation(
        _obs('obs-3', subject={'type': 'contract', 'id': 'tool_policy'}),
        user_id=USER)
    evolution.record_observation(
        _obs('obs-4', kind='friction', status='dismissed'), user_id=USER)
    clusters = evolution.cluster_observations(user_id=USER)
    assert len(clusters) == 2  # dismissed never clusters
    top = clusters[0]
    assert top['kind'] == 'premise_objection'
    assert top['subject'] == {'type': 'tool', 'id': 'grep_search'}
    assert top['count'] == 2
    assert top['observation_ids'] == ['obs-2', 'obs-1']
    kinds = [c['kind'] for c in evolution.cluster_observations(
        user_id=USER, kinds=('friction',))]
    assert kinds == []  # obs-4 is dismissed


def test_status_transitions_are_owner_driven(store):
    from lib import evolution
    evolution.record_observation(_obs('obs-a'), user_id=USER)
    parked = evolution.set_observation_status('obs-a', 'dismissed',
                                              user_id=USER)
    assert parked['status'] == 'dismissed'
    assert evolution.cluster_observations(user_id=USER) == []
    with pytest.raises(evolution.EvolutionStateError):
        evolution.set_observation_status('obs-a', 'bogus', user_id=USER)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.set_observation_status('obs-missing', 'dismissed',
                                         user_id=USER)


def test_deterministic_id_helper(store):
    from lib.evolution.observations import observation_id
    first = observation_id('friction', {'type': 'tool', 'id': 'grep_search'},
                           'statement', salt='sig|e1')
    second = observation_id('friction', {'type': 'tool', 'id': 'grep_search'},
                            'statement', salt='sig|e1')
    assert first == second and first.startswith('obs-')
    assert observation_id('friction', {'type': 'tool', 'id': 'other'},
                          'statement', salt='sig|e1') != first
