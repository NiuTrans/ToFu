#!/usr/bin/env python3
"""RENDER_CONTRACT Phase 4 migration tests — CAS on ``rev`` (not ``updated_at``).

These are the invariants the owner gated the Phase-4 work on.

  * TEST 1 (token proof) — proves at the DB level that ``rev`` is a strictly
    better CAS token than ``updated_at``; the trigger is already shipped. GREEN.
  * TEST 2 (regraft preservation) — was written TESTS-FIRST (RED on HEAD). It is
    now GREEN after landing RENDER_CONTRACT Phase 4 §2.2: the terminal regraft
    became a field-level OWNED-WHITELIST merge (``_merge_terminal_fields``) that
    preserves translate-owned fields (``translatedContent`` +
    ``segments[].translatedText``) instead of a whole-dict replace. It asserts
    BOTH the top-level translatedContent AND a per-segment translatedText
    survive a regraft.
  * TEST 3 (merge NEUTER) — reverts the merge to the old whole-dict replace and
    proves BOTH fields are re-dropped → confirms the merge is load-bearing.

────────────────────────────────────────────────────────────────────────────
TEST 1 — rev distinguishes two writers that read the SAME updated_at
────────────────────────────────────────────────────────────────────────────
The whole point of Phase 4. ``updated_at`` is a wall-clock millisecond stamp:
two writers that both read a row at millisecond T, and both stamp their write
with T, BOTH satisfy ``WHERE updated_at=T`` — so the second silently clobbers
the first (the L3 "two same-ms writers both pass CAS" bug). ``rev`` is a
trigger-bumped monotonic integer: after writer B commits, rev has advanced, so
writer A's ``WHERE rev=<rev A read>`` MISSES and A must re-read instead of
clobbering. This test drives the two CAS shapes directly against a real row and
asserts the token choice is what decides the outcome.

  NEUTER (in-test): the SAME race resolved with an ``updated_at`` CAS lets A
  clobber B → proves ``rev`` (not some incidental ordering) is load-bearing.

────────────────────────────────────────────────────────────────────────────
TEST 2 — terminal regraft must PRESERVE a concurrent translation  (RED on HEAD)
────────────────────────────────────────────────────────────────────────────
The owner's explicit worry: once terminal-sync AND auto-translate BOTH CAS on
``rev``, they truly contend on one monotonic token, so the terminal write's
CAS-miss → re-read → regraft path (``_sync.py`` ~985) gets hit FAR more often.
Today that path does ``_fresh_messages[-1] = last_msg`` — a blind REPLACE of the
fresh tail with the backend's assembled assistant dict, which carries NO
``translatedContent``. So a translation committed onto the tail between the
terminal sync's SELECT and its regraft re-read is DROPPED.

This test inserts a translation commit (adds ``translatedContent`` to the tail,
bumps the row) in the read→write window, forcing the regraft, and asserts BOTH
the final answer AND the translation survive. On HEAD the answer survives but
the translation is clobbered → this test FAILS. The Phase-4 fix must make the
regraft a FIELD-LEVEL MERGE (graft our terminal fields onto the fresh tail,
preserving fields the backend never owns — translatedContent, _showingTranslation,
segments[].translatedText, originalContent) instead of a whole-dict replace.

Standalone runner (real DB, mirrors tests/test_terminal_cas_retry.py); also
importable as pytest functions.
"""

import json as _json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules['flask'] = _quart

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _seed_conv(db, conv_id, messages):
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'rev-cas-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms, 'settings': '{}',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings'], retry=True)
    db.commit()


def _row(db, conv_id):
    r = db.execute('SELECT messages, updated_at, rev FROM conversations '
                   'WHERE id=? AND user_id=1', (conv_id,)).fetchone()
    msgs = _json.loads(r[0]) if isinstance(r[0], str) else r[0]
    ua = r[1] if not isinstance(r, dict) else r['updated_at']
    rev = int((r[2] if not isinstance(r, dict) else r['rev']) or 0)
    return msgs, ua, rev


def _read_tail(db, conv_id):
    msgs, _ua, _rev = _row(db, conv_id)
    return msgs


def _cleanup(db, *ids):
    from lib.database import db_execute_with_retry
    for cid in ids:
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
    db.commit()


# ──────────────────────────────────────────────────────────────────────────
# TEST 1 — rev vs updated_at under a same-millisecond two-writer race
# ──────────────────────────────────────────────────────────────────────────

def _cas_write(db, conv_id, new_messages, *, token_col, token_val, forced_updated_at):
    """Read-modify-write guarded on ``token_col`` == ``token_val``.

    ``forced_updated_at`` is written verbatim so two writers can deliberately
    stamp the SAME millisecond (the whole point of the test). Returns True iff
    the CAS UPDATE actually landed (rowcount > 0).
    """
    from lib.database import json_dumps_pg
    cur = db.execute(
        f'UPDATE conversations SET messages=?, updated_at=? '
        f'WHERE id=? AND user_id=1 AND {token_col}=?',
        (json_dumps_pg(new_messages), forced_updated_at, conv_id, token_val))
    db.commit()
    return (getattr(cur, 'rowcount', 0) or 0) > 0


