"""Terminal auto-translate durability: durable-runtime commit, failure verdict, sweep.

Architecture under test (post-migration): the whole-turn terminal DELIVERABLE
commits through the durable ``_translate_runtime`` TaskRuntime — the same
authority the boot sweep and goal-mode per-turn boundary use — never through
the in-memory ``_Accumulator`` daemon thread. A provider failure or process
restart therefore leaves a persisted ``_translateFailed`` verdict on the turn
projection (``mark_turn_translation_failed``), and the boot sweep re-spawns
any settled turn that still lacks a translation. The accumulator survives
only to stamp its cached preview translations onto the projection's segments
(``finalize_incremental_stamp_only``).
"""

import pytest


pytestmark = pytest.mark.unit


def _task(**overrides):
    task = {
        'id': 'task-terminal-durable',
        'convId': 'conv-terminal-durable',
        '_turnId': 'turn-terminal-durable',
        'config': {'autoTranslate': True, 'uiLang': 'zh'},
        '_userId': 1,
    }
    task.update(overrides)
    return task


# ── 1. Root turn routes to the DURABLE spawn, not the accumulator ────────

def test_root_turn_commits_through_durable_spawn(monkeypatch):
    """The terminal coordinator must spawn a durable runtime task for the
    root turn (migration off the in-memory accumulator) AND hand the live
    accumulator a stamp-only finalize for its cached previews."""
    import lib.translate.terminal as terminal

    calls = []

    # Import before patching lifecycle so commit's module-level repository
    # reference cannot capture a mock and leak into later authority tests.
    import lib.translate.commit as commit
    monkeypatch.setattr(
        terminal, '_spawn_whole_turn_translation',
        lambda **kw: calls.append(('spawn', kw['turn_id'])))
    monkeypatch.setattr(
        'lib.translate.incremental.finalize_incremental_stamp_only',
        lambda _task: calls.append(('stamp_only', 'root')) or True)
    monkeypatch.setattr(
        'lib.translate.incremental.cancel_incremental', lambda _task: True)
    monkeypatch.setattr(
        'lib.translate.incremental.submit_thinking_segment',
        lambda *a, **k: False)

    import types

    def _turn(conv_id, turn_id, *, user_id):
        return {'status': 'completed', 'turnId': turn_id,
                'projection': {'content': 'hello world'}}

    monkeypatch.setattr('lib.turn_lifecycle.get_turn', _turn)
    monkeypatch.setattr(commit, 'get_turn', _turn)
    monkeypatch.setattr(
        'lib.text_lang.detect_language',
        lambda text, force_fasttext=False: types.SimpleNamespace(code='en'))

    task = _task(_turnId='root', _turnVisibleRunTurnIds=['root'])
    terminal._translate_settled_turns(task)

    assert ('spawn', 'root') in calls
    assert ('stamp_only', 'root') in calls


def test_root_turn_durable_spawn_carries_message_id(monkeypatch):
    """The durable spawn for the root turn forwards ``_assistantMsgId`` so
    the live preview addresses the same message the committed translation
    will overlay."""
    import lib.translate.terminal as terminal

    spawned = {}

    import lib.translate.commit as commit
    monkeypatch.setattr(
        terminal, '_spawn_whole_turn_translation',
        lambda **kw: spawned.update(kw))
    monkeypatch.setattr(
        'lib.translate.incremental.finalize_incremental_stamp_only',
        lambda _task: True)
    monkeypatch.setattr(
        'lib.translate.incremental.cancel_incremental', lambda _task: True)
    monkeypatch.setattr(
        'lib.translate.incremental.submit_thinking_segment',
        lambda *a, **k: False)

    import types
    monkeypatch.setattr(
        'lib.turn_lifecycle.get_turn',
        lambda conv_id, turn_id, *, user_id: {
            'status': 'completed', 'turnId': turn_id,
            'projection': {'content': 'hello world'}})
    monkeypatch.setattr(
        commit, 'get_turn',
        lambda conv_id, turn_id, *, user_id: {
            'status': 'completed', 'turnId': turn_id,
            'projection': {'content': 'hello world'}})
    monkeypatch.setattr(
        'lib.text_lang.detect_language',
        lambda text, force_fasttext=False: types.SimpleNamespace(code='en'))

    task = _task(_turnId='root', _turnVisibleRunTurnIds=['root'],
                 _assistantMsgId='msg-abc')
    terminal._translate_settled_turns(task)

    assert spawned['turn_id'] == 'root'
    assert spawned['message_id'] == 'msg-abc'


