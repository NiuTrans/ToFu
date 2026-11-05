"""The normalized result's declared byte budget includes its UTF-8 encoding."""

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('text', ['x' * 600, 'é' * 600, '豆' * 600, '🧈' * 600])
def test_json_result_budget_counts_encoded_bytes(monkeypatch, text):
    from lib.browser import adapters, protocol
    from lib.browser.adapter_errors import AdapterExecutionError
    from lib.browser.page import BrowserPage
    from lib.browser.sessions import BrowserSessionLease
    from lib.site_packs._manifest import parse_manifest
    from lib.site_packs._projection import pack_to_adapter

    pack = parse_manifest({
        'pack': 'tofu.site-pack/v1', 'id': 'byte-fixture', 'name': 'Byte fixture',
        'domains': ['example.com'], 'operations': [{
            'name': 'inspect', 'description': 'Read JSON',
            'steps': [
                {'do': 'research', 'url': 'https://example.com'},
                {'do': 'normalize', 'kind': 'json_payload', 'max_output_bytes': 1024},
            ],
        }],
    }, source='byte-fixture.json')
    adapter = pack_to_adapter(pack)
    monkeypatch.setattr(adapters, 'get_adapter', lambda identifier: adapter)
    monkeypatch.setattr(protocol, 'require_capabilities', lambda *a, **kw: None)
    monkeypatch.setattr(adapters, 'audit_log', lambda *a, **kw: None)
    page = BrowserPage(BrowserSessionLease('bytes', '41', 'device', profile='Original'))
    monkeypatch.setattr(page, 'research',
                        lambda url, **kw: {'page': {'url': url}, 'result': {
                            'ok': True, 'status': 200, 'data': [{'note': text}],
                            'source': {'document_url': url, 'request_url': url, 'response_url': url}}})

    def invoke():
        return adapters.invoke_adapter(adapter.id, 'inspect', {}, owner_user_id='41',
                                       client_id='device', _page=page)

    if text.isascii():
        assert invoke()['ok']
    else:
        with pytest.raises(AdapterExecutionError) as failure:
            invoke()
        assert failure.value.code == 'output_budget_exhausted'