def test_rev_distinguishes_same_ms_writers():
    """Two writers read the same row (same updated_at, same rev). B commits
    first. A then CAS-writes.
      * rev CAS  → A MISSES (rev advanced by B's trigger) → B preserved.  ✅
      * updated_at CAS with a forced same-ms stamp → A PASSES → clobbers B. ✗ (NEUTER)
    """
    from lib.database import DOMAIN_CHAT, get_thread_db

    # ---- rev token: A must miss, B's write survives ----
    conv_id = 'cv-revcas-rev'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1', 'timestamp': 1, '_msgId': 'm0'},
        {'role': 'assistant', 'content': 'base', 'timestamp': 2, '_msgId': 'm1'},
    ])
    try:
        _msgs, ua0, rev0 = _row(db, conv_id)
        SAME_MS = ua0 + 5  # both writers will stamp THIS exact millisecond

        # Writer B commits first (writes "B-WON", stamps SAME_MS). Trigger bumps rev.
        b_msgs = [dict(_msgs[0]), dict(_msgs[1], content='B-WON')]
        b_ok = _cas_write(db, conv_id, b_msgs, token_col='rev', token_val=rev0,
                          forced_updated_at=SAME_MS)
        assert b_ok, 'writer B (rev CAS on rev0) should land'

        # Writer A read rev0 too; now tries to write "A-CLOBBER" guarded on rev0.
        a_msgs = [dict(_msgs[0]), dict(_msgs[1], content='A-CLOBBER')]
        a_ok = _cas_write(db, conv_id, a_msgs, token_col='rev', token_val=rev0,
                          forced_updated_at=SAME_MS)
        assert not a_ok, (
            'rev CAS FAILED to protect: writer A (guarded on stale rev0) was '
            'allowed to write even though B already bumped rev — the token is '
            'not doing its job')
        tail = _read_tail(db, conv_id)[-1]
        assert tail['content'] == 'B-WON', (
            f'rev CAS: B\'s write must survive A\'s stale attempt, got {tail["content"]!r}')
    finally:
        _cleanup(db, conv_id)
    _ok('rev CAS: same-ms writer A (stale rev) MISSES → B preserved (no clobber)')

    # ---- NEUTER: updated_at token with a forced same-ms → A clobbers B ----
    conv_id = 'cv-revcas-ua'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1', 'timestamp': 1, '_msgId': 'm0'},
        {'role': 'assistant', 'content': 'base', 'timestamp': 2, '_msgId': 'm1'},
    ])
    try:
        _msgs, ua0, _rev0 = _row(db, conv_id)
        SAME_MS = ua0 + 5

        b_msgs = [dict(_msgs[0]), dict(_msgs[1], content='B-WON')]
        b_ok = _cas_write(db, conv_id, b_msgs, token_col='updated_at', token_val=ua0,
                          forced_updated_at=SAME_MS)
        assert b_ok, 'writer B (updated_at CAS on ua0) should land'

        # A read ua0; guarded on ua0. But B stamped SAME_MS != ua0, so does A miss?
        # No — A read ua0 BEFORE B wrote, and the classic bug is when A and B
        # read the SAME ua0 and B's stamp equals what A still guards on. Model
        # the true collision: B stamps the SAME value A holds (ua0), which is
        # exactly what happens when two writers fire within one clock tick and
        # both compute now_ms==ua0. Re-seed to force that exact shape:
        db.execute('UPDATE conversations SET updated_at=? WHERE id=? AND user_id=1',
                   (ua0, conv_id))
        db.commit()
        a_msgs = [dict(_msgs[0]), dict(_msgs[1], content='A-CLOBBER')]
        a_ok = _cas_write(db, conv_id, a_msgs, token_col='updated_at', token_val=ua0,
                          forced_updated_at=ua0)
        assert a_ok, 'NEUTER premise: updated_at CAS with a same-ms stamp lets A through'
        tail = _read_tail(db, conv_id)[-1]
        assert tail['content'] == 'A-CLOBBER', (
            'NEUTER premise failed to reproduce the clobber')
    finally:
        _cleanup(db, conv_id)
    _ok('NEUTER: updated_at CAS with same-ms stamp lets A CLOBBER B '
        '(the L3 data-loss the rev token closes)')


# ──────────────────────────────────────────────────────────────────────────
# TEST 2 — terminal regraft must preserve a concurrent translation  (RED)
# ──────────────────────────────────────────────────────────────────────────

