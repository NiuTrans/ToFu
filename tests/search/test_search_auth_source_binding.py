"""The host's optional auth binding never freezes credential authority."""

import pytest

pytestmark = pytest.mark.unit


def test_host_auth_binding_rereads_current_source_in_both_lookup_paths(monkeypatch):
    from lib import auth_sources
    from lib.search.search_bridge import _ChatuiAuthSourceProvider

    row = {'enabled': True, 'cookies': [{'name': 'session', 'value': 'fixture'}]}
    calls = []

    def get_source(domain):
        calls.append(('domain', domain))
        return row

    def match_source(url):
        calls.append(('url', url))
        return row

    monkeypatch.setattr(auth_sources, 'get_source', get_source)
    monkeypatch.setattr(auth_sources, 'match_source', match_source)
    bound = _ChatuiAuthSourceProvider().bind()
    assert bound.get_source('example.test')['enabled']
    row = {'enabled': False, 'cookies': []}
    assert bound.get_source('example.test') == row
    assert bound.match_source('https://example.test/page') == row
    assert calls == [('domain', 'example.test'), ('domain', 'example.test'),
                     ('url', 'https://example.test/page')]
