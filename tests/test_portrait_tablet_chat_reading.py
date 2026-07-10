"""Portrait-tablet (769–1024px, coarse pointer) chat + reading drift-guard.

Symptom this guards against (fixed 2026-07-08):

  1. CHAT — the base `.message-actions` is a HOVER-REVEAL (`opacity:0` →
     `1` only on `.message:hover`). A coarse-pointer tablet can never fire
     `:hover`, so copy / edit / regenerate / delete were INVISIBLE and
     unreachable at 769–1024px. The ≤768 phone block re-flows them inline
     (`opacity:1` + `position:relative`), but the portrait-tablet chat drawer
     block (which keeps the roomier desktop message layout) never re-showed
     them.

  2. READING — the reader single-pane predicate `≤768 ∪ (≤1024 ∧ coarse)`
     applied the SAME 360px-phone compaction (`.paper-report-content{padding:14px}`,
     shrunk tab buttons) to a ~900px portrait tablet, wasting its room and
     giving finger-hostile targets.

  3. CHAT WIDTH — once the chat sidebar becomes a slide-over drawer on the
     tablet band, the chat pane spans the full viewport, but the base
     `.chat-inner{max-width:820px}` leaves ~102px of dead gutter each side at a
     1024px portrait tablet. The band must widen the reading measure so the
     pane breathes (> 820px).

Both fixes live in a dedicated
`@media(min-width:769px) and (max-width:1024px) and (pointer:coarse)` block
(one for chat near the tablet drawer, one for reading after the shared
single-pane block). This test pins those two invariants and NEUTER-proves
each is load-bearing.
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


def _iter_media_blocks(css: str):
    """Yield (header, body) for every top-level @media block, brace-matched."""
    for m in re.finditer(r'@media([^{]*)\{', css):
        header = m.group(1)
        brace = m.end() - 1
        depth = 0
        i = brace
        while i < len(css):
            if css[i] == '{':
                depth += 1
            elif css[i] == '}':
                depth -= 1
                if depth == 0:
                    yield header, css[brace + 1:i]
                    break
            i += 1


def _is_tablet_portrait(header: str) -> bool:
    """True iff the media condition is EXACTLY the portrait-tablet band:
    min-width:769 ∧ max-width:1024 ∧ pointer:coarse (the block our fix uses)."""
    h = re.sub(r'\s+', '', header)
    return ('min-width:769px' in h
            and 'max-width:1024px' in h
            and 'pointer:coarse' in h)


def _strip_comments(css: str) -> str:
    """Remove /* ... */ comments. CRITICAL for rule extraction: a comment that
    quotes a literal rule (e.g. `The base .chat-inner{max-width:820px} then`)
    would otherwise be matched as if it were real CSS (a trap logged in the
    mobile-breakpoint skill memory)."""
    return re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)


def _tablet_portrait_bodies(css: str) -> list[str]:
    """Bodies of every EXACT portrait-tablet band block (chat + reading),
    with comments stripped so quoted-rule prose can't be mis-parsed."""
    return [_strip_comments(body)
            for header, body in _iter_media_blocks(css)
            if _is_tablet_portrait(header)]


def _rule_decls(body: str, selector: str) -> str | None:
    """Return the declaration text of the rule whose selector list ends with
    `selector` immediately before its `{`. Whitespace-insensitive; None if the
    selector doesn't own a rule block in this body."""
    compact = re.sub(r'\s+', '', body)
    for mm in re.finditer(re.escape(selector), compact):
        j = mm.end()
        # Next char must begin the declaration block (selector is the last in
        # its comma-list) OR continue a comma-list we don't care about here.
        brace = compact.find('{', j)
        if brace == -1:
            continue
        if '}' in compact[j:brace]:
            continue
        end = compact.find('}', brace)
        if end == -1:
            continue
        # The selector must be the token right before '{' (no other selector
        # glued after it without a comma) — i.e. compact[j] is '{' or ',' or
        # part of a combinator we reject. Accept only when j == brace (selector
        # directly precedes '{') so we read THIS selector's own block.
        if j == brace:
            return compact[brace + 1:end]
    return None


# ─────────────────────────── invariants ───────────────────────────

