#!/usr/bin/env python3
"""Executable lifecycle contract for TaskRuntime.

Validates the shared authority used by chat, paper, translation, production,
and other background capabilities. Run standalone:

    python tests/test_task_runtime.py
"""

import asyncio
import logging
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.agent_core.task_runtime import TaskRuntime  # noqa: E402


pytestmark = pytest.mark.unit


def _color(s, c):
    return f'\033[{c}m{s}\033[0m'


def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def test_create_and_get():
    rt = TaskRuntime('test-kind', ttl=60)
    task = rt.create(user_id=1, meta={'foo': 'bar'})
    assert task['id']
    assert task['kind'] == 'test-kind'
    assert task['status'] == 'pending'
    assert task['events'] == []
    assert task['meta']['foo'] == 'bar'
    assert task['_principalContext'] == {
        'kind': 'user', 'subject_id': 'user:1', 'owner_user_id': 1,
        'tenant_id': None, 'scopes': [],
    }
    assert task['error'] is None
    assert task['result'] is None
    assert task['finished_at'] is None

    found = rt.get(task['id'])
    assert found is task
    assert rt.get('nonexistent') is None
    _ok('create() and get()')


def test_explicit_task_id():
    rt = TaskRuntime('test')
    task = rt.create(user_id=1, task_id='custom-id-123')
    assert task['id'] == 'custom-id-123'
    assert rt.get('custom-id-123') is task
    _ok('create() with explicit task_id')


def test_custom_fields_are_atomic_and_cannot_override_lifecycle():
    rt = TaskRuntime('test-kind')
    task = rt.create(user_id=1)

    assert rt.mark_running(task['id'], fields={'progress': '1/3'}) is True
    assert rt.update_fields(
        task['id'],
        fields={'progress': '2/3', 'statusMessage': 'working'},
        only_if_status='running',
    ) is True
    assert task['progress'] == '2/3'

    with pytest.raises(ValueError, match='runtime-owned fields: status'):
        rt.update_fields(task['id'], fields={'status': 'done'})

    assert rt.finish(task['id'], result='complete') is True
    assert rt.update_fields(
        task['id'], fields={'progress': '3/3'},
        only_if_status='running') is False
    assert task['progress'] == '2/3'
    _ok('custom fields cannot bypass atomic lifecycle transitions')


def test_terminal_event_fields_cannot_spoof_runtime_verdict():
    rt = TaskRuntime('test-kind', push_channel='')
    task = rt.create(user_id=1)
    rt.mark_running(task['id'])

    assert rt.finish(
        task['id'],
        result='translated',
        terminal_event_fields={
            'type': 'error',
            'status': 'error',
            'result': 'spoofed',
            'convId': 'conversation-1',
        },
    ) is True

    terminal_event = task['events'][-1]
    assert terminal_event['type'] == 'done'
    assert terminal_event['status'] == 'done'
    assert terminal_event['result'] == 'translated'
    assert terminal_event['convId'] == 'conversation-1'
    _ok('terminal correlation is extensible but verdict is runtime-owned')


def test_append_event_assigns_seq():
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    s0 = rt.append_event(task['id'], {'type': 'progress', 'msg': 'one'})
    s1 = rt.append_event(task['id'], {'type': 'progress', 'msg': 'two'})
    s2 = rt.append_event(task['id'], {'type': 'progress', 'msg': 'three'})
    assert s0 == 0 and s1 == 1 and s2 == 2
    assert task['events'][0]['seq'] == 0
    assert task['events'][1]['seq'] == 1
    assert task['events'][2]['seq'] == 2
    assert task['events'][1]['msg'] == 'two'
    _ok('append_event() assigns monotonic seq')


def test_append_event_transitions_to_running():
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    assert task['status'] == 'pending'
    rt.append_event(task['id'], {'type': 'started'})
    assert task['status'] == 'running'
    _ok('append_event() transitions pending → running')


def test_append_event_unknown_task():
    rt = TaskRuntime('test')
    seq = rt.append_event('does-not-exist', {'type': 'noop'})
    assert seq is None
    _ok('append_event() on unknown task returns None')


