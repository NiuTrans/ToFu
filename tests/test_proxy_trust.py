"""Behaviour guard: X-Forwarded-For is NEVER trusted, and the docs say so.

Replaces the ProxyFix-wiring suite that used to live in
``tests/test_proxy_trust.py``. That suite was a "dead guard" — the most
dangerous shape in this repo (charter 2026-07-27): it was 9/9 GREEN while the
implementation it claimed to protect had never existed.

What was wrong with it (measured 2026-07-27, epic pt_30d400a167df4440):
  * Its docstring announced "Tests for TOFU_TRUST_PROXY_HOPS / Werkzeug
    ProxyFix wiring", but ``git grep ProxyFix`` outside that file matched
    only a COMMENT in ``.env.example``. The production app never installed
    ProxyFix. Not anchor drift — the guarded implementation never landed.
  * 7 of its 9 tests built their OWN mini Flask app, wrapped ProxyFix on it
    by hand, and asserted that Werkzeug behaves like Werkzeug. That is an
    upstream-library test; it can never fail because of anything in Tofu.
  * ``TestServerEnvParsing`` asserted ``int('not-a-number')`` raises
    ValueError — a Python-builtin test — under a docstring claiming "the
    production code does this", pointing at code that does not exist.
  * Its one test against the real app conceded in its own comment: "If
    ProxyFix were silently active, this still 200s" — i.e. it could not
    detect the condition it was named for.

Why ProxyFix can never be wired here (measured, not inferred):
  * ``server.py:1373`` → ``app = Quart(...)``. Quart is ASGI.
  * ``hasattr(app, 'wsgi_app')`` is **False**; ``asgi_app`` is True.
  * ``ProxyFix`` is WSGI middleware that wraps ``app.wsgi_app``.
  * Neither Quart nor Hypercorn exposes any trusted-proxy / forwarded-header
    option (probed: zero matching attributes on either class).
So "just wire up ProxyFix" is not an option that exists — the ticket's
either/or was really a single choice.

What this file guards instead — the two things that are actually TRUE and
that a future change could plausibly break:

  1. ``X-Forwarded-For`` does not influence ``request.remote_addr``. This is
     the safe default (a spoofed header must never move an IP-keyed
     decision), and the assertion is on the OBSERVED value, so it keeps
     biting if someone later adds an ASGI forwarded-header middleware
     without gating it.
  2. ``.env.example`` does not promise a trusted-proxy feature that the
     server does not implement. A config file that documents a
     non-existent security control is worse than silence: an operator who
     sets it believes real client IPs are being honoured when every
     IP-keyed decision is in fact seeing the proxy's own address.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_proxy_trust.py -v
"""
from __future__ import annotations

import pathlib
import re

import pytest


REPO = pathlib.Path(__file__).resolve().parent.parent


