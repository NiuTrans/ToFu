#!/usr/bin/env python3
"""Render a local HTML file to PNG with headless Chrome and report ink geometry.

Usage:
    python3 shoot.py <html-relative-or-abs> <out.png> [--w 1280] [--h 900] [--sel CSS_SELECTOR]

Some hosts' Playwright chrome-headless-shell is missing the GTK/ATK sonames.
Point TOFU_ICON_SHOOT_LIB at a lib dir that provides them (e.g. your conda
env's lib) and it is injected into LD_LIBRARY_PATH; unset means the system
chrome dependencies are complete and nothing is injected.
"""
from __future__ import annotations

import argparse
import os
import sys

_EXTRA_LIB = os.environ.get('TOFU_ICON_SHOOT_LIB', '')

if (_EXTRA_LIB and os.path.isdir(_EXTRA_LIB)
        and _EXTRA_LIB not in os.environ.get('LD_LIBRARY_PATH', '')):
    os.environ['LD_LIBRARY_PATH'] = _EXTRA_LIB + ':' + os.environ.get('LD_LIBRARY_PATH', '')
    os.execv(sys.executable, [sys.executable] + sys.argv)

from playwright.sync_api import sync_playwright  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('out')
    ap.add_argument('--w', type=int, default=1280)
    ap.add_argument('--h', type=int, default=900)
    ap.add_argument('--dpr', type=int, default=2)
    ap.add_argument('--sel', action='append', default=[],
                    help='CSS selector to report bounding box for (repeatable)')
    a = ap.parse_args()

    src = os.path.abspath(a.src)
    with sync_playwright() as p:
        b = p.chromium.launch(args=['--no-sandbox'])
        pg = b.new_page(viewport={'width': a.w, 'height': a.h},
                        device_scale_factor=a.dpr)
        pg.goto('file://' + src)
        pg.wait_for_timeout(1400)
        pg.screenshot(path=a.out, full_page=True)
        for sel in a.sel:
            box = pg.evaluate(
                """(s) => { const el = document.querySelector(s);
                     if (!el) return null;
                     const r = el.getBoundingClientRect();
                     const cs = getComputedStyle(el);
                     return {w: +r.width.toFixed(1), h: +r.height.toFixed(1),
                             font: cs.fontFamily.split(',')[0], size: cs.fontSize,
                             weight: cs.fontWeight, spacing: cs.letterSpacing,
                             color: cs.color}; }""", sel)
            print(f'{sel} -> {box}')
        b.close()
    print(f'wrote {a.out} ({os.path.getsize(a.out)} bytes)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