# ── 2. Failure verdict on the projection (durable recovery lever) ────────

def test_commit_merge_clears_failure_verdict_on_success():
    from lib.translate.commit import _merge_translation

    projection = {
        'content': 'hello world',
        '_translateFailed': {'reason': 'terminal_attempts_exhausted'},
    }
    merged, changed = _merge_translation(
        projection,
        field='translatedContent',
        translated_text='译文',
        model='m',
        segment_translations={},
    )
    assert changed is True
    assert merged['translatedContent'] == '译文'
    assert merged['_translateDone'] is True
    assert '_translateFailed' not in merged


def test_failure_verdict_does_not_regress_committed_turn(monkeypatch):
    import lib.translate.commit as commit

    monkeypatch.setattr(
        commit,
        'get_turn',
        lambda conv, turn, user_id=None: {
            'projection': {'content': 'x', '_translateDone': True},
            'projectionRevision': 3,
        },
    )
    monkeypatch.setattr(
        commit,
        'update_turn_projection',
        lambda *a, **k: pytest.fail('must not write over a committed turn'),
    )
    result = commit.mark_turn_translation_failed(
        'conv', 'turn', user_id=1, reason='x', error='y')
    assert result is not None


def test_worker_settle_error_persists_failure_verdict(monkeypatch):
    """A durable-runtime translation that dies records ``_translateFailed``
    on the turn projection via the worker's settle-error path, sourcing the
    owner from the task's ``_userId``."""
    import lib.translate.runtime._worker as worker

    marked = []
    monkeypatch.setattr(
        'lib.translate.commit.mark_turn_translation_failed',
        lambda conv, turn, *, user_id, reason, error: marked.append(
            (conv, turn, user_id, reason)))

    task = {
        'id': 't', 'convId': 'conv-x', '_turnId': 'turn-x', '_userId': 7,
    }
    worker._settle_error(
        task,
        't',
        ValueError('Empty translation result'),
        'conv-x',
        'turn-x',
        'msg-1',
        'translatedContent',
    )

    assert marked == [('conv-x', 'turn-x', 7, 'worker_error')]


# ── 3. Boot sweep re-spawns untranslated settled turns ───────────────────

def test_sweep_respawns_only_untranslated_settled_turns(monkeypatch):
    import lib.translate.terminal as terminal

    spawned = []
    monkeypatch.setattr(
        terminal, '_spawn_whole_turn_translation',
        lambda **kw: spawned.append(kw) is None)

    def _skip(content, target, target_code):
        return content == 'already chinese'

    import lib.translate.skip_policy as skip_policy
    monkeypatch.setattr(
        skip_policy, 'should_skip_automatic_translation', _skip)

    import lib.turn_lifecycle as lifecycle
    monkeypatch.setattr(
        lifecycle,
        'list_turns',
        lambda conv, user_id=None, light=False: {
            'turns': [
                # untranslated settled turn -> respawn
                {'turnId': 't1', 'status': 'completed',
                 'projection': {'content': 'hello world'}},
                # already translated -> skip
                {'turnId': 't2', 'status': 'completed',
                 'projection': {'content': 'x', 'translatedContent': 'y'}},
                # explicitly done -> skip
                {'turnId': 't3', 'status': 'completed',
                 'projection': {'content': 'x', '_translateDone': True}},
                # still running -> skip
                {'turnId': 't4', 'status': 'running',
                 'projection': {'content': 'hello world'}},
                # failed verdict -> eligible again, respawn
                {'turnId': 't5', 'status': 'completed',
                 'projection': {'content': 'hello world',
                                '_translateFailed': {'reason': 'x'}}},
                # empty content -> skip
                {'turnId': 't6', 'status': 'completed',
                 'projection': {'content': '  '}},
            ]
        },
    )

    count = terminal.sweep_conversation_translations(
        'conv', user_id=1,
        config={'autoTranslate': True, 'uiLang': 'zh'})
    assert count == 2
    assert {s['turn_id'] for s in spawned} == {'t1', 't5'}


