#!/usr/bin/env python3
"""Mobile full-screen overlays must NOT size height off bare `100dvh`/`100vh`.

Regression context (2026-07-16): the project-assistant panel
(`.project-modal.pm-workbench`) rendered as a thin BLACK LINE in the Android
WebView. Root cause: the phone block (`@media(max-width:768px)`) forced the
modal full-screen with bare `height:100dvh`, but this WebView measures the
initial-containing-block / `dvh` as 0 (documented at styles.css ~21463 and
main.js `_installViewportHeightGuard`). So the flex column collapsed to ~0px,
leaving only the dark header/border band → a black line. Chrome resolves
`dvh` correctly, so it only reproduced in the app.

The project-wide fix for this WebView-collapse class is to size off the
`--vh100` guard var (published by `_installViewportHeightGuard` as a known-good
pixel height), keeping a plain `100dvh`/`100vh` fallback:
    height: var(--vh100, 100dvh)
The same latent bug was found on `.settings-panel` and the base mobile
`.modal` and fixed in the same pass.

Invariant locked here (env-independent CSS parse): inside the phone block
(`@media(max-width:768px)`), any full-screen overlay rule that pins `height`
(or `max-height`) to a full-viewport `100dvh`/`100vh` MUST route it through
`var(--vh100, …)`. A bare `100dvh`/`100vh` on those props is forbidden — it is
the exact shape that collapses to 0 in the WebView.

NEUTER: rewriting a guarded value back to bare `100dvh` re-trips the assertion.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_CSS_PATH = os.path.join(ROOT, 'static', 'styles.css')

# Full-screen overlay selectors that go edge-to-edge on phones and therefore
# pin height to the full viewport. These are the ones the WebView collapses.
_FULLSCREEN_SELECTORS = ('.modal', '.settings-panel', '.project-modal.pm-workbench')

# Any height / max-height declaration + its value (so we can inspect whether a
# full-viewport value is guarded by --vh100).
_HEIGHT_DECL_RE = re.compile(r'((?:max-)?height):\s*([^;]+)')
# A value that references the full viewport (100dvh / 100vh), guarded or not.
_FULLVIEW_VAL_RE = re.compile(r'100d?vh')


def _phone_block(css: str) -> str:
    """Return the body of the `@media(max-width:768px)` phone block
    (brace-matched, nested rules included)."""
    for m in re.finditer(r'@media[^{]*\{', css):
        opener = m.group(0)
        if 'max-width:768px' not in opener.replace(' ', ''):
            continue
        # Skip compound openers (e.g. coarse/tablet bands) — we want the plain
        # phone block that also carries the fullscreen `.modal` rule.
        i = m.end() - 1
        depth = 0
        for j in range(i, len(css)):
            if css[j] == '{':
                depth += 1
            elif css[j] == '}':
                depth -= 1
                if depth == 0:
                    body = css[i + 1:j]
                    if '.project-modal.pm-workbench' in body:
                        return body
                    break
    raise AssertionError(
        'phone @media(max-width:768px) block containing '
        '.project-modal.pm-workbench not found')


def _rule_bodies(block: str, selector: str):
    """Yield the declaration body of every rule for `selector`, matched at a
    class boundary so `.modal` never matches `.modal-overlay`/`.project-modal`.

    The char BEFORE the selector must not be a `-`/word char/`.` (so `.modal`
    is not seen inside `.project-modal`), and the char AFTER must be `{`, `:`,
    ` `, `,` or `.` (rule open, pseudo, combinator, group, or a further class
    like `.project-modal.pm-workbench`)."""
    esc = re.escape(selector)
    pat = re.compile(r'(?<![\w.\-])' + esc + r'(?=[{:,. ])\s*\{([^{}]*)\}')
    for m in pat.finditer(block):
        yield m.group(1)


def test_fullscreen_overlays_use_vh100_guard_not_bare_dvh():
    css = open(_CSS_PATH, encoding='utf-8').read()
    block = _phone_block(css)
    checked = 0
    for selector in _FULLSCREEN_SELECTORS:
        bodies = list(_rule_bodies(block, selector))
        assert bodies, (
            f'expected a phone-block rule for {selector!r} — the positive test '
            f'is stale (selector renamed or rule moved).')
        for body in bodies:
            for prop, val in _HEIGHT_DECL_RE.findall(body):
                if not _FULLVIEW_VAL_RE.search(val):
                    continue  # not a full-viewport height — irrelevant
                assert '--vh100' in val, (
                    f'{selector!r} sets {prop}:{val.strip()!r} using '
                    f'bare 100dvh/100vh — this collapses to 0 in the Android '
                    f'WebView (initial-containing-block measured as 0). Size it '
                    f'off var(--vh100, 100dvh) like the other full-screen '
                    f'overlays (see styles.css --vh100 guard + '
                    f'main.js _installViewportHeightGuard).')
                checked += 1
    assert checked >= len(_FULLSCREEN_SELECTORS), (
        f'expected to verify a full-viewport height on each of '
        f'{_FULLSCREEN_SELECTORS} but only checked {checked} — the rules may '
        f'have stopped pinning height, making this guard vacuous.')


def _base_rule_body(css: str, selector: str) -> str:
    """Return the declaration body of the BASE (non-media-query) rule for
    `selector`. Scans top-level rules only — skips anything inside an @media."""
    esc = re.escape(selector)
    pat = re.compile(r'(?<![\w.\-])' + esc + r'(?=[{:,. ])\s*\{([^{}]*)\}')
    for m in pat.finditer(css):
        # Determine media-nesting depth at this match by counting unbalanced
        # `@media …{` openers before it.
        prefix = css[:m.start()]
        media_open = len(re.findall(r'@media[^{]*\{', prefix))
        # Count closes that belong to @media by brace-matching from each opener
        # is expensive; approximate: a base rule has equal { and } from all
        # @media blocks already closed before it. Use net brace depth instead.
        depth = 0
        for ch in prefix:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
        if depth == 0:  # top-level, not inside any @media
            return m.group(1)
    raise AssertionError(f'base (top-level) rule for {selector!r} not found')


def test_base_pm_workbench_height_uses_vh100_guard():
    """The 1280px landscape tablet (pointer:coarse, width>1024) matches NEITHER
    the phone block NOR any coarse/tablet media query — only the BASE
    `.project-modal.pm-workbench` rule governs it. That base rule pinned
    `height:82vh` (bare vh), which the Android WebView measures as 0 → the panel
    collapsed to a black line even AFTER the phone-block fix. Regression
    2026-07-16 (diagnostics: innerWidth 1280, vh100 published 513px, panel still
    black). The base full-viewport height MUST route through var(--vh100, …)."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    body = _base_rule_body(css, '.project-modal.pm-workbench')
    height_vals = [v for p, v in _HEIGHT_DECL_RE.findall(body) if p == 'height']
    assert height_vals, ('.project-modal.pm-workbench base rule no longer sets '
                         'height — positive test stale.')
    # SOME height declaration must be guarded by --vh100 (the WebView-safe one).
    # A bare `82vh`/`100vh` fallback line is allowed to co-exist BEFORE it.
    assert any('--vh100' in v for v in height_vals), (
        f'.project-modal.pm-workbench base rule sets height={height_vals!r} '
        f'with NO var(--vh100, …) guard — bare vh collapses to 0 in the Android '
        f'WebView on the 1280px tablet. Add a guarded override like '
        f'height:min(720px, calc(var(--vh100,100vh)*0.82)).')


