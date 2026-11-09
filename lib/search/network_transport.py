"""Bind the standalone search package's public ports to Tofu networking."""
from lib.http_transport.connection_setup import prepare_sdk_wire_adapter
from lib.http_transport.outbound import RequestNetworkPolicy, send_routed_request
from lib.http_transport.outbound_httpx import managed_httpx_client


def requests_transport(adapter, wire_send, request, *, explicit_proxy=False, **options):
    """The package validated this redirect hop before entering its wire port."""
    def send(prepared, **wire_options):
        prepare_sdk_wire_adapter(adapter, prepared, wire_options.get('proxies'))
        return wire_send(prepared, **wire_options)
    return send_routed_request(
        send, request, policy=RequestNetworkPolicy(explicit_proxy=explicit_proxy), **options)


def httpx_client_factory(**options):
    return managed_httpx_client(**options)


def browser_proxy_resolver(url):
    """Select the initial destination's route for one anonymous browser context."""
    from lib.proxy import network_route_candidates
    from lib.http_transport.connection_setup import NetworkRoutesExhausted
    routes = network_route_candidates(url, scope='global')
    if not routes:
        raise NetworkRoutesExhausted('No configured network route connected')
    return routes[0].requests_proxies()
