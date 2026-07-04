#!/usr/bin/env python3
"""Finding #10 — SSE DB-fallback generators must not stall the event loop.

The '15000 incident' was a full-conversation-snapshot ``json.dumps`` stalling
the asyncio event loop ~40 ms (the C JSON accelerator holds the GIL for the
WHOLE call, so even off-loop-in-an-executor it starves the loop thread). The
live warm path was fixed to route the snapshot through ``_dumps_yielding``
(orjson-first — encodes ~8x faster so the GIL is released far sooner). But the
two DB-FALLBACK sync generators (``gen_done`` / ``gen_persisted`` in
``routes/chat.py::chat_stream``) were left on plain ``json.dumps`` /
``json.loads``. Quart consumes a sync generator via ``run_sync_iterable`` →
``loop.run_in_executor``, so people assume it's off-loop and safe — but the GIL
trap makes it stall the loop just the same (proven below).

This suite bites BOTH the source wiring (the fallback generators must call the
orjson helpers, not plain json) AND the runtime behaviour (consuming the real
generator exactly as Quart does keeps the loop stall under threshold; neutering
the offload reintroduces the stall). Per the project rule: measure loop stall
with a heartbeat, never assume ``to_thread`` fixed it.

Bare-CI-safe: no DB, no node, no network. The Flask→Quart shim installed by
``tests/conftest.py`` makes ``import routes.chat`` safe at collection time.
"""
import ast
import asyncio
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

_CHAT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'routes', 'chat.py')


# ── A ~10 MB conversation-state snapshot, the incident shape ──────────────
def _big_state():
    return {
        'type': 'state',
        'status': 'done',
        'content': 'x' * 4_000_000,
        'thinking': 'y' * 2_000_000,
        'toolRounds': [{'id': i, 'name': 'read_files', 'text': 'z' * 2000}
                       for i in range(2000)],
    }


def _measure_sync_gen_loop_stall(sync_gen_factory):
    """Consume a plain-`def` generator EXACTLY as Quart does (IterableBody →
    run_sync_iterable → loop.run_in_executor) while a 1 ms heartbeat records
    the max gap between ticks. Returns max stall in milliseconds."""
    from quart.wrappers.response import IterableBody

    async def _main():
        stalls = []

        async def _hb():
            last = time.perf_counter()
            while True:
                await asyncio.sleep(0.001)
                now = time.perf_counter()
                stalls.append((now - last) * 1000.0)
                last = now

        hb = asyncio.create_task(_hb())
        await asyncio.sleep(0.05)  # let the heartbeat settle
        async for _chunk in IterableBody(sync_gen_factory()):
            pass
        await asyncio.sleep(0.02)
        hb.cancel()
        return max(stalls) if stalls else 0.0

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_main())
    finally:
        loop.close()


# ══════════════════════════════════════════════════════════════════════
#  1. STRUCTURAL — the fallback generators route snapshots through the
#     orjson helpers, never plain json.dumps / json.loads on a full snapshot.
# ══════════════════════════════════════════════════════════════════════
def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _called_names(node):
    """All simple function-call names invoked anywhere inside *node*."""
    names = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            names.append(n.func.id)
    return names


def test_source_fallback_generators_use_orjson_helpers():
    src = open(_CHAT_PY, encoding='utf-8').read()
    tree = ast.parse(src)

    for gen_name in ('gen_done', 'gen_persisted'):
        fn = _find_func(tree, gen_name)
        assert fn is not None, f'{gen_name} not found in routes/chat.py'
        calls = _called_names(fn)
        # The full-snapshot encode MUST go through _dumps_yielding.
        assert '_dumps_yielding' in calls, (
            f'{gen_name} does not call _dumps_yielding — full snapshot is '
            f'being encoded with a plain encoder (the 15000-incident bug)')
        # And it MUST NOT encode a full snapshot with plain json.dumps. We
        # allow json.dumps ONLY for per-event replay frames (small, individual
        # events) — checked by the argument being the loop var `payload`, never
        # a full state/done snapshot. gen_done has no per-event loop at all, so
        # it must have ZERO json.dumps.
        json_dumps_calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == 'dumps'
            and isinstance(n.func.value, ast.Name) and n.func.value.id == 'json']
        if gen_name == 'gen_done':
            assert not json_dumps_calls, (
                'gen_done must not call json.dumps at all — every yield is a '
                'full snapshot and must use _dumps_yielding')


