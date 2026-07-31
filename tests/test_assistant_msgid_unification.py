"""Duplicate-assistant-bubble root-cause regression.

THE BUG (observed on 793/3728 live conversations)
--------------------------------------------------
Every assistant turn was materialised TWICE in ``conversations.messages``:
  * a BACKEND-created copy (server UUID ``_msgId``, ``_gitSha``/``_snapshotId``,
    no ``_committedProjection``) at the correct interleaved position, AND
  * a FRONTEND live bubble (``tmp_`` ``_msgId``, ``_committedProjection:true``,
    ``_liveLastRoundUsage``) appended at the tail.

Both shared the SAME ``_taskId`` and identical content — one turn shown twice.

ROOT CAUSE — DIVERGENT IDENTITY. The frontend mints ``_assistantMsgId =
'tmp_...'`` before the send POST and stamps it on the live bubble; the backend
commit (``_sync_result_to_conversation`` / ``_sync_partial_to_conversation``)
created a fresh assistant slot and let ``_assign_message_ids`` mint a *different*
server UUID, IGNORING the shipped ``_assistantMsgId``. Because the two ids never
matched, a reconnect / rescue-PUT merge keyed on ``_msgId`` could not recognise
the live bubble as the committed row and appended it a SECOND time.

This mirrors the user-message fix already in ``build_user_msg_from_payload``
(turn_builder.py), which preserves the client ``_msgId`` verbatim for the exact
same "duplicate bubble" reason. This suite pins the assistant-side analogue.

TWO layers, each with a NEUTER control proving the fix is load-bearing:
  1. BACKEND (real ``_sync_result_to_conversation`` / ``_sync_partial_to_conversation``
     against a temp DB): a task carrying ``_assistantMsgId='tmp_...'`` commits an
     assistant slot whose ``_msgId`` IS that id (not a fresh UUID); an empty
     ``_assistantMsgId`` still falls back to a minted UUID (no regression).
  2. FRONTEND (real shipped ``_rebaseUnackedTail`` under node): a local assistant
     bubble whose ``_taskId`` the server already carries is deduped even when its
     ``_msgId`` is a divergent ``tmp_`` id (protects pre-fix bubbles in flight).
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._conv_bundle_sources import JS_DIR, source_argv, sources_defining

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ═══════════════════════════════════════════════════════════════════════════
#  LAYER 1 — backend adopts the client _assistantMsgId (real DB sync)
# ═══════════════════════════════════════════════════════════════════════════

def _commit_via_real_sync(conv_id, *, assistant_msg_id, content='the answer',
                          thinking='', partial=False):
    """Seed a conv whose tail is a USER turn (so the sync must CREATE the
    assistant slot), drive the REAL sync, and return the committed tail dict."""
    from lib.database import DOMAIN_CHAT, get_thread_db, db_execute_with_retry
    from lib.tasks_pkg.manager import (
        create_task, _sync_result_to_conversation, _sync_partial_to_conversation,
        build_result_meta, _conv_latest_task, _conv_latest_task_lock,
    )
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    import time as _t

    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(_t.time() * 1000)
    # Tail is a user turn → no trailing assistant → sync appends a NEW slot.
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'msgid-unif-test',
        'messages': json_dumps_pg([
            {'role': 'user', 'content': 'U1', 'timestamp': 1, '_msgId': 'm-u1'},
        ]),
        'msg_count': 1, 'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()
    try:
        task = create_task(conv_id, [{'role': 'user', 'content': 'U1'}],
                           {'assistantMsgId': assistant_msg_id})
        task['content'] = content
        task['thinking'] = thinking
        task['finishReason'] = 'stop'
        with _conv_latest_task_lock:
            _conv_latest_task[conv_id] = task['id']
        if partial:
            _sync_partial_to_conversation(task)
        else:
            _sync_result_to_conversation(task, build_result_meta(task))
        # Read back the committed tail from the DB (authoritative).
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)).fetchone()
        import json as _json
        msgs = _json.loads(row[0] or '[]')
        return task['id'], (msgs[-1] if msgs else None), msgs
    finally:
        with _conv_latest_task_lock:
            _conv_latest_task.pop(conv_id, None)
        db_execute_with_retry(
            db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()


@pytest.mark.unit
def test_terminal_sync_adopts_client_assistant_msgid():
    """The REAL terminal sync stamps the created assistant slot with the
    client-shipped _assistantMsgId — NOT a fresh server UUID."""
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_assistant_msgid_unification.terminal')
    tid, tail, msgs = _commit_via_real_sync(
        'cv-msgid-terminal', assistant_msg_id='tmp_client_abc123')
    assert tail is not None and tail.get('role') == 'assistant'
    assert tail.get('_msgId') == 'tmp_client_abc123', (
        f'created assistant slot did NOT adopt the client _assistantMsgId — '
        f'got _msgId={tail.get("_msgId")!r} (the divergent-UUID bug)')
    # Exactly ONE assistant for this turn (no duplicate materialised).
    asst = [m for m in msgs if m.get('role') == 'assistant']
    assert len(asst) == 1, f'expected exactly one assistant slot, got {len(asst)}'


@pytest.mark.unit
def test_partial_sync_adopts_client_assistant_msgid():
    """The REAL partial checkpoint sync ALSO stamps the created slot with the
    client id (both slot-creation points unified, not only the terminal path)."""
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_assistant_msgid_unification.partial')
    tid, tail, msgs = _commit_via_real_sync(
        'cv-msgid-partial', assistant_msg_id='tmp_client_def456',
        content='partial so far', partial=True)
    assert tail is not None and tail.get('role') == 'assistant'
    assert tail.get('_msgId') == 'tmp_client_def456', (
        f'partial-sync created slot did NOT adopt the client _assistantMsgId — '
        f'got _msgId={tail.get("_msgId")!r}')


@pytest.mark.unit
def test_empty_assistant_msgid_falls_back_to_minted_uuid():
    """No regression for headless / legacy callers: an empty _assistantMsgId
    still gets a freshly-minted UUID (a real id, just server-side)."""
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_assistant_msgid_unification.fallback')
    tid, tail, msgs = _commit_via_real_sync(
        'cv-msgid-fallback', assistant_msg_id='')
    assert tail is not None and tail.get('role') == 'assistant'
    mid = tail.get('_msgId') or ''
    assert mid and not mid.startswith('tmp_'), (
        f'empty _assistantMsgId should mint a server UUID; got {mid!r}')


@pytest.mark.unit
def test_new_assistant_slot_helper_is_pure():
    """The _new_assistant_slot helper adopts the id when present, omits it when
    absent (so _assign_message_ids mints one) — the load-bearing branch."""
    from lib.tasks_pkg.manager import _new_assistant_slot
    with_id = _new_assistant_slot({'_assistantMsgId': 'tmp_x'})
    assert with_id.get('_msgId') == 'tmp_x' and with_id['role'] == 'assistant'
    without = _new_assistant_slot({'_assistantMsgId': ''})
    assert '_msgId' not in without, 'empty id must NOT stamp an _msgId'
    none_task = _new_assistant_slot({})
    assert '_msgId' not in none_task


# ═══════════════════════════════════════════════════════════════════════════
#  LAYER 2 — frontend _rebaseUnackedTail dedups by _taskId (real JS under node)
# ═══════════════════════════════════════════════════════════════════════════

_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.console = console;
global.activeStreams = new Map();
global.ConvCache = { put() {}, remove() {} };
global.debugLog = function() {};
global.Api = { conversations: {} };
global.config = {};
global.activeConvId = null;
global.renderChat = function() {};

/* Eval every shipped file the bundle needs for _rebaseUnackedTail, in
 * production order (resolved by tests/_conv_bundle_sources.py). Hard-coding the
 * two paths broke when the cluster moved in Epic-E slice 3. */
for (let i = 2; i < process.argv.length; i++) {
  eval(fs.readFileSync(process.argv[i], 'utf8'));
}

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _rebaseUnackedTail !== 'function') {
  console.log('FAIL fn_exposed _rebaseUnackedTail missing'); process.exit(0);
}

// ── The exact production bug: server has the committed assistant (UUID id),
//    the client still holds the SAME turn's live bubble with a divergent tmp_
//    id. Same _taskId. The rescue-PUT must NOT re-append the tmp_ twin. ──
const serverMsgs = [
  { role: 'user', content: 'U1', timestamp: 1000, _msgId: 'm-u1' },
  { role: 'assistant', content: 'the answer', timestamp: 1100,
    finishReason: 'stop', _msgId: 'server-uuid-1', _taskId: 'task-abc' },
];
const localMsgs = [
  { role: 'user', content: 'U1', timestamp: 1000, _msgId: 'm-u1' },
  { role: 'assistant', content: 'the answer', timestamp: 1100,
    _committedProjection: true, _msgId: 'tmp_client_1', _taskId: 'task-abc' },
];
const merged = _rebaseUnackedTail(serverMsgs, localMsgs);
const asst = merged.filter(m => m.role === 'assistant');
check('single_assistant_after_rebase', asst.length === 1);
check('kept_server_committed_copy', asst.some(m => m._msgId === 'server-uuid-1'));
check('dropped_tmp_twin', !merged.some(m => m._msgId === 'tmp_client_1'));

// ── A genuinely NEW local assistant (its _taskId is NOT on the server) must
//    still be appended — the dedup only fires for a taskId the server has. ──
const merged2 = _rebaseUnackedTail(
  [{ role: 'user', content: 'U1', timestamp: 1000, _msgId: 'm-u1' },
   { role: 'assistant', content: 'old', _msgId: 'srv-a', _taskId: 'task-old' }],
  [{ role: 'user', content: 'U1', timestamp: 1000, _msgId: 'm-u1' },
   { role: 'assistant', content: 'old', _msgId: 'srv-a', _taskId: 'task-old' },
   { role: 'user', content: 'U2', timestamp: 2000, _msgId: 'm-u2' },
   { role: 'assistant', content: 'brand new', _msgId: 'tmp_new',
     _taskId: 'task-new' }]);
check('genuine_new_task_appended', merged2.some(m => m._msgId === 'tmp_new'));

console.log(out.join('\n'));
"""