# ═══════════════════════════════════════════════════════════
#  1. Observed behaviour: XFF must not move remote_addr
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestForwardedHeaderNeverTrusted:
    """A spoofed ``X-Forwarded-For`` must not change the peer address the
    app sees, because that address keys real security decisions:

      * per-IP rate-limit buckets — ``lib/rate_limit_api.py:226``,
        ``lib/rate_limiter.py:40``
      * the open-mode loopback grant — ``routes/api_v1/auth.py:173``
      * audit trails — ``routes/api_v1/auth.py:326,360,394``

    These assert the RESULT (what address arrived), never how it got there,
    so the guard survives any reasonable rewrite of the transport layer.
    """

    def _observe_peer(self, flask_client, monkeypatch, path, sent_client, headers):
        """Return the ``remote_addr`` the app observed for one real request.

        Two things this has to get right (both cost a failing run to find):
          * the probe path must be in the auth gate's PUBLIC allow-list,
            otherwise ``auth_before_request`` 401s it and the view never
            runs — the capture dict stays empty and the test reports a
            misleading ``None``;
          * each probe needs a UNIQUE endpoint name, since the app is
            session-scoped and a duplicate name raises
            "View function mapping is overwriting an existing endpoint".
        """
        from quart import request
        from routes.api_v1 import auth as auth_mod

        app = flask_client._c.app
        captured = {}
        endpoint = 'probe_peer_%s' % abs(hash(path))

        if endpoint not in app.view_functions:
            async def _probe():
                captured['addr'] = request.remote_addr
                return {'addr': request.remote_addr}
            app.add_url_rule(path, endpoint, _probe)
        else:
            # Re-point the existing view at THIS call's capture dict.
            async def _probe():
                captured['addr'] = request.remote_addr
                return {'addr': request.remote_addr}
            app.view_functions[endpoint] = _probe

        monkeypatch.setattr(
            auth_mod, '_PUBLIC_EXACT',
            frozenset(auth_mod._PUBLIC_EXACT) | {path})

        flask_client.get(path, headers=headers,
                         scope_base={'client': sent_client})
        assert 'addr' in captured, (
            'probe view never ran for %s — the request was rejected before '
            'reaching it, so this test proves nothing' % path)
        return captured['addr']

    def test_spoofed_xff_does_not_become_the_peer(self, flask_client, monkeypatch):
        """The header names a different IP than the socket — socket wins."""
        addr = self._observe_peer(
            flask_client, monkeypatch, '/_probe_peer_xff',
            ('203.0.113.7', 5555),
            {'X-Forwarded-For': '198.51.100.42'})
        assert addr == '203.0.113.7', (
            'X-Forwarded-For moved remote_addr to %r — a spoofable header '
            'must never key rate limits / loopback grants / audit logs' % (addr,))

    def test_xff_chain_does_not_become_the_peer(self, flask_client, monkeypatch):
        """A multi-hop chain is equally untrusted."""
        addr = self._observe_peer(
            flask_client, monkeypatch, '/_probe_peer_chain',
            ('10.1.2.3', 5555),
            {'X-Forwarded-For': '203.0.113.7, 192.0.2.10, 192.0.2.11'})
        assert addr == '10.1.2.3', (
            'an X-Forwarded-For chain rewrote remote_addr to %r' % (addr,))

    def test_loopback_peer_is_reported_verbatim(self, flask_client, monkeypatch):
        """A same-host reverse proxy presents 127.0.0.1 — pin that reality.

        This is the fact that makes 'loopback == trusted' unsafe: the app
        cannot distinguish a genuine local process from a public request
        forwarded by a proxy on the same box.
        """
        addr = self._observe_peer(
            flask_client, monkeypatch, '/_probe_peer_loopback',
            ('127.0.0.1', 5555),
            {'X-Forwarded-For': '203.0.113.7'})
        assert addr == '127.0.0.1', (
            'expected the verbatim socket peer, got %r' % (addr,))


