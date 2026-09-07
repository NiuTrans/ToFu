"""Security regression tests for the critical fixes:

  #1 SSRF guard — block fetches to private / loopback / reserved addresses,
     on the initial URL and on redirect hops.
  #2 Insecure SSL fallback — verify=False retry must be opt-in.
"""

import socket

import pytest
import requests

import tofu_search.fetch.core as core
import tofu_search.fetch.utils as utils
from tofu_search import configure

# ── #1 SSRF guard: address classification ──

@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1",
    "169.254.169.254",      # cloud metadata
    "0.0.0.0", "::1", "fe80::1", "fc00::1",
])
def test_blocked_ips(ip):
    assert utils._ip_is_blocked(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_ips_allowed(ip):
    assert utils._ip_is_blocked(ip) is False


def test_host_is_safe_literal_internal():
    assert utils._host_is_safe("127.0.0.1") is False
    assert utils._host_is_safe("169.254.169.254") is False


def test_host_is_safe_literal_public():
    assert utils._host_is_safe("8.8.8.8") is True


def test_host_is_safe_resolves_to_internal(monkeypatch):
    # Hostname that DNS-resolves to a private address must be blocked.
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 0))])
    assert utils._host_is_safe("evil.example.com") is False


def test_host_is_safe_resolves_to_public(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert utils._host_is_safe("example.com") is True


# ── #1 SSRF guard: _should_fetch chokepoint ──

def test_should_fetch_blocks_metadata_endpoint():
    assert utils._should_fetch("http://169.254.169.254/latest/meta-data/") is False


def test_should_fetch_blocks_localhost():
    assert utils._should_fetch("http://localhost:8080/admin") is False
    assert utils._should_fetch("http://127.0.0.1/") is False


def test_should_fetch_can_be_disabled(monkeypatch):
    configure(block_private_addresses=False)
    # With the guard off, localhost passes the SSRF check (other filters still apply).
    assert utils._should_fetch("http://127.0.0.1/") is True


def test_fetch_page_content_blocks_internal():
    # End-to-end: the public entry point refuses an internal URL outright.
    assert core.fetch_page_content("http://169.254.169.254/") is None


# ── #1 SSRF guard: redirect hop coverage via the adapter ──

def test_ssrf_adapter_blocks_internal_redirect_target():
    adapter = utils._SSRFGuardAdapter()
    req = requests.Request("GET", "http://10.0.0.9/internal").prepare()
    with pytest.raises(requests.exceptions.InvalidURL):
        adapter.send(req)


def test_ssrf_adapter_disabled_passes_through(monkeypatch):
    configure(block_private_addresses=False)
    adapter = utils._SSRFGuardAdapter()
    req = requests.Request("GET", "http://10.0.0.9/internal").prepare()
    # With guard off, send() proceeds to the real transport (which we stub).
    sentinel = object()
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send",
                        lambda self, request, **kw: sentinel)
    assert adapter.send(req) is sentinel


# ── #2 Insecure SSL fallback gate ──

def test_insecure_ssl_fallback_off_by_default():
    assert core.get_config().allow_insecure_ssl_fallback is False


def test_ssl_error_returns_none_when_fallback_disabled(monkeypatch):
    # A non-legacy SSLError must NOT silently retry with verify=False.
    calls = []

    def fake_do_request(url, timeout, verify=True, legacy_ssl=False, deadline_ts=None):
        calls.append({"verify": verify, "legacy_ssl": legacy_ssl})
        raise requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")

    monkeypatch.setattr(core, "_do_request", fake_do_request)
    # Use a public host so the SSRF guard doesn't short-circuit first.
    monkeypatch.setattr(utils, "_host_is_safe", lambda host: True)

    result = core.fetch_page_content("https://example.com/")
    assert result is None
    # Only the initial verify=True attempt — no insecure retry.
    assert calls == [{"verify": True, "legacy_ssl": False}]


def test_ssl_error_retries_insecurely_when_enabled(monkeypatch):
    configure(allow_insecure_ssl_fallback=True)
    calls = []

    def fake_do_request(url, timeout, verify=True, legacy_ssl=False, deadline_ts=None):
        calls.append(verify)
        if verify:
            raise requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")
        # Second (insecure) attempt also fails — we only assert it was attempted.
        raise core._HttpError(500, url)

    monkeypatch.setattr(core, "_do_request", fake_do_request)
    monkeypatch.setattr(utils, "_host_is_safe", lambda host: True)

    core.fetch_page_content("https://example.com/")
    assert False in calls  # an insecure (verify=False) retry was attempted
