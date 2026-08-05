"""tests/test_cache_turn_boundary_freeze.py

Turn-boundary re-bill root fix (epic pt_62ed8cce25324eb2). Measured
2026-08-01: 44 turn_boundary_rebill records, ~25 with true gap < 300s — a new
user turn's round-1 read back ~0 because the volatile context blocks are
re-rendered FRESH every task:

  * CLAUDE.md/journal _isMeta carrier (index 1, INSIDE the cached prefix) —
    the journal digest changes as the project works;
  * memory-count hint in the system floor — changes on every memory CRUD;
  * the previous turn's tail blocks (date / digest / board / …) on the
    previous user message — persistence strips them, so the next task
    rebuilds that message BARE (a prefix mutation at a deep position for any
    tool-heavy prior turn).

The fix (lib/tasks_pkg/system_context/_freeze.py + _inject.py wiring): while
the previous cache entry is alive (warm window = head-marker TTL), inject
BYTE-FROZEN head renders and restore last turn's tail blocks onto historical
user messages verbatim.

Pins:
  1. e2e boundary parity — turn B's head (system + carrier) and historical
     user messages are BYTE-IDENTICAL to turn A's wire; only the newest user
     message differs (fresh tail — free).
  2. journal/memory changes WITHIN the window stay frozen; BEYOND the window
     they re-render (no stale context past TTL).
  3. restore is idempotent (endpoint re-entry never duplicates).
  4. edit-and-resend (content changed) disables restore (hash mismatch).
  5. NEUTER — freeze/restore disabled → the boundary mutations return
     (carrier differs, hint differs, historical message bare).

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q \
        tests/test_cache_turn_boundary_freeze.py
"""

from __future__ import annotations

import copy
import time

import pytest

pytestmark = pytest.mark.unit

CONV = 'conv-freeze-e2e-1'


# ── Deterministic fixtures for every block _inject_system_contexts renders ──

@pytest.fixture
def injected_env(monkeypatch):
    """Pin every volatile render input so only the freeze under test varies.

    Returns a dict of mutable render sources: ``proj_text`` (the CLAUDE.md /
    journal body) and ``mem_hint`` (the memory-count hint) — flip them between
    turns to simulate a journal append / memory CRUD.
    """
    src = {'proj_text': 'CLAUDE-MD-AND-JOURNAL-V1', 'mem_hint': 'MEM-HINT count=1'}

    monkeypatch.setattr(
        'lib.project_mod.get_context_for_prompt',
        lambda base_path=None, conv_id=None: src['proj_text'])
    monkeypatch.setattr(
        'lib.memory.build_memory_context',
        lambda project_path=None: src['mem_hint'])
    # Keep the assembly surface minimal + deterministic: no preference
    # profile, no sibling digest/charter/goals/board, no skills index, no
    # peer-protocol block. What remains per turn: static block (deterministic
    # per conv config), memory_accum (hint under test), claude_md carrier
    # (proj text under test), and the date tail block.
    monkeypatch.setattr(
        'lib.agent_core.personal_scope.resolve_preferences_enabled',
        lambda cfg, memory_enabled=False: False)
    monkeypatch.setattr(
        'lib.conversations.project_summary.build_project_digest',
        lambda *a, **k: '')
    monkeypatch.setattr(
        'lib.conversations.project_charter.render_charter_injection_block',
        lambda *a, **k: '')
    monkeypatch.setattr(
        'lib.conversations.project_watch.render_goals_injection_block',
        lambda *a, **k: '')
    monkeypatch.setattr(
        'lib.conversations.project_board.render_board_injection_block',
        lambda *a, **k: '')
    monkeypatch.setattr(
        'lib.conversations.project_peer.render_peer_protocol_block',
        lambda *a, **k: '')
    monkeypatch.setattr('lib.skills.build_skills_index', lambda *a, **k: '')

    from lib.tasks_pkg.system_context import _freeze
    _freeze._reset_for_tests()
    from lib.tasks_pkg.cache_tracking import _state as _st
    with _st._cache_lock:
        _st._cache_states.clear()
    yield src
    _freeze._reset_for_tests()
    with _st._cache_lock:
        _st._cache_states.clear()


def _seed_warm(conv=CONV):
    """Mark the conv cache-warm (a recent LLM round on some thread)."""
    from lib.tasks_pkg.cache_tracking._state import (
        CacheState, _cache_lock, _cache_states)
    st = CacheState()
    st.call_count = 3
    st.last_update_time = time.time()
    with _cache_lock:
        _cache_states[(conv, 424242)] = st


def _inject(messages, *, project_path):
    from lib.tasks_pkg.system_context import _inject_system_contexts
    _inject_system_contexts(
        messages, project_path, True, True, True, False, True,
        conv_id=CONV, task=None, model='test-model',
        system_prompt_mode='append', tool_names=None, disabled_blocks=None)