def test_stale_attempt_persistence_fence_is_not_duplicated_as_error(caplog):
    """The authority boundary already emits the one actionable WARNING."""
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)

    def reject_stale_attempt(_sequence):
        raise RuntimeError(
            'conversation event rejected: attempt is stale or no longer current')

    with caplog.at_level(logging.DEBUG, logger='lib.agent_core.task_runtime'):
        sequence = rt.append_event(
            task['id'],
            {'type': 'round_committed', 'attemptId': 'attempt-stale'},
            before_push=reject_stale_attempt,
        )

    persistence_records = [
        record for record in caplog.records
        if 'authoritative persistence failed' in record.getMessage()
    ]
    assert sequence == 0
    assert task['_pushWithheldCount'] == 1
    assert len(persistence_records) == 1
    assert persistence_records[0].levelno == logging.DEBUG


def test_finish_done():
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    ok = rt.finish(task['id'], result={'output': 42})
    assert ok
    assert task['status'] == 'done'
    assert task['result'] == {'output': 42}
    assert task['error'] is None
    assert task['finished_at'] is not None

    # Terminal event must have been emitted
    assert task['events'][-1]['type'] == 'done'
    assert task['events'][-1]['status'] == 'done'
    assert task['events'][-1]['result'] == {'output': 42}
    _ok('finish(result=) emits done event')


def test_finish_error_string():
    rt = TaskRuntime('test', error_source='unit_test')
    task = rt.create(user_id=1)
    rt.finish(task['id'], error='something broke')
    assert task['status'] == 'error'
    assert task['error'] is not None
    assert task['error']['detail'] == 'something broke'
    assert task['events'][-1]['type'] == 'error'
    assert task['events'][-1]['error'] == task['error']
    _ok('finish(error="str") wraps in envelope and emits error event')


def test_finish_error_exception():
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    try:
        raise ValueError('oh no')
    except ValueError as e:
        rt.finish(task['id'], error=e)
    assert task['status'] == 'error'
    assert task['error'] is not None
    assert 'oh no' in str(task['error'])
    _ok('finish(error=Exception) wraps in envelope')


def test_finish_error_dict():
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    # A COMPLETE envelope (kind + string message) passes through verbatim —
    # that is what make_envelope/from_exception produced upstream.
    envelope = {'kind': 'ratelimit', 'message': '⚠️ API 请求已达限频（429）',
                'detail': 'too many requests'}
    rt.finish(task['id'], error=envelope)
    assert task['error'] is envelope
    _ok('finish(error=dict) preserves a complete envelope')

    # An INCOMPLETE dict (kind but no message) is COMPLETED via
    # make_envelope — the frontend's isErrorEnvelope requires a string
    # message, so the old verbatim passthrough rendered these as
    # 'Unknown error' + a JSON blob (the worker_lost stall-reap defect).
    task2 = rt.create(user_id=1)
    rt.finish(task2['id'], error={'kind': 'worker_lost', 'detail': 'stalled'})
    env2 = task2['error']
    assert env2['kind'] == 'worker_lost'
    assert env2['detail'] == 'stalled'
    assert isinstance(env2.get('message'), str) and env2['message']
    _ok('finish(error=incomplete dict) completes it into a full envelope')

    # A domain extension may travel with the envelope, but it must not expand
    # the closed error-kind vocabulary.
    task3 = rt.create(user_id=1)
    rt.finish(task3['id'], error={
        'kind': 'unregistered_domain_error',
        'message': 'domain failure',
        'detail': 'bad state',
        'outcome': {'format': 'domain.outcome/v1'},
    })
    env3 = task3['error']
    assert env3['kind'] == 'generic'
    assert env3['message'] == 'domain failure'
    assert env3['outcome'] == {'format': 'domain.outcome/v1'}
    _ok('finish(error=dict) validates kind and retains domain extensions')


def test_finish_idempotent():
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    assert rt.finish(task['id'], result='first') is True
    # Second finish should be ignored
    assert rt.finish(task['id'], result='second') is False
    assert task['result'] == 'first'
    _ok('finish() is idempotent (second call returns False)')


def test_abort_then_finish_marks_aborted():
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    rt.abort(task['id'])
    assert task['abort_event'].is_set()
    rt.finish(task['id'])  # No error → should become 'aborted'
    assert task['status'] == 'aborted'
    assert task['events'][-1]['type'] == 'aborted'
    _ok('abort() + finish() marks as aborted')


def test_abort_then_finish_with_error_marks_error():
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    rt.abort(task['id'])
    rt.finish(task['id'], error='crashed during abort')
    # Errors take precedence over abort
    assert task['status'] == 'error'
    _ok('abort() + finish(error=) marks as error (error wins)')


def test_abort_unknown_task():
    rt = TaskRuntime('test')
    assert rt.abort('does-not-exist') is False
    _ok('abort() on unknown task returns False')


