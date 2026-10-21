#!/usr/bin/env python3
"""Acceptance: the DEPLOYED tofu_search actually carries the identity-fallback seams.

Discipline mirror: tests/_acceptance_runaway_guards.py ("merged ≠ live").
tofu-search 0.5.2 (commit 495f63f in the tofu-search repo) added five seams
that offer shell/wall outcomes to the host BrowserProvider. The code landing
in the tofu-search repo does NOT change what chatui runs until the package is
re-installed into the serving env — so this script checks the INSTALLED
library, offline, not the repo.

Checks (no network, no pytest — plain script, exit 1 on any FAIL):
  1. installed tofu_search >= 0.5.2 (version floor — informational);
  2. BEHAVIOUR: a 200 SPA-shell outcome drives fetch_page_content into the
     registered BrowserProvider with reason='spa_shell' (the internal SSO-app
     failure shape). On 0.5.1 the provider is never consulted → FAIL;
  3. BEHAVIOUR: browser-first sources avoid cookie replay when the browser
     succeeds; explicit cookies-replay sources recover through the browser
     before any anonymous GET;
  4. chatui seam: install_search_bridge() leaves a browser provider AND an
     auth-source provider registered (the host half of the contract).

Run after deploying tofu-search 0.5.2 into the serving env:
    python3 tests/_acceptance_fetch_identity_fallback.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILURES = []


def check(name, ok, detail=''):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f' — {detail}' if detail else ''))
    if not ok:
        FAILURES.append(name)


def main():
    import tofu_search
    import tofu_search.fetch.core as core
    from tofu_search.providers import (
        AuthSourceProvider,
        BrowserProvider,
        get_auth_source_provider,
        get_browser_provider,
        register_auth_source_provider,
        register_browser_provider,
    )

    print(f'acceptance: tofu_search identity-fallback seams (imported from {tofu_search.__file__})')

    # ── 1. version floor ──
    ver = getattr(tofu_search, '__version__', '0.0.0')
    def _v(s):
        return tuple(int(p) for p in s.split('.')[:3])
    check('version >= 0.5.2', _v(ver) >= (0, 5, 2), f'installed={ver}')

    # ── shared fakes: every network/render hop is stubbed, offline ──
    calls = {'browser': [], 'do_request': 0, 'auth_replay': 0}

    class _ProbeBrowser(BrowserProvider):
        def is_connected(self):
            return True

        def fetch_url(self, url, *, max_chars=None, timeout=15):
            return 'probe-content'

    # Record the reason by intercepting core's module-level _try_browser_fetch
    # (the same name fetch_page_content wraps; this observes the CALL, i.e.
    # whether the pipeline offers the URL to the host browser at all).
    real_browser = core._try_browser_fetch

    def recording_browser(url, max_chars, reason='unknown'):
        calls['browser'].append(reason)
        return real_browser(url, max_chars, reason=reason)

    real_do_request = core._do_request

    class _Resp:
        headers = {'Content-Type': 'text/html; charset=utf-8'}
        encoding = 'utf-8'

    def fake_do_request(url, timeout, **kw):
        calls['do_request'] += 1
        return _Resp(), b'<html><body><div id="app"></div></body></html>'

    saved = {k: getattr(core, k) for k in (
        '_try_browser_fetch', '_do_request', '_get_reader', '_should_fetch',
        '_is_known_spa', '_is_bot_protection', '_is_bot_extracted_text',
        '_extract_html_text', '_looks_like_spa_shell', '_try_playwright_fallback',
        '_try_authenticated_fetch',
    )}
    saved_provider = get_browser_provider()
    saved_auth = get_auth_source_provider()
    try:
        core._try_browser_fetch = recording_browser
        core._do_request = fake_do_request
        core._get_reader = lambda url: None
        core._should_fetch = lambda url: True
        core._is_known_spa = lambda url: False
        core._is_bot_protection = lambda html: False
        core._is_bot_extracted_text = lambda text: False
        core._extract_html_text = lambda html, limit, url=None, scope=None: None
        core._looks_like_spa_shell = lambda html, result: True
        core._try_playwright_fallback = lambda url, max_chars, timeout: None
        register_browser_provider(_ProbeBrowser())

        # ── 2. SPA shell → browser consulted with reason='spa_shell' ──
        out = core.fetch_page_content('https://accept-spa.example.com/app', max_chars=50000)
        check('SPA shell → BrowserProvider consulted (reason=spa_shell)',
              calls['browser'] == ['spa_shell'], f'calls={calls["browser"]}')
        check('SPA shell → provider content returned', out == 'probe-content', f'out={out!r}')

        # ── 3. registry strategy owns identity order and browser recovery ──
        class _Auth(AuthSourceProvider):
            def match_source(self, url):
                return {'domain': 'accept-walled.example.com',
                        'access_strategy': strategy,
                        'cookies': [{'name': 's', 'value': 'x'}]}

        def failed_replay(url, src, mc, timeout):
            calls['auth_replay'] += 1
            return None

        register_auth_source_provider(_Auth())
        core._try_authenticated_fetch = failed_replay
        for strategy, reason, replays in (
                ('browser_first', 'auth_source_browser_first', 0),
                ('cookies_replay', 'auth_source_browser_fallback', 1)):
            calls['browser'].clear()
            calls['do_request'] = calls['auth_replay'] = 0
            out = core.fetch_page_content(
                'https://accept-walled.example.com/' + strategy, max_chars=50000)
            check(f'{strategy} → BrowserProvider consulted ({reason})',
                  calls['browser'] == [reason], f'calls={calls["browser"]}')
            check(f'{strategy} → registry controls cookie replay',
                  calls['auth_replay'] == replays, f'replays={calls["auth_replay"]}')
            check(f'{strategy} → provider content returned', out == 'probe-content')
            check(f'{strategy} → anonymous GET skipped once browser delivers',
                  calls['do_request'] == 0, f'do_request={calls["do_request"]}')
    finally:
        for k, v in saved.items():
            setattr(core, k, v)
        register_browser_provider(saved_provider)
        register_auth_source_provider(saved_auth)

    # ── 4. chatui seam: install_search_bridge registers both providers ──
    try:
        from lib.search.search_bridge import install_search_bridge
        install_search_bridge()
        check('chatui browser provider registered', get_browser_provider() is not None)
        check('chatui auth-source provider registered', get_auth_source_provider() is not None)
    except Exception as e:
        check('chatui seam install', False, f'{type(e).__name__}: {e}')

    if FAILURES:
        print(f'\nACCEPTANCE FAIL ({len(FAILURES)}): {FAILURES}')
        print('hint: inspect the failed behavior and the imported package path; '
              'run the package identity-fallback tests before reinstalling.')
        return 1
    print('\nACCEPTANCE PASS — imported tofu_search carries the identity-fallback seams')
    return 0


if __name__ == '__main__':
    sys.exit(main())