def _turn_a(project_path):
    """Turn A: inject a fresh task list (Q2 is the new user question)."""
    msgs = [
        {'role': 'system', 'content': 'base prompt'},
        {'role': 'user', 'content': 'Q1 first question'},
        {'role': 'assistant', 'content': 'A1 first answer'},
        {'role': 'user', 'content': 'Q2 second question'},
    ]
    _inject(msgs, project_path=project_path)
    return msgs


def _turn_b_rebuild(project_path, *, q2_content='Q2 second question',
                    a2='A2 second answer', q3='Q3 third question'):
    """Turn B: the SAME conversation rebuilt BARE from the store (no injected
    blocks — persistence strips them), then injected again."""
    msgs = [
        {'role': 'system', 'content': 'base prompt'},
        {'role': 'user', 'content': 'Q1 first question'},
        {'role': 'assistant', 'content': 'A1 first answer'},
        {'role': 'user', 'content': q2_content},
        {'role': 'assistant', 'content': a2},
        {'role': 'user', 'content': q3},
    ]
    _inject(msgs, project_path=project_path)
    return msgs


def _system_bytes(msgs):
    m = msgs[0]
    c = m.get('content')
    if isinstance(c, str):
        return c
    return '\n\n'.join(b.get('text', '') for b in c
                       if isinstance(b, dict) and b.get('type') == 'text')


def _carrier(msgs):
    for m in msgs:
        if m.get('role') == 'user' and m.get('_isMeta'):
            return m
    return None


def _msg_bytes(m):
    c = m.get('content')
    if isinstance(c, str):
        return c
    return repr([(b.get('type'), b.get('text', '')[:40])
                 if isinstance(b, dict) else b for b in c])


def _find_user(msgs, needle):
    for m in msgs:
        if m.get('role') != 'user':
            continue
        c = m.get('content')
        text = c if isinstance(c, str) else ' '.join(
            b.get('text', '') for b in c if isinstance(b, dict))
        if needle in text:
            return m
    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  1 — e2e boundary parity (the core fix)
# ═══════════════════════════════════════════════════════════════════════════════

def test_e2e_turn_boundary_byte_parity(injected_env, tmp_path):
    """Turn B's head + historical messages are byte-identical to turn A's
    wire even though the journal/memory renders changed between turns —
    the frozen head + restored tail keep the whole cached prefix stable."""
    _seed_warm()
    proj = str(tmp_path)

    wa = _turn_a(proj)
    # Sanity: turn A really injected the carrier + a date tail block.
    assert _carrier(wa) is not None
    q2_a = _find_user(wa, 'Q2 second question')
    assert q2_a is not None and isinstance(q2_a.get('content'), list), (
        'turn A must attach the tail block(s) onto the newest user message')

    # The world moves on: journal appended, memory CRUD'd (turn B would
    # re-render different bytes without the freeze).
    injected_env['proj_text'] = 'CLAUDE-MD-AND-JOURNAL-V2-CHANGED'
    injected_env['mem_hint'] = 'MEM-HINT count=2 CHANGED'

    wb = _turn_b_rebuild(proj)

    # HEAD: system floor byte-identical (frozen memory hint).
    assert _system_bytes(wb) == _system_bytes(wa), (
        'system floor changed across the boundary — head freeze failed')
    # HEAD: the index-1 carrier byte-identical (frozen CLAUDE.md/journal body).
    ca, cb = _carrier(wa), _carrier(wb)
    assert ca is not None and cb is not None
    assert cb['content'] == ca['content'], (
        'carrier body changed across the boundary — claude_md freeze failed')
    assert msgs_index(wb, cb) == msgs_index(wa, ca), (
        'carrier moved from index 1')

    # HISTORICAL user message: restored byte-identical to turn A's wire.
    q2_b = _find_user(wb, 'Q2 second question')
    assert q2_b is not None
    assert _msg_bytes(q2_b) == _msg_bytes(q2_a), (
        'previous turn user message lost its tail blocks — restore failed')

    # The NEWEST user message gets fresh blocks (the free tail region).
    q3_b = _find_user(wb, 'Q3 third question')
    assert q3_b is not None and isinstance(q3_b.get('content'), list), (
        'newest user message must carry this turn fresh tail blocks')


def msgs_index(msgs, msg):
    for i, m in enumerate(msgs):
        if m is msg:
            return i
    return -1


# ═══════════════════════════════════════════════════════════════════════════════
#  2 — warm-window gating
# ═══════════════════════════════════════════════════════════════════════════════

def test_frozen_only_within_warm_window(injected_env, tmp_path, monkeypatch):
    """Within the window the carrier is frozen even when the journal changes;
    once the window is cold the fresh render lands (no stale context past
    TTL). The freeze never reaches past the entry's own lifetime."""
    from lib.tasks_pkg.system_context import _freeze as _fz
    _seed_warm()
    proj = str(tmp_path)

    wa = _turn_a(proj)
    injected_env['proj_text'] = 'JOURNAL-V2'

    # Warm: frozen body wins over the changed render.
    wb = _turn_b_rebuild(proj)
    assert _carrier(wb)['content'] == _carrier(wa)['content']
    assert 'JOURNAL-V2' not in _carrier(wb)['content']

    # Cold: the next render is fresh — the changed journal lands.
    monkeypatch.setattr(_fz, 'is_warm', lambda cid, now=None: False)
    wc = _turn_b_rebuild(proj)
    assert 'JOURNAL-V2' in _carrier(wc)['content'], (
        'a cold window must re-render — stale context past TTL is forbidden')


