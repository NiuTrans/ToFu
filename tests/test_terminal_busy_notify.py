"""tests/test_terminal_busy_notify.py — the busy channel's CLEAR must be
event-driven at the task's terminal seam (epic pt_3ea0e045).

Measured incident (2026-08-01, conv ms91b45tva0sym, task f3f224c9): the turn
settled at 09:20:43 — message committed fr=stop, persist status=done, the
client rendered the finish footer — yet the sidebar showed 回答中 and the
composer showed Stop for 103s+, and every click-open attached to the DEAD
task and replayed its done. Root cause: the busy projection's CLEAR signal
only rode the NEXT incidental conversation write (a checkpoint sync / client
PUT), and for the ~30s the ``_finalize_started_at`` latch was held even
those frames were forced to say busy. On a tab busy with other conversations
the designed poll fallback (``_crossDeviceReconcile``'s conv-state probe,
gated on ``activeStreams.size === 0``) starves, so the stale badge survived
until the next write landed (or F5).

The fix: ``notify_terminal_busy_state(task)`` (manager/_registry.py, one
shared helper next to the projection it feeds) is called from BOTH terminal
seams — the orchestrator's ``_finalize_and_emit_done`` AFTER the latch pop
(the autopilot hook has concluded by then, so a spawned VU carrier projects
itself as ``<tid>#vu`` and an ordinary settle projects IDLE) and the
endpoint loop's ``_finalize`` (which holds no latch).

Pinned here:
  1. Source-order contract (with an in-test NEUTER proving the ratchet is
     keyed on the ordering).
  2. The helper's wire contract (conv id / rev=None / owning user id; a
     convId-less task is a silent no-op).
  3. The projection truth the frame carries: done+no-latch ⇒ excluded
     (CLEAR), running ⇒ included, live VU carrier ⇒ ``#vu``-marked.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_terminal_busy_notify.py -v
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
FINALIZE_PATH = os.path.join(ROOT, 'lib', 'tasks_pkg', 'orchestrator', '_finalize.py')
ENDPOINT_PATH = os.path.join(ROOT, 'lib', 'tasks_pkg', 'endpoint', '_run.py')

_LATCH_POP = "task.pop('_finalize_started_at', None)"
_DONE_APPEND = 'append_event(task, done_evt)'
_NOTIFY_CALL = 'notify_terminal_busy_state(task)'


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


# ═════════════════════════════════════════════════════════════════════
#  1. Source-order contract
# ═════════════════════════════════════════════════════════════════════

def test_orchestrator_notify_after_latch_pop_and_done_append():
    """The frame must fire only once the projection is truthful: status
    terminal, hook concluded, latch popped, done event queued. Firing while
    the latch is still held would make the projection say BUSY for another
    ~30s (the latch is unconditional) and ship the very staleness being
    fixed."""
    src = _read(FINALIZE_PATH)
    done_pos = src.index(_DONE_APPEND)
    pop_pos = src.index(_LATCH_POP)
    notify_pos = src.index(_NOTIFY_CALL)
    assert pop_pos > done_pos, 'precondition: latch pop must follow the done append'
    assert notify_pos > pop_pos, (
        f'{FINALIZE_PATH}: notify_terminal_busy_state must run AFTER '
        f'task.pop(\'_finalize_started_at\', None) — the latch is what makes '
        f'conv_has_work_in_flight say busy for a terminal task, so a frame '
        f'emitted before the pop ships a forced-busy projection'
    )


def test_endpoint_notify_after_done_append():
    src = _read(ENDPOINT_PATH)
    assert src.index(_NOTIFY_CALL) > src.index(_DONE_APPEND), (
        f'{ENDPOINT_PATH}: the endpoint finalize (no latch) must emit the '
        f'busy frame after the done event is queued'
    )


def test_neuter_remove_notify_breaks_ratchet():
    """NEUTER: delete the call from string copies of both seams — the
    ratchets above must flip red, proving they are keyed on the emission
    and would catch a regression that drops it."""
    for path in (FINALIZE_PATH, ENDPOINT_PATH):
        src = _read(path)
        assert _NOTIFY_CALL in src, f'precondition: call missing in {path}'
        neutered = src.replace(_NOTIFY_CALL, '', 1)
        assert _NOTIFY_CALL not in neutered, 'NEUTER failed to apply'
        # The ratchet predicate, re-run on the neutered source, must fail.
        with pytest.raises((ValueError, AssertionError)):
            if path == FINALIZE_PATH:
                notify_pos = neutered.index(_NOTIFY_CALL)  # ValueError
                assert notify_pos > neutered.index(_LATCH_POP)
            else:
                notify_pos = neutered.index(_NOTIFY_CALL)  # ValueError
                assert notify_pos > neutered.index(_DONE_APPEND)


# ═════════════════════════════════════════════════════════════════════
#  2. Wire contract of the helper
# ═════════════════════════════════════════════════════════════════════

def test_helper_emits_notify_with_conv_rev_none_and_owner(monkeypatch):
    from lib.tasks_pkg.manager import _registry

    calls = []
    monkeypatch.setattr('lib.conversations.notify_conv_changed',
                        lambda conv_id, **kw: calls.append((conv_id, kw)))

    _registry.notify_terminal_busy_state(
        {'id': 't-abc', 'convId': 'conv-xyz', '_userId': '7'})

    assert len(calls) == 1, f'expected exactly one notify, got {calls}'
    conv_id, kw = calls[0]
    assert conv_id == 'conv-xyz'
    # rev=None deliberately: the client applies the reducer half of a
    # rev-less frame (busy update) without triggering a body refetch.
    assert kw.get('rev') is None
    assert str(kw.get('user_id')) == '7'


def test_helper_silent_noop_without_conv_id(monkeypatch):
    from lib.tasks_pkg.manager import _registry

    calls = []
    monkeypatch.setattr('lib.conversations.notify_conv_changed',
                        lambda *a, **kw: calls.append((a, kw)))

    _registry.notify_terminal_busy_state({'id': 't-abc', 'convId': ''})
    _registry.notify_terminal_busy_state(None)

    assert calls == [], 'a convId-less task must never emit a busy frame'


# ═════════════════════════════════════════════════════════════════════
#  3. The projection truth the frame carries
# ═════════════════════════════════════════════════════════════════════

@pytest.fixture()
def _clean_registry():
    from lib.tasks_pkg.manager._state import tasks, tasks_lock
    with tasks_lock:
        snapshot = dict(tasks)
        tasks.clear()
    try:
        yield
    finally:
        with tasks_lock:
            tasks.clear()
            tasks.update(snapshot)


def _mk(tid, **kw):
    base = {'id': tid, 'convId': 'conv-1', 'aborted': False,
            'status': 'running', '_userId': ''}
    base.update(kw)
    return base


def test_projection_excludes_done_task_once_latch_popped(_clean_registry):
    """THE frame-is-a-clear property: after status='done' AND the latch pop,
    the conv must be ABSENT from the projection (the client reads absence /
    an empty list as IDLE)."""
    from lib.tasks_pkg.manager import _registry
    from lib.tasks_pkg.manager._state import tasks

    tasks['t-done'] = _mk('t-done', status='done')
    assert _registry.snapshot_running_by_conv() == {}, (
        'a done task with no latch must not project — this is the exact '
        'state the terminal frame now ships'
    )


def test_projection_includes_running_and_vu_carrier(_clean_registry):
    """COMPLEMENT: the same emission point must keep a conv busy when real
    work continues — a live worker and a live VU carrier (``#vu`` marker so
    the client lights the dot without minting an attach target)."""
    from lib.tasks_pkg.manager import _registry
    from lib.tasks_pkg.manager._state import tasks

    tasks['t-worker'] = _mk('t-worker', status='running')
    tasks['t-vu'] = _mk('t-vu', status='running', _vu_subtask=True,
                        _inline_messages=True)
    out = _registry.snapshot_running_by_conv()
    assert out.get('conv-1') == ['t-worker', 't-vu#vu'], (
        f'a live worker + live VU carrier must keep the conv busy: {out}'
    )


def test_projection_done_task_with_fresh_latch_still_busy(_clean_registry):
    """The latch window itself is UNCHANGED (deliberate): inside the ~30s
    pre-carrier sliver the projection still says busy — the fix moves the
    CLEAR to the moment the latch pops, it does not shorten the sliver."""
    import time

    from lib.tasks_pkg.manager import _registry
    from lib.tasks_pkg.manager._state import tasks

    tasks['t-fin'] = _mk('t-fin', status='done',
                         _finalize_started_at=time.time())
    out = _registry.snapshot_running_by_conv()
    assert out.get('conv-1') == ['t-fin'], (
        'the finalize latch must keep projecting busy inside its window '
        '(the VU-carrier-registration race the latch exists for)'
    )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
