#!/usr/bin/env python3
"""Migration tests for routes/translate.py after TaskRuntime adoption.

Verifies the legacy poll response shape is preserved end-to-end so the
frontend (translation.js, paper-reader.js) continues to work unchanged.

Specifically:
  - taskId, status, translated, model, error, progress, statusMessage, partial
  - 'Task not found' string (404) for missing tasks
  - poll_batch returns matching list shape
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Install Flask→Quart shim before importing routes (matches server.py)
import quart as _quart
sys.modules['flask'] = _quart


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def test_runtime_created():
    from routes.translate import _translate_runtime, _translate_tasks
    assert _translate_runtime is not None
    # _translate_tasks must point at the runtime's internal dict (compatibility)
    assert _translate_tasks is _translate_runtime._tasks
    _ok('_translate_runtime created; _translate_tasks shim points at internal dict')


def test_create_task_via_runtime():
    from routes.translate import _translate_runtime
    task = _translate_runtime.create(
        meta={'convId': 'c1', 'msgIdx': 0, 'targetLang': 'English', 'textLen': 100},
    )
    task.update({
        'status': 'running', 'result': None, 'error': None,
        'model': None, 'progress': None,
        'convId': 'c1', 'msgIdx': 0, 'msgId': None, 'field': 'translatedContent',
        'targetLang': 'English', 'textLen': 100,
        'completed_at': None,
    })
    found = _translate_runtime.get(task['id'])
    assert found is task
    assert found['status'] == 'running'
    assert found['convId'] == 'c1'
    _ok('runtime.create() + custom fields preserved')


def test_partial_and_status_fields_writable():
    """Verify the mutable fields that translation.js polls for still work."""
    from routes.translate import _translate_runtime
    task = _translate_runtime.create()
    task['status'] = 'running'
    task['progress'] = '3/5'
    task['statusMessage'] = '⏳ Retrying due to 429…'
    task['statusKind'] = 'rate_limit'
    task['partial'] = '部分翻译...'
    task['partialUpdatedAt'] = time.time()

    # Write through the events_lock (mirrors what _do_translate does)
    with task['events_lock']:
        task['progress'] = '4/5'

    assert task['progress'] == '4/5'
    assert task['statusMessage'] == '⏳ Retrying due to 429…'
    assert task['partial'] == '部分翻译...'
    _ok('mutable fields (progress, statusMessage, partial) writable via events_lock')


def test_done_state_with_result():
    from routes.translate import _translate_runtime
    task = _translate_runtime.create()
    task.update({'status': 'running', 'result': None, 'completed_at': None})

    with task['events_lock']:
        task['status'] = 'done'
        task['result'] = 'Hello world'
        task['model'] = 'gpt-4o'
        task['completed_at'] = time.time()

    found = _translate_runtime.get(task['id'])
    assert found['status'] == 'done'
    assert found['result'] == 'Hello world'
    assert found['model'] == 'gpt-4o'
    _ok('done state with result + model preserved')


def test_error_state_with_envelope():
    """Translation uses a typed envelope dict for errors (per recent migration)."""
    from routes.translate import _translate_runtime
    from lib.error_envelope import make_envelope as _make_env
    task = _translate_runtime.create()
    task.update({'status': 'running', 'error': None, 'completed_at': None})

    envelope = _make_env('generic', detail='boom', context='translate',
                          source='routes.translate', raw='boom')
    with task['events_lock']:
        task['status'] = 'error'
        task['error'] = envelope
        task['completed_at'] = time.time()

    assert task['status'] == 'error'
    assert isinstance(task['error'], dict)
    assert task['error']['detail'] == 'boom'
    _ok('error envelope (dict) preserved as task["error"]')


def test_cleanup_removes_finished_only():
    """cleanup_translate_tasks should drop done/error past TTL but keep running."""
    from routes.translate import _translate_runtime, _cleanup_translate_tasks
    # Use a tiny TTL for this test
    _translate_runtime.ttl = 0.05
    try:
        t1 = _translate_runtime.create()
        t1['status'] = 'running'
        t2 = _translate_runtime.create()
        # Mark t2 as terminal via the runtime (so finished_at is set)
        _translate_runtime.finish(t2['id'], result='ok')
        time.sleep(0.1)

        n_before = _translate_runtime.task_count
        _cleanup_translate_tasks()
        n_after = _translate_runtime.task_count

        assert n_after < n_before  # at least t2 removed
        assert _translate_runtime.get(t1['id']) is not None  # running survives
        assert _translate_runtime.get(t2['id']) is None      # done purged
    finally:
        _translate_runtime.ttl = 1800
    _ok('cleanup_translate_tasks() purges finished, keeps running')


def test_poll_endpoint_done_shape():
    """HTTP /api/translate/poll/<id> returns legacy shape on done."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    app = mod.app

    import asyncio
    from routes.translate import _translate_runtime

    async def _t():
        task = _translate_runtime.create()
        task.update({'status': 'running', 'result': None, 'error': None,
                      'completed_at': None, 'model': None, 'progress': None})
        with task['events_lock']:
            task['status'] = 'done'
            task['result'] = 'translated text'
            task['model'] = 'gpt-4o'
            task['completed_at'] = time.time()

        async with app.test_client() as client:
            r = await client.get(f'/api/v1/translate/poll/{task["id"]}')
            assert r.status_code == 200
            data = await r.get_json()
            assert data['taskId'] == task['id']
            assert data['status'] == 'done'
            assert data['translated'] == 'translated text'
            assert data['model'] == 'gpt-4o'

    asyncio.run(_t())
    _ok('HTTP /api/translate/poll/<id> done returns {taskId,status,translated,model}')


