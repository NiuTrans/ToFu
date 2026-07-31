"""Drift-guard for the tofu input-box ↔ project-bar visual kinship (2026-07-09).

The tofu project bar became a rich "gallery-hung diorama" (warm frame + soft
two-layer elevation + frosted-glass pills + a per-scene [data-decor] wash). The
input box was DELIBERATELY stripped bare in an earlier pass (every decorative
pseudo-element retired to content:none). This optimization makes the input box
"follow suit" with the bar's NEW refined vocabulary WITHOUT re-adding the
retired ornaments and WITHOUT ever putting a moving/blurred layer behind the
live typing surface. Four items:

  1. Shared gallery frame — .input-box gets the bar's slim warm frame line +
     soft two-layer shadow + 14px radius.
  2. Frosted-glass toolbar pills — preset-toggle / submenu-trigger /
     search-mode-toggle gain backdrop-filter:blur (the bar pill idiom).
  3. Warm focus — :focus-within warms the frame + a STATIC amber ring (no lift,
     no animation).
  4. Scene-tinted ATMOSPHERIC WATERCOLOR WASH (smooth NON-tiled gradients:
     light top band → scene colour pooled LOW + soft complementary blooms,
     mirroring the bar's canvas palette; static) behind the TOOLBAR TRAY
     (.input-actions) ONLY, read from the sibling .project-bar[data-decor] via
     a pure-CSS ~ selector.

Owner's non-negotiable guardrails, encoded as assertions:
  (a) the tofu TEXTAREA rule carries NO backdrop-filter and NO animation, and
      the retired input-box/-area/-row/-inner decorative pseudo-elements stay
      content:none (never resurrected);
  (b) the warm focus-within rule exists (warm frame color + amber ring, no
      transform lift);
  (c) the frame kinship (14px radius + two-layer shadow) AND the frosted-pill
      kinship (backdrop-filter on the toolbar pills) both exist;
  (d) the item-4 scene wash is scoped to `.input-actions` (the tray) and is
      NEVER applied to the textarea / input-row / whole input-box.

Plus NEUTER tests that re-poison a COPY of the CSS to prove each guard is
load-bearing (a faithful revert must flip the assertion False).

Env-independent: parses static/styles.css directly (no node/jsdom), using the
same _strip_comments + brace-split rule-splitter as the sibling styles.css
drift-guards (comments can hold literal braces — the documented test trap).
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _strip_comments(css: str) -> str:
    """Remove /* … */ comments (they can contain literal braces that corrupt the
    naive brace-based rule splitter — the documented styles.css test trap).

    Delegates to the SINGLE shared implementation (charter #24).

    EQUIVALENCE, MEASURED on the real 22k-line static/styles.css rather than
    assumed: the local ``re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)`` this
    replaced and ``strip_comments(lang='css', inline=True)`` produce an
    IDENTICAL selector set (6466 rules, 0 selectors unique to either side) and
    a byte-identical whitespace-stripped content signature. They differ only in
    LINE NUMBERING -- the shared one blanks comment lines to preserve line
    count, the local one deleted them (20295 vs 22400 lines) -- which leaves 25
    rule bodies differing in whitespace alone. Every assertion here is
    whitespace-insensitive (substring / regex on a rule body), so the swap is
    behaviour-preserving; the suite is the proof.

    Keeping N copies of "what counts as a comment" is what let a fix land in one
    copy and not its duplicate -- incident 3 in the shared module's docstring.
    """
    from tests._source_scan import strip_comments
    return strip_comments(css, lang='css', inline=True)


def _rules(css: str):
    """Yield (normalized-selector, body) for every innermost rule."""
    css = _strip_comments(css)
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        sel = re.sub(r'\s+', ' ', m.group(1)).strip()
        yield sel, m.group(2)


def _rule_body(css: str, selector: str) -> str | None:
    """Return the body of the FIRST rule whose selector-list is EXACTLY
    `selector` (whitespace-normalized)."""
    want = re.sub(r'\s+', ' ', selector).strip()
    for sel, body in _rules(css):
        if sel == want:
            return body
    return None


# ── Selectors under test ──────────────────────────────────────────────────
BOX = '[data-theme="tofu"] .input-box'
FOCUS = '[data-theme="tofu"] .input-box:focus-within'
TEXTAREA = '[data-theme="tofu"] textarea'
FROSTED_PILLS = (
    '[data-theme="tofu"] .preset-toggle',
    '[data-theme="tofu"] .submenu-trigger',
    '[data-theme="tofu"] .search-mode-toggle',
)


# ═══════════════════════════════════════════════════════════════════════════
#  (a) TEXTAREA stays clean; retired decorative pseudo-elements stay retired
# ═══════════════════════════════════════════════════════════════════════════
def test_tofu_textarea_has_no_blur_or_animation():
    """The live typing surface must never get a backdrop-filter or animation —
    that is the owner's hard legibility guardrail."""
    css = _read(CSS)
    for sel, body in _rules(css):
        if sel != re.sub(r'\s+', ' ', TEXTAREA).strip():
            continue
        assert 'backdrop-filter' not in body, (
            f'{TEXTAREA} gained a backdrop-filter — blur behind live text is '
            'forbidden.\nbody=' + body)
        assert 'animation' not in body, (
            f'{TEXTAREA} gained an animation — no moving layer behind live '
            'text.\nbody=' + body)


