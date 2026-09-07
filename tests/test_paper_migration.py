#!/usr/bin/env python3
"""Paper TaskRuntime lifecycle, deduplication, polling, and abort contracts."""

import os
import sys
import time

TEST_OWNER_USER_ID = 1

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# Apply the conftest.py ``auth_mode`` marker module-wide so every test
# in this file runs with TOFU_AUTH_MODE=open. The live ``server.app``
# these tests use doesn't carry a Bearer token, and the conftest default
# of ``private`` would 401 every HTTP call. The marker is a no-op when
# the file is run standalone via ``python tests/test_paper_migration.py``.
try:
    import pytest
    pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('open')]
except ImportError:
    pass


# ─── Report tasks ────────────────────────────────────────────────

def test_report_runtime_created():
    from lib.paper.report_runtime import _report_runtime
    assert _report_runtime is not None
    assert _report_runtime.kind == 'paper-report'
    _ok('paper report runtime is the lifecycle authority')


def test_report_new_task_registers_dedup():
    from lib.paper.report_runtime import (
        _new_report_task,
        _report_index_get,
        _report_runtime,
    )
    tid = f'rpt_test_{int(time.time())}'
    task = _new_report_task(tid, 'phash-r-1', 'en', 'gpt-4o', user_id=TEST_OWNER_USER_ID)

    # Direct lookup
    assert _report_runtime.get(tid) is task
    # Dedup-index lookup
    found = _report_index_get('phash-r-1', 'en', user_id=TEST_OWNER_USER_ID)
    assert found is task
    # Different lang → not found
    assert _report_index_get('phash-r-1', 'zh', user_id=TEST_OWNER_USER_ID) is None
    _ok('_new_report_task registers (phash,lang) → task in dedup index')


def test_report_append_event():
    from lib.paper.report_runtime import (
        _new_report_task,
        _append_report_event,
    )
    tid = f'rpt_evt_{int(time.time())}'
    task = _new_report_task(tid, 'phash-r-2', 'en', 'gpt-4o', user_id=TEST_OWNER_USER_ID)
    _append_report_event(task, {'type': 'tool_start', 'roundNum': 1, 'toolName': 'web_search'})
    _append_report_event(task, {'type': 'delta', 'text': 'Hello'})
    assert len(task['events']) == 2
    assert task['events'][0]['seq'] == 0
    assert task['events'][1]['seq'] == 1
    assert task['events'][0]['toolName'] == 'web_search'
    _ok('_append_report_event preserves seq + payload')


def test_report_execution_fields_present():
    from lib.paper.report_runtime import _new_report_task
    task = _new_report_task('rpt_legacy', 'phash-r-3', 'en', 'gpt-4o', user_id=TEST_OWNER_USER_ID)
    # Report-engine fields live on the task snapshot.
    for f in ('task_id', 'paper_hash', 'lang', 'model', 'status',
               'finished_at', 'full_text', 'enriched_text',
               'tool_rounds', 'round_counter', 'events', 'events_lock',
               'abort_event'):
        assert f in task, f'Missing report field: {f}'
    assert task['status'] == 'pending'
    assert task['full_text'] == ''
    assert task['enriched_text'] == ''
    assert task['tool_rounds'] == []
    _ok('all report execution fields are present')


def test_report_cleanup_purges_finished():
    from lib.paper.report_runtime import (
        _new_report_task,
        _cleanup_stale_report_tasks,
        _report_runtime,
        _report_index_get,
    )
    tid = f'rpt_cleanup_{int(time.time())}'
    task = _new_report_task(tid, 'phash-r-cleanup', 'en', 'gpt-4o', user_id=TEST_OWNER_USER_ID)

    _report_runtime.finish(tid, result='done')
    original_ttl = _report_runtime.ttl
    _report_runtime.ttl = -1
    try:
        _cleanup_stale_report_tasks()
    finally:
        _report_runtime.ttl = original_ttl

    assert _report_runtime.get(tid) is None
    # Dedup index entry must also be removed
    assert _report_index_get('phash-r-cleanup', 'en', user_id=TEST_OWNER_USER_ID) is None
    _ok('cleanup purges finished task AND its dedup-index entry')


