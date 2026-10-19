"""Scoped sessions, bounded workflows and observable bridge performance."""
import json
import threading
from types import SimpleNamespace

import pytest

from lib.browser import adapters, workflows
from lib.browser.metrics import BrowserTimings, measure_browser
from lib.browser.site_tool import site_tool

pytestmark = pytest.mark.unit



@pytest.fixture
def fixture(monkeypatch):
    runtime = SimpleNamespace(owner_user_id='41', client_id='device-a')
    adapter = adapters.SiteAdapter(id='workflow-test', name='Test', domains=('example.test',), commands=(
        adapters.AdapterCommand('search', 'Search', input_schema={'type': 'object'}, output_schema={'type': 'array'}),
        adapters.AdapterCommand('detail', 'Detail', input_schema={
            'type': 'object', 'additionalProperties': False, 'required': ['url'],
            'properties': {'url': {'type': 'string'}}}),
        adapters.AdapterCommand('write', 'Write', access='write'),
    ))
    monkeypatch.setitem(adapters._registry, adapter.id, adapter)
    monkeypatch.setattr('lib.browser.access.is_read_allowed', lambda owner, url: True)
    monkeypatch.setattr(workflows, 'is_read_allowed', lambda owner, url: True)
    monkeypatch.setattr(adapters, 'adapter_health', lambda *a, **k: {'healthy': True})
    monkeypatch.setattr('lib.browser.queue.get_connected_clients', lambda **k: [
        {'client_id': 'device-a', 'profile': 'main'}] if k['owner_user_id'] == '41' else [])
    pages = []

    class Page:
        def __init__(self, lease, **kwargs):
            self.lease = lease
            self.closed = False
            self.options = kwargs
            pages.append(self)
        def __enter__(self): return self
        def __exit__(self, *exc): self.closed = True

    monkeypatch.setattr(workflows, 'BrowserPage', Page)
    monkeypatch.setattr(workflows, 'acquire_browser_lease', lambda **k: SimpleNamespace(**k))
    calls = []

    def invoke(site, operation, params, **kwargs):
        calls.append((operation, params, kwargs))
        result = ([{'url': 'https://example.test/1'}, {'url': 'https://example.test/2'}]
                  if operation == 'search' else {'content': params['url'], 'access_token': 'secret-value'})
        return {'result': result}

    monkeypatch.setattr(adapters, 'invoke_adapter', invoke)
    return runtime, adapter, pages, calls


def plan(parallelism=1):
    return {'steps': [{'id': 'search', 'operation': 'search'},
                      {'id': 'details', 'operation': 'detail', 'foreach': {
                          'step': 'search', 'field': 'url', 'parameter': 'url',
                          'parallelism': parallelism}}]}


def test_session_readiness_is_not_login_and_reference_is_scoped(fixture):
    import base64
    runtime, adapter, _, _ = fixture
    result = json.loads(site_tool({'site': adapter.id, 'action': 'status'}, runtime))
    assert result['status'] == 'ready' and result['authentication'] == 'unknown'
    workflows.validate_session(result['session_ref'], adapter, runtime)
    descriptor = json.loads(base64.urlsafe_b64decode(result['session_ref']))
    for key, value in [('owner', '42'), ('device', 'other'), ('profile', 'private'),
                       ('site', 'other'), ('expires_at', 0)]:
        with pytest.raises(adapters.AdapterExecutionError, match='expired or changed'):
            changed = base64.urlsafe_b64encode(json.dumps({**descriptor, key: value}).encode()).decode()
            workflows.validate_session(changed, adapter, runtime)


def test_workflow_reuses_owned_page_and_redacts_intermediate_results(fixture):
    runtime, adapter, pages, calls = fixture
    result = json.loads(site_tool({'site': adapter.id, 'workflow': plan()}, runtime))
    assert result['status'] == 'ok'
    assert result['coverage']['completed_steps'] == 2
    assert len(calls) == 3 and len(pages) == 1 and pages[0].closed
    assert all(call[2]['_page'] is pages[0] for call in calls)
    assert 'secret-value' not in json.dumps(result)
    assert all(page.closed for page in pages)


