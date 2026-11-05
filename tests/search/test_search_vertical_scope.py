"""Vertical search borrows exact task inputs and joins its owned work."""

from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def search_context():
    from lib.identity import PrincipalContext
    from lib.search.browser_scope import SearchBrowserBinding
    from lib.tasks_pkg.handlers.search import _core

    principal = PrincipalContext.user(subject_id='vertical-fixture', owner_user_id=41)
    task = {'id': 'vertical-fixture', '_userId': '41',
            '_principalContext': principal.to_payload(), 'abort_event': Event()}
    return _core, task, SearchBrowserBinding('41', 'selected-device', 'Original')


@pytest.mark.parametrize('main_fails', [False, True])
@pytest.mark.parametrize('browser_available', [False, True])
def test_vertical_keeps_task_browser_config_without_unrelated_context(
        search_context, monkeypatch, main_fails, browser_available):
    from lib.browser import queue
    from lib.search.browser_scope import SearchBrowserBinding, current_browser_identity
    from lib.search.search_bridge import _ChatuiBrowserProvider, _ChatuiSiteSearchProvider
    from lib.site_doctor.ownership import current_knowledge_scope, search_knowledge_scope
    from tofu_search.config import bind_call_config, get_config

    core, task, binding = search_context
    if not browser_available:
        binding = SearchBrowserBinding(binding.user_id, '', '')
    monkeypatch.setattr(queue, 'get_connected_clients',
                        lambda **kw: pytest.fail('worker reselected a browser'))
    foreign = ContextVar('unrelated_request_secret', default=None)
    observed = {}

    def vertical():
        observed.update(identity=current_browser_identity(),
                        scope=current_knowledge_scope(), config=get_config(),
                        browser=_ChatuiBrowserProvider().bind(),
                        native=_ChatuiSiteSearchProvider().bind(), foreign=foreign.get())
        return {'source': 'vertical'}

    def main(*args, **kwargs):
        if main_fails:
            raise RuntimeError('controlled main search failure')
        return []

    monkeypatch.setattr(core, 'resolve_vertical', lambda *args: vertical)
    monkeypatch.setattr(core, 'perform_web_search', main)
    config = replace(get_config(), local_rerank_cancel_event=task['abort_event'])
    token = foreign.set('request-only')
    try:
        with binding.activate(), search_knowledge_scope(task, binding) as scope:
            with bind_call_config(config):
                result = core._web_search_one('query', 'question')
            assert observed['identity'] == binding
            assert observed['scope'] is scope
            assert observed['config'] is config
            assert observed['browser'].knowledge_scope is scope
            assert observed['native'].knowledge_scope is scope
            assert observed['foreign'] is None
            assert result[3] == {'source': 'vertical'}
            assert (result[1] or {}).get('reason') == ('exception' if main_fails else None)
            assert current_knowledge_scope() is scope
            assert current_browser_identity() == binding
    finally:
        foreign.reset(token)


@pytest.mark.parametrize('provider_name', ['_ChatuiBrowserProvider', '_ChatuiSiteSearchProvider'])
def test_provider_never_borrows_another_devices_task(search_context, provider_name):
    from lib.search import search_bridge
    from lib.search.browser_scope import SearchBrowserBinding
    from lib.site_doctor.ownership import search_knowledge_scope

    _core, task, binding = search_context
    provider_type = getattr(search_bridge, provider_name)
    other = SearchBrowserBinding(binding.user_id, 'another-device', binding.profile)
    with binding.activate(), search_knowledge_scope(task, binding) as scope:
        provider = provider_type().bind()
        with other.activate():
            assert provider_type().bind().knowledge_scope is None
            assert provider.bind() is provider
            assert provider.knowledge_scope is scope


@pytest.mark.parametrize('cancelled', [False, True])
def test_native_search_carries_task_cancellation(search_context, monkeypatch, cancelled):
    from lib.browser import adapters
    from lib.search.search_bridge import _ChatuiSiteSearchProvider
    from lib.site_doctor.ownership import search_knowledge_scope
    from tofu_search import SiteSearchTerminalError

    _core, task, binding = search_context
    with binding.activate(), search_knowledge_scope(task, binding):
        provider = _ChatuiSiteSearchProvider().bind()
    if cancelled:
        task['abort_event'].set()
        monkeypatch.setattr(adapters, 'acquire_browser_lease',
                            lambda **kw: pytest.fail('cancelled search acquired a page'))
        with pytest.raises(SiteSearchTerminalError):
            provider.search('modelplaza', 'query')
    else:
        captured = {}

        def invoke(*args, **kwargs):
            captured.update(kwargs)
            return {'ok': True, 'result': []}

        monkeypatch.setattr(adapters, 'invoke_adapter', invoke)
        assert provider.search('modelplaza', 'query') == []
        assert captured['owner_user_id'] == binding.user_id
        assert captured['client_id'] == binding.client_id
        assert captured['task_id'] == task['id']
        assert captured['cancel_event'] is task['abort_event']


def test_main_control_exit_joins_vertical_before_scope_settlement(search_context, monkeypatch):
    from lib.site_doctor.ownership import search_knowledge_scope

    core, task, binding = search_context
    started, release, returned = Event(), Event(), Event()
    observations = []

    class StopSearch(BaseException):
        pass

    def vertical():
        started.set()
        assert release.wait(10), 'test did not release vertical work'
        observations.append('vertical_finished')

    def main(*args, **kwargs):
        assert started.wait(10)
        raise StopSearch()

    def call():
        try:
            with binding.activate(), search_knowledge_scope(task, binding):
                core._web_search_one('query', 'question')
        except StopSearch:
            observations.append('caller_settled')
        finally:
            returned.set()

    monkeypatch.setattr(core, 'resolve_vertical', lambda *args: vertical)
    monkeypatch.setattr(core, 'perform_web_search', main)
    with ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(call)
        try:
            assert started.wait(10)
            assert not returned.wait(.2), 'caller left its vertical work running'
        finally:
            release.set()
            future.result(timeout=10)
    assert observations == ['vertical_finished', 'caller_settled']


def test_queued_worker_keeps_provider_snapshot_and_owns_file_cleanup(search_context, monkeypatch):
    from lib.search import browser_scope
    from lib.search.search_bridge import _ChatuiBrowserProvider
    from lib.search.search_runtime import submit_search_worker
    from tofu_search import providers

    _core, _task, binding = search_context
    old = providers.get_browser_provider()
    release = Event()
    aborted = []
    monkeypatch.setattr(browser_scope, '_abort_bound_browser_file',
                        lambda transfer_id, **kw: aborted.append((transfer_id, kw['binding'])))

    def work():
        provider = providers.get_browser_provider()
        assert (provider.user_id, provider.client_id, provider.profile) == binding
        assert browser_scope.remember_bound_browser_file('https://example.test/file', 'worker-file')
        raise RuntimeError('worker failed after receiving a file')

    providers.register_browser_provider(_ChatuiBrowserProvider())
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            blocker = pool.submit(release.wait, 10)
            try:
                with binding.activate():
                    future = submit_search_worker(pool, work)
                assert aborted == []
                providers.register_browser_provider(None)
            finally:
                release.set()
            assert blocker.result(timeout=10)
            with pytest.raises(RuntimeError, match='worker failed'):
                future.result(timeout=10)
        assert aborted == [('worker-file', binding)]
    finally:
        providers.register_browser_provider(old)