def test_base_message_actions_is_hover_reveal():
    """Precondition anchor: the BASE `.message-actions` really is a hover-reveal
    (opacity:0). If this ever changes to always-visible, the tablet re-show
    below becomes redundant and this guard should be revisited."""
    css = _read(CSS)
    # The base rule lives outside any @media block.
    base = re.search(r'\.message-actions\{([^}]*)\}', css)
    assert base, '.message-actions base rule not found'
    assert 'opacity:0' in re.sub(r'\s+', '', base.group(1)), (
        'base .message-actions is no longer opacity:0 — the tablet touch-reveal '
        'guard assumes a hover-reveal base; revisit this test.')


def test_tablet_portrait_reveals_message_actions():
    """A portrait-tablet band block MUST set `.message-actions{opacity:1}` so
    the hover-only actions are visible on a coarse-pointer tablet."""
    css = _read(CSS)
    revealed = False
    for body in _tablet_portrait_bodies(css):
        decls = _rule_decls(body, '.message-actions')
        if decls and 'opacity:1' in decls:
            revealed = True
            break
    assert revealed, (
        'no portrait-tablet @media block reveals `.message-actions{opacity:1}` '
        '— copy/edit/regenerate/delete are invisible on a coarse-pointer tablet '
        '(base is opacity:0 hover-reveal, which a touch tablet cannot trigger).')


def test_tablet_portrait_inflates_reader_padding():
    """A portrait-tablet band block MUST give the reader panes MORE padding than
    the 14px phone compaction — a ~900px tablet has room. We assert the reader
    padding rule exists and its first px value is > 14."""
    css = _read(CSS)
    inflated = False
    for body in _tablet_portrait_bodies(css):
        # match `.paper-report-content,.paper-report-body{padding:...}`
        m = re.search(
            r'\.paper-report-content\s*,\s*\.paper-report-body\s*\{[^}]*padding:\s*(\d+)px',
            body)
        if m and int(m.group(1)) > 14:
            inflated = True
            break
    assert inflated, (
        'no portrait-tablet @media block inflates .paper-report-* padding above '
        'the 14px phone value — reading prose stays cramped on a tablet.')


def test_tablet_portrait_grows_reader_tab_touch_target():
    """The reader tabs (Q&A/Report/Babel) must be roomier than the phone's
    cramped `padding:10px 6px` on a tablet — assert a `.paper-tab-btn` rule in
    the band whose horizontal padding exceeds the phone's 6px."""
    css = _read(CSS)
    grew = False
    for body in _tablet_portrait_bodies(css):
        decls = _rule_decls(body, '.paper-tab-btn')
        if not decls:
            continue
        m = re.search(r'padding:(\d+)px(\d+)px', decls)
        if m and int(m.group(2)) > 6:
            grew = True
            break
    assert grew, (
        'no portrait-tablet block grows .paper-tab-btn horizontal padding '
        'beyond the 6px phone value — reader tabs stay finger-hostile.')


def test_tablet_portrait_widens_chat_measure():
    """A portrait-tablet band block MUST widen `.chat-inner` beyond the base
    820px so the chat pane fills a full-width tablet drawer layout instead of
    sitting in ~102px dead gutters at 1024px."""
    css = _read(CSS)
    # Base cap is 820px (outside any @media block); assert we push past it.
    widened = False
    for body in _tablet_portrait_bodies(css):
        decls = _rule_decls(body, '.chat-inner')
        if not decls:
            continue
        m = re.search(r'max-width:(\d+)px', decls)
        if m and int(m.group(1)) > 820:
            widened = True
            break
    assert widened, (
        'no portrait-tablet block widens .chat-inner max-width beyond the base '
        '820px — the chat reading column does not breathe on a tablet.')


def test_reader_panes_clip_horizontal_overflow():
    """The reader prose panes MUST be `overflow-x:hidden`. They are
    `overflow-y:auto`, which computes `overflow-x` to `auto` — so a glossary
    hover-card (or any wide child) overflowing the right edge turned the pane
    horizontally scrollable, producing the 'pointless left-right swiping' the
    owner reported on a portrait tablet. Assert BOTH base rules clip it."""
    css = re.sub(r'\s+', '', _strip_comments(_read(CSS)))
    for sel in ('.paper-report-content', '.paper-report-body'):
        # The selector owns MULTIPLE rules (a --reader-measure combined rule and
        # the flex/overflow prose rule). Find the one that actually sets
        # overflow-y:auto — that's the scroll container that must clip X.
        found = False
        for m in re.finditer(re.escape(sel) + r'\{([^}]*)\}', css):
            decls = m.group(1)
            if 'overflow-y:auto' in decls:
                assert 'overflow-x:hidden' in decls, (
                    sel + ' scroll rule is not overflow-x:hidden — the reader '
                    'pane can be swiped horizontally when a child overflows.')
                found = True
        assert found, sel + ' overflow-y:auto scroll rule not found'


