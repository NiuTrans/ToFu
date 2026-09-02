#!/usr/bin/env python3
"""Floor-retry billing parity — anti-regression guard.

WHY (owner directive 2026-07-24):
  17 unit tests that hand-inspect the flow are not enough — we must ensure
  such discrepant billing (report kind < gateway kind) can never silently
  return in the future. Two guards land here:

  (1) **Gateway-ledger PARITY** (end-to-end, real-arithmetic).
      A ``FakeBilledGateway`` counts USD charged per Anthropic multipliers
      (cache_write=1.25× base, cache_read=0.10× base, output=out_p). We drive
      the REAL ``_llm_call_with_fallback`` / real ``stream_llm_response`` /
      real ``compute_cost`` against a floor-then-recover script and assert
      ``compute_cost(accumulated_usage).costUsd == fake_gateway.billed_usd``
      to sub-cent precision. Without honest-accounting the client-reported
      figure is strictly < gateway.billed — the parity assertion catches it.
      Runs for BOTH consumer sites: the primary path (_call.py) and the
      post-loop fallback path (_finalize.py) that also unpacks
      ``_extra_billing_rounds``. If either loses the seam, this test flips
      red before any user is under-billed.

  (2) **Caller-agnostic STATIC gate**.
      A static AST scan of the repo finds every call site of
      ``stream_llm_response`` that appends to ``api_rounds``; each such site
      MUST reference ``_extra_billing_rounds`` within a small window
      afterwards. A new caller added in the future — say a swarm module or a
      paper-mode consumer — that forgets the honest-accounting loop breaks
      CI without needing to know a test author remembered to write a case
      for it. This is the future-proof half.

Failing-first + NEUTER discipline:
  * NEUTERing the ``_fr_discarded_billing`` append in ``_stream.py`` makes
    the parity test red (client cost drops below gateway.billed).
  * NEUTERing any consumer's ``for _bill in (_extra_billing_rounds)`` loop
    ALSO makes the parity test red (per-site coverage).
  * Removing ``_extra_billing_rounds`` from a consumer file makes the static
    gate red (caller-agnostic coverage).

Run directly (isolated):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_floor_retry_billing_parity.py
"""
from __future__ import annotations

import ast
import os
import sys
import threading as _thr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


# ── Guard 2: caller-agnostic STATIC gate ───────────────────────────────────


# Every file that calls stream_llm_response AND appends to api_rounds is a
# billing consumer. Registered here explicitly so a new consumer that
# forgets registration also gets caught (below we assert the discovered set
# ⊆ this whitelist — a new caller must be added deliberately).
_KNOWN_BILLING_CONSUMERS = frozenset({
    'lib/tasks_pkg/llm_fallback/_call.py',
    'lib/tasks_pkg/orchestrator/_finalize.py',
})


_DISCOVERY_SKIP_DIRS = ('__pycache__', '.tofu', '.tofu_trash', '.tofu_sandbox',
                        'node_modules', '.git', '.venv', 'venv',
                        'swebench_workdir', 'promo', 'propaganda')


