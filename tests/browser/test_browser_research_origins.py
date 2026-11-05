"""Research must stop at an origin change even when the final tab comes back."""

import json

import pytest

from tests._browser_extension_probe import run_extension_probe

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('phase', ['initial', 'scroll', 'signals', 'snapshot', 'pagination'])
def test_research_discards_cross_origin_evidence_and_releases_capture(phase):
    probe = run_extension_probe('researchOrigin:' + phase)
    assert 'FOREIGN_PRIVATE_CONTENT' not in json.dumps(probe['result'])
    assert probe['result']['research']['stopReason'] == 'cross_origin_redirect'
    assert probe['result']['collectedText'] == ''
    assert probe['result']['url'] == 'https://outside.test/private'
    assert probe['limits']['activeCaptures'] == 0
    assert 'tabs.remove' in probe['operations']


def test_serialized_research_functions_guard_the_actual_page_realm():
    for row in run_extension_probe('researchOriginRealm'):
        assert row.get('result') == {'originMismatch': 'https://outside.test/private'}, row
        assert row['documentReads'] == 0


@pytest.mark.parametrize('surface', ['page', 'tool'])
def test_older_extensions_are_refused_before_research_dispatch(monkeypatch, surface):
    from lib.browser import access, protocol
    from lib.browser.page import BrowserPage
    from lib.browser.queue import _state, mark_poll
    from lib.browser.research import research_page
    from lib.browser.sessions import BrowserSessionLease
    from lib.browser.tool_runtime import BrowserToolRuntime

    # The queue modules share this registry by reference; replace only our row.
    monkeypatch.setitem(_state._clients, 'legacy-research', {})
    mark_poll('legacy-research', owner_user_id='41', protocol_version=2,
              capabilities=sorted(protocol.ALL_CAPABILITIES - {'research_origin_guard'}))
    monkeypatch.setattr(access, 'is_read_allowed', lambda *a: True)
    def sender(*args, **kwargs):
        pytest.fail('An older extension must not receive a research command')
    if surface == 'page':
        page = BrowserPage(BrowserSessionLease('legacy', '41', 'legacy-research'), sender=sender)
        with pytest.raises(protocol.BrowserUpgradeRequired) as failure:
            page.research('https://example.test')
        assert failure.value.missing == ('research_origin_guard',)
    else:
        runtime = BrowserToolRuntime(owner_user_id='41', client_id='legacy-research', sender=sender)
        result = research_page({'url': 'https://example.test'}, runtime)
        assert result.startswith('Error:') and 'research_origin_guard' in result


def test_generic_research_never_accepts_a_stopped_origin_receipt(monkeypatch):
    from lib.browser import access, protocol
    from lib.browser.research import research_page
    from lib.browser.tool_runtime import BrowserToolRuntime

    monkeypatch.setattr(access, 'is_read_allowed', lambda *a: True)
    monkeypatch.setattr(protocol, 'require_capabilities', lambda *a: {})
    runtime = BrowserToolRuntime(owner_user_id='41', client_id='device', sender=lambda *a, **kw: ({
        'url': 'https://example.test', 'collectedText': 'must not be accepted',
        'research': {'stopReason': 'cross_origin_redirect'},
    }, None))
    result = research_page({'url': 'https://example.test'}, runtime)
    assert result.startswith('Error:') and 'cross-origin' in result
    assert 'must not be accepted' not in result
