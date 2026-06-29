"""tests/test_user_profile.py — the rolling personal-preference profile.

Covers the layer-1 storage + the layer-2 cache-safe injection. The headline
acceptance criterion (per the build brief) is the cache test: injecting the
profile onto the prepended ``_isMeta`` user message must NOT make
``detect_cache_break`` log a per-round ``PREFIX MUTATION DETECTED`` — because
the injection site calls ``notify_compaction``.
"""

import json
import os
import tempfile

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def tmp_data_dir(monkeypatch):
    """Redirect the server data dir so the profile lands in a tmp tree."""
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv('TOFU_DATA_DIR', d)
        yield d


# ───────────────────────── storage / registry ─────────────────────────

def test_profile_registered_in_artifact_registry():
    from lib.agent_artifacts import (USER_PROFILE_FILE, KNOWN_ARTIFACT_NAMES,
                                      is_agent_artifact)
    assert USER_PROFILE_FILE == '.tofu_user_profile.md'
    assert USER_PROFILE_FILE in KNOWN_ARTIFACT_NAMES
    # The .tofu prefix is what makes every consumer (gitignore/export) catch it.
    assert is_agent_artifact(USER_PROFILE_FILE)


def test_save_load_roundtrip(tmp_data_dir):
    from lib.memory import user_profile as up
    assert up.load_profile() == ''  # none yet
    res = up.save_profile('## Style\n- Replies in Chinese\n- Concise')
    assert res['saved'] and res['chars'] > 0 and not res['over_cap']
    assert os.path.isfile(up.profile_path())
    body = up.load_profile()
    assert 'Replies in Chinese' in body


def test_empty_save_clears_file(tmp_data_dir):
    from lib.memory import user_profile as up
    up.save_profile('- something')
    assert os.path.isfile(up.profile_path())
    up.save_profile('   ')
    assert not os.path.isfile(up.profile_path())
    assert up.load_profile() == ''


def test_over_cap_flagged_not_truncated(tmp_data_dir):
    from lib.memory import user_profile as up
    big = '- ' + ('x' * (up.USER_PROFILE_CHAR_CAP + 500))
    res = up.save_profile(big)
    assert res['saved'] and res['over_cap']
    # Saved verbatim (forcing function for the consolidation pass — not a
    # silent mid-sentence truncation).
    assert up.profile_char_count() > up.USER_PROFILE_CHAR_CAP
    assert up.profile_over_cap()


def test_render_block_and_summary(tmp_data_dir):
    from lib.memory import user_profile as up
    assert up.render_profile_block('') is None
    up.save_profile('## Prefs\n- Likes TypeScript\n- No unsolicited refactors')
    block = up.render_profile_block()
    assert block.startswith('<system-reminder>')
    assert '[USER PREFERENCE PROFILE]' in block
    assert 'Likes TypeScript' in block
    items = up.profile_summary_for_event()
    assert items == ['Likes TypeScript', 'No unsolicited refactors']


def test_event_types_registered():
    from lib.agent_core.events import event_types
    et = event_types()
    assert 'preferences_applied' in et


# ───────────────────────── injection placement ─────────────────────────

def _base_messages():
    """A realistic post-first-round message list with the _isMeta carrier."""
    return [
        {'role': 'system', 'content': 'static system prompt'},
        {'role': 'user', 'content': '[PROJECT CO-PILOT MODE] ctx',
         '_isMeta': True},
        {'role': 'user', 'content': 'do the thing'},
    ]


def test_profile_injected_on_isMeta_tail_not_system(tmp_data_dir):
    """The profile block must land on the _isMeta user msg, never messages[0]."""
    from lib.tasks_pkg.system_context import _append_user_profile_block
    from lib.memory import user_profile as up
    up.save_profile('- Replies in Chinese')
    block = up.render_profile_block()
    msgs = _base_messages()
    ok = _append_user_profile_block(msgs, block)
    assert ok
    # System message untouched.
    assert msgs[0]['content'] == 'static system prompt'
    # Block landed on the _isMeta carrier (index 1), as an appended text block.
    carrier = msgs[1]
    assert carrier.get('_isMeta')
    joined = ''.join(b['text'] for b in carrier['content']
                     if isinstance(b, dict))
    assert '[USER PREFERENCE PROFILE]' in joined


def test_profile_injection_idempotent(tmp_data_dir):
    from lib.tasks_pkg.system_context import _append_user_profile_block
    from lib.memory import user_profile as up
    up.save_profile('- Replies in Chinese')
    block = up.render_profile_block()
    msgs = _base_messages()
    assert _append_user_profile_block(msgs, block) is True
    # Second call sees the marker already present → no double-inject.
    assert _append_user_profile_block(msgs, block) is False


