"""Subscription HTTP policy port, independent of provider host recognition.

OAuth owners import these helpers; the common network layer still owns routing,
timeouts, pooling and safe connection recovery. Loopback callbacks stay direct.
"""
from lib.http_transport import client


def http_get(url, **options):
    options.setdefault('network_scope', 'subscription')
    return client.http_get(url, **options)


def http_post(url, **options):
    options.setdefault('network_scope', 'subscription')
    return client.http_post(url, **options)