# Preview-modal (model-view / image / PDF popup) base-rule selectors. These
# live in a BASE rule (styles.css ~7013) and are governed by that base rule on
# the 769–1024px coarse-pointer tablet (the phone block's preview fix does not
# reach it). Bare `85vh`/`90vh` there collapses the flex popup to a thin line in
# the Android WebView. Regression 2026-07-17 (model-view button → thin line;
# diagnostics innerWidth 837, vh100 1242px). Unlike the fullscreen overlays
# these use partial-viewport values (85vh/90vh), so match ANY *vh, not 100vh.
#
# `.cost-popover` (2026-07-17) is the same class: the finish-turn cost breakdown
# popover pinned `max-height:calc(100vh - 16px)` in its BASE rule (styles.css
# ~2931). JS positions it via window.innerHeight (fine), but the common case
# (content fits, no inline max-height set) is governed by that CSS 100vh, which
# the WebView measures as 0 → the popover collapsed to a narrow sliver.
_PREVIEW_SELECTORS = ('.preview-text-panel', '.preview-text-body',
                      '.preview-body', '.preview-image', '.cost-popover')
_ANY_VH_RE = re.compile(r'\d+d?vh')


def test_preview_modal_base_heights_use_vh100_guard():
    css = open(_CSS_PATH, encoding='utf-8').read()
    checked = 0
    for selector in _PREVIEW_SELECTORS:
        body = _base_rule_body(css, selector)
        vals = [v for _, v in _HEIGHT_DECL_RE.findall(body)
                if _ANY_VH_RE.search(v)]
        assert vals, (
            f'{selector!r} base rule no longer pins a viewport height — '
            f'positive test stale (rule renamed/moved).')
        assert any('--vh100' in v for v in vals), (
            f'{selector!r} base rule sets a bare vh height {vals!r} with NO '
            f'var(--vh100, …) guard — this collapses to 0 in the Android '
            f'WebView on the 769–1024px tablet, rendering the model-view popup '
            f'as a thin line. Add a guarded override like '
            f'max-height:calc(var(--vh100,100vh)*0.85).')
        checked += 1
    assert checked == len(_PREVIEW_SELECTORS)


