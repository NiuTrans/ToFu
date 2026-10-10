"""tests/test_ssrf_allowlist.py — hostname-anchored SSRF allowlist.

``allow_private_hosts`` is the EXPLICIT way for a host application to say "I do
mean to fetch this internal host". The design constraint that shapes every test
here: the judgement is anchored on the HOSTNAME, never on the resolved IP. An
internal load balancer rotates its address between lookups (your-llm-gateway.example.com was
observed as both 10.176.18.71 and 10.192.19.176 within one session), so an
IP-based allowlist silently rots — while the hostname stays stable.
"""

from unittest import mock

import pytest

import tofu_search
from tofu_search.fetch.utils import _host_is_allowlisted, _host_is_safe

PRIVATE_A = '10.176.18.71'
PRIVATE_B = '10.192.19.176'


@pytest.fixture(autouse=True)
def _reset_config():
    """Every test starts from the shipped default (allowlist empty)."""
    tofu_search.configure(allow_private_hosts=set())
    yield
    tofu_search.configure(allow_private_hosts=set())


def _resolving_to(addr):
    """Patch getaddrinfo so a hostname resolves to ``addr`` deterministically."""
    return mock.patch(
        'tofu_search.fetch.utils.socket.getaddrinfo',
        return_value=[(2, 1, 6, '', (addr, 443))],
    )


# ── The default posture is unchanged (fail-safe) ──

@pytest.mark.unit
def test_default_allowlist_is_empty_so_private_host_stays_blocked():
    with _resolving_to(PRIVATE_A):
        assert _host_is_safe('your-llm-gateway.example.com') is False


@pytest.mark.unit
def test_public_address_unaffected_by_the_feature():
    with _resolving_to('93.184.216.34'):
        assert _host_is_safe('example.com') is True


# ── The allowlist admits what it names ──

@pytest.mark.unit
def test_exact_hostname_entry_admits_that_host():
    tofu_search.configure(allow_private_hosts={'your-llm-gateway.example.com'})
    with _resolving_to(PRIVATE_A):
        assert _host_is_safe('your-llm-gateway.example.com') is True


@pytest.mark.unit
def test_parent_suffix_entry_admits_subdomain():
    tofu_search.configure(allow_private_hosts={'example-corp.com'})
    with _resolving_to(PRIVATE_A):
        assert _host_is_safe('your-llm-gateway.example.com') is True


@pytest.mark.unit
def test_allowlist_is_ip_independent_across_lb_rotation():
    """THE point of hostname anchoring: the same host admitted at both IPs.

    An IP allowlist would have admitted only one of these two addresses.
    """
    tofu_search.configure(allow_private_hosts={'example-corp.com'})
    for addr in (PRIVATE_A, PRIVATE_B):
        with _resolving_to(addr):
            assert _host_is_safe('your-llm-gateway.example.com') is True, addr


# ── The allowlist refuses what it does NOT name ──

@pytest.mark.unit
def test_non_allowlisted_private_host_still_blocked():
    tofu_search.configure(allow_private_hosts={'example-corp.com'})
    with _resolving_to(PRIVATE_A):
        assert _host_is_safe('internal.evil.io') is False


@pytest.mark.unit
@pytest.mark.parametrize('host', [
    'evil-example-corp.com',        # dash prefix, not a dot boundary
    'notexample_corp.com',
    'example-corp.com.evil.io',     # allowlisted name as a LEFT label
    'xexample_corp.com',
])
def test_suffix_spoofing_is_rejected(host):
    """Suffix matching is anchored on a dot boundary, so these must not match."""
    tofu_search.configure(allow_private_hosts={'example-corp.com'})
    assert _host_is_allowlisted(host) is False


@pytest.mark.unit
def test_literal_private_ip_cannot_launder_through_a_named_host():
    """A bare-IP URL is judged numerically BEFORE the allowlist is consulted."""
    tofu_search.configure(allow_private_hosts={'example-corp.com', PRIVATE_A})
    assert _host_is_safe(PRIVATE_A) is False
    assert _host_is_safe('127.0.0.1') is False
    assert _host_is_safe('169.254.169.254') is False   # cloud metadata


@pytest.mark.unit
def test_loopback_and_metadata_never_admitted_by_a_domain_entry():
    tofu_search.configure(allow_private_hosts={'example-corp.com'})
    with _resolving_to('169.254.169.254'):
        assert _host_is_safe('metadata.google.internal') is False


# ── Entry normalization ──

@pytest.mark.unit
@pytest.mark.parametrize('entry', ['example-corp.com', '.internal.example.com', 'SanKuai.COM', ' example-corp.com '])
def test_entry_forms_are_normalized(entry):
    tofu_search.configure(allow_private_hosts={entry})
    assert _host_is_allowlisted('your-llm-gateway.example.com') is True


@pytest.mark.unit
def test_trailing_dot_host_is_normalized():
    tofu_search.configure(allow_private_hosts={'example-corp.com'})
    assert _host_is_allowlisted('your-llm-gateway.example.com.') is True


@pytest.mark.unit
def test_blank_entries_do_not_admit_everything():
    """A stray '' entry must not degrade into an allow-all."""
    tofu_search.configure(allow_private_hosts={'', '  '})
    assert _host_is_allowlisted('your-llm-gateway.example.com') is False
    with _resolving_to(PRIVATE_A):
        assert _host_is_safe('your-llm-gateway.example.com') is False


# ── The full gate honours the allowlist ──

@pytest.mark.unit
def test_should_fetch_gate_reflects_the_allowlist():
    from tofu_search.fetch.utils import _should_fetch
    url = 'https://api.openai.com/ml/modelPlaza/modelInfo'
    with _resolving_to(PRIVATE_A):
        assert _should_fetch(url) is False
        tofu_search.configure(allow_private_hosts={'example-corp.com'})
        assert _should_fetch(url) is True
