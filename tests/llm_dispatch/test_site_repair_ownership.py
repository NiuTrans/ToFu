"""Owned searches defer model/rule effects, preserve cancellation and exact rules."""
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
import threading

import pytest

from lib.agent_core.execution_budget import ExecutionBudget
from lib.identity import PrincipalContext
from lib.search.browser_scope import SearchBrowserBinding
from lib.site_doctor import ownership, runner, trigger
from lib.site_knowledge.repository import SiteKnowledgeRepository
from lib import site_knowledge

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.unit
SITE = 'xiaohongshu.com'
URL = 'https://www.xiaohongshu.com/search_result?keyword=fixture'
JS = '(() => [])()'
CARDS = [{'title': 'Valid card', 'url': 'https://www.xiaohongshu.com/explore/fixture'}]


@pytest.fixture
def context(semantic_authority, monkeypatch, tmp_path):
    factory, owner, _ = semantic_authority
    principal = PrincipalContext.user(subject_id='search-fixture', owner_user_id=owner, tenant_id='tenant-a')
    task = {'id': 'search-repair-fixture', '_userId': owner,
            '_principalContext': principal.to_payload(), 'status': 'running',
            'config': {}, 'abort_event': threading.Event()}
    task['_executionBudget'] = ExecutionBudget(task)
    from lib.browser import access
    monkeypatch.setattr(access, '_STORE_PATH', str(tmp_path / 'browser_access.json'))
    access.grant_write(owner, SITE, client_id='device-a', profile='profile-a')
    monkeypatch.setattr(ownership, 'SiteKnowledgeRepository',
        lambda *a, **kw: SiteKnowledgeRepository(*a, **kw, client_factory=factory))
    from lib.tasks_pkg import manager
    monkeypatch.setattr(manager, 'make_task_abort_check', lambda task, **kw: task['abort_event'].is_set)
    trigger._last_attempt.clear()
    trigger._in_flight.clear()
    monkeypatch.delenv('TOFU_SITE_DOCTOR', raising=False)
    monkeypatch.delenv('TOFU_SITE_DOCTOR_COOLDOWN_S', raising=False)
    yield task, SearchBrowserBinding(str(owner), 'device-a', 'profile-a')
    assert not trigger._in_flight
    trigger._last_attempt.clear()


