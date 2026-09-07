"""Render the inline tool-row debug panel in a REAL browser, all three themes,
and measure the PAINTED pixels — not the CSS I wrote.

Why this exists as a separate harness rather than another static pin: every
number in tests/test_debug_panel_contrast.py is computed FROM styles.css, so
it proves my arithmetic, not the render. A cascade mistake (a later rule
out-specifying mine, a token that resolves differently than I assumed, the
kind chip inheriting the wrong ground) would keep that guard green while the
screen stayed washed out. This reads getComputedStyle on the live DOM and
recomputes contrast from what the browser actually resolved.

Not a pytest case: it needs a browser and it is an acceptance instrument.
Run: python3 tests/_ri_panel_visual_check.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

_PAGE = """
<!DOCTYPE html><html data-theme="__THEME__"><head>
<link rel="stylesheet" href="file://__ROOT__/static/styles.css">
<link rel="stylesheet" href="file://__ROOT__/static/request-inspector-trace.css">
</head><body>
<div id="chatinner" style="padding:20px">
  <div data-prn="1"><div class="ptool-line">grep_search</div></div>
</div>
<script>
/* Mount the panel markup EXACTLY as _riMountToolPanel builds it, then fill it
 * with the markup createBlock produces, so the cascade under test is the real
 * one (.ri-state-body .debug-msg-* inside .ri-state-panel). */
const panel = document.createElement('div');
panel.className = 'ri-state-panel';
panel.innerHTML =
  '<div class="ri-state-panel-head">' +
    '<span class="ri-state-panel-kind">__KIND__</span>' +
    '<span class="ri-state-panel-title">Round 10 工具结果后 · +3 msgs</span>' +
    '<span class="ri-state-panel-close">x</span>' +
  '</div>' +
  '<div class="ri-state-body">' +
    '<div class="debug-msg-block open">' +
      '<div class="debug-msg-header"><span class="role-assistant">ASSISTANT</span>' +
      '<span class="debug-msg-summary">#2 · 4.0KB · ~1.2Ktok</span></div>' +
      '<div class="debug-msg-body"><pre>{\\n  <span class="debug-key">"content"</span>: ' +
      '<span class="debug-str">"grep has_app_context"</span>,\\n  ' +
      '<span class="debug-key">"n"</span>: <span class="debug-num">2</span>,\\n  ' +
      '<span class="debug-key">"role"</span>: <span class="debug-null">null</span>\\n}</pre></div>' +
    '</div>' +
    '<div class="debug-msg-block open">' +
      '<div class="debug-msg-header"><span class="role-tool">TOOL</span>' +
      '<span class="debug-msg-summary">#3 · 924B · ~264tok</span></div>' +
      '<div class="debug-msg-body"><pre><span class="debug-str">"ok"</span></pre></div>' +
    '</div>' +
    '<div class="debug-msg-block open">' +
      '<div class="debug-msg-header"><span class="role-system">SYSTEM</span></div>' +
      '<div class="debug-msg-body"><pre>x</pre></div></div>' +
    '<div class="debug-msg-block open">' +
      '<div class="debug-msg-header"><span class="role-user">USER</span></div>' +
      '<div class="debug-msg-body"><pre>x</pre></div></div>' +
    '<div class="debug-msg-block open">' +
      '<div class="debug-msg-header"><span class="role-tools">TOOLS</span></div>' +
      '<div class="debug-msg-body"><pre>x</pre></div></div>' +
  '</div>';
