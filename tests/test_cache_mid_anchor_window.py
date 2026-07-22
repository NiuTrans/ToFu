#!/usr/bin/env python3
"""Mid-history-anchor 20-block-lookback suite — root-fix the periodic
whole-prefix rewrite that was being laundered into ``upstream_identical``.

BACKGROUND (evidence, see this turn's investigation + the JOURNAL):
  Anthropic prompt caching only searches ~20 CONTENT BLOCKS backward from a
  breakpoint to find a prior cache entry to EXTEND. ``add_cache_breakpoints``
  plants a mid-history "stepping-stone" breakpoint that TRAILS the rolling tail
  so the tail can always reach the previously-written entry. But the trail was
  measured in MESSAGES (``_MID_TRAIL=12``) while the tail advances in BLOCKS
  (~3 blocks/tool-round), and the mid anchor only JUMPS every ``_MID_STEP=8``
  messages. Between jumps the tail keeps pulling away, so the mid→tail BLOCK
  span sawtooths 17→20→23→26 and SPENDS HALF THE ROUNDS past 20 — on those
  rounds the tail can't extend the mid entry and the whole prefix past the mid
  is re-written. Measured live: read collapses to the ~74–80k static floor on a
  BYTE-IDENTICAL, SAME-ROUTING body, mislabelled ``upstream_identical`` (the
  "blame the gateway" verdict the owner flagged).

THE FIX under test:
  A. LAYOUT (lib/llm/cache.py) — shrink ``_MID_TRAIL`` so the mid→tail BLOCK
     span stays ≤ ``_MID_LOOKBACK`` (20) on EVERY round, across content shapes
     (prose, empty-content, parallel tool calls), while keeping the anchor
     quantized (jumps, not every round) and never on the early-user turn.
  B. DETECTOR (wire_fingerprint.marker_signature + detect_cache_break) —
     fingerprint each message-marker's cumulative BLOCK position, add
     ``mid_anchor_out_of_window(sig)``, and give ``detect_cache_break`` a
     dedicated ``cache_mid_out_of_window`` verdict so a byte-identical,
     same-routing read-collapse whose mid anchor is out of the lookback window
     is NAMED a client-side breakpoint-layout miss — NEVER laundered into
     ``upstream_identical`` / ``server_side``.

Run DIRECTLY (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_cache_mid_anchor_window.py
"""

from __future__ import annotations

import json as _json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_mid_mode_env():
    """Save/restore TOFU_CACHE_MID_MODE around every test so a test that pins a
    mode (e.g. the CURRENT-mode geometry tests) never leaks into another test
    (e.g. the drop-DEFAULT tests, which must run with the env UNSET)."""
    _prev = os.environ.get('TOFU_CACHE_MID_MODE')
    try:
        yield
    finally:
        if _prev is None:
            os.environ.pop('TOFU_CACHE_MID_MODE', None)
        else:
            os.environ['TOFU_CACHE_MID_MODE'] = _prev


# ─────────────────────────────────────────────────────────────────────────────
#  Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _grow(rounds, *, prose=True, parallel=1):
    """A growing agent tool-loop body (OpenAI shape), like a real run_task."""
    msgs = [{'role': 'system', 'content': 'S' * 40000},
            {'role': 'user', 'content': 'task'}]
    for r in range(rounds):
        tcs = [{'id': f't{r}_{k}', 'type': 'function',
                'function': {'name': 'rf', 'arguments': '{}'}}
               for k in range(parallel)]
        msgs.append({'role': 'assistant',
                     'content': ('analysing the file now' if prose else ''),
                     'tool_calls': tcs})
        for k in range(parallel):
            msgs.append({'role': 'tool', 'tool_call_id': f't{r}_{k}',
                         'content': 'R' * 1400})
    return {'model': 'claude-sonnet-4', '_task_id': 'p',
            'tools': [{'type': 'function',
                       'function': {'name': 'rf', 'parameters': {}}}],
            'messages': msgs}


def _anthropic_blocks(msg):
    """Content-block count Anthropic sees for a message (the lookback unit)."""
    n = 0
    c = msg.get('content')
    if isinstance(c, list):
        n += len(c)
    elif isinstance(c, str) and c:
        n += 1
    if msg.get('tool_calls'):
        n += len(msg['tool_calls'])
    return max(1, n)


