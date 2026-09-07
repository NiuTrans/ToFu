"""tests/test_cache_prefix_byte_identity_r4r5r6.py

Regression guard for the "offset then rebound" prefix-cache miss seen on conv
mrne3bqe1nvafr (run 1e394541): R3 read✓ → R4 read=0 → R5 read=0 → R6 read✓.

The production detector labelled R4/R5 "server-side PROVEN: byte-identical", a
verdict resting on ``canonical_messages`` — which erases the transforms under
suspicion. Per the peer that owns the source fix, the miss is CLIENT-caused
(R6 read back the exact R3-era entry, so the server never evicted it) and the
wire fingerprint is BLIND to the flip.

So EVERY assertion here is on the ACTUAL Anthropic-translated wire bytes
(``json.dumps`` of ``body['messages']`` / ``system`` / ``tools`` with only
``cache_control`` stripped where noted), NOT on ``canonical_messages`` /
``diff_canonical``.

SCOPE / HONESTY (updated after tracing the live paths + cache.py fix 10cd77c):
the ``_task_id``-drop TTL flip is a REAL HAZARD but is NOT proven to be R4/R5's
live cause. Three layers now make it not fire in the tool loop:
  (1) ``add_cache_breakpoints`` reads ``_task_id`` NON-destructively (10cd77c) —
      the pop moved to the wire boundary (``prepare_request``), so re-feeding the
      SAME body on a 429/503 retry keeps the latch stable on attempt 2+;
  (2) every orchestrator rebuild RE-SETS it (``_run.py:1037`` per round;
      ``llm_fallback/_call.py:286`` reactive-compact; ``:471`` model fallback);
  (3) the ``dispatch_stream`` 429-retry re-copies the ORIGINAL each attempt
      (``_adapt_stream_body_for_slot``: ``body = dict(body_or_messages)``).
So on mrne3bqe (global TTL True, never hot-reloaded that day) no TTL delta fired
— R4/R5 remains a LIVE-ONLY miss not yet reproduced to a specific flipping field
(the peer owns the live byte-probe hunt, the ttl-aware marker_signature detector
fix, and the ``_task_id`` hardening).

Properties locked here:

  1. ``test_shared_prefix_byte_identical_across_rounds`` — in a single
     append-only task (the inner tool loop), the SHARED prefix of consecutive
     rounds' final wire bodies is byte-identical, INCLUDING across the
     empty-thinking round (R4: content=0 thinking=0) with a head image +
     reasoning replay. This is the property the Phase-0.5 representation-
     invariance fix guarantees; the test fails if that fix regresses.

  2. ``test_ttl_marker_stable_only_when_task_id_preserved`` — a HAZARD guard.
     ``add_cache_breakpoints`` picks the stable-block TTL from the per-task
     latch ONLY when ``body['_task_id']`` is present; a body REBUILD that drops
     ``_task_id`` falls back to the live global ``CACHE_EXTENDED_TTL`` and can
     flip the stable marker ``{"ttl":"1h"}`` ↔ ``{}`` — a different gateway
     cache entry → full miss. This documents the latent hazard; it is NOT the
     proven mrne3bqe cause.

  3. ``test_live_retry_preserves_task_id`` — the LIVE-PATH proof that the
     hazard does NOT fire on the real 429-retry path: driving the actual
     ``dispatch_stream`` loop with a slot that 429s once then succeeds, the
     ORIGINAL body still carries ``_task_id`` on the retry attempt (the pop hit
     a per-attempt shallow copy). This is the empirical evidence that exonerates
     the retry path — and a guard that fails loudly if a future refactor makes
     dispatch mutate the caller's body in place.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q \
        tests/test_cache_prefix_byte_identity_r4r5r6.py
"""

import copy
import json

import pytest

import lib as _lib
from lib.llm import add_cache_breakpoints, build_body
from lib.llm.anthropic_outbound import openai_body_to_anthropic