def test_retired_decorative_pseudo_elements_stay_retired():
    """The mascot / grip-wings / sparkles / D-pad / speech-bubble pseudo-
    elements must stay content:none (never resurrected as part of this pass)."""
    css = _strip_comments(_read(CSS))
    retired = [
        '[data-theme="tofu"] .input-box::before',
        '[data-theme="tofu"] .input-box::after',
        '[data-theme="tofu"] .input-area::before',
        '[data-theme="tofu"] .input-area::after',
        '[data-theme="tofu"] .input-row::before',
        '[data-theme="tofu"] .input-row::after',
        '[data-theme="tofu"] .input-inner::before',
    ]
    want = {re.sub(r'\s+', ' ', s).strip() for s in retired}
    for sel, body in _rules(css):
        parts = {p.strip() for p in sel.split(',')}
        if parts & want:
            # every retired pseudo-element listed here must be neutralized
            assert 'content:none' in body.replace(' ', '') or \
                'display:none' in body.replace(' ', ''), (
                f'a retired decorative pseudo-element ({sel}) is no longer '
                'content:none/display:none — an ornament was resurrected.\n'
                'body=' + body)


# ═══════════════════════════════════════════════════════════════════════════
#  (b) warm focus-within rule exists (warm frame + amber ring, no lift)
# ═══════════════════════════════════════════════════════════════════════════
def test_focus_within_is_warm_and_static():
    """:focus-within warms the frame + adds a static amber ring; no lift."""
    body = _rule_body(_read(CSS), FOCUS)
    assert body is not None, f'{FOCUS} rule not found (structure changed?)'
    flat = body.replace(' ', '')
    # warm amber ring from the bar's #C4956A family (rgb 196,149,106)
    assert '196,149,106' in flat, (
        f'{FOCUS} lost the warm-amber focus ring (rgba(196,149,106,…)).\n'
        'body=' + body)
    # NO lift — the box must not translate on focus
    m = re.search(r'transform\s*:\s*([^;]+)', body)
    assert m is None or 'none' in m.group(1), (
        f'{FOCUS} introduced a transform lift — focus must be static.\n'
        'transform=' + (m.group(1) if m else '<none>'))
    assert 'animation' not in flat, f'{FOCUS} must not animate.\nbody=' + body