def _mid_tail_block_gap(body):
    """The mid→tail distance in CONTENT-BLOCK space (Anthropic's lookback unit).

    Returns None if fewer than 2 message-level markers (no mid armed yet)."""
    cum = []
    t = 0
    for m in body['messages']:
        cum.append(t)
        t += _anthropic_blocks(m)
    positions = []
    for i, m in enumerate(body['messages']):
        c = m.get('content')
        if i > 0 and isinstance(c, list):
            for bi, blk in enumerate(c):
                if isinstance(blk, dict) and blk.get('cache_control'):
                    positions.append(cum[i] + bi)
    if len(positions) < 2:
        return None
    return max(positions) - min(positions)


def _msg_marker_indices(body):
    return [i for i, m in enumerate(body['messages'])
            if i > 0 and isinstance(m.get('content'), list)
            and any(isinstance(x, dict) and 'cache_control' in x
                    for x in m['content'])]


# ─────────────────────────────────────────────────────────────────────────────
#  Part A — the LAYOUT fix: mid→tail block gap stays within the lookback window
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestMidAnchorWithinBlockWindow:

    def _lookback(self):
        from lib.llm.cache import _MID_LOOKBACK
        return _MID_LOOKBACK

    def test_block_gap_never_exceeds_lookback(self):
        """★ THE ROOT-FIX GUARD (failing-first on _MID_TRAIL=12). Across a long
        growing tool loop, the mid→tail BLOCK gap must NEVER exceed the ~20-block
        lookback — else the tail cannot extend the mid entry and the prefix is
        re-written that round. Old params sawtooth to 26–30 → this FAILS; the
        fix keeps every round ≤ lookback. (CURRENT mode — the DEFAULT is now
        `drop`, which arms no mid; this test guards the current-mode geometry
        still reachable via the env opt-in.)"""
        import os as _os
        _os.environ.setdefault('CACHE_EXTENDED_TTL', '1')
        _os.environ['TOFU_CACHE_MID_MODE'] = 'current'
        import lib as _lib
        _lib.CACHE_EXTENDED_TTL = True
        from lib.llm.cache import add_cache_breakpoints

        lookback = self._lookback()
        worst = 0
        offenders = []
        for shape in (dict(prose=True, parallel=1),
                      dict(prose=False, parallel=1),
                      dict(prose=True, parallel=2)):
            for r in range(6, 64):
                body = _grow(r, **shape)
                add_cache_breakpoints(body)
                gap = _mid_tail_block_gap(body)
                if gap is None:
                    continue
                worst = max(worst, gap)
                if gap > lookback:
                    offenders.append((shape, r, gap))
        assert not offenders, (
            f'mid→tail BLOCK gap exceeded the {lookback}-block lookback on '
            f'{len(offenders)} rounds (peak={worst}); tail cannot extend the '
            f'mid entry → whole-prefix rewrite. First offenders: {offenders[:5]}')

    def test_mid_anchor_stays_quantized(self):
        """The anchor must still JUMP (occupy few distinct positions over many
        rounds), not move every round — else it is always a fresh write.
        (CURRENT mode opt-in — the DEFAULT `drop` arms no mid.)"""
        import os as _os
        _os.environ.setdefault('CACHE_EXTENDED_TTL', '1')
        _os.environ['TOFU_CACHE_MID_MODE'] = 'current'
        import lib as _lib
        _lib.CACHE_EXTENDED_TTL = True
        from lib.llm.cache import add_cache_breakpoints

        seen = []
        for r in range(18, 40):
            body = _grow(r)
            add_cache_breakpoints(body)
            marks = _msg_marker_indices(body)
            if len(marks) == 2:
                seen.append(marks[0])
        assert seen, 'mid anchor never armed across the sampled rounds'
        assert len(set(seen)) < len(seen), \
            f'mid anchor moved every round (not quantized): {seen}'

    def test_mid_anchor_never_on_early_user(self):
        """The anti-oscillation invariant survives the trail shrink: the mid /
        tail marker must never collapse onto the first user turn (msg[1]).
        (CURRENT mode opt-in — the DEFAULT `drop` arms no mid.)"""
        import os as _os
        _os.environ.setdefault('CACHE_EXTENDED_TTL', '1')
        _os.environ['TOFU_CACHE_MID_MODE'] = 'current'
        import lib as _lib
        _lib.CACHE_EXTENDED_TTL = True
        from lib.llm.cache import add_cache_breakpoints

        for r in range(4, 40):
            body = _grow(r)
            add_cache_breakpoints(body)
            assert 1 not in _msg_marker_indices(body), \
                f'r={r}: a marker landed on the early user turn (msg[1])'

    def test_total_never_exceeds_four(self):
        """Even after the fix, total markers never exceed Anthropic's hard 4.
        (CURRENT mode opt-in — it arms the mid, the worst case for marker
        count; the DEFAULT `drop` places strictly fewer.)"""
        import os as _os
        _os.environ.setdefault('CACHE_EXTENDED_TTL', '1')
        _os.environ['TOFU_CACHE_MID_MODE'] = 'current'
        import lib as _lib
        _lib.CACHE_EXTENDED_TTL = True
        from lib.llm.cache import add_cache_breakpoints

        def _count(body):
            n = 0
            for m in body['messages']:
                c = m.get('content')
                if isinstance(c, list):
                    n += sum(1 for x in c
                             if isinstance(x, dict) and x.get('cache_control'))
            for t in body.get('tools') or []:
                fn = t.get('function', {})
                if isinstance(fn, dict) and fn.get('cache_control'):
                    n += 1
            return n
        for r in (2, 8, 16, 30, 50):
            body = _grow(r)
            add_cache_breakpoints(body)
            assert _count(body) <= 4, f'r={r}: exceeded 4 breakpoints'


