"""Search/fetch's public transport ports retain preparation and host routing."""
import asyncio
import pytest
import requests

from tests.proxy.test_unified_network import isolated_routes, proxy_server, failover_routes

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('transport', ['requests', 'httpx'])
def test_search_ports_share_route_failover(transport, failover_routes):  # noqa: F811
    from tofu_search.config import SearchConfig, bind_call_config
    from tofu_search.network_transport import NetworkSession
    from lib.search.network_transport import requests_transport, httpx_client_factory
    received, reports = failover_routes
    with bind_call_config(SearchConfig(requests_transport=requests_transport)):
        client = NetworkSession() if transport == 'requests' else httpx_client_factory()
        with client:
            response = client.post('http://search.invalid/query', json={'query': 'test'})
            assert response.json() == {'ok': True}
    assert len(received) == 1
    assert reports == [('bad', False), ('good', True)]


def test_fetch_rejects_private_hop_before_host_route_probe(monkeypatch):
    from tofu_search.config import SearchConfig, bind_call_config
    from tofu_search.fetch.utils import _SSRFGuardAdapter
    from lib.search.network_transport import requests_transport
    def forbidden(*args, **kwargs):
        pytest.fail('blocked redirect reached the network authority')
    monkeypatch.setattr('lib.proxy.network_route_candidates', forbidden)
    with bind_call_config(SearchConfig(requests_transport=requests_transport,
                                       block_private_addresses=True)):
        adapter = _SSRFGuardAdapter()
        try:
            with pytest.raises(requests.exceptions.InvalidURL, match='SSRF'):
                adapter.send(requests.Request('GET', 'http://127.0.0.1/private').prepare())
        finally:
            adapter.close()
