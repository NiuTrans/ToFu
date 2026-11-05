"""Request-scoped proxy exemptions end with their last holder.

Ephemeral dispatch slots lease their self-hosted host's proxy exemption
instead of registering it for the process lifetime. A permanent registration
(configured providers, discovery, local serving) always outlives a lease, and a
lease removes only the ``no_proxy`` token it added.
"""

from __future__ import annotations

import os

import pytest

import lib.proxy as proxy

pytestmark = pytest.mark.unit

_URL = 'http://lease-a.tofu-test.invalid:8000/v1'
_HOST = 'lease-a.tofu-test.invalid'


@pytest.fixture(autouse=True)
def _isolated_bypass_registry(monkeypatch):
    for name in ('no_proxy', 'NO_PROXY'):
        monkeypatch.setenv(name, 'localhost,127.0.0.1')
    monkeypatch.setattr(proxy, '_registered_hosts', set())
    monkeypatch.setattr(proxy, '_host_leases', {})
    monkeypatch.setattr(proxy, '_lease_env_tokens', set())


def _bypassed() -> bool:
    return proxy.describe_route(_URL)['decisionReason'] == 'registered_bypass'


def test_concurrent_leases_share_one_exemption_until_the_last_release():
    assert not _bypassed()
    assert proxy.lease_no_proxy_url(_URL) == _HOST
    assert proxy.lease_no_proxy_url(_URL) == _HOST
    assert proxy.proxies_for(_URL) == {'no_proxy': '*'}
    assert os.environ['no_proxy'] == os.environ['NO_PROXY'] == (
        f'localhost,127.0.0.1,{_HOST}')

    proxy.release_no_proxy_host(_HOST)
    assert _bypassed()
    assert _HOST in os.environ['no_proxy'].split(',')

    proxy.release_no_proxy_host(_HOST)
    assert not _bypassed()
    assert os.environ['no_proxy'] == os.environ['NO_PROXY'] == 'localhost,127.0.0.1'
    # An unmatched release is inert.
    proxy.release_no_proxy_host(_HOST)
    assert not _bypassed()


def test_permanent_registration_outlives_a_lease():
    proxy.lease_no_proxy_url(_URL)
    assert proxy.register_no_proxy_host(_HOST)
    proxy.release_no_proxy_host(_HOST)
    assert _bypassed()
    assert _HOST in os.environ['no_proxy'].split(',')


def test_lease_never_removes_a_token_it_did_not_add(monkeypatch):
    for name in ('no_proxy', 'NO_PROXY'):
        monkeypatch.setenv(name, f'localhost,{_HOST}')
    proxy.lease_no_proxy_url(_URL)
    assert _bypassed()
    proxy.release_no_proxy_host(_HOST)
    assert not _bypassed()
    assert os.environ['no_proxy'] == f'localhost,{_HOST}'
