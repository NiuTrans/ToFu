"""Tests for lib.tasks_pkg.auto_translate — the server-side safety net.

The safety net (``_maybe_auto_translate_assistant``) is the server-side
guarantee that an assistant reply gets translated even when the frontend is
offline. It also OWNS the lifecycle of any incremental per-round accumulator
the orchestrator created during the task: it must either hand off to
``finalize_incremental`` (the autoTranslate-on happy path) or tear the
accumulator down via ``cancel_incremental`` on every skip path. A dangling
accumulator was the root cause of the recurring
``worker idle-timeout — abandoning (finalize never called)`` warnings and the
"I have to click Translate every time" bug: its pre-translated segments were
silently discarded and nothing committed a translation.

These tests inject a fake DB (the function accepts a ``db`` param) and stub the
incremental hand-off / cancel so no real LLM, push, or DB is touched.
"""

import json

import lib.tasks_pkg.auto_translate as at


class _FakeRow(tuple):
    """A 2-col row (messages, settings) indexable like the real sqlite row."""


class _FakeDB:
    """Minimal stand-in for the thread-local chat DB connection.

    Only ``execute(...).fetchone()`` is exercised by the skip paths under test
    (none of them reach an UPDATE).
    """

    def __init__(self, messages, settings):
        self._row = _FakeRow((json.dumps(messages), json.dumps(settings)))

    def execute(self, sql, params=()):
        self._last_sql = sql
        return self

    def fetchone(self):
        # The safety net's first query selects (messages, settings).
        return self._row


def _make_task(task_id='t-net', auto=True):
    return {'id': task_id, 'convId': 'conv-net', 'config': {'autoTranslate': auto}}


def _install_accumulator_spy(monkeypatch):
    """Replace finalize/stamp-only/cancel with spies and report which fired."""
    calls = {'finalize': 0, 'cancel': 0, 'stamp_only': 0}

    def _fake_finalize(task, conv_id, msg_idx, content, msg_id=None, target=None):
        calls['finalize'] += 1
        calls['target'] = target
        return True  # pretend an accumulator existed and we took ownership

    def _fake_stamp_only(task, conv_id, msg_idx, msg_id=None):
        calls['stamp_only'] += 1
        return True  # pretend an accumulator existed and we stamped its cache

    def _fake_cancel(task):
        calls['cancel'] += 1
        return True

    monkeypatch.setattr('lib.translate.finalize_incremental', _fake_finalize)
    monkeypatch.setattr('lib.translate.finalize_incremental_stamp_only',
                        _fake_stamp_only)
    monkeypatch.setattr('lib.translate.cancel_incremental', _fake_cancel)
    return calls


def test_autotranslate_off_cancels_accumulator(monkeypatch):
    """settings.autoTranslate=False → early return, but a dangling accumulator
    (created because task.config.autoTranslate was True — the gate divergence)
    MUST be cancelled, not orphaned."""
    calls = _install_accumulator_spy(monkeypatch)
    db = _FakeDB(messages=[{'role': 'assistant', 'content': 'Hello world'}],
                 settings={'autoTranslate': False})
    task = _make_task(auto=True)  # config says ON, settings say OFF → divergence

    at._maybe_auto_translate_assistant('conv-net', 'Hello world', 0, db=db, task=task)

    assert calls['finalize'] == 0, 'must not finalize when settings.autoTranslate=False'
    assert calls['cancel'] == 1, 'orphaned accumulator must be cancelled on the skip path'


def test_already_chinese_stamps_cached_narration_not_discards(monkeypatch):
    """★ FIX #1: already-target content → the DELIVERABLE needs no
    translatedContent, but the accumulator already translated the inter-round
    narration LIVE. That path must STAMP the cached narration (stamp-only
    finalize) and take ownership — NOT cancel it (which threw the Chinese away,
    the reported loss). No whole-message finalize either (no deliverable to
    translate)."""
    calls = _install_accumulator_spy(monkeypatch)
    zh = '你好世界，这是一段中文内容用于测试。'
    db = _FakeDB(messages=[{'role': 'assistant', 'content': zh}],
                 settings={'autoTranslate': True})
    task = _make_task(auto=True)

    at._maybe_auto_translate_assistant('conv-net', zh, 0, db=db, task=task)

    assert calls['finalize'] == 0, 'no deliverable translation on the already-target path'
    assert calls['stamp_only'] == 1, 'cached narration must be stamped, not discarded'
    assert calls['cancel'] == 0, 'stamp-only took ownership → the finally must NOT cancel'


def test_happy_path_hands_off_and_does_not_cancel(monkeypatch):
    """English content + autoTranslate on + an accumulator → finalize takes
    ownership and cancel must NOT fire (it would double-handle)."""
    calls = _install_accumulator_spy(monkeypatch)
    db = _FakeDB(messages=[{'role': 'assistant', 'content': 'A plain english reply.'}],
                 settings={'autoTranslate': True})
    task = _make_task(auto=True)

    at._maybe_auto_translate_assistant('conv-net', 'A plain english reply.', 0,
                                       db=db, task=task)

    assert calls['finalize'] == 1, 'happy path must hand off to finalize'
    assert calls['cancel'] == 0, 'must NOT cancel after a successful finalize handoff'


def test_no_task_skips_cancel(monkeypatch):
    """When called without a task (legacy / no incremental accumulator), the
    finally block must not attempt a cancel."""
    calls = _install_accumulator_spy(monkeypatch)
    db = _FakeDB(messages=[{'role': 'assistant', 'content': 'english reply'}],
                 settings={'autoTranslate': False})

    at._maybe_auto_translate_assistant('conv-net', 'english reply', 0, db=db, task=None)

    assert calls['cancel'] == 0, 'no task → no cancel attempt'
    assert calls['finalize'] == 0
