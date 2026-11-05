"""Conversation-to-key affinity across failover retries and re-mints.

Anthropic's prompt cache is keyed per upstream API key. A conversation that
failed over onto a second credential is warm ONLY there; routing its next
turn back to the compiler's rank-0 credential re-bills the whole prefix.

Two mechanisms break that affinity on model-routing v2 request-scoped route
groups (10-24/10-25 production logs: CACHE NAMESPACE SWITCH + prefix re-writes
at turn boundaries with no failure in the conversation's own lane):

1. Affinity is recorded against ``Slot.key_name`` — a request-random
   ``ephemeral_<16hex>`` handle minted per turn, so the recorded key never
   exists in the next turn's freshly minted group. The sticky lookup misses
   and the picker falls back to ``route_rank`` order, silently undoing a
   mid-turn failover rebind at the next turn boundary.
2. ``execute_invocation`` wraps EVERY invocation in ``isolated_conv_affinity``
   and discards the scope's routing entries on exit. A root invocation's
   scope IS the conversation id, so a root turn's warm key is discarded at
   turn end — contradicting the documented contract that the map persists
   across turns.
"""
from __future__ import annotations

import threading

import pytest

from lib.llm_dispatch.slot import Slot

pytestmark = pytest.mark.unit

_PIN = 'model-route:1:testpin'


def _routed_slot(key_name, *, credential, deployment='dep-x', rank=0):
    """A v2 request-scoped slot the way mint_routed_slot_group shapes it."""
    return Slot(
        key_name=key_name, api_key=f'sk-{credential}-{key_name}',
        model='wire-alpha', logical_model='alpha', capabilities={'text'},
        provider_id=_PIN, request_scoped=True, routing_provider_id='provider-a',
        routing_owner_user_id=1, route_offering_id='off-x',
        route_connection_id='conn-x', route_credential_id=credential,
        route_deployment_id=deployment, route_rank=rank)


@pytest.fixture
def dispatcher():
    from lib.llm_dispatch import conv_affinity
    from lib.llm_dispatch.factory import get_dispatcher

    conv_affinity.clear_conv_affinity()
    conv_affinity._conv_keys.clear()
    d = get_dispatcher()
    d.initialize()
    slots = []

    def reset_group(group_slots):
        with d._lock:
            for s in slots:
                if s in d.slots:
                    d.slots.remove(s)
            slots[:] = list(group_slots)
            d.slots.extend(slots)

    yield d, reset_group

    conv_affinity.clear_conv_affinity()
    conv_affinity._conv_keys.clear()
    with d._lock:
        for s in slots:
            if s in d.slots:
                d.slots.remove(s)


def test_failover_identity_survives_route_group_remint(dispatcher, monkeypatch):
    """Turn 2's fresh mint must land on the credential turn 1 failed over to."""
    from lib.llm_dispatch import conv_affinity
    from lib.llm_dispatch.provider_pin import provider_pin

    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
    d, reset_group = dispatcher

    def pick(**kwargs):
        return d.pick_slot(capability='text', prefer_model='alpha',
                           strict_model=True, **kwargs)

    # ── Turn 1: rank-0 credential-a is picked, fails, failover rebinds to b ──
    reset_group([_routed_slot('ephemeral_t1_a', credential='credential-a', rank=0),
                 _routed_slot('ephemeral_t1_b', credential='credential-b', rank=1)])
    with provider_pin(_PIN):
        conv_affinity.set_conv_affinity('conv-aff')
        try:
            assert pick().key_name == 'ephemeral_t1_a'
            # The recorded affinity is the stable route identity, not the
            # request-random key_name of this turn's mint.
            assert conv_affinity.get_preferred_key(
                'conv-aff', route_key='alpha') == 'credential-a:dep-x'
            # The failover retry excludes the failed (key, model) pair.
            rebound = pick(exclude_pairs={('ephemeral_t1_a', 'wire-alpha')})
            assert rebound.key_name == 'ephemeral_t1_b'
            assert conv_affinity.get_preferred_key(
                'conv-aff', route_key='alpha') == 'credential-b:dep-x'
        finally:
            conv_affinity.clear_conv_affinity()

    # ── Turn 2: a fresh group re-mints the same credentials under NEW
    #    request-random key names. Rank order still prefers credential-a. ──
    reset_group([_routed_slot('ephemeral_t2_a', credential='credential-a', rank=0),
                 _routed_slot('ephemeral_t2_b', credential='credential-b', rank=1)])
    with provider_pin(_PIN):
        conv_affinity.set_conv_affinity('conv-aff')
        try:
            # The conversation is warm ONLY on credential-b; the fresh mint
            # must land there, not on the rank-0 credential-a.
            assert pick().key_name == 'ephemeral_t2_b'
        finally:
            conv_affinity.clear_conv_affinity()