def test_profile_falls_back_to_real_user_when_no_meta(tmp_data_dir):
    from lib.tasks_pkg.system_context import _append_user_profile_block
    from lib.memory import user_profile as up
    up.save_profile('- Replies in Chinese')
    block = up.render_profile_block()
    msgs = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'hello'},
    ]
    assert _append_user_profile_block(msgs, block) is True
    # Landed on the real user msg (the tail), not the system prefix.
    assert msgs[0]['content'] == 'sys'
    assert isinstance(msgs[1]['content'], list)


# ───────────────── HARD acceptance: cache-safe across rounds ─────────────────

def test_profile_injection_is_cache_safe(tmp_data_dir):
    """Injecting the profile onto the _isMeta tail must NOT register a
    prefix-mutation cache break across rounds.

    Simulates the round prologue: each round (a) re-injects the profile block
    via _append_user_profile_block + notify_compaction (exactly what
    _inject_system_contexts does at ★2.5), then (b) runs detect_cache_break.
    Without the notify_compaction call, round 2 would flag prefix_mutation
    because the _isMeta carrier sits inside messages[0:N-2] after the first
    tool round. This is the regression the brief requires us to prove absent.
    """
    from lib.tasks_pkg.cache_tracking import (detect_cache_break,
                                              notify_compaction,
                                              _cache_states)
    from lib.tasks_pkg.system_context import _append_user_profile_block
    from lib.memory import user_profile as up

    up.save_profile('- Replies in Chinese\n- Concise')
    block = up.render_profile_block()
    conv = 'prof-cache-1'
    _cache_states.pop(conv, None)

    def _round_messages(tool_tail):
        # system + _isMeta carrier + original user + a growing tool tail.
        return [
            {'role': 'system', 'content': 'static system prompt'},
            {'role': 'user', 'content': '[PROJECT CO-PILOT MODE] ctx',
             '_isMeta': True},
            {'role': 'user', 'content': 'do the thing'},
            {'role': 'assistant', 'content': 'working'},
            {'role': 'tool', 'content': tool_tail},
        ]

    # Round 1 — first injection, baseline (first call never flags).
    m1 = _round_messages('tool result 1')
    assert _append_user_profile_block(m1, block) is True
    notify_compaction(conv)
    r1 = detect_cache_break(conv, m1, None, 'claude-opus-4',
                            usage={'cache_creation_input_tokens': 50000,
                                   'cache_read_input_tokens': 20000})
    assert r1 is None

    # Rounds 2 & 3 — the carrier now sits INSIDE the cached prefix
    # (messages[0:N-2]). Re-inject + notify each round, as the real prologue
    # does. With notify_compaction, NO prefix_mutation break must surface.
    for i, tail in enumerate(['tool result 2', 'tool result 3'], start=2):
        m = _round_messages(tail)
        assert _append_user_profile_block(m, block) is True
        notify_compaction(conv)
        r = detect_cache_break(conv, m, None, 'claude-opus-4',
                               usage={'cache_creation_input_tokens': 2000,
                                      'cache_read_input_tokens': 70000})
        assert r is None or 'prefix_mutation' not in r, (
            f'round {i} falsely flagged prefix_mutation: {r}')

    # No breaks accumulated.
    assert _cache_states[conv].total_breaks == 0


def test_without_notify_would_flag(tmp_data_dir):
    """Negative control: the SAME mutation WITHOUT notify_compaction DOES
    flag prefix_mutation — proving the test above is actually exercising the
    guard, not passing vacuously.
    """
    from lib.tasks_pkg.cache_tracking import detect_cache_break, _cache_states
    from lib.tasks_pkg.system_context import _append_user_profile_block
    from lib.memory import user_profile as up

    up.save_profile('- Replies in Chinese')
    block = up.render_profile_block()
    conv = 'prof-cache-neg'
    _cache_states.pop(conv, None)

    def _round_messages(meta_text, tail):
        return [
            {'role': 'system', 'content': 'static system prompt'},
            {'role': 'user', 'content': meta_text, '_isMeta': True},
            {'role': 'user', 'content': 'do the thing'},
            {'role': 'assistant', 'content': 'working'},
            {'role': 'tool', 'content': tail},
        ]

    m1 = _round_messages('[PROJECT CO-PILOT MODE] ctx', 'tail 1')
    _append_user_profile_block(m1, block)
    detect_cache_break(conv, m1, None, 'claude-opus-4',
                       usage={'cache_creation_input_tokens': 50000,
                              'cache_read_input_tokens': 20000})
    # Round 2: prefix carrier text actually changed AND no notify → flag.
    m2 = _round_messages('[PROJECT CO-PILOT MODE] ctx EDITED', 'tail 2')
    _append_user_profile_block(m2, block)
    r2 = detect_cache_break(conv, m2, None, 'claude-opus-4',
                            usage={'cache_creation_input_tokens': 51000,
                                   'cache_read_input_tokens': 20000})
    assert r2 is not None and 'prefix_mutation' in r2


