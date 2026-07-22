#!/usr/bin/env python3
"""Cache-NAMESPACE fingerprint suite — close the last unfingerprinted client
variable behind a "byte-identical → server-side" miss verdict.

BACKGROUND (evidence, see the JOURNAL / this turn's investigation):
  The always-on cache tracer compares only ``body['messages']`` + hoisted
  ``system``/``tools`` + marker layout + raw bytes between rounds. When those
  match it emits "prefix byte-identical → NOT a client-side change → upstream
  cache miss". But Anthropic prompt caching is namespaced by the request's
  (upstream API key + ``anthropic-beta`` header + endpoint): if ANY of those
  flips round-over-round, a byte-identical prefix lands on a DIFFERENT cache
  namespace and is a GUARANTEED cold miss — a CLIENT-caused miss the tracer
  was blind to (``_sse_core.py`` computed a routing fingerprint but only fed it
  to the opt-in, default-OFF byte-probe; it never reached ``usage`` or the
  verdict). The dispatch layer CAN flip the key mid-conversation (cooldown /
  429 / 401 / timeout → sticky key scored ``inf`` → picker rebinds), and the
  ``extended-cache-ttl`` beta header is latched per-TASK, so a new turn can
  re-latch a changed global — the one client variable that "can flip yet was
  fingerprinted nowhere".

THE FIX under test:
  1. ``routing_fingerprint(key_hash, anthropic_beta, endpoint)`` +
     ``diff_routing(old, new)`` in wire_fingerprint.py — normalize the three
     namespace-determining attributes and name which flipped (``<ns>key`` /
     ``<ns>beta`` / ``<ns>endpoint``). Beta token ORDER is normalized so a mere
     reorder is not a false flip; a genuine ``extended-cache-ttl`` presence
     flip IS.
  2. ``detect_cache_break`` relays ``usage['_wire_routing']`` into a 3-state
     verdict:
       - body changed              → existing client-culprit branches (untouched);
       - body identical + ns flip  → NAME the client cache-namespace switch,
                                      returned under ``cache_namespace_switch``,
                                      NEVER ``server_side``;
       - body identical + ns same  → upstream miss, wording explicitly states
                                      "key + beta + endpoint all identical".

Each behavioural test carries a NEUTER control proving the relay is
load-bearing (drop ``_wire_routing`` → the SAME beta flip launders back into
the byte-identical "upstream cache miss" verdict).

Run DIRECTLY (env-guarded):
    python tests/test_cache_namespace_fingerprint.py
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


# ─────────────────────────────────────────────────────────────────────────────
#  Part 1 — the helper: routing_fingerprint + diff_routing (pure)
# ─────────────────────────────────────────────────────────────────────────────

def test_routing_fingerprint_diff_names_each_flip():
    """routing_fingerprint captures key/beta/endpoint; diff_routing names which
    of the three flipped, and a beta TOKEN REORDER is NOT a false flip while an
    extended-cache-ttl PRESENCE flip IS."""
    from lib.tasks_pkg.wire_fingerprint import diff_routing, routing_fingerprint

    base = routing_fingerprint(key_hash='keyAAA', anthropic_beta='prompt-caching-2024',
                               endpoint='https://gw/aws.claude-opus/messages')

    # identical → no diff
    same = routing_fingerprint(key_hash='keyAAA', anthropic_beta='prompt-caching-2024',
                               endpoint='https://gw/aws.claude-opus/messages')
    assert diff_routing(base, same) == [], 'identical routing must not flag'

    # key flip
    kf = routing_fingerprint(key_hash='keyBBB', anthropic_beta='prompt-caching-2024',
                             endpoint='https://gw/aws.claude-opus/messages')
    assert diff_routing(base, kf) == ['<ns>key']

    # endpoint flip
    ef = routing_fingerprint(key_hash='keyAAA', anthropic_beta='prompt-caching-2024',
                             endpoint='https://OTHER/aws.claude-opus/messages')
    assert diff_routing(base, ef) == ['<ns>endpoint']

    # extended-cache-ttl PRESENCE flip (the mandated blind-spot variable)
    with_ttl = routing_fingerprint(
        key_hash='keyAAA', endpoint='https://gw/aws.claude-opus/messages',
        anthropic_beta='prompt-caching-2024,extended-cache-ttl-2025-04-11')
    assert diff_routing(base, with_ttl) == ['<ns>beta'], (
        'adding extended-cache-ttl to the beta header must flag <ns>beta')

    # beta token REORDER only → NOT a flip (normalized)
    reordered = routing_fingerprint(
        key_hash='keyAAA', endpoint='https://gw/aws.claude-opus/messages',
        anthropic_beta='extended-cache-ttl-2025-04-11,prompt-caching-2024')
    assert diff_routing(with_ttl, reordered) == [], (
        'a pure beta token reorder must NOT register as a namespace flip')

    # a missing side (mid-deploy: no prior routing captured) is inert
    assert diff_routing(None, base) == []
    assert diff_routing(base, None) == []


# ─────────────────────────────────────────────────────────────────────────────
#  Part 2 — the detector: 3-state verdict
# ─────────────────────────────────────────────────────────────────────────────

def _identical_body():
    """Two-round byte-identical body + its wire fingerprints."""
    from lib.tasks_pkg.wire_fingerprint import (
        canonical_messages, static_prefix_hash, wire_byte_prefix,
    )
    msgs = [{'role': 'system', 'content': 'STATIC SYSTEM'},
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'hi there'}]
    return (msgs, canonical_messages(msgs), static_prefix_hash(msgs),
            wire_byte_prefix(msgs))


def test_detector_names_namespace_switch_not_server_side():
    """★ CORE. Body byte-identical both rounds, but the upstream KEY flipped →
    the miss must be NAMED a client cache-namespace switch, returned under
    ``cache_namespace_switch`` (NOT ``server_side``), and must not claim it is
    an upstream/server fault."""
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
    from lib.tasks_pkg.wire_fingerprint import routing_fingerprint

    _cache_states.clear()
    conv = 'ns-key-flip'
    msgs, fp, st, wb = _identical_body()
    r_a = routing_fingerprint(key_hash='keyAAA', anthropic_beta='prompt-caching-2024',
                              endpoint='https://gw/claude/messages')
    r_b = routing_fingerprint(key_hash='keyBBB', anthropic_beta='prompt-caching-2024',
                              endpoint='https://gw/claude/messages')
    u1 = {'cache_read_tokens': 90000, 'cache_creation_input_tokens': 50000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb, '_wire_routing': r_a}
    u2 = {'cache_read_tokens': 40000, 'cache_creation_input_tokens': 120000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb, '_wire_routing': r_b}
    detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u1))
    r = detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u2))
    assert r is not None, 'expected a break (read dropped on a re-write)'
    assert 'cache_namespace_switch' in r, (
        f'a body-identical + key-flip miss must be keyed cache_namespace_switch, '
        f'never server_side — got: {r}')
    assert 'server_side' not in r, f'must NOT enter the server_side branch: {r}'
    blob = _json.dumps(r)
    assert 'namespace' in blob.lower() and 'key' in blob.lower(), (
        f'the verdict must name the key/namespace switch: {r}')


def test_detector_names_beta_ttl_flip_as_namespace_switch():
    """★ MANDATED. Body byte-identical, ONLY the anthropic-beta header flips
    from carrying extended-cache-ttl to not — the one client variable that can
    flip yet was fingerprinted nowhere. Must be named a client namespace switch,
    NOT server-side."""
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
    from lib.tasks_pkg.wire_fingerprint import routing_fingerprint

    _cache_states.clear()
    conv = 'ns-beta-flip'
    msgs, fp, st, wb = _identical_body()
    r_with = routing_fingerprint(
        key_hash='keyAAA', endpoint='https://gw/claude/messages',
        anthropic_beta='prompt-caching-2024,extended-cache-ttl-2025-04-11')
    r_without = routing_fingerprint(
        key_hash='keyAAA', endpoint='https://gw/claude/messages',
        anthropic_beta='prompt-caching-2024')
    u1 = {'cache_read_tokens': 90000, 'cache_creation_input_tokens': 50000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb, '_wire_routing': r_with}
    u2 = {'cache_read_tokens': 40000, 'cache_creation_input_tokens': 120000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb, '_wire_routing': r_without}
    detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u1))
    r = detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u2))
    assert r is not None
    assert 'cache_namespace_switch' in r, (
        f'a beta-header flip on identical body must be named a client namespace '
        f'switch, not server-side — got: {r}')
    assert 'server_side' not in r
    blob = _json.dumps(r).lower()
    assert 'beta' in blob or 'namespace' in blob, (
        f'the verdict must name the beta/namespace switch: {r}')


def test_detector_upstream_verdict_states_namespace_identical():
    """Body identical AND routing identical → the upstream-miss verdict is
    allowed, but it must EXPLICITLY state key+beta+endpoint all match last
    round (evidence-grade, not an elimination guess)."""
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
    from lib.tasks_pkg.wire_fingerprint import routing_fingerprint

    _cache_states.clear()
    conv = 'ns-same'
    msgs, fp, st, wb = _identical_body()
    r_same = routing_fingerprint(key_hash='keyAAA', anthropic_beta='prompt-caching-2024',
                                 endpoint='https://gw/claude/messages')
    u1 = {'cache_read_tokens': 90000, 'cache_creation_input_tokens': 50000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb, '_wire_routing': dict(r_same)}
    u2 = {'cache_read_tokens': 40000, 'cache_creation_input_tokens': 120000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb, '_wire_routing': dict(r_same)}
    detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u1))
    r = detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u2))
    assert r is not None
    assert 'cache_namespace_switch' not in r, (
        f'routing was identical — must NOT claim a namespace switch: {r}')
    blob = _json.dumps(r).lower()
    assert 'upstream cache miss' in blob, f'expected the upstream-miss verdict: {r}'
    assert 'identical' in blob and ('endpoint' in blob or 'beta' in blob), (
        f'the upstream verdict must state key+beta+endpoint matched last round: {r}')


def test_detector_NEUTER_without_routing_launders_beta_flip_to_server_side():
    """NEUTER control — proves the relay is load-bearing. The SAME beta-header
    flip, but with NO ``_wire_routing`` captured (pre-fix behaviour), launders
    back into the byte-identical "upstream cache miss" verdict and NEVER names
    the client namespace switch."""
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break

    _cache_states.clear()
    conv = 'ns-neuter'
    msgs, fp, st, wb = _identical_body()
    # NO _wire_routing key at all → detector is blind to the namespace flip.
    u1 = {'cache_read_tokens': 90000, 'cache_creation_input_tokens': 50000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb}
    u2 = {'cache_read_tokens': 40000, 'cache_creation_input_tokens': 120000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb}
    detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u1))
    r = detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u2))
    assert r is not None
    assert 'cache_namespace_switch' not in r, (
        f'NEUTER: without _wire_routing the namespace switch MUST NOT be named '
        f'(this is exactly the blind spot the relay closes) — got: {r}')
    blob = _json.dumps(r).lower()
    assert 'upstream cache miss' in blob, (
        f'NEUTER: without routing the miss should launder to the byte-identical '
        f'upstream verdict — got: {r}')


# ─────────────────────────────────────────────────────────────────────────────
#  Part 3 — the last unfingerprinted cache-key input: per-marker cache_control.ttl
# ─────────────────────────────────────────────────────────────────────────────
#  marker_signature captured WHERE breakpoints sit (count + position) but NOT
#  their ttl VALUE. A stable-block marker whose ttl flips 5m↔1h round-over-round
#  (the _task_id-drop latch bypass) is a CLIENT-caused cache reset on a
#  byte-identical body — the extended-cache-ttl beta header is only a coarse
#  proxy. The live <ttl-flip> branch was doubly dead: it read a 'stable_ttls'
#  key marker_signature never emitted, AND it was nested inside markers_regressed
#  which only fires on a COUNT drop (a pure ttl flip leaves count unchanged).

def _marked_body(ttl):
    """A one-round body with a single stable-block cache_control marker whose
    ttl is `ttl` ('' = bare 5m default, '1h' = extended). Returns (msgs, wire
    fingerprints, marker_signature) with byte-IDENTICAL semantic content
    regardless of ttl (the marker ttl is the ONLY difference)."""
    from lib.tasks_pkg.wire_fingerprint import (
        canonical_messages, marker_signature, static_prefix_hash,
        wire_byte_prefix,
    )
    _cc = {'type': 'ephemeral'}
    if ttl:
        _cc['ttl'] = ttl
    body = {
        'model': 'claude-opus-4',
        'system': [{'type': 'text', 'text': 'STATIC SYSTEM',
                    'cache_control': dict(_cc)}],
        'messages': [
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'hi there'},
        ],
    }
    msgs = body['messages']
    return (msgs, canonical_messages(msgs), static_prefix_hash(msgs),
            wire_byte_prefix(msgs), marker_signature(body))


def test_marker_signature_captures_ttl_value():
    """marker_signature must fingerprint each stable marker's cache_control.ttl,
    so a 5m↔1h flip changes the signature while the marker COUNT/POSITION and
    the body bytes stay identical."""
    from lib.tasks_pkg.wire_fingerprint import markers_ttl_flipped

    *_, sig_5m = _marked_body('')      # bare ephemeral = 5-minute default
    *_, sig_1h = _marked_body('1h')    # extended ttl
    assert sig_5m.get('count') == sig_1h.get('count') == 1, (
        'the marker count/position must be identical — only the ttl differs')
    assert markers_ttl_flipped(sig_5m, sig_1h) is True, (
        'a 5m↔1h ttl flip on the same marker must be detected')
    assert markers_ttl_flipped(sig_5m, sig_5m) is False, (
        'an unchanged ttl must NOT flag')
    # A missing side (mid-deploy / non-Claude) is inert.
    assert markers_ttl_flipped(None, sig_1h) is False
    assert markers_ttl_flipped(sig_1h, None) is False


def test_markers_ttl_flip_no_false_positive_on_sys_count_change():
    """★ THE FALSE-POSITIVE FIX (regression guard). When the system marker COUNT
    changes but every marker's ttl VALUE is unchanged — the real case where the
    mid-history anchor arms as a conversation grows within one task and
    ``_system_bp_budget`` drops 2→1, so the ``sys`` slot goes ['1h','1h']→['1h']
    — this is NOT a ttl flip and must NOT fire. The old multiset compare cried
    wolf here, mislabelling a byte-changed miss as an in-task ttl re-key
    (observed live: conv mrqcu3o7 call 28→29). A genuine value flip on a
    surviving slot must still fire (asserted above)."""
    from lib.tasks_pkg.wire_fingerprint import markers_ttl_flipped

    # Two sys markers @1h + tool @1h + tail (bare) → one sys marker @1h (+ mid
    # anchor armed). ALL surviving-slot ttl VALUES unchanged (still 1h).
    prev = {'count': 4, 'sys': 2, 'tools': 1,
            'ttls': sorted([('sys', '1h'), ('sys', '1h'),
                            ('tools', '1h'), ('msg:k1', '')])}
    cur = {'count': 4, 'sys': 1, 'tools': 1,
           'ttls': sorted([('sys', '1h'), ('tools', '1h'),
                           ('msg:k2', ''), ('msg:k3', '')])}
    assert markers_ttl_flipped(prev, cur) is False, (
        'a sys marker-count change with UNCHANGED ttl value must NOT be flagged '
        'as a ttl flip (the multiset false-positive)')

    # Control: the SAME count change but the surviving sys ttl actually flips
    # 1h→5m → this IS a real re-key and MUST still fire.
    cur_flip = {'count': 4, 'sys': 1, 'tools': 1,
                'ttls': sorted([('sys', ''), ('tools', '1h'),
                                ('msg:k2', ''), ('msg:k3', '')])}
    assert markers_ttl_flipped(prev, cur_flip) is True, (
        'a genuine 1h→5m value flip on the surviving sys slot must still fire')


def test_detector_names_ttl_flip_client_side_on_identical_body():
    """★ THE #2 FIX. Body byte-identical, marker count/position identical, but a
    stable marker's cache_control.ttl flipped 1h→5m (the _task_id-drop latch
    bypass). That is a CLIENT-caused cache reset — the whole prefix re-keys — and
    must be NAMED (<ttl-flip>), NOT laundered into a byte-identical server-side
    verdict."""
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break

    _cache_states.clear()
    conv = 'ttl-flip-named'
    msgs, fp, st, wb, sig_1h = _marked_body('1h')
    _, _, _, _, sig_5m = _marked_body('')
    u1 = {'cache_read_tokens': 90000, 'cache_creation_input_tokens': 50000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_markers': sig_1h}
    u2 = {'cache_read_tokens': 40000, 'cache_creation_input_tokens': 120000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_markers': sig_5m}
    detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u1))
    r = detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u2))
    assert r is not None, 'expected a break (read dropped on a re-write)'
    assert 'server_side' not in r, (
        f'a client-caused ttl flip must NOT be attributed server-side: {r}')
    blob = _json.dumps(r).lower()
    # The ttl-flip verdict names it precisely: "cache TTL marker flipped between
    # turns … re-billed under a new cache key" — assert that culprit wording,
    # not the bare 'ttl' substring (which the innocent upstream verdict shares).
    assert 'marker flipped' in blob or 'new cache key' in blob, (
        f'the verdict must name the cache_control ttl flip as the culprit: {r}')


def test_detector_NEUTER_ttl_signature_without_ttl_launders_to_upstream():
    """NEUTER control — proves the ttl fingerprint is load-bearing. The SAME
    marker ttl flip, but with marker signatures that DROP the ttl field (the
    pre-fix marker_signature), leaves the round byte-identical AND marker-count
    identical → the miss launders back into the upstream/byte-identical verdict
    and the <ttl-flip> culprit is never named."""
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break

    _cache_states.clear()
    conv = 'ttl-flip-neuter'
    msgs, fp, st, wb, sig_1h = _marked_body('1h')
    _, _, _, _, sig_5m = _marked_body('')

    def _strip_ttl(sig):
        # Reproduce the pre-fix signature: same count/position, NO ttl field.
        return {k: v for k, v in sig.items() if k != 'ttls'}

    u1 = {'cache_read_tokens': 90000, 'cache_creation_input_tokens': 50000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_markers': _strip_ttl(sig_1h)}
    u2 = {'cache_read_tokens': 40000, 'cache_creation_input_tokens': 120000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_markers': _strip_ttl(sig_5m)}
    detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u1))
    r = detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u2))
    assert r is not None
    blob = _json.dumps(r).lower()
    # NOTE: assert on the CULPRIT wording, not the bare substring 'ttl' — the
    # innocent upstream verdict itself says "a TTL boundary", so a substring
    # check would false-match. The fix's ttl-flip verdict says "cache TTL marker
    # flipped between turns"; its absence is the NEUTER signal.
    assert 'server_side' in r, (
        f'NEUTER: without the ttl fingerprint the miss must land in the '
        f'byte-identical server_side branch — got: {r}')
    assert 'marker flipped' not in blob and 're-billed under a new cache key' \
        not in blob, (
        f'NEUTER: without the ttl in the marker signature the ttl-flip culprit '
        f'MUST NOT be named (this is exactly the blind spot the fix closes) — '
        f'got: {r}')
    assert 'upstream cache miss' in blob, (
        f'NEUTER: without the ttl fingerprint the miss launders to the '
        f'byte-identical upstream verdict — got: {r}')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
