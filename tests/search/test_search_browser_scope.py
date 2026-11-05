"""Search/fetch fan-out carries one explicit browser decision across workers."""

from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
from threading import Lock
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('tool', ['fetch_url', 'web_search'])
@pytest.mark.parametrize('batch', [False, True])
@pytest.mark.parametrize('selection', ['offline_pinned', 'initially_empty', 'profile_changed'])
def test_streaming_workers_keep_initial_browser_decision(monkeypatch, tool, batch, selection):
    from lib.browser import queue
    from lib.search import search_bridge
    from lib.tasks_pkg.handlers.search import _core
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator

    lookups, observed = [], []
    lock = Lock()
    ready = dict(client_id='other-browser', profile='Other', last_poll=2, capabilities=[])
    pinned = dict(client_id='pinned-browser', profile='Original', last_poll=1, capabilities=[])

    def clients(*, owner_user_id):
        with lock:
            lookups.append(owner_user_id)
            if selection == 'offline_pinned':
                return [ready]
            if selection == 'initially_empty':
                return [] if len(lookups) == 1 else [ready]
            return [dict(pinned, profile='Original' if len(lookups) == 1 else 'Changed')]

    monkeypatch.setattr(queue, 'get_connected_clients', clients)

    def record_binding():
        provider = search_bridge._ChatuiBrowserProvider().bind()
        site_provider = search_bridge._ChatuiSiteSearchProvider().bind()
        assert (provider.user_id, provider.client_id, provider.profile) == (
            site_provider.user_id, site_provider.client_id, site_provider.profile)
        with lock:
            observed.append((provider.user_id, provider.client_id, provider.profile))

    def fetch(url, question):
        record_binding()
        return {'url': url, 'page_content': 'ok', 'raw_chars': 2,
                'filtered_chars': 2, 'is_pdf': False, 'error_msg': None}

    def search(query, *args, **kwargs):
        record_binding()
        return [], {'reason': 'offline fixture'}, None, None

    monkeypatch.setattr(_core, '_fetch_url_one', fetch)
    monkeypatch.setattr(_core, '_web_search_one', search)
    task = {'id': 'scope-spec', '_userId': '41', 'aborted': False,
            'lastUserQuery': 'fixture', '_tool_result_cache': {},
            'config': {'browserClientId': 'pinned-browser'
                       if selection != 'initially_empty' else ''}}
    args = ({'urls': ['https://example.test/one', 'https://example.test/two']}
            if batch else {'url': 'https://example.test/one'}) if tool == 'fetch_url' else (
                {'queries': ['one', 'two']} if batch else {'query': 'one'})

    result = StreamingToolAccumulator(task, project_path='/tmp')._execute_one(tool, args)

    expected = ('41', 'pinned-browser', 'Original') if selection == 'profile_changed' else ('41', '', '')
    assert observed == [expected] * (2 if batch else 1), str(result)
    assert lookups == ['41'], 'workers must activate the captured decision without resolving again'
    assert search_bridge._ChatuiBrowserProvider().bind().user_id == ''


@pytest.mark.parametrize('owner', [True, False, 1.0, Decimal('1'), b'1', 0, -1])
def test_invalid_browser_owner_is_refused_before_registry_lookup(monkeypatch, owner):
    from lib.browser import queue
    from lib.search.search_bridge import bind_search_browser

    looked_up = []
    monkeypatch.setattr(queue, 'get_connected_clients',
                        lambda **kwargs: looked_up.append(kwargs) or [])
    with pytest.raises(ValueError):
        with bind_search_browser(user_id=owner):
            pytest.fail('invalid owner reached browser scope')
    assert looked_up == []


def test_scope_import_does_not_activate_optional_search_or_browser_execution():
    subprocess.run([sys.executable, '-c', """
import sys
import lib.search.browser_scope
assert 'tofu_search' not in sys.modules
assert 'lib.browser.queue' not in sys.modules
assert 'lib.browser.file_transfer' not in sys.modules
"""], check=True, capture_output=True, text=True)