# ───────────────────────── layer 3: consolidation ─────────────────────────

def test_apply_reinforcement_replaces_in_place(tmp_data_dir):
    from lib.memory import user_profile as up
    up.save_profile('## Style\n- Replies in English\n- Concise')
    res = up.apply_reinforcement('- Replies in English',
                                 '- Replies in Chinese')
    assert res['saved'] and res['matched']
    body = up.load_profile()
    assert '- Replies in Chinese' in body
    assert 'Replies in English' not in body
    # Replace-in-place: bullet COUNT unchanged (no growth) — still 2 bullets.
    assert body.count('\n- ') == 2


def test_apply_reinforcement_ambiguous_is_noop(tmp_data_dir):
    from lib.memory import user_profile as up
    up.save_profile('- dup line\n- dup line')
    res = up.apply_reinforcement('- dup line', '- changed')
    assert res['matched'] is False and res['saved'] is False
    assert 'changed' not in up.load_profile()


def test_pending_stage_resolve_accept(tmp_data_dir):
    from lib.memory import user_profile as up
    up.save_profile('## Style\n- Concise')
    entry = up.stage_pending({'text': 'Prefers TypeScript',
                              'evidence': 'said so'})
    assert entry['id'] and up.load_pending()
    # New prefs are NOT written until confirmed.
    assert 'TypeScript' not in up.load_profile()
    res = up.resolve_pending(entry['id'], accept=True)
    assert res['resolved'] and res['accepted']
    assert 'Prefers TypeScript' in up.load_profile()
    assert up.load_pending() == []  # cleared


def test_pending_stage_resolve_dismiss(tmp_data_dir):
    from lib.memory import user_profile as up
    entry = up.stage_pending({'text': 'Likes verbose logs'})
    res = up.resolve_pending(entry['id'], accept=False)
    assert res['resolved'] and not res['accepted']
    assert 'verbose' not in up.load_profile()
    assert up.load_pending() == []


def test_stage_pending_is_idempotent(tmp_data_dir):
    from lib.memory import user_profile as up
    a = up.stage_pending({'text': 'Same pref'})
    b = up.stage_pending({'text': 'Same pref'})
    assert a['id'] == b['id']
    assert len(up.load_pending()) == 1


# ───────── REQUIRED test 1: over-cap consolidation rewrites, not appends ─────────

def test_over_cap_consolidation_distils_in_place(tmp_data_dir, monkeypatch):
    """When the profile is over cap, the consolidation pass must apply a
    'distil' action that REWRITES the whole body shorter — never append-grow.
    """
    from lib.memory import user_profile as up
    from lib.memory import profile_consolidate as pc

    # Seed an over-cap profile (lots of redundant bullets).
    bloated = '## Preferences\n' + '\n'.join(
        f'- redundant preference number {i} stated verbosely '
        + ('x' * 40) for i in range(60))
    up.save_profile(bloated)
    assert up.profile_over_cap()
    pre_chars = up.profile_char_count()

    distilled = '## Preferences\n- Concise\n- Replies in Chinese'

    # Mock the cheap model: return a single distil action.
    def _fake_dispatch(messages, **kw):
        # Prove the pass told the model it was over cap.
        assert 'OVER CAP' in messages[1]['content']
        return (json.dumps({'actions': [
            {'kind': 'distil', 'full_profile': distilled}]}), {})

    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', _fake_dispatch)

    msgs = [
        {'role': 'user', 'content': 'please keep being concise and use chinese, '
         'this is a long enough message to clear the surface threshold so the '
         'consolidation pass actually runs and asks the model what to do here.'},
        {'role': 'assistant', 'content': 'understood, I will.'},
    ]
    learned = pc.run_profile_consolidation(msgs)
    # Distil is auto-applied (compression of existing prefs, not a new fact).
    body = up.load_profile()
    assert body.strip() == distilled.strip()
    assert up.profile_char_count() < pre_chars      # SHRANK
    assert not up.profile_over_cap()                # back under cap
    # Distil isn't surfaced as a learned chip (it's housekeeping, not a new pref).
    assert all(l['kind'] != 'new' for l in learned)


# ───────── REQUIRED test 2: cross-task profile EDIT is cache-safe ─────────