def test_abort_already_finished():
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    rt.finish(task['id'], result='ok')
    assert rt.abort(task['id']) is False
    _ok('abort() on finished task returns False')


def test_poll_cursor_replay():
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    for i in range(5):
        rt.append_event(task['id'], {'type': 'progress', 'i': i})

    # First poll from cursor=0
    r0 = rt.poll(task['id'], cursor=0)
    assert r0['ok'] is True
    assert r0['format'] == 'tofu.task-replay/v1'
    assert r0['cursor'] == {'requested': 0, 'next': 5, 'reset': False}
    assert len(r0['events']) == 5
    assert r0['next_cursor'] == 5
    assert r0['done'] is False
    assert r0['status'] == 'running'

    # Add more events
    rt.append_event(task['id'], {'type': 'progress', 'i': 5})
    rt.append_event(task['id'], {'type': 'progress', 'i': 6})

    # Poll from cursor=5 returns only new events
    r1 = rt.poll(task['id'], cursor=5)
    assert len(r1['events']) == 2
    assert r1['next_cursor'] == 7
    assert r1['events'][0]['i'] == 5
    _ok('poll(cursor=N) returns only new events since cursor')


def test_poll_future_cursor_resets_before_later_events_arrive():
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    rt.append_event(task['id'], {'type': 'progress', 'i': 0})

    corrected = rt.poll(task['id'], cursor=99)
    assert corrected['events'] == []
    assert corrected['next_cursor'] == 1
    assert corrected['cursor'] == {
        'requested': 99, 'next': 1, 'reset': True,
    }

    # A client that accepts the producer-owned reset must receive an event
    # appended after the correction instead of remaining stranded at 99.
    rt.append_event(task['id'], {'type': 'progress', 'i': 1})
    resumed = rt.poll(task['id'], cursor=corrected['next_cursor'])
    assert [event['i'] for event in resumed['events']] == [1]
    assert resumed['cursor'] == {
        'requested': 1, 'next': 2, 'reset': False,
    }

    assert rt.poll(task['id'], cursor=-8)['cursor']['requested'] == 0
    assert rt.poll(task['id'], cursor='bad')['cursor']['requested'] == 0
    _ok('poll() authoritatively resets a future/untrusted cursor')


def test_poll_after_done():
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    rt.append_event(task['id'], {'type': 'progress'})
    rt.finish(task['id'], result={'value': 99})

    r = rt.poll(task['id'], cursor=0)
    assert r['done'] is True
    assert r['status'] == 'done'
    assert r['result'] == {'value': 99}
    # Must include the terminal event
    types = [e['type'] for e in r['events']]
    assert 'done' in types
    _ok('poll() after finish() returns done=True with result')


def test_poll_after_error():
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    rt.finish(task['id'], error='something failed')
    r = rt.poll(task['id'], cursor=0)
    assert r['done'] is True
    assert r['status'] == 'error'
    assert r['error']['detail'] == 'something failed'
    _ok('poll() after finish(error=) returns done=True with error envelope')


def test_poll_unknown_task():
    rt = TaskRuntime('test')
    r = rt.poll('does-not-exist', cursor=0)
    assert r['ok'] is False
    assert r['error'] == 'not_found'
    assert r['done'] is True
    assert r['format'] == 'tofu.task-replay/v1'
    assert r['cursor'] == {'requested': 0, 'next': 0, 'reset': False}
    _ok('poll() on unknown task returns ok=False, error=not_found')


def test_poll_surfaces_model():
    """A task whose worker named its making-model (paper podcast/video)
    exposes it in EVERY poll frame — running and terminal — so a live
    poller can badge the artifact without waiting for a lookup re-attach.
    A task with no model concept must NOT grow the field (frames stay
    minimal for every other kind)."""
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    task['model'] = 'm-alpha'
    rt.append_event(task['id'], {'type': 'progress'})

    running = rt.poll(task['id'], cursor=0)
    assert running.get('model') == 'm-alpha', \
        f'running frame lost the model: {running!r}'

    rt.finish(task['id'], result={'final_path': '/job/final.mp4'})
    done = rt.poll(task['id'], cursor=0)
    assert done['done'] is True
    assert done.get('model') == 'm-alpha', \
        f'done frame lost the model: {done!r}'

    # Complement pin: no model → no key at all.
    plain = rt.create(user_id=1)
    rt.append_event(plain['id'], {'type': 'progress'})
    r2 = rt.poll(plain['id'], cursor=0)
    assert 'model' not in r2, f"model-less task grew a model field: {r2!r}"
    rt.finish(plain['id'], result='ok')
    r3 = rt.poll(plain['id'], cursor=0)
    assert 'model' not in r3, f"model-less done frame grew a model field: {r3!r}"
    _ok('poll() surfaces task.model (running + done); absent when unnamed')


