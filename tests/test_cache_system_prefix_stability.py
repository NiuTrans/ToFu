#!/usr/bin/env python3
"""Prefix-cache money-leak regression suite — the two root-cause fixes for the
"cache_read pinned at the static floor (~29298), whole body re-billed every
round" bug.

ROOT CAUSE (byte-proven, see debug/cache_live_conv_byte_diff.py + the JOURNAL
entry): project conversations injected the VOLATILE cross-conversation digest /
charter / board blocks INTO the system message. On the Anthropic path
``openai_body_to_anthropic`` hoists ``system`` to the top-level ``system``
field (the CACHED FLOOR), so rewriting it every turn (the sibling list
re-orders, epics move) re-billed the whole body uncached. Worse, the detector's
wire fingerprint only hashed ``body['messages']`` — NEVER the hoisted system —
so it laundered the client-caused miss into a false "server-side — PROVEN".

FIX 1 (止血, lib/tasks_pkg/system_context/_inject.py + _reminders.py):
  the digest / charter / board blocks now ride the TRUE tail via
  ``_refresh_tail_block`` (the same cache-safe seam ``<relevant_memories>`` and
  the preference detail tier already use), NOT the system message. The static
  system prefix therefore stays byte-identical across turns → ``cache_read`` can
  grow instead of being pinned.

FIX 2 (仪器, lib/tasks_pkg/wire_fingerprint.py + cache_tracking/_detect.py +
  _state.py): ``system_fingerprint(system, tools)`` hashes the hoisted system
  block + tool schemas; ``detect_cache_break`` folds a system-fp change into the
  wire-prefix-changed verdict, so a per-turn system mutation is NAMED
  (``<hoisted>.system``) and can no longer be laundered into "server-side
  PROVEN".

Each part carries a NEUTER control proving the fix is load-bearing.

Run DIRECTLY (env-guarded):
    python tests/test_cache_system_prefix_stability.py
"""

from __future__ import annotations

import copy
import json as _json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────────
#  FIX 2 — the instrument: system_fingerprint + detector no longer launders
# ─────────────────────────────────────────────────────────────────────────────

def test_system_fingerprint_catches_system_and_tools_change():
    """system_fingerprint hashes the hoisted system block + tool schemas; a
    change in EITHER shows, a cache_control marker move does NOT."""
    from lib.tasks_pkg.wire_fingerprint import system_fingerprint
    sys_a = [{'type': 'text', 'text': 'static prompt'},
             {'type': 'text', 'text': 'related conversation(s): [c1]'}]
    sys_b = [{'type': 'text', 'text': 'static prompt'},
             {'type': 'text', 'text': 'related conversation(s): [c2]'}]  # digest moved
    tools = [{'function': {'name': 't1', 'description': 'd', 'parameters': {}}}]
    fa = system_fingerprint(sys_a, tools)
    fb = system_fingerprint(sys_b, tools)
    assert fa['system'] != fb['system'], 'a system-text change must show'
    assert fa['tools'] == fb['tools'], 'unchanged tools must match'

    # cache_control marker move on the SAME text is invisible (server ignores it)
    sys_a_marked = [{'type': 'text', 'text': 'static prompt',
                     'cache_control': {'type': 'ephemeral'}},
                    {'type': 'text', 'text': 'related conversation(s): [c1]'}]
    assert system_fingerprint(sys_a_marked, tools)['system'] == fa['system'], (
        'a cache_control marker move must NOT register as a system change')

    # a tool schema edit shows
    tools2 = [{'function': {'name': 't1', 'description': 'CHANGED', 'parameters': {}}}]
    assert system_fingerprint(sys_a, tools2)['tools'] != fa['tools']


