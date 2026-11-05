"""Specialized JSON data retains its observed source through common admission."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit
PAGE = 'https://site.test/models'
REQUEST = 'https://site.test/api/attributes?names=fixture'
RESPONSE = 'https://site.test/api/attributes/current'


@pytest.fixture
def operation(monkeypatch):
    from lib.browser import adapters, page, protocol
    from lib.browser.access import BrowserAccessDenied
    from lib.browser.sessions import BrowserSessionLease
    from lib.site_packs._manifest import parse_manifest
    from lib.site_packs._projection import pack_to_adapter

    payload = {'ok': True, 'status': 200, 'source': {
        'document_url': PAGE, 'request_url': REQUEST, 'response_url': RESPONSE,
    }, 'data': [{'name': 'Fixture', 'price': 0, 'enabled': False, 'accessToken': 'secret'}]}
    transport = SimpleNamespace(after_execute=None)
    checks, commands, denied = [], [], set()
    monkeypatch.setattr(page, 'require_capabilities', lambda *a, **k: None)
    monkeypatch.setattr(protocol, 'require_capabilities', lambda *a, **k: None)
    monkeypatch.setattr(adapters, 'audit_log', lambda *a, **k: None)
    def authorize(owner, url, *, access, client_id, profile):
        checks.append((owner, url, access, client_id, profile))
        if url in denied:
            raise BrowserAccessDenied('Source access revoked')
    monkeypatch.setattr(page, 'require_access', authorize)
    def sender(command, params, **route):
        commands.append((command, route))
        state = {'url': PAGE, 'tabId': 7}
        if command == 'page_state':
            return state, None
        if transport.after_execute:
            transport.after_execute()
        return {'value': payload, 'page': state}, None
    browser = page.BrowserPage(BrowserSessionLease(
        'json-source', '41', 'source-device', profile='Work', tab_id=7), sender=sender)
    document = {'pack': 'tofu.site-pack/v1', 'id': 'source-fixture', 'name': 'Source fixture',
        'domains': ['site.test'], 'operations': [{
            'name': 'attributes', 'description': 'Read observed JSON attributes',
            'steps': [{'do': 'extract', 'js': '({})', 'into': 'payload'},
                      {'do': 'normalize', 'kind': 'json_payload'}],
        }]}
    adapter = pack_to_adapter(parse_manifest(document, source='source-fixture.json'))
    monkeypatch.setattr(adapters, 'get_adapter', lambda identifier: adapter)
    def run():
        return adapters.invoke_adapter(adapter.id, 'attributes', {}, owner_user_id='41',
                                       client_id='source-device', _page=browser)
    return SimpleNamespace(run=run, payload=payload, checks=checks, commands=commands,
                           denied=denied, page=browser, transport=transport)


def test_same_origin_redirect_preserves_attributes_and_explicit_authority(operation):
    before = deepcopy(operation.payload)
    result = operation.run()['result']
    assert result['status'] == 'ok'
    assert result['data'] == [{'name': 'Fixture', 'price': 0, 'enabled': False}]
    assert operation.payload == before
    for url in (PAGE, REQUEST, RESPONSE):
        assert ('41', url, 'read', 'source-device', 'Work') in operation.checks
    assert all(route['owner_user_id'] == '41' and route['client_id'] == 'source-device'
               for _, route in operation.commands)


@pytest.mark.parametrize('source', [None, {},
    {'document_url': PAGE, 'request_url': REQUEST},
    {'document_url': PAGE, 'request_url': REQUEST, 'response_url': 'https://outside.test/private'},
    {'document_url': 'https://outside.test/page', 'request_url': REQUEST, 'response_url': RESPONSE},
    {'document_url': PAGE, 'request_url': 'https://outside.test/api', 'response_url': RESPONSE},
    {'document_url': PAGE, 'request_url': REQUEST, 'response_url': 'http://site.test/data'},
    {'document_url': PAGE, 'request_url': REQUEST, 'response_url': 'https://site.test:8443/data'},
    {'document_url': PAGE, 'request_url': REQUEST, 'response_url': 'https://user:private@site.test/data'},
])
def test_unverified_or_foreign_json_source_is_rejected(operation, source):
    from lib.browser.adapter_errors import AdapterExecutionError

    operation.payload['source'] = source
    with pytest.raises(AdapterExecutionError) as failure:
        operation.run()
    assert failure.value.code == 'source_unverified'
    assert 'private' not in str(failure.value)


def test_final_response_rechecks_current_read_policy(operation):
    from lib.browser.adapter_errors import AdapterExecutionError

    operation.denied.add(RESPONSE)
    with pytest.raises(AdapterExecutionError) as failure:
        operation.run()
    assert failure.value.code == 'access_denied'


@pytest.mark.parametrize('ok,status', [('true', 200), (True, 503), (True, '200'), (True, None)])
def test_json_success_requires_observed_http_success(operation, ok, status):
    from lib.browser.adapter_errors import AdapterExecutionError

    operation.payload.update(ok=ok, status=status)
    with pytest.raises(AdapterExecutionError) as failure:
        operation.run()
    assert failure.value.code == 'fetch_failed'


def test_missing_data_is_not_invented_as_an_empty_success(operation):
    from lib.browser.adapter_errors import AdapterExecutionError

    del operation.payload['data']
    with pytest.raises(AdapterExecutionError) as failure:
        operation.run()
    assert failure.value.code == 'invalid_json_payload'


def test_default_https_port_and_hostname_case_do_not_reject_same_origin(operation):
    operation.payload['source']['response_url'] = 'https://SITE.TEST:443/resolved'
    assert operation.run()['result']['data'][0]['price'] == 0


def test_explicit_empty_data_and_truncation_keep_their_existing_meaning(operation):
    operation.payload.update(data=[], truncated=True)
    assert operation.run()['result'] == {'status': 'partial', 'data': [],
        'coverage': {'scope': 'requested_attributes', 'truncated': True}}


@pytest.mark.parametrize('stop', ['cancel', 'release'])
def test_captured_data_does_not_outlive_its_invocation(operation, stop):
    import threading
    from lib.browser.adapter_errors import AdapterExecutionError

    event = threading.Event()
    operation.page._cancel_event = event
    if stop == 'cancel':
        operation.transport.after_execute = event.set
    else:
        operation.transport.after_execute = lambda: setattr(operation.page.lease, 'released_at', 1)
    with pytest.raises(AdapterExecutionError):
        operation.run()
    assert not any(url == RESPONSE for _, url, _, _, _ in operation.checks)


def test_invalid_url_error_does_not_publish_secret_parser_details(operation):
    import traceback
    from lib.browser.adapter_errors import AdapterExecutionError

    operation.payload['source']['response_url'] = 'https://site.test:do-not-log-this/data'
    with pytest.raises(AdapterExecutionError) as failure:
        operation.run()
    assert 'do-not-log-this' not in ''.join(traceback.format_exception(failure.value))