def test_affinity_never_sticks_to_failed_or_excluded_identity(dispatcher, monkeypatch):
    """A rebind must never resurrect an excluded or cooled credential."""
    import time

    from lib.llm_dispatch import conv_affinity
    from lib.llm_dispatch.provider_pin import provider_pin

    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
    d, reset_group = dispatcher

    a = _routed_slot('ephemeral_f_a', credential='credential-a', rank=0)
    b = _routed_slot('ephemeral_f_b', credential='credential-b', rank=1)
    reset_group([a, b])
    with provider_pin(_PIN):
        conv_affinity.set_conv_affinity('conv-safe')
        try:
            assert d.pick_slot(capability='text', prefer_model='alpha',
                               strict_model=True).key_name == 'ephemeral_f_a'
            # Sticky on b after the failover rebind...
            rebound = d.pick_slot(
                capability='text', prefer_model='alpha', strict_model=True,
                exclude_pairs={('ephemeral_f_a', 'wire-alpha')})
            assert rebound.key_name == 'ephemeral_f_b'
            # ...but when credential-b's slot cools down (failed), the picker
            # must route AROUND it even though the sticky entry names it.
            b.cooldown_until = time.time() + 1000
            assert d.pick_slot(capability='text', prefer_model='alpha',
                               strict_model=True).key_name == 'ephemeral_f_a'
            # And an explicit exclusion of the sticky slot is honoured too.
            b.cooldown_until = 0.0
            chosen = d.pick_slot(
                capability='text', prefer_model='alpha', strict_model=True,
                exclude_pairs={('ephemeral_f_b', 'wire-alpha')})
            assert chosen.key_name == 'ephemeral_f_a'
        finally:
            conv_affinity.clear_conv_affinity()


def test_affinity_scoped_to_pinned_provider_group(dispatcher, monkeypatch):
    """An identity recorded in one request's group must not leak into another
    request's group minted from the same credential."""
    from lib.llm_dispatch import conv_affinity
    from lib.llm_dispatch.provider_pin import provider_pin

    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
    d, reset_group = dispatcher

    # Request 1's group: a failover lands the conv on credential-b.
    reset_group([_routed_slot('ephemeral_g1_a', credential='credential-a', rank=0),
                 _routed_slot('ephemeral_g1_b', credential='credential-b', rank=1)])
    with provider_pin(_PIN):
        conv_affinity.set_conv_affinity('conv-g1')
        try:
            assert d.pick_slot(
                capability='text', prefer_model='alpha', strict_model=True,
                exclude_pairs={('ephemeral_g1_a', 'wire-alpha')}
            ).key_name == 'ephemeral_g1_b'
        finally:
            conv_affinity.clear_conv_affinity()

    # Request 2's group (different pin) shares credential-b but under a
    # different deployment; its rank-0 is its own credential-a slot.
    other_pin = 'model-route:1:other'
    own_a = _routed_slot('ephemeral_g2_a', credential='credential-a', rank=0)
    shared_b = _routed_slot('ephemeral_g2_b', credential='credential-b',
                            deployment='dep-other', rank=1)
    own_a.provider_id = other_pin
    shared_b.provider_id = other_pin
    reset_group([own_a, shared_b])
    with provider_pin(other_pin):
        conv_affinity.set_conv_affinity('conv-g2')
        try:
            # No affinity recorded for conv-g2: rank order decides.
            assert d.pick_slot(capability='text', prefer_model='alpha',
                               strict_model=True).key_name == 'ephemeral_g2_a'
        finally:
            conv_affinity.clear_conv_affinity()
    # conv-g1's recorded affinity survives, scoped to its own pin.
    from lib.llm_dispatch.conv_affinity import get_preferred_key
    assert get_preferred_key('conv-g1', route_key='alpha') is not None


def test_root_invocation_keeps_conversation_key_affinity(monkeypatch):
    """A root invocation's scope IS the conversation: turn end must NOT wipe
    the recorded warm key (children still discard their ephemeral scope)."""
    from lib.llm_dispatch import conv_affinity
    from lib.tasks_pkg import invocation
    from lib.tasks_pkg.orchestrator import _run

    conv_affinity.clear_conv_affinity()
    conv_affinity._conv_keys.clear()
    try:
        def execute(task, *, policy):
            conv_affinity.record_conv_key(
                task['convId'], 'warm-key', route_key='fixture-model')
            _run.finalize_task_lane(task, tid=task['id'][:8])

        monkeypatch.setattr(_run, '_execute_task_invocation', execute)
        root = {'id': 'affinity-root-task', 'convId': 'affinity-root-conv',
                '_userId': 71, '_transientRuntime': True, 'config': {},
                'abort_event': threading.Event()}
        invocation.execute_invocation(
            root, policy=invocation.ROOT_INVOCATION_POLICY)
        assert conv_affinity.get_preferred_key(
            'affinity-root-conv', route_key='fixture-model') == 'warm-key'
    finally:
        conv_affinity.clear_conv_affinity()
        conv_affinity._conv_keys.clear()
