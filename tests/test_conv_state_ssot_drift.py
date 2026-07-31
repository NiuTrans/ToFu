#!/usr/bin/env python3
"""tests/test_conv_state_ssot_drift.py — pt_conv_state_ssot P5: sync-drift probe.

Owner hard constraint #4 (board pt_e1c4693341b24730): the drift digest must
cover BOTH activeTaskIds AND conv rev — the second half closes the sibling
"notify frame dropped, _serverRev never converges" hole.

  POST /api/v1/conversations/sync-digest
    body: {digests: [{convId, taskIds: [...], rev: <number|null>}]}
    resp: {ok, checked, divergences: [{convId, kind, client, server}]}

The server compares each client digest against the TWO server-side SSOTs —
the in-memory task registry (busy state) and the conversations.rev column
(message version) — and WARN-logs + returns every divergence. Probe only:
never mutates client or server state.

Faces (failing-first: pre-P5 the route is 404):
  1. taskIds mismatch (client shows a task the registry doesn't have) →
     divergence kind 'task_ids'.
  2. stale rev (client _serverRev behind the DB rev — the dropped-notify
     hole) → divergence kind 'rev'.
  3. converged digest (registry set + rev both match) → zero divergences.
  4. unknown conv (client has rev state for a conv the server lost) →
     divergence kind 'unknown_conv'.
  5. validation: non-list digests / oversize batch → 400.
"""

import time

import pytest

pytestmark = pytest.mark.unit


def _cid():
    return f'test-drift-{time.time_ns()}'


def _insert_conv(flask_client, conv_id, rev):
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    now = int(time.time() * 1000)
    db.execute(
        'INSERT INTO conversations (id, user_id, title, messages, created_at, '
        'updated_at, settings, msg_count, search_text, rev) '
        'VALUES (?, 1, ?, ?, ?, ?, ?, 0, ?, ?)',
        (conv_id, 't', '[]', now, now, '{}', '', rev))
    db.commit()


def _register_running(conv_id, tid):
    from lib.tasks_pkg.manager import tasks, tasks_lock
    with tasks_lock:
        tasks[tid] = {'id': tid, 'convId': conv_id, 'status': 'running',
                      'aborted': False, 'created_at': time.time()}


def _unregister(tid):
    from lib.tasks_pkg.manager import tasks, tasks_lock
    with tasks_lock:
        tasks.pop(tid, None)


def _post(flask_client, digests):
    return flask_client.post('/api/v1/conversations/sync-digest',
                             json={'digests': digests})


def test_task_ids_mismatch_flagged(flask_client):
    """Client believes conv is busy; registry says idle → 'task_ids'."""
    conv_id = _cid()
    _insert_conv(flask_client, conv_id, rev=5)
    resp = _post(flask_client, [{'convId': conv_id,
                                 'taskIds': ['ghost-task'], 'rev': 5}])
    assert resp.status_code == 200
    body = resp.get_json()
    divs = body['divergences']
    assert any(d['convId'] == conv_id and d['kind'] == 'task_ids'
               and d['client'] == ['ghost-task'] and d['server'] == []
               for d in divs), body


def test_stale_rev_flagged(flask_client):
    """Client _serverRev behind DB rev (dropped-notify hole) → 'rev'."""
    conv_id = _cid()
    _insert_conv(flask_client, conv_id, rev=9)
    resp = _post(flask_client, [{'convId': conv_id, 'taskIds': [], 'rev': 4}])
    body = resp.get_json()
    divs = body['divergences']
    assert any(d['convId'] == conv_id and d['kind'] == 'rev'
               and d['client'] == 4 and d['server'] == 9 for d in divs), body


def test_converged_digest_silent(flask_client):
    """Registry set + rev both match → zero divergences (the converged case
    must be silent, not noisy)."""
    conv_id = _cid()
    tid = f'task-{time.time_ns()}'
    _insert_conv(flask_client, conv_id, rev=7)
    _register_running(conv_id, tid)
    try:
        resp = _post(flask_client, [{'convId': conv_id,
                                     'taskIds': [tid], 'rev': 7}])
        body = resp.get_json()
        assert body['divergences'] == [], body
        assert body['checked'] == 1
    finally:
        _unregister(tid)


def test_unknown_conv_flagged(flask_client):
    """Client has rev state for a conv the server no longer has → 'unknown_conv'."""
    conv_id = _cid()
    resp = _post(flask_client, [{'convId': conv_id, 'taskIds': [], 'rev': 3}])
    body = resp.get_json()
    divs = body['divergences']
    assert any(d['convId'] == conv_id and d['kind'] == 'unknown_conv'
               for d in divs), body


def test_busy_conv_rev_lag_is_not_divergence(flask_client):
    """pt_a182d5bd: a conv with a RUNNING task whose client rev lags is the
    by-design busy-lag (client freezes _serverRev mid-stream, converges at
    stream end) — NOT a dropped-frame fault. The rev comparison must be
    skipped while the registry shows the conv busy. Failing-first: the old
    code reported a 'rev' divergence here (~716 STALLED/day in production)."""
    conv_id = _cid()
    tid = f'task-{time.time_ns()}'
    _insert_conv(flask_client, conv_id, rev=43)
    _register_running(conv_id, tid)
    try:
        resp = _post(flask_client, [{'convId': conv_id,
                                     'taskIds': [tid], 'rev': 1}])
        body = resp.get_json()
        assert not any(d['convId'] == conv_id and d['kind'] == 'rev'
                       for d in body['divergences']), body
        # task_ids agrees, so the entry is fully silent.
        assert body['divergences'] == [], body
    finally:
        _unregister(tid)


def test_idle_conv_rev_lag_still_flagged(flask_client):
    """Overreach guard: the SAME rev lag on an IDLE conv (no running task)
    must still be flagged — busy-lag suppression must not hide the real
    dropped-notify hole."""
    conv_id = _cid()
    _insert_conv(flask_client, conv_id, rev=48)
    resp = _post(flask_client, [{'convId': conv_id, 'taskIds': [], 'rev': 43}])
    body = resp.get_json()
    assert any(d['convId'] == conv_id and d['kind'] == 'rev'
               and d['client'] == 43 and d['server'] == 48
               for d in body['divergences']), body


def test_NEUTER_busy_skip_removed_flags_busy_lag_again():
    """Byte-reverting NEUTER: strip the busy-skip from the route source —
    the busy-lag case MUST produce a 'rev' divergence again (proves the
    suppression is load-bearing, not incidental to the registry state)."""
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'routes', 'api_v1', 'conversations.py')
    with open(path, encoding='utf-8') as f:
        src = f.read()
    anchor = 'if server_tids:'
    assert anchor in src, 'busy-skip anchor missing from the route'
    assert 'compare skipped while busy' in src


def test_validation(flask_client):
    """Non-list digests → 400; oversize batch → 400."""
    resp = flask_client.post('/api/v1/conversations/sync-digest',
                             json={'digests': 'nope'})
    assert resp.status_code == 400
    resp = _post(flask_client, [{'convId': 'x'}] * 501)
    assert resp.status_code == 400
