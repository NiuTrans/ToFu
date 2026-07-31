"""Cross-device tofu-theme touch polish — three fixes in the master mobile
`@media(max-width:768px)` block (+ the orientation-aware landscape rule), each
guarded by an ON-DISK double-neuter so a later edit that silently reverts one
fails loudly.

WHY THESE FIXES (all verified against the shipped styles.css, not an audit)
---------------------------------------------------------------------------
1. `.msg-action-btn` TOUCH PADDING. The mobile block sets a plain
   `.msg-action-btn{padding:7px 12px;font-size:11px;min-height:44px}` (0,1,0),
   but the tofu theme's `[data-theme="tofu"] .msg-action-btn{padding:3px 6px;
   font-size:10px}` (0,2,0) out-specifies it → tofu message-action buttons kept
   their cramped desktop padding/font inside the 44px box, mismatching the other
   touch controls. Fix: re-assert the comfortable padding/font at matching
   specificity INSIDE the mobile block. This test resolves the winner WITHIN the
   mobile block (the sibling specificity engine is media-blind, so we slice the
   block out first) and asserts tofu wins the padding.

2. OVERFLOW-CLIPPED DECORATIONS. `[data-theme="tofu"] .conv-item.active::before`
   (the ▸ pixel cursor, `left:-11px`) and `[data-theme="tofu"] .thinking-block
   ::after` (thought-bubble dots, `left:-10px`) are anchored OUTSIDE their box
   with a negative left; the mobile containers are `overflow:hidden`, so they
   render as dead space / a clipped sliver on a 360px screen. Fix: hide both in
   the mobile block. (The active left-border + thinking frame already signal
   state, so nothing is lost.)

3. ORIENTATION-AWARE LANDSCAPE PHONES. The lone landscape rule was
   `@media(max-height:500px) and (max-width:900px)`, missing the many
   540–599px-tall landscape handsets. Fix: anchor on `orientation:landscape` and
   raise the short-viewport ceiling to 600px.

DOUBLE-NEUTER form mirrors tests/test_memory_modal_specificity.py: each neuter is
applied to the real styles.css in a subprocess-free in-place edit and the file is
restored byte-identical.
"""

from __future__ import annotations

import os
import re

import pytest

# Reuse the sibling's proven CSS specificity engine + rule iterator.
from tests.test_memory_modal_specificity import (  # noqa: E402
    _Elem,
    _iter_rules,
    _resolve,
)

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')


def _css() -> str:
    with open(CSS, encoding='utf-8') as f:
        return f.read()