# ── A 1x1 PNG data URI — a real head image block on the cached prefix. ──
_PNG_1x1 = (
    'data:image/png;base64,'
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk'
    'YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
)

_MODEL = 'aws.claude-opus-4.8'


def _wire_bytes(obj) -> bytes:
    """Raw-byte serialization mirroring the transport, cache_control stripped.

    ``cache_control`` is request metadata the gateway does NOT tokenize into the
    prompt; stripping it means a MOVED breakpoint does not masquerade as a
    content change (a false positive for the opposite of what we guard)."""
    def _strip_cc(o):
        if isinstance(o, dict):
            return {k: _strip_cc(v) for k, v in o.items() if k != 'cache_control'}
        if isinstance(o, list):
            return [_strip_cc(x) for x in o]
        return o
    return json.dumps(_strip_cc(obj), ensure_ascii=False, sort_keys=False).encode('utf-8')


def _tools():
    return [{
        'type': 'function',
        'function': {
            'name': 'grep_search',
            'description': 'Search file contents.',
            'parameters': {'type': 'object',
                           'properties': {'pattern': {'type': 'string'},
                                          'max_results': {'type': 'integer'}},
                           'required': ['pattern']},
        },
    }]


def _system():
    return {'role': 'system', 'content': 'You are a helpful assistant.\n' + ('S' * 4000)}


def _head_user():
    return {'role': 'user', 'content': [
        {'type': 'text', 'text': 'Look at this screenshot and investigate the repo.'},
        {'type': 'image_url', 'image_url': {'url': _PNG_1x1}},
    ]}


def _assistant_toolcall(rn, *, with_thinking):
    tcid = f'toolu_{rn:04d}'
    msg = {'role': 'assistant', 'tool_calls': [{
        'id': tcid, 'type': 'function',
        'function': {'name': 'grep_search',
                     'arguments': json.dumps({'pattern': f'symbol_{rn}', 'max_results': 20})},
    }]}
    if with_thinking:
        msg['content'] = f'Let me search for symbol_{rn}.'
        msg['reasoning_content'] = f'Grep symbol_{rn} before editing.'
        msg['thinking_signature'] = f'sig-{rn}-' + ('a' * 40)
    else:
        # The empty-thinking trigger round (R4 shape): tool_call only.
        msg['content'] = ''
    return msg, tcid


def _tool_result(tcid, rn):
    return {'role': 'tool', 'tool_call_id': tcid,
            'content': f'match line {rn}: def symbol_{rn}(): ...\n' + ('R' * 200)}


def _sequence(n_rounds=30, empty_round=18):
    """Append-only inner tool loop; yields (label, request_messages) per round.

    ``empty_round`` is placed deep enough that the mid-history anchor in
    add_cache_breakpoints is already armed, so a str↔list flip on a prefix
    message could occur if the representation-invariance fix regressed."""
    messages = [_system(), _head_user()]
    for rn in range(1, n_rounds + 1):
        yield f'R{rn}', copy.deepcopy(messages)
        asst, tcid = _assistant_toolcall(rn, with_thinking=(rn != empty_round))
        messages.append(asst)
        messages.append(_tool_result(tcid, rn))


def _wire_body(messages, *, task_id='', ttl_global=False):
    """Run the EXACT prepare_request pipeline: build_body → add_cache_breakpoints
    → openai_body_to_anthropic, and return the final translated body."""
    _prev = getattr(_lib, 'CACHE_EXTENDED_TTL', False)
    _lib.CACHE_EXTENDED_TTL = ttl_global
    try:
        body = build_body(_MODEL, copy.deepcopy(messages), max_tokens=2048,
                          thinking_enabled=True, thinking_depth='medium',
                          tools=_tools(), stream=True)
        if task_id:
            body['_task_id'] = task_id
        add_cache_breakpoints(body)
        return openai_body_to_anthropic(body)
    finally:
        _lib.CACHE_EXTENDED_TTL = _prev