def test_sweep_noop_when_auto_translate_off(monkeypatch):
    import lib.translate.terminal as terminal

    monkeypatch.setattr(
        terminal, '_spawn_whole_turn_translation',
        lambda **kw: pytest.fail('must not spawn when autoTranslate off'))
    assert terminal.sweep_conversation_translations(
        'conv', user_id=1, config={'autoTranslate': False}) == 0


def test_sweep_is_fail_soft_on_turn_listing_error(monkeypatch):
    import lib.translate.terminal as terminal
    import lib.turn_lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle,
        'list_turns',
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('storage down')))
    assert terminal.sweep_conversation_translations(
        'conv', user_id=1,
        config={'autoTranslate': True, 'uiLang': 'zh'}) == 0


@pytest.mark.parametrize('use_default_clock', [True, False])
def test_boot_sweep_reads_recent_owned_conversations(monkeypatch, use_default_clock):
    """The production clock path must reach the bounded recovery sweep."""
    import time

    import lib.translate.terminal as terminal

    now_ms = int(time.time() * 1000)
    catalog_calls = []
    swept = []

    def catalog(**kwargs):
        catalog_calls.append(kwargs)
        return [
            {'id': 'recent', 'updated_at': now_ms,
             'settings': {'autoTranslate': True, 'uiLang': 'zh'}},
            {'id': 'old',
             'updated_at': now_ms - terminal._BOOT_SWEEP_MAX_AGE_MS - 60_000},
        ]

    monkeypatch.setattr(
        'lib.conversations.catalog.list_conversation_metadata', catalog,
    )
    monkeypatch.setattr(
        terminal, 'sweep_conversation_translations',
        lambda conv, **kwargs: swept.append((conv, kwargs)) or 2,
    )

    kwargs = {} if use_default_clock else {'now_ms': now_ms}
    summary = terminal.run_boot_translation_sweep(user_id=7, **kwargs)

    assert summary == {'scanned': 1, 'swept': 1, 'respawned': 2}
    assert catalog_calls == [{
        'user_id': 7, 'limit': terminal._BOOT_SWEEP_CONVERSATION_LIMIT,
        'order_by': 'updated_at_desc', 'settings_keys': ('autoTranslate', 'uiLang'),
    }]
    assert swept == [('recent', {
        'user_id': 7, 'config': {'autoTranslate': True, 'uiLang': 'zh'},
    })]


@pytest.mark.parametrize('projection', [
    {'content': ''},
    {'content': 'answer', 'translatedContent': '已有译文'},
])
def test_root_segment_handoff_does_not_depend_on_deliverable(monkeypatch, projection):
    import lib.translate.terminal as terminal

    handed_off = []
    monkeypatch.setattr(
        'lib.turn_lifecycle.get_turn',
        lambda *args, **kwargs: {'status': 'completed', 'projection': projection},
    )
    monkeypatch.setattr(
        'lib.translate.incremental.finalize_incremental_stamp_only',
        lambda task: handed_off.append(task['id']) or True,
    )
    monkeypatch.setattr(
        'lib.translate.incremental.cancel_incremental',
        lambda task: pytest.fail('completed segment handoff must not be cancelled'),
    )
    monkeypatch.setattr(
        terminal, '_spawn_whole_turn_translation',
        lambda **kwargs: pytest.fail('no deliverable translation is needed'),
    )

    task = _task()
    terminal._translate_settled_turns(task)
    assert handed_off == [task['id']]
