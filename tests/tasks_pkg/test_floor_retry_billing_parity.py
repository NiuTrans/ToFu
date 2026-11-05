#!/usr/bin/env python3
"""Real FloorRetry transport and client/gateway billing parity.

FakeBilledGateway counts the provider's charges using pinned prices. The real
round dispatcher, stream manager and cost arithmetic must report every paid
attempt, including discarded resends. Disabling the mitigation remains the
control case. test_fallback_accounting covers every round path and post-loop
synthesis through behavior, without depending on source layout.
"""
from __future__ import annotations

import ast
import os
import sys
import threading as _thr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Quart is the async Flask shim used elsewhere in the suite; keep it wired
# before any project import that pulls the flask compatibility alias.
import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


# ── Fake gateway ledger ────────────────────────────────────────────────────
#
# Anthropic pricing convention used by lib/cost.py:compute_cost
# (input=uncached, cache_write=1.25× base, cache_read=0.10× base, output=out_p).
# We use PLACEHOLDER rates that (a) are non-zero, (b) exercise the write/read
# multiplier arithmetic, (c) match what get_pricing_data returns after we
# monkeypatch it. The parity assertion is arithmetic-independent — it only
# cares that BOTH sides use the SAME rates.

_BASE_INPUT_PER_1M_USD = 15.0   # made-up but plausible input price
_OUTPUT_PER_1M_USD = 75.0
_CACHE_WRITE_MUL = 1.25
_CACHE_READ_MUL = 0.10


def _bill_usd(usage: dict) -> float:
    """Compute the USD the gateway would bill for ONE billed round.
    Mirrors ``compute_cost`` Anthropic path; kept small on purpose."""
    inp = int(usage.get('prompt_tokens') or usage.get('input_tokens') or 0)
    out = int(usage.get('completion_tokens') or usage.get('output_tokens') or 0)
    cw = int(usage.get('cache_creation_input_tokens') or 0)
    cr = int(usage.get('cache_read_tokens') or 0)
    # Anthropic convention: inp is UNCACHED residual (inp << cw+cr).
    uncached = inp
    return (
        (uncached * _BASE_INPUT_PER_1M_USD) / 1e6
        + (cw * _BASE_INPUT_PER_1M_USD * _CACHE_WRITE_MUL) / 1e6
        + (cr * _BASE_INPUT_PER_1M_USD * _CACHE_READ_MUL) / 1e6
        + (out * _OUTPUT_PER_1M_USD) / 1e6
    )


class FakeBilledGateway:
    """Scripted dispatcher that records EVERY request it processed.

    ``.billed_usd`` is what a real gateway would charge — one bill per
    ``__call__`` because every request the mock 'accepts' is one the gateway
    would have accepted. This is the truth the client-reported cost MUST
    match after honest accounting lands."""

    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self._billed = []  # list[dict] — one per accepted request
        self.calls = 0

    def __call__(self, body, **kwargs):
        self.calls += 1
        i = min(self._i, len(self._script) - 1)
        self._i += 1
        step = self._script[i]
        if isinstance(step, Exception):
            # Gateway REJECTED (e.g. 503) — no bill accrued for this attempt.
            raise step
        usage = dict(step)
        # A resend was accepted by the gateway → BILLED, even if the client
        # decides to discard the response text.
        self._billed.append(usage)
        return ({'role': 'assistant', 'content': 'ok'}, 'stop', usage)

    @property
    def billed_usd(self) -> float:
        return sum(_bill_usd(u) for u in self._billed)


def _seed_wire_fp(conv_id: str, fp):
    from lib.tasks_pkg.cache_tracking._state import (
        _cache_lock,
        _cache_states,
    )
    from lib.tasks_pkg.cache_tracking._state import CacheState, _state_key
    key = _state_key(conv_id, user_id=1)
    with _cache_lock:
        st = _cache_states.get(key)
        if st is None:
            st = CacheState()
            _cache_states[key] = st
        st.wire_fp = list(fp)


def _mk_task(conv_id: str = 'billing-parity') -> dict:
    return {
        'id': 'task-parity',
        'convId': conv_id,
        '_userId': 1,
        'content': '',
        'thinking': '',
        'config': {},
        'events': [],
        'content_lock': _thr.Lock(),
        'events_lock': _thr.Lock(),
    }