def _first_prefix_divergence(prev_body, cur_body):
    """First shared-prefix (prev minus its in-flight tail) message index whose
    wire bytes differ; -1 if the shared prefix is byte-identical."""
    pm = prev_body.get('messages') or []
    cm = cur_body.get('messages') or []
    shared = max(0, len(pm) - 1)
    for i in range(min(shared, len(cm))):
        if _wire_bytes(pm[i]) != _wire_bytes(cm[i]):
            return i
    return -1


def _stable_cc_markers(body):
    """All cache_control dicts on stable segments (system + tools + non-tail
    messages) of the final Anthropic body."""
    out = []
    sys_ = body.get('system')
    if isinstance(sys_, list):
        for b in sys_:
            if isinstance(b, dict) and 'cache_control' in b:
                out.append(b['cache_control'])
    for t in body.get('tools') or []:
        fn = t.get('function') if isinstance(t, dict) and isinstance(t.get('function'), dict) else t
        if isinstance(fn, dict) and 'cache_control' in fn:
            out.append(fn['cache_control'])
    return out


# ═══════════════════════════════════════════════════════════════════════════════
#  1. Shared-prefix byte identity across the append-only loop (Phase-0.5 guard)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_shared_prefix_byte_identical_across_rounds():
    """In one append-only task, consecutive rounds' shared prefix wire bytes are
    identical — INCLUDING across the empty-thinking round with head image +
    reasoning replay. Guards the Phase-0.5 representation-invariance fix."""
    bodies = [(label, _wire_body(snap, task_id='taskX', ttl_global=True))
              for label, snap in _sequence(n_rounds=30, empty_round=18)]
    flips = []
    for k in range(1, len(bodies)):
        div = _first_prefix_divergence(bodies[k - 1][1], bodies[k][1])
        if div != -1:
            flips.append((bodies[k - 1][0], bodies[k][0], div))
    assert not flips, (
        'shared-prefix wire bytes flipped between rounds (client-caused '
        f'prefix-cache miss): {flips}')


@pytest.mark.unit
def test_empty_thinking_round_message_is_prefix_stable():
    """The empty-thinking assistant round, once frozen into history, has
    byte-identical wire bytes in every later round's prefix (it is not the
    flip vector under the current code)."""
    bodies = [(label, _wire_body(snap, task_id='taskX', ttl_global=True))
              for label, snap in _sequence(n_rounds=24, empty_round=12)]
    # After R12 the empty-thinking assistant sits at a fixed prefix index.
    # Compare that message's bytes across the last several rounds.
    ref_label, ref_body = bodies[-4]
    ref_msgs = ref_body['messages']
    # locate an assistant tool_use block with no preceding thinking sibling
    idx = next((i for i, m in enumerate(ref_msgs)
                if m.get('role') == 'assistant'
                and isinstance(m.get('content'), list)
                and any(b.get('type') == 'tool_use' for b in m['content'])
                and not any(b.get('type') == 'thinking' for b in m['content'])), None)
    assert idx is not None, 'expected an empty-thinking assistant tool_use message in prefix'
    ref_bytes = _wire_bytes(ref_msgs[idx])
    for label, body in bodies[-3:]:
        assert _wire_bytes(body['messages'][idx]) == ref_bytes, (
            f'empty-thinking prefix message[{idx}] bytes changed at {label}')