# ═══════════════════════════════════════════════════════════
#  2. The docs must not promise a control that does not exist
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDocsDoNotPromiseUnwiredProxyTrust:
    """``.env.example`` must not advertise a trusted-proxy knob unless the
    server actually honours it.

    This is a BEHAVIOUR guard on the documentation contract: it asserts the
    OUTCOME ("the file does not promise trusted-proxy support while the
    server has no implementation"), computed from the live tree, rather
    than pinning any particular wording. If someone genuinely wires
    forwarded-header support later, the promise becomes true and the guard
    stops objecting — see ``_server_honours_forwarded_headers``.
    """

    @staticmethod
    def _server_honours_forwarded_headers() -> bool:
        """True when SOME real mechanism CONSUMES X-Forwarded-For.

        Deliberately mechanism-agnostic: ProxyFix (WSGI), an ASGI
        forwarded-header middleware, or a hand-rolled parser all count.

        ⚠️ Must distinguish MENTIONING the header from TRUSTING it. Three
        production files name ``X-Forwarded-For`` today
        (``lib/agent_core/principal.py:12``, ``routes/api_v1/auth.py:167``,
        ``routes/api_v1/browser.py:64``) and every one is a COMMENT saying
        the header is deliberately ignored. A predicate that counts those
        would report "feature exists", which is exactly backwards — and it
        would keep reporting that after someone deleted a real
        implementation and left the comment behind. So we look for the
        syntax of actually reading or installing it, never prose.
        """
        import subprocess
        # Real-consumption signatures only:
        #   headers.get('X-Forwarded-For')  / headers['X-Forwarded-For']
        #   ProxyFix(                        (installation, not the word)
        #   x_for=                           (ProxyFix / uvicorn hop config)
        #   forwarded_allow_ips              (hypercorn/uvicorn trust list)
        patterns = [
            r"headers\s*(\.get\s*\(|\[)\s*['\"][Xx]-[Ff]orwarded-[Ff]or",
            r'ProxyFix\s*\(',
            r'\bx_for\s*=',
            r'\bforwarded_allow_ips\b',
        ]
        found = set()
        for pat in patterns:
            try:
                out = subprocess.run(
                    ['git', 'grep', '-lIE', pat, '--', '*.py'],
                    cwd=str(REPO), capture_output=True, text=True,
                    timeout=60).stdout
            except Exception as e:  # pragma: no cover - git present here
                pytest.skip('git grep unavailable: %s' % e)
            found |= {ln.strip() for ln in out.splitlines() if ln.strip()}
        # This guard file itself documents the patterns; never count it.
        found -= {'tests/test_proxy_trust.py'}
        return any(not f.startswith('tests/') for f in found)

    def test_env_example_does_not_advertise_unwired_proxy_hops(self):
        env = (REPO / '.env.example')
        assert env.is_file(), '.env.example missing'
        text = env.read_text(encoding='utf-8')

        # A PROMISE is a settable variable line — `# TOFU_TRUST_PROXY_HOPS=0`
        # (commented-out entries are the file's way of advertising a knob).
        # Merely NAMING the variable in prose is not a promise: the file is
        # allowed — encouraged — to explain that the setting was removed and
        # why. Matching the bare string would make this guard reject its own
        # fix, which is how a guard ends up being deleted instead of heeded.
        promise = re.search(
            r'^\s*#?\s*[A-Z_]*TRUST_PROXY_HOPS\s*=', text, re.M)
        if not promise:
            return  # nothing advertised — fine

        if self._server_honours_forwarded_headers():
            return  # promise is backed by an implementation — fine

        pytest.fail(
            '.env.example advertises a settable %r but no non-test code '
            'consumes X-Forwarded-For / ProxyFix. An operator who sets it '
            'will believe real client IPs are honoured while every IP-keyed '
            'decision (rate-limit buckets, the open-mode loopback grant, '
            'audit logs) still sees the proxy address. Either wire a real '
            'ASGI forwarded-header mechanism, or state the true semantics: '
            'remote_addr is always the direct peer.'
            % (promise.group(0).strip(),))

    def test_env_example_states_the_true_peer_semantics(self):
        """Silence is not enough — the file must tell operators the truth.

        Removing the false promise without saying what actually happens
        leaves the same trap one step further away: a same-host reverse
        proxy makes every public request look like 127.0.0.1, and an
        operator has no way to know that from the config file.
        """
        text = (REPO / '.env.example').read_text(encoding='utf-8')
        if self._server_honours_forwarded_headers():
            return
        # Look for an explicit statement about the direct-peer semantics
        # near any proxy discussion. Accept any phrasing that names the
        # invariant; we assert the FACT is stated, not its wording.
        has_statement = bool(re.search(
            r'(direct(ly)?[- ]connected peer|direct peer|immediate peer|'
            r'always the (direct|immediate) )', text, re.I))
        assert has_statement, (
            '.env.example must state that remote_addr is always the '
            'directly-connected peer (X-Forwarded-For is ignored), so an '
            'operator behind a same-host reverse proxy understands that '
            'every request appears to come from loopback.')