def _body() -> dict:
    return {'model': 'aws.claude-opus-4.8',
            'messages': [{'role': 'system', 'content': 'S'},
                         {'role': 'user', 'content': 'go'}]}


# Floor-collapse usage: full body billed as cache_write; small cache_read.
_FLOOR = {
    'prompt_tokens': 10,
    'output_tokens': 200,
    'cache_read_tokens': 28_000,
    'cache_creation_input_tokens': 150_000,
    '_wire_fp': [{'k': 'a'}],
}
# Recovered usage: cache hit — most of the body reads from cache, tiny write.
_HIT = {
    'prompt_tokens': 10,
    'output_tokens': 200,
    'cache_read_tokens': 178_000,
    'cache_creation_input_tokens': 1_200,
    '_wire_fp': [{'k': 'a'}],
}


@pytest.fixture
def pinned_pricing(monkeypatch):
    """Pin lib.cost to the same made-up rates the FakeBilledGateway uses so
    the ``compute_cost == billed_usd`` parity is a pure arithmetic identity
    (no dependency on the shipped pricing table). If someone changes the
    pricing table this test still passes as long as BOTH sides agree — that
    is the exact invariant we care about.
    """
    from lib import cost as _cost

    def _fake_get_pricing_data():
        return {
            'usdToCny': 1.0,   # irrelevant for the USD parity assertion
            'inputPrice': _BASE_INPUT_PER_1M_USD,
            'outputPrice': _OUTPUT_PER_1M_USD,
            'cacheWriteMul': _CACHE_WRITE_MUL,
            'cacheReadMul': _CACHE_READ_MUL,
        }

    def _fake_lookup_pricing(_model_id, _provider_id=None, **_kwargs):
        return {
            'input': _BASE_INPUT_PER_1M_USD,
            'output': _OUTPUT_PER_1M_USD,
            'cacheWriteMul': _CACHE_WRITE_MUL,
            'cacheReadMul': _CACHE_READ_MUL,
        }

    monkeypatch.setattr(_cost, 'get_pricing_data', _fake_get_pricing_data)
    monkeypatch.setattr(_cost, 'lookup_pricing', _fake_lookup_pricing)


# ── Guard 1: end-to-end gateway-ledger PARITY ──────────────────────────────


def _drive_primary_path(monkeypatch, script, conv_id: str):
    """Real _llm_call_with_fallback → real stream_llm_response → recorded
    api_rounds + accumulated_usage. Returns (accumulated, api_rounds, gateway)."""
    import lib.tasks_pkg.manager._stream as _mgr
    import lib.tasks_pkg.llm_fallback._call as _fb
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '1')
    # MAX=2 matches the production default; primary + up to 2 resends.
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY_MAX', '2')
    _seed_wire_fp(conv_id, [{'k': 'a'}])
    gateway = FakeBilledGateway(script)
    _orig = _mgr.dispatch_stream
    _mgr.dispatch_stream = gateway
    try:
        task = _mk_task(conv_id)
        api_rounds: list = []
        accumulated: dict = {}
        result = _fb._llm_call_with_fallback(
            task, _body(), 'aws.claude-opus-4.8', 0, 4096,
            tool_call_happened=False, tool_list=None,
            messages=_body()['messages'], preset='opus',
            thinking_enabled=False,
            accumulated_usage=accumulated, api_rounds=api_rounds,
            on_tool_call_ready=None,
        )
        assert result['_loop_action'] is None
    finally:
        _mgr.dispatch_stream = _orig
    return accumulated, api_rounds, gateway