def test_detector_names_system_change_not_server_side():
    """★ THE INSTRUMENT FIX. A round whose MESSAGES are byte-identical but whose
    hoisted SYSTEM block changed must be NAMED (<hoisted>.system) and must NOT
    be laundered into a false 'server-side — PROVEN'."""
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
    from lib.tasks_pkg.wire_fingerprint import (
        canonical_messages, static_prefix_hash, system_fingerprint,
    )
    _cache_states.clear()
    conv = 'sysfp-named'
    # Identical messages both rounds (the body did NOT change) ...
    msgs = [{'role': 'user', 'content': 'hi'},
            {'role': 'assistant', 'content': 'yo'}]
    fp = canonical_messages(msgs)
    st = static_prefix_hash(msgs)
    # ... but the hoisted system block DID change (digest re-ordered).
    sys1 = 'static\n\nrelated conversation(s): [c1 — foo]'
    sys2 = 'static\n\nrelated conversation(s): [c2 — bar]'
    tools = [{'function': {'name': 't', 'description': 'd', 'parameters': {}}}]
    u1 = {'cache_read_tokens': 90000, 'cache_creation_input_tokens': 50000,
          '_wire_fp': fp, '_wire_static': st,
          '_wire_system': system_fingerprint(sys1, tools)}
    u2 = {'cache_read_tokens': 26016, 'cache_creation_input_tokens': 120000,
          '_wire_fp': fp, '_wire_static': st,
          '_wire_system': system_fingerprint(sys2, tools)}
    detect_cache_break(conv, msgs, tools, 'claude-opus-4', usage=dict(u1))
    r = detect_cache_break(conv, msgs, tools, 'claude-opus-4', usage=dict(u2))
    assert r is not None, 'expected a break (read pinned, big re-write)'
    blob = _json.dumps(r)
    assert 'PROVEN' not in blob, (
        f'a system-block mutation was laundered into a PROVEN server-side '
        f'verdict — the instrument blind spot is NOT closed: {r}')
    assert '<hoisted>' in blob or 'prefix' in blob, (
        f'the system change was not named as the culprit: {r}')


def test_detector_NEUTER_without_system_fp_launders_to_proven():
    """NEUTER control: the SAME system-block-only mutation, but with NO
    _wire_system captured (pre-Fix-2 behavior), IS laundered into the
    not-a-client-culprit verdict (byte-identical messages → the generic
    'upstream cache miss' verdict, the current wording; historically
    'upstream cache eviction' / 'server-side — PROVEN'). Proves Fix 2's system
    fingerprint is load-bearing."""
    from lib.tasks_pkg.cache_tracking import _cache_states, detect_cache_break
    from lib.tasks_pkg.wire_fingerprint import (
        canonical_messages, static_prefix_hash,
    )
    _cache_states.clear()
    conv = 'sysfp-neuter'
    msgs = [{'role': 'user', 'content': 'hi'},
            {'role': 'assistant', 'content': 'yo'}]
    fp = canonical_messages(msgs)
    st = static_prefix_hash(msgs)
    # NO _wire_system key at all → detector cannot see the hoisted system.
    u1 = {'cache_read_tokens': 90000, 'cache_creation_input_tokens': 50000,
          '_wire_fp': fp, '_wire_static': st}
    u2 = {'cache_read_tokens': 26016, 'cache_creation_input_tokens': 120000,
          '_wire_fp': fp, '_wire_static': st}
    detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u1))
    r = detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u2))
    assert r is not None
    blob = _json.dumps(r)
    # Without the system fingerprint, the messages look byte-identical → the
    # miss is laundered into the NOT-a-client-culprit verdict (the generic
    # 'upstream cache miss' wording) and the real <hoisted>.system culprit is
    # never named.
    assert 'upstream cache miss' in blob, (
        f'NEUTER expectation: without _wire_system the miss should launder to '
        f'the not-a-client-culprit (upstream cache miss) verdict (this is exactly '
        f'the blind spot Fix 2 closes) — got: {r}')
    assert '<hoisted>' not in blob, (
        f'NEUTER expectation: the system culprit must NOT be named without the '
        f'system fingerprint — got: {r}')


# ─────────────────────────────────────────────────────────────────────────────
#  FIX 1 — 止血: volatile project blocks ride the tail, system prefix is stable
# ─────────────────────────────────────────────────────────────────────────────

_DIGEST_MARKER = 'related conversation(s)'


