"""Tests for lib/llm_dispatch/big_prefix_gate.py — per-key big-prefix admission.

Guards the client-side root fix for the single-key LRU prompt-cache eviction:
big concurrent prefixes on ONE api key must be bounded so they don't evict each
other's cache. Lightweight, no DB, no network — pure semaphore + estimator.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_big_prefix_admission.py
"""

import importlib
import os
import threading
import time

import pytest

pytestmark = pytest.mark.unit

MOD = 'lib.llm_dispatch.big_prefix_gate'


@pytest.fixture
def gate(monkeypatch):
    """Fresh module import with a clean per-key semaphore registry each test."""
    import lib.llm_dispatch.big_prefix_gate as g
    importlib.reload(g)
    # Deterministic defaults; individual tests override via monkeypatch.
    for k in ('TOFU_BIG_PREFIX_GATE', 'TOFU_BIG_PREFIX_THRESHOLD_TOKENS',
              'TOFU_BIG_PREFIX_MAX_PER_KEY', 'TOFU_BIG_PREFIX_WAIT_MS'):
        monkeypatch.delenv(k, raising=False)
    return g


# ── estimate_prefix_tokens ──

def test_estimate_counts_string_content(gate):
    msgs = [{'role': 'user', 'content': 'x' * 4000}]
    assert gate.estimate_prefix_tokens(msgs) == 1000  # 4000 chars / 4


def test_estimate_counts_block_text_and_thinking(gate):
    msgs = [{'role': 'assistant', 'content': [
        {'type': 'text', 'text': 'a' * 400},
        {'type': 'thinking', 'thinking': 'b' * 400},
    ]}]
    assert gate.estimate_prefix_tokens(msgs) == 200  # 800 / 4


def test_estimate_counts_image_base64_payload(gate):
    msgs = [{'role': 'user', 'content': [
        {'type': 'image', 'source': {'type': 'base64', 'data': 'Z' * 4000}},
    ]}]
    assert gate.estimate_prefix_tokens(msgs) == 1000


def test_estimate_accepts_body_dict_with_tools(gate):
    body = {'messages': [{'role': 'user', 'content': 'hi'}],
            'tools': [{'name': 'x', 'description': 'd' * 400}]}
    est = gate.estimate_prefix_tokens(body)
    assert est > 90  # tools JSON dominates


def test_estimate_malformed_returns_zero(gate):
    assert gate.estimate_prefix_tokens(None) == 0
    assert gate.estimate_prefix_tokens(42) == 0


# ── gating decision: small requests never block ──

def test_small_request_is_noop_even_at_capacity(gate, monkeypatch):
    monkeypatch.setenv('TOFU_BIG_PREFIX_MAX_PER_KEY', '1')
    monkeypatch.setenv('TOFU_BIG_PREFIX_THRESHOLD_TOKENS', '150000')
    # Occupy the (would-be) single slot with a big holder.
    held = threading.Event()
    release = threading.Event()

    def _big():
        with gate.big_prefix_slot('key_0', 200000):
            held.set()
            release.wait(2)

    t = threading.Thread(target=_big, daemon=True)
    t.start()
    assert held.wait(1)
    # A SMALL request must pass instantly despite the big one holding the slot.
    t0 = time.time()
    with gate.big_prefix_slot('key_0', 1000):  # below threshold → no-op
        pass
    assert time.time() - t0 < 0.3
    release.set()
    t.join(2)


def test_disabled_gate_is_noop(gate, monkeypatch):
    monkeypatch.setenv('TOFU_BIG_PREFIX_GATE', '0')
    monkeypatch.setenv('TOFU_BIG_PREFIX_MAX_PER_KEY', '1')
    monkeypatch.setenv('TOFU_BIG_PREFIX_THRESHOLD_TOKENS', '10')
    # Two big requests on the same key must NOT serialize when disabled.
    order = []
    rel = threading.Event()

    def _hold():
        with gate.big_prefix_slot('key_0', 999999):
            order.append('in')
            rel.wait(2)

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    while 'in' not in order:
        time.sleep(0.01)
    t0 = time.time()
    with gate.big_prefix_slot('key_0', 999999):
        pass
    assert time.time() - t0 < 0.3  # not blocked
    rel.set()
    t.join(2)


