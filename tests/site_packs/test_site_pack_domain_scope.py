"""Site operations keep declared domains through the real BrowserPage boundary."""

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

START = 'https://app.example.com/start'
FOREIGN = 'https://outside.test/private'
EXTRACT = {'do': 'extract', 'js': '() => ({ok: true, data: []})', 'into': 'payload'}
NORMALIZE = {'do': 'normalize', 'kind': 'json_payload'}


class Bridge:
    """Only the extension transport is replaced; policy and page state stay real."""

    def __init__(self):
        self.calls = []
        self.url = START
        self.redirect = None
        self.states = []
        self.research_url = None
        self.research_stop = ''
        self.after_command = None

    def __call__(self, command, params, **route):
        self.calls.append((command, dict(params), route))
        if self.after_command:
            self.after_command(command)
        if command in ('create_tab', 'navigate'):
            self.url = self.redirect or params['url']
            return {'id': 7, 'url': self.url}, None
        if command == 'page_state':
            self.url = self.states.pop(0) if self.states else self.url
            return {'tabId': 7, 'url': self.url}, None
        if command == 'research_url':
            final_url = params['url'] if self.research_url is None else self.research_url
            return {'url': params['url'] if self.research_url is None else self.research_url,
                    'ok': True, 'status': 200, 'data': [],
                    'source': {'document_url': final_url, 'request_url': params['url'],
                               'response_url': final_url},
                    'research': {'stopReason': self.research_stop}}, None
        return {'value': {'ok': True, 'status': 200, 'data': [], 'source': {
            'document_url': self.url, 'request_url': self.url, 'response_url': self.url}}}, None


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    from lib.browser import access, adapters, page, protocol, queue
    from lib.browser.sessions import BrowserSessionLease
    from lib.site_packs._manifest import parse_manifest
    from lib.site_packs._projection import pack_to_adapter

    bridge = Bridge()
    monkeypatch.setattr(access, '_STORE_PATH', str(tmp_path / 'access.json'))
    monkeypatch.setattr(access, 'audit_log', lambda *a, **kw: None)
    monkeypatch.setattr(adapters, 'audit_log', lambda *a, **kw: None)
    monkeypatch.setattr(protocol, 'require_capabilities', lambda *a: None)
    monkeypatch.setattr(page, 'require_capabilities', lambda *a: None)
    monkeypatch.setattr(queue, 'send_browser_command', bridge)

    def lease(**kwargs):
        return BrowserSessionLease('scope-fixture', kwargs['owner_user_id'],
                                   kwargs['client_id'], profile='Work')

    monkeypatch.setattr(adapters, 'acquire_browser_lease', lease)

    def invoke(steps, *, params=None, auth=False, borrowed=None, access_mode='read'):
        document = {
            'pack': 'tofu.site-pack/v1', 'id': 'scope-fixture', 'name': 'Scope fixture',
            'domains': ['example.com'], 'operations': [{
                'name': 'inspect', 'description': 'Inspect a site', 'access': access_mode,
                'input_schema': {'type': 'object', 'properties': {
                    'destination': {'type': 'string'}}},
                'steps': steps,
            }],
        }
        if auth:
            document['auth'] = {'login_url': 'https://login.example.com'}
        adapter = pack_to_adapter(parse_manifest(document, source='scope-fixture.json'))
        monkeypatch.setattr(adapters, 'get_adapter', lambda identifier: adapter)
        return adapters.invoke_adapter(adapter.id, 'inspect', params,
                                       owner_user_id='41', client_id='device', _page=borrowed)

    return SimpleNamespace(
        invoke=invoke, bridge=bridge, access=access,
        page=lambda **kwargs: page.BrowserPage(
            lease(owner_user_id='41', client_id='device'), reuse_tab=True, **kwargs))


@pytest.mark.parametrize('kind', ['navigate', 'research'])
@pytest.mark.parametrize('url', [FOREIGN, 'https://example.com.outside.test',
                                'https://example.com@outside.test'])
def test_rendered_target_is_checked_even_without_a_url_parameter(runtime, kind, url):
    from lib.browser.adapter_errors import AdapterExecutionError
    with pytest.raises(AdapterExecutionError) as failure:
        runtime.invoke([
            {'do': kind, 'url': '{destination|raw}'}, EXTRACT, NORMALIZE,
        ], params={'destination': url})
    assert failure.value.code == 'access_denied'
    assert runtime.bridge.calls == []


