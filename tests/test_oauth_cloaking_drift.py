"""tests/test_oauth_cloaking_drift.py — 伪装层漂移自动警报（E1，设计稿 §4.1）。

Parses the REFERENCE checkout of CLIProxyAPI (``../CLIProxyAPI``, env override
``TOFU_CLIPROXYAPI_PATH``) and pins Tofu's cloaking constants to it:

  * Claude Code version / User-Agent        (helps/claude_device_profile.go)
  * X-Stainless device-profile kit          (same)
  * token endpoint + scope + client_id      (auth/claude/anthropic_auth.go)
  * Codex originator / User-Agent           (codex_executor_request.go)
  * anthropic-beta baseline (wire order, with/without tools)
                                            (claude_executor_request.go)
  * billing salt + cch=00000 OAuth branch   (claude_executor_cloaking.go)
  * egress whitelists (server + agent) cover every subscription host

Why a guard instead of a sync runbook: the cloaking spec is an arms race —
the 2026-07-31 port drifted FOUR ways in four days (2.1.63→2.1.220,
codex_cli_rs→codex-tui, console→platform token endpoint, beta list). Any
future drift turns this suite RED with the exact field that moved; the sync
itself stays a deliberate, verified-by-owner act (docs/modules/remote_execution.md
§4.1: the test ALARMS, it never auto-edits).

The suite skips wholesale when the reference checkout is absent (CI / the
sanitized opensource export).
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

import pytest

from lib.desktop import egress as _server_egress
from lib.desktop_agent import _egress as _agent_egress
import lib.oauth.claude as claude
import lib.oauth.outbound as outbound

pytestmark = pytest.mark.unit

_REF = Path(os.environ.get(
    'TOFU_CLIPROXYAPI_PATH',
    Path(__file__).resolve().parents[2] / 'CLIProxyAPI'))

_SKIP = unittest.skipUnless(
    _REF.is_dir(),
    f'reference checkout {_REF} absent (set TOFU_CLIPROXYAPI_PATH)')


def _read(rel: str) -> str:
    return (_REF / rel).read_text(encoding='utf-8')


def _go_const(text: str, name: str) -> str:
    m = re.search(r'\b%s\s*=\s*"([^"]+)"' % re.escape(name), text)
    assert m, f'Go const {name} not found (reference repo layout changed?)'
    return m.group(1)


def _go_beta_list(text: str, var: str) -> list:
    """Resolve a ``[]string{...}`` block, following ``xxxBeta`` const refs."""
    m = re.search(r'\b%s\s*=\s*\[\]string\{(.*?)\}' % re.escape(var),
                  text, re.S)
    assert m, f'Go list {var} not found'
    out = []
    for tok in re.finditer(r'"([^"]+)"|(\b\w+Beta\b)', m.group(1)):
        lit, ref = tok.groups()
        out.append(lit if lit else _go_const(text, ref))
    return out


def _expected_oauth_betas(with_tools: bool) -> list:
    """Mirror ``claudeCodeCLIBetas`` for the OAuth/cli path: no 1m variant,
    no thinking.display, non-legacy system, no fast mode, no diagnostics."""
    src = _read('internal/runtime/executor/claude_executor_request.go')
    betas = [_go_const(src, 'claudeCodeBeta'),
             _go_const(src, 'claudeOAuthBeta')]
    betas += _go_beta_list(src, 'claudeCodeCLIConstantBetas')
    betas.append(_go_const(src, 'claudeMidConvSystemBeta'))
    if with_tools:
        betas.append(_go_const(src, 'claudeAdvancedToolUseBeta'))
    betas.append(_go_const(src, 'claudeEffortBeta'))
    betas.append(_go_const(src, 'claudeFallbackCreditBeta'))
    betas.append(_go_const(src, 'claudeExtendedCacheTTLBeta'))
    return betas


def _sync_msg(field: str, upstream, ours) -> str:
    return (f'cloaking drift on {field}:\n  upstream(CLIProxyAPI)={upstream!r}'
            f'\n  tofu={ours!r}\n  → sync lib/oauth/outbound.py (and the pins '
            'listed in docs/modules/remote_execution.md), then '
            're-run this suite')


@_SKIP
class TestClaudeIdentityDrift(unittest.TestCase):

    def setUp(self):
        self.profile = _read(
            'internal/runtime/executor/helps/claude_device_profile.go')

    def test_version_and_user_agent(self):
        ua = _go_const(self.profile, 'defaultClaudeFingerprintUserAgent')
        m = re.match(r'claude-cli/(\d+\.\d+\.\d+) ', ua)
        self.assertTrue(m, f'unparseable upstream UA {ua!r}')
        self.assertEqual(outbound.CLAUDE_CODE_VERSION, m.group(1),
                         _sync_msg('CLAUDE_CODE_VERSION', m.group(1),
                                   outbound.CLAUDE_CODE_VERSION))
        self.assertEqual(outbound._CLAUDE_USER_AGENT, ua,
                         _sync_msg('_CLAUDE_USER_AGENT', ua,
                                   outbound._CLAUDE_USER_AGENT))

    def test_stainless_device_profile(self):
        pairs = (
            ('defaultClaudeFingerprintPackageVersion',
             outbound._CLAUDE_STAINLESS_PACKAGE_VERSION),
            ('defaultClaudeFingerprintRuntimeVersion',
             outbound._CLAUDE_STAINLESS_RUNTIME_VERSION),
            ('defaultClaudeFingerprintOS', outbound._CLAUDE_STAINLESS_OS),
            ('defaultClaudeFingerprintArch', outbound._CLAUDE_STAINLESS_ARCH),
        )
        for go_name, ours in pairs:
            self.assertEqual(ours, _go_const(self.profile, go_name),
                             _sync_msg(go_name, _go_const(self.profile, go_name),
                                       ours))

    def test_token_url_scope_client_id(self):
        src = _read('internal/auth/claude/anthropic_auth.go')
        cfg = claude.CLAUDE_OAUTH_CONFIG
        self.assertEqual(cfg['token_url'], _go_const(src, 'TokenURL'),
                         _sync_msg('token_url', _go_const(src, 'TokenURL'),
                                   cfg['token_url']))
        self.assertEqual(cfg['scope'], _go_const(src, 'ClaudeOAuthScope'),
                         _sync_msg('scope', _go_const(src, 'ClaudeOAuthScope'),
                                   cfg['scope']))
        self.assertEqual(cfg['client_id'], _go_const(src, 'ClientID'),
                         _sync_msg('client_id', _go_const(src, 'ClientID'),
                                   cfg['client_id']))


@_SKIP
class TestCodexIdentityDrift(unittest.TestCase):

    def test_originator_and_user_agent(self):
        src = _read('internal/runtime/executor/codex_executor_request.go')
        originator = _go_const(src, 'codexOriginator')
        ua = _go_const(src, 'codexUserAgent')
        self.assertEqual(outbound._CODEX_ORIGINATOR, originator,
                         _sync_msg('_CODEX_ORIGINATOR', originator,
                                   outbound._CODEX_ORIGINATOR))
        self.assertEqual(outbound._CODEX_USER_AGENT, ua,
                         _sync_msg('_CODEX_USER_AGENT', ua,
                                   outbound._CODEX_USER_AGENT))
        # provider_probe carries a SECOND hard-coded copy — pin it too.
        probe_src = Path('lib/provider_probe.py').read_text(encoding='utf-8')
        self.assertIn(f"'originator': '{originator}'", probe_src,
                      _sync_msg('provider_probe originator', originator,
                                'stale literal'))
        self.assertIn(ua, probe_src,
                      _sync_msg('provider_probe UA', ua, 'stale literal'))


@_SKIP
class TestBetaBaselineDrift(unittest.TestCase):

    def test_baseline_without_tools(self):
        expected = _expected_oauth_betas(with_tools=False)
        ours = outbound._merge_betas('', has_tools=False).split(',')
        self.assertEqual(ours, expected,
                         _sync_msg('beta baseline (no tools)', expected, ours))

    def test_baseline_with_tools(self):
        expected = _expected_oauth_betas(with_tools=True)
        ours = outbound._merge_betas('', has_tools=True).split(',')
        self.assertEqual(ours, expected,
                         _sync_msg('beta baseline (tools)', expected, ours))

    def test_caller_betas_still_appended(self):
        # Tofu divergence, deliberate: unknown caller betas append AFTER the
        # baseline (CLIProxyAPI drops them on the Anthropic base). Our own
        # features ride custom betas, so dropping would silently break them.
        merged = outbound._merge_betas('extended-cache-ttl-2025-04-11,foo-bar-1',
                                       has_tools=False)
        betas = merged.split(',')
        self.assertEqual(betas.count('extended-cache-ttl-2025-04-11'), 1)
        self.assertEqual(betas[-1], 'foo-bar-1')


@_SKIP
class TestBillingHeaderDrift(unittest.TestCase):

    def test_salt_and_cch_branch(self):
        src = _read('internal/runtime/executor/claude_executor_cloaking.go')
        salt = _go_const(src, 'fingerprintSalt')
        self.assertEqual(outbound._BILLING_FP_SALT, salt,
                         _sync_msg('fingerprint salt', salt,
                                   outbound._BILLING_FP_SALT))
        upstream_has_cch = 'cch=00000' in src
        ours = outbound._billing_header_text('x')
        self.assertEqual('cch=00000;' in ours, upstream_has_cch,
                         _sync_msg('cch=00000 OAuth branch',
                                   upstream_has_cch, ours))
        self.assertIn('cc_entrypoint=cli;', ours)
        self.assertIn(f'cc_version={outbound.CLAUDE_CODE_VERSION}.', ours)


@_SKIP
class TestEgressWhitelistCoverage(unittest.TestCase):
    """Every host the OAuth flows can target must be egress-eligible on BOTH
    enforcement copies (server + agent) — the token endpoint moved to
    platform.claude.com in the 2.1.220 era; an unlisted host would make the
    geo-blocked deployment unable to refresh."""

    def test_whitelists_cover_subscription_hosts(self):
        cfg = claude.CLAUDE_OAUTH_CONFIG
        from urllib.parse import urlparse
        needed = {
            urlparse(cfg['token_url']).hostname,
            urlparse(cfg['auth_url']).hostname,
            'api.anthropic.com',
            'auth.openai.com',
            'chatgpt.com',
        }
        for wl, side in ((_server_egress.ALLOWED_EGRESS_HOSTS, 'server'),
                         (_agent_egress._ALLOWED_HOSTS, 'agent')):
            missing = needed - set(wl)
            self.assertFalse(
                missing, f'{side} egress whitelist missing {missing} — add '
                'the host(s) (exact-match frozenset) and re-run')


class TestComparatorSelfCheck(unittest.TestCase):
    """NEUTER: the alarm must actually fire on a mismatch (a comparator that
    never fails is worse than none)."""

    def test_sync_message_and_assertion_fire(self):
        with self.assertRaises(AssertionError) as ctx:
            self.assertEqual('a', 'b', _sync_msg('demo', 'b', 'a'))
        self.assertIn('cloaking drift on demo', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