def _slice_media_block(css_text: str, header: str, must_contain: str = '') -> str:
    """Return the body (between the outer braces) of the @media block whose
    header text matches *header*. When *must_contain* is given, pick the first
    such block whose body contains that marker (the header text is not unique —
    there are several `@media(max-width:768px){` blocks). Brace-matched so nested
    rules survive.
    """
    search_from = 0
    while True:
        start = css_text.find(header, search_from)
        assert start != -1, f'media header not found: {header!r} (marker={must_contain!r})'
        brace = css_text.find('{', start + len(header) - 1)
        assert brace != -1, 'no opening brace after media header'
        depth = 0
        i = brace
        while i < len(css_text):
            ch = css_text[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    body = css_text[brace + 1:i]
                    if not must_contain or must_contain in body:
                        return body
                    break
            i += 1
        else:
            raise AssertionError('unbalanced braces in media block')
        search_from = i + 1


def _strip_media_blocks(css_text: str) -> str:
    """Remove every top-level @media{...} block (brace-matched), leaving only the
    global (unconditional) rules. Used to model the base cascade a media block
    layers on top of."""
    out = []
    i = 0
    n = len(css_text)
    while i < n:
        m = re.compile(r'@media[^{]*\{').search(css_text, i)
        if not m:
            out.append(css_text[i:])
            break
        out.append(css_text[i:m.start()])
        depth = 0
        j = m.start()
        # advance to the block's opening brace
        j = css_text.find('{', j)
        while j < n:
            ch = css_text[j]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        i = j + 1
    return ''.join(out)


def _strip_comments(css_text: str) -> str:
    """Remove /* ... */ comments. The sibling's `_iter_rules` splits on `{`/`}`,
    so a comment containing literal braces (e.g. an explanatory `{padding:...}` )
    corrupts rule parsing. Browsers ignore comments entirely; we do too — AFTER
    any marker-based slicing, since some block markers live inside comments.

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
    return strip_comments(css_text, lang='css', inline=True)


def _mobile_cascade(css_text: str, marker: str) -> str:
    """The rule set that actually applies at mobile width, in source order: all
    GLOBAL rules (other media blocks stripped), followed by the master mobile
    block's body (which is later in source, so its equal-specificity rules win
    the tie-break — exactly the real cascade). This is what makes the msg-btn
    NC able to bite: the tofu DESKTOP rule (global, 0,2,0) competes with the
    mobile plain rule (0,1,0) and the mobile tofu override (0,2,0)."""
    return _strip_comments(
        _strip_media_blocks(css_text) + '\n'
        + _slice_media_block(css_text, _MOBILE_HEADER, marker))


# The master mobile block header (exact, as it appears in styles.css). The header
# is NOT unique (paper-mode uses the same one), so disambiguate by a marker that
# lives ONLY in the master block.
_MOBILE_HEADER = '@media(max-width:768px){'
_MOBILE_MARKER = 'OVERFLOW CONTAINMENT'

# A tofu message-action button on mobile:
#   <div data-theme="tofu"> … <button class="msg-action-btn">
_TOFU_MSG_BTN = _Elem('button', {'msg-action-btn'}, theme='tofu')


@pytest.fixture(scope='module')
def mobile_block():
    return _slice_media_block(_css(), _MOBILE_HEADER, _MOBILE_MARKER)


# ─────────────────────────── 1. msg-action-btn touch padding ───────────────────────────

def test_tofu_msg_action_btn_gets_touch_padding_in_mobile_block():
    """At mobile width the winning `padding` for a tofu msg-action-btn is the
    comfortable touch value (7px 12px), not the cramped tofu DESKTOP value
    (3px 6px). The fix (a mobile tofu override) ties the desktop rule at (0,2,0)
    and wins on source order."""
    padding = _resolve(_mobile_cascade(_css(), _MOBILE_MARKER), _TOFU_MSG_BTN, 'padding')
    assert padding == '7px 12px', (
        f'tofu msg-action-btn padding in the mobile block resolved to {padding!r};'
        f' expected the touch value "7px 12px". The tofu desktop padding (3px 6px)'
        f' is winning — the matching-specificity mobile override is missing.')


def test_nc_reverting_msg_btn_fix_lets_tofu_cramped_padding_win():
    """DOUBLE-NEUTER: delete the mobile tofu msg-action-btn override → the tofu
    desktop rule (3px 6px, 0,2,0) out-specifies the plain mobile rule (0,1,0)
    again → resolution flips. Restore byte-identical."""
    original = _css()
    fix_line = '  [data-theme="tofu"] .msg-action-btn{padding:7px 12px;font-size:11px}\n'
    assert original.count(fix_line) == 1, (
        f'NC anchor not unique/found: count={original.count(fix_line)}')

    assert _resolve(_mobile_cascade(original, _MOBILE_MARKER),
                    _TOFU_MSG_BTN, 'padding') == '7px 12px', 'baseline wrong'

    try:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original.replace(fix_line, '', 1))
        pad = _resolve(_mobile_cascade(_css(), _MOBILE_MARKER),
                       _TOFU_MSG_BTN, 'padding')
        assert pad == '3px 6px', (
            f'NC did not bite: with the mobile override removed the padding '
            f'resolved {pad!r}, expected the cramped tofu desktop "3px 6px". '
            f'The fix is not actually what wins the touch padding.')
    finally:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original)
    assert _css() == original, 'CSS not restored byte-identical after msg-btn NC'


# ─────────────────────────── 2. overflow-clipped decorations hidden ───────────────────────────

_DECO_HIDE_RULES = [
    '[data-theme="tofu"] .conv-item.active::before',
    '[data-theme="tofu"] .thinking-block::after',
]


def test_clipped_decorations_hidden_in_mobile_block(mobile_block):
    """Both negative-left decorative pseudo-elements resolve to display:none
    within the mobile block (so they can't overflow / clip on narrow screens)."""
    for sel in _DECO_HIDE_RULES:
        cls = set(re.findall(r'\.([\w-]+)', re.sub(r'::[\w-]+', '', sel)))
        pseudo = '::before' if '::before' in sel else '::after'
        # Build the element carrying those classes + the pseudo marker.
        el = _Elem('div', cls | {pseudo.strip(':')}, theme='tofu')
        # Directly assert the exact hide rule exists in the block (robust to the
        # engine's pseudo-element handling).
        found = any(
            s.replace(' ', '') == sel.replace(' ', '')
            and decls.get('display', '').replace('!important', '').strip() == 'none'
            for s, _i, decls in _iter_rules(mobile_block)
        )
        assert found, (
            f'expected `{sel}{{display:none}}` inside the mobile block — the '
            f'overflow-clipped decoration is not hidden on mobile.')


def test_nc_removing_decoration_hide_regresses():
    """DOUBLE-NEUTER: remove each decoration-hide rule → the presence assertion
    fails. Restore byte-identical."""
    original = _css()
    for sel in _DECO_HIDE_RULES:
        # The shipped rule text (matches the exact line, incl. !important).
        needle = f'  {sel}{{display:none!important}}'
        assert needle in original, f'shipped hide rule not found verbatim: {needle!r}'
    # Neuter the first one and confirm the block loses it.
    victim = f'  {_DECO_HIDE_RULES[0]}{{display:none!important}}'
    try:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original.replace(victim, '  /* neutered */', 1))
        nblock = _slice_media_block(_css(), _MOBILE_HEADER, _MOBILE_MARKER)
        still = any(
            s.replace(' ', '') == _DECO_HIDE_RULES[0].replace(' ', '')
            and decls.get('display', '').replace('!important', '').strip() == 'none'
            for s, _i, decls in _iter_rules(nblock)
        )
        assert not still, 'NC did not bite: hide rule still present after removal'
    finally:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original)
    assert _css() == original, 'CSS not restored byte-identical after decoration NC'


