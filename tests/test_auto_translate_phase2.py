"""Phase-2 tests for lib.tasks_pkg.auto_translate._maybe_auto_translate_assistant.

Two failure classes the unified default (Phase 1) did NOT fix:

  1. WRONG TARGET — the trigger selected the message by POSITION
     (``len(messages)-1`` / fresh-enumeration index). A concurrent frontend
     write that shifted the row made the translation land on the wrong
     message. Phase 2 resolves the target by stable ``_msgId`` first.

  2. DOUBLE-FIRE — the per-turn endpoint trigger, the end-of-task rescan, and
     a retried safety net could all schedule the same message because dedup
     leaned on ``translatedContent`` existing + a msgIdx-keyed running-task
     scan (both race). Phase 2 adds an atomic per-(conv, msgId) in-flight
     guard claimed BEFORE scheduling.

These tests inject a fake DB and stub the spawn + incremental hand-off so no
real LLM / push / DB / thread is touched. They assert the message the spawned
translate worker is pointed at (by id and resolved index) and that the
in-flight guard is claimed/released correctly.
"""

import json

import pytest

import lib.tasks_pkg.auto_translate as at
import lib.translate.inflight as ifl

pytestmark = pytest.mark.unit


class _FakeDB:
    """execute(...).fetchone() over a (messages, settings) row; UPDATEs no-op."""

    def __init__(self, messages, settings):
        self._messages = messages
        self._settings = settings

    def execute(self, sql, params=()):
        self._last_sql = sql
        return self

    def fetchone(self):
        s = self._last_sql
        if 'SELECT messages, settings' in s:
            return (json.dumps(self._messages), json.dumps(self._settings))
        if 'SELECT updated_at' in s:
            return (123456,)
        return None


@pytest.fixture(autouse=True)
def _clean_inflight():
    with ifl._lock:
        ifl._inflight.clear()
    yield
    with ifl._lock:
        ifl._inflight.clear()


def _spy_spawn(monkeypatch):
    """Replace the spawned translate thread with a synchronous recorder.

    Captures the (msg_idx, msg_id) the worker would translate with — that is
    exactly the target-resolution + id-anchoring contract under test. Also
    runs the closure's body so its in-flight release fires.
    """
    captured = {}

    def _fake_do_translate(task_id, content, target, source, conv_id, msg_idx,
                           field, *, msg_id=None):
        captured['msg_idx'] = msg_idx
        captured['msg_id'] = msg_id
        captured['content'] = content

    monkeypatch.setattr('lib.translate._do_translate', _fake_do_translate)

    class _SyncThread:
        def __init__(self, target=None, daemon=None, name=None):
            self._target = target

        def start(self):
            captured['spawned'] = True
            self._target()   # run synchronously for deterministic assertions

    monkeypatch.setattr(at.threading, 'Thread', _SyncThread)
    return captured


def test_id_anchored_target_when_index_shifted(monkeypatch):
    """task._assistantMsgId points at message B; caller passes the index of A
    (a concurrent insert shifted rows). The worker MUST translate B (by id), at
    B's resolved index — never A."""
    messages = [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': 'older english reply', '_msgId': 'mA'},
        {'role': 'assistant', 'content': 'the english reply to translate', '_msgId': 'mB'},
    ]
    db = _FakeDB(messages, {'autoTranslate': True})
    task = {'id': 't1', 'convId': 'c1', 'config': {'autoTranslate': True},
            '_assistantMsgId': 'mB'}
    captured = _spy_spawn(monkeypatch)
    monkeypatch.setattr('lib.translate.finalize_incremental', lambda *a, **k: False)
    # Caller passes idx=1 (== A), but the task id says mB (idx 2).
    at._maybe_auto_translate_assistant('c1', 'the english reply to translate',
                                       1, db=db, task=task)
    assert captured.get('spawned'), 'must spawn a translate worker'
    assert captured['msg_id'] == 'mB', 'must target by stable id, not the passed index'
    assert captured['msg_idx'] == 2, 'must use the id-resolved index (B at 2), not 1 (A)'