def test_terminal_regraft_preserves_concurrent_translation():
    """A translation lands on the tail in the terminal-sync read→write window,
    forcing a CAS miss + regraft. The final answer AND the translation must both
    survive. HEAD's whole-dict regraft (``_fresh_messages[-1] = last_msg``) drops
    the translation → this test FAILS on HEAD by design (tests-first)."""
    from lib.database import DOMAIN_CHAT, get_thread_db, db_execute_with_retry, json_dumps_pg
    from lib.tasks_pkg.manager import (create_task, _sync_result_to_conversation,
                                        _conv_latest_task, _conv_latest_task_lock)

    conv_id = 'cv-revcas-regraft-xlate'
    db = get_thread_db(DOMAIN_CHAT)
    # In-flight: trailing empty assistant placeholder carrying a stable id so
    # both the terminal sync and the simulated translate writer target the SAME
    # row by identity.
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1', 'timestamp': 1, '_msgId': 'mu'},
        {'role': 'assistant', 'content': '', 'timestamp': 2, '_msgId': 'ma'},
    ])

    real_execute = db.execute
    state = {'bumped': False}
    TRANSLATION = '这是并发落库的译文，绝不能被终态重嫁接丢弃'
    SEG_TRANSLATION = '这是并发落库的分段旁白译文'

    def _intercept(sql, params=()):
        # Just before the terminal sync's FIRST messages-UPDATE, a "translate
        # commit" lands on the tail: sets translatedContent (NOT content) and
        # bumps updated_at — exactly what _commit_translation_inner does.
        if (not state['bumped'] and isinstance(sql, str)
                and 'UPDATE conversations' in sql and 'SET messages' in sql):
            state['bumped'] = True
            _cur = real_execute(
                'SELECT messages FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)).fetchone()
            _msgs = _json.loads(_cur[0]) if isinstance(_cur[0], str) else _cur[0]
            _msgs[-1]['translatedContent'] = TRANSLATION
            _msgs[-1]['_showingTranslation'] = True
            _msgs[-1]['_translateDone'] = True
            # ★ segments variant (owner's correction): a per-segment narration
            #   translation is stamped onto segments[i].translatedText. The
            #   backend's regraft carries its OWN settled segments (structure)
            #   but NOT this translatedText — a whole-list overwrite would drop
            #   it exactly like translatedContent. The nested merge must backfill
            #   it by (llmRound,type,deliverable).
            _msgs[-1].setdefault('segments', [
                {'type': 'text', 'llmRound': 0, 'deliverable': False, 'text': 'narr'},
            ])
            _msgs[-1]['segments'][0]['translatedText'] = SEG_TRANSLATION
            real_execute(
                'UPDATE conversations SET messages=?, updated_at=? WHERE id=? AND user_id=1',
                (json_dumps_pg(_msgs), int(time.time() * 1000) + 777, conv_id))
            db.commit()
        return real_execute(sql, params)

    try:
        task = create_task(conv_id, [{'role': 'user', 'content': 'U1'}], {})
        task['content'] = 'THE FINAL ANSWER'
        task['_assistantMsgId'] = 'ma'
        # Backend's own settled segments: same llmRound=0 text segment, but NO
        # translatedText (the backend never produces translations). The nested
        # merge must keep this structure AND backfill the fresh translatedText.
        task['segments'] = [
            {'type': 'text', 'llmRound': 0, 'deliverable': False, 'text': 'narr'},
        ]
        with _conv_latest_task_lock:
            _conv_latest_task[conv_id] = task['id']

        db.execute = _intercept
        try:
            _sync_result_to_conversation(task, {'finishReason': 'stop'})
        finally:
            db.execute = real_execute

        assert state['bumped'], 'interceptor never fired — no CAS miss was forced'
        tail = _read_tail(db, conv_id)[-1]
        # The final answer must land (regraft re-applies our content) …
        assert tail['content'] == 'THE FINAL ANSWER', (
            f'final answer lost after regraft — got {tail["content"]!r}')
        # … AND the concurrently-committed translation must NOT be clobbered.
        assert tail.get('translatedContent') == TRANSLATION, (
            'REGRAFT CLOBBER: the terminal regraft REPLACED the fresh tail with '
            'the backend dict and DROPPED translatedContent committed concurrently. '
            'Phase-4 fix: regraft must MERGE our terminal fields onto the fresh '
            'tail, preserving backend-non-owned fields (translatedContent, '
            '_showingTranslation, segments[].translatedText, originalContent).')
        # The backend's structural segment must win (text='narr') AND carry the
        # concurrently-stamped translatedText (nested merge, not list overwrite).
        _segs = tail.get('segments') or []
        assert _segs and _segs[0].get('translatedText') == SEG_TRANSLATION, (
            'SEGMENTS REGRAFT CLOBBER: the whole-list segments overwrite dropped '
            'segments[0].translatedText. The nested merge must backfill per-segment '
            f'translations by (llmRound,type,deliverable). got segments={_segs!r}')
    finally:
        with _conv_latest_task_lock:
            _conv_latest_task.pop(conv_id, None)
        db.execute = real_execute
        _cleanup(db, conv_id)
    _ok('terminal regraft preserves a concurrently-committed translation')


def test_neuter_wholedict_regraft_redrops_translation():
    """NEUTER: replace the field-level merge with the historical whole-dict
    regraft (``_fresh_messages[-1] = last_msg``) and prove BOTH the top-level
    translatedContent AND segments[0].translatedText are re-dropped — confirming
    ``_merge_terminal_fields`` is what preserves them (load-bearing)."""
    import lib.tasks_pkg.manager._sync as _syncmod
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.tasks_pkg.manager import (create_task, _sync_result_to_conversation,
                                        _conv_latest_task, _conv_latest_task_lock)

    conv_id = 'cv-revcas-neuter'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1', 'timestamp': 1, '_msgId': 'mu'},
        {'role': 'assistant', 'content': '', 'timestamp': 2, '_msgId': 'ma'},
    ])

    real_execute = db.execute
    state = {'bumped': False}
    TRANSLATION = '译文-neuter'

    def _intercept(sql, params=()):
        if (not state['bumped'] and isinstance(sql, str)
                and 'UPDATE conversations' in sql and 'SET messages' in sql):
            state['bumped'] = True
            _cur = real_execute(
                'SELECT messages FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)).fetchone()
            _msgs = _json.loads(_cur[0]) if isinstance(_cur[0], str) else _cur[0]
            _msgs[-1]['translatedContent'] = TRANSLATION
            real_execute(
                'UPDATE conversations SET messages=?, updated_at=? WHERE id=? AND user_id=1',
                (json_dumps_pg(_msgs), int(time.time() * 1000) + 777, conv_id))
            db.commit()
        return real_execute(sql, params)

    # NEUTER: swap the merge for the OLD whole-dict replace semantics.
    _orig_merge = _syncmod._merge_terminal_fields
    def _wholedict_replace(fresh_tail, terminal_msg):
        fresh_tail.clear()
        fresh_tail.update(terminal_msg)
        return fresh_tail

    try:
        task = create_task(conv_id, [{'role': 'user', 'content': 'U1'}], {})
        task['content'] = 'THE FINAL ANSWER'
        task['_assistantMsgId'] = 'ma'
        with _conv_latest_task_lock:
            _conv_latest_task[conv_id] = task['id']

        _syncmod._merge_terminal_fields = _wholedict_replace
        db.execute = _intercept
        try:
            _sync_result_to_conversation(task, {'finishReason': 'stop'})
        finally:
            db.execute = real_execute
            _syncmod._merge_terminal_fields = _orig_merge

        assert state['bumped'], 'interceptor never fired — no CAS miss was forced'
        tail = _read_tail(db, conv_id)[-1]
        assert tail['content'] == 'THE FINAL ANSWER', 'NEUTER: answer should still land'
        assert tail.get('translatedContent') != TRANSLATION, (
            'NEUTER FAILED: whole-dict replace should have DROPPED translatedContent — '
            'if it survived, the merge is not what preserves it')
    finally:
        with _conv_latest_task_lock:
            _conv_latest_task.pop(conv_id, None)
        db.execute = real_execute
        _syncmod._merge_terminal_fields = _orig_merge
        _cleanup(db, conv_id)
    _ok('NEUTER: whole-dict regraft re-drops translatedContent (merge is load-bearing)')


# ──────────────────────────────────────────────────────────────────────────
# TEST 4 — whitelist drift guard (static AST; no DB)
# ──────────────────────────────────────────────────────────────────────────
# The reverse risk of an OWNED-WHITELIST merge: it preserves any UNLISTED field
# from the fresh tail. That is correct for translate-owned fields, but it means
# a FUTURE backend-owned terminal field, if added to the terminal write path but
# forgotten in _TERMINAL_OWNED_FIELDS, would be silently CLOBBERED by a stale/
# empty fresh-tail value on a CAS-miss regraft — the same "forgot to sync one
# list" drift the owner keeps getting bitten by, only this time it drops a
# BACKEND field instead of a translate field. This guard makes the whitelist
# self-policing: every ``last_msg['<literal>'] = …`` write on the terminal path
# must be registered in EXACTLY ONE of the two declared sets (owned=overwrite or
# excluded=nested-merge), else CI fails.

def _terminal_written_keys():
    """Parse lib/tasks_pkg/manager/_sync.py and return the set of string-literal
    keys assigned via ``last_msg['<key>'] = …`` inside ``_sync_result_to_conversation``
    (the terminal path). Reads (``last_msg['x']`` used as an rvalue, e.g. inside a
    Call arg) are naturally NOT assignment targets, so they're excluded."""
    import ast
    import lib.tasks_pkg.manager._sync as _syncmod
    src = open(_syncmod.__file__, encoding='utf-8').read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == '_sync_result_to_conversation'), None)
    assert fn is not None, 'could not locate _sync_result_to_conversation in _sync.py'
    keys = set()

    def _record_target(tgt):
        if (isinstance(tgt, ast.Subscript)
                and isinstance(tgt.value, ast.Name) and tgt.value.id == 'last_msg'):
            sl = tgt.slice
            # py3.9+: slice is the Constant directly; older wraps in ast.Index
            if isinstance(sl, ast.Index):  # pragma: no cover - old py
                sl = sl.value
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                keys.add(sl.value)

    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                _record_target(t)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            _record_target(node.target)
    return keys