# ─────────────────────────────────────────────────────────────────────────────
#  Part B — the DETECTOR dimension: marker block positions + out-of-window bucket
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestMarkerBlockWindow:

    def test_marker_signature_captures_block_positions(self):
        """marker_signature must fingerprint each message-marker's cumulative
        BLOCK position so mid→tail window reachability is computable."""
        from lib.tasks_pkg.wire_fingerprint import marker_signature
        # Anthropic-shape body: system hoisted; messages carry block lists.
        body = {
            'model': 'claude-opus-4',
            'system': [{'type': 'text', 'text': 'SYS',
                        'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}],
            'messages': [
                {'role': 'user', 'content': [{'type': 'text', 'text': 'u0'}]},
                {'role': 'assistant', 'content': [
                    {'type': 'text', 'text': 'a1'},
                    {'type': 'text', 'text': 'mid',
                     'cache_control': {'type': 'ephemeral', 'ttl': '1h'}}]},
                {'role': 'tool', 'content': [{'type': 'text', 'text': 'r'}]},
                {'role': 'assistant', 'content': [
                    {'type': 'text', 'text': 'tail',
                     'cache_control': {'type': 'ephemeral'}}]},
            ],
        }
        sig = marker_signature(body)
        assert 'msg_blocks' in sig, 'marker_signature must expose msg_blocks'
        assert len(sig['msg_blocks']) == 2, \
            f'two message markers expected, got {sig["msg_blocks"]}'

    def test_mid_anchor_out_of_window_predicate(self):
        """mid_anchor_out_of_window is True only when the two message markers'
        block span exceeds the lookback; ≤ lookback and <2 markers are inert."""
        from lib.tasks_pkg.wire_fingerprint import mid_anchor_out_of_window
        near = {'msg_blocks': [10, 28]}   # span 18 ≤ 20
        far = {'msg_blocks': [10, 36]}    # span 26 > 20
        assert mid_anchor_out_of_window(near) is False
        assert mid_anchor_out_of_window(far) is True
        # <2 markers → inert (no mid armed)
        assert mid_anchor_out_of_window({'msg_blocks': [10]}) is False
        assert mid_anchor_out_of_window({'msg_blocks': []}) is False
        # missing side (mid-deploy / non-Claude) → inert
        assert mid_anchor_out_of_window(None) is False
        assert mid_anchor_out_of_window({}) is False


# ─────────────────────────────────────────────────────────────────────────────
#  Part C — the DETECTOR FALSE-POSITIVE fix: the system/head marker at block 0
#           must NOT poison the mid→tail span (OpenAI-protocol wire path).
#
#  Root cause (this turn's live-log investigation, 734 CacheRoundRecords):
#  on the OpenAI-protocol path the system prompt STAYS at ``messages[0]`` (it is
#  NOT hoisted to a top-level ``system`` field), so its cache_control marker
#  lands at cumulative block 0. ``mid_anchor_out_of_window`` computed
#  ``max(msg_blocks) - min(msg_blocks)`` = ``tail - 0`` = the tail's ABSOLUTE
#  block position, which crosses the 20-block lookback on ANY long conversation
#  — regardless of the true mid→tail geometry (measured max mid→tail span = 14,
#  never > 20, even at parallel=20). That inflated ``cache_mid_out_of_window``
#  into a near-constant false positive. The fix: ``marker_signature`` emits
#  ``body_msg_blocks`` (message markers EXCLUDING the system/head marker), and
#  the predicate measures that body-only span.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestHeadMarkerDoesNotPoisonSpan:

    def _real_openai_body_with_small_mid_tail_gap(self):
        """Build a REAL wire body via add_cache_breakpoints on the OpenAI shape
        (system = messages[0], gets a marker at block 0), long enough that the
        mid anchor is armed but whose TRUE mid→tail block span is small (≤ 14).
        """
        import os as _os
        _os.environ.setdefault('CACHE_EXTENDED_TTL', '1')
        # This class tests the CURRENT-mode mid-anchor geometry, so it must opt
        # INTO current mode (the DEFAULT is now `drop`, which arms no mid).
        _os.environ['TOFU_CACHE_MID_MODE'] = 'current'
        import lib as _lib
        _lib.CACHE_EXTENDED_TTL = True
        from lib.llm.cache import add_cache_breakpoints
        body = _grow(18, prose=True, parallel=1)   # long enough to arm the mid
        add_cache_breakpoints(body)
        return body

    def test_system_marker_lands_at_block_zero(self):
        """Precondition: on the OpenAI shape the system marker really is at
        cumulative block 0 (else this whole false-positive class wouldn't
        exist). Guards the assumption the fix rests on."""
        from lib.tasks_pkg.wire_fingerprint import marker_signature
        body = self._real_openai_body_with_small_mid_tail_gap()
        # system must have received a marker as messages[0]
        sys0 = body['messages'][0]
        assert sys0['role'] == 'system'
        assert isinstance(sys0['content'], list) and \
            any(isinstance(b, dict) and b.get('cache_control')
                for b in sys0['content']), 'system[0] should carry a marker'
        sig = marker_signature(body)
        assert 0 in sig['msg_blocks'], \
            f'system marker expected at block 0: {sig["msg_blocks"]}'

    def test_true_mid_tail_gap_is_within_lookback(self):
        """Sanity: the ACTUAL mid→tail block span (excluding system) is well
        within the lookback — so ANY out-of-window verdict on this body is a
        pure false positive from the head marker."""
        from lib.llm.cache import _MID_LOOKBACK
        body = self._real_openai_body_with_small_mid_tail_gap()
        gap = _mid_tail_block_gap(body)   # helper already excludes system (i>0)
        assert gap is not None, 'mid anchor should be armed at r=18'
        assert gap <= _MID_LOOKBACK, \
            f'true mid→tail gap {gap} should be within lookback {_MID_LOOKBACK}'

    def test_predicate_false_on_small_body_span_despite_head_marker(self):
        """★ THE FIX GUARD (failing-first before body_msg_blocks). The real body
        has system@0 + a small mid→tail span; the predicate must be FALSE.
        Pre-fix (span from raw msg_blocks incl. block 0 = tail-0 > 20) it was
        TRUE — a false positive."""
        from lib.tasks_pkg.wire_fingerprint import (
            marker_signature, mid_anchor_out_of_window,
        )
        body = self._real_openai_body_with_small_mid_tail_gap()
        sig = marker_signature(body)
        assert mid_anchor_out_of_window(sig) is False, (
            f'head marker at block 0 must NOT be counted in the mid→tail span; '
            f'msg_blocks={sig.get("msg_blocks")} '
            f'body_msg_blocks={sig.get("body_msg_blocks")}')

    def test_body_msg_blocks_excludes_head_marker(self):
        """marker_signature must emit body_msg_blocks that drops the system/head
        marker while msg_blocks still carries it (backward-compat)."""
        from lib.tasks_pkg.wire_fingerprint import marker_signature
        body = self._real_openai_body_with_small_mid_tail_gap()
        sig = marker_signature(body)
        assert 'body_msg_blocks' in sig, 'must expose body_msg_blocks'
        assert 0 in sig['msg_blocks'], 'msg_blocks keeps the head marker (compat)'
        assert 0 not in sig['body_msg_blocks'], \
            'body_msg_blocks must exclude the head marker at block 0'

    def test_NEUTER_using_raw_msg_blocks_false_positives(self):
        """NEUTER — proves body_msg_blocks is load-bearing. Reconstruct the
        PRE-FIX behaviour by feeding a signature that DROPS body_msg_blocks so
        the predicate falls back to raw msg_blocks (with the block-0 head
        marker) → it FALSELY reports out-of-window on a body whose true mid→tail
        span is within the lookback."""
        from lib.tasks_pkg.wire_fingerprint import (
            marker_signature, mid_anchor_out_of_window,
        )
        body = self._real_openai_body_with_small_mid_tail_gap()
        sig = marker_signature(body)
        # Emulate the pre-fix signature: keep raw msg_blocks (head at 0), drop
        # the body-only field so the predicate uses the poisoned span.
        neutered = {'msg_blocks': list(sig['msg_blocks'])}
        assert min(neutered['msg_blocks']) == 0, 'precondition: head marker at 0'
        assert mid_anchor_out_of_window(neutered) is True, (
            'NEUTER: with only raw msg_blocks the block-0 head marker inflates '
            'the span past the lookback → the exact false positive the fix '
            f'removes; msg_blocks={neutered["msg_blocks"]}')


def _identical_body():
    from lib.tasks_pkg.wire_fingerprint import (
        canonical_messages, static_prefix_hash, wire_byte_prefix,
    )
    msgs = [{'role': 'system', 'content': 'STATIC SYSTEM'},
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'hi there'}]
    return (msgs, canonical_messages(msgs), static_prefix_hash(msgs),
            wire_byte_prefix(msgs))


def test_detector_names_mid_out_of_window_not_upstream():
    """★ CORE (B). Body byte-identical AND routing identical both rounds, the
    read collapsed, and the mid anchor is OUT of the lookback window → the miss
    must be NAMED ``cache_mid_out_of_window`` (a client-side breakpoint-layout
    miss), NEVER ``server_side`` / laundered into the upstream verdict."""
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
    from lib.tasks_pkg.wire_fingerprint import routing_fingerprint

    _cache_states.clear()
    conv = 'mid-oow'
    msgs, fp, st, wb = _identical_body()
    r_same = routing_fingerprint(key_hash='keyAAA',
                                 anthropic_beta='prompt-caching-2024',
                                 endpoint='https://gw/claude/messages')
    # mid anchor far from tail (span 30 > 20) on BOTH rounds.
    mk = {'count': 4, 'sys': 1, 'tools': 1, 'ttls': [],
          'msg': [('mid', 0), ('tail', 0)], 'msg_blocks': [12, 42]}
    u1 = {'cache_read_tokens': 260000, 'cache_creation_input_tokens': 8000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_routing': dict(r_same), '_wire_markers': dict(mk)}
    u2 = {'cache_read_tokens': 79615, 'cache_creation_input_tokens': 190000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_routing': dict(r_same), '_wire_markers': dict(mk)}
    detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u1))
    r = detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u2))
    assert r is not None, 'expected a break (read collapsed to the floor)'
    assert 'cache_mid_out_of_window' in r, (
        f'a byte-identical, same-routing read-collapse with the mid anchor out '
        f'of the lookback window must be named cache_mid_out_of_window — got: {r}')
    assert 'server_side' not in r, f'must NOT enter the server_side branch: {r}'
    blob = _json.dumps(r).lower()
    assert 'lookback' in blob or 'window' in blob, (
        f'the verdict must name the lookback-window layout miss: {r}')