def _run(override=None):
    harness = os.path.join(HERE, '_msgid_unif_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    paths = source_argv('_rebaseUnackedTail', override=override)
    try:
        return subprocess.run(['node', harness, *paths],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_frontend_taskid_dedup_drops_tmp_twin():
    proc = _run()
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, '_taskId dedup failures:\n' + output
    assert output.count('PASS') >= 4, f'expected >=4 PASS:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_neuter_taskid_dedup_is_load_bearing(tmp_path):
    """NEUTER: remove the _taskId dedup branch → the tmp_ twin is re-appended →
    single_assistant_after_rebase FAILS. Proves the dedup is load-bearing."""
    # Epic-E slice 3 (b33d9d21) moved _rebaseUnackedTail (and its _taskId dedup
    # branch) out of conversations.js — locate it by SYMBOL so a further move
    # re-points automatically instead of leaving the neuter aimed at a file that
    # no longer defines it (a neuter that cannot bite reads as a passing guard).
    helpers_js = sources_defining('_rebaseUnackedTail')[0]
    with open(helpers_js, encoding='utf-8') as f:
        src = f.read()
    marker = "if (lm.role === 'assistant' && lm._taskId && serverAsstTaskIds.has(lm._taskId)) {"
    assert marker in src, 'neuter target not found — update the _taskId dedup marker'
    # Neuter by forcing the branch condition to false (guard never fires).
    neutered = src.replace(
        marker,
        "if (false && lm.role === 'assistant' && lm._taskId && serverAsstTaskIds.has(lm._taskId)) {  // NEUTER",
        1)
    nfile = tmp_path / 'conv_persist_helpers_neutered.js'
    nfile.write_text(neutered, encoding='utf-8')
    rel = os.path.relpath(helpers_js, JS_DIR).replace(os.sep, '/')
    proc = _run(override={rel: str(nfile)})
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    assert lines.get('single_assistant_after_rebase') is False, (
        'NEUTER did not bite: the tmp_ twin was still deduped without the '
        '_taskId branch — the dedup is not load-bearing.\n' + output)
    assert lines.get('dropped_tmp_twin') is False, (
        'NEUTER: the tmp_ twin should reappear.\n' + output)
    # A genuinely new task must still append (neuter only removed the dedup).
    assert lines.get('genuine_new_task_appended') is True, output