def test_profile_edit_between_tasks_is_cache_safe(tmp_data_dir):
    """The exact scenario the cap targets: the profile is REWRITTEN between
    tasks (consolidation), and the next task injects the NEW body. Across the
    rounds of that next task there must be NO prefix_mutation break — because
    the injection site calls notify_compaction.
    """
    from lib.tasks_pkg.cache_tracking import (detect_cache_break,
                                              notify_compaction, _cache_states)
    from lib.tasks_pkg.system_context import _append_user_profile_block
    from lib.memory import user_profile as up

    conv = 'prof-edit-xtask'
    _cache_states.pop(conv, None)

    def _round_messages(block, tail):
        m = [
            {'role': 'system', 'content': 'static system prompt'},
            {'role': 'user', 'content': '[PROJECT CO-PILOT MODE] ctx',
             '_isMeta': True},
            {'role': 'user', 'content': 'do the thing'},
            {'role': 'assistant', 'content': 'working'},
            {'role': 'tool', 'content': tail},
        ]
        _append_user_profile_block(m, block)
        return m

    # ── Task A: profile v1, two rounds.
    up.save_profile('- Replies in English')
    block_v1 = up.render_profile_block()
    mA1 = _round_messages(block_v1, 'tA round1')
    notify_compaction(conv)
    assert detect_cache_break(conv, mA1, None, 'claude-opus-4',
                              usage={'cache_creation_input_tokens': 50000,
                                     'cache_read_input_tokens': 20000}) is None
    mA2 = _round_messages(block_v1, 'tA round2')
    notify_compaction(conv)
    rA2 = detect_cache_break(conv, mA2, None, 'claude-opus-4',
                             usage={'cache_creation_input_tokens': 2000,
                                    'cache_read_input_tokens': 70000})
    assert rA2 is None or 'prefix_mutation' not in rA2

    # ── Consolidation edits the profile BETWEEN tasks.
    up.save_profile('- Replies in Chinese\n- Concise')
    block_v2 = up.render_profile_block()
    assert block_v2 != block_v1

    # ── Task B: injects the NEW profile body; rounds must stay cache-clean.
    for i, tail in enumerate(['tB round1', 'tB round2'], start=1):
        mB = _round_messages(block_v2, tail)
        notify_compaction(conv)
        rB = detect_cache_break(conv, mB, None, 'claude-opus-4',
                                usage={'cache_creation_input_tokens': 2000,
                                       'cache_read_input_tokens': 70000})
        assert rB is None or 'prefix_mutation' not in rB, (
            f'task B round {i} falsely flagged prefix_mutation: {rB}')

    assert _cache_states[conv].total_breaks == 0


def test_preference_learned_event_registered():
    from lib.agent_core.events import event_types
    assert 'preference_learned' in event_types()


# ───────── per-user scoping (multi-user isolation) ─────────

def test_empty_scope_uses_global_file_unchanged(tmp_data_dir):
    """scope='' must resolve to the EXACT legacy global path — no migration,
    byte-identical for every open/private personal install."""
    from lib.memory import user_profile as up
    from lib.agent_artifacts import USER_PROFILE_FILE
    p = up.profile_path('')
    assert p.endswith(os.path.join('memories', USER_PROFILE_FILE))
    assert 'profiles' not in p  # NOT under the per-tenant subtree
    # Default arg == explicit '' .
    assert up.profile_path() == up.profile_path('')


def test_scoped_path_isolated_and_traversal_proof(tmp_data_dir):
    from lib.memory import user_profile as up
    a = up.profile_path('user-42')
    b = up.profile_path('user-99')
    g = up.profile_path('')
    assert a != b != g and a != g
    assert os.path.join('memories', 'profiles') in a
    # A hostile user_id can never escape the profiles subtree.
    evil = up.profile_path('../../../../etc/passwd')
    base = os.path.realpath(os.path.join(os.path.dirname(g), 'profiles'))
    assert os.path.realpath(evil).startswith(base)


def test_profiles_do_not_leak_across_scopes(tmp_data_dir):
    from lib.memory import user_profile as up
    up.save_profile('## About the user\n- Tenant A is a data scientist', scope='userA')
    up.save_profile('## About the user\n- Tenant B is a frontend dev', scope='userB')
    assert 'data scientist' in up.load_profile('userA')
    assert 'data scientist' not in up.load_profile('userB')
    assert 'frontend dev' in up.load_profile('userB')
    # The global (open/private) profile is untouched by either tenant.
    assert up.load_profile('') == ''


def test_resolve_profile_scope_from_authcontext():
    from lib.memory import user_profile as up
    from lib.api_keys import AuthContext, local_admin_context
    # Open mode synthetic admin → no tenant binding → global scope.
    assert up.resolve_profile_scope(local_admin_context()) == ''
    # Private-mode Bearer key without user_id → global scope.
    assert up.resolve_profile_scope(AuthContext(key_id='k_x')) == ''
    # Multi-user login key carries user_id → that is the scope.
    assert up.resolve_profile_scope(AuthContext(key_id='k_y', user_id='42')) == '42'
    # Robust to None / junk.
    assert up.resolve_profile_scope(None) == ''


