"""Development TLS certificates cover reachable names, not bind wildcards."""

from __future__ import annotations

import ipaddress

import pytest


pytestmark = pytest.mark.unit


def test_auto_certificate_uses_configured_and_bind_sans(tmp_path, monkeypatch):
    x509 = pytest.importorskip('cryptography.x509')
    from lib import server_tls
    import server

    monkeypatch.setenv('TOFU_TLS_SANS', 'tofu.test,192.0.2.44')

    cert_path, key_path = server._ensure_tls_certs(
        bind_host='192.0.2.45', data_root=str(tmp_path))
    assert cert_path and key_path
    assert server._ensure_tls_certs is server_tls.ensure_tls_certificates

    with open(cert_path, 'rb') as fh:
        cert = x509.load_pem_x509_certificate(fh.read())
    san = cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value
    dns = set(san.get_values_for_type(x509.DNSName))
    ips = set(san.get_values_for_type(x509.IPAddress))
    assert {'localhost', 'tofu.test'} <= dns
    assert ipaddress.ip_address('127.0.0.1') in ips
    assert ipaddress.ip_address('192.0.2.44') in ips
    assert ipaddress.ip_address('192.0.2.45') in ips
    assert ipaddress.ip_address('0.0.0.0') not in ips


def test_missing_explicit_certificate_never_downgrades_to_http(tmp_path):
    from lib.server_tls import ensure_tls_certificates

    with pytest.raises(FileNotFoundError, match='configured TLS file'):
        ensure_tls_certificates(
            str(tmp_path / 'missing-cert.pem'),
            str(tmp_path / 'missing-key.pem'),
            data_root=str(tmp_path / 'generated'),
        )

    assert not (tmp_path / 'generated').exists()