# ── per-key concurrency cap ──

def test_big_requests_serialize_beyond_cap(gate, monkeypatch):
    monkeypatch.setenv('TOFU_BIG_PREFIX_MAX_PER_KEY', '1')
    monkeypatch.setenv('TOFU_BIG_PREFIX_THRESHOLD_TOKENS', '100')
    monkeypatch.setenv('TOFU_BIG_PREFIX_WAIT_MS', '2000')

    events = []
    holder_in = threading.Event()
    holder_release = threading.Event()

    def _holder():
        with gate.big_prefix_slot('key_0', 200000):
            events.append('holder_in')
            holder_in.set()
            holder_release.wait(3)
            events.append('holder_out')

    t = threading.Thread(target=_holder, daemon=True)
    t.start()
    assert holder_in.wait(1)

    # Second big request on SAME key must block until holder releases.
    entered = threading.Event()

    def _waiter():
        with gate.big_prefix_slot('key_0', 200000):
            events.append('waiter_in')
            entered.set()

    w = threading.Thread(target=_waiter, daemon=True)
    w.start()
    # Give the waiter a moment; it must NOT have entered yet.
    assert not entered.wait(0.4)
    holder_release.set()
    assert entered.wait(2)
    assert events.index('holder_out') < events.index('waiter_in')
    t.join(2)
    w.join(2)


def test_different_keys_do_not_block_each_other(gate, monkeypatch):
    monkeypatch.setenv('TOFU_BIG_PREFIX_MAX_PER_KEY', '1')
    monkeypatch.setenv('TOFU_BIG_PREFIX_THRESHOLD_TOKENS', '100')

    in0 = threading.Event()
    rel = threading.Event()

    def _hold_key0():
        with gate.big_prefix_slot('key_0', 200000):
            in0.set()
            rel.wait(2)

    t = threading.Thread(target=_hold_key0, daemon=True)
    t.start()
    assert in0.wait(1)
    # A big request on key_1 must proceed while key_0 is held.
    t0 = time.time()
    with gate.big_prefix_slot('key_1', 200000):
        pass
    assert time.time() - t0 < 0.3
    rel.set()
    t.join(2)


# ── single-key no-op (route B) ──

def test_single_key_model_is_noop(gate, monkeypatch):
    """key_count<=1 → gate skips entirely: two big requests on the SAME key do
    NOT serialize, because gating one shared pool only adds latency."""
    monkeypatch.setenv('TOFU_BIG_PREFIX_MAX_PER_KEY', '1')
    monkeypatch.setenv('TOFU_BIG_PREFIX_THRESHOLD_TOKENS', '100')
    monkeypatch.setenv('TOFU_BIG_PREFIX_WAIT_MS', '5000')

    in0 = threading.Event()
    rel = threading.Event()

    def _hold():
        with gate.big_prefix_slot('key_0', 200000, conv_id='cA', key_count=1):
            in0.set()
            rel.wait(3)

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    assert in0.wait(1)
    # Second big request on the same single key must pass instantly (no gating).
    t0 = time.time()
    with gate.big_prefix_slot('key_0', 200000, conv_id='cB', key_count=1):
        pass
    assert time.time() - t0 < 0.3, 'single-key model must not serialize'
    rel.set()
    t.join(3)


