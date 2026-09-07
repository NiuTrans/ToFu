"""Focused executable specs for per-tool runtime cancellation.

Pins the accepted plan for ``lib/tasks_pkg/tool_runtime/context.py``:

* callback registration/unregistration is idempotent and race-safe,
* scoped resources are cancelled exactly once,
* terminal settlement is single-winner,
* parent cancellation fans out to every parallel scoped context.

These are the contracts the concurrent ``run_command``/route integration must
satisfy. They exercise only the already-integrated context primitive, so they
must pass against the current tree.
"""

from __future__ import annotations

import threading

import pytest

from lib.tasks_pkg.tool_runtime import (
    ToolCancelled,
    ToolExecutionContext,
    cancel_task_contexts,
    context_for_task,
    unregister_context,
)

pytestmark = pytest.mark.unit


def _make_context(task=None, **overrides) -> ToolExecutionContext:
    values = {
        'task': task or {'id': 'ctx-task', 'aborted': False},
        'round_num': 1,
        'tool_call_id': 'tc-1',
        'tool_name': 'run_command',
        'owner_user_id': 1,
        'round_entry': {},
    }
    values.update(overrides)
    return ToolExecutionContext(**values)


# ── Registration / unregistration ──────────────────────────────────────
def test_tool_cancelled_is_runtime_error():
    assert issubclass(ToolCancelled, RuntimeError)


def test_register_cancel_callback_rejects_non_callable():
    ctx = _make_context()
    with pytest.raises(TypeError):
        ctx.register_cancel_callback(None)  # type: ignore[arg-type]


def test_unregister_is_idempotent_and_removes_callback():
    ctx = _make_context()
    calls: list[int] = []
    unregister = ctx.register_cancel_callback(lambda: calls.append(1))
    unregister()
    unregister()  # idempotent — a second unregister is a harmless no-op
    assert ctx.request_resource_cancellation() == (0, ())
    assert calls == []


def test_register_after_cancellation_invokes_immediately():
    """The cancellation-before-spawn seam: a resource that registers its
    cleanup AFTER cancellation was already requested must be torn down
    immediately, never stored for a fanout that already happened."""
    ctx = _make_context(task={'id': 't', 'aborted': True})
    calls: list[int] = []
    unregister = ctx.register_cancel_callback(lambda: calls.append(1))
    assert calls == [1], 'must invoke immediately when already cancelled'
    assert callable(unregister)
    # Nothing was stored, so a later fanout finds nothing.
    assert ctx.request_resource_cancellation() == (0, ())


# ── Exactly-once resource cancellation ─────────────────────────────────
def test_request_resource_cancellation_invokes_each_callback_once():
    ctx = _make_context()
    calls: list[int] = []
    for i in range(3):
        ctx.register_cancel_callback((lambda i=i: calls.append(i)))
    count, errors = ctx.request_resource_cancellation()
    assert count == 3
    assert errors == ()
    assert sorted(calls) == [0, 1, 2]
    # Registry cleared: a second fanout invokes nothing.
    assert ctx.request_resource_cancellation() == (0, ())


def test_request_resource_cancellation_collects_failures_and_continues():
    ctx = _make_context()
    calls: list[int] = []

    def boom():
        raise RuntimeError('boom')

    ctx.register_cancel_callback(boom)
    ctx.register_cancel_callback(lambda: calls.append(1))
    count, errors = ctx.request_resource_cancellation()
    assert count == 2
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert calls == [1], 'one failing callback must not block the rest'


def test_callback_registering_new_callback_during_fanout_not_double_invoked():
    """A callback registered from inside a fanout must be invoked exactly
    once. With cancellation already requested it fires immediately; the
    already-snapshotted fanout does not then invoke it a second time."""
    ctx = _make_context(task={'id': 't', 'aborted': False})
    calls: list[str] = []

    def outer():
        calls.append('outer')
        ctx.register_cancel_callback(lambda: calls.append('inner'))

    ctx.register_cancel_callback(outer)
    ctx.abort_event.set()  # cancel_task signals before callback fanout
    count, _ = ctx.request_resource_cancellation()
    # Only `outer` was in the snapshot; `inner` registered mid-fanout and was
    # invoked immediately because cancellation was already requested.
    assert count == 1
    assert calls == ['outer', 'inner']