def test_tablet_portrait_reveals_report_toc():
    """A portrait-tablet band block MUST re-show the sidebar TOC
    (`.paper-report-doc > .paper-report-toc{display:block}`) — the reader
    single-panes to full width on a tablet, so there is room for the contents
    nav that the `max-width:900px` phone collapse hides."""
    css = _read(CSS)
    shown = False
    for body in _tablet_portrait_bodies(css):
        decls = _rule_decls(body, '.paper-report-doc>.paper-report-toc')
        if decls and 'display:block' in decls:
            shown = True
            break
    assert shown, (
        'no portrait-tablet @media block sets '
        '.paper-report-doc > .paper-report-toc{display:block} — the table of '
        'contents stays hidden on a full-width tablet reader.')


def test_nc_reader_overflow_x_clip_is_load_bearing():
    """Strip `overflow-x:hidden` from the reader panes on a COPY → the clip
    invariant must FAIL (proves it is load-bearing)."""
    css = _read(CSS)
    assert 'overflow-y:auto;overflow-x:hidden' in re.sub(r'\s+', '', css), \
        'expected reader overflow-x:hidden not present in shipped CSS'
    poisoned = re.sub(r'\s+', '', _strip_comments(css))
    poisoned = re.sub(r'(\.paper-report-(?:content|body)\{[^}]*?)overflow-x:hidden;',
                      r'\1', poisoned)
    for sel in ('.paper-report-content', '.paper-report-body'):
        for m in re.finditer(re.escape(sel) + r'\{([^}]*)\}', poisoned):
            if 'overflow-y:auto' in m.group(1):
                assert 'overflow-x:hidden' not in m.group(1), (
                    'neutering overflow-x:hidden did not remove it from ' + sel)


def test_nc_tablet_toc_reveal_is_load_bearing():
    """Flip the tablet TOC reveal to display:none on a COPY → the reveal
    invariant must FAIL."""
    css = _read(CSS)
    assert '.paper-report-doc > .paper-report-toc{display:block}' in css, \
        'expected tablet TOC reveal rule not present in shipped CSS'
    poisoned = css.replace(
        '.paper-report-doc > .paper-report-toc{display:block}',
        '.paper-report-doc > .paper-report-toc{display:none}', 1)
    # Poison ONLY the tablet-band occurrence; verify the tablet block no longer
    # reveals the TOC.
    shown = False
    for body in _tablet_portrait_bodies(poisoned):
        decls = _rule_decls(body, '.paper-report-doc>.paper-report-toc')
        if decls and 'display:block' in decls:
            shown = True
    assert not shown, (
        'flipping the tablet TOC reveal to display:none still passed — the '
        'reveal invariant is not pinned to the tablet block.')


def test_depth_footer_is_sticky():
    """The thinking-depth control lives ONLY at the bottom of the model dropdown
    for any viewport > 768px (the ≤768 phone path routes to the bottom-sheet
    `.mobile-depth-bar`, and `.ps-dd-depth-wrap` is `display:none` there). The
    dropdown is `max-height:50vh; overflow-y:auto`, so on a short landscape
    viewport (50vh ≈ 200px) the depth footer was stranded below a long,
    scrollable model list and could not be reached — the 'can't adjust thinking
    depth in landscape' regression. It MUST be `position:sticky` so it stays
    visible while the model list scrolls under it."""
    css = _read(CSS)
    # Anchor to the BASE rule (`.ps-dd-depth-wrap{` with no theme prefix); the
    # tofu override `[data-theme="tofu"] .ps-dd-depth-wrap{` appears earlier in
    # source, so a bare search would match that instead.
    m = re.search(r'(?<![\]\w ])\.ps-dd-depth-wrap\{([^}]*)\}',
                  _strip_comments(css))
    assert m, '.ps-dd-depth-wrap base rule not found'
    decls = re.sub(r'\s+', '', m.group(1))
    assert 'position:sticky' in decls, (
        '.ps-dd-depth-wrap is not position:sticky — the depth footer can be '
        'scrolled out of reach in a short (landscape) dropdown.')
    assert 'background:' in decls, (
        '.ps-dd-depth-wrap sticky footer has no opaque background — the '
        'scrolling model list would bleed through it.')