# ═══════════════════════════════════════════════════════════════════════════════
#  2. TTL-latch stability under body rebuild — the reproduced live vector
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_ttl_marker_stable_only_when_task_id_preserved():
    """The stable-block TTL marker must NOT flip between two rebuilds of the
    same round when ``_task_id`` is preserved (the latch pins it), and DOES flip
    when a rebuild drops ``_task_id`` and the live global differs — documenting
    the client-side cache-key flip that produced R4/R5's full miss."""
    from lib.tasks_pkg.cache_tracking._ttl import (
        latch_extended_ttl,
        release_ttl_latch,
    )

    snap = list(_sequence(n_rounds=20, empty_round=12))[15][1]

    # Latch the task at extended-TTL=True (as round 0 would when the global is on).
    _lib.CACHE_EXTENDED_TTL = True
    release_ttl_latch('taskLatch')
    assert latch_extended_ttl('taskLatch') is True

    # Round with _task_id preserved: latch returns True regardless of the live
    # global — so even if the global is later flipped OFF (e.g. a settings
    # change mid-task), the stable marker stays {"ttl":"1h"}.
    a = _wire_body(snap, task_id='taskLatch', ttl_global=True)
    b = _wire_body(snap, task_id='taskLatch', ttl_global=False)  # global flipped, latch wins
    a_cc, b_cc = _stable_cc_markers(a), _stable_cc_markers(b)
    assert a_cc == b_cc, (
        'latch failed: stable TTL marker flipped despite _task_id preserved — '
        f'{a_cc} vs {b_cc}')
    assert all(cc.get('ttl') == '1h' for cc in a_cc), (
        f'expected 1h stable TTL under a True latch, got {a_cc}')

    # Negative control: a REBUILD that DROPS _task_id bypasses the latch and
    # reads the live global — flipping the stable TTL marker. This is the bug.
    c = _wire_body(snap, task_id='', ttl_global=False)  # rebuild lost _task_id
    c_cc = _stable_cc_markers(c)
    assert c_cc != a_cc, (
        'expected the TTL marker to FLIP when _task_id is dropped on rebuild — '
        'if this assertion fails the vector no longer reproduces (good, if '
        'intentionally fixed; update this guard)')
    assert all('ttl' not in cc for cc in c_cc), (
        f'expected no 1h TTL after latch bypass, got {c_cc}')

    release_ttl_latch('taskLatch')


@pytest.mark.unit
def test_add_cache_breakpoints_preserves_task_id():
    """add_cache_breakpoints reads ``_task_id`` NON-destructively (cache.py fix
    10cd77c): it must LEAVE ``_task_id`` on the body so an in-task 429/503 retry
    that re-feeds the SAME body dict keeps the latch decision stable on attempt
    2+. If it popped here, attempt 2 would fall back to the live global TTL and
    flip the marker → the mrne3bqe hazard. The key is stripped only at the wire
    boundary (prepare_request), guarded by
    test_prepare_request_strips_task_id_without_mutating_caller."""
    snap = list(_sequence(n_rounds=8, empty_round=4))[5][1]
    _lib.CACHE_EXTENDED_TTL = True
    body = build_body(_MODEL, copy.deepcopy(snap), max_tokens=2048,
                      thinking_enabled=True, thinking_depth='medium',
                      tools=_tools(), stream=True)
    body['_task_id'] = 'taskKeep'
    add_cache_breakpoints(body)
    assert body.get('_task_id') == 'taskKeep', (
        'add_cache_breakpoints must PRESERVE _task_id (non-destructive read) so '
        'an in-task retry re-feeding this body keeps the TTL/beta latch stable; '
        'popping it here is the regression that reintroduces the mrne3bqe flip')


@pytest.mark.unit
def test_prepare_request_strips_task_id_without_mutating_caller():
    """The wire-serialization boundary (prepare_request) is where ``_task_id`` is
    stripped, so the internal marker never leaks onto the OpenAI wire. The
    caller's canonical dict must retain it because stream_chat reuses that same
    object on a transport retry."""
    from lib.llm._sse_core import prepare_request

    snap = list(_sequence(n_rounds=8, empty_round=4))[5][1]
    _lib.CACHE_EXTENDED_TTL = True
    body = build_body(_MODEL, copy.deepcopy(snap), max_tokens=2048,
                      thinking_enabled=True, thinking_depth='medium',
                      tools=_tools(), stream=True)
    body['_task_id'] = 'taskWire'
    plan = prepare_request(body, log_prefix='[test]')
    assert '_task_id' not in plan.body, (
        'prepare_request must strip _task_id from the wire envelope')
    assert body.get('_task_id') == 'taskWire', (
        'prepare_request mutated the caller body; attempt 2 would lose the '
        'task-stable cache TTL latch')


