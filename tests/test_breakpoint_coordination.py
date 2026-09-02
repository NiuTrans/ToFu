"""Breakpoint coordination drift-guard.

The mobile breakpoint used to be hardcoded (bare `innerWidth <= 768`, a local
`MOBILE_BP = 768`, two `matchMedia('(max-width:768px)')` strings) across
mobile_panels.js / orchestration.js / main_folders_mobile.js, each of which had
to stay in lock-step with the CSS `@media(max-width:768px)` master block. Any
drift silently half-breaks the mobile layout (e.g. the sidebar drawer opens with
no backdrop because the JS predicate and the CSS drawer trigger disagree).

We consolidated onto a SINGLE source of truth: `TOFU_BP.mobile` in
static/js/core.js, plus `isMobileViewport()` / `mobileMediaQuery()`. This test
encodes the two invariants that keep that consolidation honest:

  1. AGREEMENT — the numeric constant in core.js EQUALS the CSS master mobile
     `@media(max-width:NNNpx)` threshold. If someone bumps one without the
     other, this fails.

  2. NO REGRESSION (ratchet) — no source JS file (excluding core.js, which
     DEFINES the constant, and generated bundle-*/feature-* artifacts) may
     contain a bare `innerWidth <op> 768` or a hardcoded
     `matchMedia('(max-width:768px)')` / `MOBILE_BP = 768`. New call sites must
     go through the shared helpers. A `'(max-width:768px)'` used purely as a
     `||` FALLBACK next to `mobileMediaQuery()` is allowed (kept for the case
     where core.js hasn't loaded), as is an embedded CSS `@media` string inside
     a JS-authored `<style>` block.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')
JS_DIR = os.path.join(ROOT, 'frontend', 'src')
CORE_JS = os.path.join(JS_DIR, 'runtime', 'app-runtime.js')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _js_mobile_bp() -> int:
    """Parse `mobile: <int>` out of the frozen TOFU_BP object in core.js."""
    src = _read(CORE_JS)
    m = re.search(r'TOFU_BP\s*=\s*Object\.freeze\(\{\s*mobile:\s*(\d+)', src)
    assert m, 'TOFU_BP = Object.freeze({ mobile: <int> }) not found in core.js'
    return int(m.group(1))


def _js_tablet_bp() -> int:
    """Parse `tablet: <int>` out of the frozen TOFU_BP object in core.js."""
    src = _read(CORE_JS)
    m = re.search(r'TOFU_BP\s*=\s*Object\.freeze\(\{[^}]*\btablet:\s*(\d+)', src)
    assert m, 'TOFU_BP = Object.freeze({ … tablet: <int> }) not found in core.js'
    return int(m.group(1))


def _css_tablet_drawer_bp() -> int:
    """The width in the CSS portrait-tablet drawer predicate
    `@media(...)(max-width:Npx) and (pointer:coarse)`. Both the paper-mode block
    and the new chat-drawer block use the same N — assert they share it."""
    css = _read(CSS)
    hits = set(int(n) for n in re.findall(
        r'\(max-width:(\d+)px\)\s+and\s+\(pointer:coarse\)', css))
    assert hits, 'no `(max-width:Npx) and (pointer:coarse)` predicate found in CSS'
    assert len(hits) == 1, (
        f'multiple DIFFERENT coarse-pointer tablet widths in CSS: {sorted(hits)} — '
        f'they must all share one value (paper mode + chat drawer).')
    return hits.pop()


def _css_master_mobile_bp() -> int:
    """The threshold of the CSS master mobile block — the `@media(max-width:Npx)`
    whose body carries the unique `OVERFLOW CONTAINMENT` marker."""
    css = _read(CSS)
    # Find every @media(max-width:Npx){ header and pick the one whose block body
    # contains the master-block marker.
    for m in re.finditer(r'@media\(max-width:(\d+)px\)\{', css):
        n = int(m.group(1))
        # brace-match this block
        brace = m.end() - 1
        depth = 0
        i = brace
        while i < len(css):
            if css[i] == '{':
                depth += 1
            elif css[i] == '}':
                depth -= 1
                if depth == 0:
                    body = css[brace + 1:i]
                    if 'OVERFLOW CONTAINMENT' in body:
                        return n
                    break
            i += 1
    raise AssertionError('CSS master mobile block (OVERFLOW CONTAINMENT) not found')


# ─────────────────────────── 1. AGREEMENT ───────────────────────────

def test_js_and_css_mobile_breakpoint_agree():
    js = _js_mobile_bp()
    css = _css_master_mobile_bp()
    assert js == css, (
        f'mobile breakpoint drift: core.js TOFU_BP.mobile={js} but the CSS master '
        f'mobile @media block is max-width:{css}px. They MUST match — update both '
        f'or neither.')


def test_js_and_css_tablet_breakpoint_agree():
    """The portrait-tablet drawer threshold must be ONE number: core.js
    TOFU_BP.tablet == the CSS `(max-width:Npx) and (pointer:coarse)` width shared
    by paper mode and the chat drawer. Prevents a second uncoordinated magic
    number for the exact viewport this epic exists to make consistent."""
    js = _js_tablet_bp()
    css = _css_tablet_drawer_bp()
    assert js == css, (
        f'tablet breakpoint drift: core.js TOFU_BP.tablet={js} but the CSS '
        f'coarse-pointer tablet predicate is max-width:{css}px. They MUST match.')


# ─────────── extraction-and-eval: the tablet-drawer JS predicate actually flips ───────────

def _node_eval_tablet_predicate(*, poison: bool, pointer: str = 'coarse',
                                width: int = 800) -> bool:
    """Extract isMobileViewport + isTabletDrawerViewport + TOFU_BP from core.js,
    run them under node with a STUBBED window modelling a *width*px device whose
    pointer is *pointer*, and return isTabletDrawerViewport(). jsdom can't apply
    the sheet or resolve matchMedia semantics, so we eval the real predicate
    source directly.

    When poison=True we corrupt the extracted predicate body (force `return
    false`) to prove the assertion is load-bearing (the NC control)."""
    import json
    import shutil
    import subprocess
    import tempfile

    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for extraction-and-eval')

    src = _read(CORE_JS)

    def _extract(fn_name: str) -> str:
        # Grab `function <name>(...) { ... }` by brace-matching from the header.
        m = re.search(r'function\s+' + re.escape(fn_name) + r'\s*\(', src)
        assert m, f'{fn_name} not found in core.js'
        i = src.find('{', m.end())
        depth = 0
        j = i
        while j < len(src):
            if src[j] == '{':
                depth += 1
            elif src[j] == '}':
                depth -= 1
                if depth == 0:
                    return src[m.start():j + 1]
            j += 1
        raise AssertionError(f'unbalanced braces extracting {fn_name}')

    tofu_bp = f'const TOFU_BP = Object.freeze({{ mobile: {_js_mobile_bp()}, tablet: {_js_tablet_bp()} }});'
    fn_mobile = _extract('isMobileViewport')
    fn_tablet = _extract('isTabletDrawerViewport')
    fn_mq = _extract('tabletDrawerMediaQuery')
    if poison:
        # Neuter the predicate: whatever its body, force it to return false.
        fn_tablet = 'function isTabletDrawerViewport(){ return false; }'

    # Stub a window: 800px wide, coarse pointer → the tablet media query matches.
    harness = f'''
const _W = {width};
const _POINTER = {json.dumps(pointer)};
const window = {{
  innerWidth: _W,
  matchMedia: function(q) {{
    // Model a _W-px device whose primary pointer is _POINTER. A width predicate
    // matches when _W <= its max-width; a `pointer:coarse` predicate matches
    // only when the device pointer is coarse.
    let ok = true;
    const mw = q.match(/max-width:(\\d+)px/);
    if (mw) ok = ok && (_W <= parseInt(mw[1], 10));
    if (/pointer:\\s*coarse/.test(q)) ok = ok && (_POINTER === 'coarse');
    return {{ matches: ok }};
  }}
}};
{tofu_bp}
{fn_mobile}
{fn_mq}
{fn_tablet}
process.stdout.write(JSON.stringify({{
  tablet: !!isTabletDrawerViewport(),
  mobile: !!isMobileViewport()
}}));
'''
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
        f.write(harness)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=20)
        assert out.returncode == 0, f'node eval failed: {out.stderr}'
        data = json.loads(out.stdout)
    finally:
        os.unlink(tmp)
    # Sanity: an 800px coarse device is NOT a phone.
    assert data['mobile'] is False, 'harness bug: 800px should not be mobile'
    return data['tablet']


def test_tablet_drawer_predicate_flips_true_at_800_coarse():
    """POSITIVE: the real isTabletDrawerViewport() returns true for an 800px
    coarse-pointer tablet → the drawer behaviors (backdrop/auto-collapse/swipe
    gates that call isDrawerViewport) will fire."""
    assert _node_eval_tablet_predicate(poison=False) is True, (
        'isTabletDrawerViewport() did not flip true at 800px coarse — the '
        'portrait-tablet drawer would never engage.')


def test_nc_poisoned_tablet_predicate_does_not_flip():
    """POISONED-FIXTURE NC: force the predicate body to `return false` → the
    positive assertion above would fail. Proves the eval is load-bearing (not a
    tautology that passes regardless of the predicate)."""
    assert _node_eval_tablet_predicate(poison=True) is False, (
        'poisoned predicate still returned true — the extraction-and-eval is not '
        'actually exercising the real function body.')


def test_tablet_drawer_predicate_false_for_fine_pointer_desktop():
    """pointer:coarse is LOAD-BEARING: a 900px FINE-pointer desktop (a narrowed
    window, not a touch tablet) must KEEP the pinned two-pane layout — the drawer
    must NOT engage. Confirms requirement (3): landscape/desktop stays split."""
    assert _node_eval_tablet_predicate(poison=False, pointer='fine', width=900) is False, (
        'isTabletDrawerViewport() flipped true for a fine-pointer desktop — the '
        'pointer:coarse guard is not being honored, so desktops would wrongly get '
        'the mobile drawer.')


# ─────────────────────────── 2. NO REGRESSION (ratchet) ───────────────────────────

# Files allowed to mention 768 near a viewport check:
#   core.js         — DEFINES the constant + helpers (and documents the old sites)
_ALLOWED_FILES = {'app-runtime.js', 'core.js'}
_GENERATED_RE = re.compile(r'^(?:bundle|feature|styles)-[0-9a-f]{8}\.js$')

# A bare mobile-width comparison against the literal 768.
_BARE_WIDTH_RE = re.compile(r'innerWidth\s*[<>]=?\s*768')
# A hardcoded local breakpoint constant.
_LOCAL_CONST_RE = re.compile(r'\bMOBILE_BP\s*=\s*768\b')


def _iter_source_js():
    for base, _dirs, files in os.walk(JS_DIR):
        for fn in files:
            if not fn.endswith(('.js', '.ts')):
                continue
            if fn in _ALLOWED_FILES or _GENERATED_RE.match(fn):
                continue
            yield os.path.join(base, fn), fn


def test_no_bare_768_width_check_in_source():
    offenders = []
    for path, fn in _iter_source_js():
        src = _read(path)
        for m in _BARE_WIDTH_RE.finditer(src):
            # Tolerate a bare 768 that is a `||` FALLBACK next to an
            # isMobileViewport() reference (the core-not-loaded guard) — same
            # allowance the media-query fallback gets.
            ctx = src[max(0, m.start() - 160):m.start()]
            if 'isMobileViewport' in ctx:
                continue
            line = src[:m.start()].count('\n') + 1
            offenders.append(f'{fn}:{line}: {m.group(0)}')
    assert not offenders, (
        'bare `innerWidth <op> 768` found — route mobile-width checks through '
        'isMobileViewport() (core.js) instead:\n' + '\n'.join(offenders))


def test_no_local_mobile_bp_constant_in_source():
    offenders = []
    for path, fn in _iter_source_js():
        if _LOCAL_CONST_RE.search(_read(path)):
            offenders.append(fn)
    assert not offenders, (
        'a local `MOBILE_BP = 768` constant was reintroduced (use TOFU_BP.mobile '
        'from core.js): ' + ', '.join(offenders))


def test_hardcoded_mobile_media_query_only_as_fallback():
    """A `matchMedia('(max-width:768px)')` in source JS is only tolerated when it
    sits next to a `mobileMediaQuery()` call as a `||` fallback (core-not-loaded
    guard). A standalone hardcoded one is drift-prone → flagged."""
    offenders = []
    pat = re.compile(r"matchMedia\(\s*['\"]\(max-width:768px\)['\"]\s*\)")
    for path, fn in _iter_source_js():
        src = _read(path)
        for m in pat.finditer(src):
            # Look at the ~120 chars before the match for the fallback marker.
            ctx = src[max(0, m.start() - 120):m.start()]
            if 'mobileMediaQuery' in ctx:
                continue
            line = src[:m.start()].count('\n') + 1
            offenders.append(f'{fn}:{line}')
    assert not offenders, (
        "standalone hardcoded matchMedia('(max-width:768px)') found — call "
        'mobileMediaQuery() (core.js) instead:\n' + '\n'.join(offenders))


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