def test_depth_footer_opaque_under_tofu():
    """The tofu dropdown has its own bg (#F4F2EB), so the sticky footer needs a
    matching opaque bg under tofu or the scrolled list bleeds through."""
    css = _strip_comments(_read(CSS))
    m = re.search(r'\[data-theme="tofu"\]\s*\.ps-dd-depth-wrap\{([^}]*)\}', css)
    assert m, 'tofu .ps-dd-depth-wrap override not found'
    assert 'background:' in re.sub(r'\s+', '', m.group(1)), (
        'tofu sticky depth footer has no opaque background override.')


# ─────────────────────────── NEUTER controls ───────────────────────────

def test_nc_depth_footer_sticky_is_load_bearing():
    """Strip `position:sticky` from `.ps-dd-depth-wrap` on a COPY → the sticky
    invariant must FAIL (proves it is load-bearing)."""
    css = _read(CSS)
    m = re.search(r'\.ps-dd-depth-wrap\{[^}]*position:sticky[^}]*\}',
                  _strip_comments(css))
    assert m, 'sticky depth-wrap rule not found for NC'
    poisoned = _strip_comments(css).replace('position:sticky', 'position:static')
    m2 = re.search(r'\.ps-dd-depth-wrap\{([^}]*)\}', poisoned)
    assert m2 and 'position:sticky' not in re.sub(r'\s+', '', m2.group(1)), (
        'neutering position:sticky did not remove it — the invariant is not '
        'pinned to this rule.')


def test_nc_chat_measure_widen_is_load_bearing():
    """Rewrite the tablet `.chat-inner` max-width back down to 820px on a COPY →
    the widen invariant must FAIL."""
    css = _read(CSS)
    assert '.chat-inner{max-width:920px}' in css.replace('\n', ''), \
        'expected tablet chat-width rule not present in shipped CSS'
    poisoned = css.replace('.chat-inner{max-width:920px}',
                           '.chat-inner{max-width:820px}')
    assert poisoned != css, 'NC substitution did not apply — marker drift'
    widened = False
    for body in _tablet_portrait_bodies(poisoned):
        decls = _rule_decls(body, '.chat-inner')
        if not decls:
            continue
        m = re.search(r'max-width:(\d+)px', decls)
        if m and int(m.group(1)) > 820:
            widened = True
    assert not widened, (
        'shrinking the tablet chat measure to 820px still passed — the widen '
        'invariant is not pinned to the tablet block.')


def test_nc_message_actions_reveal_is_load_bearing():
    """Remove the `opacity:1` from the tablet `.message-actions` rule on a COPY
    → the reveal invariant must FAIL (proves it is load-bearing, not incidental
    to some other opacity elsewhere)."""
    css = _read(CSS)
    assert 'message-actions{opacity:1}' in re.sub(r'[ \t]+', '', css) or \
        '.message-actions{opacity:1}' in css.replace('\n', ''), \
        'expected reveal rule not present in shipped CSS'
    poisoned = css.replace('.message-actions{opacity:1}',
                           '.message-actions{opacity:0}')
    assert poisoned != css, 'NC substitution did not apply — marker drift'
    revealed = False
    for body in _tablet_portrait_bodies(poisoned):
        decls = _rule_decls(body, '.message-actions')
        if decls and 'opacity:1' in decls:
            revealed = True
    assert not revealed, (
        'neutering the tablet reveal to opacity:0 still passed — the invariant '
        'is not actually pinned to the tablet block.')


def test_nc_reader_padding_inflation_is_load_bearing():
    """Rewrite the tablet reader padding back down to 14px on a COPY → the
    inflation invariant must FAIL."""
    css = _read(CSS)
    m = re.search(
        r'(\.paper-report-content\s*,\s*\.paper-report-body\s*\{padding:)(\d+px \d+px|\d+px)\}',
        css)
    assert m, 'tablet reader padding rule not found for NC'
    poisoned = css[:m.start(2)] + '14px' + css[m.end(2):]
    inflated = False
    for body in _tablet_portrait_bodies(poisoned):
        mm = re.search(
            r'\.paper-report-content\s*,\s*\.paper-report-body\s*\{[^}]*padding:\s*(\d+)px',
            body)
        if mm and int(mm.group(1)) > 14:
            inflated = True
    assert not inflated, (
        'shrinking the tablet reader padding to 14px still passed — the '
        'inflation invariant is not pinned to the tablet block.')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
