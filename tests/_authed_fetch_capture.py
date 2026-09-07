#!/usr/bin/env python3
"""authed_fetch_capture.py — one-shot authenticated-SPA capture harness.

Purpose (docs/modules/browser_automation.md, your-llm-gateway.example.com acceptance):
load an SSO-protected SPA through Playwright with the cookies stored in
lib/auth_sources, RECORD every XHR/fetch request + response status during
the render, and dump the extracted text — one run yields both the content
AND the real data-API endpoint (captured from live traffic, never guessed).

Outcome classes (printed as VERDICT):
  OK             — body text looks like real content (>200 chars, no SSO redirect)
  LOGIN_WALL     — final document redirected to an SSO/passport page, or body
                   stays a tiny shell → cookies missing/insufficient
  RENDERED_EMPTY — page rendered (no SSO redirect) but body text stayed small
                   AND/OR every data XHR failed → inspect the XHR status table

Usage:
  python3 debug/authed_fetch_capture.py [--url URL] [--domain DOMAIN]
                                        [--timeout 45] [--anonymous]
                                        [--out /tmp/capture.json]

  --anonymous   ignore the auth-source store (proves the harness itself and
                shows the no-cookie baseline = the wall we expect to beat)
  --domain      auth-source domain to pull cookies from (default example-corp.com)

Note: on this box CONDA_PREFIX must be set (the live server has it) so
tofu_search's playwright layer can augment LD_LIBRARY_PATH for Chromium.
"""

import argparse
import json
import os
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Importing tofu_search's playwright pool first triggers its LD_LIBRARY_PATH
# augmentation (needs CONDA_PREFIX in env, as the live server has).
import tofu_search.fetch.playwright_pool  # noqa: F401
from playwright.sync_api import sync_playwright

DEFAULT_URL = ('https://api.openai.com/ml/modelPlaza/modelInfo'
               '?sortType=releaseTime&labels=modelCapability:%E6%96%87%E6%9C%AC%E7%94%9F%E6%88%90')
DEFAULT_DOMAIN = 'example-corp.com'
_SSO_MARKERS = ('sso', 'passport', 'login', 'cas')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default=DEFAULT_URL)
    ap.add_argument('--domain', default=DEFAULT_DOMAIN)
    ap.add_argument('--timeout', type=int, default=45)
    ap.add_argument('--anonymous', action='store_true')
    ap.add_argument('--out', default='/tmp/authed_capture.json')
    args = ap.parse_args()

    cookies = []
    if not args.anonymous:
        from lib.auth_sources import get_source
        row = get_source(args.domain)
        if not row or not row.get('cookies'):
            print(f'ERROR: no enabled cookies stored for domain={args.domain} — '
                  'paste them first (Settings → auth sources or POST /api/v1/auth-sources)')
            return 2
        cookies = row['cookies']
        print(f'[harness] loaded {len(cookies)} cookie(s) for {args.domain} '
              f'(names: {[c.get("name") for c in cookies][:8]})')
    else:
        print('[harness] ANONYMOUS mode — no cookies attached')

    xhr = []          # {method, url, status, resource_type, content_type}
    doc_chain = []    # main-document redirect chain

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(locale='zh-CN')
        if cookies:
            context.add_cookies(cookies)
        page = context.new_page()
        page.route('**/*.{png,jpg,jpeg,gif,svg,webp,ico,woff,woff2,ttf,otf,mp4,mp3,webm}',
                   lambda route: route.abort())

        def on_request(req):
            if req.resource_type in ('xhr', 'fetch'):
                xhr.append({'method': req.method, 'url': req.url,
                            'status': None, 'resource_type': req.resource_type,
                            'content_type': ''})

        def on_response(resp):
            if resp.request.resource_type in ('xhr', 'fetch'):
                for entry in reversed(xhr):
                    if entry['url'] == resp.url and entry['status'] is None:
                        entry['status'] = resp.status
                        try:
                            entry['content_type'] = resp.headers.get('content-type', '')
                        except Exception:
                            pass
                        break

        page.on('request', on_request)
        page.on('response', on_response)

        t0 = time.time()
        resp = page.goto(args.url, timeout=args.timeout * 1000,
                         wait_until='domcontentloaded')
        doc_chain.append({'url': args.url, 'status': resp.status if resp else None})
        try:
            page.wait_for_function(
                'document.body && document.body.innerText.trim().length > 200',
                timeout=min(args.timeout, 15) * 1000)
        except Exception:
            pass
        # Extra settle so lazy XHRs (list fetch after first paint) fire.
        page.wait_for_timeout(3000)

        final_url = page.url
        title = page.title()
        try:
            body_text = page.locator('body').text_content(timeout=5000) or ''
        except Exception:
            body_text = page.evaluate('document.body?.innerText || ""')
        body_text = body_text.strip()
        if final_url != args.url:
            doc_chain.append({'url': final_url, 'status': None})
        browser.close()

    elapsed = time.time() - t0
    # Wall detection compares NETLOC, not the full URL: the SSO login page
    # carries the original page (…/modelPlaza/…) as a redirect query param,
    # so substring-matching the whole URL both misses and over-excludes.
    final_host = urlparse(final_url).netloc.lower()
    target_host = urlparse(args.url).netloc.lower()
    final_path = urlparse(final_url).path.lower()
    left_origin = final_host != target_host
    is_sso = (
        (left_origin and any(m in final_host for m in _SSO_MARKERS))
        or final_path.startswith(('/sso', '/login', '/passport'))
        or ('登录' in title)
    )
    failed_xhr = [x for x in xhr if x['status'] and x['status'] >= 400]

    if is_sso or len(body_text) <= 200:
        verdict = 'LOGIN_WALL'
    elif not xhr or (failed_xhr and len(failed_xhr) == len([x for x in xhr if x['status']])):
        verdict = 'RENDERED_EMPTY'
    else:
        verdict = 'OK'

    report = {
        'verdict': verdict,
        'elapsed_s': round(elapsed, 1),
        'final_url': final_url,
        'title': title,
        'body_chars': len(body_text),
        'body_preview': body_text[:1500],
        'doc_chain': doc_chain,
        'xhr': xhr,
    }
    with open(args.out, 'w') as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    print(f'\n===== VERDICT: {verdict} ({elapsed:.1f}s) =====')
    print(f'final_url: {final_url}')
    print(f'title: {title}')
    print(f'body_chars: {len(body_text)}')
    print(f'--- XHR/fetch ({len(xhr)}) ---')
    for x in xhr:
        print(f"  {x['method']:4} {x['status']} {x['url'][:130]}  ({x['content_type'][:40]})")
    print('--- body preview (first 800 chars) ---')
    print(body_text[:800])
    print(f'\nfull report: {args.out}')
    return 0 if verdict == 'OK' else 1


if __name__ == '__main__':
    sys.exit(main())