# ═══════════════════════════════════════════════════════════════════════════
#  (c) frame kinship + frosted-pill kinship both exist
# ═══════════════════════════════════════════════════════════════════════════
def test_input_box_shares_gallery_frame():
    """.input-box adopts the bar's frame vocabulary: 14px radius + a two-layer
    (comma-separated) soft shadow."""
    body = _rule_body(_read(CSS), BOX)
    assert body is not None, f'{BOX} rule not found'
    m = re.search(r'border-radius\s*:\s*([^;]+)', body)
    assert m is not None and '14px' in m.group(1), (
        f'{BOX} lost the 14px gallery-frame radius.\nbody=' + body)
    shadow = re.search(r'box-shadow\s*:\s*([^;]+)', body)
    assert shadow is not None and ',' in shadow.group(1), (
        f'{BOX} lost the soft two-layer (comma-separated) elevation.\n'
        'box-shadow=' + (shadow.group(1) if shadow else '<none>'))


def test_toolbar_pills_are_frosted_glass():
    """The tofu toolbar pills gain backdrop-filter:blur — the project bar's
    frosted-glass pill idiom, so the two bars read as one surface."""
    css = _read(CSS)
    for sel in FROSTED_PILLS:
        body = _rule_body(css, sel)
        assert body is not None, f'{sel} rule not found'
        flat = body.replace(' ', '')
        assert 'backdrop-filter:blur' in flat, (
            f'{sel} did not gain backdrop-filter:blur — the frosted-glass '
            'kinship with the project bar pills is missing.\nbody=' + body)


# ═══════════════════════════════════════════════════════════════════════════
#  (d) item 4 scene wash is TRAY-scoped, never behind the textarea
# ═══════════════════════════════════════════════════════════════════════════
def test_scene_wash_is_scoped_to_toolbar_tray_only():
    """Every [data-decor] sibling-wash rule must target `.input-actions` (the
    tray) and NEVER the textarea / input-row / bare .input-box."""
    css = _strip_comments(_read(CSS))
    wash_rules = [
        (sel, body) for sel, body in _rules(css)
        if 'data-decor' in sel and '~ .input-box' in sel
    ]
    assert wash_rules, (
        'no item-4 scene-wash rules found — the [data-decor] ~ .input-box '
        'sibling wash is missing.')
    for sel, body in wash_rules:
        assert sel.endswith('.input-actions'), (
            'scene wash is not tray-scoped — it must end at .input-actions, '
            f'got selector: {sel}')
        assert 'textarea' not in sel and '.input-row' not in sel, (
            f'scene wash leaked onto the typing surface: {sel}')


def test_scene_wash_reads_all_three_scenes():
    """The wash echoes the bar for meadow / pool / sky (off intentionally
    excluded so a scene-off bar shows no wash)."""
    css = _strip_comments(_read(CSS))
    sels = ' '.join(sel for sel, _ in _rules(css)
                    if 'data-decor' in sel and '~ .input-box' in sel)
    for scene in ('meadow', 'pool', 'sky'):
        assert f'[data-decor="{scene}"]' in sels, (
            f'scene wash missing the {scene} variant.')
    assert '[data-decor="off"]' not in sels, (
        'scene wash must NOT apply when the bar scene is off.')


# ═══════════════════════════════════════════════════════════════════════════
#  NEUTER tests — re-poison a COPY to prove each guard is load-bearing
# ═══════════════════════════════════════════════════════════════════════════
def _assert_neuter(sel: str, poison_body, check):
    """Rewrite `sel`'s body via poison_body(body) in a COPY of the CSS, then run
    `check(poisoned_css)` which must raise AssertionError."""
    css = _strip_comments(_read(CSS))
    body = _rule_body(css, sel)
    assert body is not None, f'{sel} not found — test stale'
    poisoned_body = poison_body(body)
    assert poisoned_body != body, 'neuter did not change the body — test stale'
    # replace only the first exact "{body}" occurrence
    poisoned_css = css.replace('{' + body + '}', '{' + poisoned_body + '}', 1)
    assert poisoned_css != css, 'neuter did not rewrite CSS — test stale'
    with pytest.raises(AssertionError):
        check(poisoned_css)


def test_NC_textarea_blur_is_flagged():
    """If the textarea gained a backdrop-filter, guard (a) must fire."""
    def check(css):
        for sel, body in _rules(css):
            if sel != re.sub(r'\s+', ' ', TEXTAREA).strip():
                continue
            assert 'backdrop-filter' not in body, 'flagged'
    _assert_neuter(TEXTAREA,
                   lambda b: b.rstrip().rstrip('}') + ';backdrop-filter:blur(6px)',
                   check)


