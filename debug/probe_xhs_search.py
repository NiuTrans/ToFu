"""Live end-to-end smoke test for the Xiaohongshu (小红书) auth-source path.

This is the ONE thing the offline unit tests can't cover: whether the DOM
selectors in ``lib/search/engines/xhs.py`` actually match the live, logged-in
search-results markup (XHS rotates class names, so the extractor is anchored
on stable href patterns but may still need a tweak).

Prerequisite — connect a throwaway account FIRST, one of:
  • Settings → Search → 需要登录的来源 → Xiaohongshu → 浏览器登录 / 粘贴 Cookie, OR
  • this script with --cookie "web_session=...; a1=..." (persists it for you), OR
  • this script with --login (opens a headful browser to log in; needs a display).

Then run the live checks (needs network + Playwright chromium installed):

    python debug/probe_xhs_search.py "拉面"                     # use stored cookies
    python debug/probe_xhs_search.py "拉面" --cookie "web_session=…; a1=…"
    python debug/probe_xhs_search.py --login "拉面"             # capture via headful login
    python debug/probe_xhs_search.py "拉面" --no-fetch          # skip the per-note fetch
    python debug/probe_xhs_search.py "拉面" --keep               # keep cookies after run

By default the script does NOT mutate your stored config beyond what's needed:
if you pass --cookie it upserts+enables for the run and (unless --keep) restores
the source to its prior state at the end. If the source was already connected,
it's left untouched.

What it checks, in order:
  1. Connection state (enabled + cookie count).
  2. Live search via the real Playwright pool (search_authenticated).
     → On 0 results, dumps the raw extractor output AND a snippet of the
       page's anchor hrefs so you can see what selectors to fix.
  3. Live fetch of the first result note (authenticated fetch path).

Exit code 0 = search returned ≥1 normalised result.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_DOMAIN = 'xiaohongshu.com'


def _print_state(tag, A):
    src = A.get_source(_DOMAIN)
    if not src:
        print(f'  [{tag}] no source row')
        return None
    print(f'  [{tag}] enabled={src.get("enabled")} '
          f'cookies={len(src.get("cookies") or [])} '
          f'proxy={"yes" if src.get("proxy") else "no"}')
    return src


def _raw_dom_diagnostics(cookies, proxy):
    """When extraction yields 0 items, scrape raw anchor hrefs so the user can
    see whether the page loaded, hit a login wall, or just changed selectors."""
    from urllib.parse import quote
    from lib.fetch.playwright_pool import _pw_pool

    diag_js = r"""
    (() => {
      const anchors = Array.from(document.querySelectorAll('a[href]'))
        .map(a => a.href)
        .filter(h => /\/(explore|search_result)\//.test(h));
      const bodyLen = (document.body && document.body.innerText || '').length;
      const hasLogin = /登录|扫码|login/i.test(document.body && document.body.innerText || '');
      return {
        anchorCount: anchors.length,
        sampleAnchors: Array.from(new Set(anchors)).slice(0, 10),
        bodyLen: bodyLen,
        looksLikeLogin: hasLogin,
        title: document.title,
      };
    })()
    """
    url = ('https://www.xiaohongshu.com/search_result?keyword='
           + quote('拉面') + '&source=web_search_result_notes')
    print('\n--- RAW DOM DIAGNOSTICS (extractor returned 0) ---')
    out = _pw_pool.search_authenticated(
        url, cookies=cookies, proxy=proxy, timeout=25,
        extractor_js=diag_js, wait_selector='')
    print('  diagnostic payload:', out)
    if isinstance(out, dict):
        if out.get('looksLikeLogin') and not out.get('anchorCount'):
            print('  → Page shows a LOGIN WALL. Cookies are missing/expired — '
                  'reconnect the account.')
        elif out.get('anchorCount'):
            print('  → Anchors EXIST but the engine extractor missed them. '
                  'Update the selectors in lib/search/engines/xhs.py _EXTRACTOR_JS '
                  'to climb from these hrefs to their card title.')
        else:
            print('  → No note anchors at all. Either the search page markup '
                  'changed wholesale or the page did not finish rendering.')


def main() -> int:
    ap = argparse.ArgumentParser(description='Live XHS auth-search smoke test')
    ap.add_argument('query', nargs='?', default='拉面', help='search keyword')
    ap.add_argument('--cookie', default='', help='raw Cookie header to upsert+enable for this run')
    ap.add_argument('--login', action='store_true', help='capture cookies via headful login first')
    ap.add_argument('--no-fetch', action='store_true', help='skip the per-note authenticated fetch')
    ap.add_argument('--keep', action='store_true', help='keep cookies after the run (do not restore)')
    args = ap.parse_args()

    import lib.auth_sources as A
    from lib.search.engines.xhs import search_xhs, xhs_search_available

    print(f'=== XHS live smoke test  query={args.query!r} ===')
    prior = A.get_source(_DOMAIN)
    prior_connected = bool(prior and prior.get('enabled') and prior.get('cookies'))
    restore_needed = False

    # ── Optionally connect for this run ──
    if args.login:
        from lib.fetch.interactive_login import capture_login_cookies
        print('\n[login] opening headful browser — log in, then return here…')
        res = capture_login_cookies(_DOMAIN, 'https://www.xiaohongshu.com/explore', timeout_s=240)
        print('[login] result:', {k: res.get(k) for k in ('ok', 'reason', 'cookie_count', 'error')})
        if not res.get('ok'):
            print('FAILED: interactive login did not capture cookies.')
            return 2
        restore_needed = not args.keep and not prior_connected
    elif args.cookie:
        A.upsert_source(_DOMAIN, cookie_header=args.cookie, enabled=True)
        restore_needed = not args.keep and not prior_connected
        print('\n[connect] upserted cookies from --cookie')

    print('\n[1] connection state:')
    src = _print_state('now', A)
    if not xhs_search_available():
        print('\nNOT CONNECTED. Connect a throwaway account first '
              '(Settings UI, or --cookie / --login). Aborting.')
        return 2

    try:
        # ── 2. Live search ──
        print(f'\n[2] live search via Playwright (this launches chromium)…')
        results = search_xhs(args.query, max_results=10)
        print(f'    → {len(results)} normalised result(s)')
        for i, r in enumerate(results[:10], 1):
            print(f'    {i}. {r["title"][:60]!r}')
            print(f'       {r["url"]}')
            if r.get('snippet'):
                print(f'       snippet: {r["snippet"][:80]!r}')

        if not results:
            _raw_dom_diagnostics(src.get('cookies') or [], src.get('proxy') or '')
            print('\nRESULT: 0 results — see diagnostics above to fix selectors.')
            return 1

        # ── 3. Live fetch of the first note (authenticated path) ──
        if not args.no_fetch:
            from lib.fetch import fetch_page_content
            first = results[0]['url']
            print(f'\n[3] live fetch of first note (auth fetch path):\n    {first}')
            body = fetch_page_content(first, max_chars=2000)
            if body and len(body) > 50:
                print(f'    → fetched {len(body):,} chars; preview:')
                print('    ' + body[:400].replace('\n', '\n    '))
            else:
                print('    → fetch returned little/no content. The note page '
                      'markup or login state may differ from search; check '
                      'lib/fetch/core.py auth routing.')

        print('\nRESULT: OK — XHS search path works end-to-end.')
        return 0
    finally:
        if restore_needed:
            A.delete_source(_DOMAIN)
            print('\n[cleanup] restored xiaohongshu.com to its prior '
                  '(disconnected) state. Pass --keep to retain cookies.')


if __name__ == '__main__':
    raise SystemExit(main())
