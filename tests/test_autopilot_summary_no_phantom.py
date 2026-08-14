"""tests/test_autopilot_summary_no_phantom.py — the summarize carrier must
never leak into the active-task registry as a phantom ``status='running'`` task.

ROOT CAUSE (the bug this guards)
--------------------------------
``autopilot.summarize_run`` reused ``create_task`` purely as a message
container for a SYNCHRONOUS reporter sub-turn. ``create_task`` marks a task
``status='running'`` AND registers it in ``_conv_latest_task[conv_id]``, but the
carrier is NEVER spawned and NEVER finalized — so it lingered forever
(``cleanup_old_tasks`` only evicts done/error/aborted). ``GET /api/chat/active``
then reported a phantom running task for that conv, and the frontend
orphan-recovery (``initActiveTasks`` Case C / cross-tab reconcile) birthed a
permanently-stuck "Waiting…" placeholder whose SSE never completes.

THE FIX (two prongs, both asserted here)
----------------------------------------
A. ``summarize_run`` creates the carrier with ``conv_id=''`` (matching the
   VU/reporter convention) so it never claims ``_conv_latest_task``; and calls
   ``discard_task`` in a ``finally`` so it's popped from the registry the instant
   the synchronous reporter returns.
B. ``GET /api/chat/active`` filters out ``_inline_messages`` / ``_vu_subtask``
   carrier tasks — these never stream, so reconnect must never see them.

SOURCE-LEVEL NEGATIVE CONTROLS (documented; run by hand to prove load-bearing)
  • Revert (B): drop the ``if not t.get('_inline_messages') ...`` filter in
    ``routes/chat.py::chat_active`` → ``test_active_excludes_inline_carrier``
    FAILS (the carrier shows up).
  • Revert (A): change the carrier back to ``create_task(conv_id, ...)`` AND
    delete the ``discard_task`` call → ``test_summarize_leaves_no_phantom``
    FAILS (``_conv_latest_task`` keeps the conv → carrier and a stray running
    task lingers).
"""

import pytest


# ── Fix B: /api/chat/active hides carrier/holder tasks ─────────────────

@pytest.fixture()
def put_task():
    """Insert a synthetic task into the in-memory registry; auto-cleanup."""
    from lib.tasks_pkg import tasks, tasks_lock
    added = []

    def _put(task):
        with tasks_lock:
            tasks[task['id']] = task
        added.append(task['id'])
        return task['id']

    yield _put

    with tasks_lock:
        for tid in added:
            tasks.pop(tid, None)


@pytest.mark.api
def test_active_excludes_inline_carrier(flask_client, put_task):
    """A ``_inline_messages`` carrier must NOT appear in /api/chat/active.

    NEGATIVE CONTROL for fix B: with the filter reverted this carrier appears
    and the frontend orphan-recovery would spawn a stuck placeholder.
    """
    put_task({'id': 'carrier-inline-1', 'convId': 'conv-phantom-1',
              'status': 'running', '_inline_messages': True})
    put_task({'id': 'vu-sub-1', 'convId': 'conv-phantom-1',
              'status': 'running', '_vu_subtask': True})
    # A real streaming task for the SAME conv MUST still be reported.
    put_task({'id': 'real-stream-1', 'convId': 'conv-phantom-1',
              'status': 'running'})

    resp = flask_client.get('/api/v1/chat/active')
    assert resp.status_code == 200
    ids = {t['id'] for t in (resp.get_json() or {}).get('items') or []}
    assert 'real-stream-1' in ids, 'real streaming task must remain reconnectable'
    assert 'carrier-inline-1' not in ids, 'inline-messages carrier leaked into /active'
    assert 'vu-sub-1' not in ids, 'VU sub-task leaked into /active'


@pytest.mark.api
def test_active_keeps_autopilot_kick_carrier(flask_client, put_task):
    """The autopilot-KICK carrier (``_autopilot_kick``, a REAL streaming task)
    is NOT an inline/vu holder, so it must still be reported + reconnectable."""
    put_task({'id': 'kick-carrier-1', 'convId': 'conv-kick-1',
              'status': 'running', '_autopilot_kick': True})
    resp = flask_client.get('/api/v1/chat/active')
    assert resp.status_code == 200
    ids = {t['id'] for t in (resp.get_json() or {}).get('items') or []}
    assert 'kick-carrier-1' in ids, 'autopilot-kick carrier must stay reconnectable'


