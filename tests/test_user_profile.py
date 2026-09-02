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
    """Redirect the server data dir so the profile lands in a tmp tree.

    ``lib.runtime_paths`` freezes its ``_BASE`` (and thus ``data_root()``) at
    import time, so setting ``$TOFU_DATA_DIR`` here is a no-op — the profile
    path would still resolve to the real ``<repo>/data`` and, on a dev box that
    already has a ``.tofu_user_profile.md``, the test would read the operator's
    live profile instead of an empty tmp tree. Patch ``_server_data_dir``
    directly (the single seam every profile path resolves through) so the
    redirect actually takes effect regardless of the frozen ``_BASE``."""
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv('TOFU_DATA_DIR', d)
        monkeypatch.setattr('lib.memory.storage._server_data_dir',
                            lambda: d)
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
    import lib.memory.user_profile as up
    assert up.load_profile() == ''  # none yet
    res = up.save_profile('## Style\n- Replies in Chinese\n- Concise')
    assert res['saved'] and res['chars'] > 0 and not res['over_cap']
    assert os.path.isfile(up.profile_path())
    body = up.load_profile()
    assert 'Replies in Chinese' in body


def test_empty_save_clears_file(tmp_data_dir):
    import lib.memory.user_profile as up
    up.save_profile('- something')
    assert os.path.isfile(up.profile_path())
    up.save_profile('   ')
    assert not os.path.isfile(up.profile_path())
    assert up.load_profile() == ''


def test_over_cap_write_is_rejected_without_partial_save(tmp_data_dir):
    import lib.memory.user_profile as up
    big = '- ' + ('x' * (up.USER_PROFILE_CHAR_CAP + 500))
    res = up.save_profile(big)
    assert not res['saved'] and res['over_cap']
    assert up.load_profile() == ''


def test_render_block_and_summary(tmp_data_dir):
    import lib.memory.user_profile as up
    assert up.render_profile_block('') is None
    up.save_profile('## Prefs\n- Likes TypeScript\n- No unsolicited refactors')
    block = up.render_profile_block()
    assert block.startswith('<system-reminder>')
    assert '[USER CONTEXT]' in block
    assert 'Likes TypeScript' in block
    items = up.profile_summary_for_event()
    assert items == ['Likes TypeScript', 'No unsolicited refactors']


def test_event_types_registered():
    from lib.agent_core.events import event_types
    et = event_types()
    assert 'preferences_applied' in et


# ───────────────────────── injection placement ─────────────────────────







def test_apply_reinforcement_replaces_in_place(tmp_data_dir):
    import lib.memory.user_profile as up
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
    import lib.memory.user_profile as up
    up.save_profile('- dup line\n- dup line')
    res = up.apply_reinforcement('- dup line', '- changed')
    assert res['matched'] is False and res['saved'] is False
    assert 'changed' not in up.load_profile()


def test_pending_stage_resolve_accept(tmp_data_dir):
    import lib.memory.user_profile as up
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
    import lib.memory.user_profile as up
    entry = up.stage_pending({'text': 'Likes verbose logs'})
    res = up.resolve_pending(entry['id'], accept=False)
    assert res['resolved'] and not res['accepted']
    assert 'verbose' not in up.load_profile()
    assert up.load_pending() == []


def test_stage_pending_is_idempotent(tmp_data_dir):
    import lib.memory.user_profile as up
    a = up.stage_pending({'text': 'Same pref'})
    b = up.stage_pending({'text': 'Same pref'})
    assert a['id'] == b['id']
    assert len(up.load_pending()) == 1


def test_pending_is_tenant_scoped_and_concurrent_staging_loses_nothing(
        tmp_data_dir):
    from concurrent.futures import ThreadPoolExecutor
    import lib.memory.user_profile as up

    with ThreadPoolExecutor(max_workers=8) as pool:
        entries = list(pool.map(
            lambda index: up.stage_pending(
                {'text': f'private preference {index}'}, scope='tenant-a'),
            range(32)))

    assert len({entry['id'] for entry in entries}) == 32
    assert len(up.load_pending('tenant-a')) == 32
    assert up.load_pending('tenant-b') == []
    assert up.load_pending() == []


def test_concurrent_identical_pending_proposals_share_one_record(
        tmp_data_dir):
    from concurrent.futures import ThreadPoolExecutor
    import lib.memory.user_profile as up

    with ThreadPoolExecutor(max_workers=8) as pool:
        entries = list(pool.map(
            lambda _index: up.stage_pending(
                {'text': 'one preference'}, scope='tenant-a'),
            range(24)))

    assert len({entry['id'] for entry in entries}) == 1
    assert len(up.load_pending('tenant-a')) == 1