def test_memory_hint_frozen_then_fresh(injected_env, tmp_path, monkeypatch):
    """Memory CRUD inside the window keeps the hint frozen (the hint lives in
    the system floor = the cached head); past the window it refreshes."""
    from lib.tasks_pkg.system_context import _freeze as _fz
    _seed_warm()
    proj = str(tmp_path)

    wa = _turn_a(proj)
    injected_env['mem_hint'] = 'MEM-HINT count=99'

    wb = _turn_b_rebuild(proj)
    assert 'count=99' not in _system_bytes(wb), (
        'memory hint must stay frozen inside the warm window')
    assert _system_bytes(wb) == _system_bytes(wa)

    monkeypatch.setattr(_fz, 'is_warm', lambda cid, now=None: False)
    wc = _turn_b_rebuild(proj)
    assert 'count=99' in _system_bytes(wc), (
        'a cold window must refresh the memory hint')


# ═══════════════════════════════════════════════════════════════════════════════
#  3 — restore idempotency + edit guard
# ═══════════════════════════════════════════════════════════════════════════════

def test_restore_idempotent_on_reentry(injected_env, tmp_path):
    """Endpoint-mode re-entry (same list injected twice) must not duplicate
    the restored blocks: after a restore the message no longer matches the
    bare hash, so it is never re-touched."""
    _seed_warm()
    proj = str(tmp_path)
    wa = _turn_a(proj)
    q2_a = _find_user(wa, 'Q2 second question')

    wb = _turn_b_rebuild(proj)
    q2_first = copy.deepcopy(_find_user(wb, 'Q2 second question'))
    # Re-inject the SAME list (endpoint Critic re-entry).
    _inject(wb, project_path=proj)
    q2_second = _find_user(wb, 'Q2 second question')
    assert _msg_bytes(q2_second) == _msg_bytes(q2_first), (
        'restore duplicated blocks on re-entry')
    assert _msg_bytes(q2_second) == _msg_bytes(q2_a)


def test_edit_and_resend_disables_restore(injected_env, tmp_path):
    """An edited user row must NOT receive the old blocks — its bare hash no
    longer matches (the edit SHOULD change the wire)."""
    _seed_warm()
    proj = str(tmp_path)
    wa = _turn_a(proj)
    q2_a = _find_user(wa, 'Q2 second question')

    wb = _turn_b_rebuild(proj, q2_content='Q2 EDITED question')
    q2_b = _find_user(wb, 'Q2 EDITED question')
    assert q2_b is not None
    assert _msg_bytes(q2_b) != _msg_bytes(q2_a), (
        'edited message must differ (no stale restore)')
    # …and it carries NO injected blocks (it is not the newest message and
    # its hash matched nothing).
    c = q2_b.get('content')
    assert isinstance(c, str) or all(
        '<system-reminder>' not in (b.get('text', '') if isinstance(b, dict) else '')
        for b in c), 'edited historical message got stale blocks restored'


# ═══════════════════════════════════════════════════════════════════════════════
#  4 — NEUTER: the bug without the fix
# ═══════════════════════════════════════════════════════════════════════════════

def test_neuter_without_freeze_boundary_mutates(injected_env, tmp_path, monkeypatch):
    """NEUTER-by-data: with the head freeze off (cold is_warm) and the tail
    restore disabled, the SAME two turns produce a DIFFERENT head and a BARE
    historical message — exactly the 2026-08-01 turn_boundary_rebill bug."""
    from lib.tasks_pkg.system_context import _freeze as _fz
    _seed_warm()
    proj = str(tmp_path)

    wa = _turn_a(proj)
    injected_env['proj_text'] = 'JOURNAL-V2'
    injected_env['mem_hint'] = 'MEM-HINT count=2'

    monkeypatch.setattr(_fz, 'is_warm', lambda cid, now=None: False)
    monkeypatch.setattr(_fz, 'restore_tail_blocks', lambda msgs, cid: 0)

    wb = _turn_b_rebuild(proj)

    # The head mutated (fresh journal + hint) — re-keys the whole prefix.
    assert _carrier(wb)['content'] != _carrier(wa)['content']
    assert _system_bytes(wb) != _system_bytes(wa)
    # The historical user message is BARE (its tail blocks were stripped at
    # persistence and never restored) — a deep-prefix mutation for a
    # tool-heavy prior turn.
    q2_b = _find_user(wb, 'Q2 second question')
    q2_a = _find_user(wa, 'Q2 second question')
    assert _msg_bytes(q2_b) != _msg_bytes(q2_a), (
        'NEUTER expected: historical message must differ without restore')