def test_report_cleanup_keeps_running():
    from lib.paper.report_runtime import (
        _new_report_task,
        _cleanup_stale_report_tasks,
        _report_runtime,
    )
    tid = f'rpt_keep_{int(time.time())}'
    task = _new_report_task(tid, 'phash-r-keep', 'en', 'gpt-4o', user_id=TEST_OWNER_USER_ID)
    _report_runtime.mark_running(tid)
    _cleanup_stale_report_tasks()
    assert _report_runtime.get(tid) is not None  # still alive
    _ok('cleanup keeps running tasks even past TTL')


def test_report_lookup_endpoint():
    """HTTP /api/paper/report/lookup finds task by (paper_hash, lang)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    app = mod.app

    import asyncio
    from lib.paper.report_runtime import (
        _new_report_task,
        _report_runtime,
    )

    async def _t():
        tid = f'rpt_lookup_{int(time.time() * 1000)}'
        task = _new_report_task(tid, 'phash-lookup-x', 'zh', 'opus', user_id=TEST_OWNER_USER_ID)
        _report_runtime.mark_running(tid)

        async with app.test_client() as client:
            r = await client.post('/api/v1/paper/report/lookup',
                                   json={'paper_hash': 'phash-lookup-x', 'lang': 'zh'})
            assert r.status_code == 200
            data = await r.get_json()
            assert data['ok'] is True
            assert data['task_id'] == tid
            assert data['status'] == 'running'

            # Different lang → ok=False
            r2 = await client.post('/api/v1/paper/report/lookup',
                                    json={'paper_hash': 'phash-lookup-x', 'lang': 'fr'})
            data2 = await r2.get_json()
            assert data2['ok'] is False

    asyncio.run(_t())
    _ok('HTTP /api/paper/report/lookup matches by (phash,lang)')


def test_report_poll_endpoint():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    app = mod.app

    import asyncio
    from lib.paper.report_runtime import (
        _append_report_event,
        _new_report_task,
        _report_runtime,
    )

    async def _t():
        tid = f'rpt_poll_{int(time.time() * 1000)}'
        task = _new_report_task(tid, 'phash-poll-y', 'en', 'opus', user_id=TEST_OWNER_USER_ID)
        _report_runtime.mark_running(tid)
        _append_report_event(task, {'type': 'delta', 'text': 'A'})
        _append_report_event(task, {'type': 'delta', 'text': 'B'})

        async with app.test_client() as client:
            r = await client.get(f'/api/v1/paper/report/poll?task_id={tid}&cursor=0')
            assert r.status_code == 200
            data = await r.get_json()
            assert data['ok'] is True
            assert data['status'] == 'running'
            assert len(data['events']) == 2
            assert data['next_cursor'] == 2
            assert data['paper_hash'] == 'phash-poll-y'

            # Cursor advances
            r2 = await client.get(f'/api/v1/paper/report/poll?task_id={tid}&cursor=2')
            data2 = await r2.get_json()
            assert len(data2['events']) == 0
            assert data2['next_cursor'] == 2

            # Unknown task → 404
            r3 = await client.get('/api/v1/paper/report/poll?task_id=no-such&cursor=0')
            assert r3.status_code == 404

    asyncio.run(_t())
    _ok('HTTP /api/paper/report/poll cursor + 404 paths work')


def test_report_abort_endpoint():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    app = mod.app

    import asyncio
    from lib.paper.report_runtime import (
        _new_report_task,
        _report_runtime,
    )

    async def _t():
        tid = f'rpt_abort_{int(time.time() * 1000)}'
        task = _new_report_task(tid, 'phash-abort-z', 'en', 'opus', user_id=TEST_OWNER_USER_ID)
        _report_runtime.mark_running(tid)

        async with app.test_client() as client:
            # Factory-minted route: task_id is a PATH segment (was a body key).
            r = await client.post(f'/api/v1/paper/report/abort/{tid}')
            assert r.status_code == 200
            assert task['abort_event'].is_set()

            # Unknown task → 404
            r2 = await client.post('/api/v1/paper/report/abort/no-such')
            assert r2.status_code == 404

    asyncio.run(_t())
    _ok('HTTP /api/paper/report/abort sets abort_event')


def test_all_factory_abort_routes_set_event():
    """The report/qa/translate ABORT routes are factory-minted
    (register_task_routes, enable_poll=False). Each must reach the RUNNING
    task and set its abort_event — the same event AbortSignal.from_event reads.

    NC (proven by hand): replacing ``ok = runtime.abort(task_id)`` with a
    falsey value in routes/_task_routes.py::_abort makes the abort no-op — the
    live task's abort_event is never set → this test FAILS (assert False on
    is_set()). Restoring it byte-identical → passes.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    app = mod.app

    import asyncio
    from lib.paper.qa_runtime import (
        _new_qa_task,
        _qa_runtime,
    )
    from lib.paper.report_runtime import (
        _new_report_task,
        _report_runtime,
    )
    from lib.paper.translate_runtime import (
        _new_translate_task,
        _translate_runtime,
    )

    cases = [
        ('report', _report_runtime,
         _new_report_task('rpt_fab', 'phash-fab-r', 'en', 'opus',
                          user_id=TEST_OWNER_USER_ID)),
        ('qa', _qa_runtime,
         _new_qa_task('qa_fab', 'phash-fab-q', 'en', 'opus',
                      question='q', user_id=TEST_OWNER_USER_ID)),
        ('translate', _translate_runtime,
         _new_translate_task('tr_fab', 'phash-fab-t', 'zh', 'opus',
                             user_id=TEST_OWNER_USER_ID)),
    ]

    async def _t():
        async with app.test_client() as client:
            for kind, runtime, task in cases:
                tid = task['task_id']
                runtime.mark_running(tid)
                # Factory route: task_id is a PATH segment.
                r = await client.post(f'/api/v1/paper/{kind}/abort/{tid}')
                assert r.status_code == 200, (kind, r.status_code)
                assert task['abort_event'].is_set(), \
                    f'{kind} abort did not reach the running task'
                # Unknown task → 404 through the same factory route.
                r2 = await client.post(f'/api/v1/paper/{kind}/abort/no-such-{kind}')
                assert r2.status_code == 404, (kind, r2.status_code)

    asyncio.run(_t())
    _ok('factory-minted report/qa/translate abort routes set abort_event (+404)')