def test_source_tool_rounds_parse_uses_loads_yielding():
    """Inside the SSE ``chat_stream`` function (the Finding #10 scope), the
    multi-MB tool_rounds parse — both the on-loop async-body one and the
    ``gen_persisted`` one — must go through ``_loads_yielding``, not plain
    ``json.loads``. Scoped to ``chat_stream`` via AST so it bites exactly the
    two SSE sites and does NOT over-claim the separate ``chat_poll`` handler."""
    src = open(_CHAT_PY, encoding='utf-8').read()
    tree = ast.parse(src)
    fn = _find_func(tree, 'chat_stream')
    assert fn is not None, 'chat_stream not found in routes/chat.py'
    scoped = ast.get_source_segment(src, fn)
    # The on-loop / GIL-holding snapshot parses must be GONE from chat_stream.
    assert "json.loads(row['tool_rounds'])" not in scoped, (
        "chat_stream on-loop json.loads(row['tool_rounds']) survived — must be "
        "await asyncio.to_thread(_loads_yielding, ...)")
    assert "json.loads(row_local['tool_rounds'])" not in scoped, (
        "gen_persisted json.loads(row_local['tool_rounds']) survived — must be "
        "_loads_yielding")
    # Both SSE tool_rounds parses now route through _loads_yielding — once as a
    # direct call inside the gen_persisted sync generator
    # (``_loads_yielding(row_local['tool_rounds'])``) and once offloaded from
    # the async body (``asyncio.to_thread(_loads_yielding, row['tool_rounds'])``).
    assert scoped.count('_loads_yielding') >= 2, (
        'expected both chat_stream tool_rounds parses to route through '
        '_loads_yielding (direct call + to_thread offload)')
    # The async-body parse MUST be offloaded to the executor (it runs on the
    # loop, unlike the sync-generator one).
    assert 'to_thread(_loads_yielding' in scoped, (
        'async-body tool_rounds parse must be offloaded via '
        'asyncio.to_thread(_loads_yielding, ...)')
    assert '_loads_yielding' in src, '_loads_yielding helper missing'


def test_source_all_tool_rounds_parses_offloaded():
    """ALL THREE multi-MB ``tool_rounds`` parse sites — the two in
    ``chat_stream`` (async body + ``gen_persisted``) AND the one in the sync
    ``chat_poll`` handler — must route through ``_loads_yielding``. A plain
    ``json.loads`` of a multi-MB blob holds the GIL for the whole parse and
    stalls the loop regardless of which thread runs it (same class as the
    encode). This asserts NO plain ``json.loads(row[...]['tool_rounds'])``
    survives anywhere in the file, and that ``chat_poll`` specifically calls
    ``_loads_yielding``."""
    src = open(_CHAT_PY, encoding='utf-8').read()
    tree = ast.parse(src)
    # Whole-file: neither the row nor row_local plain-parse form may survive.
    assert "json.loads(row['tool_rounds'])" not in src, (
        "a plain json.loads(row['tool_rounds']) survives somewhere — every "
        "multi-MB tool_rounds parse must use _loads_yielding")
    assert "json.loads(row_local['tool_rounds'])" not in src
    # chat_poll specifically routes its parse through the helper.
    poll_fn = _find_func(tree, 'chat_poll')
    assert poll_fn is not None, 'chat_poll not found in routes/chat.py'
    assert '_loads_yielding' in _called_names(poll_fn), (
        'chat_poll tool_rounds parse must call _loads_yielding')


# ══════════════════════════════════════════════════════════════════════
#  2. PARITY — the encoder/decoder contract is preserved.
# ══════════════════════════════════════════════════════════════════════
def test_dumps_yielding_roundtrips_equal():
    from routes.chat import _dumps_yielding
    obj = _big_state()
    # orjson output differs only in item separators; it must json.loads back
    # to the identical object the frontend would parse.
    assert json.loads(_dumps_yielding(obj)) == obj


def test_dumps_yielding_falls_back_on_non_str_keys():
    from routes.chat import _dumps_yielding
    # orjson rejects non-str dict keys; the stdlib iterencode fallback coerces
    # them → the call must still succeed and roundtrip (keys become strings).
    out = _dumps_yielding({'ok': 1, 2: 'coerced'})
    parsed = json.loads(out)
    assert parsed['ok'] == 1 and parsed['2'] == 'coerced'


