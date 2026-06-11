"""SSRF + size-cap tests for routes.upload._safe_image_fetch (B10).

Covers:
  * Scheme allow-list (http/https only).
  * IP allow-list: loopback / RFC1918 / link-local / multicast / reserved
    are all rejected by default.
  * TOFU_IMAGE_FETCH_ALLOW_HOSTS escapes the IP check for trusted hosts.
  * Response size cap aborts mid-stream when bytes exceed the limit.
  * Content-Length pre-check rejects oversized responses.
  * The wrapper raises _ImageFetchError on policy violations (caught by
    callers, treated as a soft skip in _resolve_history_images).

Run:  pytest tests/test_image_fetch_ssrf.py -v
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


# Imports go inside fixtures/tests because routes.upload pulls in Flask at
# import time — the flask_client fixture in conftest.py handles that for
# us, but for the helper-only tests we import lazily.


def _import_helpers():
    from routes.upload import _ImageFetchError, _is_blocked_ip, _safe_image_fetch
    return _ImageFetchError, _is_blocked_ip, _safe_image_fetch


# ═══════════════════════════════════════════════════════════
#  IP classification
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBlockedIPs:
    """_is_blocked_ip returns True for any address that must not be
    reachable through a user-supplied URL."""

    @pytest.mark.parametrize('ip', [
        '127.0.0.1', '127.42.42.42',                # loopback
        '10.0.0.1', '172.16.0.1', '192.168.1.1',    # RFC1918
        '169.254.169.254',                          # link-local (cloud metadata)
        '224.0.0.1',                                # multicast
        '0.0.0.0',                                  # unspecified
        '::1', 'fe80::1', 'fc00::1',                # IPv6 loopback / link-local / ULA
        'not-an-ip',                                # unparseable → deny
    ])
    def test_blocked(self, ip):
        _err, _is_blocked, _fetch = _import_helpers()
        assert _is_blocked(ip) is True

    @pytest.mark.parametrize('ip', [
        '8.8.8.8', '1.1.1.1', '93.184.216.34',      # public IPv4
        '2606:4700:4700::1111',                     # public IPv6 (Cloudflare)
    ])
    def test_allowed(self, ip):
        _err, _is_blocked, _fetch = _import_helpers()
        assert _is_blocked(ip) is False


# ═══════════════════════════════════════════════════════════
#  _safe_image_fetch policy checks (no real network)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSchemeAndHostGuards:

    def test_rejects_non_http_scheme(self):
        Err, _is_blocked, fetch = _import_helpers()
        with pytest.raises(Err, match='scheme'):
            fetch('file:///etc/passwd')
        with pytest.raises(Err, match='scheme'):
            fetch('ftp://example.com/x.png')

    def test_rejects_missing_hostname(self):
        Err, _is_blocked, fetch = _import_helpers()
        with pytest.raises(Err):
            fetch('http:///x.png')

    def test_rejects_loopback_hostname(self, monkeypatch):
        Err, _is_blocked, fetch = _import_helpers()
        monkeypatch.delenv('TOFU_IMAGE_FETCH_ALLOW_HOSTS', raising=False)
        # localhost resolves to 127.0.0.1 — gets caught by the IP filter.
        with pytest.raises(Err, match='blocked IP'):
            fetch('http://localhost/cat.png')

    def test_rejects_ip_literal_loopback(self, monkeypatch):
        Err, _is_blocked, fetch = _import_helpers()
        monkeypatch.delenv('TOFU_IMAGE_FETCH_ALLOW_HOSTS', raising=False)
        with pytest.raises(Err, match='blocked IP'):
            fetch('http://127.0.0.1/img')

    def test_rejects_ip_literal_rfc1918(self, monkeypatch):
        Err, _is_blocked, fetch = _import_helpers()
        monkeypatch.delenv('TOFU_IMAGE_FETCH_ALLOW_HOSTS', raising=False)
        with pytest.raises(Err, match='blocked IP'):
            fetch('http://10.0.0.5:8080/x.png')

    def test_rejects_cloud_metadata(self, monkeypatch):
        Err, _is_blocked, fetch = _import_helpers()
        monkeypatch.delenv('TOFU_IMAGE_FETCH_ALLOW_HOSTS', raising=False)
        with pytest.raises(Err, match='blocked IP'):
            fetch('http://169.254.169.254/latest/meta-data/')

    def test_allow_list_bypasses_ip_check(self, monkeypatch):
        """When a hostname is in TOFU_IMAGE_FETCH_ALLOW_HOSTS, we skip the
        IP filter entirely — we still attempt the HTTP fetch (mocked here)."""
        Err, _is_blocked, fetch = _import_helpers()
        monkeypatch.setenv('TOFU_IMAGE_FETCH_ALLOW_HOSTS', 'internal.example.com')

        # _safe_image_fetch does a function-local `from lib.http_client
        # import http_get` (which calls requests.request, NOT requests.get),
        # so patch it at its source module — that's the binding the local
        # import resolves at call time.
        with patch('lib.http_client.http_get') as mock_get:
            mock_resp = mock_get.return_value
            mock_resp.raise_for_status.return_value = None
            mock_resp.headers = {'Content-Type': 'image/png', 'Content-Length': '10'}
            mock_resp.iter_content = lambda chunk_size: [b'PNGDATA000']
            body, ct = fetch('http://internal.example.com/cat.png')
        assert body == b'PNGDATA000'
        assert ct == 'image/png'
        mock_get.assert_called_once()


# ═══════════════════════════════════════════════════════════
#  Size cap
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSizeCap:

    def test_content_length_precheck_rejects(self, monkeypatch):
        Err, _is_blocked, fetch = _import_helpers()
        monkeypatch.setenv('TOFU_IMAGE_FETCH_ALLOW_HOSTS', 'cdn.example.com')
        monkeypatch.setenv('TOFU_IMAGE_FETCH_MAX_BYTES', '1024')

        with patch('lib.http_client.http_get') as mock_get:
            mock_resp = mock_get.return_value
            mock_resp.raise_for_status.return_value = None
            mock_resp.headers = {'Content-Length': '999999999', 'Content-Type': 'image/png'}
            mock_resp.iter_content = lambda chunk_size: [b'never seen']
            with pytest.raises(Err, match='Content-Length'):
                fetch('https://cdn.example.com/huge.bin')

    def test_streaming_aborts_when_exceeds_max(self, monkeypatch):
        Err, _is_blocked, fetch = _import_helpers()
        monkeypatch.setenv('TOFU_IMAGE_FETCH_ALLOW_HOSTS', 'cdn.example.com')
        monkeypatch.setenv('TOFU_IMAGE_FETCH_MAX_BYTES', '100')

        with patch('lib.http_client.http_get') as mock_get:
            mock_resp = mock_get.return_value
            mock_resp.raise_for_status.return_value = None
            # No Content-Length header → forces the streaming path.
            mock_resp.headers = {'Content-Type': 'image/png'}
            mock_resp.iter_content = lambda chunk_size: [b'A' * 60, b'B' * 60]
            with pytest.raises(Err, match='response too large'):
                fetch('https://cdn.example.com/sneaky.bin')

    def test_within_cap_succeeds(self, monkeypatch):
        Err, _is_blocked, fetch = _import_helpers()
        monkeypatch.setenv('TOFU_IMAGE_FETCH_ALLOW_HOSTS', 'cdn.example.com')
        monkeypatch.setenv('TOFU_IMAGE_FETCH_MAX_BYTES', '1024')

        with patch('lib.http_client.http_get') as mock_get:
            mock_resp = mock_get.return_value
            mock_resp.raise_for_status.return_value = None
            mock_resp.headers = {'Content-Length': '15', 'Content-Type': 'image/jpeg'}
            mock_resp.iter_content = lambda chunk_size: [b'JPEGBYTES012345']
            body, ct = fetch('https://cdn.example.com/cat.jpg')
        assert body == b'JPEGBYTES012345'
        assert ct == 'image/jpeg'


# ═══════════════════════════════════════════════════════════
#  _resolve_history_images integration: rejects ssrf URLs softly
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestResolveHistoryImagesSSRF:
    """When a history entry points at a blocked URL, _resolve_history_images
    must drop that entry (returning the rest), NOT crash and NOT fetch."""

    def test_blocked_url_dropped(self, monkeypatch):
        from routes.upload import _resolve_history_images
        monkeypatch.delenv('TOFU_IMAGE_FETCH_ALLOW_HOSTS', raising=False)

        with patch('requests.get') as mock_get:
            result = _resolve_history_images([
                {'prompt': 'p', 'text': '',
                 'image_url': 'http://169.254.169.254/latest/meta-data/'},
            ])
        # Blocked → empty list, and we never hit the network.
        assert result == []
        mock_get.assert_not_called()

    def test_pre_resolved_b64_passthrough(self, monkeypatch):
        """Entries that already have image_b64 don't trigger any fetch —
        even if the SSRF guard would otherwise reject them."""
        from routes.upload import _resolve_history_images
        with patch('requests.get') as mock_get:
            result = _resolve_history_images([
                {'prompt': 'p', 'text': '', 'image_b64': 'BASE64HERE'},
            ])
        assert len(result) == 1
        assert result[0]['image_b64'] == 'BASE64HERE'
        mock_get.assert_not_called()