def test_NC_focus_ring_removal_is_flagged():
    """If the warm amber ring were reverted, guard (b) must fire."""
    def check(css):
        body = _rule_body(css, FOCUS)
        assert body is not None
        assert '196,149,106' in body.replace(' ', ''), 'flagged'
    _assert_neuter(FOCUS,
                   lambda b: b.replace('196,149,106', '110,86,207'),  # back to plain accent
                   check)


def test_NC_frame_radius_revert_is_flagged():
    """If the 14px gallery radius were reverted, guard (c) must fire."""
    def check(css):
        body = _rule_body(css, BOX)
        assert body is not None
        m = re.search(r'border-radius\s*:\s*([^;]+)', body)
        assert m is not None and '14px' in m.group(1), 'flagged'
    _assert_neuter(BOX,
                   lambda b: b.replace('border-radius:14px', 'border-radius:var(--radius)'),
                   check)


def test_NC_pill_frost_removal_is_flagged():
    """If a toolbar pill lost its backdrop-filter, guard (c-pills) must fire."""
    def check(css):
        body = _rule_body(css, '[data-theme="tofu"] .preset-toggle')
        assert body is not None
        assert 'backdrop-filter:blur' in body.replace(' ', ''), 'flagged'
    _assert_neuter('[data-theme="tofu"] .preset-toggle',
                   lambda b: re.sub(r'-?webkit-?backdrop-filter\s*:[^;]+;?', '',
                                    b.replace('backdrop-filter:blur(6px) saturate(1.05)', ''))
                   if 'backdrop-filter' in b else b,
                   check)


def test_NC_wash_leak_to_textarea_is_flagged():
    """If the scene wash were retargeted onto the textarea, guard (d) must
    fire — proving the tray-scoping assertion is load-bearing."""
    css = _strip_comments(_read(CSS))
    # find the meadow wash rule and retarget it to the textarea
    target = None
    for sel, body in _rules(css):
        if 'data-decor="meadow"' in sel and '~ .input-box' in sel:
            target = (sel, body)
            break
    assert target is not None, 'meadow wash rule not found — test stale'
    sel, body = target
    leaked = sel.replace('.input-box .input-actions', '.input-box textarea')
    poisoned = css.replace(sel + '{' + body + '}', leaked + '{' + body + '}', 1)
    assert poisoned != css, 'neuter did not rewrite the selector — test stale'

    def check(c):
        for s, b in _rules(c):
            if 'data-decor' in s and '~ .input-box' in s:
                assert s.endswith('.input-actions'), 'flagged'
                assert 'textarea' not in s, 'flagged'
    with pytest.raises(AssertionError):
        check(poisoned)


# ═══════════════════════════════════════════════════════════════════════════
#  (e) item-4 wash is an ATMOSPHERIC WATERCOLOR WASH — smooth, NON-tiled,
#      colour pooled LOW with a light top band (mirrors the bar's canvas)
# ═══════════════════════════════════════════════════════════════════════════
def _wash_rules(css: str):
    return [(sel, body) for sel, body in _rules(css)
            if 'data-decor' in sel and '~ .input-box' in sel
            and sel.endswith('.input-actions')]


def _bg_value(body: str) -> str:
    m = re.search(r'background\s*:\s*(.+?)(?:;\s*$|;\s*[a-z-]+\s*:|$)', body,
                  flags=re.DOTALL)
    return m.group(1) if m else ''


def _has_tiling(bg: str) -> bool:
    """True if the background value uses `background-size` (a `/` at the TOP
    level, i.e. outside any parentheses). Gradient internals use legacy rgba
    comma syntax so they never contain a top-level `/` — a top-level slash means
    a per-layer size, i.e. TILING (the repeating dot texture we moved away
    from). Robust to inner `rgba(...)` parens that broke a naive regex."""
    depth = 0
    for ch in bg:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth = max(0, depth - 1)
        elif ch == '/' and depth == 0:
            return True
    return False


