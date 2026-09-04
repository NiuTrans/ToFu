"""Brand-area PIXEL guards — the mascot's ground shadow must actually be PAINTED.

WHY A SEPARATE, BROWSER-BACKED SUITE
------------------------------------
tests/test_brand_wordmark_parity.py resolves the CSS cascade in Python. That is
the right tool for "which rule wins", but it is structurally blind to "did this
reach the screen": a rule can resolve to `content:""` + a gradient and still
paint nothing (clipped by an ancestor, covered by a sibling, zero-sized, or
positioned outside the visible box). The mascot's ground shadow is exactly that
kind of decoration — nothing about it is observable except pixels.

The check is a differential render: screenshot the brand area with the shadow
live, screenshot it again with `.welcome-icon::before{content:none}`, and diff.
Any surviving difference IS the shadow, so the assertion cannot pass vacuously.
Both the pixel count AND the location are asserted — a shadow that renders in
the wrong place (e.g. drifting to a positioned ancestor because `.welcome-icon`
lost `position:relative`) is a defect this must catch, not a pass.

TWO HARNESS TRAPS, both hit while writing this (they are why this file inlines
the stylesheet and settles layout before shooting):

1. STALE / UNPARSED EXTERNAL CSS. Loading `styles.css` via `<link>` from a
   file:// page left `document.styleSheets` unreadable, and probes disagreed
   with what was on screen. Inlining the stylesheet into a `<style>` tag removes
   the loader from the experiment entirely.
2. INJECTING STYLES TOO EARLY. `add_style_tag` immediately after `goto` raced
   the first layout/paint, and the "shadow off" screenshot came back identical
   to "shadow on" — reporting a false ZERO for a shadow that was in fact
   painting ~1300 pixels. Every screenshot here waits for fonts + a settle
   delay, and the neuter is baked into the page's own markup rather than
   injected after load.

Skips (never silently passes) when Chromium is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')
ICON = os.path.join(ROOT, 'static', 'icons', 'tofu-welcome.svg')

# This host's playwright chrome-headless-shell is missing GTK/ATK sonames that
# the conda `tofu` env provides. Mirrors static/icons/_gen/wordmark-preview/shoot.py.
_CONDA_LIB = ('/path/to/your/data'
              'your-username/miniforge3/envs/tofu/lib')

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        not os.path.isdir(_CONDA_LIB) and not shutil.which('google-chrome') and not shutil.which('chromium'),
        reason='conda tofu lib and chromium unavailable on this machine'
    )
]

_THEMES = ('tofu', 'dark', 'light')

# The worker runs in a subprocess: LD_LIBRARY_PATH must be set BEFORE the
# process starts, and a crashing browser must not take pytest down with it.
_WORKER = r'''
import json, os, sys
from playwright.sync_api import sync_playwright
from PIL import Image, ImageChops

css_path, icon_path, theme, outdir = sys.argv[1:5]
css = open(css_path, encoding='utf-8').read()

PAGE = """<!doctype html><html lang="zh" data-theme="%s"><head><meta charset="utf-8">
<style>%s</style>
<style>body{margin:0;background:inherit}
 /* NOTE: `*` does NOT match pseudo-elements, so `*{animation:none}` alone left
    the shadow's breathing animation running — the two screenshots then captured
    different animation phases and the diff picked up unrelated motion. The
    pseudo-element selectors are what actually freeze the shadow at its rest
    state (scale 1 / opacity 1), which is what these frozen-frame assertions
    about geometry assume. */
 *,*::before,*::after{animation:none!important;transition:none!important}
 %s</style></head>
<body><div class="welcome"><div class="welcome-icon">
<img src="file://%s" width="64" height="64" alt="Tofu"></div>
<h2 class="tofu-brand"><span class="tofu-brand-t">T</span><span class="tofu-brand-o1">o</span><span
 class="tofu-brand-f">f</span><span class="tofu-brand-u">u</span><small>豆腐</small></h2>