def _system_text_of(messages):
    """Concatenated text of the first system message (the hoisted floor)."""
    if not messages or messages[0].get('role') != 'system':
        return ''
    sc = messages[0].get('content', '')
    if isinstance(sc, str):
        return sc
    if isinstance(sc, list):
        return '\n\n'.join(b.get('text', '') for b in sc
                           if isinstance(b, dict) and b.get('type') == 'text')
    return ''


def _base_messages():
    return [
        {'role': 'system', 'content': [{'type': 'text', 'text': 'STATIC PROMPT'}]},
        {'role': 'user', 'content': 'first turn'},
        {'role': 'assistant', 'content': 'ok'},
        {'role': 'user', 'content': 'second turn'},
    ]


def test_fix1_volatile_block_rides_tail_not_system():
    """_refresh_tail_block places a volatile block on the LAST user message and
    leaves the system floor untouched — so a per-turn change to that block never
    rewrites the cached system prefix."""
    from lib.tasks_pkg.system_context._reminders import _refresh_tail_block

    msgs_n = _base_messages()
    _refresh_tail_block(msgs_n, '<system-reminder>\n[PROJECT BOARD] v1\n</system-reminder>',
                        '[PROJECT BOARD]')
    # System floor is unchanged (the whole point).
    assert _system_text_of(msgs_n) == 'STATIC PROMPT', (
        'volatile block leaked into the system floor')
    # It landed on the last user message.
    last_user = msgs_n[3]
    assert isinstance(last_user['content'], list)
    assert any('[PROJECT BOARD] v1' in b.get('text', '')
               for b in last_user['content'] if isinstance(b, dict)), (
        'block did not land on the last user message')

    # Per-turn refresh: a NEW value on the SAME message replaces the stale one
    # (no duplicate, no proliferation) — the endpoint-reentry idempotency.
    action = _refresh_tail_block(
        msgs_n, '<system-reminder>\n[PROJECT BOARD] v2\n</system-reminder>',
        '[PROJECT BOARD]')
    assert action == 'replaced'
    texts = [b.get('text', '') for b in msgs_n[3]['content'] if isinstance(b, dict)]
    assert any('[PROJECT BOARD] v2' in t for t in texts)
    assert not any('[PROJECT BOARD] v1' in t for t in texts), 'stale block not stripped'


def test_fix1_system_floor_byte_identical_across_turns():
    """★ THE 止血 INVARIANT. Two consecutive turns whose ONLY difference is a
    changed volatile board/digest value must produce a BYTE-IDENTICAL system
    floor (so cache_read grows) — because the volatile block rides the tail.

    NEUTER twin: append the same volatile block to the SYSTEM message (the OLD
    behavior) and prove the floor then DIVERGES (cache bust)."""
    from lib.tasks_pkg.system_context._reminders import (
        _refresh_tail_block, _append_to_system_message,
    )

    # ── FIXED path: volatile block on the tail ──
    a = _base_messages()
    b = _base_messages()
    b.append({'role': 'assistant', 'content': 'ok2'})
    b.append({'role': 'user', 'content': 'third turn'})
    _refresh_tail_block(a, '<system-reminder>\n[PROJECT BOARD] epics=[x]\n</system-reminder>',
                        '[PROJECT BOARD]')
    _refresh_tail_block(b, '<system-reminder>\n[PROJECT BOARD] epics=[y MOVED]\n</system-reminder>',
                        '[PROJECT BOARD]')
    assert _system_text_of(a) == _system_text_of(b), (
        'FIX 1 broken: the system floor changed across turns despite the '
        'volatile block riding the tail')

    # ── NEUTER: the OLD system-append behavior busts the floor ──
    a2 = _base_messages()
    b2 = _base_messages()
    _append_to_system_message(
        a2, '<system-reminder>\n[PROJECT BOARD] epics=[x]\n</system-reminder>',
        as_separate_block=True)
    _append_to_system_message(
        b2, '<system-reminder>\n[PROJECT BOARD] epics=[y MOVED]\n</system-reminder>',
        as_separate_block=True)
    assert _system_text_of(a2) != _system_text_of(b2), (
        'NEUTER expectation: appending the volatile block to the SYSTEM message '
        '(old behavior) MUST diverge the floor — proving Fix 1 (tail placement) '
        'is load-bearing')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
