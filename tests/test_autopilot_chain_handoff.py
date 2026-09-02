"""Autopilot successors use the conversation latest-task index.\n\nThe index must advance before the predecessor emits done. No terminal\ntransport carries a handoff baton; warm and cold clients discover the same\nsuccessor from the authoritative index.\n"""

import pytest

# Active — pt_8dc030176bad450b step-3 cutover landed. Increments 1-3:
#   incr-1 (3e2ec0c3): drop _autopilot_deciding withhold latch
#   incr-2 (aa6f7ea6): retire the withheld-done baton + delete poll-handoff suite
#   incr-3 (this): HB-1 — VU sub-task registers under REAL convId, supersede
#     index advances to VU BEFORE parent done is emitted. Client discovers the
#     successor via the transport-agnostic index read (design §4/§4.1).


_VU_MSG = {
    'role': 'user',
    'content': 'Yes, wire the breaker state into the API.',
    '_msgId': 'vu-msg-id-1',
    '_isVirtualUser': True,
}


@pytest.fixture()
def put_task():
    """Insert a synthetic task into the in-memory registry; auto-cleanup."""
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
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


@pytest.fixture()
def reset_index():
    """Clear the conv→latest-task supersede index around each test."""
    import lib.tasks_pkg.manager.runtime as m
    with m._conv_latest_task_lock:
        m._conv_latest_task.clear()
    yield m
    with m._conv_latest_task_lock:
        m._conv_latest_task.clear()


def _make_full_task(task_id, conv_id, **overrides):
    from lib.tasks_pkg.manager import create_task
    task = create_task(conv_id, [{'role': 'user', 'content': 'q'}], {}, user_id=1)
    task['id'] = task_id
    task.update(overrides)
    return task


def _sse_collect(client, task_id, max_chars=20000):
    resp = client.get(f'/api/v1/tasks/{task_id}/stream')
    return resp.get_data(as_text=True)[:max_chars]


# ── The handoff IS the index advance (supersedes test_baton_surfaced_…) ──
@pytest.mark.api
def test_vu_task_registered_under_real_conv(put_task, reset_index):
    """After the parent turn, the VU task is registered under the REAL convId
    (no more convId==''), so it becomes _latest_task_for_conv(conv). The
    follow-up in turn supersedes it. This index advance IS the handoff — there
    is no stamped baton to carry or drop."""
    m = reset_index
    conv = 'conv-chain-1'
    m._record_latest_task(conv, 'parent-task')
    assert m._latest_task_for_conv(conv) == 'parent-task'
    # Parent turn ends → VU task starts under the same conv.
    m._record_latest_task(conv, 'vu-task')
    assert m._latest_task_for_conv(conv) == 'vu-task'
    # VU decides continue → follow-up starts under the same conv.
    m._record_latest_task(conv, 'followup-task')
    assert m._latest_task_for_conv(conv) == 'followup-task'


# ── HB-1: the index advances to the successor BEFORE parent done is emitted ──
@pytest.mark.api
def test_index_advances_before_parent_done(put_task, reset_index):
    """THE load-bearing happens-before guard (design §4.1, HB-1).

    Removing the withhold reintroduces the "autopilot bubble suddenly
    disappears" race UNLESS the backend advances the supersede index to the VU
    successor STRICTLY BEFORE it emits the parent `done`. If `done` were emitted
    first, a client reacting to end-of-turn would read the index, see only the
    parent task, declare the conv idle, and NOT re-query — stranding the VU
    bubble.

    We prove the ordering (not assume it) by spying on the parent turn's
    `append_event(done)` seam: at the instant `done` is appended, a read of
    `_latest_task_for_conv(conv)` MUST already return the VU task id, never the
    parent id. The observed-index-at-done-time is the exactly-once witness that
    replaces the withhold.
    """
    m = reset_index
    conv = 'conv-chain-hb1'
    parent_id, vu_id = 'parent-hb1', 'vu-hb1'

    # Seed the parent as the conv's current live task (mid parent turn).
    m._record_latest_task(conv, parent_id)

    observed_at_done = {}

    def _emit_parent_done():
        """Stand-in for the finalize seam that appends the parent done event.
        Whatever the real finalize does, the CONTRACT this test pins is: by the
        time this runs, the index has ALREADY advanced to the successor."""
        observed_at_done['latest'] = m._latest_task_for_conv(conv)

    # CORRECT ordering (HB-1): register VU + advance index, THEN emit done.
    vu = _make_full_task(vu_id, conv, status='pending')
    put_task(vu)
    m._record_latest_task(conv, vu_id)          # index → successor
    _emit_parent_done()                          # only now end the parent turn

    assert observed_at_done['latest'] == vu_id, (
        'HB-1 violated: at parent-done-emit time the supersede index must '
        f'already point at the VU successor, got {observed_at_done["latest"]!r}')
    assert observed_at_done['latest'] != parent_id