</div></body></html>"""

def shot(kill, tag):
    """Render the brand area. `kill` bakes the neuter into the page itself —
    injecting it after load raced first paint and produced a false zero."""
    extra = '.welcome-icon::before{content:none!important}' if kill else ''
    html = PAGE % (theme, css, extra, icon_path)
    f = os.path.join(outdir, 'p_%s_%s.html' % (theme, tag))
    open(f, 'w', encoding='utf-8').write(html)
    with sync_playwright() as p:
        b = p.chromium.launch(args=['--no-sandbox'])
        pg = b.new_page(viewport={'width': 520, 'height': 400},
                        device_scale_factor=2)
        pg.goto('file://' + f)
        pg.wait_for_load_state('load')
        try:
            pg.evaluate('() => document.fonts.ready')
        except Exception:
            pass
        pg.wait_for_timeout(700)          # settle: layout + paint
        rect = pg.evaluate("""() => {const r =
            document.querySelector('.welcome-icon').getBoundingClientRect();
            return {x:r.x, y:r.y, w:r.width, h:r.height, bottom:r.bottom};}""")
        png = os.path.join(outdir, 'p_%s_%s.png' % (theme, tag))
        pg.screenshot(path=png)
        b.close()
    return png, rect

on, rect = shot(False, 'on')
off, _ = shot(True, 'off')
A = Image.open(on).convert('RGB')
B = Image.open(off).convert('RGB')
d = ImageChops.difference(A, B)
bbox = d.getbbox()
px = sum(1 for q in d.getdata() if sum(q) > 3)
print('RESULT' + json.dumps({'px': px, 'bbox': bbox, 'rect': rect, 'dpr': 2}))
'''


# ── animation-phase worker ───────────────────────────────────────────────────
# The suite above FREEZES animations (`*{animation:none}`) so the diff is stable.
# That makes it structurally blind to motion defects: the ground shadow was being
# carried up and down by the mascot's float (measured: the icon's bottom edge —
# the shadow's GROUND LINE — swung 3.48px), and every frozen-frame assertion
# passed the whole time.
#
# This worker samples geometry at explicit ANIMATION PHASES. It does not sleep
# and hope: it pauses every running animation and seeks `currentTime`, so the
# rest phase (t=0) and the apex (t=half the 4s cycle) are exact and the test is
# deterministic rather than timing-dependent.
_ANIM_WORKER = r'''
import json, os, sys
from playwright.sync_api import sync_playwright

css_path, icon_path, theme, outdir = sys.argv[1:5]
css = open(css_path, encoding='utf-8').read()

PAGE = """<!doctype html><html lang="zh" data-theme="%s"><head><meta charset="utf-8">
<style>%s</style><style>body{margin:0}
 .welcome{min-height:0;padding:40px 0;width:420px}</style></head>
<body><div class="welcome"><div class="welcome-icon">
<img src="file://%s" width="64" height="64" alt="Tofu"></div>
<h2 class="tofu-brand"><span class="tofu-brand-t">T</span><span class="tofu-brand-o1">o</span><span
 class="tofu-brand-f">f</span><span class="tofu-brand-u">u</span><small>豆腐</small></h2>