def test_pre_spawn_dedup_second_call_stands_down(monkeypatch):
    """When the (conv,msgId) slot is already claimed, the safety net must NOT
    spawn a second translate worker."""
    messages = [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': 'english reply', '_msgId': 'mX'},
    ]
    db = _FakeDB(messages, {'autoTranslate': True})
    task = {'id': 't1', 'convId': 'c1', 'config': {'autoTranslate': True},
            '_assistantMsgId': 'mX'}
    # Simulate a translation ALREADY in flight for this exact message.
    assert ifl.claim_inflight('c1', 'mX', 1)
    captured = _spy_spawn(monkeypatch)
    monkeypatch.setattr('lib.translate.finalize_incremental', lambda *a, **k: False)
    at._maybe_auto_translate_assistant('c1', 'english reply', 1, db=db, task=task)
    assert not captured.get('spawned'), \
        'must stand down — translation already in-flight for mX'


def test_spawn_failure_releases_guard(monkeypatch):
    """If claim succeeds but the translate thread fails to start, ownership
    never transferred to a worker — the outer finally MUST release the guard so
    the message isn't wedged in-flight forever (the latent-leak guardrail)."""
    messages = [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': 'english reply', '_msgId': 'mF'},
    ]
    db = _FakeDB(messages, {'autoTranslate': True})
    task = {'id': 't1', 'convId': 'c1', 'config': {'autoTranslate': True},
            '_assistantMsgId': 'mF'}
    monkeypatch.setattr('lib.translate.finalize_incremental', lambda *a, **k: False)

    class _BoomThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError('thread pool exhausted')

    monkeypatch.setattr(at.threading, 'Thread', _BoomThread)
    # Must not raise out (the outer except swallows it).
    at._maybe_auto_translate_assistant('c1', 'english reply', 1, db=db, task=task)
    assert not ifl.is_inflight('c1', 'mF', 1), \
        'spawn failure must release the in-flight guard (no permanent wedge)'


def test_spawn_path_releases_guard_after_worker_settles(monkeypatch):
    """The spawned worker owns + releases the guard; after it settles the slot
    is free for a future re-translate."""
    messages = [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': 'english reply', '_msgId': 'mW'},
    ]
    db = _FakeDB(messages, {'autoTranslate': True})
    task = {'id': 't1', 'convId': 'c1', 'config': {'autoTranslate': True},
            '_assistantMsgId': 'mW'}
    captured = _spy_spawn(monkeypatch)   # _SyncThread runs the closure (incl. its finally)
    monkeypatch.setattr('lib.translate.finalize_incremental', lambda *a, **k: False)
    at._maybe_auto_translate_assistant('c1', 'english reply', 1, db=db, task=task)
    assert captured.get('spawned')
    assert not ifl.is_inflight('c1', 'mW', 1), \
        'worker finally must release the guard after settling'


def test_incremental_handoff_keeps_guard_until_finalize(monkeypatch):
    """When finalize_incremental takes over, the safety net must NOT release the
    guard in its own finally — the incremental worker owns it (and releases on
    its own finalize). So right after the call returns the slot is still held."""
    messages = [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': 'english reply', '_msgId': 'mI'},
    ]
    db = _FakeDB(messages, {'autoTranslate': True})
    task = {'id': 't1', 'convId': 'c1', 'config': {'autoTranslate': True},
            '_assistantMsgId': 'mI'}
    captured = _spy_spawn(monkeypatch)
    # finalize_incremental TAKES ownership (returns True) → no spawn.
    monkeypatch.setattr('lib.translate.finalize_incremental', lambda *a, **k: True)
    at._maybe_auto_translate_assistant('c1', 'english reply', 1, db=db, task=task)
    assert not captured.get('spawned'), 'incremental owns it → no whole-message spawn'
    assert ifl.is_inflight('c1', 'mI', 1), \
        'guard stays held — incremental worker releases it on finalize'