def test_NC_neuter_preview_bare_vh_trips_the_guard():
    """NEUTER: strip the --vh100 guard from a preview rule → the predicate flags it."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    body = _base_rule_body(css, '.preview-text-panel')
    neutered = re.sub(r'max-height:\s*calc\(var\(--vh100[^;]*;', '', body)
    vals = [v for _, v in _HEIGHT_DECL_RE.findall(neutered)
            if _ANY_VH_RE.search(v)]
    assert vals, 'neuter removed all viewport heights — unexpected'
    assert not any('--vh100' in v for v in vals), (
        'neuter no-op — .preview-text-panel not in the expected guarded form')


def test_NC_neuter_base_bare_vh_trips_the_guard():
    """NEUTER: strip the base rule's --vh100 guard line → the base-rule
    assertion's core predicate must flag it."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    body = _base_rule_body(css, '.project-modal.pm-workbench')
    # Emulate the pre-fix state: drop any height line that references --vh100.
    neutered = re.sub(r'height:\s*[^;]*--vh100[^;]*;', '', body)
    height_vals = [v for p, v in _HEIGHT_DECL_RE.findall(neutered) if p == 'height']
    assert height_vals, 'neuter removed all height — unexpected'
    assert not any('--vh100' in v for v in height_vals), (
        'neuter no-op — base rule not in the expected guarded form')