def test_NEUTER_poll_model_line_loadbearing():
    """Amputate the ``if task.get('model'):`` block from a COPY of
    lib/agent_core/task_runtime.py, import the copy, and prove the poll
    frame LOSES the model — the behavior probe above must be wired to the
    shipped line, not to a stub."""
    import importlib.util

    real = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'lib', 'agent_core', 'task_runtime.py')
    with open(real, encoding='utf-8') as f:
        src = f.read()
    marker = (
        "        if task.get('model'):\n"
        "            extras['model'] = task['model']\n"
    )
    assert marker in src, 'poll model marker not found — test is stale'
    broken = src.replace(marker, '', 1)
    assert broken != src

    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       '_task_runtime_no_model.py')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        spec = importlib.util.spec_from_file_location(
            '_task_runtime_no_model', tmp)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        rt = mod.TaskRuntime('neuter')
        task = rt.create(user_id=1)
        task['model'] = 'm-alpha'
        rt.finish(task['id'], result='ok')
        frame = rt.poll(task['id'], cursor=0)
        assert 'model' not in frame, \
            'amputating the model line did NOT remove it from the frame'
        _ok('NEUTER: without the poll model line the frame loses the model')
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    with open(real, encoding='utf-8') as f:
        assert f.read() == src, 'shipped file modified!'


def test_spawn_outside_event_loop():
    """Spawn falls back to daemon thread when no asyncio loop is running."""
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    completed = threading.Event()

    def worker(tid):
        rt.append_event(tid, {'type': 'progress', 'step': 1})
        time.sleep(0.05)
        rt.append_event(tid, {'type': 'progress', 'step': 2})
        rt.finish(tid, result='thread-result')
        completed.set()

    rt.spawn(task['id'], worker, task['id'])
    assert completed.wait(timeout=2), 'Worker did not complete in 2s'
    assert task['status'] == 'done'
    assert task['result'] == 'thread-result'
    _ok('spawn() works outside event loop (daemon thread)')


def test_spawn_inside_event_loop():
    """Spawn uses a tracked executor worker when running inside a loop."""
    rt = TaskRuntime('test')

    async def _runner():
        task = rt.create(user_id=1)
        completed = threading.Event()

        def worker():
            rt.append_event(task['id'], {'type': 'progress'})
            rt.finish(task['id'], result='loop-result')
            completed.set()

        rt.spawn(task['id'], worker)
        # Wait for the asyncio task to complete
        for _ in range(20):
            if completed.is_set():
                break
            await asyncio.sleep(0.05)
        return task

    task = asyncio.run(_runner())
    assert task['status'] == 'done'
    assert task['result'] == 'loop-result'
    _ok('spawn() works inside event loop (isolated asyncio.to_thread worker)')


def test_spawn_does_not_inherit_request_contextvars():
    """Background workers must not inherit request/app ContextVars.

    Quart stores request-scoped DB connections in ContextVars. Propagating the
    route context into a long-running worker strands that connection outside
    request teardown and can retain SQLite's writer lane forever.
    """
    import contextvars

    rt = TaskRuntime('context-isolation')
    request_marker = contextvars.ContextVar(
        'task_runtime_request_marker', default='no-request')

    async def _runner():
        completed = threading.Event()
        observed = {}
        task = rt.create(user_id=1)
        token = request_marker.set('request-context')
        try:
            def worker():
                observed['value'] = request_marker.get()
                rt.finish(task['id'], result='done')
                completed.set()

            rt.spawn(task['id'], worker)
            for _ in range(40):
                if completed.is_set():
                    break
                await asyncio.sleep(0.025)
        finally:
            request_marker.reset(token)
        return observed, completed.is_set()

    observed, completed = asyncio.run(_runner())
    assert completed, 'isolated executor worker did not complete'
    assert observed.get('value') == 'no-request', (
        'spawn propagated request ContextVars into a background worker')