def test_consolidation_writes_to_task_scope(tmp_data_dir, monkeypatch):
    """The daemon reads scope off the task and writes the tenant's file, not
    the global one."""
    import json as _json
    from lib.memory import user_profile as up
    from lib.memory import profile_consolidate as pc

    def _fake_dispatch(messages, **kw):
        return (_json.dumps({'actions': [
            {'kind': 'new', 'header': 'About the user',
             'text': 'Is a backend engineer', 'evidence': 'said so'}]}), {})

    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', _fake_dispatch)
    msgs = [
        {'role': 'user', 'content': 'fyi I work as a backend engineer, and this '
         'is a sufficiently long message to clear the 200-char surface threshold '
         'so the consolidation pass actually runs the cheap model here please.'},
        {'role': 'assistant', 'content': 'noted, I will remember that for you.'},
    ]
    pc.run_profile_consolidation(msgs, task={'_profileScope': 'tenant7'})
    assert 'backend engineer' in up.load_profile('tenant7')
    assert up.load_profile('') == ''  # global profile NOT touched


# ───────── structured per-item view (settings UI) ─────────

def test_parse_items_groups_by_header(tmp_data_dir):
    from lib.memory import user_profile as up
    up.save_profile('## Preferences\n- Replies in Chinese\n- Concise\n'
                    '## About the user\n- Backend engineer')
    items = up.parse_items()
    assert {'header': 'Preferences', 'text': 'Replies in Chinese'} in items
    assert {'header': 'Preferences', 'text': 'Concise'} in items
    assert {'header': 'About the user', 'text': 'Backend engineer'} in items
    assert len(items) == 3


def test_serialize_items_roundtrips(tmp_data_dir):
    from lib.memory import user_profile as up
    items = [
        {'header': 'Preferences', 'text': 'Replies in Chinese'},
        {'header': 'About the user', 'text': 'Backend engineer'},
        {'header': 'Preferences', 'text': 'Concise'},  # regroups under header
    ]
    body = up.serialize_items(items)
    # Items regroup under their header.
    assert '## Preferences' in body and '## About the user' in body
    reparsed = up.parse_items(body)
    texts = {(i['header'], i['text']) for i in reparsed}
    assert ('Preferences', 'Replies in Chinese') in texts
    assert ('Preferences', 'Concise') in texts
    assert ('About the user', 'Backend engineer') in texts


def test_save_items_drops_empty_and_persists(tmp_data_dir):
    from lib.memory import user_profile as up
    res = up.save_items([
        {'header': 'Preferences', 'text': '  Replies in Chinese '},
        {'header': 'Preferences', 'text': ''},      # dropped
        {'header': 'About the user', 'text': 'Likes Rust'},
    ])
    assert res['saved']
    items = up.parse_items()
    assert len(items) == 2
    assert all(i['text'] for i in items)


def test_save_items_empty_clears(tmp_data_dir):
    from lib.memory import user_profile as up
    up.save_profile('- something')
    up.save_items([])
    assert up.load_profile() == ''


# ───────── auto-apply: new prefs/identity are written, not staged ─────────

def test_consolidation_auto_applies_new_preference(tmp_data_dir, monkeypatch):
    """A 'new' action is now WRITTEN immediately (no staging) and surfaced as
    a 'added' learned chip — the user is informed, not asked."""
    import json as _json
    from lib.memory import user_profile as up
    from lib.memory import profile_consolidate as pc

    def _fake_dispatch(messages, **kw):
        return (_json.dumps({'actions': [
            {'kind': 'new', 'header': 'About the user',
             'text': 'Is a backend engineer', 'evidence': 'said so'}]}), {})

    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', _fake_dispatch)
    msgs = [
        {'role': 'user', 'content': 'just so you know, I work as a backend '
         'engineer, this is a sufficiently long message to clear the surface '
         'threshold (200 chars) so the consolidation pass actually runs the '
         'cheap model here instead of skipping the turn as too short to bother.'},
        {'role': 'assistant', 'content': 'good to know, I will keep that in mind.'},
    ]
    learned = pc.run_profile_consolidation(msgs)
    # Written straight into the profile under the right header.
    body = up.load_profile()
    assert 'Is a backend engineer' in body
    assert '## About the user' in body
    # Surfaced as an informational 'added' chip — never 'pending'.
    assert learned and learned[0]['kind'] == 'added'
    assert learned[0]['pending'] is False
    # Nothing staged behind a confirm gate.
    assert up.load_pending() == []


# ───────── REQUIRED: consolidation is OFF the synchronous done path ─────────