def _unregistered(keys, owned, excluded):
    """Keys that are in NEITHER declared set — the drift the guard must catch."""
    return set(keys) - set(owned) - set(excluded)


def test_terminal_owned_fields_cover_all_writes():
    """Every string-literal key the terminal path writes onto ``last_msg`` must
    be registered in exactly one declared ownership set. Also asserts the inbox
    sidecar keys (written via a loop var in ``_persist_inject_sidecars``, so not
    caught by the literal scan) are in the owned set."""
    from lib.tasks_pkg.manager._sync import (
        _TERMINAL_OWNED_FIELDS, _TERMINAL_MERGE_EXCLUDED, INBOX_INJECT_SIDECAR_FIELDS)

    written = _terminal_written_keys()
    assert written, 'AST scan found NO last_msg writes — scanner is broken, not clean'

    missing = _unregistered(written, _TERMINAL_OWNED_FIELDS, _TERMINAL_MERGE_EXCLUDED)
    assert not missing, (
        'DRIFT: these terminal-path last_msg writes are registered in NEITHER '
        '_TERMINAL_OWNED_FIELDS nor _TERMINAL_MERGE_EXCLUDED, so on a CAS-miss '
        'regraft they would be silently clobbered by a stale fresh-tail value: '
        f'{sorted(missing)}. Add each to the owned set (whole-value overwrite) '
        'or the excluded set (bespoke nested merge).')

    # A key must not be double-registered (owned AND excluded is contradictory).
    _dup = set(_TERMINAL_OWNED_FIELDS) & set(_TERMINAL_MERGE_EXCLUDED)
    assert not _dup, f'keys registered in BOTH owned and excluded: {sorted(_dup)}'

    # Sidecar lanes are persisted onto last_msg on the terminal path via a loop
    # var, so the literal scan can't see them — assert they're in the owned set.
    _sidecar_missing = set(INBOX_INJECT_SIDECAR_FIELDS) - set(_TERMINAL_OWNED_FIELDS)
    assert not _sidecar_missing, (
        f'inbox sidecar fields not in _TERMINAL_OWNED_FIELDS: {sorted(_sidecar_missing)}')
    _ok(f'terminal ownership sets cover all {len(written)} literal last_msg '
        f'writes (+ {len(INBOX_INJECT_SIDECAR_FIELDS)} sidecar) — no drift')