def test_spawn_holds_strong_ref_under_gc():
    """The asyncio branch must keep a STRONG reference to the worker Task.

    The event loop holds only a weak reference to a bare ensure_future()
    result, so under GC pressure an untracked Task can be collected mid-flight
    and the worker silently never runs. spawn() must register the Task in
    self._bg_tasks so it survives gc.collect(), and evict it on completion.
    """
    import gc

    rt = TaskRuntime('test')

    async def _runner():
        N = 25
        completed = [threading.Event() for _ in range(N)]
        ids = []
        for i in range(N):
            task = rt.create(user_id=1)
            ids.append(task['id'])

            def worker(idx, tid):
                time.sleep(0.01)
                rt.finish(tid, result='ok')
                completed[idx].set()

            rt.spawn(task['id'], worker, i, task['id'])
            # Hammer GC in the scheduling window — a weakly-referenced Task
            # would be collectible here.
            gc.collect()

        # While workers are in flight, the strong-ref set must be non-empty.
        assert rt._bg_tasks, 'spawn() kept no strong ref — Task is GC-droppable'

        for _ in range(200):
            if all(c.is_set() for c in completed):
                break
            gc.collect()
            await asyncio.sleep(0.01)
        return ids

    ids = asyncio.run(_runner())
    for tid in ids:
        t = rt.get(tid)
        assert t['status'] == 'done', f'task {tid[:8]} dropped: {t["status"]}'
    # The done-callback must have self-evicted every finished Task.
    assert rt._bg_tasks == set(), 'bg-task set did not self-evict on completion'
    _ok('spawn() holds a strong ref (GC-safe) and self-evicts on done')


def test_spawn_worker_crash_caught():
    """If the worker raises uncaught, finish(error=) is auto-called."""
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)

    def bad_worker():
        raise RuntimeError('worker exploded')

    rt.spawn(task['id'], bad_worker)
    # Wait for crash handling
    for _ in range(20):
        if task['status'] in ('error', 'done'):
            break
        time.sleep(0.05)
    assert task['status'] == 'error'
    assert task['error'] is not None
    assert 'worker exploded' in str(task['error'])
    _ok('spawn() catches worker crashes and auto-finishes with error')


def test_abort_event_signal():
    """Worker can poll task['abort_event'] to detect abort requests."""
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    iterations = []

    def worker(tid):
        for i in range(100):
            t = rt.get(tid)
            if t['abort_event'].is_set():
                rt.append_event(tid, {'type': 'aborted_at', 'i': i})
                rt.finish(tid)  # No error → 'aborted'
                return
            iterations.append(i)
            time.sleep(0.01)

    rt.spawn(task['id'], worker, task['id'])
    time.sleep(0.05)
    rt.abort(task['id'])
    for _ in range(50):
        if task['status'] in ('aborted', 'done', 'error'):
            break
        time.sleep(0.02)
    assert task['status'] == 'aborted', f"Expected 'aborted', got {task['status']}"
    assert len(iterations) > 0
    assert len(iterations) < 100  # Stopped early
    _ok('abort_event signals worker; finish() then marks aborted')


def test_cleanup_stale():
    rt = TaskRuntime('test', ttl=0.1)  # 100ms TTL for testing
    t1 = rt.create(user_id=1)
    t2 = rt.create(user_id=1)
    t3 = rt.create(user_id=1)
    rt.finish(t1['id'])
    rt.finish(t2['id'], error='boom')
    # t3 still pending — should NOT be cleaned even after TTL
    assert rt.task_count == 3

    time.sleep(0.15)  # Wait past TTL
    removed = rt.cleanup_stale()
    assert removed == 2, f'Expected 2 stale tasks cleaned, got {removed}'
    assert rt.task_count == 1
    assert rt.get(t1['id']) is None
    assert rt.get(t2['id']) is None
    assert rt.get(t3['id']) is not None  # Still pending — preserved
    _ok('cleanup_stale() purges only finished tasks past TTL')


def test_stats():
    rt = TaskRuntime('test')
    rt.create(user_id=1)  # pending
    t2 = rt.create(user_id=1)
    rt.append_event(t2['id'], {'type': 'started'})  # → running
    t3 = rt.create(user_id=1)
    rt.finish(t3['id'])
    t4 = rt.create(user_id=1)
    rt.finish(t4['id'], error='x')

    s = rt.stats()
    assert s['kind'] == 'test'
    assert s['total'] == 4
    assert s['pending'] == 1
    assert s['running'] == 1
    assert s['done'] == 1
    assert s['error'] == 1
    _ok('stats() returns aggregate counts by status')


def test_list_running():
    rt = TaskRuntime('test')
    t1 = rt.create(user_id=1)
    t2 = rt.create(user_id=1)
    rt.append_event(t2['id'], {'type': 'started'})
    t3 = rt.create(user_id=1)
    rt.finish(t3['id'])

    running = rt.list_running()
    ids = sorted([t['id'] for t in running])
    expected = sorted([t1['id'], t2['id']])
    assert ids == expected
    _ok('list_running() returns pending + running tasks')