def test_consolidation_spawn_does_not_block_done(monkeypatch):
    """``_spawn_async_profile_consolidation`` must return IMMEDIATELY — it must
    NOT wait on the (potentially multi-second) cheap-LLM consolidation call.

    We make the consolidation pass sleep for a long time; the spawn call must
    return in a tiny fraction of that. This is the proof that the cheap-LLM
    round-trip no longer sits on the path to the done event.
    """
    import time as _time
    from lib.tasks_pkg import commit_round as cr

    started = {'flag': False}
    SLEEP = 2.0

    def _slow_consolidate(messages, task=None):
        started['flag'] = True
        _time.sleep(SLEEP)
        return [{'kind': 'reinforced', 'summary': 'x', 'pending': False, 'id': ''}]

    # The daemon body imports run_profile_consolidation from this module.
    monkeypatch.setattr(
        'lib.memory.profile_consolidate.run_profile_consolidation',
        _slow_consolidate)
    # Don't touch the DB / event bus from the daemon in this test.
    monkeypatch.setattr(cr, 'append_event', lambda *a, **k: None)
    monkeypatch.setattr(cr, '_patch_assistant_message_with_prefs',
                        lambda *a, **k: None)

    task = {'id': 'deadbeefcafef00d', 'convId': 'c1',
            '_profileConsolidateEligible': True}

    t0 = _time.time()
    cr._spawn_async_profile_consolidation(task, [{'role': 'user', 'content': 'hi'}],
                                          cfg={})
    elapsed = _time.time() - t0
    # Spawn returned essentially instantly — NOT after the LLM sleep.
    assert elapsed < SLEEP / 2, f'spawn blocked for {elapsed:.2f}s'

    # And the daemon really did start the (slow) work in the background.
    deadline = _time.time() + 1.0
    while not started['flag'] and _time.time() < deadline:
        _time.sleep(0.02)
    assert started['flag'], 'consolidation daemon never started'


def test_consolidation_gated_off_spawns_nothing(monkeypatch):
    """No thread is spawned when ineligible (memory off / error / no id)."""
    from lib.tasks_pkg import commit_round as cr
    calls = {'n': 0}

    def _boom(messages, task=None):
        calls['n'] += 1
        return []
    monkeypatch.setattr(
        'lib.memory.profile_consolidate.run_profile_consolidation', _boom)

    # ineligible: flag false
    cr._spawn_async_profile_consolidation(
        {'id': 'x' * 16, 'convId': 'c', '_profileConsolidateEligible': False},
        [], cfg={})
    # error present
    cr._spawn_async_profile_consolidation(
        {'id': 'x' * 16, 'convId': 'c', 'error': 'boom',
         '_profileConsolidateEligible': True}, [], cfg={})
    import time as _time
    _time.sleep(0.2)
    assert calls['n'] == 0


# ───────── tiered profile: relevance gating (core always-on, detail gated) ─────────

_TIERED_PROFILE = (
    '## Preferences\n'
    '- Uses ruff for Python linting\n'
    '- Prefers measurement-first optimization\n'
    '## About the user\n'
    '- Builds Spanish-to-Chinese translation using TDD\n'
    '- Maintains the FMG grader for CJK patch scoring\n'
    '- Works with DolphinFS storage on large data engines'
)


def test_split_profile_tiers_separates_core_and_detail(tmp_data_dir):
    """## Preferences → always-on core; other sections → relevance-gated detail."""
    from lib.memory import user_profile as up
    up.save_profile(_TIERED_PROFILE)
    core, detail = up.split_profile_tiers()
    # Core carries only the work-style header + its bullets.
    assert '## Preferences' in core
    assert 'Uses ruff for Python linting' in core
    assert 'Prefers measurement-first optimization' in core
    # Detail tier is the identity facts, header-tagged, one item per bullet.
    assert 'About the user' not in core
    assert any('Spanish-to-Chinese translation' in d for d in detail)
    assert any('FMG grader' in d for d in detail)
    assert len(detail) == 3
    # Every detail item keeps its section header for context.
    assert all(d.startswith('About the user: ') for d in detail)


def test_headerless_leading_bullets_default_to_core(tmp_data_dir):
    """A bullet with no preceding ## is a standing instruction → core."""
    from lib.memory import user_profile as up
    up.save_profile('- Always answer in Chinese\n## About the user\n- Likes Rust')
    core, detail = up.split_profile_tiers()
    assert 'Always answer in Chinese' in core
    assert any('Likes Rust' in d for d in detail)


def test_render_tiers_core_always_present_detail_gated(tmp_data_dir):
    """Core block is emitted regardless of query; detail only when relevant."""
    from lib.memory import user_profile as up
    up.save_profile(_TIERED_PROFILE)

    # Relevant turn → detail surfaces the matching identity fact.
    core, detail = up.render_profile_tiers(
        query='fix the spanish to chinese translation tests')
    assert core is not None and '[USER PREFERENCE PROFILE]' in core
    assert 'Uses ruff' in core                       # core present
    assert detail is not None
    assert 'Spanish-to-Chinese translation' in detail
    assert 'FMG grader' not in detail                # only the relevant bullet
    assert '[USER PREFERENCE PROFILE — relevant detail]' in detail

    # Irrelevant turn → core still present, NO detail block at all.
    core2, detail2 = up.render_profile_tiers(
        query='make this button border slightly rounder in CSS')
    assert core2 == core                             # byte-identical core
    assert detail2 is None

    # Empty query → no detail (nothing to relevance-match).
    core3, detail3 = up.render_profile_tiers(query='')
    assert core3 == core
    assert detail3 is None