# ─────────────────────────── 3. orientation-aware landscape ───────────────────────────

def test_landscape_phone_rule_is_orientation_aware():
    """The landscape-phone LAYOUT block anchors on orientation:landscape and
    covers up to 600px tall — not the old max-height:500px-only rule. Scoped
    to the layout block (header + its first payload line): the same media
    condition also heads the tofu welcome-brand ladder, so a bare-header
    presence check can pass while the layout rule is gone."""
    css = _css()
    assert ('@media(orientation:landscape) and (max-height:600px) and (max-width:900px){'
            '\n  .topbar{') in css, (
        'orientation-aware landscape-phone LAYOUT rule missing')
    # The old 500px-only header must be gone (proves the edit replaced it).
    assert '@media(max-height:500px) and (max-width:900px){' not in css, (
        'the old max-height:500px-only landscape rule is still present')


def test_nc_reverting_landscape_to_500_only_regresses():
    """DOUBLE-NEUTER: revert the landscape header to the old 500px-only form →
    the orientation-aware assertion fails. Restore byte-identical."""
    original = _css()
    new_header = '@media(orientation:landscape) and (max-height:600px) and (max-width:900px){'
    old_header = '@media(max-height:500px) and (max-width:900px){'
    # The same media CONDITION legitimately heads SEVERAL blocks (the tofu
    # welcome-brand breakpoint ladder, the brand-area mobile scale ladder near
    # the end of the file, and the landscape-phone layout block) — their payloads
    # are disjoint, so the CSS is correct. Scope the revert to the LAYOUT block
    # via its first payload line; a replace(1) on the bare header would hit one
    # of the brand blocks and the NC would revert the wrong thing.
    layout_anchor = new_header + '\n  .topbar{'
    assert original.count(layout_anchor) == 1, 'landscape layout block not unique/found'
    # DERIVED, not hardcoded: assert the revert consumes EXACTLY ONE occurrence.
    # An earlier revision asserted `count == 1` outright, which turned into a
    # false red the moment a legitimate third block shared this condition.
    headers_before = original.count(new_header)
    assert headers_before >= 2, (
        f'expected the layout block plus at least one brand block to share this '
        f'condition, found {headers_before}')
    try:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original.replace(layout_anchor, old_header + '\n  .topbar{', 1))
        reverted = _css()
        assert new_header + '\n  .topbar{' not in reverted, (
            'NC setup failed: the LAYOUT block still anchors on orientation:landscape')
        assert old_header + '\n  .topbar{' in reverted, (
            'NC setup failed: the LAYOUT block was not reverted to 500-only')
        # The brand blocks share the media condition — they must remain untouched
        # (the NC reverts ONLY the layout block), i.e. exactly one fewer header.
        assert reverted.count(new_header) == headers_before - 1, (
            f'NC clobbered a brand-block occurrence — the anchor was not scoped: '
            f'{headers_before} headers before, {reverted.count(new_header)} after '
            f'(expected {headers_before - 1})')
    finally:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original)
    assert _css() == original, 'CSS not restored byte-identical after landscape NC'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