@pytest.mark.parametrize('url', ['https://example.com', START,
                                'https://WWW.EXAMPLE.COM./page'])
def test_declared_hosts_and_subdomains_keep_the_same_execution(runtime, url):
    result = runtime.invoke([{'do': 'navigate', 'url': url}, EXTRACT, NORMALIZE])
    assert result['ok'] and result['result']['data'] == []
    assert runtime.bridge.calls[0][1]['url'] == url
    assert runtime.bridge.calls[-1][0] == 'close_tab'
    assert all(call[2]['owner_user_id'] == '41' and call[2]['client_id'] == 'device'
               for call in runtime.bridge.calls)


@pytest.mark.parametrize('auth,code', [(False, 'access_denied'), (True, 'login_required')])
def test_navigation_redirect_stops_before_extraction_and_closes_lease(runtime, auth, code):
    from lib.browser.adapter_errors import AdapterExecutionError
    runtime.bridge.redirect = FOREIGN
    with pytest.raises(AdapterExecutionError) as failure:
        runtime.invoke([{'do': 'navigate', 'url': START}, EXTRACT, NORMALIZE], auth=auth)
    assert failure.value.code == code
    assert not any(call[0] == 'page_execute' for call in runtime.bridge.calls)
    assert runtime.bridge.calls[-1][0] == 'close_tab'
    if auth:
        assert failure.value.details['login_url'] == 'https://login.example.com'


def test_research_receipt_cannot_return_a_foreign_domain(runtime):
    from lib.browser.adapter_errors import AdapterExecutionError
    runtime.bridge.research_url = FOREIGN
    with pytest.raises(AdapterExecutionError) as failure:
        runtime.invoke([{'do': 'research', 'url': START}, NORMALIZE])
    assert failure.value.code == 'access_denied'


@pytest.mark.parametrize('url', ['https://other.example.com', ''])
def test_research_origin_stop_is_terminal_even_within_the_declared_site(runtime, url):
    from lib.browser.adapter_errors import AdapterExecutionError
    runtime.bridge.research_url = url
    runtime.bridge.research_stop = 'cross_origin_redirect'
    with pytest.raises(AdapterExecutionError) as failure:
        runtime.invoke([{'do': 'navigate', 'url': START}, EXTRACT,
                        {'do': 'try', 'steps': [{'do': 'research', 'url': START}]}, NORMALIZE])
    assert failure.value.code == 'access_denied'
    assert runtime.bridge.calls[-1][0] == 'close_tab'


@pytest.mark.parametrize('access_mode,action', [
    ('read', {'do': 'fill', 'selector': '#query', 'value': 'keywords'}),
    ('read', EXTRACT),
    ('write', {'do': 'click', 'selector': '#submit'}),
])
def test_late_redirect_is_checked_before_trusted_or_write_actions(runtime, access_mode, action):
    from lib.browser.adapter_errors import AdapterExecutionError
    runtime.bridge.states = [START, FOREIGN]
    with pytest.raises(AdapterExecutionError) as failure:
        runtime.invoke([{'do': 'navigate', 'url': START}, action, EXTRACT, NORMALIZE],
                       access_mode=access_mode)
    assert failure.value.code == 'access_denied'
    assert [call[0] for call in runtime.bridge.calls] == [
        'create_tab', 'page_state', 'page_state', 'close_tab']
    assert not runtime.access.has_write_grant('41', FOREIGN, client_id='device', profile='Work')


@pytest.mark.parametrize('optional_step', [
    {'do': 'wait', 'selector': '#ready', 'optional': True},
    {'do': 'try', 'steps': [{'do': 'fill', 'selector': '#query', 'value': 'keywords'}]},
])
def test_best_effort_steps_never_swallow_live_access_denials(runtime, optional_step):
    from lib.browser.adapter_errors import AdapterExecutionError
    # First collect a valid payload, then deny the current site. A swallowed
    # optional failure would otherwise return that payload as successful.
    def deny_before_optional_step(command):
        if command == 'page_state' and sum(
                call[0] == 'page_state' for call in runtime.bridge.calls) == 4:
            runtime.access.replace_read_denials('41', ['example.com'])
    runtime.bridge.after_command = deny_before_optional_step
    with pytest.raises(AdapterExecutionError) as failure:
        runtime.invoke([{'do': 'navigate', 'url': START}, EXTRACT, optional_step, NORMALIZE])
    assert failure.value.code == 'access_denied'
    assert runtime.bridge.calls[-1][0] == 'close_tab'