def test_concurrent_append_events():
    """Stress test: multiple threads appending events concurrently."""
    rt = TaskRuntime('test')
    task = rt.create(user_id=1)
    NUM_THREADS = 8
    EVENTS_PER_THREAD = 50

    def producer(thread_id):
        for i in range(EVENTS_PER_THREAD):
            rt.append_event(task['id'], {
                'type': 'progress',
                'thread': thread_id,
                'i': i,
            })

    threads = [threading.Thread(target=producer, args=(t,))
               for t in range(NUM_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected_total = NUM_THREADS * EVENTS_PER_THREAD
    assert len(task['events']) == expected_total

    # Verify seq numbers are monotonic and unique
    seqs = [e['seq'] for e in task['events']]
    assert seqs == list(range(expected_total)), 'seq numbers are not monotonic!'
    _ok(f'thread-safe append_event() — {NUM_THREADS} threads × {EVENTS_PER_THREAD} events')


def test_push_integration():
    """Verify events are pushed to push_event when push_channel set.

    TaskRuntime imports push_event from its sole owner,
    ``lib.agent_core.push``.
    """
    import lib.agent_core.push as push_module
    received = []

    # Monkey-patch push_event to capture calls
    original = push_module.push_event

    def capture(channel, task_id, event, *, user_id):
        received.append((channel, task_id, event, user_id))

    push_module.push_event = capture
    try:
        rt = TaskRuntime('test', push_channel='paper')
        task = rt.create(user_id=1)
        rt.append_event(task['id'], {'type': 'progress'})
        rt.finish(task['id'], result='ok')

        assert len(received) >= 2
        for channel, tid, _, user_id in received:
            assert channel == 'paper'
            assert tid == task['id']
            assert user_id == 1
        _ok('push_channel auto-pushes events to lib.agent_core.push.push_event')
    finally:
        push_module.push_event = original


def test_no_push_channel():
    """When push_channel=None, events are NOT pushed."""
    import lib.agent_core.push as push_module
    received = []
    original = push_module.push_event

    def capture(channel, task_id, event):
        received.append((channel, task_id, event))

    push_module.push_event = capture
    try:
        rt = TaskRuntime('test', push_channel='')  # explicit empty string
        # Empty push_channel still falsy, so push_event won't be called
        task = rt.create(user_id=1)
        rt.append_event(task['id'], {'type': 'x'})
        rt.finish(task['id'])
        assert len(received) == 0
        _ok('push_channel="" disables push (events not broadcast)')
    finally:
        push_module.push_event = original


# ════════════════════════════════════════════════════════════
#  Run all tests
# ════════════════════════════════════════════════════════════

def main():
    print()
    print(_color('═══ TaskRuntime Unit Tests ═══', '36'))
    print()

    tests = [
        test_create_and_get,
        test_explicit_task_id,
        test_custom_fields_are_atomic_and_cannot_override_lifecycle,
        test_terminal_event_fields_cannot_spoof_runtime_verdict,
        test_append_event_assigns_seq,
        test_append_event_transitions_to_running,
        test_append_event_unknown_task,
        test_finish_done,
        test_finish_error_string,
        test_finish_error_exception,
        test_finish_error_dict,
        test_finish_idempotent,
        test_abort_then_finish_marks_aborted,
        test_abort_then_finish_with_error_marks_error,
        test_abort_unknown_task,
        test_abort_already_finished,
        test_poll_cursor_replay,
        test_poll_future_cursor_resets_before_later_events_arrive,
        test_poll_after_done,
        test_poll_after_error,
        test_poll_unknown_task,
        test_poll_surfaces_model,
        test_NEUTER_poll_model_line_loadbearing,
        test_spawn_outside_event_loop,
        test_spawn_inside_event_loop,
        test_spawn_holds_strong_ref_under_gc,
        test_spawn_worker_crash_caught,
        test_abort_event_signal,
        test_cleanup_stale,
        test_stats,
        test_list_running,
        test_concurrent_append_events,
        test_push_integration,
        test_no_push_channel,
    ]

    for test_fn in tests:
        try:
            test_fn()
        except AssertionError as e:
            _fail(f'{test_fn.__name__}: {e}')
        except Exception as e:
            _fail(f'{test_fn.__name__}: unexpected {type(e).__name__}: {e}')

    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