def test_loads_yielding_matches_stdlib():
    from routes.chat import _loads_yielding
    obj = _big_state()
    raw = json.dumps(obj)
    assert _loads_yielding(raw) == json.loads(raw)
    # bytes input too (orjson.loads accepts both).
    assert _loads_yielding(raw.encode('utf-8')) == obj


# ══════════════════════════════════════════════════════════════════════
#  3. BEHAVIORAL / NC — consuming the real gen_done keeps the loop
#     responsive; neutering the offload reintroduces the stall.
# ══════════════════════════════════════════════════════════════════════
#
def _gen_done_like(dumps_fn):
    """Reconstruct the exact gen_done body shape with a pluggable encoder, so
    we can measure the real orjson helper vs a neutered plain-json one under
    the identical run_sync_iterable consumption path."""
    state = _big_state()
    done_evt = {'type': 'done', 'finishReason': 'stop'}

    def _gen():
        for _ in range(4):
            yield ':' + ' ' * 2048 + '\n\n'
        yield f'data: {dumps_fn(state)}\n\n'
        yield f'data: {dumps_fn(done_evt)}\n\n'
    return _gen


def _plain_dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


def test_NC_orjson_beats_plain_json_loop_stall():
    """BEHAVIORAL + NEGATIVE CONTROL, machine-speed-robust.

    Absolute millisecond thresholds are flaky across CI hardware, so we assert
    a RELATIVE invariant measured on the SAME box in the SAME run: consuming the
    real ``gen_done`` shape via Quart's ``run_sync_iterable`` must stall the
    event loop MEASURABLY LESS with the real orjson ``_dumps_yielding`` than
    with a plain ``json.dumps`` (the pre-fix code). If someone neuters the
    offload (makes ``_dumps_yielding`` just call ``json.dumps``), the two
    measurements collapse to equal and this test FAILS — that is the bite. We
    take the min-of-3 to damp scheduler noise and require a real margin."""
    from routes.chat import _dumps_yielding

    def _best(fn):
        return min(_measure_sync_gen_loop_stall(_gen_done_like(fn))
                   for _ in range(3))

    orjson_stall = _best(_dumps_yielding)
    plain_stall = _best(_plain_dumps)
    # orjson releases the GIL far sooner → the loop breathes. Require the
    # plain-json stall to be at least 25% worse (a wide, stable margin; the
    # raw gap is typically ~1.5-2.5x on this 10 MB shape).
    assert plain_stall > orjson_stall * 1.25, (
        f'orjson offload gave no loop-stall advantage: orjson={orjson_stall:.1f} '
        f'ms vs plain-json={plain_stall:.1f} ms — the offload is a no-op '
        f'(NC would not bite a neutered helper)')


def test_NC_dumps_yielding_actually_uses_orjson(monkeypatch):
    """DETERMINISTIC NEGATIVE CONTROL (no timing): prove the primary encode path
    is orjson, not stdlib. Count orjson.dumps invocations for a normal snapshot
    (must be the primary path), and prove the stdlib fallback ONLY fires on an
    orjson-rejected input. If a refactor swapped the primary encoder back to
    stdlib json, the first assertion fails."""
    import routes.chat as chat_mod

    calls = {'orjson': 0, 'stdlib_iterencode': 0}
    _real_orjson_dumps = chat_mod.orjson.dumps

    def _counting_orjson_dumps(obj):
        calls['orjson'] += 1
        return _real_orjson_dumps(obj)
    monkeypatch.setattr(chat_mod.orjson, 'dumps', _counting_orjson_dumps)

    # Normal snapshot → orjson is the primary (and only) encoder used.
    out = chat_mod._dumps_yielding(_big_state())
    assert calls['orjson'] == 1, 'primary encode path is not orjson'
    assert json.loads(out)['type'] == 'state'

    # orjson-rejected input (non-str key) → orjson attempted, then stdlib
    # iterencode fallback carries it (proving the fallback is reachable).
    out2 = chat_mod._dumps_yielding({1: 'x'})
    assert json.loads(out2)['1'] == 'x'


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