def test_neuter_drift_guard_catches_unregistered_field():
    """NEUTER: simulate a NEW terminal field written but forgotten in both sets.
    The guard's coverage check MUST flag it — proving the guard is load-bearing
    (not vacuously green because the current source happens to be clean)."""
    from lib.tasks_pkg.manager._sync import (
        _TERMINAL_OWNED_FIELDS, _TERMINAL_MERGE_EXCLUDED)

    written = _terminal_written_keys()
    FAKE = '__fake_unregistered_terminal_field__'
    # Real source is clean …
    assert _unregistered(written, _TERMINAL_OWNED_FIELDS, _TERMINAL_MERGE_EXCLUDED) == set()
    # … but the moment a write lands that's in neither set, the guard flags it.
    drifted = _unregistered(written | {FAKE}, _TERMINAL_OWNED_FIELDS, _TERMINAL_MERGE_EXCLUDED)
    assert drifted == {FAKE}, (
        'NEUTER FAILED: the coverage check did not flag an unregistered field — '
        'the drift guard would not catch a forgotten future terminal field')
    _ok('NEUTER: drift guard flags an unregistered terminal field '
        '(coverage check is load-bearing)')


# ──────────────────────────────────────────────────────────────────────────
# TEST 5 (step-2, static) — no message writer stamps rev in its SET clause
# ──────────────────────────────────────────────────────────────────────────
# The trigger (conversations_rev_bump_trg) OWNS rev — it advances it in the SAME
# statement as any messages write. A writer that ALSO wrote ``SET rev=?`` would
# fight the trigger (double-bump on PG BEFORE; on SQLite the AFTER trigger's
# nested UPDATE would re-fire on a rev-only write only if it touched messages —
# it doesn't, but a manual rev in the messages UPDATE still corrupts the
# monotonic sequence). This static guard asserts NO conversations-messages
# UPDATE in the two CAS writers sets ``rev`` — the token may only appear in the
# WHERE clause. It's the invariant behind "trigger is the sole bumper".

def _conversations_update_statements(path):
    """Return the list of string-literal SQL fragments in ``path`` that UPDATE
    the conversations table's messages column (crude but sufficient: any string
    containing both 'UPDATE conversations' and 'SET messages')."""
    import ast
    src = open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    out = []

    def _walk_str(node):
        # Join implicitly-concatenated / f-string constant parts into one text.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return ''.join(_walk_str(v) or '' for v in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return (_walk_str(node.left) or '') + (_walk_str(node.right) or '')
        return ''

    for node in ast.walk(tree):
        s = _walk_str(node)
        if s and 'UPDATE conversations' in s and 'SET messages' in s:
            out.append(s)
    return out


def test_no_writer_stamps_rev_in_set_clause():
    """Neither the terminal sync nor the translate commit may write ``rev`` in a
    messages UPDATE's SET clause — the trigger is the sole bumper. Guards the
    step-2 token switch from accidentally introducing a manual rev stamp."""
    import lib.tasks_pkg.manager._sync as _syncmod
    import lib.translate.commit as _commitmod

    for label, path in (('_sync.py', _syncmod.__file__),
                        ('commit.py', _commitmod.__file__)):
        stmts = _conversations_update_statements(path)
        assert stmts, f'{label}: found NO conversations messages UPDATE — scanner broken'
        for s in stmts:
            # Extract the SET…WHERE span and assert 'rev' is not an assigned col.
            _up = s.upper()
            _set_i = _up.find('SET ')
            _where_i = _up.find('WHERE', _set_i)
            set_clause = s[_set_i:_where_i] if _where_i > _set_i else s[_set_i:]
            assert 'rev=' not in set_clause.replace(' ', '') and 'REV=' not in set_clause.upper().replace(' ', ''), (
                f'{label}: a messages UPDATE writes rev in its SET clause — the '
                f'trigger must be the sole bumper. Offending SQL:\n{s}')
    _ok('no message writer stamps rev in SET (trigger is sole bumper)')


# ──────────────────────────────────────────────────────────────────────────
# TEST 3 (step-2) — high-frequency terminal×translate contention: nothing lost
# ──────────────────────────────────────────────────────────────────────────