def test_core_block_is_byte_stable_across_queries(tmp_data_dir):
    """The always-on core must not vary with the turn — that's its whole point
    (cache stability). Different queries → identical core block."""
    from lib.memory import user_profile as up
    up.save_profile(_TIERED_PROFILE)
    cores = {up.render_profile_tiers(query=q)[0]
             for q in ('translation', 'css tweak', 'FMG grader', '', 'random')}
    assert len(cores) == 1                            # exactly one distinct core


def _tiered_messages():
    """Post-first-round message list with the _isMeta carrier, for the full
    _inject_system_contexts path."""
    return [
        {'role': 'system', 'content': [{'type': 'text',
            'text': 'IMPORTANT: You must NEVER generate or guess URLs static'}]},
        {'role': 'user', 'content': '[PROJECT CO-PILOT MODE] ctx', '_isMeta': True},
        {'role': 'user', 'content': '__QUERY__'},
    ]


def _isMeta_text(messages):
    """Concatenated text of the _isMeta carrier (where profile blocks land)."""
    for m in messages:
        if m.get('role') == 'user' and m.get('_isMeta'):
            c = m.get('content', '')
            if isinstance(c, str):
                return c
            return ''.join(b.get('text', '') for b in c if isinstance(b, dict))
    return ''


def test_inject_tiered_relevance_gating_end_to_end(tmp_data_dir):
    """The headline acceptance test (per the brief): through the real
    _inject_system_contexts, the core profile is ALWAYS on the _isMeta tail,
    the detail tier appears ONLY on a relevant turn, and is ABSENT on an
    irrelevant turn — with neither ever touching messages[0]."""
    from lib.tasks_pkg.system_context import _inject_system_contexts
    from lib.memory import user_profile as up
    up.save_profile(_TIERED_PROFILE)

    def _run(query):
        msgs = _tiered_messages()
        msgs[2]['content'] = query
        task = {'config': {'preferencesEnabled': True}}
        _inject_system_contexts(
            msgs, project_path='', project_enabled=False,
            memory_enabled=True, search_enabled=False, swarm_enabled=False,
            has_real_tools=True, conv_id='', task=task)
        return msgs

    # ── Relevant turn (translation) ──
    m_rel = _run('please fix the spanish to chinese translation tests for peru terms')
    meta_rel = _isMeta_text(m_rel)
    sys_rel = m_rel[0]['content'][0]['text']
    # Core present on the tail; detail present with the matching fact.
    assert '[USER PREFERENCE PROFILE]' in meta_rel
    assert 'Uses ruff' in meta_rel                              # core
    assert '[USER PREFERENCE PROFILE — relevant detail]' in meta_rel
    assert 'Spanish-to-Chinese translation' in meta_rel        # gated-in
    assert 'FMG grader' not in meta_rel                        # gated-out
    # Profile NEVER touches the system prefix.
    assert 'USER PREFERENCE PROFILE' not in sys_rel

    # ── Irrelevant turn (CSS) ──
    m_irr = _run('make the submit button border a little rounder in the css')
    meta_irr = _isMeta_text(m_irr)
    assert '[USER PREFERENCE PROFILE]' in meta_irr              # core still on
    assert 'Uses ruff' in meta_irr
    # NO detail block, none of the identity facts leaked in.
    assert '[USER PREFERENCE PROFILE — relevant detail]' not in meta_irr
    assert 'Spanish-to-Chinese translation' not in meta_irr
    assert 'FMG grader' not in meta_irr
    assert 'DolphinFS' not in meta_irr

    # Core block byte-identical between the two turns (cache-stable).
    def _core_segment(meta):
        start = meta.index('[USER PREFERENCE PROFILE]')
        end = meta.index('</system-reminder>', start)
        return meta[start:end]
    assert _core_segment(meta_rel) == _core_segment(meta_irr)