def test_NEUTER_single_key_without_hint_still_serializes(gate, monkeypatch):
    """NEUTER / control: the SAME two big requests, but WITHOUT the key_count
    hint (key_count=None, the legacy default) DO serialize — proving the no-op
    is driven by the key_count signal, not by something else. This is exactly
    the wasteful behavior route B removes on single-key models."""
    monkeypatch.setenv('TOFU_BIG_PREFIX_MAX_PER_KEY', '1')
    monkeypatch.setenv('TOFU_BIG_PREFIX_RESIDENCY_MAX', '1')
    monkeypatch.setenv('TOFU_BIG_PREFIX_THRESHOLD_TOKENS', '100')
    monkeypatch.setenv('TOFU_BIG_PREFIX_RESIDENCY_WAIT_MS', '400')

    in0 = threading.Event()
    rel = threading.Event()

    def _hold():
        with gate.big_prefix_slot('key_0', 200000, conv_id='cA'):  # no key_count
            in0.set()
            rel.wait(3)

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    assert in0.wait(1)
    # A DISTINCT conv (new competitor) on the saturated single-key working set
    # must wait the (short) residency budget before degrading through.
    t0 = time.time()
    with gate.big_prefix_slot('key_0', 200000, conv_id='cB'):  # no key_count
        elapsed = time.time() - t0
    assert elapsed >= 0.35, 'without the key_count hint the gate still gates'
    rel.set()
    t.join(3)


def test_multi_key_model_still_gates(gate, monkeypatch):
    """key_count>=2 → gate still active: a distinct big conv on a saturated
    single-key working set waits (route B only disables the SINGLE-key case)."""
    monkeypatch.setenv('TOFU_BIG_PREFIX_MAX_PER_KEY', '1')
    monkeypatch.setenv('TOFU_BIG_PREFIX_RESIDENCY_MAX', '1')
    monkeypatch.setenv('TOFU_BIG_PREFIX_THRESHOLD_TOKENS', '100')
    monkeypatch.setenv('TOFU_BIG_PREFIX_RESIDENCY_WAIT_MS', '400')

    in0 = threading.Event()
    rel = threading.Event()

    def _hold():
        with gate.big_prefix_slot('key_0', 200000, conv_id='cA', key_count=2):
            in0.set()
            rel.wait(3)

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    assert in0.wait(1)
    t0 = time.time()
    with gate.big_prefix_slot('key_0', 200000, conv_id='cB', key_count=2):
        elapsed = time.time() - t0
    assert elapsed >= 0.35, 'multi-key model must still gate a distinct conv'
    rel.set()
    t.join(3)


# ── degraded-proceed when wait budget exceeded ──

def test_degraded_proceed_after_budget(gate, monkeypatch, caplog):
    monkeypatch.setenv('TOFU_BIG_PREFIX_MAX_PER_KEY', '1')
    monkeypatch.setenv('TOFU_BIG_PREFIX_THRESHOLD_TOKENS', '100')
    monkeypatch.setenv('TOFU_BIG_PREFIX_WAIT_MS', '200')  # tiny budget

    in0 = threading.Event()
    rel = threading.Event()

    def _hold():
        with gate.big_prefix_slot('key_0', 200000):
            in0.set()
            rel.wait(3)

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    assert in0.wait(1)

    # Second big request can't get a permit within 200ms → proceeds degraded.
    t0 = time.time()
    with gate.big_prefix_slot('key_0', 200000):
        elapsed = time.time() - t0
    # Waited ~the budget then proceeded (well under the holder's 3s hold).
    assert 0.15 <= elapsed < 1.5
    rel.set()
    t.join(3)


# ── env config parsing ──

def test_config_defaults(gate):
    assert gate.gate_enabled() is True
    assert gate.threshold_tokens() == 150000
    assert gate.max_per_key() == 2
    assert gate.wait_budget_ms() == 45000.0


def test_config_overrides(gate, monkeypatch):
    monkeypatch.setenv('TOFU_BIG_PREFIX_THRESHOLD_TOKENS', '50000')
    monkeypatch.setenv('TOFU_BIG_PREFIX_MAX_PER_KEY', '3')
    monkeypatch.setenv('TOFU_BIG_PREFIX_WAIT_MS', '10000')
    assert gate.threshold_tokens() == 50000
    assert gate.max_per_key() == 3
    assert gate.wait_budget_ms() == 10000.0


def test_config_bad_values_fall_back(gate, monkeypatch):
    monkeypatch.setenv('TOFU_BIG_PREFIX_THRESHOLD_TOKENS', 'abc')
    monkeypatch.setenv('TOFU_BIG_PREFIX_MAX_PER_KEY', '0')
    assert gate.threshold_tokens() == 150000
    assert gate.max_per_key() == 2