def test_high_freq_terminal_translate_contention_loses_nothing():
    """Drive the REAL terminal sync while a REAL translate commit lands in its
    read→write window under a FROZEN CLOCK, forcing the SAME-MILLISECOND
    collision — the exact shape only ``rev`` can survive.

    Why the frozen clock (this is the crux — an earlier version was falsely
    GREEN without it): if the concurrent translate writer stamps a DIFFERENT
    ``updated_at`` than the terminal sync's baseline, the terminal CAS simply
    MISSES and the §2.2 regraft-merge (already in HEAD) preserves everything —
    so the test would pass even on the updated_at token and prove nothing about
    the switch. The production bug the owner fears is the SAME-MS case: both
    writers compute ``now_ms == baseline``, so the terminal CAS ``WHERE
    updated_at=baseline`` does NOT miss — it PASSES and CLOBBERS the translation
    the other writer just committed. We reproduce that deterministically by
    freezing ``time.time()`` to the seeded row's timestamp for the whole round,
    so every ``int(time.time()*1000)`` equals the baseline.

    Under updated_at (HEAD): terminal CAS passes → translation clobbered → RED.
    Under rev (post W1+W6): translate bumps rev via the trigger → terminal CAS
    ``WHERE rev=baseline_rev`` MISSES → regraft-merge → BOTH survive → GREEN.

    Tests-first: expected RED on HEAD, GREEN once W1+W6 switch to rev CAS."""
    import time as _time
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg.manager import (create_task, _sync_result_to_conversation,
                                        _conv_latest_task, _conv_latest_task_lock)
    from lib.translate.commit import _commit_translation_to_db

    N = 5
    losses = []
    for r in range(N):
        conv_id = f'cv-revcas-contend-{r}'
        db = get_thread_db(DOMAIN_CHAT)
        _seed_conv(db, conv_id, [
            {'role': 'user', 'content': f'U{r}', 'timestamp': 1, '_msgId': 'mu'},
            {'role': 'assistant', 'content': '', 'timestamp': 2, '_msgId': 'ma'},
        ])
        # Freeze the clock at the seeded row's exact updated_at so BOTH writers
        # compute now_ms == baseline → the same-ms collision.
        _msgs0, ua0, _rev0 = _row(db, conv_id)
        _frozen = ua0 / 1000.0
        real_execute = db.execute
        real_time = _time.time
        TRANS = f'译文-{r}'
        state = {'fired': False}

        def _intercept(sql, params=()):
            # On the terminal sync's FIRST messages-UPDATE, fire a REAL translate
            # commit against the SAME row (its own CAS loop). Under the frozen
            # clock it stamps updated_at==baseline, so on HEAD the terminal CAS
            # that follows still matches baseline and clobbers.
            if (not state['fired'] and isinstance(sql, str)
                    and 'UPDATE conversations' in sql and 'SET messages' in sql):
                state['fired'] = True
                db.execute = real_execute
                try:
                    _commit_translation_to_db(conv_id, 1, 'translatedContent', TRANS,
                                               msg_id='ma', model='test')
                finally:
                    db.execute = _intercept
            return real_execute(sql, params)

        try:
            task = create_task(conv_id, [{'role': 'user', 'content': f'U{r}'}], {})
            task['content'] = f'ANSWER-{r}'
            task['_assistantMsgId'] = 'ma'
            with _conv_latest_task_lock:
                _conv_latest_task[conv_id] = task['id']
            _time.time = lambda: _frozen
            db.execute = _intercept
            try:
                _sync_result_to_conversation(task, {'finishReason': 'stop'})
            finally:
                db.execute = real_execute
                _time.time = real_time
            tail = _read_tail(db, conv_id)[-1]
            ok_answer = tail.get('content') == f'ANSWER-{r}'
            ok_trans = tail.get('translatedContent') == TRANS
            if not (ok_answer and ok_trans):
                losses.append((r, tail.get('content'), tail.get('translatedContent')))
        finally:
            with _conv_latest_task_lock:
                _conv_latest_task.pop(conv_id, None)
            db.execute = real_execute
            _time.time = real_time
            _cleanup(db, conv_id)

    assert not losses, (
        f'CONTENTION LOSS in {len(losses)}/{N} rounds — the terminal answer and/or '
        f'the concurrent translation was dropped: {losses}. This is the same-ms '
        f'clobber under updated_at CAS (frozen clock forces both writers onto the '
        f'baseline ms); W1+W6 switching to rev CAS must make the terminal write '
        f'MISS-and-regraft so BOTH survive every round.')
    _ok(f'high-freq terminal×translate contention (frozen-clock same-ms): {N}/{N} '
        f'rounds kept BOTH answer and translation (nothing lost)')


# ──────────────────────────────────────────────────────────────────────────
# TEST 5b (step-2) — creator path (persist_conv_messages) is CAS-free: a rev>0
#                    row is appended to without a false 409
# ──────────────────────────────────────────────────────────────────────────