def test_parity_primary_path_recovered_resend(monkeypatch, pinned_pricing):
    """Primary path: floor → recovered. Gateway billed 2, client MUST report 2.
    NEUTERing _stream.py preservation → parity break of ~10 dollars per turn
    on this synthetic case (the discarded 150 000-token cache_write vanishes)."""
    from lib.cost import compute_cost
    accumulated, api_rounds, gateway = _drive_primary_path(
        monkeypatch, [_FLOOR, _HIT], 'parity-primary')

    # Every billed request must appear in api_rounds exactly once.
    assert gateway.calls == 2, f'expected primary + 1 resend; got {gateway.calls}'
    assert len(api_rounds) == 2, (
        f'api_rounds must record BOTH billed requests; got {len(api_rounds)}: '
        f'{[r.get("tag") for r in api_rounds]}')
    assert api_rounds[0].get('responseAuthoring', True) is True
    assert api_rounds[1]['responseAuthoring'] is False

    reported = compute_cost(accumulated, model_id='aws.claude-opus-4.8')
    assert reported is not None
    reported_usd = reported['costUsd']
    gateway_usd = gateway.billed_usd

    # Sub-cent parity: this is a pure-arithmetic identity when honest.
    assert abs(reported_usd - gateway_usd) < 1e-4, (
        f'PARITY BROKEN — client reports ${reported_usd:.4f} but gateway '
        f'billed ${gateway_usd:.4f} (diff ${gateway_usd - reported_usd:+.4f}). '
        f'accumulated={accumulated}')


def test_parity_primary_path_all_floored(monkeypatch, pinned_pricing):
    """Worst-case shape: all N attempts stay floored. Gateway billed N,
    client MUST report N. This is the shape production 8427 saw last night
    (Task 65c6976c R21 cycle #59117 → every collapse charged 1.25× three
    times before the loop gave up)."""
    from lib.cost import compute_cost
    accumulated, api_rounds, gateway = _drive_primary_path(
        monkeypatch,
        [_FLOOR, dict(_FLOOR, cache_creation_input_tokens=149_000),
         dict(_FLOOR, cache_creation_input_tokens=148_500)],
        'parity-all-floored')

    assert gateway.calls == 3, 'primary + 2 resends when nothing recovers'
    assert len(api_rounds) == 3, (
        f'api_rounds must include ALL 3 gateway-billed attempts; '
        f'got {len(api_rounds)}: {[r.get("tag") for r in api_rounds]}')

    reported_usd = compute_cost(accumulated, model_id='aws.claude-opus-4.8')['costUsd']
    gateway_usd = gateway.billed_usd
    assert abs(reported_usd - gateway_usd) < 1e-4, (
        f'PARITY BROKEN on all-floored case — reported ${reported_usd:.4f} '
        f'vs gateway ${gateway_usd:.4f}. This shape is the 8427 R21 loop; '
        f'silent under-billing here compounds hourly.')


def test_parity_primary_path_no_resend_when_disabled(monkeypatch, pinned_pricing):
    """When the mitigation is OFF (2026-07-23 default), gateway.calls==1 and
    parity must still hold. Serves as the NEUTER control for the WHOLE
    honest-accounting seam — the code paths that append discards must be
    inert when there is nothing to discard."""
    from lib.cost import compute_cost
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '0')
    import lib.tasks_pkg.manager._stream as _mgr
    import lib.tasks_pkg.llm_fallback._call as _fb
    _seed_wire_fp('parity-off', [{'k': 'a'}])
    gateway = FakeBilledGateway([_FLOOR])
    _orig = _mgr.dispatch_stream
    _mgr.dispatch_stream = gateway
    try:
        task = _mk_task('parity-off')
        api_rounds: list = []
        accumulated: dict = {}
        _fb._llm_call_with_fallback(
            task, _body(), 'aws.claude-opus-4.8', 0, 4096,
            tool_call_happened=False, tool_list=None,
            messages=_body()['messages'], preset='opus',
            thinking_enabled=False,
            accumulated_usage=accumulated, api_rounds=api_rounds,
            on_tool_call_ready=None,
        )
    finally:
        _mgr.dispatch_stream = _orig

    assert gateway.calls == 1, 'mitigation off = exactly one request'
    assert len(api_rounds) == 1
    reported_usd = compute_cost(accumulated, model_id='aws.claude-opus-4.8')['costUsd']
    assert abs(reported_usd - gateway.billed_usd) < 1e-4


def test_default_gate_stays_off(monkeypatch):
    """A future commit that flips the default back to '1' without landing
    real TCO evidence must fail here. Kept out of the other test file so a
    partial revert cannot dodge both guards."""
    from lib.tasks_pkg import floor_retry as fr
    monkeypatch.delenv('TOFU_CACHE_FLOOR_RETRY', raising=False)
    assert fr.floor_retry_enabled() is False, (
        'floor-retry default must be OFF — the honest-accounting parity '
        'tests show every resend is a strict expected-cost loss until a '
        'real production A/B (now measurable) proves otherwise.')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