def test_parallel_reads_have_distinct_owned_pages_and_join_cleanup(fixture, monkeypatch):
    runtime, adapter, pages, calls = fixture
    barrier = threading.Barrier(2, timeout=5)
    original = adapters.invoke_adapter
    def simultaneous(site, operation, params, **kwargs):
        if operation == 'detail':
            barrier.wait()
        return original(site, operation, params, **kwargs)
    monkeypatch.setattr(adapters, 'invoke_adapter', simultaneous)
    result = workflows.execute_workflow(adapter, plan(2), runtime)
    assert result['status'] == 'ok'
    assert len(pages) == 3 and all(page.closed for page in pages)
    assert calls[1][2]['_page'] is not calls[2][2]['_page']


@pytest.mark.parametrize('steps', [
    [{'id': 'write', 'operation': 'write'}],
    [{'id': 'x', 'operation': 'search'}, {'id': 'x', 'operation': 'search'}],
    [{'id': 'x', 'operation': 'detail', 'foreach': {'step': 'missing', 'field': 'url', 'parameter': 'url'}}],
    [{'id': 'x', 'operation': 'search', 'foreach': {'step': 'x', 'field': 'url', 'parameter': 'url', 'parallelism': 0}}],
])
def test_invalid_plans_fail_before_any_browser_work(fixture, steps):
    runtime, adapter, pages, calls = fixture
    with pytest.raises(adapters.AdapterValidationError):
        workflows.execute_workflow(adapter, {'steps': steps}, runtime)
    assert not calls and not pages


def test_failure_preserves_completed_reads_and_never_retries(fixture, monkeypatch):
    runtime, adapter, pages, calls = fixture
    original = adapters.invoke_adapter
    def fail(site, operation, params, **kwargs):
        if params.get('url', '').endswith('/2'):
            raise adapters.AdapterExecutionError('login_required', 'Sign in in the browser')
        return original(site, operation, params, **kwargs)
    monkeypatch.setattr(adapters, 'invoke_adapter', fail)
    result = workflows.execute_workflow(adapter, plan(), runtime)
    assert result['status'] == 'partial'
    assert result['error']['code'] == 'login_required'
    assert result['coverage']['completed_steps'] == 1
    assert len(result['steps'][1]['result']) == 1
    assert all(page.closed for page in pages)


def test_large_workflow_results_reach_shared_l0_without_output_budget(fixture, monkeypatch):
    runtime, adapter, pages, _ = fixture
    payload = ['x' * 5000 for _ in range(20)]
    monkeypatch.setattr(adapters, 'invoke_adapter', lambda *a, **k: {'result': payload})
    result = workflows.execute_workflow(adapter, {'steps': plan()['steps'][:1]}, runtime)
    assert result['status'] == 'ok' and result['error'] is None
    assert result['steps'][0]['result'] == payload
    assert len(json.dumps(result)) > 80000
    assert all(page.closed for page in pages)


def test_workflow_output_budget_is_not_an_execution_parameter(fixture):
    runtime, adapter, pages, calls = fixture
    with pytest.raises(adapters.AdapterValidationError):
        workflows.execute_workflow(adapter, {'max_chars': 4096, **plan()}, runtime)
    assert not calls and not pages


