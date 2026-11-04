"""Request retirement scales with its own slots and preserves ordered membership."""
from dataclasses import dataclass
import gc
import random
import weakref

import pytest

from lib.llm_dispatch import slot_pool
from lib.llm_dispatch.slot_pool import SlotPool

pytestmark = pytest.mark.unit


@dataclass
class Member:
    provider_id: str
    credential: str
    request_scoped: bool = True

    def key_stats_provider_id(self):
        return 'owner:23:provider'

    def key_stats_key_name(self):
        return self.credential


def identities(values):
    return [id(value) for value in values]


def check_views(pool, expected):
    assert identities(pool) == identities(expected)
    assert len(pool) == len(expected)
    assert identities(pool.scope(None)) == identities(
        [slot for slot in expected if not slot.request_scoped])
    for pin in ('a', 'b', 'c'):
        assert identities(pool.by_provider(pin)) == identities(
            [slot for slot in expected if slot.provider_id == pin])
    assert pool.key_stats_key_names('owner:23:provider') == tuple(
        sorted({slot.credential for slot in expected}))


@pytest.mark.parametrize('foreign_count', [0, 100, 10000])
def test_retirement_does_not_inspect_unrelated_slot_identities(monkeypatch, foreign_count):
    foreign = [Member('b', 'shared') for _ in range(foreign_count)]
    own = [Member('a', 'shared'), Member('a', 'own')]
    pool = SlotPool(foreign + own)
    observed = []

    def observe_identity(value):
        observed.append(id(value))
        return id(value)

    with monkeypatch.context() as probe:
        probe.setattr(slot_pool, 'id', observe_identity, raising=False)
        assert pool.withdraw(own) == set(identities(own))
    assert set(observed) <= set(identities(own))
    check_views(pool, foreign)


def test_withdraw_is_identity_based_removes_duplicates_and_preserves_prior_views():
    first, equal, other = Member('a', 'shared'), Member('a', 'shared'), Member('b', 'other')
    pool = SlotPool([first, other, equal, first])
    prior = pool.scope('a')
    assert pool.withdraw([first, first, Member('a', 'absent')]) == {id(first)}
    check_views(pool, [other, equal])
    assert identities(prior) == identities([first, equal, first])
    assert pool.withdraw([first]) == set()
    assert pool.withdraw([equal, other]) == {id(equal), id(other)}
    check_views(pool, [])


def test_generic_sequence_edits_and_reload_keep_all_indexes_exact():
    rng = random.Random(23)
    members = [Member(pin, str(i % 3), request_scoped=i % 4 != 0)
               for i, pin in enumerate('abcabcabcabc')]
    pool, expected = SlotPool(), []
    for _ in range(250):
        chosen = rng.choice(members)
        operation = rng.randrange(10)
        if operation == 0:
            pool.publish([chosen, chosen])
            expected.extend([chosen, chosen])
        elif operation == 1:
            removed = {id(s) for s in expected if s is chosen}
            assert pool.withdraw([chosen]) == removed
            expected = [s for s in expected if s is not chosen]
        elif operation == 2:
            pool.insert(0, chosen)
            expected.insert(0, chosen)
        elif operation == 3:
            pool[1:3] = [chosen]
            expected[1:3] = [chosen]
        elif operation == 4:
            del pool[::2]
            del expected[::2]
        elif operation == 5:
            pool.reverse()
            expected.reverse()
        elif operation == 6:
            pool.sort(key=lambda s: s.credential)
            expected.sort(key=lambda s: s.credential)
        elif operation == 7:
            pool += [chosen]
            expected += [chosen]
        elif operation == 8:
            configured = [s for s in members if not s.request_scoped]
            pool.replace_configured(configured)
            expected = configured + [s for s in expected if s.request_scoped]
        elif expected:
            assert pool.pop(-1) is expected.pop(-1)
        check_views(pool, expected)
        assert identities(pool[::-1]) == identities(expected[::-1])
        assert identities(reversed(pool)) == identities(reversed(expected))
        assert pool == expected and expected == pool
    pool *= 2
    expected *= 2
    check_views(pool, expected)
    pool.clear()
    check_views(pool, [])


def test_failed_key_identity_read_does_not_partially_publish():
    class BrokenMember(Member):
        def key_stats_key_name(self):
            raise ValueError('cannot resolve identity')

    survivor = Member('a', 'shared')
    pool = SlotPool([survivor])
    with pytest.raises(ValueError, match='cannot resolve identity'):
        pool.publish([Member('b', 'new'), BrokenMember('c', 'broken')])
    check_views(pool, [survivor])


def test_retired_slots_and_credentials_are_not_retained():
    survivor = Member('b', 'shared')
    retired = Member('a', 'retired')
    ref = weakref.ref(retired)
    pool = SlotPool([retired, survivor, retired])
    pool.withdraw([retired])
    del retired
    gc.collect()
    assert ref() is None
    check_views(pool, [survivor])


def test_last_request_retirement_keeps_recorded_configured_credential_identity():
    configured = Member('a', 'original', request_scoped=False)
    request = Member('b', 'request')
    pool = SlotPool([configured, request])
    configured.credential = 'edited'
    pool.withdraw([request])
    assert pool.key_stats_key_names('owner:23:provider') == ('original',)
    pool.withdraw([configured])
    check_views(pool, [])


def test_diagnostic_iteration_survives_concurrent_retirement_and_publication():
    first, second, third = (Member(pin, pin) for pin in ('a', 'b', 'c'))
    pool = SlotPool([first, second])
    snapshot = iter(pool)
    assert next(snapshot) is first
    pool.withdraw([second])
    pool.publish([third])
    # Diagnostics have a finite snapshot, with no invalidated dict iterator.
    assert list(snapshot) == [second]
    check_views(pool, [first, third])
