"""Guard: the ``conversations.messages`` blob must never be overwritten blindly.

WHY
---
``conversations.messages`` is a single JSON blob holding the whole transcript,
so every writer does read-modify-write. The row carries a ``rev`` column bumped
by a DB trigger on every genuine messages change, which lets a writer make its
UPDATE conditional and lose a race safely. A writer that omits that predicate
overwrites whatever another thread appended between its read and its write —
silently, with no exception and no failing test.

Measured incident (conv ms3sfyrmn31omb, 2026-07-28): ``logs/app.log`` holds 13
``Appended VU msg`` lines while the row holds 8 ``_isVirtualUser`` rows. The
per-round snapshot daemon (``_patch_assistant_message_with_git``) kept writing
back a copy of the transcript it had read BEFORE the autopilot append landed,
erasing five completed virtual-user turns. The follow-up task then rebuilt its
context from a transcript whose last message was the assistant's own previous
answer — which is why the model kept reporting that it was being handed back
its own reply as the new instruction.

WHAT IS ASSERTED (results, not implementation)
----------------------------------------------
1. Behaviour — a concurrent append survives a snapshot write-back, and it
   survives because the patch REPLAYS rather than because it happened to run
   first. Driven through the real production functions.
2. Complement — a legitimate whole-transcript rebuild still lands, so "write
   nothing at all" cannot pass this file.
3. Ratchet — no NEW unguarded whole-blob writer may appear. Scan surface is
   enumerated by ``tests/_scan_conv_messages_writers.py`` (shared with the
   human-readable dump, so the two can never disagree about the input set).
4. Anti-regression — the positional "last assistant" fallback must stay gone;
   under concurrency it stamps one task's snapshot onto another task's turn.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._scan_conv_messages_writers import (  # noqa: E402
    scan_writers,
    unguarded_writers,
)

pytestmark = pytest.mark.unit


# ══════════════════════════════════════════════════════════════════════
#  A fake conversations row with REAL rev-bump semantics
# ══════════════════════════════════════════════════════════════════════

class _FakeRow(dict):
    """dict with ``.keys()`` — matches the DictRow/sqlite3.Row access the store uses."""


class _FakeConvDb:
    """Minimal ``conversations`` row honouring the production rev contract.

    Mirrors ``conversations_rev_bump_trg``: ``rev`` advances whenever an UPDATE
    genuinely changes ``messages``. Statements carrying ``AND rev=?`` are applied
    only when the supplied rev still matches, and report ``rowcount`` accordingly
    — which is exactly the signal the store's CAS depends on.
    """

    def __init__(self, conv_id='c1', messages='[]'):
        self.conv_id = conv_id
        self.messages = messages
        self.rev = 0
        self.updated_at = 0
        self.lock = threading.Lock()
        self.on_read = None      # test hook: fires after a messages SELECT
        self.blind_writes = 0    # UPDATEs with no rev/updated_at predicate

    # -- DB API surface the store touches -----------------------------
    def execute(self, sql, params=()):
        low = ' '.join(sql.split()).lower()
        if low.startswith('select rev'):
            return _FakeCursor(rows=[_FakeRow(rev=self.rev)])
        if low.startswith('select messages'):
            row = _FakeRow(messages=self.messages, updated_at=self.updated_at,
                           rev=self.rev)
            cur = _FakeCursor(rows=[row])
            if self.on_read:
                hook, self.on_read = self.on_read, None
                hook()
            return cur
        if low.startswith('update conversations'):
            return self._apply_update(low, params)
        return _FakeCursor(rows=[])

    def _apply_update(self, low, params):
        with self.lock:
            if 'and rev=?' in low:
                expected = params[-1]
                if int(expected) != self.rev:
                    return _FakeCursor(rowcount=0)
            else:
                self.blind_writes += 1
            new_messages = params[0]
            if new_messages != self.messages:
                self.messages = new_messages
                self.rev += 1
            self.updated_at = params[1] if len(params) > 1 else self.updated_at
            return _FakeCursor(rowcount=1)

    def commit(self):
        pass

    def begin(self):
        pass

    def rollback(self):
        pass


class _FakeCursor:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


@pytest.fixture()
def store_env(monkeypatch):
    """Real ``DefaultConversationStore`` wired to the fake row."""
    db = _FakeConvDb()
    import lib.database as _db_mod
    import lib.database.messages_rows as _rows_mod
    monkeypatch.setattr(_db_mod, 'get_thread_db', lambda *a, **k: db, raising=False)
    monkeypatch.setattr(_db_mod, 'db_execute_with_retry',
                        lambda conn, sql, params=(): conn.execute(sql, params),
                        raising=False)
    monkeypatch.setattr(_db_mod, 'json_dumps_pg',
                        lambda obj, **k: __import__('json').dumps(obj, ensure_ascii=False),
                        raising=False)
    monkeypatch.setattr(_rows_mod, 'mirror_write_and_commit',
                        lambda *a, **k: None, raising=False)

    ps = importlib.import_module('lib.tasks_pkg.persistence_store')
    return ps.DefaultConversationStore(), db, ps


def _msgs(db):
    import json
    return json.loads(db.messages)


def _seed(db, messages):
    import json
    db.messages = json.dumps(messages, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════
#  1. BEHAVIOUR — the reported data loss cannot happen
# ══════════════════════════════════════════════════════════════════════

def test_concurrent_append_survives_the_snapshot_write_back(store_env, monkeypatch):
    """Reproduces the incident shape end-to-end and asserts the row survives.

    Timeline (measured at 09:05:36 on conv ms3sfyrmn31omb):
      1. snapshot daemon reads the transcript (assistant turn, no VU row yet)
      2. autopilot appends the VU reply  → row grows
      3. snapshot daemon writes its stamped copy back

    Step 3 must not delete what step 2 added.
    """
    store, db, _ps = store_env
    _seed(db, [
        {'role': 'user', 'content': 'do the thing'},
        {'role': 'assistant', 'content': 'done', '_taskId': 'task-A'},
    ])

    def _autopilot_appends_mid_flight():
        rows = _msgs(db)
        rows.append({'role': 'user', 'content': 'VU reply', '_isVirtualUser': True})
        _seed(db, rows)
        db.rev += 1

    db.on_read = _autopilot_appends_mid_flight

    import lib.tasks_pkg.commit_round._commit as commit_mod
    monkeypatch.setattr(commit_mod, 'get_conversation_store', lambda: store,
                        raising=False)
    import lib.agent_core.store as _store_mod
    monkeypatch.setattr(_store_mod, 'get_conversation_store', lambda: store,
                        raising=False)

    commit_mod._patch_assistant_message_with_git(
        {'convId': 'c1', 'id': 'task-A'},
        {'gitSha': 'deadbeef1234', 'snapshotId': 'deadbeef1234'})

    final = _msgs(db)
    vu = [m for m in final if m.get('_isVirtualUser')]
    assert len(vu) == 1, (
        f'the concurrently-appended virtual-user turn was erased: {final}')
    assert vu[0]['content'] == 'VU reply'


def test_the_snapshot_stamp_still_lands_on_its_own_turn(store_env, monkeypatch):
    """Complement: surviving the race must not mean skipping the write.

    Without this, "never write anything" would satisfy the test above.
    """
    store, db, _ps = store_env
    _seed(db, [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': 'a', '_taskId': 'task-A'},
    ])

    def _append():
        rows = _msgs(db)
        rows.append({'role': 'user', 'content': 'VU', '_isVirtualUser': True})
        _seed(db, rows)
        db.rev += 1

    db.on_read = _append

    import lib.tasks_pkg.commit_round._commit as commit_mod
    monkeypatch.setattr(commit_mod, 'get_conversation_store', lambda: store,
                        raising=False)
    import lib.agent_core.store as _store_mod
    monkeypatch.setattr(_store_mod, 'get_conversation_store', lambda: store,
                        raising=False)

    commit_mod._patch_assistant_message_with_git(
        {'convId': 'c1', 'id': 'task-A'},
        {'gitSha': 'cafebabe9999', 'snapshotId': 'cafebabe9999'})

    final = _msgs(db)
    owned = [m for m in final if m.get('_taskId') == 'task-A']
    assert owned and owned[0].get('_gitSha') == 'cafebabe9999', (
        f'the snapshot id never reached its own assistant turn: {final}')


def test_a_whole_transcript_save_refuses_to_clobber_a_concurrent_append(store_env):
    """The blunt primitive must fail loudly rather than erase rows."""
    store, db, ps = store_env
    _seed(db, [{'role': 'user', 'content': 'q'}])
    stale = _msgs(db)          # a copy read before the concurrent append
    stale_rev = db.rev         # ...and the rev it was read at

    rows = _msgs(db)
    rows.append({'role': 'user', 'content': 'VU', '_isVirtualUser': True})
    _seed(db, rows)
    db.rev += 1                # the append bumps rev, as the trigger would

    with pytest.raises(ps.ConcurrentWriteConflict):
        store.save_conversation_messages('c1', stale, expected_rev=stale_rev)

    assert any(m.get('_isVirtualUser') for m in _msgs(db)), \
        'the stale whole-blob save destroyed the concurrently appended row'


def test_a_legitimate_whole_transcript_rebuild_still_lands(store_env):
    """Complement to the guard above — an uncontended save must succeed.

    Prevents "make every save raise" from passing this file.
    """
    store, db, _ps = store_env
    _seed(db, [{'role': 'user', 'content': 'q'}])
    rebuilt = _msgs(db) + [{'role': 'assistant', 'content': 'a'}]

    assert store.save_conversation_messages(
        'c1', rebuilt, expected_rev=db.rev) > 0
    assert len(_msgs(db)) == 2



@pytest.fixture()
def vu_env(monkeypatch):
    """Real ``_append_vu_message_to_conv`` wired to the fake row.

    The VU append reaches the DB directly rather than through the store, so it
    needs its own wiring — patched on ``lib.database`` (the module the function
    imports from at call time) so the REAL production body runs.
    """
    db = _FakeConvDb()
    import lib.database as _db_mod
    import lib.database.messages_rows as _rows_mod
    import lib.conversations as _conv_mod
    monkeypatch.setattr(_db_mod, 'get_thread_db', lambda *a, **k: db, raising=False)
    monkeypatch.setattr(_db_mod, 'json_dumps_pg',
                        lambda obj, **k: __import__('json').dumps(obj, ensure_ascii=False),
                        raising=False)
    monkeypatch.setattr(_rows_mod, 'mirror_write_and_commit',
                        lambda *a, **k: None, raising=False)
    monkeypatch.setattr(_conv_mod, 'build_search_text', lambda m: '', raising=False)
    baton = importlib.import_module('lib.tasks_pkg.autopilot_baton')
    return baton, db


def test_the_vu_append_does_not_erase_a_concurrent_writer(vu_env):
    """The VU append is an AGGRESSOR too, not only a victim.

    In the measured incident the snapshot daemon won and the VU row died. The
    mirror image is just as real: this function used to issue an unconditional
    UPDATE, so an assistant sync or translation commit landing after its SELECT
    was erased the same way. Fixing only the reader leaves the identical hole
    open at the hottest concurrency point in the system.
    """
    baton, db = vu_env
    _seed(db, [{'role': 'user', 'content': 'q'}])

    state = {'fired': False}
    original_execute = db.execute

    def _assistant_lands_mid_flight(sql, params=()):
        cur = original_execute(sql, params)
        low = ' '.join(sql.split()).lower()
        if low.startswith('select messages') and not state['fired']:
            state['fired'] = True
            rows = _msgs(db)
            rows.append({'role': 'assistant', 'content': 'concurrent reply',
                         '_taskId': 'task-OTHER'})
            _seed(db, rows)
            db.rev += 1
        return cur

    db.execute = _assistant_lands_mid_flight

    baton._append_vu_message_to_conv('c1', 'vu-1', 'VU reply', run_id='run-1')

    final = _msgs(db)
    assert any(m.get('_taskId') == 'task-OTHER' for m in final), (
        f'the VU append erased a concurrently-committed assistant turn: {final}')


def test_the_vu_append_still_lands_when_uncontended(vu_env):
    """Complement: surviving the race must not mean never writing.

    Without this, making the append always bail would satisfy the guard above.
    """
    baton, db = vu_env
    _seed(db, [{'role': 'user', 'content': 'q'}])

    result = baton._append_vu_message_to_conv('c1', 'vu-1', 'VU reply',
                                              run_id='run-1')
    assert result is not None, 'the uncontended VU append reported failure'
    vu_rows = [m for m in _msgs(db) if m.get('_isVirtualUser')]
    assert len(vu_rows) == 1 and vu_rows[0]['content'] == 'VU reply'


def test_the_vu_append_stands_down_behind_a_real_human_turn(vu_env):
    """A human spoke mid-flight → do not append behind them; report it.

    Appending after a real human turn puts machine-authored text into the
    history the model reads back as words the person said. The caller reads the
    ``None`` as "stand down and preserve", so the reply still reaches the user
    through the sidecar — this asserts the row is NOT written, not that the
    text is discarded.
    """
    baton, db = vu_env
    _seed(db, [{'role': 'user', 'content': 'q'}])

    state = {'fired': False}
    original_execute = db.execute

    def _human_speaks_mid_flight(sql, params=()):
        low = ' '.join(sql.split()).lower()
        if low.startswith('select messages') and not state['fired']:
            state['fired'] = True
            rows = _msgs(db)
            rows.append({'role': 'user', 'content': 'wait, stop',
                         'timestamp': 2 ** 62})
            _seed(db, rows)
            db.rev += 1
        return original_execute(sql, params)

    db.execute = _human_speaks_mid_flight

    result = baton._append_vu_message_to_conv('c1', 'vu-1', 'VU reply',
                                              run_id='run-1')

    assert result is None, (
        'the VU append reported success despite standing down for a human')
    assert not any(m.get('_isVirtualUser') for m in _msgs(db)), (
        'a virtual-user row was appended behind a real human turn — the next '
        'turn would feed it back to the model as something the human said')


def test_the_sanctioned_unconditional_overwrite_is_reachable(store_env):
    """Boot-time recovery is a real sole-writer case and must stay expressible."""
    store, db, _ps = store_env
    _seed(db, [{'role': 'user', 'content': 'q'}])
    store.overwrite_conversation_messages_unconditional(
        'c1', [{'role': 'user', 'content': 'rebuilt'}])
    assert _msgs(db) == [{'role': 'user', 'content': 'rebuilt'}]
    assert db.blind_writes == 1


# ══════════════════════════════════════════════════════════════════════
#  2. ANTI-REGRESSION — no positional fallback
# ══════════════════════════════════════════════════════════════════════

def test_a_snapshot_never_lands_on_another_tasks_turn(store_env, monkeypatch):
    """No ``_taskId`` match → write nothing; never guess the last assistant.

    The old fallback ("if nothing carries my task id, take the last assistant
    message") is the same clobber bug wearing a quieter mask: under concurrency
    the last assistant message belongs to a DIFFERENT task by the time this
    daemon runs, so the snapshot id gets stamped onto someone else's turn.
    """
    store, db, _ps = store_env
    _seed(db, [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': 'other turn', '_taskId': 'task-OTHER'},
    ])

    import lib.tasks_pkg.commit_round._commit as commit_mod
    monkeypatch.setattr(commit_mod, 'get_conversation_store', lambda: store,
                        raising=False)
    import lib.agent_core.store as _store_mod
    monkeypatch.setattr(_store_mod, 'get_conversation_store', lambda: store,
                        raising=False)

    commit_mod._patch_assistant_message_with_git(
        {'convId': 'c1', 'id': 'task-MINE'},
        {'gitSha': 'aaaabbbbcccc', 'snapshotId': 'aaaabbbbcccc'})

    victim = _msgs(db)[1]
    assert '_gitSha' not in victim, (
        "stamped this task's snapshot onto another task's assistant turn: "
        f'{victim}')


# ══════════════════════════════════════════════════════════════════════
#  3. RATCHET — the scan surface, then the assertion
# ══════════════════════════════════════════════════════════════════════

def test_the_scan_actually_finds_the_known_whole_blob_writers():
    """Pin the scan SURFACE before trusting any ratchet built on it.

    A scanning guard fails at its INPUT SET, and an input set does not report
    its own gaps — a shrunken scan looks identical to a clean repo. So assert
    the scan still sees writers it is known to cover, on both the CAS and the
    formerly-unguarded side.
    """
    writers = scan_writers()
    seen = {(w['path'], w['func']) for w in writers}

    expected_maintenance = {
        ('lib/database/message_archive_offload.py',
         'offload_frozen_message_archives'),
    }
    assert seen == expected_maintenance, (
        'only the reviewed frozen-archive maintenance seam may write the '
        'retired transcript blob directly; business writes must use '
        f'conversation_repository: {sorted(seen)}')


def test_the_conversation_store_primitive_is_cas_guarded():
    """The store's plain save must carry a rev predicate.

    Asserted on the RESULT (the SQL the store actually issues is rev-guarded),
    not on any constant, so rewriting the implementation keeps this honest.
    """
    offenders = [
        w for w in unguarded_writers()
        if w['path'] == 'lib/tasks_pkg/persistence_store.py'
    ]
    assert not offenders, (
        'persistence_store still has an unguarded whole-blob write: '
        + '; '.join(f'{o["func"]}() line {o["line"]}' for o in offenders))


def test_no_new_unguarded_whole_blob_writer_appears():
    """Ratchet: the unguarded set may shrink, never grow.

    Each remaining entry is a pre-existing writer outside this epic's write-set.
    Removing one means deleting its line here; adding one fails the build.
    """
    known = set()
    current = {(w['path'], w['func']) for w in unguarded_writers()}
    new = current - known
    assert not new, (
        'NEW unguarded whole-blob write(s) to conversations.messages:\n  '
        + '\n  '.join(f'{p}::{f}()' for p, f in sorted(new))
        + '\nUse a rev-CAS predicate, or the store\'s '
          'patch_message_fields_by_task / '
          'overwrite_conversation_messages_unconditional.')