@pytest.mark.parametrize('redirect', [None, FOREIGN])
def test_borrowed_page_restores_scope_on_success_and_failure(runtime, redirect):
    from lib.browser.adapter_errors import AdapterExecutionError
    page = runtime.page()
    runtime.bridge.redirect = redirect
    try:
        if redirect:
            with pytest.raises(AdapterExecutionError):
                runtime.invoke([{'do': 'navigate', 'url': START}, EXTRACT, NORMALIZE], borrowed=page)
        else:
            assert runtime.invoke([{'do': 'navigate', 'url': START}, EXTRACT, NORMALIZE],
                                  borrowed=page)['ok']
        assert page.lease.active
        assert not any(call[0] == 'close_tab' for call in runtime.bridge.calls)
        runtime.bridge.redirect = None
        assert page.new_tab(FOREIGN)['ok']
    finally:
        page.close()


def test_nested_invocation_cannot_widen_the_borrowed_page_scope(runtime):
    from lib.browser.adapter_errors import AdapterExecutionError
    from lib.browser.site_scope import SiteScope
    page = runtime.page()
    try:
        with page.site_scope(SiteScope(('app.example.com',), 'Outer operation')):
            with pytest.raises(AdapterExecutionError) as failure:
                runtime.invoke([{'do': 'navigate', 'url': 'https://example.com'},
                                EXTRACT, NORMALIZE], borrowed=page)
            assert failure.value.code == 'access_denied'
            assert runtime.bridge.calls == []
            assert page.new_tab(START)['ok']
        assert page.new_tab(FOREIGN)['ok']
    finally:
        page.close()


@pytest.mark.parametrize('interruption', ['cancel', 'control_exit'])
def test_borrowed_scope_and_cleanup_survive_task_interruption(runtime, interruption):
    import threading
    from lib.browser.adapter_errors import AdapterExecutionError

    cancelled = threading.Event()
    page = runtime.page(cancel_event=cancelled)

    def interrupt(command):
        if command == 'page_execute':
            if interruption == 'control_exit':
                raise KeyboardInterrupt()
            cancelled.set()
    runtime.bridge.after_command = interrupt
    try:
        with pytest.raises(KeyboardInterrupt if interruption == 'control_exit'
                           else AdapterExecutionError):
            runtime.invoke([{'do': 'navigate', 'url': START}, EXTRACT, NORMALIZE],
                           borrowed=page)
        assert page.lease.active
        runtime.bridge.after_command = None
        cancelled.clear()
        assert page.new_tab(FOREIGN)['ok']
    finally:
        cancelled.set()
        page.close()
    assert runtime.bridge.calls[-1][0] == 'close_tab'
    assert not page.lease.active


@pytest.mark.parametrize('optional_step', [
    {'do': 'wait', 'selector': '#ready', 'optional': True},
    {'do': 'try', 'steps': [{'do': 'fill', 'selector': '#query', 'value': 'keywords'}]},
])
def test_optional_steps_cannot_turn_cancellation_into_success(runtime, optional_step):
    import threading
    from lib.browser.adapter_errors import AdapterExecutionError

    cancelled = threading.Event()
    page = runtime.page(cancel_event=cancelled)

    def cancel_before_optional_step(command):
        if command == 'page_state' and sum(
                call[0] == 'page_state' for call in runtime.bridge.calls) == 4:
            cancelled.set()
    runtime.bridge.after_command = cancel_before_optional_step
    try:
        with pytest.raises(AdapterExecutionError, match='cancelled'):
            runtime.invoke([{'do': 'navigate', 'url': START}, EXTRACT, optional_step, NORMALIZE],
                           borrowed=page)
    finally:
        page.close()
    assert runtime.bridge.calls[-1][0] == 'close_tab'