@pytest.mark.api
def test_sharded_active_merges_only_durable_reconnectable_tasks(
        flask_client, monkeypatch):
    """Any replica can discover a direct browser task and its ingress key.

    Hidden/background carriers do not carry ``reconnectable`` and therefore
    must not leak into the plain connectToTask recovery view.
    """
    import json
    import routes.chat as chat_mod

    class _DB:
        def execute(self, _sql):
            return self

        def fetchall(self):
            return [
                {'task_id': 'remote-ui-1', 'conv_id': 'remote-conv',
                 'status': 'running', 'metadata': json.dumps({
                     'reconnectable': True, 'affinityKey': 'aff-remote'})},
                {'task_id': 'remote-carrier-1', 'conv_id': 'remote-conv',
                 'status': 'running', 'metadata': json.dumps({
                     'affinityKey': 'aff-remote'})},
            ]

    def _db_for_test(_domain):
        return _DB()

    def _no_auth():
        return None

    monkeypatch.setenv('TOFU_RUNTIME_STATE_BACKEND', 'redis')
    monkeypatch.setattr(chat_mod, 'get_thread_db', _db_for_test)
    monkeypatch.setattr(chat_mod, 'current_auth', _no_auth)

    resp = flask_client.get('/api/v1/chat/active')
    items = (resp.get_json() or {}).get('items') or []
    remote = {item['id']: item for item in items if item['id'].startswith('remote-')}
    assert remote['remote-ui-1']['affinityKey'] == 'aff-remote'
    assert 'remote-carrier-1' not in remote



@pytest.mark.api
def test_active_excludes_terminal_and_aborted_tasks(flask_client, put_task):
    """The reconnect view contains live work only.

    Terminal task dicts intentionally remain in the in-memory registry for a
    short poll/replay grace period.  Surfacing them through ``/chat/active``
    makes the frontend reconnect to an already-completed SSE and can turn the
    Send button back into Stop immediately after a successful turn.
    """
    put_task({'id': 'live-1', 'convId': 'conv-live', 'status': 'running'})
    put_task({'id': 'pending-1', 'convId': 'conv-pending', 'status': 'pending'})
    put_task({'id': 'done-1', 'convId': 'conv-done', 'status': 'done'})
    put_task({'id': 'error-1', 'convId': 'conv-error', 'status': 'error'})
    put_task({'id': 'aborted-1', 'convId': 'conv-aborted',
              'status': 'running', 'aborted': True})

    resp = flask_client.get('/api/v1/chat/active')
    assert resp.status_code == 200
    ids = {t['id'] for t in (resp.get_json() or {}).get('items') or []}
    assert {'live-1', 'pending-1'} <= ids
    assert {'done-1', 'error-1', 'aborted-1'}.isdisjoint(ids)


# ── discard_task: carrier removed from registry + conv-latest index ────

def test_discard_task_pops_registry_and_latest_index():
    from lib.tasks_pkg import create_task, discard_task, tasks, tasks_lock
    from lib.tasks_pkg.manager import _conv_latest_task, _conv_latest_task_lock

    task = create_task('conv-discard-1', [{'role': 'user', 'content': 'hi'}], {})
    tid = task['id']
    # create_task registered it both places.
    with tasks_lock:
        assert tid in tasks
    with _conv_latest_task_lock:
        assert _conv_latest_task.get('conv-discard-1') == tid

    discard_task(tid, 'conv-discard-1')

    with tasks_lock:
        assert tid not in tasks, 'discard_task must pop the registry entry'
    with _conv_latest_task_lock:
        assert _conv_latest_task.get('conv-discard-1') != tid, \
            'discard_task must clear the conv-latest index entry it claimed'


def test_discard_task_preserves_other_convs_latest():
    """discard_task must only clear the index entry that points at THIS task."""
    from lib.tasks_pkg import create_task, discard_task
    from lib.tasks_pkg.manager import _conv_latest_task, _conv_latest_task_lock

    keep = create_task('conv-keep-1', [{'role': 'user', 'content': 'a'}], {})
    drop = create_task('conv-drop-1', [{'role': 'user', 'content': 'b'}], {})
    discard_task(drop['id'], 'conv-drop-1')
    with _conv_latest_task_lock:
        assert _conv_latest_task.get('conv-keep-1') == keep['id'], \
            'discard_task wrongly touched an unrelated conv'
    # cleanup
    discard_task(keep['id'], 'conv-keep-1')


# NOTE: the summarize-carrier phantom test was removed with the autopilot
# summary REPORT layer (the on-demand summarize_run path is gone). The generic
# carrier hygiene it also exercised — /api/chat/active hides _inline_messages /
# _vu_subtask carriers, and discard_task pops the registry + conv-latest index —
# stays covered by the tests above (still used by the VU reporter-less paths).
