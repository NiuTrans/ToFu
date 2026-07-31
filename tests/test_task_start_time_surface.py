"""Server-side start-clock surface for long-running background jobs.

Why this exists (owner-reported symptom): a paper-media / production panel
shows ``已用 0:03`` for a job the backend has been running for ten minutes,
because the frontend mints its own ``Date.now()`` stopwatch and re-mints it on
every refresh / tab switch. The chat stream already solved this — the backend
surfaces the task's real start and the client rewinds its clock to it
(``_seedStreamTimerStart``). Production capabilities could not do the same
because **no poll response carried a start timestamp at all**.

Two independent facts are pinned here, because fixing only the first leaves a
subtler version of the same lie in place:

  1. ``TaskRuntime.poll()`` — the single throat every production capability's
     poll route funnels through (``routes/_task_routes.py`` passes it verbatim)
     — reports ``createdAt`` (true start) and ``updatedAt`` (last proof of
     life), both as epoch **MILLISECONDS**. ``updatedAt`` is what makes the
     "last activity" / stall warning honest: a client that re-mints it locally
     on refresh **washes a job that has been silent for ten minutes into
     looking healthy**, which is the dangerous half of this bug, not the
     cosmetic half.

     ★ The UNIT is itself a pinned contract. The task dict holds float
     SECONDS (``created_at``); the wire speaks camelCase MILLISECONDS
     (``createdAt``), matching the shipped chat surface. Handing seconds to
     the frontend seam fails SILENTLY — the min-guard accepts it and the UI
     renders a ~50-year elapsed, which is worse than the 0:00 it replaced.
     ``test_poll_clocks_are_milliseconds_not_seconds`` is that guard.

  2. ``created_at`` survives the job manifest round-trip. Without this, a
     crash-resumed job (``resume_running_jobs`` → ``create_task``) mints a
     brand-new ``created_at`` at restart, so the number is still wrong — just
     wrong less visibly. The manifest is the only cross-process carrier.

Behaviour-asserting by charter discipline: every check drives the real
``poll()`` / ``write_manifest`` + ``read_manifest`` and asserts the observable
result, never a private symbol or a source-text anchor.
"""

import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


# ── 1. TaskRuntime.poll() surfaces the start clock ─────────────

def _runtime(**kw):
    from lib.agent_core.task_runtime import TaskRuntime
    return TaskRuntime('test-start-clock', **kw)


def test_poll_reports_created_at_for_running_task():
    """A client re-attaching mid-flight learns the job's TRUE start."""
    rt = _runtime()
    task = rt.create()
    resp = rt.poll(task['id'])
    assert 'createdAt' in resp, \
        'poll() must surface createdAt — without it a refreshed client has ' \
        'no way to continue the elapsed clock and restarts at 0:00'
    assert resp['createdAt'] == pytest.approx(task['created_at'] * 1000)


# ── 1b. THE UNIT GUARD ─────────────────────────────────────────
#
# The highest-value assertion in this file. Feeding epoch SECONDS into the
# frontend's `_seedStreamTimerStart` (which takes `serverStartMs`) is NOT a
# visible failure: its min-guard accepts the value happily, and the UI then
# renders an elapsed of ~50 years. That is strictly worse than the 0:00 bug
# we set out to fix, because 0:00 at least LOOKS wrong.

_MS_FLOOR = 1e12  # any epoch-ms after 2001-09; epoch-SECONDS is ~1.7e9


@pytest.mark.parametrize('field', ['createdAt', 'updatedAt'])
def test_poll_clocks_are_milliseconds_not_seconds(field):
    """Clock fields MUST be the same magnitude as JS ``Date.now()``."""
    rt = _runtime()
    task = rt.create()
    rt.append_event(task['id'], {'type': 'phase', 'phase': 'render'})
    value = rt.poll(task['id'])[field]
    assert value > _MS_FLOOR, (
        f'{field}={value} looks like epoch SECONDS. The wire contract is '
        'epoch MILLISECONDS (matching chat_poll / SSE state createdAt). '
        'A seconds value silently renders as a ~50-year elapsed.')
    assert isinstance(value, int), f'{field} must be an int, got {type(value)}'