def test_registration_fanout_race_each_callback_exactly_once():
    """Stress the registration/cancellation race: no callback is skipped and
    none is invoked twice regardless of interleaving."""
    task = {'id': 'race', 'aborted': False}
    ctx = _make_context(task=task)
    n_callbacks = 40
    per_callback = [0] * n_callbacks
    lock = threading.Lock()
    ready = threading.Barrier(3)
    start = threading.Event()

    def registrant():
        ready.wait()
        start.wait()
        for i in range(n_callbacks):
            def callback(i=i):
                with lock:
                    per_callback[i] += 1
            ctx.register_cancel_callback(callback)

    def canceller():
        ready.wait()
        start.wait()
        ctx.abort_event.set()
        ctx.request_resource_cancellation()

    t1 = threading.Thread(target=registrant)
    t2 = threading.Thread(target=canceller)
    t1.start()
    t2.start()
    ready.wait()
    start.set()
    t1.join()
    t2.join()

    assert all(value == 1 for value in per_callback), (
        f'each callback must be invoked exactly once, got {per_callback}')


# ── Exactly-once terminal settlement ───────────────────────────────────
def test_settle_once_exactly_once():
    ctx = _make_context()
    assert ctx.settle_once('done') is True
    assert ctx.settle_once('error') is False
    assert ctx.settle_once('done') is False
    assert ctx.terminal_state == 'done'
    assert ctx.snapshot()['terminalState'] == 'done'


def test_settle_once_concurrent_single_winner():
    ctx = _make_context()
    results: list[bool] = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(ctx.settle_once('done'))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert results.count(False) == 7


def test_settle_once_does_not_drop_pending_resource_cleanup():
    ctx = _make_context()
    calls: list[int] = []
    ctx.register_cancel_callback(lambda: calls.append(1))
    assert ctx.settle_once('cancelled') is True
    assert ctx.request_resource_cancellation() == (1, ())
    assert calls == [1]


# ── Parent fanout across parallel scoped contexts ──────────────────────
def _parallel_contexts(task, n=3):
    entries = []
    for i in range(n):
        round_entry = {'toolCallId': f'tc-{i}'}
        ctx = context_for_task(
            task,
            round_num=i + 1,
            tool_call_id=f'tc-{i}',
            tool_name='run_command',
            round_entry=round_entry,
        )
        entries.append(ctx)
    return entries


def test_context_for_task_registers_and_unregister_removes():
    task = {'id': 'fanout-task', 'aborted': False, '_userId': 1}
    ctx = context_for_task(
        task, round_num=1, tool_call_id='tc', tool_name='run_command',
        round_entry={})
    assert isinstance(task.get('_activeToolContexts'), dict)
    assert len(task['_activeToolContexts']) == 1
    unregister_context(ctx)
    assert not task.get('_activeToolContexts')


def test_cancel_task_contexts_fans_out_to_parallel_contexts_exactly_once():
    task = {'id': 'fanout-task', 'aborted': False, '_userId': 1}
    contexts = _parallel_contexts(task, n=3)
    calls: list[str] = []
    for ctx in contexts:
        ctx.register_cancel_callback(lambda: calls.append('x'))

    count, errors = cancel_task_contexts(task)
    assert count == 3
    assert errors == ()
    assert len(calls) == 3
    # Every context's registry is cleared → a second fanout is a no-op.
    assert cancel_task_contexts(task) == (0, ())


def test_unregister_context_excludes_later_fanout():
    task = {'id': 'fanout-task', 'aborted': False, '_userId': 1}
    contexts = _parallel_contexts(task, n=3)
    for ctx in contexts:
        ctx.register_cancel_callback(lambda: None)
    unregister_context(contexts[1])
    count, _ = cancel_task_contexts(task)
    assert count == 2


def test_unregister_during_fanout_snapshot_is_stable():
    """A peer unregistered mid-fanout still runs for THIS fanout (the
    snapshot is taken up front) and is absent from the next one."""
    task = {'id': 'fanout-task', 'aborted': False, '_userId': 1}
    contexts = _parallel_contexts(task, n=2)
    calls: list[str] = []

    def first():
        calls.append('first')
        unregister_context(contexts[1])  # remove peer mid-fanout

    contexts[0].register_cancel_callback(first)
    contexts[1].register_cancel_callback(lambda: calls.append('second'))

    count, _ = cancel_task_contexts(task)
    assert count == 2, 'peer still runs this fanout (stable snapshot)'
    assert calls == ['first', 'second']
    # Peer was unregistered during fanout → a later fanout finds nothing.
    assert cancel_task_contexts(task) == (0, ())


def test_cancel_task_contexts_without_registry_returns_zero():
    assert cancel_task_contexts({'id': 't'}) == (0, ())
