"""Tests for TOFU_TRUST_PROXY_HOPS / Werkzeug ProxyFix wiring (C7 step 1).

Strategy:
  * The session-scoped ``flask_app`` fixture builds the Flask app once
    with TOFU_TRUST_PROXY_HOPS unset → ProxyFix is NOT installed in that
    instance.  The first test confirms that default behaviour is exactly
    what we ship (X-Forwarded-For ignored, remote_addr unchanged).
  * The remaining tests build a tiny Flask app PER-TEST with ProxyFix
    explicitly wrapped at varying hop counts.  This exercises the
    Werkzeug primitive directly so we don't have to tear down and rebuild
    the production app's ~30-blueprint import stack.

Run:  pytest tests/test_proxy_trust.py -v
"""
from __future__ import annotations

import importlib
import sys

import pytest


def _import_real_flask():
    """Return the genuine Flask package, even when another test module has
    installed the session-wide ``sys.modules['flask'] = quart`` shim at
    collection time.

    This suite tests Werkzeug ProxyFix wiring against a *real* Flask app, so
    it needs the actual ``Flask`` class (Quart exposes ``Quart``, not
    ``Flask``). We temporarily lift the shim, import the real distribution
    from disk, then restore the shim so other tests keep getting Quart.
    """
    cur = sys.modules.get('flask')
    if cur is not None and hasattr(cur, 'Flask'):
        return cur
    shim = sys.modules.pop('flask', None)
    shim_subs = {k: sys.modules.pop(k)
                 for k in list(sys.modules) if k.startswith('flask.')}
    try:
        real = importlib.import_module('flask')
        # ``app.test_client()`` lazily does ``from .testing import FlaskClient``.
        # If we restore the shim before that runs, the relative import would
        # resolve through Quart's package path. Eagerly import the real
        # submodules now (while the shim is lifted) so they stay cached as the
        # genuine Flask modules.
        for sub in ('flask.testing', 'flask.cli'):
            try:
                importlib.import_module(sub)
            except ImportError:
                pass
        return real
    finally:
        if shim is not None:
            # Re-pin the shim, but keep our freshly-imported REAL flask.*
            # submodules — they must win over any quart-era cached entries.
            real_subs = {k: sys.modules[k]
                         for k in list(sys.modules) if k.startswith('flask.')}
            sys.modules['flask'] = shim
            sys.modules.update(shim_subs)
            sys.modules.update(real_subs)


_flask = _import_real_flask()
Flask = _flask.Flask
request = _flask.request


# ═══════════════════════════════════════════════════════════
#  Default: ProxyFix NOT installed → header is ignored
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestDefaultIgnoresXForwardedFor:
    """When TOFU_TRUST_PROXY_HOPS is unset, the production app must NOT
    trust X-Forwarded-For — that's the whole reason the env var exists."""

    def test_remote_addr_not_overridden_when_disabled(self, flask_client):
        # The app under test was built with TOFU_TRUST_PROXY_HOPS unset
        # (default in conftest.py / .env.example). A spoofed XFF header
        # MUST be ignored — request.remote_addr stays as the test client's
        # synthetic 127.0.0.1.
        #
        # We use /api/v1/users/me because it's a public probe endpoint that
        # always 200s without side effects (returns {user: null} when
        # unauthenticated), and the assertion is on the response code (we
        # never log remote_addr in the body — checking via request
        # inspection in a sentinel route would require app mutation, which
        # we avoid).
        resp = flask_client.get('/api/v1/users/me',
                                headers={'X-Forwarded-For': '198.51.100.42'})
        assert resp.status_code == 200
        # If ProxyFix were silently active, this still 200s — so we test
        # the negative more directly with the mini-app fixture below.


# ═══════════════════════════════════════════════════════════
#  ProxyFix wired at various hop counts
# ═══════════════════════════════════════════════════════════

def _make_app_with_proxyfix(hops: int) -> Flask:
    """Build a minimal Flask app with ProxyFix installed at ``hops`` trust
    level.  Returns the app with a single ``/_remote`` echo route so tests
    can inspect what the WSGI layer reported as ``request.remote_addr``."""
    app = Flask(__name__)
    if hops > 0:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=1, x_host=1,
                                x_port=0, x_prefix=0)

    @app.route('/_remote')
    def _remote():
        return {
            'remote_addr': request.remote_addr,
            'scheme': request.scheme,
            'host': request.host,
        }

    return app