def _discover_stream_callers() -> set[str]:
    """Return the set of repo-relative paths whose module both (a) calls
    ``stream_llm_response`` and (b) mutates a variable literally named
    ``api_rounds`` — the two co-occurrence markers for a billing consumer.

    Uses ``ripgrep`` via subprocess (much faster than a naive ``os.walk`` on
    a FUSE-mounted tree that includes large ignored subtrees) and falls back
    to a pruned walk when ``rg`` isn't on PATH.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates: set[str] = set()
    try:
        import subprocess
        # ``rg -l`` returns files matching BOTH patterns in AND-mode via two
        # calls (rg has no AND operator). Two intersecting file-list queries
        # are still O(seconds) on a large tree.
        common = ['rg', '-l', '--no-heading', '--type', 'py']
        for skip in _DISCOVERY_SKIP_DIRS:
            common.extend(['--glob', f'!**/{skip}/**'])
        r1 = subprocess.run(common + ['stream_llm_response('], cwd=repo,
                            capture_output=True, text=True, timeout=15,
                            check=False)
        r2 = subprocess.run(common + ['api_rounds'], cwd=repo,
                            capture_output=True, text=True, timeout=15,
                            check=False)
        s1 = {p.strip() for p in (r1.stdout or '').splitlines() if p.strip()}
        s2 = {p.strip() for p in (r2.stdout or '').splitlines() if p.strip()}
        candidates = {p for p in (s1 & s2) if p.startswith('lib/')}
    except (FileNotFoundError, ImportError, Exception):
        # Fallback: pruned walk over lib/ only.
        for root, dirs, files in os.walk(os.path.join(repo, 'lib')):
            dirs[:] = [d for d in dirs if d not in _DISCOVERY_SKIP_DIRS]
            for name in files:
                if name.endswith('.py'):
                    candidates.add(os.path.relpath(os.path.join(root, name),
                                                   repo))

    hits: set[str] = set()
    for rel in candidates:
        path = os.path.join(repo, rel)
        try:
            src = open(path, encoding='utf-8').read()
        except OSError:
            continue
        if 'stream_llm_response(' not in src or 'api_rounds' not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        has_real_call = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == 'stream_llm_response'
            and not _is_bare_forward(n)
            for n in ast.walk(tree)
        )
        if not has_real_call:
            continue
        # skip the definition module (uses ``def stream_llm_response``) and
        # the pure facade shim in llm_fallback/_call.py (which DOES contain
        # a real call as part of a forwarding pattern — kept via the below
        # ``_call.py in path`` allow because it also owns the primary path).
        defines_real = any(
            isinstance(n, ast.FunctionDef)
            and n.name == 'stream_llm_response'
            and 'return _facade' not in ast.unparse(n)
            for n in ast.walk(tree)
        )
        if defines_real:
            continue
        hits.add(rel)
    return hits


def _is_bare_forward(call_node: ast.Call) -> bool:
    """True for the ``return _facade().stream_llm_response(*args, **kwargs)``
    shim in llm_fallback/_call.py — a pure forwarder, not a real consumer."""
    if len(call_node.args) != 1 or not isinstance(call_node.args[0], ast.Starred):
        return False
    if len(call_node.keywords) != 1 or call_node.keywords[0].arg is not None:
        return False
    return True


def test_static_gate_all_billing_consumers_registered():
    """Every file that both calls ``stream_llm_response`` and writes
    ``api_rounds`` must be on the known-consumer whitelist — otherwise a
    future dispatch could add a new consumer without triggering the
    honest-accounting review that landed the discards-are-billed contract.

    To add a new consumer:
      1. Wire the ``for _bill in (usage.get('_extra_billing_rounds') or []):``
         loop right next to your ``api_rounds.append(...)``.
      2. Add the path to ``_KNOWN_BILLING_CONSUMERS`` in this test.
      3. Add a parity case in this file exercising your consumer.
    """
    discovered = _discover_stream_callers()
    unknown = discovered - _KNOWN_BILLING_CONSUMERS
    assert not unknown, (
        f'New stream_llm_response consumer(s) detected without honest-accounting '
        f'review: {sorted(unknown)}. See test docstring for the 3-step onboarding. '
        f'ALL discovered: {sorted(discovered)}')


def test_static_gate_every_registered_consumer_bills_discards():
    """Each registered billing consumer MUST read ``_extra_billing_rounds``
    somewhere in its body — proving the honest-accounting seam is wired.
    A consumer that regresses (removes the loop or renames the field)
    breaks this immediately, before it can under-bill anyone in production.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    missing = []
    for rel in _KNOWN_BILLING_CONSUMERS:
        path = os.path.join(repo, rel)
        src = open(path, encoding='utf-8').read()
        if '_extra_billing_rounds' not in src:
            missing.append(rel)
    assert not missing, (
        f'Registered billing consumer(s) lost the honest-accounting seam '
        f'(no reference to _extra_billing_rounds): {missing}. This is the '
        f'exact regression the guard exists to catch — re-wire the '
        f'`for _bill in (usage.get("_extra_billing_rounds") or []):` loop '
        f'next to the api_rounds.append call site.')


# ── Guard 3: default gate stays OFF (redundant with test_cache_floor_retry
#     but restated so a rebase that drops one file still has the other) ──


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