document.querySelector('[data-prn="1"]').insertAdjacentElement('afterend', panel);
</script></body></html>
"""

_PROBE = r"""
() => {
  const lum = (rgb) => {
    const [r, g, b] = rgb;
    const f = (c) => { c /= 255; return c <= 0.03928 ? c/12.92 : Math.pow((c+0.055)/1.055, 2.4); };
    return 0.2126*f(r) + 0.7152*f(g) + 0.0722*f(b);
  };
  const parse = (s) => (s.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
  /* Walk up for the first non-transparent background — the ground the text is
   * ACTUALLY painted on, which is the number that matters. */
  const groundOf = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      const bg = getComputedStyle(n).backgroundColor;
      const m = bg.match(/rgba?\(([^)]+)\)/);
      if (m) {
        const p = m[1].split(',').map(Number);
        if (p.length < 4 || p[3] > 0.9) return [p[0], p[1], p[2]];
      }
      n = n.parentElement;
    }
    const b = getComputedStyle(document.body).backgroundColor;
    return parse(b).length === 3 ? parse(b) : [255, 255, 255];
  };
  const cr = (fg, bg) => {
    const a = lum(fg), b = lum(bg);
    return (Math.max(a,b) + 0.05) / (Math.min(a,b) + 0.05);
  };
  const out = { items: [], panel: {} };
  const sel = {
    'role-system': '.ri-state-body .role-system',
    'role-user': '.ri-state-body .role-user',
    'role-assistant': '.ri-state-body .role-assistant',
    'role-tool': '.ri-state-body .role-tool',
    'role-tools': '.ri-state-body .role-tools',
    'summary': '.ri-state-body .debug-msg-summary',
    'key': '.ri-state-body .debug-key',
    'str': '.ri-state-body .debug-str',
    'num': '.ri-state-body .debug-num',
    'null': '.ri-state-body .debug-null',
    'title': '.ri-state-panel-title',
    'kind-chip': '.ri-state-panel-kind',
    'pre': '.ri-state-body pre',
  };
  for (const [name, s] of Object.entries(sel)) {
    const el = document.querySelector(s);
    if (!el) { out.items.push({ name, missing: true }); continue; }
    const cs = getComputedStyle(el);
    const fg = parse(cs.color);
    const bg = groundOf(el);
    out.items.push({
      name,
      color: cs.color,
      ground: 'rgb(' + bg.join(',') + ')',
      ratio: Math.round(cr(fg, bg) * 100) / 100,
      fontPx: parseFloat(cs.fontSize),
    });
  }
  const p = document.querySelector('.ri-state-panel');
  const chip = document.querySelector('.ri-state-panel-kind');
  out.panel = {
    rendered: !!p && p.getBoundingClientRect().height > 40,
    tabsPresent: !!document.querySelector('.ri-panel-tab'),
    chipVisible: !!chip && chip.getBoundingClientRect().width > 0,
  };
  return out;
}
"""


async def main() -> int:
    # Headless Chrome on this host dies with "libatk-1.0.so.0: cannot open
    # shared object file" unless sys.prefix/lib is on LD_LIBRARY_PATH. That
    # rule already lives in ONE place (the shared chromium_env module the
    # motion-video render chain uses) — reuse it rather than re-deriving it.
    sys.path.insert(0, ROOT)
    try:
        from chromium_env import ensure_chromium_env
        ensure_chromium_env()
    except Exception as e:  # pragma: no cover - diagnostic path
        print(f'warning: chromium_env bootstrap unavailable ({e}); '
              f'the launch may fail on missing GUI libs')

    from playwright.async_api import async_playwright

    tmp_pages = []
    for theme in ('dark', 'light', 'tofu'):
        html = _PAGE.replace('__THEME__', theme).replace('__ROOT__', ROOT) \
                    .replace('__KIND__', 'Result state')
        p = os.path.join('/tmp', f'_ri_panel_{theme}.html')
        with open(p, 'w', encoding='utf-8') as f:
            f.write(html)
        tmp_pages.append((theme, p))

    failures = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=['--no-sandbox',
                                                 '--allow-file-access-from-files'])
        for theme, path in tmp_pages:
            page = await browser.new_page(viewport={'width': 1000, 'height': 800})
            await page.goto('file://' + path)
            await page.wait_for_timeout(350)
            res = await page.evaluate(_PROBE)
            print(f'\n=== theme={theme} ===')
            print(f'  panel: {res["panel"]}')
            if not res['panel']['rendered']:
                failures.append(f'{theme}: panel did not render')
            if res['panel']['tabsPresent']:
                failures.append(f'{theme}: a .ri-panel-tab is still painted')
            if not res['panel']['chipVisible']:
                failures.append(f'{theme}: the axis kind chip is invisible')
            for it in res['items']:
                if it.get('missing'):
                    failures.append(f'{theme}: {it["name"]} not in the DOM')
                    print(f'  {it["name"]:14s} MISSING')
                    continue
                flag = '' if it['ratio'] >= 4.5 else '  <-- FAIL'
                if it['ratio'] < 4.5:
                    failures.append(
                        f'{theme}: {it["name"]} {it["color"]} on {it["ground"]} '
                        f'= {it["ratio"]}:1')
                print(f'  {it["name"]:14s} {it["color"]:22s} on {it["ground"]:18s} '
                      f'{it["ratio"]:6.2f}:1  {it["fontPx"]:>5.1f}px{flag}')
            shot = os.path.join('/tmp', f'_ri_panel_{theme}.png')
            await page.locator('.ri-state-panel').screenshot(path=shot)
            print(f'  screenshot -> {shot}')
            await page.close()
        await browser.close()

    print('\n' + '=' * 60)
    if failures:
        print(f'FAIL — {len(failures)} finding(s):')
        for f in failures:
            print('  •', f)
        return 1
    print('PASS — every painted token clears 4.5:1 in all three themes, '
          'the tab strip is gone, and the axis chip is visible.')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