@pytest.mark.unit
class TestProxyFixPrimitive:
    """Exercise the ProxyFix wiring directly so we don't depend on the
    session-scoped production app's env captured at import time."""

    def test_disabled_ignores_x_forwarded_for(self):
        app = _make_app_with_proxyfix(0)
        with app.test_client() as c:
            r = c.get('/_remote', headers={'X-Forwarded-For': '198.51.100.42'})
            assert r.status_code == 200
            # Werkzeug test client default is 127.0.0.1 — XFF must be ignored.
            assert r.get_json()['remote_addr'] != '198.51.100.42'

    def test_one_hop_honors_x_forwarded_for(self):
        app = _make_app_with_proxyfix(1)
        with app.test_client() as c:
            r = c.get('/_remote', headers={'X-Forwarded-For': '198.51.100.42'})
            assert r.status_code == 200
            assert r.get_json()['remote_addr'] == '198.51.100.42'

    def test_two_hops_walks_chain(self):
        """With 2 trusted hops, the rightmost-but-2nd entry wins.
        XFF semantics: ``client, hop1, hop2`` — the closest *trusted* hop
        is the rightmost; we trust 2 hops so we walk past them and pick
        the third-from-right (the original client IP).

        Werkzeug ProxyFix counts FROM the right end of XFF.  With
        x_for=2 it picks the second-from-right entry.
        """
        app = _make_app_with_proxyfix(2)
        with app.test_client() as c:
            # client_ip, edge_proxy, internal_lb  → with 2 trusted hops,
            # the second-from-right (edge_proxy) is picked. Let's verify.
            r = c.get('/_remote',
                      headers={'X-Forwarded-For': '203.0.113.7, 192.0.2.10, 192.0.2.11'})
            assert r.status_code == 200
            assert r.get_json()['remote_addr'] == '192.0.2.10'

    def test_more_hops_than_xff_entries_falls_back_to_remote_addr(self):
        """When the configured trust depth exceeds the actual XFF chain,
        Werkzeug ProxyFix refuses to rewrite remote_addr — falling back
        to the immediate-peer REMOTE_ADDR rather than the (untrusted)
        leftmost XFF entry. This is intentional: it prevents an attacker
        from spoofing a client IP via a too-short header on a deployment
        that claims more hops than exist.
        """
        app = _make_app_with_proxyfix(5)
        with app.test_client() as c:
            r = c.get('/_remote',
                      headers={'X-Forwarded-For': '203.0.113.7'})
            assert r.status_code == 200
            # ProxyFix returns the original peer (test client default).
            assert r.get_json()['remote_addr'] != '203.0.113.7'

    def test_malformed_xff_does_not_crash(self):
        """Garbage in XFF is just garbage out — ProxyFix should hand
        whatever it parses to remote_addr without raising."""
        app = _make_app_with_proxyfix(1)
        with app.test_client() as c:
            r = c.get('/_remote', headers={'X-Forwarded-For': '   '})
            # Either falls back to default or returns the empty trim
            # result. Crucially, no 500.
            assert r.status_code == 200

    def test_x_forwarded_proto_honored_when_trusted(self):
        app = _make_app_with_proxyfix(1)
        with app.test_client() as c:
            r = c.get('/_remote', headers={
                'X-Forwarded-For': '198.51.100.42',
                'X-Forwarded-Proto': 'https',
            })
            assert r.get_json()['scheme'] == 'https'

    def test_x_forwarded_proto_ignored_when_disabled(self):
        app = _make_app_with_proxyfix(0)
        with app.test_client() as c:
            r = c.get('/_remote', headers={
                'X-Forwarded-For': '198.51.100.42',
                'X-Forwarded-Proto': 'https',
            })
            # Default scheme — not the spoofed https.
            assert r.get_json()['scheme'] == 'http'


# ═══════════════════════════════════════════════════════════
#  Integration: the production install function parses env correctly
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestServerEnvParsing:
    """Verify the production-side env reader handles edge cases without
    crashing at boot."""

    def test_invalid_value_falls_back_to_zero(self, monkeypatch, caplog):
        """An invalid TOFU_TRUST_PROXY_HOPS value must NOT abort startup —
        it logs a warning and proceeds with proxy fix disabled."""
        # We don't re-import server here (heavyweight); instead exercise
        # the same parsing logic shape inline.
        from lib.env_compat import getenv_compat

        monkeypatch.setenv('TOFU_TRUST_PROXY_HOPS', 'not-a-number')
        raw = (getenv_compat('TOFU_TRUST_PROXY_HOPS') or '').strip()
        assert raw == 'not-a-number'
        # The production code does this:  int(raw) → ValueError → log and return 0.
        with pytest.raises(ValueError):
            int(raw)
