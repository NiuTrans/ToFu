"""Public transport injection keeps request preparation and standalone behavior."""
import pytest
import requests

pytestmark = pytest.mark.unit
from tofu_search.config import SearchConfig, bind_call_config
from tofu_search.network_transport import NetworkSession


def test_transport_receives_prepared_request_and_explicit_proxy_intent():
    seen = []
    def transport(adapter, wire_send, request, *, explicit_proxy, **kwargs):
        seen.append((request, explicit_proxy))
        response = requests.Response()
        response.status_code = 200
        response._content = b'ok'
        return response
    with bind_call_config(SearchConfig(requests_transport=transport)):
        with NetworkSession() as session:
            session.headers['X-Session'] = 'retained'
            session.cookies.set('session', 'retained', domain='search.invalid', path='/')
            session.post('https://search.invalid/query', data={'q': 'test'})
            session.get('https://search.invalid/', proxies={'no_proxy': '*'})
    assert seen[0][0].body == 'q=test'
    assert seen[0][0].headers['X-Session'] == 'retained'
    assert seen[0][0].headers['Cookie'] == 'session=retained'
    assert [explicit for _, explicit in seen] == [False, True]


def test_anonymous_browser_projects_host_route_without_library_preference():
    from tofu_search.fetch.browser_proxy import anonymous_browser_proxy
    with bind_call_config(SearchConfig(
            proxy_url='http://old.invalid:80',
            browser_proxy_resolver=lambda url: {'https': 'http://user:secret@current.invalid:8080'})):
        assert anonymous_browser_proxy('https://example.invalid/') == {
            'server': 'http://current.invalid:8080', 'username': 'user', 'password': 'secret'}