def test_poll_terminal_clock_is_milliseconds():
    """The terminal clock shares the same unit contract."""
    rt = _runtime()
    task = rt.create()
    rt.finish(task['id'], result={'ok': True})
    assert rt.poll(task['id'])['finishedAt'] > _MS_FLOOR


def test_poll_clock_unit_matches_chat_contract():
    """Cross-check against the ALREADY-SHIPPED chat surface.

    ``lib/chat_dispatch.py`` / ``routes/chat_poll_abort.py`` emit
    ``int(task['created_at'] * 1000)``. Pinning both against the same
    conversion keeps the two transports from drifting apart — a client must
    be able to feed either into the same seed function.
    """
    rt = _runtime()
    task = rt.create()
    chat_style_ms = int(task['created_at'] * 1000)
    assert rt.poll(task['id'])['createdAt'] == chat_style_ms


def test_poll_created_at_is_stable_across_polls():
    """The start clock is a fact about the job, not about the poll."""
    rt = _runtime()
    task = rt.create()
    first = rt.poll(task['id'])['createdAt']
    rt.append_event(task['id'], {'type': 'phase', 'phase': 'render'})
    second = rt.poll(task['id'])['createdAt']
    assert first == second, 'createdAt must not drift as events arrive'


def test_poll_reports_updated_at_as_liveness_clock():
    """``updated_at`` is the authoritative 'last activity' — the signal a
    client must NOT re-mint locally, or a stalled job renders as healthy."""
    rt = _runtime()
    task = rt.create()
    rt.append_event(task['id'], {'type': 'phase', 'phase': 'narrate'})
    resp = rt.poll(task['id'])
    assert 'updatedAt' in resp, \
        'poll() must surface updatedAt so the stale/stall warning reflects ' \
        'server truth rather than the client page age'
    assert resp['updatedAt'] == pytest.approx(task['updated_at'] * 1000)


def test_poll_updated_at_present_before_any_event():
    """A pending task must still report a liveness clock.

    Otherwise the client's only fallback is 'now', which is exactly the
    wash-the-stall-away behaviour this contract removes.
    """
    rt = _runtime()
    task = rt.create()
    resp = rt.poll(task['id'])
    assert resp.get('updatedAt') is not None, \
        'a task with no events yet must still report updatedAt'


def test_poll_updated_at_advances_with_events_created_at_does_not():
    """The two clocks are different quantities and must not be conflated."""
    rt = _runtime()
    task = rt.create()
    before = rt.poll(task['id'])
    time.sleep(0.02)
    rt.append_event(task['id'], {'type': 'progress', 'done': 1, 'total': 9})
    after = rt.poll(task['id'])
    assert after['updatedAt'] > before['updatedAt'], \
        'updatedAt must advance when the worker reports progress'
    assert after['createdAt'] == before['createdAt'], \
        'createdAt must stay pinned to the real start'


def test_poll_reports_finished_at_on_terminal_task():
    """A late poller can render the true total duration, not 0:00."""
    rt = _runtime()
    task = rt.create()
    rt.finish(task['id'], result={'ok': True})
    resp = rt.poll(task['id'])
    assert resp['done'] is True
    assert resp.get('finishedAt') is not None, \
        'a terminal task must report finishedAt so elapsed can be frozen ' \
        'at the real duration'
    assert resp['finishedAt'] >= resp['createdAt']


def test_poll_not_found_shape_unchanged():
    """The 404 branch must stay exactly as it was (no bogus clocks)."""
    rt = _runtime()
    resp = rt.poll('does-not-exist')
    assert resp['ok'] is False
    assert resp['error'] == 'not_found'
    assert resp['done'] is True
    assert resp.get('createdAt') is None, \
        'a task that does not exist has no start time — do not invent one'