@pytest.mark.parametrize('owner', [None, ''])
def test_absent_owner_is_inert_without_global_registry_lookup(monkeypatch, owner):
    from lib.browser import queue
    from lib.search.browser_scope import bind_search_browser

    monkeypatch.setattr(queue, 'get_connected_clients',
                        lambda **kwargs: pytest.fail('absent owner must be inert'))
    with bind_search_browser(user_id=owner) as binding:
        assert binding == ('', '', '')


def test_selection_failure_stays_absent_when_activated_again(monkeypatch):
    from lib.browser import queue
    from lib.search.browser_scope import current_browser_identity, select_search_browser

    calls = []

    def unavailable(**kwargs):
        calls.append(kwargs)
        raise RuntimeError('registry unavailable')

    monkeypatch.setattr(queue, 'get_connected_clients', unavailable)
    binding = select_search_browser(user_id=' +41 ', client_id='pinned')
    with pytest.raises(AttributeError):
        binding.client_id = 'another-browser'
    for _ in range(2):
        with binding.activate():
            assert current_browser_identity() == ('41', '', '')
        assert current_browser_identity() == ('', '', '')
    assert calls == [{'owner_user_id': '41'}]


def test_parallel_snapshot_activations_own_their_file_handoffs(monkeypatch):
    from threading import Barrier
    from lib.browser import file_transfer
    from lib.search.browser_scope import (
        SearchBrowserBinding, claim_bound_browser_file, current_browser_identity,
        remember_bound_browser_file,
    )

    receipts, lock, together = [], Lock(), Barrier(2)
    binding = SearchBrowserBinding('41', 'browser-a', 'Work')

    def settle(action, transfer, **route):
        with lock:
            receipts.append((action, transfer, route))
        return {'transferId': transfer}

    monkeypatch.setattr(file_transfer, 'file_transfer_store', SimpleNamespace(
        abort=lambda transfer, **route: settle('abort', transfer, **route),
        consume_completed=lambda transfer, **route: settle('consume', transfer, **route)))

    def worker(transfer):
        with binding.activate():
            assert current_browser_identity() == binding
            assert remember_bound_browser_file('https://example.test/file', transfer)
            together.wait(timeout=10)
            if transfer == 'claimed':
                assert claim_bound_browser_file('https://example.test/file') == {'transferId': transfer}
        assert current_browser_identity() == ('', '', '')

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, transfer) for transfer in ('claimed', 'unclaimed')]
        for future in futures:
            future.result()
    assert sorted(receipts) == [
        ('abort', 'unclaimed', {'owner_user_id': '41', 'client_id': 'browser-a', 'internal': True}),
        ('consume', 'claimed', {'owner_user_id': '41', 'client_id': 'browser-a'}),
    ]


def test_cleanup_interruption_restores_enclosing_identity(monkeypatch):
    from lib.browser import file_transfer
    from lib.search.browser_scope import (
        SearchBrowserBinding, current_browser_identity, remember_bound_browser_file,
    )

    class InterruptedCleanup(BaseException):
        pass

    def interrupt(*args, **kwargs):
        raise InterruptedCleanup()

    monkeypatch.setattr(file_transfer, 'file_transfer_store', SimpleNamespace(abort=interrupt))
    outer = SearchBrowserBinding('41', 'outer', 'Work')
    inner = SearchBrowserBinding('42', 'inner', 'Personal')
    with outer.activate():
        with pytest.raises(InterruptedCleanup):
            with inner.activate():
                assert remember_bound_browser_file('https://example.test/file', 'unfinished')
        assert current_browser_identity() == outer
    assert current_browser_identity() == ('', '', '')


def test_failed_scope_construction_never_publishes_an_identity(monkeypatch):
    from lib.search import browser_scope

    def fail():
        raise RuntimeError('handoff registry allocation failed')

    outer = browser_scope.SearchBrowserBinding('41', 'outer', 'Work')
    inner = browser_scope.SearchBrowserBinding('42', 'inner', 'Personal')
    with outer.activate():
        monkeypatch.setattr(browser_scope, '_TaskFileHandoffs', fail)
        with pytest.raises(RuntimeError, match='allocation failed'):
            with inner.activate():
                pytest.fail('failed construction cannot enter the scope')
        assert browser_scope.current_browser_identity() == outer
    assert browser_scope.current_browser_identity() == ('', '', '')