def test_detector_NEUTER_without_block_positions_launders_to_upstream():
    """NEUTER control — proves the block-position fingerprint is load-bearing.
    The SAME collapse, but with marker signatures that DROP ``msg_blocks`` (the
    pre-fix signature) → the detector can't see the out-of-window layout and the
    miss launders back into the byte-identical ``upstream cache miss`` verdict."""
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
    from lib.tasks_pkg.wire_fingerprint import routing_fingerprint

    _cache_states.clear()
    conv = 'mid-oow-neuter'
    msgs, fp, st, wb = _identical_body()
    r_same = routing_fingerprint(key_hash='keyAAA',
                                 anthropic_beta='prompt-caching-2024',
                                 endpoint='https://gw/claude/messages')
    # Pre-fix signature: NO msg_blocks field.
    mk = {'count': 4, 'sys': 1, 'tools': 1, 'ttls': [],
          'msg': [('mid', 0), ('tail', 0)]}
    u1 = {'cache_read_tokens': 260000, 'cache_creation_input_tokens': 8000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_routing': dict(r_same), '_wire_markers': dict(mk)}
    u2 = {'cache_read_tokens': 79615, 'cache_creation_input_tokens': 190000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_routing': dict(r_same), '_wire_markers': dict(mk)}
    detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u1))
    r = detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u2))
    assert r is not None
    assert 'cache_mid_out_of_window' not in r, (
        f'NEUTER: without msg_blocks the out-of-window layout MUST NOT be named '
        f'(this is exactly the blind spot the fingerprint closes) — got: {r}')
    blob = _json.dumps(r).lower()
    assert 'upstream cache miss' in blob, (
        f'NEUTER: without block positions the miss launders to the '
        f'byte-identical upstream verdict — got: {r}')


