#!/usr/bin/env python3
"""Migration tests for lib/tasks_pkg/manager.py after TaskRuntime adoption.

This is the most critical migration — the chat orchestrator uses this
registry. We verify:
  - Module-level ``tasks`` / ``tasks_lock`` still alias the runtime's storage
  - ``create_task`` returns a dict with every legacy field
  - ``append_event`` preserves phase tracking, seq numbering, push integration
  - ``cleanup_old_tasks`` only purges finished + tracks _conv_latest_task index
  - ``abort_running_tasks_for_conv`` works against the runtime store
  - Cross-talk detection iteration over ``tasks`` continues to work
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Install Flask→Quart shim before importing routes
import quart as _quart
sys.modules['flask'] = _quart


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def test_runtime_and_aliases():
    from lib.tasks_pkg.manager import _chat_runtime, tasks, tasks_lock
    assert _chat_runtime is not None
    assert _chat_runtime.kind == 'chat'
    assert _chat_runtime.push_channel == 'chat'
    assert tasks is _chat_runtime._tasks
    assert tasks_lock is _chat_runtime._lock
    _ok('_chat_runtime created; tasks/tasks_lock alias internal storage')


def test_create_task_legacy_fields():
    from lib.tasks_pkg.manager import create_task, tasks
    task = create_task('cv-fields', [{'role': 'user', 'content': 'hello world'}],
                        {'model': 'gpt-4o', 'preset': 'low'})
    # Fields the orchestrator + 47 callers depend on
    expected = (
        'id', 'convId', 'messages', 'config',
        'status', 'content', 'thinking', 'error',
        'aborted', 'toolRounds', 'events',
        'events_lock', 'content_lock',
        'created_at', 'finishReason', 'usage', 'toolSummary',
        'phase', 'lastUserQuery', '_initial_msg_count',
        '_premature_retry_count_phase',
    )
    for f in expected:
        assert f in task, f'Missing legacy field: {f}'
    assert task['status'] == 'running'  # chat tasks start running, not pending
    assert task['content'] == ''
    assert task['thinking'] == ''
    assert task['toolRounds'] == []
    assert task['phase'] is None
    assert task['lastUserQuery'] == 'hello world'
    assert task['_initial_msg_count'] == 1
    # Both locks must be distinct objects (NOT aliases of each other)
    assert task['events_lock'] is not task['content_lock']
    # Task is registered in the unified runtime
    assert tasks.get(task['id']) is task
    _ok('create_task returns dict with all 21 legacy fields')


def test_create_task_extracts_user_query_from_multimodal():
    from lib.tasks_pkg.manager import create_task
    multimodal = [{
        'role': 'user',
        'content': [
            {'type': 'image_url', 'image_url': 'data:image/png;base64,xxx'},
            {'type': 'text', 'text': 'What is in this image?'},
        ],
    }]
    task = create_task('cv-mm', multimodal, {'model': 'x'})
    assert task['lastUserQuery'] == 'What is in this image?'
    _ok('create_task extracts text from multimodal user content')


def test_create_task_user_query_truncation():
    from lib.tasks_pkg.manager import create_task
    long_query = 'x' * 5000
    task = create_task('cv-trunc', [{'role': 'user', 'content': long_query}], {})
    assert len(task['lastUserQuery']) == 500  # truncated
    _ok('create_task truncates lastUserQuery to 500 chars')


def test_conv_latest_task_freshness_guard():
    from lib.tasks_pkg.manager import (create_task, _conv_latest_task,
                                          _conv_latest_task_lock)
    t1 = create_task('cv-fresh', [{'role': 'user', 'content': 'first'}], {})
    with _conv_latest_task_lock:
        assert _conv_latest_task.get('cv-fresh') == t1['id']

    # Starting a second task in the same conv should overwrite the index
    t2 = create_task('cv-fresh', [{'role': 'user', 'content': 'second'}], {})
    with _conv_latest_task_lock:
        assert _conv_latest_task.get('cv-fresh') == t2['id']
    assert t1['id'] != t2['id']
    _ok('_conv_latest_task index updated on every create_task')


def test_append_event_phase_tracking():
    from lib.tasks_pkg.manager import create_task, append_event
    task = create_task('cv-phase', [{'role': 'user', 'content': 'q'}], {})
    append_event(task, {
        'type': 'phase', 'phase': 'searching',
        'detail': 'web search', 'tools': ['web_search'], 'round': 1,
    })
    assert task['phase'] == {
        'phase': 'searching', 'detail': 'web search',
        'tools': ['web_search'], 'round': 1,
    }
    assert task['events'][0]['seq'] == 0
    _ok('append_event sets task["phase"] for type=phase events')


def test_append_event_delta_clears_phase():
    from lib.tasks_pkg.manager import create_task, append_event
    task = create_task('cv-delta', [{'role': 'user', 'content': 'q'}], {})
    append_event(task, {'type': 'phase', 'phase': 'thinking', 'detail': ''})
    assert task['phase'] is not None
    append_event(task, {'type': 'delta', 'content': 'hi'})
    assert task['phase'] is None  # cleared by delta
    assert len(task['events']) == 2
    _ok('append_event clears phase when type=delta arrives')


def test_append_event_seq_monotonic():
    from lib.tasks_pkg.manager import create_task, append_event
    task = create_task('cv-seq', [{'role': 'user', 'content': 'q'}], {})
    for i in range(20):
        append_event(task, {'type': 'tick', 'i': i})
    seqs = [e['seq'] for e in task['events']]
    assert seqs == list(range(20))
    _ok('append_event seq numbers are monotonic 0..N-1 (no duplicates)')


def test_append_event_thread_safe():
    from lib.tasks_pkg.manager import create_task, append_event
    task = create_task('cv-mt', [{'role': 'user', 'content': 'q'}], {})

    NUM_THREADS = 6
    EVENTS_PER_THREAD = 50

    def producer(tid_label):
        for i in range(EVENTS_PER_THREAD):
            append_event(task, {'type': 'progress',
                                 'thread': tid_label, 'i': i})

    threads = [threading.Thread(target=producer, args=(t,))
                for t in range(NUM_THREADS)]
    for t in threads: t.start()
    for t in threads: t.join()

    expected = NUM_THREADS * EVENTS_PER_THREAD
    assert len(task['events']) == expected
    seqs = [e['seq'] for e in task['events']]
    assert sorted(seqs) == list(range(expected))
    _ok(f'append_event is thread-safe ({NUM_THREADS} threads × {EVENTS_PER_THREAD} events)')


def test_append_event_legacy_dict_fallback():
    """Tasks inserted directly into ``tasks`` (test pattern) still get appended."""
    from lib.tasks_pkg.manager import append_event, tasks, tasks_lock
    legacy_task = {
        'id': 'legacy-dict-test',
        'events': [],
        'events_lock': threading.Lock(),
        'phase': None,
    }
    with tasks_lock:
        tasks['legacy-dict-test'] = legacy_task
    try:
        append_event(legacy_task, {'type': 'tick', 'i': 1})
        assert len(legacy_task['events']) == 1
        assert legacy_task['events'][0]['seq'] == 0
    finally:
        with tasks_lock:
            tasks.pop('legacy-dict-test', None)
    _ok('append_event falls back gracefully for legacy direct-insert tasks')


def test_abort_running_tasks_for_conv():
    from lib.tasks_pkg.manager import (create_task, abort_running_tasks_for_conv,
                                          tasks)
    t1 = create_task('cv-abort', [{'role': 'user', 'content': 'a'}], {})
    t2 = create_task('cv-abort', [{'role': 'user', 'content': 'b'}], {})
    t3 = create_task('cv-abort', [{'role': 'user', 'content': 'c'}], {})
    # All running, none aborted
    assert all(not t['aborted'] for t in (t1, t2, t3))

    # Abort all but t3
    n = abort_running_tasks_for_conv('cv-abort', exclude_task_id=t3['id'])
    assert n == 2
    assert t1['aborted'] is True
    assert t2['aborted'] is True
    assert t3['aborted'] is False
    assert t1.get('_abort_reason') == 'superseded_by_new_task'
    _ok('abort_running_tasks_for_conv works against runtime store')


def test_cleanup_old_tasks_purges_finished():
    from lib.tasks_pkg.manager import (create_task, cleanup_old_tasks,
                                          _chat_runtime, _conv_latest_task,
                                          _conv_latest_task_lock)
    # Use a tiny TTL for the test
    _chat_runtime.ttl = 0.05
    try:
        running = create_task('cv-cleanup-r', [{'role': 'user', 'content': 'r'}], {})
        finished = create_task('cv-cleanup-f', [{'role': 'user', 'content': 'f'}], {})

        # Mark `finished` as terminal with old finished_at so it qualifies
        finished['status'] = 'done'
        finished['finished_at'] = time.time() - 10  # 10s ago, well past 0.05s TTL

        time.sleep(0.1)  # also exceed TTL relative to created_at
        cleanup_old_tasks()

        assert _chat_runtime.get(running['id']) is not None  # running survives
        assert _chat_runtime.get(finished['id']) is None     # finished purged

        # Conv-latest-task entry for the cleaned task should be gone
        with _conv_latest_task_lock:
            assert 'cv-cleanup-f' not in _conv_latest_task
    finally:
        _chat_runtime.ttl = 3600
    _ok('cleanup_old_tasks purges finished + clears _conv_latest_task entry')


def test_cleanup_old_tasks_keeps_running_past_ttl():
    """A task that has been streaming for > TTL must NOT be cleaned up."""
    from lib.tasks_pkg.manager import (create_task, cleanup_old_tasks,
                                          _chat_runtime)
    _chat_runtime.ttl = 0.05
    try:
        long_running = create_task('cv-long', [{'role': 'user', 'content': 'q'}], {})
        # Even if created_at is ancient, status='running' means we keep it
        long_running['created_at'] = time.time() - 7200
        time.sleep(0.1)
        cleanup_old_tasks()
        assert _chat_runtime.get(long_running['id']) is not None
    finally:
        _chat_runtime.ttl = 3600
    _ok('cleanup_old_tasks preserves running tasks regardless of age')


def test_cross_talk_iteration():
    """The cross-talk detection in checkpoint_task_partial iterates `tasks` —
    must continue to work via the alias."""
    from lib.tasks_pkg.manager import create_task, tasks, tasks_lock
    cleanup_ids = []
    try:
        t1 = create_task('cv-x1', [{'role': 'user', 'content': 'a'}], {})
        t2 = create_task('cv-x2', [{'role': 'user', 'content': 'b'}], {})
        t3 = create_task('cv-x3', [{'role': 'user', 'content': 'c'}], {})
        cleanup_ids.extend([t1['id'], t2['id'], t3['id']])

        # Same iteration pattern used in checkpoint_task_partial
        with tasks_lock:
            running = [(tid[:8], t.get('convId', '')[:8])
                       for tid, t in tasks.items()
                       if t['status'] == 'running']
        # Should find at least our 3 (plus any from other tests still in registry)
        running_convs = {c for _, c in running}
        assert 'cv-x1' in running_convs
        assert 'cv-x2' in running_convs
        assert 'cv-x3' in running_convs
    finally:
        with tasks_lock:
            for tid in cleanup_ids:
                tasks.pop(tid, None)
    _ok('cross-talk-detection iteration over `tasks` works via the alias')


def test_push_channel_integration():
    """append_event auto-pushes to channel='chat'.

    The chat runtime (TaskRuntime) resolves push_event from its canonical
    home lib.agent_core.push after the 2026-06 leaf relocation, so the
    capture patch targets that module (lib.push remains a re-export shim).
    """
    from lib.agent_core import push as push_module
    from lib.tasks_pkg.manager import create_task, append_event
    received = []

    original = push_module.push_event
    def capture(channel, task_id, event):
        received.append((channel, task_id, event))
    push_module.push_event = capture
    try:
        task = create_task('cv-push', [{'role': 'user', 'content': 'q'}], {})
        append_event(task, {'type': 'delta', 'content': 'hi'})
        # Find the delta event push (filter out other events)
        delta_pushes = [(c, tid, e) for (c, tid, e) in received
                         if e.get('type') == 'delta' and tid == task['id']]
        assert len(delta_pushes) == 1
        ch, tid, evt = delta_pushes[0]
        assert ch == 'chat'
        assert tid == task['id']
        assert evt['content'] == 'hi'
    finally:
        push_module.push_event = original
    _ok('append_event auto-pushes events on channel="chat"')


def test_routes_chat_imports_still_work():
    """The 47 import sites use `from lib.tasks_pkg import tasks, tasks_lock` — verify."""
    from lib.tasks_pkg import tasks, tasks_lock, create_task, append_event
    from lib.tasks_pkg.manager import (
        tasks as mgr_tasks, tasks_lock as mgr_tasks_lock,
    )
    # Both import paths must yield the same object
    assert tasks is mgr_tasks
    assert tasks_lock is mgr_tasks_lock
    _ok('routes/chat.py-style imports (tasks, tasks_lock from lib.tasks_pkg) work')


def test_chat_streams_via_http_endpoints():
    """End-to-end: POST → SSE endpoint reads from runtime → poll endpoint reads from runtime."""
    # This test exercises chat task plumbing, not the auth gate, so flip
    # to ``open`` mode for its lifetime. The test reuses the
    # process-wide app instance built by conftest (which conftest pins
    # to ``private`` for the rest of the suite); switching modes via
    # the runtime API is enough — the auth middleware re-reads the
    # mode on every request.
    import importlib.util
    from lib import auth_mode as _auth_mode
    _prev_mode_env = os.environ.pop('TOFU_AUTH_MODE', None)
    _auth_mode.reset_for_tests()
    _auth_mode.set_mode('open', set_by='chat-stream-test')
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    app = mod.app

    import asyncio
    from lib.tasks_pkg.manager import create_task, append_event

    async def _t():
        task = create_task('cv-http', [{'role': 'user', 'content': 'q'}], {})

        async with app.test_client() as client:
            # poll endpoint should find the task
            r = await client.get(f'/api/chat/poll/{task["id"]}')
            assert r.status_code == 200, f'Got {r.status_code}'
            data = await r.get_json()
            assert data['status'] == 'running'
            assert data.get('content') == ''

            # Add some content via append_event
            append_event(task, {'type': 'delta', 'content': 'hello'})
            task['content'] = 'hello'

            r2 = await client.get(f'/api/chat/poll/{task["id"]}')
            data2 = await r2.get_json()
            assert data2['content'] == 'hello'

            # Mark done — stream endpoint should return immediately
            append_event(task, {'type': 'done', 'finishReason': 'stop'})
            task['status'] = 'done'
            task['finishReason'] = 'stop'

            r3 = await client.get(f'/api/chat/poll/{task["id"]}')
            data3 = await r3.get_json()
            assert data3['status'] == 'done'

    try:
        asyncio.run(_t())
    finally:
        # Restore conftest's mode lock for downstream tests.
        _auth_mode.reset_for_tests()
        if _prev_mode_env is not None:
            os.environ['TOFU_AUTH_MODE'] = _prev_mode_env
        else:
            os.environ['TOFU_AUTH_MODE'] = 'private'
        _auth_mode.reset_for_tests()
    _ok('HTTP /api/chat/poll/<id> end-to-end works against runtime-backed store')


def main():
    print()
    print(_color('═══ chat manager.py Migration Tests ═══', '36'))
    print()
    tests = [
        test_runtime_and_aliases,
        test_create_task_legacy_fields,
        test_create_task_extracts_user_query_from_multimodal,
        test_create_task_user_query_truncation,
        test_conv_latest_task_freshness_guard,
        test_append_event_phase_tracking,
        test_append_event_delta_clears_phase,
        test_append_event_seq_monotonic,
        test_append_event_thread_safe,
        test_append_event_legacy_dict_fallback,
        test_abort_running_tasks_for_conv,
        test_cleanup_old_tasks_purges_finished,
        test_cleanup_old_tasks_keeps_running_past_ttl,
        test_cross_talk_iteration,
        test_push_channel_integration,
        test_routes_chat_imports_still_work,
        test_chat_streams_via_http_endpoints,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} MIGRATION TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
