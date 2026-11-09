"""HTTP transport, body admission, response policy and request lifecycle.

Entry points live in the named modules; importing the package starts no work.
Application assembly registers inbound policy; clients own outbound sessions.
"""


def http_get(url, **options):
    """Public host network port for optional plugins; lazily load the client."""
    from lib.http_transport.client import http_get as send
    return send(url, **options)


def managed_session(**options):
    """Public owned-session port; plugins retain their headers and cookies."""
    from lib.http_transport.outbound import managed_session as create
    return create(**options)
