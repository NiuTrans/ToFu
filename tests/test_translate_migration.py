#!/usr/bin/env python3
"""Translation TaskRuntime lifecycle and HTTP polling contracts.

Verifies that translation state changes only through TaskRuntime and that the
frontend polling projection exposes the canonical task fields end to end.

Specifically:
  - taskId, status, translated, model, error, progress, statusMessage, partial
  - 'Task not found' string (404) for missing tasks
  - poll_batch returns matching list shape
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


pytestmark = pytest.mark.unit

def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def test_runtime_created():
    from lib.translate import _translate_runtime
    assert _translate_runtime is not None
    assert _translate_runtime.kind == 'translate'
    assert _translate_runtime.push_channel == 'translate'
    _ok('translation runtime owns lifecycle and push authority')


def test_create_task_via_runtime():
    from lib.translate import _translate_runtime
    task = _translate_runtime.create(user_id=1,
        meta={'convId': 'c1', 'msgIdx': 0, 'targetLang': 'English', 'textLen': 100},
    )
    assert _translate_runtime.mark_running(
        task['id'], fields={'model': None, 'progress': None})
    found = _translate_runtime.get(task['id'])
    assert found is task
    assert found['status'] == 'running'
    assert found['meta']['convId'] == 'c1'
    _ok('runtime creation metadata and running transition are canonical')


def test_partial_and_status_fields_writable():
    """Verify the mutable fields that translation.js polls for still work."""
    from lib.translate import _translate_runtime
    task = _translate_runtime.create(user_id=1)
    _translate_runtime.mark_running(task['id'])
    assert _translate_runtime.update_fields(task['id'], fields={
        'progress': '4/5',
        'statusMessage': '⏳ Retrying due to 429…',
        'statusKind': 'rate_limit',
        'partial': '部分翻译...',
        'partialUpdatedAt': time.time(),
    }, only_if_status='running')

    assert task['progress'] == '4/5'
    assert task['statusMessage'] == '⏳ Retrying due to 429…'
    assert task['partial'] == '部分翻译...'
    _ok('translation presentation fields update atomically through TaskRuntime')


def test_done_state_with_result():
    from lib.translate import _translate_runtime
    task = _translate_runtime.create(user_id=1)
    _translate_runtime.mark_running(task['id'])
    _translate_runtime.update_fields(task['id'], fields={'model': 'gpt-4o'})
    assert _translate_runtime.finish(task['id'], result='Hello world')

    found = _translate_runtime.get(task['id'])
    assert found['status'] == 'done'
    assert found['result'] == 'Hello world'
    assert found['model'] == 'gpt-4o'
    _ok('done state with result + model preserved')


def test_error_state_with_envelope():
    """Translation uses a typed envelope dict for errors (per recent migration)."""
    from lib.translate import _translate_runtime
    from lib.error_envelope import make_envelope as _make_env
    task = _translate_runtime.create(user_id=1)
    _translate_runtime.mark_running(task['id'])

    envelope = _make_env('generic', detail='boom', context='translate',
                          source='routes.translate', raw='boom')
    assert _translate_runtime.finish(
        task['id'], error=envelope, error_context='translate')

    assert task['status'] == 'error'
    assert isinstance(task['error'], dict)
    assert task['error']['detail'] == 'boom'
    _ok('error envelope (dict) preserved as task["error"]')


def test_cleanup_removes_finished_only():
    """cleanup_translate_tasks should drop done/error past TTL but keep running."""
    from lib.translate import _translate_runtime, _cleanup_translate_tasks
    # Use a tiny TTL for this test
    _translate_runtime.ttl = 0.05
    try:
        t1 = _translate_runtime.create(user_id=1)
        _translate_runtime.mark_running(t1['id'])
        t2 = _translate_runtime.create(user_id=1)
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
    """HTTP translation polling projects a completed runtime task."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    app = mod.app

    import asyncio
    from lib.translate import _translate_runtime

    async def _t():
        task = _translate_runtime.create(user_id=1)
        _translate_runtime.mark_running(task['id'])
        _translate_runtime.update_fields(task['id'], fields={'model': 'gpt-4o'})
        _translate_runtime.finish(task['id'], result='translated text')

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
    from lib.translate import _translate_runtime

    async def _t():
        task = _translate_runtime.create(user_id=1)
        _translate_runtime.mark_running(task['id'], fields={
            'progress': '2/5', 'statusMessage': 'retry 1', 'statusKind': 'rate_limit',
            'partial': '中间结果...', 'model': None,
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
    from lib.translate import _translate_runtime

    async def _t():
        # Two tasks — one done, one running
        t1 = _translate_runtime.create(user_id=1)
        _translate_runtime.mark_running(t1['id'])
        _translate_runtime.update_fields(t1['id'], fields={'model': 'm1'})
        _translate_runtime.finish(t1['id'], result='first result')

        t2 = _translate_runtime.create(user_id=1)
        _translate_runtime.mark_running(
            t2['id'], fields={'progress': '1/3', 'partial': 'part'})

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
    print(_color('═══ Translation Runtime Contract Tests ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_storage
    guard_standalone_storage('test_translate_migration.__main__')

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
    print(_color(f'═══ ALL {len(tests)} CONTRACT TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