# ─────────────────────────────────────────────────────────────────────────────
#  Part D — the MISATTRIBUTION fix: <mid-out-of-window> must NOT hijack the
#           verdict of a round whose prefix BYTES genuinely changed.
#
#  Live evidence (this turn, 734 CacheRoundRecords): 128/128 real floor-collapses
#  bucketed cache_mid_out_of_window had body_identical=False — the body DID
#  change. The layout token was appended on any read-collapse regardless of
#  byte-identity and its verdict branch preempted prefix_mutation/body_change,
#  MASKING the true, actionable culprit (a per-round prefix mutation). The fix
#  gates the token on the prefix being otherwise byte-clean (no content culprit),
#  and appends it AFTER every content-culprit detector.
# ─────────────────────────────────────────────────────────────────────────────

def _changed_body_pair():
    """Two rounds whose SHARED prefix bytes DIFFER (a real content mutation):
    round-2 rewrites an already-sent message's content. Returns per-round
    (msgs, canonical_fp, static, wire_bytes)."""
    from lib.tasks_pkg.wire_fingerprint import (
        canonical_messages, static_prefix_hash, wire_byte_prefix,
    )
    base = [{'role': 'system', 'content': 'STATIC SYSTEM'},
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'analysis v1 of the prefix'},
            {'role': 'user', 'content': 'more'},
            {'role': 'assistant', 'content': 'tail turn'}]
    # round 2: mutate an ALREADY-cached prefix message (index 2) in place.
    changed = [dict(m) for m in base]
    changed[2] = {'role': 'assistant', 'content': 'analysis v2 REWRITTEN prefix'}

    def _pack(msgs):
        return (msgs, canonical_messages(msgs), static_prefix_hash(msgs),
                wire_byte_prefix(msgs))
    return _pack(base), _pack(changed)