def test_poll_endpoint_running_with_partial():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    app = mod.app

    import asyncio
    from routes.translate import _translate_runtime

    async def _t():
        task = _translate_runtime.create()
        task.update({
            'status': 'running', 'result': None, 'error': None,
            'progress': '2/5', 'statusMessage': 'retry 1', 'statusKind': 'rate_limit',
            'partial': '中间结果...',
            'completed_at': None, 'model': None,
        })

        async with app.test_client() as client:
            r = await client.get(f'/api/v1/translate/poll/{task["id"]}')
            assert r.status_code == 200
            data = await r.get_json()
            assert data['status'] == 'running'
            assert data['progress'] == '2/5'
            assert data['statusMessage'] == 'retry 1'
            assert data['statusKind'] == 'rate_limit'
            assert data['partial'] == '中间结果...'

    asyncio.run(_t())
    _ok('HTTP poll running shape includes progress/statusMessage/partial')


def test_poll_endpoint_unknown_task():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    app = mod.app

    import asyncio

    async def _t():
        async with app.test_client() as client:
            r = await client.get('/api/v1/translate/poll/no-such-task-xyz')
            assert r.status_code == 404
            data = await r.get_json()
            assert data['error'] == 'Task not found'
            assert data['status'] == 'not_found'

    asyncio.run(_t())
    _ok('HTTP poll unknown task → 404 {error: "Task not found", status: "not_found"}')


def test_poll_batch_shape():
    """poll_batch returns a list of {taskId, status, ...} matching frontend expectations."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    app = mod.app

    import asyncio
    from routes.translate import _translate_runtime

    async def _t():
        # Two tasks — one done, one running
        t1 = _translate_runtime.create()
        t1.update({'status': 'running', 'result': None})
        with t1['events_lock']:
            t1['status'] = 'done'
            t1['result'] = 'first result'
            t1['model'] = 'm1'
            t1['completed_at'] = time.time()

        t2 = _translate_runtime.create()
        t2.update({'status': 'running', 'progress': '1/3', 'partial': 'part'})

        async with app.test_client() as client:
            r = await client.post('/api/v1/translate/poll-batch',
                                   json={'taskIds': [t1['id'], t2['id'], 'missing-id']})
            assert r.status_code == 200
            results = (await r.get_json())['items']  # {ok, items} envelope
            assert isinstance(results, list)
            assert len(results) == 3

            by_id = {x.get('taskId') or 'missing': x for x in results}
            # First — done
            r1 = by_id.get(t1['id'])
            assert r1['status'] == 'done'
            assert r1['translated'] == 'first result'
            # Second — running
            r2 = by_id.get(t2['id'])
            assert r2['status'] == 'running'
            assert r2['progress'] == '1/3'
            assert r2['partial'] == 'part'
            # Third — missing → status='not_found' inline (no taskId)
            assert any(r.get('status') == 'not_found' for r in results)

    asyncio.run(_t())
    _ok('HTTP /api/translate/poll_batch returns matching shape')


def main():
    print()
    print(_color('═══ translate.py Migration Tests ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_translate_migration.__main__')

    tests = [
        test_runtime_created,
        test_create_task_via_runtime,
        test_partial_and_status_fields_writable,
        test_done_state_with_result,
        test_error_state_with_envelope,
        test_cleanup_removes_finished_only,
        test_poll_endpoint_done_shape,
        test_poll_endpoint_running_with_partial,
        test_poll_endpoint_unknown_task,
        test_poll_batch_shape,
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