def test_inject_tiered_detail_is_cache_safe(tmp_data_dir):
    """Re-injecting the tiered blocks each round + notify_compaction must NOT
    flag prefix_mutation — same guarantee the single-block version had, now
    with the per-turn detail block riding the same tail."""
    from lib.tasks_pkg.cache_tracking import (detect_cache_break,
                                              notify_compaction, _cache_states)
    from lib.tasks_pkg.system_context import _append_user_profile_block
    from lib.memory import user_profile as up
    up.save_profile(_TIERED_PROFILE)
    core, detail = up.render_profile_tiers(
        query='fix the spanish to chinese translation')
    assert core and detail
    conv = 'prof-tier-cache'
    _cache_states.pop(conv, None)

    def _round(tail):
        m = [
            {'role': 'system', 'content': 'static system prompt'},
            {'role': 'user', 'content': '[PROJECT CO-PILOT MODE] ctx',
             '_isMeta': True},
            {'role': 'user', 'content': 'do the thing'},
            {'role': 'assistant', 'content': 'working'},
            {'role': 'tool', 'content': tail},
        ]
        _append_user_profile_block(m, core, marker='[USER PREFERENCE PROFILE]')
        _append_user_profile_block(
            m, detail, marker='[USER PREFERENCE PROFILE — relevant detail]')
        return m

    m1 = _round('tool 1')
    notify_compaction(conv)
    assert detect_cache_break(conv, m1, None, 'claude-opus-4',
        usage={'cache_creation_input_tokens': 50000,
               'cache_read_input_tokens': 20000}) is None
    for i, tail in enumerate(['tool 2', 'tool 3'], start=2):
        m = _round(tail)
        notify_compaction(conv)
        r = detect_cache_break(conv, m, None, 'claude-opus-4',
            usage={'cache_creation_input_tokens': 2000,
                   'cache_read_input_tokens': 70000})
        assert r is None or 'prefix_mutation' not in r, (
            f'round {i} falsely flagged: {r}')
    assert _cache_states[conv].total_breaks == 0


# ───────── chip honesty: applied_profile_items mirrors the injected tiers ─────────

def test_applied_profile_items_mirrors_injection(tmp_data_dir):
    """The chip payload (applied_profile_items) must equal EXACTLY what
    render_profile_tiers injects: full core + only the relevance-selected
    detail — never an arbitrary first-N slice. This is the "frontend shows the
    real data" guarantee."""
    from lib.memory import user_profile as up
    up.save_profile(_TIERED_PROFILE)

    # Relevant turn → core (all) + the matching detail bullet only.
    applied = up.applied_profile_items(
        _TIERED_PROFILE, query='fix the spanish to chinese translation tests')
    assert applied['core'] == ['Uses ruff for Python linting',
                               'Prefers measurement-first optimization']
    assert applied['detail'] == [
        'About the user: Builds Spanish-to-Chinese translation using TDD']
    assert 'FMG grader' not in ' '.join(applied['detail'])  # irrelevant excluded

    # The chip's detail MUST be a subset of the injected detail block (agree).
    _core_blk, _detail_blk = up.render_profile_tiers(
        _TIERED_PROFILE, query='fix the spanish to chinese translation tests')
    assert _detail_blk is not None
    for d in applied['detail']:
        assert d in _detail_blk

    # Irrelevant turn → core present, detail EMPTY (matches absent detail block).
    applied_irr = up.applied_profile_items(
        _TIERED_PROFILE, query='make the button border rounder in css')
    assert applied_irr['core']                 # core always reported
    assert applied_irr['detail'] == []         # nothing irrelevant in the chip
    _c2, _d2 = up.render_profile_tiers(
        _TIERED_PROFILE, query='make the button border rounder in css')
    assert _d2 is None                         # ...and none injected either


def test_applied_profile_items_empty_profile(tmp_data_dir):
    from lib.memory import user_profile as up
    a = up.applied_profile_items('', query='anything')
    assert a == {'core': [], 'detail': []}


def test_consolidation_daemon_emits_preference_learned(monkeypatch):
    """The daemon body produces preference_learned events + stashes on task."""
    from lib.tasks_pkg import commit_round as cr

    learned = [{'kind': 'pending', 'summary': 'Prefers TypeScript',
                'pending': True, 'id': 'abc123'}]
    monkeypatch.setattr(
        'lib.memory.profile_consolidate.run_profile_consolidation',
        lambda messages, task=None: learned)

    events = []
    monkeypatch.setattr(cr, 'append_event',
                        lambda task, ev: events.append(ev))
    monkeypatch.setattr(cr, '_patch_assistant_message_with_prefs',
                        lambda *a, **k: None)

    task = {'id': 'feedface0000', 'convId': 'c1'}
    # Run the daemon body synchronously (no thread) for a deterministic assert.
    cr._run_profile_consolidation_async(task, [{'role': 'user', 'content': 'hi'}])

    assert task['_preferencesLearned'] == learned
    pl = [e for e in events if e.get('type') == 'preference_learned']
    assert len(pl) == 1
    assert pl[0]['kind'] == 'pending' and pl[0]['id'] == 'abc123'
    assert pl[0]['pending'] is True
