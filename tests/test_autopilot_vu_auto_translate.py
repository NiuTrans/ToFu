"""Autopilot VU turns must hit the server-side auto-translate safety net.

Root cause this guards (verified against conv mre58lxth33ncr): the virtual-user
(VU) turn is persisted by ``autopilot._append_vu_message_to_conv`` on a code
path SEPARATE from ``manager._sync_result_to_conversation`` (which owns the
assistant/critic safety net). Before the fix, ``autopilot.py`` had ZERO
``_maybe_auto_translate_*`` calls, so every VU turn in an autopilot run was left
untranslated unless a viewer happened to fire a manual translate — the reported
"this conversation never triggers auto-translate" bug.

The fix adds ``_maybe_auto_translate_vu`` and calls it at the VU-append success
site in ``maybe_run_autopilot``. This test drives a real VU append then the
translate hook and asserts the safety net is enqueued for the VU row at the
CORRECT index (resolved from the persisted ``_msgId``, not guessed), with NO
``task`` handed in (the parent task's ``_assistantMsgId`` / incremental
accumulator belong to the assistant turn, not the VU content).

The neuter uses the in-memory, read-only ``neutered_source`` harness (never
writes the shipped file), so it cannot poison the tree — no ``_NC_GUARDED_SOURCES``
entry is required.
"""

import json
import os

import pytest

import lib.tasks_pkg.autopilot as ap
from tests._nc_harness import neutered_source

pytestmark = pytest.mark.unit

_AUTOPILOT_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'lib', 'tasks_pkg', 'autopilot.py',
)


class _FakeDB:
    """Stateful stand-in for the thread-local chat DB.

    Handles the two statements the production path issues in sequence:
      • ``_append_vu_message_to_conv``: ``SELECT messages`` then an
        ``UPDATE`` (applied via the monkeypatched ``db_execute_with_retry``).
      • ``_maybe_auto_translate_vu``: ``SELECT messages`` to resolve the row.
    """

    def __init__(self, messages=None):
        self.messages = list(messages or [])

    def execute(self, sql, params=()):
        self._sql = sql
        self._params = params
        return self

    def fetchone(self):
        if 'SELECT messages' in self._sql:
            return (json.dumps(self.messages),)
        return None


def _install(monkeypatch, db, spy_calls):
    """Point autopilot's lazy DB + translate imports at our fakes."""
    monkeypatch.setattr('lib.database.get_thread_db', lambda *_a, **_k: db)

    def _fake_update(_db, _sql, params):
        # The append's UPDATE carries the full messages JSON as params[0].
        if 'UPDATE conversations' in _sql and params:
            db.messages = json.loads(params[0])

    monkeypatch.setattr('lib.database.db_execute_with_retry', _fake_update)
    # search-text builder is irrelevant to the assertion; keep it cheap + safe.
    monkeypatch.setattr('lib.conversations.build_search_text', lambda _m: '')

    def _spy(conv_id, content, msg_idx, db=None, task=None, **kw):
        spy_calls.append({
            'conv_id': conv_id, 'content': content, 'msg_idx': msg_idx,
            'db_is_fake': db is fake_db_ref[0], 'task': task,
        })

    fake_db_ref = [db]
    monkeypatch.setattr(
        'lib.tasks_pkg.auto_translate._maybe_auto_translate_assistant', _spy)


def test_vu_append_enqueues_translate_at_resolved_index(monkeypatch):
    """A real VU append followed by the translate hook enqueues the safety net
    for the VU row at its ACTUAL persisted index (not 0, not guessed), passing
    the live db and NO task."""
    # Two pre-existing turns → the appended VU row lands at idx 2.
    db = _FakeDB([
        {'role': 'user', 'content': 'hi', '_msgId': 'u1'},
        {'role': 'assistant', 'content': 'hello', '_msgId': 'a1'},
    ])
    calls = []
    _install(monkeypatch, db, calls)

    vu_id = 'vu-xyz'
    vu_text = 'Please continue and verify the fix works end to end.'
    # Drive the REAL append (writes into the fake DB via the patched UPDATE).
    vu_msg = ap._append_vu_message_to_conv('conv-1', vu_id, vu_text, run_id='run-1')
    assert vu_msg is not None
    assert db.messages[-1]['_msgId'] == vu_id  # append landed

    # The production call site: translate the just-appended VU turn.
    ap._maybe_auto_translate_vu('conv-1', vu_id, vu_text)

    assert len(calls) == 1, 'safety net must be enqueued exactly once for the VU turn'
    c = calls[0]
    assert c['conv_id'] == 'conv-1'
    assert c['content'] == vu_text
    assert c['msg_idx'] == 2, 'index must be resolved from persisted _msgId, not guessed'
    assert c['db_is_fake'] is True, 'the live db handle must be threaded through'
    assert c['task'] is None, 'must NOT pass the parent task (wrong accumulator/anchor)'


def test_missing_vu_row_does_not_fire(monkeypatch):
    """If the _msgId is absent from the persisted messages, no translate fires
    (defends against firing against a stale/positional guess)."""
    db = _FakeDB([{'role': 'user', 'content': 'hi', '_msgId': 'u1'}])
    calls = []
    _install(monkeypatch, db, calls)

    ap._maybe_auto_translate_vu('conv-1', 'not-present', 'some text')
    assert calls == [], 'no matching _msgId → no translate enqueued'


def test_call_site_wired_after_vu_append(monkeypatch):
    """maybe_run_autopilot must call _maybe_auto_translate_vu right after the
    VU append succeeds — the wiring, not just the helper, is load-bearing."""
    src = open(_AUTOPILOT_SRC, encoding='utf-8').read()
    i_append = src.index('vu_msg = _append_vu_message_to_conv(')
    i_call = src.index('_maybe_auto_translate_vu(conv_id, vu_msg_id, vu_text)')
    assert i_call > i_append, 'translate call must follow the VU append site'


def test_neuter_index_resolver_makes_it_miss(monkeypatch):
    """NEUTER (in-memory, read-only): break the _msgId → index resolver and the
    translate stops firing for a row that IS present — proving the resolver is
    the load-bearing line, not incidental."""
    db = _FakeDB([{'role': 'user', 'content': 'x', '_msgId': 'vu-xyz'}])
    calls = []
    _install(monkeypatch, db, calls)

    # Sanity: canonical path DOES fire for this row.
    ap._maybe_auto_translate_vu('conv-1', 'vu-xyz', 'text')
    assert len(calls) == 1, 'baseline: resolver finds the row and fires'

    calls.clear()
    # Neuter the equality that matches the VU row by its _msgId.
    with neutered_source(
        _AUTOPILOT_SRC,
        "m.get('_msgId') == vu_msg_id",
        "m.get('_msgId') == '__nc_never_matches__'",
    ) as mod:
        mod._maybe_auto_translate_vu('conv-1', 'vu-xyz', 'text')

    assert calls == [], 'NEUTER: broken resolver must make the translate MISS (bite)'