# ─── Paper translate tasks ───────────────────────────────────────

def test_translate_runtime_created():
    from lib.paper.translate_runtime import _translate_runtime
    assert _translate_runtime is not None
    assert _translate_runtime.kind == 'paper-translate'
    _ok('paper translation runtime is the lifecycle authority')


def test_translate_new_task_execution_fields():
    from lib.paper.translate_runtime import _new_translate_task
    task = _new_translate_task('tr_test', 'phash-tr-1', 'zh', 'opus', user_id=TEST_OWNER_USER_ID)
    for f in ('task_id', 'paper_hash', 'lang', 'model', 'status',
               'finished_at', 'full_text', 'progress',
               'events', 'events_lock', 'abort_event'):
        assert f in task, f'Missing translate field: {f}'
    assert task['progress'] == {'done': 0, 'total': 0}
    _ok('paper-translate execution fields (full_text, progress) are present')


def test_translate_dedup_index():
    from lib.paper.translate_runtime import (
        _new_translate_task,
        _translate_index_get,
    )
    task = _new_translate_task('tr_dedup', 'phash-tr-2', 'zh', 'opus', user_id=TEST_OWNER_USER_ID)
    found = _translate_index_get('phash-tr-2', 'zh', user_id=TEST_OWNER_USER_ID)
    assert found is task
    assert _translate_index_get('phash-tr-2', 'en', user_id=TEST_OWNER_USER_ID) is None
    _ok('paper-translate dedup index works')


def test_translate_lookup_endpoint():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    app = mod.app

    import asyncio
    from lib.paper.translate_runtime import (
        _new_translate_task,
        _translate_runtime,
    )

    async def _t():
        tid = f'tr_lookup_{int(time.time() * 1000)}'
        task = _new_translate_task(tid, 'phash-tr-look', 'ja', 'opus', user_id=TEST_OWNER_USER_ID)
        _translate_runtime.mark_running(tid)

        async with app.test_client() as client:
            r = await client.post('/api/v1/paper/translate/lookup',
                                   json={'paper_hash': 'phash-tr-look', 'lang': 'ja'})
            assert r.status_code == 200
            data = await r.get_json()
            assert data['ok'] is True
            assert data['task_id'] == tid

    asyncio.run(_t())
    _ok('HTTP /api/paper/translate/lookup works')


