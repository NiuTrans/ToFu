"""Public egress guard regression tests (redirect SSRF + memory bounds)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import requests

from lib.safe_fetch import (
    SafeFetchError, _PublicEgressAdapter, ip_is_public, validate_public_url,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('address', [
    '127.0.0.1', '10.0.0.8', '169.254.169.254', '::1', 'fe80::1',
    '::ffff:127.0.0.1', '0.0.0.0',
])
def test_non_public_address_classes_are_blocked(address):
    assert ip_is_public(address) is False


def test_all_dns_answers_must_be_public(monkeypatch):
    monkeypatch.delenv('TOFU_TEST_FETCH_ALLOW', raising=False)
    answers = [
        (None, None, None, None, ('93.184.216.34', 443)),
        (None, None, None, None, ('127.0.0.1', 443)),
    ]
    monkeypatch.setattr('lib.safe_fetch.socket.getaddrinfo',
                        lambda *a, **k: answers)
    with pytest.raises(SafeFetchError, match='127.0.0.1'):
        validate_public_url('https://example.test/image',
                            allow_hosts_env='TOFU_TEST_FETCH_ALLOW')


def test_exact_hostname_allowlist_preserves_deliberate_local_use(monkeypatch):
    monkeypatch.setenv('TOFU_TEST_FETCH_ALLOW', 'model.internal.test')
    with patch('lib.safe_fetch.socket.getaddrinfo') as resolve:
        validate_public_url('http://model.internal.test:8000/image',
                            allow_hosts_env='TOFU_TEST_FETCH_ALLOW')
    resolve.assert_not_called()


def test_bare_metadata_ip_cannot_be_allowlisted(monkeypatch):
    monkeypatch.setenv('TOFU_TEST_FETCH_ALLOW', '169.254.169.254')
    with pytest.raises(SafeFetchError, match='blocked IP'):
        validate_public_url('http://169.254.169.254/latest/meta-data/',
                            allow_hosts_env='TOFU_TEST_FETCH_ALLOW')


def test_dns_failure_can_only_be_deferred_for_configuration(monkeypatch):
    def _offline(*_args, **_kwargs):
        raise OSError('resolver offline')

    monkeypatch.setattr('lib.safe_fetch.socket.getaddrinfo', _offline)
    validate_public_url(
        'https://example.test/hook',
        allow_hosts_env='TOFU_TEST_FETCH_ALLOW', allow_unresolved=True)
    with pytest.raises(SafeFetchError, match='DNS resolution failed'):
        validate_public_url(
            'https://example.test/hook',
            allow_hosts_env='TOFU_TEST_FETCH_ALLOW')


def test_adapter_rechecks_each_concrete_redirect_hop(monkeypatch):
    """Requests calls adapter.send per hop; the second private hop is denied."""
    adapter = _PublicEgressAdapter('TOFU_TEST_FETCH_ALLOW')
    public = requests.Request('GET', 'https://public.example/start').prepare()
    private = requests.Request('GET', 'http://127.0.0.1/admin').prepare()
    monkeypatch.setattr('lib.safe_fetch.socket.getaddrinfo', lambda *a, **k: [
        (None, None, None, None, ('93.184.216.34', 443)),
    ])
    sentinel = object()
    with patch('requests.adapters.HTTPAdapter.send', return_value=sentinel):
        assert adapter.send(public) is sentinel
        with pytest.raises(SafeFetchError, match='blocked IP'):
            adapter.send(private)
