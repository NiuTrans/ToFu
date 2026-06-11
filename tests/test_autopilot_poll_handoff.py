"""tests/test_autopilot_poll_handoff.py — Autopilot follow-up baton on the poll path.

Regression coverage for the autopilot state-desync fix: the follow-up
handoff (``autopilotNextTaskId`` + ``autopilotVuMessage``) used to ride
ONLY on the SSE ``done`` event, so a client that fell back to
``/api/v1/chat/poll`` (SSE stripped / timed out) finalized the stream
WITHOUT the handoff and stranded the already-spawned successor task —
the sidebar dot stopped, the send box reverted from pause→send, and
translations fired while the backend was still running.

The fix makes the task dict the single source of truth:
  • ``orchestrator`` stashes ``task['_autopilot_followup'] = ap_result``;
  • ``chat_poll`` (routes/chat.py) surfaces it AND reports
    ``status='running'`` while ``task['_autopilot_deciding']`` is set (the
    VU LLM call window, during which status is already 'done' but the
    baton isn't stamped yet).

These tests inject synthetic tasks straight into the in-memory ``tasks``
registry and assert the poll route's JSON, with no live LLM / orchestrator.
They run against the real ``server.app`` (via the conftest ``flask_client``
fixture, open auth mode) so the ``ui_chat_poll`` endpoint is wired exactly
as in production.
"""

import pytest


_VU_MSG = {
    'role': 'user',
    'content': 'Yes, wire the breaker state into the API.',
    '_msgId': 'vu-msg-id-1',
    '_isVirtualUser': True,
}


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


def _poll(client, task_id):
    resp = client.get(f'/api/v1/chat/poll/{task_id}')
    return resp.status_code, resp.get_json()


@pytest.mark.api
def test_baton_surfaced_when_followup_spawned(flask_client, put_task):
    """A done autopilot task with _autopilot_followup → poll carries the baton."""
    put_task({
        'id': 'autopilot-done-1', 'status': 'done',
        'content': 'Sure, I will add a public method.', 'thinking': '',
        'finishReason': 'stop',
        '_autopilot_followup': {
            'next_task_id': 'next-task-aaaa', 'vu_msg': _VU_MSG,
        },
    })
    status, body = _poll(flask_client, 'autopilot-done-1')
    assert status == 200, body
    assert body['status'] == 'done'
    assert body['autopilotNextTaskId'] == 'next-task-aaaa'
    assert body['autopilotVuMessage'] == _VU_MSG


@pytest.mark.api
def test_status_gated_while_deciding(flask_client, put_task):
    """status='done' but _autopilot_deciding set → poll reports 'running'.

    Guards the race where the VU LLM call (multi-second) runs after status
    was flipped to 'done' but before the baton is stamped — a poll in that
    window must NOT finalize the stream.
    """
    put_task({
        'id': 'autopilot-deciding-1', 'status': 'done',
        'content': 'partial', 'thinking': '',
        '_autopilot_deciding': True,
        # No _autopilot_followup yet — VU is still thinking.
    })
    status, body = _poll(flask_client, 'autopilot-deciding-1')
    assert status == 200, body
    assert body['status'] == 'running'
    assert 'autopilotNextTaskId' not in body


@pytest.mark.api
def test_normal_done_task_unaffected(flask_client, put_task):
    """A plain done task (no autopilot) → no baton keys, status stays done."""
    put_task({
        'id': 'plain-done-1', 'status': 'done',
        'content': 'Done.', 'thinking': '', 'finishReason': 'stop',
    })
    status, body = _poll(flask_client, 'plain-done-1')
    assert status == 200, body
    assert body['status'] == 'done'
    assert 'autopilotNextTaskId' not in body
    assert 'autopilotVuMessage' not in body


@pytest.mark.api
def test_deciding_flag_cleared_does_not_gate(flask_client, put_task):
    """Once deciding is False, a done task finalizes normally with the baton."""
    put_task({
        'id': 'autopilot-done-2', 'status': 'done',
        'content': 'ok', 'thinking': '', 'finishReason': 'stop',
        '_autopilot_deciding': False,
        '_autopilot_followup': {
            'next_task_id': 'next-task-bbbb', 'vu_msg': _VU_MSG,
        },
    })
    status, body = _poll(flask_client, 'autopilot-done-2')
    assert status == 200, body
    assert body['status'] == 'done'
    assert body['autopilotNextTaskId'] == 'next-task-bbbb'