def test_failed_pending_accept_releases_claim_and_preserves_proposal(
        tmp_data_dir, monkeypatch):
    import lib.memory.user_profile as up
    from lib.memory.user_profile import _pending

    entry = up.stage_pending({'text': 'keep on failure'}, scope='tenant-a')
    monkeypatch.setattr(
        _pending, 'apply_new_preference',
        lambda *_args, **_kwargs: {'saved': False, 'over_cap': False})

    result = up.resolve_pending(entry['id'], True, scope='tenant-a')

    assert result['error'] == 'profile_save_failed'
    assert up.load_pending('tenant-a') == [entry]


def test_pending_resolution_claim_allows_only_one_consumer(
        tmp_data_dir, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import lib.memory.user_profile as up
    from lib.memory.user_profile import _pending

    entry = up.stage_pending({'text': 'consume once'}, scope='tenant-a')
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def _apply(*_args, **_kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return {'saved': True, 'over_cap': False}

    monkeypatch.setattr(_pending, 'apply_new_preference', _apply)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            up.resolve_pending, entry['id'], True, None, 'tenant-a')
        assert entered.wait(5)
        second = pool.submit(
            up.resolve_pending, entry['id'], True, None, 'tenant-a')
        second_result = second.result(timeout=5)
        release.set()
        first_result = first.result(timeout=5)

    assert first_result['resolved'] is True
    assert second_result['busy'] is True
    assert calls == [1]
    assert up.load_pending('tenant-a') == []


def test_confirmed_preference_retry_is_idempotent(tmp_data_dir):
    import lib.memory.user_profile as up

    first = up.apply_new_preference('No duplicate bullets')
    second = up.apply_new_preference('No duplicate bullets')

    assert first['saved'] and second['saved']
    assert second['already_present'] is True
    assert up.load_profile().count('- No duplicate bullets') == 1


# ───────── consolidation never rewrites the whole user document ─────────

def test_consolidation_ignores_legacy_distil_action(tmp_data_dir, monkeypatch):
    """The model cannot compress or rewrite unrelated durable context."""
    import lib.memory.user_profile as up
    import lib.memory.profile_consolidate as pc

    original = '## Preferences\n- Reply in Chinese\n## About the user\n- Works at Meituan'
    assert up.save_profile(original)['saved']
    before = up.load_profile()

    def _fake_dispatch(messages, **kw):
        return (json.dumps({'actions': [
            {'kind': 'distil', 'full_profile': '## Preferences\n- terse'}]}), {})

    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', _fake_dispatch)

    msgs = [
        {'role': 'user', 'content': 'please keep being concise and use chinese, '
         'this is a long enough message to clear the surface threshold so the '
         'consolidation pass actually runs and asks the model what to do here.'},
        {'role': 'assistant', 'content': 'understood, I will.'},
    ]
    learned = pc.run_profile_consolidation(msgs)
    assert learned == []
    assert up.load_profile() == before


# ───────── REQUIRED test 2: cross-task profile EDIT is cache-safe ─────────


def test_preference_learned_event_registered():
    from lib.agent_core.events import event_types
    assert 'preference_learned' in event_types()


# ───────── per-user scoping (multi-user isolation) ─────────

def test_empty_scope_uses_global_file_unchanged(tmp_data_dir):
    """scope='' must resolve to the EXACT legacy global path — no migration,
    byte-identical for every open/private personal install."""
    import lib.memory.user_profile as up
    from lib.agent_artifacts import USER_PROFILE_FILE
    p = up.profile_path('')
    assert p.endswith(os.path.join('memories', USER_PROFILE_FILE))
    assert 'profiles' not in p  # NOT under the per-tenant subtree
    # Default arg == explicit '' .
    assert up.profile_path() == up.profile_path('')


def test_scoped_path_isolated_and_traversal_proof(tmp_data_dir):
    import lib.memory.user_profile as up
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
    import lib.memory.user_profile as up
    up.save_profile('## About the user\n- Tenant A is a data scientist', scope='userA')
    up.save_profile('## About the user\n- Tenant B is a frontend dev', scope='userB')
    assert 'data scientist' in up.load_profile('userA')
    assert 'data scientist' not in up.load_profile('userB')
    assert 'frontend dev' in up.load_profile('userB')
    # The global (open/private) profile is untouched by either tenant.
    assert up.load_profile('') == ''


def test_resolve_profile_scope_from_authcontext():
    import lib.memory.user_profile as up
    from lib.api_keys import AuthContext, local_admin_context
    # Personal mode has the same explicit owner boundary as repositories.
    assert up.resolve_profile_scope(local_admin_context()) == '1'
    # An invalid adapter context without an owner has no profile scope.
    assert up.resolve_profile_scope(AuthContext(key_id='k_x')) == ''
    # Multi-user credentials carry the numeric repository owner explicitly.
    assert up.resolve_profile_scope(
        AuthContext(key_id='k_y', owner_user_id=42)) == '42'
    # Robust to None / junk.
    assert up.resolve_profile_scope(None) == ''


def test_consolidation_writes_to_task_scope(tmp_data_dir, monkeypatch):
    """The daemon reads scope off the task and writes the tenant's file, not
    the global one."""
    import json as _json
    import lib.memory.user_profile as up
    import lib.memory.profile_consolidate as pc

    def _fake_dispatch(messages, **kw):
        return (_json.dumps({'actions': [
            {'kind': 'new', 'header': 'About the user',
             'text': 'Is a backend engineer',
             'evidence': 'I work as a backend engineer'}]}), {})

    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', _fake_dispatch)
    msgs = [
        {'role': 'user', 'content': 'fyi I work as a backend engineer, and this '
         'is a sufficiently long message to clear the 200-char surface threshold '
         'so the consolidation pass actually runs the cheap model here please. '
         'This is an explicit durable fact about me that should remain useful '
         'across future conversations and unrelated projects.'},
        {'role': 'assistant', 'content': 'noted, I will remember that for you.'},
    ]
    pc.run_profile_consolidation(msgs, task={'_profileScope': 'tenant7'})
    assert 'backend engineer' in up.load_profile('tenant7')
    assert up.load_profile('') == ''  # global profile NOT touched


# ───────── structured per-item view (settings UI) ─────────

def test_parse_items_groups_by_header(tmp_data_dir):
    import lib.memory.user_profile as up
    up.save_profile('## Preferences\n- Replies in Chinese\n- Concise\n'
                    '## About the user\n- Backend engineer')
    items = up.parse_items()
    assert {'header': 'Preferences', 'text': 'Replies in Chinese'} in items
    assert {'header': 'Preferences', 'text': 'Concise'} in items
    assert {'header': 'About the user', 'text': 'Backend engineer'} in items
    assert len(items) == 3


def test_serialize_items_roundtrips(tmp_data_dir):
    import lib.memory.user_profile as up
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
    import lib.memory.user_profile as up
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
    import lib.memory.user_profile as up
    up.save_profile('- something')
    up.save_items([])
    assert up.load_profile() == ''


# ───────── auto-apply: new prefs/identity are written, not staged ─────────

def test_consolidation_auto_applies_new_preference(tmp_data_dir, monkeypatch):
    """A 'new' action is now WRITTEN immediately (no staging) and surfaced as
    a 'added' learned chip — the user is informed, not asked."""
    import json as _json
    import lib.memory.user_profile as up
    import lib.memory.profile_consolidate as pc

    def _fake_dispatch(messages, **kw):
        return (_json.dumps({'actions': [
            {'kind': 'new', 'header': 'About the user',
             'text': 'Is a backend engineer',
             'evidence': 'I work as a backend engineer'}]}), {})

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
    from lib.tasks_pkg.commit_round import _profile as cr

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
    monkeypatch.setattr(cr, '_patch_turn_with_prefs',
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
    from lib.tasks_pkg.commit_round import _profile as cr
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


# ───────── always-on profile rendering ─────────

_TIERED_PROFILE = (
    '## Preferences\n'
    '- Uses ruff for Python linting\n'
    '- Prefers measurement-first optimization\n'
    '## About the user\n'
    '- Builds Spanish-to-Chinese translation using TDD\n'
    '- Maintains the FMG grader for CJK patch scoring\n'
    '- Works with DolphinFS storage on large data engines'
)

def test_context_block_is_byte_stable_across_reads(tmp_data_dir):
    """The always-on context is stable for the lifetime of one stored value."""
    import lib.memory.user_profile as up
    up.save_profile(_TIERED_PROFILE)
    assert up.render_profile_block() == up.render_profile_block()
def test_context_items_for_event_handles_empty_profile(tmp_data_dir):
    import lib.memory.user_profile as up
    assert up.context_items_for_event('') == []


def test_chip_fires_on_carried_over_profile_turn(tmp_data_dir):
    """REGRESSION: the prefs chip must appear on EVERY turn where the profile
    is in context — not only the turn that freshly injected it.

    The event payload is derived from durable context on every assembly, even
    when the already-rendered context block is carried over in the transcript.
    """
    from lib.tasks_pkg.context_composer import compose_task_context
    import lib.memory.user_profile as up
    up.save_profile(_TIERED_PROFILE)

    def _run(msgs):
        task = {'config': {'preferencesEnabled': True}, '_userId': 1}
        compose_task_context(
            msgs, user_id=1, project_path='', project_enabled=False,
            memory_enabled=True, search_enabled=False,
            has_real_tools=True, conv_id='c1', task=task)
        return msgs, task.get('_appliedPreferences')

    # Turn 1: fresh messages → profile injected, chip set.
    m1, ap1 = _run([
        {'role': 'system', 'content': [{'type': 'text', 'text': 'static sys'}]},
        {'role': 'user', 'content': 'fix the spanish to chinese translation'},
    ])
    assert ap1 is not None and ap1['items']
    # The profile block is now embedded in the (reused) user message.
    assert any('[USER CONTEXT]' in str(m.get('content'))
               for m in m1)

    # Turn 2: REUSE the now-profile-carrying messages + a new user turn —
    # exactly what rebuild_messages_with_history hands the orchestrator.
    _, ap2 = _run(m1 + [
        {'role': 'assistant', 'content': 'done'},
        {'role': 'user', 'content': 'now a css tweak'},
    ])
    # The chip MUST still be set even though nothing was freshly injected.
    assert ap2 is not None, 'prefs chip vanished on the carried-over turn (the bug)'
    assert ap2['items']


def test_consolidation_daemon_emits_preference_learned(monkeypatch):
    """The daemon body produces preference_learned events + stashes on task."""
    from lib.tasks_pkg.commit_round import _profile as cr

    learned = [{'kind': 'pending', 'summary': 'Prefers TypeScript',
                'pending': True, 'id': 'abc123'}]
    monkeypatch.setattr(
        'lib.memory.profile_consolidate.run_profile_consolidation',
        lambda messages, task=None: learned)

    events = []
    monkeypatch.setattr(cr, 'append_event',
                        lambda task, ev: events.append(ev))
    monkeypatch.setattr(cr, '_patch_turn_with_prefs',
                        lambda *a, **k: None)

    task = {'id': 'feedface0000', 'convId': 'c1'}
    # Run the daemon body synchronously (no thread) for a deterministic assert.
    cr._run_profile_consolidation_async(task, [{'role': 'user', 'content': 'hi'}])

    assert task['_preferencesLearned'] == learned
    pl = [e for e in events if e.get('type') == 'preference_learned']
    assert len(pl) == 1
    assert pl[0]['kind'] == 'pending' and pl[0]['id'] == 'abc123'
    assert pl[0]['pending'] is True


def test_profile_block_keeps_every_context_category(tmp_data_dir):
    import lib.memory.user_profile as up

    up.save_context_items([
        {'type': 'identity', 'text': 'Works at Meituan'},
        {'type': 'work_rule', 'condition': 'submitting cluster jobs',
         'action': 'use hope MCP'},
        {'type': 'response_preference', 'text': 'Reply in Chinese'},
    ])
    block = up.render_profile_block()
    assert block is not None
    assert 'Works at Meituan' in block
    assert 'submitting cluster jobs' in block and 'use hope MCP' in block
    assert 'Reply in Chinese' in block


def test_context_composer_injects_all_items_for_unrelated_turn(tmp_data_dir):
    import lib.memory.user_profile as up
    from lib.tasks_pkg.context_composer import compose_task_context

    up.save_context_items([
        {'type': 'identity', 'text': 'Works at Meituan'},
        {'type': 'work_rule', 'condition': 'reading internal docs',
         'action': 'use xuecheng MCP'},
        {'type': 'response_preference', 'text': 'Lead with the conclusion'},
    ])
    messages = [
        {'role': 'system', 'content': 'static'},
        {'role': 'user', 'content': 'make this CSS border rounder'},
    ]
    task = {'config': {'preferencesEnabled': True}, '_userId': 1}
    compose_task_context(
        messages, user_id=1, project_path='', project_enabled=False,
        memory_enabled=False, search_enabled=False,
        has_real_tools=False, conv_id='', task=task)
    text = '\n'.join(str(message.get('content', '')) for message in messages)
    assert '[USER CONTEXT]' in text
    assert 'Works at Meituan' in text
    assert 'reading internal docs' in text and 'use xuecheng MCP' in text
    assert 'Lead with the conclusion' in text
    assert len(task['_appliedPreferences']['items']) == 3


def test_interactive_context_is_independent_from_experience_memory():
    from lib.agent_core.personal_scope import resolve_preferences_enabled

    assert resolve_preferences_enabled({})
    assert resolve_preferences_enabled(None)
    assert not resolve_preferences_enabled({'preferencesEnabled': False})
