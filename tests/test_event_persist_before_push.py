#!/usr/bin/env python3
"""Root-simplify frontend sync (epic pt_90a4a14875094c3f): prove the
DURABLE-BEFORE-VISIBLE ordering in ``manager.append_event``.

WHY
---
Previously ``TaskRuntime.append_event`` PUSHED a delta to the client (WS) and
``manager.append_event`` persisted the ``task_events`` row AFTERWARD. So a cold
reconnect at that instant folded a log missing the last ≤N just-pushed deltas —
the sub-checkpoint residual that kept the ``_snapshotLonger`` (content/thinking)
belt load-bearing. The fix reorders the persist BEFORE the push (via the
runtime's ``before_push`` hook), so the durable log is never behind the bytes
the client has received.

Tests (real Sidecar, real manager.append_event, instrumented push):
  1. ``test_persist_precedes_push`` — install a push spy that, AT PUSH TIME,
     reads task_events for THIS seq. After the reorder the row is ALREADY
     present at every push → the fold at any push instant equals the client
     buffer. ★ THE ORDERING GUARANTEE.
  2. ``test_cold_fold_equals_client_buffer_zero_missing`` — stream K deltas;
     at the moment the client has seen J of them (push #J observed), the
     folded log has >= J deltas (never behind). Zero missing.
  3. ``test_persist_failure_does_not_block_push`` — a persist that raises must
     NOT stop the push (best-effort ordering; a DB blip can't stall the stream).

NEUTER: monkeypatch the runtime's before_push to fire AFTER the push (restore
the OLD post-push order) → test #1 FAILS (the row is missing at push time).
Proves the ordering is real and load-bearing.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402
from lib.tasks_pkg.manager.runtime import chat_task_runtime  # noqa: E402

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('\u2713', '32'), msg)
def _fail(msg): print(' ', _color('\u2717', '31'), msg); sys.exit(1)


def _make_task(task_id):
    """Register a real chat-runtime task so manager.append_event hits the
    runtime (before_push) path, not the legacy fallback."""
    return chat_task_runtime.create(user_id=1, task_id=task_id)


def _row_seqs(task_id):
    """Current set of seqs persisted in task_events for this task."""
    from lib.tasks_pkg.event_log import read_events
    return [e['event_id'] for e in read_events(task_id, since_event_id=None)]


def test_persist_precedes_push():
    """★ At the instant of each push, the row for that seq is ALREADY in the DB."""
    from lib.tasks_pkg import manager as _mgr
    import lib.agent_core.push as _push

    tid = f'pbp-{uuid.uuid4().hex[:8]}'
    _make_task(tid)
    observations = []  # (seq, row_present_at_push_time)
    orig_push = _push.push_event

    def _spy(channel, task_id, event, *, user_id):
        if task_id == tid and event.get('type') == 'delta':
            seqs = _row_seqs(tid)
            observations.append((event.get('seq'), event.get('seq') in seqs))
        return orig_push(channel, task_id, event, user_id=user_id)

    _push.push_event = _spy
    try:
        task = chat_task_runtime.get(tid)
        for i in range(30):
            _mgr.append_event(task, {'type': 'delta', 'content': f'chunk{i} '})
    finally:
        _push.push_event = orig_push
        _cleanup(tid)

    assert observations, 'no delta pushes observed — spy not wired'
    missing = [seq for seq, present in observations if not present]
    assert not missing, (
        f'{len(missing)}/{len(observations)} deltas were pushed BEFORE their '
        f'task_events row was committed (seqs {missing[:5]}...) — durable-before-'
        f'visible ordering violated')
    _ok(f'★ all {len(observations)} deltas persisted BEFORE push (durable-before-visible)')


def test_cold_fold_equals_client_buffer_zero_missing():
    from lib.tasks_pkg import manager as _mgr
    import lib.agent_core.push as _push
    from lib.tasks_pkg.event_fold import fold_cold_state_text

    tid = f'pbp-fold-{uuid.uuid4().hex[:8]}'
    _make_task(tid)
    client_buffer = []  # what the client has "seen" at each push
    fold_at_push = []   # folded length observable at that same instant
    orig_push = _push.push_event

    def _spy(channel, task_id, event, *, user_id):
        r = orig_push(channel, task_id, event, user_id=user_id)
        if task_id == tid and event.get('type') == 'delta':
            client_buffer.append(event.get('content', ''))
            folded_c, _ = fold_cold_state_text(tid)
            fold_at_push.append((len(''.join(client_buffer)), len(folded_c)))
        return r

    _push.push_event = _spy
    try:
        task = chat_task_runtime.get(tid)
        for i in range(25):
            _mgr.append_event(task, {'type': 'delta', 'content': f'w{i} '})
    finally:
        _push.push_event = orig_push
        _cleanup(tid)

    behind = [(cb, fl) for cb, fl in fold_at_push if fl < cb]
    assert not behind, (
        f'the folded log was BEHIND the client buffer at {len(behind)} push '
        f'instants (e.g. client={behind[0][0]} > fold={behind[0][1]}) — '
        f'a cold reconnect there would lose deltas')
    _ok(f'cold fold >= client buffer at all {len(fold_at_push)} push instants (zero missing)')


def test_persist_failure_does_not_block_push():
    """A persist that raises must NOT stop the push (best-effort ordering)."""
    from lib.tasks_pkg import manager as _mgr
    import lib.agent_core.push as _push
    import lib.tasks_pkg.event_log as _elog

    tid = f'pbp-fail-{uuid.uuid4().hex[:8]}'
    _make_task(tid)
    pushed = []
    orig_push = _push.push_event
    orig_persist = _elog.append_persistent_event

    def _boom(*a, **k):
        raise RuntimeError('simulated DB blip')

    def _spy(channel, task_id, event, *, user_id):
        if task_id == tid and event.get('type') == 'delta':
            pushed.append(event.get('seq'))
        return orig_push(channel, task_id, event, user_id=user_id)

    # manager imports append_persistent_event inside _persist_before_push, so
    # patch it on the module it's imported FROM.
    _elog.append_persistent_event = _boom
    _push.push_event = _spy
    try:
        task = chat_task_runtime.get(tid)
        for i in range(5):
            _mgr.append_event(task, {'type': 'delta', 'content': f'x{i}'})
    finally:
        _elog.append_persistent_event = orig_persist
        _push.push_event = orig_push
        _cleanup(tid)

    assert len(pushed) == 5, (
        f'a persist failure BLOCKED the push ({len(pushed)}/5 pushed) — '
        f'ordering is not best-effort')
    _ok('persist failure did NOT block the push (best-effort ordering)')


def _cleanup(task_id):
    try:
        chat_task_runtime.discard(task_id)
    except Exception:
        pass


_POSITIVE = [test_persist_precedes_push,
             test_cold_fold_equals_client_buffer_zero_missing,
             test_persist_failure_does_not_block_push]


def _run(fn):
    try:
        fn(); return True
    except AssertionError as e:
        print(' ', _color('\u2717', '31'), f'{fn.__name__}: {e}'); return False
    except Exception as e:
        import traceback; traceback.print_exc()
        print(' ', _color('\u2717', '31'), f'{fn.__name__}: {type(e).__name__}: {e}')
        return False


def main():
    print()
    print(_color('\u2550\u2550\u2550 durable-before-visible ordering \u2014 neuter \u2550\u2550\u2550', '36'))
    print()
    from tests._standalone_guard import guard_standalone_storage
    guard_standalone_storage('test_event_persist_before_push')

    print(_color('Baseline (shipped code):', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('baseline failed')

    # ── NC: force the persist to fire AFTER the push (restore the OLD order)
    #    by wrapping the runtime.append_event to strip before_push and run it
    #    post-push. Test #1 must then FAIL (row absent at push time). ──
    print()
    print(_color('NC \u2014 restore post-push persist order:', '36'))
    from lib.tasks_pkg import manager as _mgr
    _orig_rt_append = chat_task_runtime.append_event

    def _post_push_append(task_id, event, *, before_push=None):
        seq = _orig_rt_append(task_id, event, before_push=None)  # push WITHOUT persist
        if before_push is not None and seq is not None:
            try:
                before_push(seq)  # persist AFTER the push (the old bug)
            except Exception:
                pass
        return seq

    chat_task_runtime.append_event = _post_push_append
    try:
        precede_ok = _run(test_persist_precedes_push)
        fail_ok = _run(test_persist_failure_does_not_block_push)
    finally:
        chat_task_runtime.append_event = _orig_rt_append
    if precede_ok:
        _fail('NC: persist-precedes-push PASSED with post-push order restored — '
              'the ordering guarantee is not real / the test does not pin it')
    if not fail_ok:
        _fail('NC: best-effort control failed under neuter')
    _ok('NC: persist-precedes-push FAILS with post-push order; best-effort control holds')

    print()
    print(_color('Post-restore baseline:', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('post-restore baseline failed')

    print()
    print(_color('\u2550\u2550\u2550 ALL PERSIST-BEFORE-PUSH TESTS + NEUTER PASSED \u2550\u2550\u2550', '32'))
    print()


if __name__ == '__main__':
    main()