def test_creator_path_exempt_from_rev_cas():
    """The CREATE/append path ``persist_conv_messages`` uses ``upsert()`` with NO
    CAS — it is the turn's creator, not a concurrent writer, so it must stay in
    the CAS-exempt domain (C1). Construct the REAL shape the owner asked for:
    seed rev=0 → a translate commit bumps it to rev>0 → persist_conv_messages
    appends a new turn → it must land (no false 409) and rev keeps advancing."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.chat.persistence import persist_conv_messages
    from lib.translate.commit import _commit_translation_to_db

    conv_id = 'cv-revcas-creator'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1', 'timestamp': 1, '_msgId': 'mu'},
        {'role': 'assistant', 'content': 'A1', 'timestamp': 2, '_msgId': 'ma'},
    ])
    try:
        _msgs, _ua0, rev0 = _row(db, conv_id)
        assert rev0 == 0, f'fresh row should start at rev=0, got {rev0}'

        # A translate commit bumps rev to >0 (the row is now "versioned").
        _commit_translation_to_db(conv_id, 1, 'translatedContent', '译文', msg_id='ma')
        _msgs1, _ua1, rev1 = _row(db, conv_id)
        assert rev1 > rev0, f'translate commit should bump rev ({rev0}→?), got {rev1}'

        # Now the creator path appends a NEW turn onto the rev>0 row. It reads no
        # baseline token and does not CAS, so it must simply land.
        _msgs1.append({'role': 'user', 'content': 'U2', 'timestamp': 3})
        _msgs1.append({'role': 'assistant', 'content': 'A2', 'timestamp': 4})
        _new_rev = persist_conv_messages(db, conv_id, _msgs1, 'creator-test')

        _msgs2, _ua2, rev2 = _row(db, conv_id)
        assert len(_msgs2) == 4, (
            f'creator append should have landed all 4 messages, got {len(_msgs2)}: '
            f'{[m.get("content") for m in _msgs2]}')
        assert _msgs2[-1].get('content') == 'A2', 'creator append tail mismatch'
        assert rev2 > rev1, (
            f'creator append changed messages so the trigger must bump rev '
            f'({rev1}→?), got {rev2} — no false 409 / no skipped write')
        # The translation from the earlier bump must still be present (the
        # creator append carried the whole array forward).
        assert _msgs2[1].get('translatedContent') == '译文', (
            'creator append dropped the earlier translation')
        if _new_rev is not None:
            assert _new_rev == rev2, (
                f'persist_conv_messages returned rev {_new_rev} but row is at {rev2}')
    finally:
        _cleanup(db, conv_id)
    _ok('creator path (persist_conv_messages) appends to a rev>0 row with no '
        'false 409 (CAS-exempt, C1)')


# ──────────────────────────────────────────────────────────────────────────
# TEST W2-guard (static) — rev-not-in-SET across ALL messages writers (W2–W5)
# ──────────────────────────────────────────────────────────────────────────
# Extends the W1+W6 rev-not-in-SET guard to the remaining CAS messages writers
# that Batch 1a will switch. Runs NOW (pre-switch) so it can't regress: it only
# asserts NO writer writes rev in SET. On HEAD these files carry no rev at all
# (vacuously pass); after Batch 1a they carry rev ONLY in WHERE — still pass.
# The moment a switch accidentally puts rev in a SET clause, this goes RED.

def test_no_writer_stamps_rev_in_set_clause_w2_w5():
    """No W2–W5 messages writer may write ``rev`` in a SET clause — the trigger
    is the sole bumper. Guards the Batch-1a token switch the same way the W1+W6
    guard does. Reuses _conversations_update_statements from the W1+W6 guard."""
    import importlib
    targets = [
        'lib.chat.persistence',
        'lib.message_queue',
        'lib.tasks_pkg.auto_translate._assistant',
        'lib.swarm.snapshot',
        'lib.tasks_pkg.persistence_store',
    ]
    scanned = 0
    for modname in targets:
        mod = importlib.import_module(modname)
        stmts = _conversations_update_statements(mod.__file__)
        for s in stmts:
            _up = s.upper()
            _set_i = _up.find('SET ')
            if _set_i < 0:
                continue
            _where_i = _up.find('WHERE', _set_i)
            set_clause = (s[_set_i:_where_i] if _where_i > _set_i else s[_set_i:])
            assert 'rev=' not in set_clause.replace(' ', '').lower(), (
                f'{modname}: a messages UPDATE writes rev in its SET clause — the '
                f'trigger must be the sole bumper. Offending SQL:\n{s}')
            scanned += 1
    assert scanned > 0, 'scanner found no messages UPDATEs across W2–W5 files — broken'
    _ok(f'W2–W5 files: no messages writer stamps rev in SET ({scanned} UPDATEs scanned)')


# ──────────────────────────────────────────────────────────────────────────
# T-queue (step-3, tests-first) — queued-mirror same-ms must not clobber terminal
# ──────────────────────────────────────────────────────────────────────────

def test_queue_mirror_same_ms_no_clobber():
    """The residual same-ms window the owner named: a terminal write lands the
    final answer, then the queued-user mirror (append_pending_user_msg) fires in
    the SAME millisecond. Under updated_at CAS the mirror's baseline still equals
    the row's stamp (frozen clock), so its UPDATE PASSES and clobbers the terminal
    answer with the mirror's pre-terminal messages array. Under rev CAS the
    terminal write bumped rev via the trigger, so the mirror MISSES → re-reads →
    appends onto the fresh tail → BOTH the answer AND the pending user row survive.

    Drives the REAL append_pending_user_msg (W2). Tests-first: RED on HEAD
    (updated_at), GREEN after Batch 1a switches W2 to rev.

    Reproduction: append_pending_user_msg captures its baseline at loop top; we
    intercept ITS messages-UPDATE and, just before it lands, fire a terminal-style
    write (content='FINAL', stamps updated_at & bumps rev). Frozen clock forces
    the terminal write's updated_at to equal the mirror's baseline ms."""
    import time as _time
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.chat.persistence import append_pending_user_msg

    conv_id = 'cv-revcas-queuemirror'
    db = get_thread_db(DOMAIN_CHAT)
    # Tail is the running turn's assistant slot (empty), id 'ma' — the gate
    # append_pending_user_msg requires (tail assistant + _msgId in valid ids).
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1', 'timestamp': 1, '_msgId': 'mu'},
        {'role': 'assistant', 'content': '', 'timestamp': 2, '_msgId': 'ma'},
    ])
    _msgs0, ua0, _rev0 = _row(db, conv_id)
    _frozen = ua0 / 1000.0
    real_execute = db.execute
    real_time = _time.time
    state = {'fired': False}

    def _intercept(sql, params=()):
        # On the mirror's messages-UPDATE, first land a terminal-style write that
        # sets the answer + bumps rev, THEN let the mirror's UPDATE proceed. Under
        # the frozen clock the terminal write stamps updated_at==ua0==the mirror's
        # baseline, so on updated_at the mirror still matches and clobbers.
        if (not state['fired'] and isinstance(sql, str)
                and 'UPDATE conversations' in sql and 'SET messages' in sql):
            state['fired'] = True
            db.execute = real_execute
            try:
                _cur = real_execute(
                    'SELECT messages FROM conversations WHERE id=? AND user_id=1',
                    (conv_id,)).fetchone()
                _m = _json.loads(_cur[0]) if isinstance(_cur[0], str) else _cur[0]
                _m[-1]['content'] = 'FINAL'
                _m[-1]['finishReason'] = 'stop'
                real_execute(
                    'UPDATE conversations SET messages=?, updated_at=? WHERE id=? AND user_id=1',
                    (json_dumps_pg(_m), int(_time.time() * 1000), conv_id))
                db.commit()
            finally:
                db.execute = _intercept
        return real_execute(sql, params)

    try:
        _time.time = lambda: _frozen
        db.execute = _intercept
        try:
            # valid_assistant_ids must contain the running assistant slot id.
            append_pending_user_msg(
                db, conv_id,
                {'role': 'user', 'content': 'QUEUED', 'timestamp': 3},
                valid_assistant_ids={'ma'})
        finally:
            db.execute = real_execute
            _time.time = real_time

        assert state['fired'], 'interceptor never fired — no same-ms terminal write injected'
        msgs = _read_tail(db, conv_id)
        _assistant = next((m for m in msgs if m.get('_msgId') == 'ma'), None)
        assert _assistant is not None, 'assistant slot ma vanished'
        assert _assistant.get('content') == 'FINAL', (
            'QUEUE-MIRROR CLOBBER: the queued-user mirror overwrote the terminal '
            f'answer under a same-ms CAS. assistant.content={_assistant.get("content")!r}. '
            'W2 switching append_pending_user_msg to rev CAS must make the mirror '
            'MISS-and-reread so the terminal answer survives.')
        # The pending queued user row should also be present (mirror still lands
        # its row after the re-read).
        assert any(m.get('content') == 'QUEUED' for m in msgs), (
            'the queued user row was lost — mirror should re-read and append it')
    finally:
        db.execute = real_execute
        _time.time = real_time
        _cleanup(db, conv_id)
    _ok('queued-mirror same-ms: terminal answer survives + queued row kept (no clobber)')


_POSITIVE = [test_rev_distinguishes_same_ms_writers,
             test_terminal_regraft_preserves_concurrent_translation,
             test_neuter_wholedict_regraft_redrops_translation,
             test_terminal_owned_fields_cover_all_writes,
             test_neuter_drift_guard_catches_unregistered_field,
             test_no_writer_stamps_rev_in_set_clause,
             test_no_writer_stamps_rev_in_set_clause_w2_w5,
             test_creator_path_exempt_from_rev_cas]

# Step-2/3 contention tests kept SEPARATE from _POSITIVE: tests-first RED on HEAD
# and only GREEN after the corresponding writers switch to rev CAS.
#   - high_freq…  : GREEN now (W1+W6 already switched, commit 2bd6702)
#   - queue_mirror: RED now, GREEN after Batch 1a switches W2
_STEP2_PENDING = [test_high_freq_terminal_translate_contention_loses_nothing]
_STEP3_PENDING = [test_queue_mirror_same_ms_no_clobber]


def _run(fn):
    try:
        fn()
        return True
    except AssertionError as e:
        print(' ', _color('✗', '31'), f'{fn.__name__}: {e}')
        return False
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(' ', _color('✗', '31'), f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
        return False


def main():
    print()
    print(_color('═══ Phase-4 rev-CAS migration — token proof + regraft preservation ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_rev_cas_migration.__main__')

    print(_color('Phase-4 §2.2 + step-2 static guards (known-green):', '36'))
    _pos_ok = all(_run(fn) for fn in _POSITIVE)
    print()
    print(_color('Step-2 contention (W1+W6 switched — GREEN):', '33'))
    _step2 = {fn.__name__: _run(fn) for fn in _STEP2_PENDING}
    for _n, _r in _step2.items():
        print('   ', _color('GREEN' if _r else 'RED (unexpected!)', '32' if _r else '31'), _n)
    print()
    print(_color('Step-3 W2 queued-mirror (tests-first — RED on HEAD, GREEN after Batch 1a):', '33'))
    _step3 = {fn.__name__: _run(fn) for fn in _STEP3_PENDING}
    for _n, _r in _step3.items():
        print('   ', _color('GREEN' if _r else 'RED (expected pre-switch)', '32' if _r else '33'), _n)
    print()
    if not _pos_ok:
        _fail('a known-green Phase-4 test failed')
    print(_color(f'═══ {len(_POSITIVE)} known-green PASSED; '
                 f'step-2 {sum(_step2.values())}/{len(_step2)} GREEN; '
                 f'step-3 {sum(_step3.values())}/{len(_step3)} GREEN (RED expected pre-Batch-1a) ═══', '36'))
    print()


if __name__ == '__main__':
    main()