def test_scene_wash_is_atmospheric_not_tiled():
    """Each scene wash must be a SMOOTH atmospheric watercolour wash — soft
    full-cover gradients (≥2 radial blooms + a vertical base), and crucially
    NON-TILED: no `background-size` slash sizing, which is what turned an
    earlier attempt into a repeating polka-dot texture (the opposite of the
    bar's Impressionist look)."""
    css = _strip_comments(_read(CSS))
    washes = _wash_rules(css)
    assert washes, 'no tray-scoped scene-wash rules found — item 4 missing.'
    for sel, body in washes:
        val = _bg_value(body)
        assert val, f'{sel} has no background.\nbody=' + body
        assert val.count('radial-gradient') >= 2, (
            f'{sel} lost its soft colour blooms — expected ≥2 radial-gradient '
            f'layers, got {val.count("radial-gradient")}.\nbackground=' + val)
        assert 'linear-gradient' in val, (
            f'{sel} lost its vertical light-top→scene base.\nbackground=' + val)
        # NON-TILED: a top-level `/` (background-size) means the layer repeats →
        # the tiled dot texture we are explicitly moving away from.
        assert not _has_tiling(val), (
            f'{sel} uses background-size tiling — the wash must be a smooth '
            'full-cover watercolour, not a repeated dab tile.\nbackground=' + val)


def test_scene_wash_pools_colour_low_and_keeps_top_light():
    """Readability guard: the saturated scene colour must be pooled LOW (radial
    blooms centred in the lower band, y ≳ 120%) and the vertical base must
    start TRANSPARENT at the top, so the frosted pill labels + placeholder keep
    contrast — exactly how the bar keeps its upper band pale."""
    css = _strip_comments(_read(CSS))
    for sel, body in _wash_rules(css):
        val = _bg_value(body)
        # every radial bloom is anchored low (…at X% Y%… with Y ≥ 120)
        ys = [float(y) for y in re.findall(r'at\s+[\d.]+%\s+([\d.]+)%', val)]
        assert ys, f'{sel} radial blooms have no explicit low anchor.\nbg=' + val
        assert min(ys) >= 120, (
            f'{sel} has a colour bloom anchored high (y={min(ys)}%) — colour '
            'must pool LOW so pill labels stay legible.\nbackground=' + val)
        # the vertical base begins fully transparent at the top (…,0) 0%)
        base = val[val.index('linear-gradient'):]
        assert re.search(r'linear-gradient\(180deg\s*,\s*rgba\([^)]*,\s*0\)\s*0%',
                         base), (
            f'{sel} vertical base does not start transparent at the top — the '
            'top band must stay light for label contrast.\nbase=' + base)


def test_NC_tiled_wash_is_flagged():
    """NEUTER: re-introduce background-size tiling on a COPY → the non-tiled
    guard must fire (proves the smooth-watercolour requirement is load-bearing)."""
    css = _strip_comments(_read(CSS))
    washes = _wash_rules(css)
    assert washes, 'item-4 wash missing — test stale'
    sel, body = washes[0]
    val = _bg_value(body)
    # tile the FIRST layer: insert a top-level `/ size` right before the comma
    # that separates layer 1 from layer 2 (a genuine background-size).
    first_comma = val.index('),') + 1   # top-level comma after layer 1's close
    tiled_val = val[:first_comma] + ' / 48px 42px' + val[first_comma:]
    tiled_body = body.replace(val, tiled_val, 1)
    assert tiled_body != body, 'neuter did not change the body — test stale'
    assert _has_tiling(tiled_val), 'neuter did not actually introduce tiling'
    poisoned = css.replace('{' + body + '}', '{' + tiled_body + '}', 1)
    assert poisoned != css, 'neuter did not rewrite CSS — test stale'

    def check(c):
        for s, b in _wash_rules(c):
            assert not _has_tiling(_bg_value(b)), 'flagged'
    with pytest.raises(AssertionError):
        check(poisoned)


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