def test_body_change_not_mislabelled_mid_out_of_window():
    """★ THE MISATTRIBUTION GUARD (failing-first before the byte-identity gate).
    A round whose prefix BYTES changed AND whose mid anchor is out of window AND
    whose read collapsed must be bucketed by its REAL culprit (prefix mutation /
    body change), NEVER cache_mid_out_of_window (a layout-only cause). Pre-fix
    the <mid-out-of-window> token was appended regardless of byte-identity and
    its verdict branch preempted the real culprit."""
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
    from lib.tasks_pkg.cache_tracking._detect import classify_verdict
    from lib.tasks_pkg.wire_fingerprint import routing_fingerprint

    _cache_states.clear()
    conv = 'mid-oow-bodychange'
    (m1, fp1, st1, wb1), (m2, fp2, st2, wb2) = _changed_body_pair()
    r_same = routing_fingerprint(key_hash='keyAAA',
                                 anthropic_beta='prompt-caching-2024',
                                 endpoint='https://gw/claude/messages')
    # mid anchor far from tail (span 30 > 20) on BOTH rounds — the geometry that
    # WOULD add <mid-out-of-window> if it were not byte-identity-gated.
    mk = {'count': 4, 'sys': 1, 'tools': 1, 'ttls': [],
          'msg': [('mid', 0), ('tail', 0)], 'msg_blocks': [12, 42],
          'body_msg_blocks': [12, 42]}
    u1 = {'cache_read_tokens': 260000, 'cache_creation_input_tokens': 8000,
          '_wire_fp': fp1, '_wire_static': st1, '_wire_bytes': wb1,
          '_wire_routing': dict(r_same), '_wire_markers': dict(mk)}
    u2 = {'cache_read_tokens': 79615, 'cache_creation_input_tokens': 190000,
          '_wire_fp': fp2, '_wire_static': st2, '_wire_bytes': wb2,
          '_wire_routing': dict(r_same), '_wire_markers': dict(mk)}
    detect_cache_break(conv, m1, None, 'claude-opus-4', usage=dict(u1))
    r = detect_cache_break(conv, m2, None, 'claude-opus-4', usage=dict(u2))
    assert r is not None, 'expected a break (prefix mutated + read collapsed)'
    assert 'cache_mid_out_of_window' not in r, (
        f'a round whose prefix BYTES changed must NOT be labelled '
        f'cache_mid_out_of_window (layout-only cause) — the real prefix '
        f'mutation must win: {r}')
    assert classify_verdict(r) != 'cache_mid_out_of_window', (
        f'bucket must reflect the real body-change culprit, not the layout '
        f'co-symptom: bucket={classify_verdict(r)} verdict={r}')


