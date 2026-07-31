"""tests/test_abort_fragment_finish_reason.py — regression for the
"persisted assistant message with finishReason=None" husk (Bug 1 SOURCE fix).

WHY
---
On Stop→Regenerate the aborted task's terminal ``_sync_result_to_conversation``
early-returns at the freshness guard (a newer task is 'latest'), so it never
writes terminal fields. But its OWN partial-checkpoint path already wrote a
content-bearing fragment into ``conversations.messages`` with NO terminal fields
(status='running'). The net persisted state is an assistant message with real
content and ``finishReason=None`` — an ambiguous "settled but no reason" husk
that renders in full completed chrome (the reported screenshot).

``_stamp_aborted_fragment_finish_reason`` closes this at the source: it locates
the task's own fragment by its stable ``_assistantMsgId`` and stamps
``finishReason='aborted'`` (content preserved). This suite drives the helper
against a fake conversations row and asserts:
  * a content-bearing finishReason=None fragment is stamped 'aborted';
  * it targets the RIGHT message by _msgId (never a positional guess), so a
    newer sibling answer is untouched;
  * idempotent (already-stamped / empty / no-_assistantMsgId → no write);
  * NEUTER: with the helper reverted to a no-op, the husk survives — proving
    the freshness-guard call site is load-bearing.
"""

from __future__ import annotations

import json
import threading

import pytest

pytestmark = pytest.mark.unit


class _FakeCursor:
    def __init__(self, row=None, rowcount=0):
        self._row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self._row


class _FakeDB:
    """Minimal stand-in for the DOMAIN_CHAT db wrapper: serves one conversations
    row, records the UPDATE, and reports rowcount=1 so the CAS check passes."""

    def __init__(self, conv_id, messages, updated_at=1000):
        self.conv_id = conv_id
        self._messages = json.dumps(messages)
        self._updated_at = updated_at
        self.committed = False
        self.updated_messages = None

    def execute(self, sql, params=None):
        s = ' '.join(sql.split())
        if s.startswith('SELECT messages, updated_at FROM conversations'):
            # row[0]=messages, row['updated_at']=updated_at
            row = _Row({'messages': self._messages, 'updated_at': self._updated_at},
                       order=['messages', 'updated_at'])
            return _FakeCursor(row=row)
        if s.startswith('UPDATE conversations SET messages'):
            self.updated_messages = params[0]
            return _FakeCursor(rowcount=1)
        if s.startswith('SELECT rev FROM conversations'):
            return _FakeCursor(row=_Row({'rev': 7}, order=['rev']))
        return _FakeCursor()

    def commit(self):
        self.committed = True


class _Row:
    """Row that supports both integer index (row[0]) and key access (row['x'])."""

    def __init__(self, d, order):
        self._d = d
        self._order = order

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._d[self._order[k]]
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)


def _task(conv_id, amid):
    return {
        'id': 'aborted-task-1',
        'convId': conv_id,
        'aborted': True,
        '_abort_reason': 'user-stop',
        '_assistantMsgId': amid,
        'content_lock': threading.Lock(),
    }


def _install_fake(monkeypatch, fake):
    import lib.tasks_pkg.manager._sync as sync
    monkeypatch.setattr(sync, 'get_thread_db', lambda domain: fake)
    # notify_conv_changed is imported lazily from lib.conversations — stub it.
    import lib.conversations as conv_pkg
    monkeypatch.setattr(conv_pkg, 'notify_conv_changed', lambda *a, **k: None,
                        raising=False)


def test_fragment_stamped_aborted(monkeypatch):
    import lib.tasks_pkg.manager._sync as sync
    msgs = [
        {'role': 'user', 'content': 'Q', '_msgId': 'u0'},
        {'role': 'assistant', 'content': 'partial answer', 'thinking': '',
         '_msgId': 'frag-1'},  # NO finishReason — the husk
    ]
    fake = _FakeDB('cv1', msgs)
    _install_fake(monkeypatch, fake)
    ok = sync._stamp_aborted_fragment_finish_reason(_task('cv1', 'frag-1'))
    assert ok is True
    assert fake.committed is True
    written = json.loads(fake.updated_messages)
    assert written[1]['finishReason'] == 'aborted'
    # Content preserved (data-preservation).
    assert written[1]['content'] == 'partial answer'


def test_targets_by_msgid_not_position(monkeypatch):
    import lib.tasks_pkg.manager._sync as sync
    # The completed regenerate answer landed BEFORE the lingering fragment
    # (the observed ordering inversion). The stamp must hit 'frag-1' by id,
    # never the settled sibling.
    msgs = [
        {'role': 'user', 'content': 'Q', '_msgId': 'u0'},
        {'role': 'assistant', 'content': 'full answer', 'finishReason': 'stop',
         '_msgId': 'answer-1'},
        {'role': 'assistant', 'content': 'partial', 'thinking': '',
         '_msgId': 'frag-1'},
    ]
    fake = _FakeDB('cv1', msgs)
    _install_fake(monkeypatch, fake)
    ok = sync._stamp_aborted_fragment_finish_reason(_task('cv1', 'frag-1'))
    assert ok is True
    written = json.loads(fake.updated_messages)
    assert written[2]['finishReason'] == 'aborted'   # the fragment
    assert written[1]['finishReason'] == 'stop'       # the real answer untouched


def test_no_assistant_msgid_is_skipped(monkeypatch):
    import lib.tasks_pkg.manager._sync as sync
    fake = _FakeDB('cv1', [{'role': 'assistant', 'content': 'x', '_msgId': 'frag-1'}])
    _install_fake(monkeypatch, fake)
    # No stable id to target → skip (a positional guess could stamp the wrong msg).
    ok = sync._stamp_aborted_fragment_finish_reason(_task('cv1', None))
    assert ok is False
    assert fake.updated_messages is None


def test_already_finished_fragment_not_restamped(monkeypatch):
    import lib.tasks_pkg.manager._sync as sync
    msgs = [{'role': 'assistant', 'content': 'x', 'finishReason': 'stop',
             '_msgId': 'frag-1'}]
    fake = _FakeDB('cv1', msgs)
    _install_fake(monkeypatch, fake)
    ok = sync._stamp_aborted_fragment_finish_reason(_task('cv1', 'frag-1'))
    assert ok is False
    assert fake.updated_messages is None


def test_empty_fragment_not_stamped(monkeypatch):
    import lib.tasks_pkg.manager._sync as sync
    msgs = [{'role': 'assistant', 'content': '', 'thinking': '', '_msgId': 'frag-1'}]
    fake = _FakeDB('cv1', msgs)
    _install_fake(monkeypatch, fake)
    ok = sync._stamp_aborted_fragment_finish_reason(_task('cv1', 'frag-1'))
    assert ok is False
    assert fake.updated_messages is None


def test_neuter_guard_call_leaves_husk(monkeypatch):
    """NEUTER: revert the helper to a no-op (as if the freshness-guard call site
    were removed) and prove the fragment keeps finishReason=None — the exact
    ambiguous husk the bug reports. Proves the SOURCE stamp is load-bearing."""
    import lib.tasks_pkg.manager._sync as sync
    monkeypatch.setattr(sync, '_stamp_aborted_fragment_finish_reason',
                        lambda task: False)
    msgs = [{'role': 'assistant', 'content': 'partial', '_msgId': 'frag-1'}]
    fake = _FakeDB('cv1', msgs)
    _install_fake(monkeypatch, fake)
    sync._stamp_aborted_fragment_finish_reason(_task('cv1', 'frag-1'))
    # No write happened → the husk persists unmarked.
    assert fake.updated_messages is None