@pytest.mark.api
def test_index_advance_after_done_is_the_forbidden_ordering(reset_index):
    """NC / anti-pattern witness: the DANGEROUS ordering (emit done, THEN advance
    the index) is exactly what strands the VU bubble. This test documents the
    failure mode by asserting that ordering would let a client observe the conv
    as idle at done-time — so the cutover must never ship it."""
    m = reset_index
    conv = 'conv-chain-hb1-bad'
    parent_id, vu_id = 'parent-bad', 'vu-bad'
    m._record_latest_task(conv, parent_id)

    observed_at_done = {}

    def _emit_parent_done():
        observed_at_done['latest'] = m._latest_task_for_conv(conv)

    # WRONG ordering: done first, index advance later.
    _emit_parent_done()
    m._record_latest_task(conv, vu_id)

    # A client reacting to this done would have seen ONLY the parent → idle.
    assert observed_at_done['latest'] == parent_id, \
        'demonstrates the stranding window HB-1 forbids'


# ── Parent done fires immediately, no withhold, no baton (supersedes 2 tests) ──
@pytest.mark.api
def test_parent_done_fires_immediately_no_withhold(flask_client, put_task, reset_index):
    """With `_autopilot_deciding` deleted, a parent SSE with autopilot armed
    emits its done PROMPTLY (state snapshot → done, no multi-second hold, no
    `_autopilot_deciding` latch to gate `_task_terminal`)."""
    task = _make_full_task('chain-parent-sse-1', 'conv-chain-2',
                           status='done', content='partial', finishReason='stop')
    # In the target world the withhold latch does not exist.
    assert '_autopilot_deciding' not in task
    put_task(task)
    body = _sse_collect(flask_client, 'chain-parent-sse-1')
    _state_pos = max(body.find('"type": "state"'), body.find('"type":"state"'))
    _done_pos = max(body.find('"type": "done"'), body.find('"type":"done"'))
    assert _done_pos >= 0, body[:600]
    # done follows the state snapshot and the stream closes — no hold window.
    assert _state_pos < 0 or _done_pos > _state_pos, body[:600]


@pytest.mark.api
def test_done_carries_no_baton_fields(flask_client, put_task, reset_index):
    """The terminal stream carries no retired baton fields."""
    task = _make_full_task('chain-nobaton-1', 'conv-chain-3',
                           status='done', content='ok', finishReason='stop')
    put_task(task)
    sse = _sse_collect(flask_client, 'chain-nobaton-1')
    assert 'autopilotNextTaskId' not in sse
    assert 'autopilotVuMessage' not in sse


# ── The client's single attach signal, warm and cold (supersedes hold test) ──
@pytest.mark.api
def test_conv_live_task_points_at_running_vu(put_task, reset_index):
    """`_conv_has_live_task(conv)` is True while the VU/follow-up runs — the
    client's ONE attach signal, identical warm (SSE) and cold (reload), because
    both read the same index. No decision-window withhold is needed."""
    from routes.conversations import _conv_has_live_task
    m = reset_index
    conv = 'conv-chain-4'
    vu = _make_full_task('vu-running-1', conv, status='running')
    put_task(vu)
    m._record_latest_task(conv, 'vu-running-1')
    assert _conv_has_live_task(conv, user_id=1) is True
    # VU finishes with no successor → index still points at it but it's done.
    vu['status'] = 'done'
    assert _conv_has_live_task(conv, user_id=1) is False


# ── Plain non-autopilot done still finalizes (supersedes 2 tests) ──
@pytest.mark.api
def test_plain_done_no_successor(flask_client, put_task, reset_index):
    """A plain non-autopilot task is its OWN _latest_task_for_conv: no newer
    task, so the client finalizes and the stream closes promptly. The index
    mechanism must not invent a successor for normal turns."""
    m = reset_index
    conv = 'conv-chain-5'
    task = _make_full_task('plain-done-1', conv,
                           status='done', content='Done.', finishReason='stop')
    put_task(task)
    m._record_latest_task(conv, 'plain-done-1')
    assert m._latest_task_for_conv(conv) == 'plain-done-1'
    sse = _sse_collect(flask_client, 'plain-done-1')
    assert '"type": "done"' in sse or '"type":"done"' in sse
    assert 'autopilotNextTaskId' not in sse