def test_mid_out_of_window_still_fires_when_body_identical():
    """Companion guard: the byte-identity gate must NOT over-correct — a truly
    byte-identical round with the same out-of-window geometry MUST still be
    named cache_mid_out_of_window (the legitimate layout miss). This is the
    NEUTER's opposite pole: proves the gate discriminates on byte-identity, not
    that it disabled the bucket wholesale."""
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
    from lib.tasks_pkg.cache_tracking._detect import classify_verdict
    from lib.tasks_pkg.wire_fingerprint import routing_fingerprint

    _cache_states.clear()
    conv = 'mid-oow-identical'
    msgs, fp, st, wb = _identical_body()
    r_same = routing_fingerprint(key_hash='keyAAA',
                                 anthropic_beta='prompt-caching-2024',
                                 endpoint='https://gw/claude/messages')
    mk = {'count': 4, 'sys': 1, 'tools': 1, 'ttls': [],
          'msg': [('mid', 0), ('tail', 0)], 'msg_blocks': [12, 42],
          'body_msg_blocks': [12, 42]}
    u1 = {'cache_read_tokens': 260000, 'cache_creation_input_tokens': 8000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_routing': dict(r_same), '_wire_markers': dict(mk)}
    u2 = {'cache_read_tokens': 79615, 'cache_creation_input_tokens': 190000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_routing': dict(r_same), '_wire_markers': dict(mk)}
    detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u1))
    r = detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u2))
    assert r is not None
    assert classify_verdict(r) == 'cache_mid_out_of_window', (
        f'a byte-IDENTICAL out-of-window collapse must still be named the '
        f'layout miss (the gate discriminates, not disables): {r}')