def test_translate_composite_lang_task_id_is_url_safe():
    """A composite review key ('review:neurips:zh') must NOT leak its colons
    into the minted task_id — the id is echoed verbatim into the poll/abort URL
    and colons arrive DOUBLE-ENCODED ('%253A') over a proxy tunnel, so the poll
    404s forever and the UI reports "translation failed". The real composite
    `lang` must still key the dedup index unchanged.

    NC (proven by hand): reverting the mint back to
    ``task_id = f'tr_..._{lang}'`` in routes/paper.py::start_translate_task
    puts a ':' in the id → the url_safe assertion FAILS and an immediate
    same-id poll 404s. The sanitized-slug mint → passes.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    app = mod.app

    import asyncio
    from urllib.parse import quote
    from lib.paper.translate_runtime import (
        _translate_runtime,
        _translate_index_get,
    )

    async def _t():
        composite = 'review:neurips:zh'
        async with app.test_client() as client:
            r = await client.post('/api/v1/paper/translate/start',
                                   json={'paper_text': 'Hello world.',
                                         'lang': composite,
                                         'paper_hash': 'phash-tr-composite',
                                         'force': True})
            assert r.status_code == 200, r.status_code
            data = await r.get_json()
            tid = data['task_id']
            # Immediately abort so the background worker doesn't do real LLM work.
            task = _translate_runtime.get(tid)
            if task:
                task['abort_event'].set()

            # (1) The id must be URL-safe: no ':' and thus nothing to double-encode.
            assert ':' not in tid, f'task_id leaked colons: {tid!r}'
            assert quote(tid, safe='') == tid, f'task_id not URL-safe: {tid!r}'

            # (2) The REAL composite lang still keys the dedup index.
            assert _translate_index_get('phash-tr-composite', composite, user_id=TEST_OWNER_USER_ID) is not None

            # (3) The exact same id round-trips through the poll URL → NOT 404
            #     (this is the failure the user hit: poll 404 → "translation failed").
            pr = await client.get(f'/api/v1/paper/translate/poll?task_id={quote(tid, safe="")}&cursor=0')
            assert pr.status_code == 200, f'poll {pr.status_code} — id not found round-tripping through URL'

    asyncio.run(_t())
    _ok('composite review lang mints a URL-safe task_id (no double-encode 404)')


def test_translate_poll_endpoint():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    app = mod.app

    import asyncio
    from lib.paper.translate_runtime import (
        _append_translate_event,
        _new_translate_task,
        _translate_runtime,
    )

    async def _t():
        tid = f'tr_poll_{int(time.time() * 1000)}'
        task = _new_translate_task(tid, 'phash-tr-poll', 'zh', 'opus', user_id=TEST_OWNER_USER_ID)
        _translate_runtime.mark_running(tid)
        _translate_runtime.update_fields(
            tid, fields={'progress': {'done': 1, 'total': 5}})
        _append_translate_event(task, {'type': 'chunk', 'index': 0, 'total': 5,
                                        'preview': 'hello'})

        async with app.test_client() as client:
            r = await client.get(f'/api/v1/paper/translate/poll?task_id={tid}&cursor=0')
            assert r.status_code == 200
            data = await r.get_json()
            assert data['status'] == 'running'
            assert data['progress'] == {'done': 1, 'total': 5}
            assert len(data['events']) == 1
            assert data['next_cursor'] == 1

    asyncio.run(_t())
    _ok('HTTP /api/paper/translate/poll preserves progress + events shape')


def main():
    print()
    print(_color('═══ paper.py Migration Tests ═══', '36'))
    print()
    tests = [
        test_report_runtime_created,
        test_report_new_task_registers_dedup,
        test_report_append_event,
        test_report_execution_fields_present,
        test_report_cleanup_purges_finished,
        test_report_cleanup_keeps_running,
        test_report_lookup_endpoint,
        test_report_poll_endpoint,
        test_report_abort_endpoint,
        test_all_factory_abort_routes_set_event,
        test_translate_runtime_created,
        test_translate_new_task_execution_fields,
        test_translate_dedup_index,
        test_translate_composite_lang_task_id_is_url_safe,
        test_translate_lookup_endpoint,
        test_translate_poll_endpoint,
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