def test_independent_workflows_for_same_owner_start_without_global_quota(fixture, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    runtime, adapter, pages, _ = fixture
    entered, release = threading.Event(), threading.Event()
    count, lock = [0], threading.Lock()
    def invoke(*args, **kwargs):
        with lock:
            count[0] += 1
            if count[0] == 16:
                entered.set()
        assert release.wait(3)
        return {'result': []}
    monkeypatch.setattr(adapters, 'invoke_adapter', invoke)
    with ThreadPoolExecutor(max_workers=16) as workers:
        futures = [workers.submit(workflows.execute_workflow, adapter,
            {'steps': [{'id': 'search', 'operation': 'search'}]}, runtime) for _ in range(16)]
        try:
            assert entered.wait(2)
        finally:
            release.set()
        assert all(future.result()['error'] is None for future in futures)
    assert all(page.closed for page in pages)


def test_metric_samples_are_bounded_and_request_local():
    with measure_browser() as collector:
        for _ in range(140):
            collector.record(queue_ms=2, total_ms=12, execution_ms=6)
        summary = collector.summary()
        assert summary['command_count'] == 128 and summary['dropped_samples'] == 12
        assert summary['transport_and_settlement_ms']['p50'] == 4
    with measure_browser() as other:
        assert other.summary()['command_count'] == 0


def test_metric_collector_is_safe_for_bounded_workers():
    collector = BrowserTimings()
    threads = [threading.Thread(target=lambda: [collector.record(queue_ms=1, total_ms=2)
                                                for _ in range(80)]) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert collector.summary()['command_count'] == 128
    assert collector.summary()['dropped_samples'] == 32


def test_workflow_cancellation_preserves_prior_result_and_closes_pages(fixture, monkeypatch):
    runtime, adapter, pages, calls = fixture
    runtime.cancel_event = threading.Event()
    original = adapters.invoke_adapter
    def cancel_after_search(*args, **kwargs):
        result = original(*args, **kwargs)
        runtime.cancel_event.set()
        return result
    monkeypatch.setattr(adapters, 'invoke_adapter', cancel_after_search)
    result = workflows.execute_workflow(adapter, plan(), runtime)
    assert result['error']['code'] == 'cancelled'
    assert result['coverage']['completed_steps'] == 1
    assert len(calls) == 1 and all(page.closed for page in pages)
    assert all(page.closed for page in pages)



def test_default_foreach_starts_every_independent_read_without_two_call_waves(fixture, monkeypatch):
    import threading
    runtime, adapter, pages, _ = fixture
    entered, release = threading.Event(), threading.Event()
    count, lock = [0], threading.Lock()
    def invoke(site, operation, params, **kwargs):
        if operation == 'search':
            return {'result': [{'url': f'https://example.test/{index}'} for index in range(32)]}
        with lock:
            count[0] += 1
            if count[0] == 32:
                entered.set()
        assert release.wait(3)
        return {'result': {'content': params['url']}}
    monkeypatch.setattr(adapters, 'invoke_adapter', invoke)
    workflow = plan()
    workflow['steps'][1]['foreach'].pop('parallelism')
    output = []
    thread = threading.Thread(target=lambda: output.append(workflows.execute_workflow(adapter, workflow, runtime)))
    thread.start()
    try:
        assert entered.wait(2)
    finally:
        release.set()
        thread.join(4)
    assert not thread.is_alive() and output[0]['error'] is None
    assert len(output[0]['steps'][1]['result']) == 32
    assert all(page.closed for page in pages)


def test_explicit_foreach_concurrency_remains_a_caller_preference(fixture, monkeypatch):
    import threading
    runtime, adapter, pages, _ = fixture
    entered, release = threading.Event(), threading.Event()
    count, lock = [0], threading.Lock()
    def invoke(site, operation, params, **kwargs):
        if operation == 'search':
            return {'result': [{'url': f'https://example.test/{index}'} for index in range(8)]}
        with lock:
            count[0] += 1
            if count[0] == 2:
                entered.set()
        assert release.wait(3)
        return {'result': {'content': params['url']}}
    monkeypatch.setattr(adapters, 'invoke_adapter', invoke)
    output = []
    thread = threading.Thread(target=lambda: output.append(workflows.execute_workflow(adapter, plan(2), runtime)))
    thread.start()
    try:
        assert entered.wait(2)
        with lock:
            assert count[0] == 2
    finally:
        release.set()
        thread.join(4)
    assert not thread.is_alive() and output[0]['error'] is None
    assert count[0] == 8 and all(page.closed for page in pages)