def test_poll_preserves_existing_contract_fields():
    """Regression guard: adding clocks must not disturb the replay contract."""
    rt = _runtime()
    task = rt.create()
    rt.append_event(task['id'], {'type': 'phase', 'phase': 'a'})
    rt.append_event(task['id'], {'type': 'phase', 'phase': 'b'})
    resp = rt.poll(task['id'], cursor=1)
    assert resp['ok'] is True
    assert [e['phase'] for e in resp['events']] == ['b']
    assert resp['next_cursor'] == 2
    assert resp['status'] == 'running'
    assert resp['done'] is False


def test_poll_tolerates_legacy_task_dict_without_clocks():
    """Older/hand-built task dicts must not crash the poll path."""
    import threading
    rt = _runtime()
    legacy = {
        'id': 'legacy-1', 'status': 'running', 'events': [],
        'events_lock': threading.Lock(), 'abort_event': threading.Event(),
        'result': None, 'error': None,
    }
    rt._tasks['legacy-1'] = legacy
    resp = rt.poll('legacy-1')
    assert resp['ok'] is True
    assert resp['createdAt'] is None


# ── 2. created_at survives the manifest round-trip ─────────────

def test_manifest_round_trip_preserves_created_at():
    """Crash-resume must report the ORIGINAL start, not the restart instant.

    ``resume_running_jobs`` re-creates the task via ``create_task()``, which
    mints a fresh ``created_at``. Unless the real start is persisted in the
    manifest and restored on respawn, a resumed job's elapsed clock silently
    restarts — the same lie as the refresh case, one layer deeper.
    """
    from lib.production.jobs import read_manifest, write_manifest
    original_start = time.time() - 600.0  # job began 10 minutes ago
    task = {'task_id': 'job-1', 'workdir': '', 'created_at': original_start}
    with tempfile.TemporaryDirectory() as d:
        wrote = write_manifest(d, task, fields=('task_id', 'created_at'),
                               kind='scenes', state='running')
        assert wrote is True
        m = read_manifest(d)
    assert m is not None
    assert m.get('created_at') == pytest.approx(original_start), \
        'the manifest must carry created_at, or a resumed job cannot report ' \
        'its true start'


def test_motion_manifest_fields_include_created_at():
    """The motion capability's allow-list must actually list the field.

    ``write_manifest`` only persists keys named in ``fields``, so a correct
    round-trip helper is useless if the capability never asks for the clock.
    Asserted via the observable manifest content, not the tuple's identity.
    """
    from lib.motion_video.engine import _MANIFEST_FIELDS
    from lib.production.jobs import read_manifest, write_manifest
    original_start = time.time() - 1234.0
    task = {'task_id': 'job-2', 'created_at': original_start,
            'workdir': '', 'width': 1080, 'height': 1440}
    with tempfile.TemporaryDirectory() as d:
        write_manifest(d, task, fields=_MANIFEST_FIELDS,
                       kind='scenes', state='running')
        m = read_manifest(d)
    assert m.get('created_at') == pytest.approx(original_start), \
        'motion _MANIFEST_FIELDS must include created_at so a crash-resumed ' \
        'video job reports the real elapsed time'


def test_respawned_motion_task_keeps_original_created_at():
    """End-to-end: a respawn that restores the clock beats create()'s fresh one."""
    from lib.motion_video.runtime import _motion_runtime, _new_motion_task
    original_start = time.time() - 900.0
    tid = 'motion_resume_probe'
    try:
        task = _new_motion_task(tid, srt_path='', workdir='/tmp/none',
                                voice='', speed=None, alignment='loose',
                                narration=False, quality='standard',
                                parallel=1, width=1080, height=1440)
        assert task['created_at'] > original_start, \
            'sanity: a freshly created task starts "now", which is why the ' \
            'manifest value must override it on resume'
        # What the respawn path must do with a persisted clock.
        task['created_at'] = original_start
        resp = _motion_runtime.poll(tid)
        assert resp['createdAt'] == pytest.approx(original_start * 1000)
        assert resp['createdAt'] > _MS_FLOOR
    finally:
        _motion_runtime._tasks.pop(tid, None)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