</div></body></html>"""

SEEK = """(ms) => {
  const anims = document.getAnimations();
  for (const a of anims) { a.pause(); a.currentTime = ms; }
  return anims.length;
}"""

PROBE = """() => {
  const ic = document.querySelector('.welcome-icon');
  const im = ic.querySelector('img');
  const ir = ic.getBoundingClientRect(), mr = im.getBoundingClientRect();
  const pb = getComputedStyle(ic, '::before');
  return {hostBottom: ir.bottom, hostTop: ir.top,
          imgTop: mr.top, shadowOpacity: parseFloat(pb.opacity),
          shadowTransform: pb.transform};
}"""

html = PAGE % (theme, css, icon_path)
f = os.path.join(outdir, 'anim_%s.html' % theme)
open(f, 'w', encoding='utf-8').write(html)

out = {}
with sync_playwright() as p:
    b = p.chromium.launch(args=['--no-sandbox'])
    pg = b.new_page(viewport={'width': 420, 'height': 300}, device_scale_factor=2)
    pg.goto('file://' + f)
    pg.wait_for_load_state('load')
    pg.wait_for_timeout(400)
    # 4s cycle: 0ms is the resting phase, 2000ms is the apex of the float.
    for label, ms in (('rest', 0), ('apex', 2000)):
        out['n_anims'] = pg.evaluate(SEEK, ms)
        out[label] = pg.evaluate(PROBE)
    b.close()
print('RESULT' + json.dumps(out))
'''


def _chromium_available() -> bool:
    try:
        import playwright  # noqa: F401
    except Exception:
        return False
    return os.path.isdir(_CONDA_LIB) or shutil.which('google-chrome') is not None


@pytest.fixture(scope='module')
def _worker_path():
    fd, path = tempfile.mkstemp(suffix='_shadow_worker.py')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(_WORKER)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _render_diff(theme: str, worker: str) -> dict:
    """Return {'px', 'bbox', 'rect', 'dpr'} for shadow-on vs shadow-off."""
    env = dict(os.environ)
    if _CONDA_LIB not in env.get('LD_LIBRARY_PATH', ''):
        env['LD_LIBRARY_PATH'] = _CONDA_LIB + ':' + env.get('LD_LIBRARY_PATH', '')
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run([sys.executable, worker, CSS, ICON, theme, td],
                           capture_output=True, text=True, env=env, timeout=180)
    for line in (r.stdout or '').splitlines():
        if line.startswith('RESULT'):
            import json
            return json.loads(line[len('RESULT'):])
    pytest.skip(f'headless Chromium unavailable / worker failed: '
                f'{(r.stderr or r.stdout or "")[-400:]}')


@pytest.mark.parametrize('theme', _THEMES)
def test_mascot_ground_shadow_is_actually_painted(theme, _worker_path):
    """The ground shadow must produce VISIBLE pixels, in every theme.

    Declaration-level guards cannot see this: the CSS resolved correctly the
    whole time the shadow was being investigated, so only a render proves it.
    """
    if not _chromium_available():
        pytest.skip('playwright/chromium not available on this host')
    res = _render_diff(theme, _worker_path)
    assert res['px'] > 0, (
        f'[{theme}] the mascot ground shadow painted ZERO pixels — it is dead '
        f'decoration. The CSS may resolve fine while the ellipse is clipped, '
        f'covered by the mascot image, or positioned outside the visible box.')


@pytest.mark.parametrize('theme', _THEMES)
def test_ground_shadow_sits_under_the_mascot(theme, _worker_path):
    """The shadow must render UNDER the mascot: horizontally within the icon
    box, and vertically at its bottom edge.

    Pixel-count alone would accept a shadow that escaped to a positioned
    ancestor — precisely the failure mode `.welcome-icon{position:relative}`
    exists to prevent, so the location is asserted too.
    """
    if not _chromium_available():
        pytest.skip('playwright/chromium not available on this host')
    res = _render_diff(theme, _worker_path)
    assert res['px'] > 0, f'[{theme}] no shadow pixels at all'
    dpr = res['dpr']
    x0, y0, x1, y1 = (v / dpr for v in res['bbox'])
    r = res['rect']
    icon_left, icon_right = r['x'], r['x'] + r['w']
    # Horizontal: inside the icon box (small tolerance for the blur falloff).
    assert x0 >= icon_left - 12 and x1 <= icon_right + 12, (
        f'[{theme}] shadow spans x[{x0:.0f}..{x1:.0f}] but the icon is '
        f'x[{icon_left:.0f}..{icon_right:.0f}] — it is not under the mascot.')
    # Vertical: at the mascot's feet, not up around its head.
    assert y1 >= r['y'] + r['h'] * 0.5, (
        f'[{theme}] shadow bottom y={y1:.0f} is above the icon mid-line '
        f'(icon y={r["y"]:.0f} h={r["h"]:.0f}) — a ground shadow belongs at the '
        f'feet.')
    assert abs(y1 - r['bottom']) < 24, (
        f'[{theme}] shadow bottom y={y1:.0f} is {abs(y1 - r["bottom"]):.0f}px '
        f'from the icon bottom ({r["bottom"]:.0f}) — it has drifted away from '
        f'the mascot, most likely anchoring to a different positioned ancestor.')


# ── motion invariants: it is a SHADOW, not a sticker ─────────────────────────

@pytest.fixture(scope='module')
def _anim_worker_path():
    fd, path = tempfile.mkstemp(suffix='_anim_worker.py')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(_ANIM_WORKER)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _anim_phases(theme: str, worker: str, css_path: str | None = None) -> dict:
    """Geometry sampled at the rest phase and the float apex."""
    env = dict(os.environ)
    if _CONDA_LIB not in env.get('LD_LIBRARY_PATH', ''):
        env['LD_LIBRARY_PATH'] = _CONDA_LIB + ':' + env.get('LD_LIBRARY_PATH', '')
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run([sys.executable, worker, css_path or CSS, ICON,
                            theme, td],
                           capture_output=True, text=True, env=env, timeout=180)
    for line in (r.stdout or '').splitlines():
        if line.startswith('RESULT'):
            import json
            return json.loads(line[len('RESULT'):])
    pytest.skip(f'headless Chromium unavailable / anim worker failed: '
                f'{(r.stderr or r.stdout or "")[-400:]}')


@pytest.mark.parametrize('theme', _THEMES)
def test_the_mascot_floats_in_every_theme(theme, _anim_worker_path):
    """The mascot must actually move — and in EVERY theme.

    The float used to be `[data-theme="tofu"]`-only, so dark/light users saw a
    completely static mascot: the same "brand form pinned to one theme" defect as
    the wordmark and the seal, applied to motion.
    """
    if not _chromium_available():
        pytest.skip('playwright/chromium not available on this host')
    res = _anim_phases(theme, _anim_worker_path)
    assert res['n_anims'] > 0, (
        f'[{theme}] no running animations at all — the brand motion is missing '
        f'in this theme.')
    travel = abs(res['apex']['imgTop'] - res['rest']['imgTop'])
    assert travel > 1.0, (
        f'[{theme}] the mascot moved {travel:.2f}px between the rest phase and '
        f'the float apex — it is static. Brand motion must reach every theme, '
        f'not just tofu.')


@pytest.mark.parametrize('theme', _THEMES)
def test_the_ground_line_never_moves(theme, _anim_worker_path):
    """THE shadow invariant: the ground line stays put while the mascot rises.

    This is what separates a shadow from a sticker. When the float sat on
    `.welcome-icon`, its `::before` ground shadow was dragged along with the
    parent — the icon's bottom edge swung 3.48px, so the "ground" floated. The
    float therefore belongs on the mascot IMAGE, leaving the host still.
    """
    if not _chromium_available():
        pytest.skip('playwright/chromium not available on this host')
    res = _anim_phases(theme, _anim_worker_path)
    drift = abs(res['apex']['hostBottom'] - res['rest']['hostBottom'])
    travel = abs(res['apex']['imgTop'] - res['rest']['imgTop'])
    assert drift < 1.0, (
        f'[{theme}] the ground line moved {drift:.2f}px between phases while the '
        f'mascot travelled {travel:.2f}px — the shadow is riding along with the '
        f'mascot instead of staying on the ground, which reads as a sticker. '
        f'The float must not be on `.welcome-icon` (the shadow\'s host); put it '
        f'on `.welcome-icon img`.')


@pytest.mark.parametrize('theme', _THEMES)
def test_the_shadow_breathes_counter_phase(theme, _anim_worker_path):
    """As the mascot rises, its contact shadow must shrink AND fade.

    A ground line that merely holds still is not enough: a fixed-size, fixed-
    opacity ellipse under a bobbing mascot still reads as a decal. Physically the
    contact shadow diffuses as the object leaves the surface.
    """
    if not _chromium_available():
        pytest.skip('playwright/chromium not available on this host')
    res = _anim_phases(theme, _anim_worker_path)
    rest_op = res['rest']['shadowOpacity']
    apex_op = res['apex']['shadowOpacity']
    assert apex_op < rest_op - 0.05, (
        f'[{theme}] shadow opacity is {apex_op:.3f} at the mascot\'s apex vs '
        f'{rest_op:.3f} at rest — it does not fade as the mascot lifts off.')

    def _scale(matrix: str) -> float:
        # 'matrix(a, b, c, d, e, f)' → a is the horizontal scale factor.
        inner = matrix[matrix.find('(') + 1:matrix.rfind(')')]
        return float(inner.split(',')[0]) if inner else 1.0

    rest_s = _scale(res['rest']['shadowTransform'])
    apex_s = _scale(res['apex']['shadowTransform'])
    assert apex_s < rest_s - 0.05, (
        f'[{theme}] shadow scale is {apex_s:.3f} at the apex vs {rest_s:.3f} at '
        f'rest — it does not shrink as the mascot lifts off.')


def test_nc_putting_the_float_back_on_the_host_breaks_the_ground_line(
        _anim_worker_path):
    """NEUTER (in-memory): move the float back onto `.welcome-icon` — exactly the
    shape that shipped — and the ground line must start moving again.

    Without this, `drift < 1.0` could be trivially satisfied by an entirely
    static page, and the invariant would be worthless.
    """
    if not _chromium_available():
        pytest.skip('playwright/chromium not available on this host')
    css = open(CSS, encoding='utf-8').read()
    anchor = '.welcome-icon img{animation:tofuMascotFloat 4s ease-in-out infinite}'
    assert css.count(anchor) == 1, (
        f'NC anchor not unique/found: count={css.count(anchor)}')
    # Reproduce the defect: the float rides the shadow's host again.
    broken = css.replace(
        anchor,
        '.welcome-icon{animation:tofuMascotFloat 4s ease-in-out infinite}', 1)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, 'styles_float_on_host.css')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(broken)
        res = _anim_phases('tofu', _anim_worker_path, css_path=path)
    drift = abs(res['apex']['hostBottom'] - res['rest']['hostBottom'])
    assert drift > 1.0, (
        f'NC did not bite: with the float back on `.welcome-icon` the ground '
        f'line still moved only {drift:.2f}px, so '
        f'test_the_ground_line_never_moves cannot detect the sticker defect.')


def test_the_pixel_probe_can_actually_fail(_worker_path):
    """NEUTER for the probe itself: with the shadow hidden on BOTH renders the
    diff must be empty, proving a non-zero count means something.

    Without this, `px > 0` could be satisfied by any incidental difference
    between the two screenshots (font jitter, animation phase) and the suite
    would pass even if the shadow never painted.
    """
    if not _chromium_available():
        pytest.skip('playwright/chromium not available on this host')
    env = dict(os.environ)
    if _CONDA_LIB not in env.get('LD_LIBRARY_PATH', ''):
        env['LD_LIBRARY_PATH'] = _CONDA_LIB + ':' + env.get('LD_LIBRARY_PATH', '')
    # Feed the worker a stylesheet whose shadow is disabled: on/off are then the
    # same page, so an honest probe reports exactly zero.
    css = open(CSS, encoding='utf-8').read()
    css += '\n.welcome-icon::before{content:none!important}\n'
    with tempfile.TemporaryDirectory() as td:
        neutered = os.path.join(td, 'styles_neutered.css')
        with open(neutered, 'w', encoding='utf-8') as f:
            f.write(css)
        r = subprocess.run([sys.executable, _worker_path, neutered, ICON,
                            'tofu', td],
                           capture_output=True, text=True, env=env, timeout=180)
        payload = None
        for line in (r.stdout or '').splitlines():
            if line.startswith('RESULT'):
                import json
                payload = json.loads(line[len('RESULT'):])
        if payload is None:
            pytest.skip('headless Chromium unavailable for the probe neuter')
    assert payload['px'] == 0, (
        f'probe is not trustworthy: with the shadow disabled in BOTH renders it '
        f'still reported {payload["px"]} differing pixels — the non-zero counts '
        f'elsewhere in this suite could be render noise rather than the shadow.')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