# ═══════════════════════════════════════════════════════════════════════════════
#  3. LIVE-PATH proof — the 429-retry loop does NOT drop _task_id
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_live_retry_preserves_task_id(monkeypatch):
    """Drive the REAL ``dispatch_stream`` retry loop: a slot that raises 429 on
    attempt 1 then succeeds on attempt 2. Assert the caller's ORIGINAL body
    still carries ``_task_id`` on BOTH attempts — i.e. the retry re-copies the
    original (``body = dict(body_or_messages)``) and the ``prepare_request`` pop
    only touched a per-attempt copy. This EXONERATES the retry path as an R4/R5
    cause and guards against a future refactor that mutates the caller in place.
    """
    from lib.llm_dispatch import api as _api
    from lib.llm_errors import RateLimitError

    # Fake slot the dispatcher hands back.
    class _Slot:
        key_name = 'k0'
        model = _MODEL
        api_key = 'x'
        base_url = None
        extra_headers = None
        oauth = ''
        protocol = 'openai'
        provider_id = 'p'
        thinking_format = ''
        adapter = None  # E4 commit 2383ae9a added slot.adapter
        consecutive_errors = 0
        def record_success(self, *a, **k): pass
        def record_error(self, *a, **k): pass

    class _Dispatcher:
        # ``slots`` mirrors the real LLMDispatcher surface — the dispatch
        # loop's big-prefix gate counts keys per model off it (single key
        # here → the gate is a no-op by design).
        slots = [_Slot()]
        def summarize_slots(self, *a, **k): return 'slot'
        def pick_and_reserve(self, **k): return _Slot()
        def has_capable_slots(self, *a, **k): return True
    monkeypatch.setattr(_api, 'get_dispatcher', lambda: _Dispatcher())

    # Record what each stream_chat attempt sees + whether the ORIGINAL keeps _task_id.
    seen = []
    original_body = {
        'model': _MODEL,
        'messages': [_system(), {'role': 'user', 'content': 'hi'}],
        '_task_id': 'taskLive',
    }

    def _fake_stream_chat(body, **kw):
        # ``body`` here is the per-attempt object dispatch built for the slot.
        seen.append({
            'attempt_body_is_original': body is original_body,
            'attempt_body_has_task_id': '_task_id' in body,
            'original_still_has_task_id': '_task_id' in original_body,
        })
        if len(seen) == 1:
            raise RateLimitError('429 simulated')
        return ({'role': 'assistant', 'content': 'ok'}, 'stop', {'prompt_tokens': 5})

    # stream_chat is imported locally inside dispatch_stream via `from lib.llm import ... stream_chat`.
    import lib.llm as _llm
    monkeypatch.setattr(_llm, 'stream_chat', _fake_stream_chat, raising=False)
    # Avoid a real sleep between 429 retries.
    monkeypatch.setattr(_api.time, 'sleep', lambda *a, **k: None)

    msg, finish, usage = _api.dispatch_stream(
        original_body, prefer_model=_MODEL, strict_model=True, max_retries=5)

    assert finish == 'stop' and msg.get('content') == 'ok'
    assert len(seen) == 2, f'expected one 429 retry then success, saw {len(seen)} attempts'
    # The KEY assertion: the ORIGINAL body kept _task_id across the retry.
    assert seen[0]['original_still_has_task_id'], (
        'the 429 first attempt POPPED _task_id off the caller original — '
        'a rebuild would then lose the latch (this is the hazard the guard covers)')
    assert seen[1]['original_still_has_task_id'], (
        'the retry attempt found _task_id gone from the caller original — '
        'dispatch mutated the caller body in place; the latch would break')
    # Each attempt worked on a per-attempt copy, not the caller original.
    assert not seen[0]['attempt_body_is_original'], (
        'dispatch handed stream_chat the caller original instead of a copy — '
        'the prepare_request pop would then mutate the caller')
