#!/usr/bin/env python3
"""Integration test: trading_simulator migration to TaskRuntime.

Verifies the legacy response shape is preserved end-to-end after the
migration so the frontend (static/js/trading/simulator.js) continues
to work without changes.

Specifically tests:
  - _create_task() → returns task_id string
  - _append_event() → events show up in poll
  - _finish_task() with result, exception, str, dict envelope
  - _get_task_progress() returns the legacy shape:
      {events, cursor, done, task_type, [result|error]}
  - 'Task not found' error string for missing tasks
  - cleanup_old_tasks works
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Install Flask→Quart shim before importing routes (matches server.py runtime)
import quart as _quart  # noqa: E402
sys.modules['flask'] = _quart


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def test_create_returns_task_id():
    from routes.trading_simulator import _create_task
    tid = _create_task('fetch')
    assert isinstance(tid, str) and len(tid) > 0
    _ok('_create_task("fetch") returns task_id string')


def test_create_distinct_kinds():
    from routes.trading_simulator import _create_task
    t1 = _create_task('fetch')
    t2 = _create_task('sim')
    assert t1 != t2
    _ok('_create_task() returns unique IDs for fetch + sim')


def test_append_and_poll_returns_events():
    from routes.trading_simulator import _create_task, _append_event, _get_task_progress
    tid = _create_task('fetch')
    _append_event(tid, {'phase': 'prices', 'done': 1, 'total': 10})
    _append_event(tid, {'phase': 'prices', 'done': 5, 'total': 10})

    resp = _get_task_progress(tid, cursor=0)
    assert resp['events'] is not None
    assert len(resp['events']) == 2
    assert resp['events'][0]['phase'] == 'prices'
    assert resp['cursor'] == 2
    assert resp['done'] is False
    assert resp['task_type'] == 'fetch'
    _ok('append + poll returns events with cursor=2, done=False, task_type=fetch')


def test_poll_cursor_replay():
    from routes.trading_simulator import _create_task, _append_event, _get_task_progress
    tid = _create_task('sim')
    for i in range(5):
        _append_event(tid, {'i': i})

    r0 = _get_task_progress(tid, cursor=0)
    assert len(r0['events']) == 5
    assert r0['cursor'] == 5

    _append_event(tid, {'i': 5})
    r1 = _get_task_progress(tid, cursor=5)
    assert len(r1['events']) == 1
    assert r1['events'][0]['i'] == 5
    assert r1['cursor'] == 6
    _ok('cursor-based replay works (5 + 1 events, cursor advances)')


def test_finish_with_result():
    from routes.trading_simulator import _create_task, _append_event, _finish_task, _get_task_progress
    tid = _create_task('fetch')
    _append_event(tid, {'phase': 'prices', 'done': 10, 'total': 10})
    _finish_task(tid, result={'rows': 100, 'symbols': 5})

    resp = _get_task_progress(tid, cursor=0)
    assert resp['done'] is True
    assert resp['result'] == {'rows': 100, 'symbols': 5}
    assert 'error' not in resp
    _ok('_finish_task(result=) → done=True with result')


def test_finish_with_string_error():
    from routes.trading_simulator import _create_task, _finish_task, _get_task_progress
    tid = _create_task('sim')
    _finish_task(tid, error='database connection failed')

    resp = _get_task_progress(tid, cursor=0)
    assert resp['done'] is True
    assert resp['error'] is not None
    # Envelope structure
    assert isinstance(resp['error'], dict)
    assert resp['error'].get('detail') == 'database connection failed'
    _ok('_finish_task(error="str") → wraps in envelope dict')


def test_finish_with_exception():
    from routes.trading_simulator import _create_task, _finish_task, _get_task_progress
    tid = _create_task('sim')
    try:
        raise ValueError('fetch_data crashed')
    except ValueError as e:
        _finish_task(tid, error=e,
                     error_context='trading-sim:fetch',
                     error_source='routes.trading_simulator:fetch')

    resp = _get_task_progress(tid, cursor=0)
    assert resp['done'] is True
    assert resp['error'] is not None
    assert isinstance(resp['error'], dict)
    assert 'fetch_data crashed' in str(resp['error'])
    _ok('_finish_task(error=Exception) → wraps in envelope, preserves message')


def test_finish_with_dict_envelope():
    from routes.trading_simulator import _create_task, _finish_task, _get_task_progress
    tid = _create_task('fetch')
    envelope = {'kind': 'rate_limit', 'detail': 'too many requests',
                'source': 'test'}
    _finish_task(tid, error=envelope)

    resp = _get_task_progress(tid, cursor=0)
    assert resp['error']['kind'] == 'rate_limit'
    assert resp['error']['detail'] == 'too many requests'
    _ok('_finish_task(error=dict) → preserves existing envelope')


def test_unknown_task_returns_legacy_string_error():
    from routes.trading_simulator import _get_task_progress
    resp = _get_task_progress('does-not-exist', cursor=0)
    # Frontend matches: if (resp.error === 'Task not found') ...
    assert resp['error'] == 'Task not found'
    assert resp['done'] is True
    assert resp['events'] == []
    _ok('unknown task → legacy {error: "Task not found", done: True}')


def test_terminal_event_emitted():
    """TaskRuntime emits a synthetic terminal event — verify it appears in poll."""
    from routes.trading_simulator import _create_task, _append_event, _finish_task, _get_task_progress
    tid = _create_task('sim')
    _append_event(tid, {'_type': 'progress', 'step': 1})
    _finish_task(tid, result={'session_id': 'sess-1'})

    resp = _get_task_progress(tid, cursor=0)
    # Should include user events PLUS terminal 'done' event
    types = [e.get('type') or e.get('_type') for e in resp['events']]
    assert 'progress' in types
    assert 'done' in types
    _ok('terminal "done" event auto-emitted in event stream')


def test_http_endpoints_via_test_client():
    """Hit the actual HTTP routes to verify end-to-end (when trading enabled)."""
    import lib as _lib
    if not getattr(_lib, 'TRADING_ENABLED', False):
        print('  ', _color('~', '33'), 'HTTP test skipped (TRADING_ENABLED=False)')
        return

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    app = mod.app

    import asyncio

    async def _test():
        async with app.test_client() as client:
            from routes.trading_simulator import _create_task, _append_event, _finish_task

            tid = _create_task('fetch')
            _append_event(tid, {'phase': 'prices', 'done': 1, 'total': 5})
            _finish_task(tid, result={'rows': 100})

            r = await client.get(f'/api/trading/sim/fetch-progress/{tid}?cursor=0')
            assert r.status_code == 200
            data = await r.get_json()
            assert data['done'] is True
            assert data['result'] == {'rows': 100}
            assert data['cursor'] >= 1
            assert data['task_type'] == 'fetch'
            _ok('HTTP /api/trading/sim/fetch-progress/<id> returns expected shape')

            r2 = await client.get('/api/trading/sim/fetch-progress/no-such-id?cursor=0')
            assert r2.status_code == 404
            data2 = await r2.get_json()
            assert data2['error'] == 'Task not found'
            _ok('HTTP unknown-task → 404 with {error: "Task not found"}')

            tid2 = _create_task('sim')
            _append_event(tid2, {'_type': 'step', 'i': 1})
            r3 = await client.get(f'/api/trading/sim/run-progress/{tid2}?cursor=0')
            assert r3.status_code == 200
            data3 = await r3.get_json()
            assert data3['done'] is False
            assert data3['task_type'] == 'sim'
            assert len(data3['events']) >= 1
            _ok('HTTP /api/trading/sim/run-progress/<id> works for sim kind')

    asyncio.run(_test())


def test_cleanup_runs_without_error():
    from routes.trading_simulator import _runtime
    n_before = _runtime.task_count
    _runtime.cleanup_stale()  # Should not crash
    n_after = _runtime.task_count
    assert n_after <= n_before  # Either same (none expired) or fewer
    _ok('_runtime.cleanup_stale() runs without error')


def main():
    print()
    print(_color('═══ trading_simulator Migration Tests ═══', '36'))
    print()

    tests = [
        test_create_returns_task_id,
        test_create_distinct_kinds,
        test_append_and_poll_returns_events,
        test_poll_cursor_replay,
        test_finish_with_result,
        test_finish_with_string_error,
        test_finish_with_exception,
        test_finish_with_dict_envelope,
        test_unknown_task_returns_legacy_string_error,
        test_terminal_event_emitted,
        test_http_endpoints_via_test_client,
        test_cleanup_runs_without_error,
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
