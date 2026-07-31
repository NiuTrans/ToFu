#!/usr/bin/env python3
"""qr_login_capture.py — QR-scan SSO login capture (headless server variant).

⚠️  MEASURED STATUS (2026-07-28) — READ BEFORE RUNNING
-----------------------------------------------------
This path has been run twice end-to-end and did NOT yield credentials:
the QR was verified on screen and DELIVERED to the operator (as an image), the
operator scanned it and confirmed in 大象 both times, and yet **this script's
headless context saw ZERO cookie change** for the whole window —
``auth_sources`` stayed ``enabled=False cookie_count=0``, no ``login detected``
/ ``SUCCESS`` line was ever emitted, and ``ctxId`` / ``ctxId-<client_id>``
remained byte-identical to the pre-scan baseline (never refreshed by the
server). No SSO event other than our own QR polling appeared.

What that DOES establish: the "QR had expired" hypothesis is disproven — the
second attempt was scanned immediately after the operator saw a freshly
refreshed code.

What it does NOT establish: WHY the confirmation did not flow back to this
context. Candidate directions — device/IP context binding, a PKCE constraint on
which endpoint may confirm, headless fingerprinting — are ALL UNVERIFIED. Do
not treat any of them as the cause, and do not conclude "QR login cannot cross
phone → headless" either: QR login is *designed* for two separate ends and
works that way every day; only this particular run is known to have failed.

So: running this again unchanged is unlikely to help. Investigate one of the
candidate directions first (e.g. compare against a headful run, or watch the
SSO poll XHR for a status transition), then change the script accordingly.
The alternative path is for a human to log in in their OWN browser and paste
the cookie via Settings → 需要登录的来源.

How it works
------------
The SSO login wall on example-corp.com is QR-based (ssosv.internal.example.com/sson/login).
A headless server has no display — but the QR can be SHOWN to the user
remotely: we screenshot it into ``static/tmp/`` (served by the app's built-in
static route), the user scans it with 大象 on their phone, and the login
completes INSIDE this script's Playwright context. We then persist the
resulting session cookies into lib/auth_sources — exactly where the fetch
pipeline's authenticated replay expects them.

Reuses tofu_search.fetch.interactive_login's core idea (wait-for-login-cookie
→ capture), replacing "headful window" with "screenshotted QR + poll".

Security posture:
  * cookie VALUES are never printed or logged (names only);
  * on success we audit_log('cookie_capture', source='qr_login');
  * the QR PNG is deleted on completion (a used login QR is useless anyway).

Usage (usually via nohup so the chat turn can return the QR link first):
  CONDA_PREFIX=<env> python3 tests/_qr_login_capture.py [--timeout 600]
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tofu_search.fetch.playwright_pool  # noqa: F401 — LD_LIBRARY_PATH augmentation
from playwright.sync_api import sync_playwright

DEFAULT_URL = ('https://api.openai.com/ml/modelPlaza/modelInfo'
               '?sortType=releaseTime&labels=modelCapability:%E6%96%87%E6%9C%AC%E7%94%9F%E6%88%90')
DOMAIN = 'example-corp.com'
LOGIN_HINT_COOKIE = 'ssoid'
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'static', 'tmp')
PNG_NAME = 'sso_qr.png'


def _wait_for_qr(page, timeout_s=25):
    """Return a description of a visible QR element, or None.

    Geometry alone is NOT sufficient and an earlier version of this gate proved
    it: the login page carries a large brand illustration on the left
    (540x468, ratio 1.15) which sails through any "big and roughly square"
    test, so the gate passed while the password form was on screen — exactly
    the failure it was written to prevent.

    So the judgement is CONTEXTUAL: the element must be square-ish AND sit
    inside the login card that also announces QR login (\u5927\u8c61\u626b\u63cf /
    \u6247\u7801 / \u4e8c\u7ef4\u7801). The brand art lives outside that card, and the password
    tab's card contains inputs instead of a QR. Both are thereby excluded.

    Returns a short string (tag + size) for logging, never image data.
    """
    import time as _t
    deadline = _t.time() + timeout_s
    js = r"""() => {
      const QR_WORDS = ['\u5927\u8c61\u626b\u63cf', '\u626b\u7801\u767b\u5f55', '\u4e8c\u7ef4\u7801', '\u5feb\u901f\u767b\u5f55'];
      const vis = (el) => {
        const cs = getComputedStyle(el);
        return cs.display !== 'none' && cs.visibility !== 'hidden' &&
               parseFloat(cs.opacity || '1') >= 0.1;
      };
      for (const el of document.querySelectorAll('canvas, img, svg')) {
        const r = el.getBoundingClientRect();
        if (r.width < 100 || r.height < 100) continue;
        if (r.width > 400 || r.height > 400) continue;      // brand art is huge
        const ratio = r.width / r.height;
        if (ratio < 0.9 || ratio > 1.12) continue;          // a QR is SQUARE
        if (!vis(el)) continue;
        if (r.bottom <= 0 || r.right <= 0) continue;
        if (r.top >= innerHeight || r.left >= innerWidth) continue;
        // Contextual anchor: walk up looking for a container that ALSO
        // announces QR login. Caps at 6 levels so we never match <body>.
        let host = el.parentElement, depth = 0, ok = false;
        while (host && depth++ < 6) {
          const t = (host.innerText || '');
          if (t && QR_WORDS.some(w => t.includes(w))) {
            // Reject if that same container holds a credential input \u2014 that
            // would be the password tab, not the QR tab.
            if (!host.querySelector('input[type="password"]')) { ok = true; }
            break;
          }
          host = host.parentElement;
        }
        if (!ok) continue;
        return `${el.tagName.toLowerCase()} ${Math.round(r.width)}x${Math.round(r.height)}`;
      }
      return null;
    }"""
    while _t.time() < deadline:
        try:
            hit = page.evaluate(js)
        except Exception:
            hit = None
        if hit:
            return hit
        page.wait_for_timeout(1000)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default=DEFAULT_URL)
    ap.add_argument('--timeout', type=int, default=600,
                    help='seconds to wait for the user to scan + confirm')
    args = ap.parse_args()

    os.makedirs(STATIC_DIR, exist_ok=True)
    png_path = os.path.join(STATIC_DIR, PNG_NAME)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        # Desktop UA is REQUIRED: the SSO page hides the QR-login tab behind
        # a `hide-in-mobile` class when it sniffs a mobile/headless UA
        # (default Playwright UA contains "HeadlessChrome").
        context = browser.new_context(
            locale='zh-CN',
            user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/126.0.0.0 Safari/537.36'),
        )
        page = context.new_page()
        print(f'[qr] goto {args.url[:90]}', flush=True)
        page.goto(args.url, wait_until='domcontentloaded', timeout=45_000)
        # The SSO login card renders async — wait for it BEFORE trying to
        # switch tabs (clicking at domcontentloaded races the SPA mount and
        # times out). The QR lives on the 二维码登录 tab (default = password form).
        try:
            page.wait_for_selector('text=登录您的账号', timeout=20_000)
        except Exception as e:
            print(f'[qr] login card did not render in time: {e}', flush=True)
        try:
            # page.click('text=二维码登录') CANNOT action this element: the tab
            # is wrapped in a `hide-in-mobile` container (mis-fired in headless
            # regardless of UA spoofing). JS-click the switch div directly.
            clicked = page.evaluate("""() => {
              const sw = document.querySelector('[class*="qrcode-change___"]');
              if (!sw) return false;
              sw.click();
              return true;
            }""")
            print(f'[qr] qrcode-change JS-click: {clicked}', flush=True)
        except Exception as e:
            print(f'[qr] qrcode-change JS-click failed: {e}', flush=True)
        # The QR renders async (canvas or img after XHR) — give it a moment.
        try:
            page.wait_for_selector('canvas, img', timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        # ── HARD GATE: prove the QR is actually on screen before screenshotting.
        # `clicked: True` only proves the switch div was found and a click was
        # dispatched — NOT that the tab changed. Without this gate the script
        # happily screenshots the PASSWORD form and reports success, handing the
        # user an unscannable image. Same failure shape as trusting any
        # easy-to-read proxy instead of the real condition.
        qr = _wait_for_qr(page, timeout_s=25)
        if not qr:
            page.screenshot(path=png_path, full_page=True)
            print('[qr] FAILED — could not switch to the QR tab: no scannable '
                  'QR element became visible.', flush=True)
            print(f'[qr] diagnostic screenshot (NOT a QR) left at {png_path} — '
                  'inspect it to see what the page actually showed.', flush=True)
            browser.close()
            return 2
        print(f'[qr] QR verified visible: {qr}', flush=True)

        page.screenshot(path=png_path, full_page=True)
        print(f'[qr] screenshot saved: {png_path}', flush=True)
        print(f'[qr] serve path: /static/tmp/{PNG_NAME}', flush=True)
        print(f'[qr] login page url: {page.url[:160]}', flush=True)

        deadline = time.time() + args.timeout
        last_shot = time.time()
        ok = False

        # Baseline = cookies present BEFORE the scan. The anonymous SSO chain
        # already sets telemetry / device-fingerprint / PKCE-context cookies
        # (_lxsdk*, WEBDFPID, logan_session_token, webDeviceUuid, ctxId*), so
        # "any cookie appeared" would fire instantly. We wait for a cookie that
        # is NEW relative to this baseline.
        #
        # Why not just wait for `ssoid`: that name was never verified against a
        # real logged-in session — it is this repo's long-standing ASSUMPTION.
        # Hard-coding it means a differently-named session cookie would time the
        # script out and DISCARD a successful login. So the primary signal is
        # "a new session-looking cookie appeared"; LOGIN_HINT_COOKIE is now only
        # a fast-path hint, not a requirement.
        baseline = {c.get('name') for c in context.cookies()}
        print(f'[qr] pre-scan baseline cookies: {sorted(baseline)}', flush=True)

        def _session_candidates(cookies):
            """New cookies that plausibly carry the session.

            Excludes the known-noise families observed in the anonymous run so a
            rotating fingerprint/PKCE value cannot be mistaken for a login.
            """
            noise = ('_lxsdk', 'WEBDFPID', 'logan_session_token',
                     'webDeviceUuid', 'ctxId', 'com.example-corp.speechfe')
            out = []
            for c in cookies:
                n = c.get('name') or ''
                if n in baseline:
                    continue
                if any(n.startswith(p) for p in noise):
                    continue
                out.append(c)
            return out

        while time.time() < deadline:
            try:
                cookies_now = context.cookies()
            except Exception as e:
                print(f'[qr] cookies() read failed: {e}', flush=True)
                break
            names = {c.get('name') for c in cookies_now}
            fresh = _session_candidates(cookies_now)
            if LOGIN_HINT_COOKIE in names or fresh:
                why = ('hint cookie %r present' % LOGIN_HINT_COOKIE
                       if LOGIN_HINT_COOKIE in names
                       else 'new session-candidate cookie(s): %s'
                            % sorted({c.get('name') for c in fresh}))
                print(f'[qr] login detected — {why}', flush=True)
                ok = True
                break
            # QR codes rotate (~1-2 min validity) — refresh the screenshot so a
            # late-scanning user always sees a live QR.
            if time.time() - last_shot > 45:
                try:
                    page.screenshot(path=png_path, full_page=True)
                    last_shot = time.time()
                    print('[qr] refreshed QR screenshot', flush=True)
                except Exception as e:
                    print(f'[qr] screenshot refresh failed: {e}', flush=True)
            time.sleep(2)

        if not ok:
            print(f'[qr] TIMEOUT after {args.timeout}s — no new session cookie '
                  f'appeared (hint {LOGIN_HINT_COOKIE!r} also absent)', flush=True)
            browser.close()
            return 1

        cookies = context.cookies()
        browser.close()

    from lib.auth_sources import upsert_source
    from lib.log import audit_log
    row = upsert_source(DOMAIN, enabled=True, cookies=cookies)
    audit_log('cookie_capture', domain=DOMAIN, source='qr_login',
              cookie_count=len(cookies))
    names = sorted({c.get('name') for c in cookies})
    print(f'[qr] SUCCESS — {len(cookies)} cookies captured for {DOMAIN} '
          f'(names only: {names})', flush=True)
    print(f'[qr] auth-source row: enabled={row.get("enabled")} '
          f'cookie_count={row.get("cookie_count")}', flush=True)
    # The POINT of this run, beyond persisting the session: learn which cookie
    # actually carries it, so DEFAULT_SOURCES['example-corp.com'].fields can be
    # pinned to measured reality instead of an assumption. Values are NEVER
    # printed — name / domain / httpOnly only.
    print('[qr] ---- measured cookie inventory (for fields pinning) ----', flush=True)
    for c in sorted(cookies, key=lambda c: (not c.get('httpOnly'), c.get('name') or '')):
        print(f"[qr]   {c.get('name'):34s} domain={c.get('domain'):24s} "
              f"httpOnly={c.get('httpOnly')} secure={c.get('secure')}", flush=True)

    try:
        os.remove(png_path)
        print('[qr] PNG cleaned up', flush=True)
    except OSError as e:
        print(f'[qr] PNG cleanup failed (remove manually): {e}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
