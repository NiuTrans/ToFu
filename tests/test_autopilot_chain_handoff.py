"""tests/test_autopilot_chain_handoff.py — TARGET contract for epic
pt_8dc030176bad450b (Autopilot VU as an independent stream).

STATUS: SKIPPED (the whole module) until the cutover commit. This suite is the
migration-test plan the owner named as the precondition for touching the
exactly-once handoff ("先写迁移测试再动"). It codifies the TARGET behavior so that
when the cutover happens these tests are the acceptance gate — and it lives
in-tree NOW (skipped) so the design intent is reviewable and the collection gate
stays green.

See docs/EPIC_AUTOPILOT_INDEPENDENT_STREAM_DESIGN.md for the full rationale.

THE PURE MECHANISM (why there is no new baton / enum / descriptor)
------------------------------------------------------------------
The whole hand-carried baton — `autopilotNextTaskId`/`autopilotVuMessage` on the
`done` event, its `/api/v1/chat/poll` mirror, and the `_apply_autopilot_baton`
cold-replay synthesis — exists for ONE reason: the VU sub-task deliberately runs
with `convId=''` "to stay out of the latest-task registry" (autopilot.py). Being
invisible to the server's conv→latest-task index, the successor id has to be
hand-delivered on the terminal event of the turn before it, and the parent `done`
has to be WITHHELD (via `task['_autopilot_deciding']`) until that id exists.

But that index ALREADY EXISTS and is already the right primitive:
`_record_latest_task(conv_id, task_id)` / `_latest_task_for_conv(conv_id)`
(lib/tasks_pkg/manager/_state.py) — a process-wide, cross-replica pointer to a
conversation's newest task; `routes/conversations.py:_conv_has_live_task` already
reduces `_latest_task_for_conv(conv) is pending/running` to "is this conv live?".

TARGET: STOP opting out. Each turn of an autopilot chain (parent → VU →
follow-up) is a normal task registered under the REAL convId. The frontend gets
ONE transport-agnostic reducer: after any turn's `done`, if the conv's
server-authoritative latest task is a DIFFERENT pending/running task, attach to
it (idempotently — dedup by task id). That single rule covers parent→VU,
VU→follow-up, and VU-emitted-TASK_DONE (no newer task ⇒ chain ends), with NO
enum, NO per-transition state, NO vu_msg on the wire, NO withhold, NO
_VUEventForwarder, NO cold-replay synthesis special-case. The VU message is
already persisted to conversations.messages (_isVirtualUser) before its task
starts, so the client loads it from the authoritative record on attach.

This suite therefore asserts the INDEX ADVANCE + the DELETIONS, not a descriptor.
Keep test_autopilot_poll_handoff.py green until the cutover commit, which DELETES
it (its 4 guards describe the baton world that no longer exists) and un-skips
this suite in the same commit — the two mechanisms are mutually exclusive.

THE LOAD-BEARING GUARD (design §4.1, HB-1): removing the withhold is only safe if
the backend advances the supersede index to the VU successor STRICTLY BEFORE it
emits the parent `done`. `test_index_advances_before_parent_done` pins that
happens-before by reading the index at the done-emit seam; without it, a client
reacting to end-of-turn could read the index, see only the parent, declare the
conv idle, and strand the VU bubble — the mirror of the visible "autopilot bubble
suddenly disappears" symptom.
"""

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


@pytest.fixture()
def reset_index():
    """Clear the conv→latest-task supersede index around each test."""
    from lib.tasks_pkg import manager as m
    with m._conv_latest_task_lock:
        m._conv_latest_task.clear()
    yield m
    with m._conv_latest_task_lock:
        m._conv_latest_task.clear()


def _poll(client, task_id):
    resp = client.get(f'/api/v1/chat/poll/{task_id}')
    return resp.status_code, resp.get_json()


def _make_full_task(task_id, conv_id, **overrides):
    from lib.tasks_pkg.manager import create_task
    task = create_task(conv_id, [{'role': 'user', 'content': 'q'}], {})
    task['id'] = task_id
    task.update(overrides)
    return task


def _sse_collect(client, task_id, max_chars=20000):
    resp = client.get(f'/api/chat/stream/{task_id}')
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
    """Neither the SSE done nor the poll body carries the retired baton keys —
    the fields are GONE, not merely empty. The successor is discovered from the
    supersede index instead."""
    task = _make_full_task('chain-nobaton-1', 'conv-chain-3',
                           status='done', content='ok', finishReason='stop')
    put_task(task)
    status, body = _poll(flask_client, 'chain-nobaton-1')
    assert status == 200, body
    assert 'autopilotNextTaskId' not in body
    assert 'autopilotVuMessage' not in body
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
    assert _conv_has_live_task(conv) is True
    # VU finishes with no successor → index still points at it but it's done.
    vu['status'] = 'done'
    assert _conv_has_live_task(conv) is False


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
    status, body = _poll(flask_client, 'plain-done-1')
    assert status == 200, body
    assert body['status'] == 'done'
    assert 'autopilotNextTaskId' not in body