# ─────────────────────────────────────────────────────────────────────────────
#  Part E — the LAYOUT DEFAULT flip: drop the mid stepping-stone by default.
#
#  Live-A/B ground truth (2026-07-20, sibling real-gateway replay, 3 real
#  conversations, frozen byte-STABLE prefixes, R1 excluded): dropping the mid
#  stone cut the ~74k floor-collapse rate ~34%→~8% and re-billed write tokens
#  3.1x (943k→306k across 50 rounds), and `drop` NEVER lost. The mid
#  stepping-stone is NET-NEGATIVE on byte-stable prefixes — so TOFU_CACHE_MID_MODE
#  now defaults to `drop`. `current` stays reachable as an explicit env opt-in
#  for instant rollback.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestDropIsDefault:

    def _long_body(self):
        import lib as _lib
        _lib.CACHE_EXTENDED_TTL = True
        return _grow(24, prose=True, parallel=1)  # long enough that current would arm a mid

    def test_default_places_no_mid_marker(self):
        """★ THE DEFAULT-FLIP GUARD (failing-first before the drop default).
        With NO env set, a long conversation body must carry ONLY the tail body
        marker — the mid stepping-stone is NOT placed. Pre-flip (default
        `current`) this body armed a mid → 2 body markers → FAILS."""
        import os as _os
        _os.environ.setdefault('CACHE_EXTENDED_TTL', '1')
        _os.environ.pop('TOFU_CACHE_MID_MODE', None)   # DEFAULT path
        from lib.llm.cache import add_cache_breakpoints, _mid_placement_mode
        assert _mid_placement_mode() == 'drop', (
            'the default mid mode must be drop (the live-A/B winner)')
        body = self._long_body()
        add_cache_breakpoints(body)
        marks = _msg_marker_indices(body)
        assert len(marks) == 1, (
            f'default (drop) must place exactly ONE body marker (the tail), no '
            f'mid stepping-stone — got body-marker indices {marks}')

    def test_NEUTER_current_default_would_arm_two_markers(self):
        """NEUTER — proves the default flip is load-bearing. Explicitly select
        `current` (the pre-flip default) on the SAME body → the mid arms and
        there are 2 body markers again. This is exactly what the drop default
        removes."""
        import os as _os
        _os.environ.setdefault('CACHE_EXTENDED_TTL', '1')
        _os.environ['TOFU_CACHE_MID_MODE'] = 'current'
        from lib.llm.cache import add_cache_breakpoints, _mid_placement_mode
        assert _mid_placement_mode() == 'current'
        body = self._long_body()
        add_cache_breakpoints(body)
        marks = _msg_marker_indices(body)
        assert len(marks) == 2, (
            f'NEUTER: current mode must arm the mid → 2 body markers (the layout '
            f'the drop default removes) — got {marks}')

    def test_env_rollback_to_current_still_works(self):
        """Emergency rollback path must stay functional: TOFU_CACHE_MID_MODE=
        current re-arms the mid on a long body, so an operator can revert the
        default flip instantly without a code change."""
        import os as _os
        _os.environ.setdefault('CACHE_EXTENDED_TTL', '1')
        _os.environ['TOFU_CACHE_MID_MODE'] = 'current'
        from lib.llm.cache import add_cache_breakpoints
        body = self._long_body()
        add_cache_breakpoints(body)
        assert len(_msg_marker_indices(body)) == 2, \
            'env opt-in to current must restore the mid marker (rollback path)'

    def test_reserved_modes_fall_back_to_drop_default(self):
        """A reserved-but-unimplemented mode (smooth/cascade) or a typo must
        degrade to the DEFAULT (drop), never silently ship an unvalidated
        layout NOR revert to the net-negative current."""
        import os as _os
        from lib.llm.cache import _mid_placement_mode
        for _m in ('smooth', 'cascade', 'bogus-typo', ''):
            _os.environ['TOFU_CACHE_MID_MODE'] = _m
            assert _mid_placement_mode() == 'drop', (
                f'reserved/unknown mode {_m!r} must fall back to the drop '
                f'default, got {_mid_placement_mode()!r}')

    def test_drop_default_never_exceeds_four_markers(self):
        """The drop default must still respect Anthropic's hard 4-marker ceiling
        (trivially — it places fewer — but guard it explicitly)."""
        import os as _os
        _os.environ.setdefault('CACHE_EXTENDED_TTL', '1')
        _os.environ.pop('TOFU_CACHE_MID_MODE', None)
        from lib.llm.cache import add_cache_breakpoints
        for r in (2, 8, 16, 30, 50):
            body = _grow(r)
            add_cache_breakpoints(body)
            n = 0
            for m in body['messages']:
                c = m.get('content')
                if isinstance(c, list):
                    n += sum(1 for x in c
                             if isinstance(x, dict) and x.get('cache_control'))
            for t in body.get('tools') or []:
                fn = t.get('function', {})
                if isinstance(fn, dict) and fn.get('cache_control'):
                    n += 1
            assert n <= 4, f'r={r}: drop default exceeded 4 markers ({n})'


def test_classify_verdict_maps_mid_out_of_window_bucket():
    """The single-source bucketer maps the new verdict key to its own bucket so
    live records + offline replay count it identically (never as upstream)."""
    from lib.tasks_pkg.cache_tracking._detect import (
        BUCKET_MID_WINDOW, classify_verdict,
    )
    assert classify_verdict({'cache_mid_out_of_window': 'x'}) == BUCKET_MID_WINDOW


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