def fake_model(monkeypatch, task, *, cancel_at='', before_pin=None):
    from lib.llm_dispatch import api
    calls = []
    script = iter(['try_extractor', 'pin_knowledge'])
    def dispatch(messages, **kwargs):
        assert kwargs['owner_user_id'] == task['_userId']
        assert not kwargs['abort_check']()
        lease = kwargs['model_budget_admit'](input_tokens=5, max_output_tokens=10)
        lease.mark_dispatched()
        lease.settle({'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7})
        calls.append(kwargs)
        name = next(script)
        if name == 'pin_knowledge' and before_pin:
            before_pin()
        if cancel_at == name:
            task['abort_event'].set()
        return '', {'total_tokens': 7, '_tool_calls': [{'id': name,
            'function': {'name': name, 'arguments': json.dumps({'wait_selector': '.card', 'extractor_js': JS})}}]}
    monkeypatch.setattr(api, 'dispatch_chat', dispatch)
    return calls


def observe(scope, browser):
    scope.observe(SITE, URL, {'anchors': 2, 'transport': 'browser'}, browser)


def test_admitted_search_defers_then_pins_with_the_same_account(context, monkeypatch):
    task, binding = context
    calls = fake_model(monkeypatch, task)
    reads, events = [], []
    browser = SimpleNamespace(scrape=lambda *a, **kw: (reads.append(kw) or CARDS))
    with ownership.search_knowledge_scope(task, binding, repair=True, emit=events.append) as scope:
        with ThreadPoolExecutor(max_workers=1) as worker:
            worker.submit(observe, scope, browser).result()
        assert calls == [] and reads == [] and scope.repository.read(SITE).entry is None
    assert len(calls) == 2 and len(reads) == 1
    assert scope.repository.read(SITE).entry['extractor_js'] == JS
    assert task['_executionBudget'].consumption()[0] == 14
    assert task['_executionBudget'].actuals_complete()
    assert [event.get('tool') for event in events[:-1]] == ['try_extractor', 'pin_knowledge']
    # No late worker observation can revive a returned invocation.
    observe(scope, browser)
    scope.close(complete=True)
    assert len(calls) == 2


@pytest.mark.parametrize('reason', ['speculative', 'exception', 'cancelled', 'principal', 'account'])
def test_unadmitted_or_revoked_search_never_runs_a_model(context, monkeypatch, reason):
    task, binding = context
    from lib.llm_dispatch import api
    monkeypatch.setattr(api, 'dispatch_chat', lambda *a, **kw: pytest.fail('unauthorized model'))
    try:
        with ownership.search_knowledge_scope(task, binding, repair=reason != 'speculative') as scope:
            observe(scope, object())
            if reason == 'exception':
                raise RuntimeError('discard search')
            if reason == 'cancelled':
                task['abort_event'].set()
            if reason == 'principal':
                task['_principalContext'] = {**task['_principalContext'], 'tenant_id': 'other'}
            if reason == 'account':
                task['_executionBudget'] = ExecutionBudget(task)
    except RuntimeError:
        assert reason == 'exception'
    assert scope.repository.read(SITE).entry is None


@pytest.mark.parametrize('cancel_at', ['try_extractor', 'pin_knowledge'])
def test_cancellation_after_dispatch_cannot_publish_a_rule(context, monkeypatch, cancel_at):
    task, binding = context
    fake_model(monkeypatch, task, cancel_at=cancel_at)
    browser = SimpleNamespace(scrape=lambda *a, **kw: CARDS)
    with ownership.search_knowledge_scope(task, binding, repair=True) as scope:
        observe(scope, browser)
    assert scope.repository.read(SITE).entry is None


def test_competing_publish_invalidates_the_live_verification(context, monkeypatch):
    task, binding = context
    with ownership.search_knowledge_scope(task, binding, repair=True) as scope:
        def newer():
            site_knowledge.pin_knowledge(SITE, repository=scope.repository, expected_version=0,
                extractor_js='(() => ["newer"])()', evidence={'verified_count': 1})
        fake_model(monkeypatch, task, before_pin=newer)
        observe(scope, SimpleNamespace(scrape=lambda *a, **kw: CARDS))
    assert scope.repository.read(SITE).version == 1
    assert 'newer' in scope.repository.read(SITE).entry['extractor_js']


def test_bound_provider_carries_only_its_exact_rules(context, monkeypatch):
    task, binding = context
    from lib.search.search_bridge import _ChatuiBrowserProvider, _ChatuiSiteKnowledgeProvider
    from tofu_search import providers
    with binding.activate(), ownership.search_knowledge_scope(task, binding) as scope:
        site_knowledge.pin_knowledge(SITE, repository=scope.repository, expected_version=0, extractor_js=JS)
        browser = _ChatuiBrowserProvider().bind()
        with SearchBrowserBinding(binding.user_id, 'foreign-device', binding.profile).activate():
            assert _ChatuiBrowserProvider().bind().knowledge_scope is None
    monkeypatch.setattr(providers, 'get_browser_provider', lambda: browser)
    with ThreadPoolExecutor(max_workers=1) as worker:
        assert worker.submit(_ChatuiSiteKnowledgeProvider().get_knowledge, SITE).result()['extractor_js'] == JS
    monkeypatch.setattr(providers, 'get_browser_provider', lambda: _ChatuiBrowserProvider())
    assert _ChatuiSiteKnowledgeProvider().get_knowledge(SITE) is None


def test_unowned_entrypoint_never_dispatches_or_reads_legacy(monkeypatch):
    from lib.llm_dispatch import api
    monkeypatch.setattr(api, 'dispatch_chat', lambda *a, **kw: pytest.fail('unowned model'))
    assert not runner.run_doctor(SITE, URL)['pinned']
    assert site_knowledge.get_knowledge(SITE) is None


@pytest.mark.parametrize('valid', [True, False])
def test_explicit_legacy_adoption_requires_live_proof_and_preserves_file(context, tmp_path, monkeypatch, valid):
    task, binding = context
    path = tmp_path / 'legacy.json'
    raw = json.dumps({SITE: {'extractor_js': JS, 'version': 92, 'scrolls': 2}})
    path.write_text(raw)
    monkeypatch.setattr(site_knowledge, '_STORE_PATH', str(path))
    with ownership.search_knowledge_scope(task, binding) as scope:
        assert scope.repository.read(SITE).entry is None
        if valid:
            result = site_knowledge.adopt_legacy(SITE, repository=scope.repository,
                scrape=lambda **kw: CARDS, verified_by='explicit-owner-adoption')
            assert result['version'] == 1
            assert result['evidence']['verified_count'] == 1
        else:
            with pytest.raises(ValueError, match='verification failed'):
                site_knowledge.adopt_legacy(SITE, repository=scope.repository,
                    scrape=lambda **kw: [], verified_by='explicit-owner-adoption')
            assert scope.repository.read(SITE).entry is None
    assert path.read_text() == raw


def test_native_adapter_drift_does_not_modify_compatibility_rules(context, monkeypatch):
    task, binding = context
    from lib.llm_dispatch import api
    monkeypatch.setattr(api, 'dispatch_chat', lambda *a, **kw: pytest.fail('wrong repair owner'))
    with ownership.search_knowledge_scope(task, binding, repair=True) as scope:
        scope.observe(SITE, URL, {'anchors': 2, 'transport': 'native'}, object())
    assert scope.repository.read(SITE).entry is None


def test_read_access_alone_never_buys_or_executes_a_repair(context, monkeypatch):
    task, binding = context
    from lib.browser import access
    from lib.llm_dispatch import api
    access.replace_write_grants(task['_userId'], [])
    monkeypatch.setattr(api, 'dispatch_chat', lambda *a, **kw: pytest.fail('read-only repair'))
    events = []
    with ownership.search_knowledge_scope(task, binding, repair=True, emit=events.append) as scope:
        observe(scope, object())
    assert not trigger._last_attempt
    assert 'authorization required' in events[0]['result']['detail']


def test_revoked_script_permission_refuses_pin_and_future_rule_use(context, monkeypatch):
    task, binding = context
    from lib.browser import access
    def revoke():
        access.replace_write_grants(task['_userId'], [])
    fake_model(monkeypatch, task, before_pin=revoke)
    with ownership.search_knowledge_scope(task, binding, repair=True) as scope:
        observe(scope, SimpleNamespace(scrape=lambda *a, **kw: CARDS))
    assert scope.repository.read(SITE).entry is None


def test_failed_admission_audit_rolls_back_only_its_attempt(context, monkeypatch):
    task, binding = context
    calls = []
    monkeypatch.setattr(trigger, 'run_doctor', lambda *a, **kw: (
        calls.append(kw) or {'pinned': False, 'rounds': 0, 'tokens': 0, 'detail': 'fixture'}))
    def fail(*a, **kw):
        raise RuntimeError('audit unavailable')
    monkeypatch.setattr(trigger, 'audit_log', fail)
    with ownership.search_knowledge_scope(task, binding, repair=True) as scope:
        observe(scope, object())
    assert calls == [] and not trigger._last_attempt and not trigger._in_flight
    monkeypatch.setattr(trigger, 'audit_log', lambda *a, **kw: None)
    with ownership.search_knowledge_scope(task, binding, repair=True) as scope:
        observe(scope, object())
    assert len(calls) == 1


def test_distinct_owned_browsers_run_concurrently_and_only_exact_duplicate_is_suppressed(context, monkeypatch):
    task, binding = context
    from lib.browser import access
    access.grant_write(task['_userId'], SITE, client_id='device-b', profile='profile-a')
    barrier = threading.Barrier(2)
    calls = []
    def doctor(*a, **kw):
        calls.append(kw['execution'].repository)
        barrier.wait(timeout=10)
        return {'pinned': False, 'rounds': 0, 'tokens': 0, 'detail': 'fixture'}
    monkeypatch.setattr(trigger, 'run_doctor', doctor)
    def search(browser):
        with ownership.search_knowledge_scope(task, browser, repair=True) as scope:
            observe(scope, object())
            observe(scope, object())
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(search, browser) for browser in (
            binding, SearchBrowserBinding(binding.user_id, 'device-b', binding.profile))]
        for future in futures:
            future.result()
    assert len(calls) == 2
    search(binding)
    assert len(calls) == 2


@pytest.mark.parametrize('batch', [False, True])
def test_real_search_handler_admits_repair_but_stream_prefetch_only_observes(context, monkeypatch, batch):
    from contextvars import ContextVar
    task, binding = context
    from lib.browser import queue
    from lib.search.search_bridge import _ChatuiBrowserProvider
    from lib.tasks_pkg.handlers.search import _handlers
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    from tofu_search import providers
    task.update(lastUserQuery='fixture', _tool_result_cache={})
    task['config']['browserClientId'] = binding.client_id
    monkeypatch.setattr(queue, 'get_connected_clients', lambda **kw: [{
        'client_id': binding.client_id, 'profile': binding.profile,
        'last_poll': 1, 'capabilities': []}])
    calls = fake_model(monkeypatch, task)
    events = []
    monkeypatch.setattr(_handlers, 'append_event', lambda task, event: events.append(event))
    current_browser = ContextVar('search-integration-browser', default=None)
    monkeypatch.setattr(providers, 'get_browser_provider', current_browser.get)
    def search(*args, **kwargs):
        browser = _ChatuiBrowserProvider().bind()
        browser.scrape = lambda *a, **kw: CARDS
        current_browser.set(browser)
        trigger.on_site_drift(SITE, URL, {'anchors': 2, 'transport': 'browser'})
        return CARDS, {'reason': 'selector drift'}, None, None
    monkeypatch.setattr(_handlers, '_load_search_backend', lambda: (
        SimpleNamespace(_web_search_one=search), lambda *a, **kw: 'empty results'))
    args = {'queries': ['one', 'two']} if batch else {'query': 'fixture'}
    prefetched = StreamingToolAccumulator(task, project_path='/tmp')._execute_one('web_search', args)
    assert not prefetched.cacheable
    assert calls == []
    result = _handlers._handle_web_search(task, {}, 'web_search', 'search-occurrence', args,
        1, {}, task['config'], '/tmp', False)
    assert result[0] == 'search-occurrence'
    assert len(calls) == 2
    progress = [event for event in events if event['type'] == 'tool_progress'
                and event.get('stage') == 'site_repair']
    assert progress[-1]['result']['pinned'] is True
    assert all(event['toolCallId'] == 'search-occurrence' for event in progress)