def test_NC_neuter_bare_dvh_trips_the_guard():
    """NEUTER: rewrite a guarded value back to bare 100dvh → the guard's core
    predicate must flag it."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    block = _phone_block(css)
    bodies = list(_rule_bodies(block, '.project-modal.pm-workbench'))
    assert bodies, 'coarse .project-modal.pm-workbench missing — positive stale'
    body = bodies[0]
    # Emulate the pre-fix state.
    neutered = re.sub(r'height:\s*var\(--vh100,\s*100dvh\)',
                      'height:100dvh', body)
    assert 'height:100dvh' in neutered and 'var(--vh100' not in neutered, (
        'neuter no-op — the current rule is not the expected guarded form')
    # The positive assertion, applied to the neutered body, must now fail.
    prop = re.search(r'((?:max-)?height):\s*([^;]+)', neutered)
    guarded = prop and '--vh100' in prop.group(2)
    assert not guarded, 'the vh-guard invariant must FAIL on bare 100dvh'


# ══════════════════════════════════════════════════════════════════════
# FULL-SWEEP LOCK (2026-07-17): every overlay/popover/drawer/dropdown/sheet
# whose height sizes off the viewport MUST route through var(--vh100, …). A
# full styles.css scan found these still on bare vh/dvh and they were fixed in
# one pass. Locking them as a selector list so ANY regression to bare vh trips.
#
# Split by governing context on the 837px coarse-pointer tablet:
#   BASE  — the rule that governs when no media query matches (A + B list).
#   PHONE — inside @media(max-width:768px)/(max-width:680px) (the ≤768 sweep).
# EXCLUDED (deliberately, decorative / JS-guarded / never-governs — see JOURNAL
# 2026-07-17): `body` (JS pins px height inline !important), `.paper-report-toc`
# (sticky doc sidebar, not an overlay), `.preset-dropdown` BASE 50vh (overridden
# by guarded bottom-sheets in BOTH touch blocks, so its base never governs).

# (selector, expected-unit-substring) — the unit tells us dvh vs vh so the
# positive test can't go vacuous if a rule stops pinning a viewport height.
_SWEEP_BASE_SELECTORS = (
    '.debug-panel', '.settings-panel', '.stg-modal', '.mcp-install-modal',
    '.timer-log-card', '.memory-modal', '.skills-files-modal',
    '.skills-files-list', '.myday-modal', '.imagegen-fullscreen img',
    '.orch-shell', '.project-brain-influence',
)
_SWEEP_PHONE_SELECTORS = (
    '.delete-turn-popup', '.submenu-dropdown', '.memory-list', '.debug-panel',
    '.preview-text-panel', '.orch-shell', '.orch-tpl-menu', '.myday-modal',
)


def _iter_rules(css):
    """Brace-walk the (comment-stripped) stylesheet, yielding
    (media_ctx_list, selector_prelude, body). Robust exact-prelude parsing."""
    # strip comments preserving newlines
    out = []; i = 0; n = len(css)
    while i < n:
        if css[i] == '/' and i + 1 < n and css[i + 1] == '*':
            j = css.find('*/', i + 2); j = n if j < 0 else j + 2
            out.append('\n' * css.count('\n', i, j)); i = j
        else:
            out.append(css[i]); i += 1
    s = ''.join(out)
    i = 0; n = len(s); stack = []; buf = ''
    while i < n:
        c = s[i]
        if c == '{':
            prelude = buf.strip(); buf = ''
            if prelude.startswith('@'):
                stack.append(prelude); i += 1; continue
            depth = 1; j = i + 1
            while j < n and depth > 0:
                if s[j] == '{': depth += 1
                elif s[j] == '}': depth -= 1
                j += 1
            yield (list(stack), prelude, s[i + 1:j - 1]); buf = ''; i = j; continue
        elif c == '}':
            if stack: stack.pop()
            buf = ''; i += 1; continue
        else:
            buf += c; i += 1


def _prelude_has(prelude, selector):
    """True if `selector` is one of the comma-separated selectors in prelude
    (exact token, trimmed)."""
    return any(part.strip() == selector for part in prelude.split(','))


def _bodies_for(css, selector, *, phone):
    """All rule bodies for `selector`. phone=False → base (no @media); phone=True
    → inside a max-width:768px/680px @media."""
    hits = []
    for media, prelude, body in _iter_rules(css):
        if not _prelude_has(prelude, selector):
            continue
        joined = ' '.join(media).replace(' ', '')
        in_phone = ('max-width:768px' in joined) or ('max-width:680px' in joined)
        if phone and in_phone:
            hits.append(body)
        elif (not phone) and (not media):
            hits.append(body)
    return hits


def test_sweep_base_overlays_use_vh100_guard():
    """Every A/B-list overlay's BASE rule that pins a viewport height must be
    guarded by var(--vh100, …). Governs the 837px coarse tablet."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    for sel in _SWEEP_BASE_SELECTORS:
        bodies = _bodies_for(css, sel, phone=False)
        assert bodies, f'{sel!r} base rule not found — positive test stale.'
        vh_bodies = [b for b in bodies
                     if any(_ANY_VH_RE.search(v) for _, v in _HEIGHT_DECL_RE.findall(b))]
        assert vh_bodies, (f'{sel!r} base rule no longer pins a viewport height '
                           f'— positive test stale (rule moved/renamed).')
        for b in vh_bodies:
            vals = [v for _, v in _HEIGHT_DECL_RE.findall(b) if _ANY_VH_RE.search(v)]
            assert any('--vh100' in v for v in vals), (
                f'{sel!r} base rule sets a bare viewport height {vals!r} with '
                f'NO var(--vh100, …) guard — collapses to 0 in the Android '
                f'WebView. Add a guarded dual declaration.')


def test_sweep_phone_overlays_use_vh100_guard():
    """Every ≤768/≤680 overlay/drawer/dropdown/sheet that pins a viewport
    height must be guarded (the phone-block dvh sweep)."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    for sel in _SWEEP_PHONE_SELECTORS:
        bodies = _bodies_for(css, sel, phone=True)
        assert bodies, f'{sel!r} phone-block rule not found — positive test stale.'
        for b in bodies:
            vals = [v for _, v in _HEIGHT_DECL_RE.findall(b) if _ANY_VH_RE.search(v)]
            if not vals:
                continue
            assert any('--vh100' in v for v in vals), (
                f'{sel!r} phone-block rule sets a bare viewport height {vals!r} '
                f'with NO var(--vh100, …) guard — collapses to 0 in the Android '
                f'WebView. Add a guarded dual declaration.')


def test_NC_neuter_sweep_bare_vh_trips_the_guard():
    """NEUTER: strip the guard from a swept base rule (.stg-modal) → flag it."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    body = _bodies_for(css, '.stg-modal', phone=False)[0]
    neutered = re.sub(r'max-height:\s*calc\(var\(--vh100[^;]*;', '', body)
    vals = [v for _, v in _HEIGHT_DECL_RE.findall(neutered) if _ANY_VH_RE.search(v)]
    assert vals, 'neuter removed all viewport heights — unexpected'
    assert not any('--vh100' in v for v in vals), (
        'neuter no-op — .stg-modal not in the expected guarded form')


if __name__ == '__main__':
    test_fullscreen_overlays_use_vh100_guard_not_bare_dvh()
    test_NC_neuter_bare_dvh_trips_the_guard()
    print('PASS')