# ── Live SSE-generator path ────────────────────────────────────────────
# These exercise the ACTUAL root cause: the SSE generator in routes/chat.py
# synthesizes a late `done` event whenever `task['status'] != 'running'`.
# Because status flips to 'done' BEFORE the multi-second autopilot VU call
# (orchestrator), a fresh SSE connection landing in that window used to emit
# a baton-less synthetic done and close the stream — the conv went idle
# (sidebar dot off / pause→send / translation fires) until a manual refresh.
# The fix gates all three synthesis sites on `_task_terminal()`, which is
# false while `_autopilot_deciding` is set.

def _make_full_task(task_id, **overrides):
    """Build a fully-formed in-memory chat task (events_lock etc.)."""
    from lib.tasks_pkg.manager import create_task
    task = create_task('cv-sse-' + task_id, [{'role': 'user', 'content': 'q'}], {})
    task['id'] = task_id  # deterministic id for the URL
    task.update(overrides)
    return task


def _sse_collect(client, task_id, max_chars=20000):
    """Open the SSE stream and return the raw body text (bounded)."""
    resp = client.get(f'/api/chat/stream/{task_id}')
    text = resp.get_data(as_text=True)
    return text[:max_chars]


@pytest.mark.api
def test_sse_holds_open_while_deciding_then_delivers_baton(flask_client, put_task):
    """The end-to-end live-path regression.

    Reproduces the real sequence: status flips to 'done' BEFORE the VU call,
    so a fresh SSE connection lands while `_autopilot_deciding` is set. The
    generator must NOT synthesize a premature (baton-less) done; it must hold
    the stream open until the autopilot hook appends the REAL done event with
    the handoff, then deliver that verbatim.

    A background timer plays the role of the autopilot hook: after a short
    delay it clears the decision marker and appends the baton-carrying done
    event. We assert the stream's first `done` is the real one (with the
    baton) — never a synthetic one emitted during the decision window.
    """
    import threading
    from lib.tasks_pkg.manager import append_event

    task = _make_full_task('sse-deciding-1', status='done',
                           content='partial', _autopilot_deciding=True)
    put_task(task)

    def _release():
        # Mimic maybe_run_autopilot finishing: stamp baton, clear marker,
        # append the real done event (which ends the SSE loop).
        task['_autopilot_followup'] = {
            'next_task_id': 'next-task-cccc', 'vu_msg': _VU_MSG,
        }
        task['_autopilot_deciding'] = False
        append_event(task, {
            'type': 'done', 'finishReason': 'stop',
            'autopilotNextTaskId': 'next-task-cccc',
            'autopilotVuMessage': _VU_MSG,
        })

    timer = threading.Timer(0.6, _release)
    timer.start()
    try:
        body = _sse_collect(flask_client, 'sse-deciding-1', max_chars=20000)
    finally:
        timer.cancel()

    assert '"type": "state"' in body or '"type":"state"' in body, body[:400]
    # The ONLY done in the stream must be the real one carrying the baton.
    assert '"type": "done"' in body or '"type":"done"' in body, body[:800]
    assert 'next-task-cccc' in body, \
        f'baton missing — stream likely synthesized a premature done: {body[:800]}'
    # Belt-and-braces: the done must come AFTER the state snapshot, proving the
    # stream stayed open through the decision window rather than closing early.
    _state_pos = max(body.find('"type": "state"'), body.find('"type":"state"'))
    _done_pos = max(body.find('"type": "done"'), body.find('"type":"done"'))
    assert _state_pos >= 0 and _done_pos > _state_pos, \
        f'done did not follow state snapshot: state@{_state_pos} done@{_done_pos}'


@pytest.mark.api
def test_sse_normal_done_task_closes_promptly(flask_client, put_task):
    """A plain done task (no autopilot) still gets its synthetic done from the
    snapshot branch — the gate must not keep normal streams open."""
    task = _make_full_task('sse-plain-done-1', status='done',
                           content='done', finishReason='stop')
    put_task(task)
    body = _sse_collect(flask_client, 'sse-plain-done-1', max_chars=20000)
    assert '"type": "done"' in body or '"type":"done"' in body, body[:600]
    assert 'autopilotNextTaskId' not in body
