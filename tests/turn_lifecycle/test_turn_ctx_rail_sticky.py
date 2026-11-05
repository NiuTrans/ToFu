"""The turn-ctx card FOLLOWS the scroll until the next round arrives.

WHY THIS FILE EXISTS
────────────────────
The rail (`.turn-ctx`) is absolutely positioned at the TOP of its own input
turn's box — out of flow so it never inflates the row (that contract is
swept by tests/turn_lifecycle/test_turn_ctx_rail_geometry.py). But an input turn is often
ONE line, so a purely box-bound rail scrolled away with its bubble while
the round's reply filled screens below it (2026-09-08 owner report: "when
a conversation is scrolling, the turn ctx should move along until it
reaches the next round").

The rail root is therefore a TRACK that info-rail.js stretches down to the
next railed round (or the end of the transcript), and its card
(`.tctx-card`) is `position: sticky` inside it: pinned to the pane's top
inside its round, pushed out exactly when the next round's card arrives, in
one continuous motion. The browser moves the card in the same frame as the
scroll; the script follower it replaced translated the card from scroll
events and trailed the transcript (2026-11-01 owner report: the following
felt "very weird"). The track extends BEYOND its host's border box, which is
why rail hosts drop content-visibility's paint containment
(`.message:has(> .turn-ctx)` in 23-reading-context-rail.css).

WHAT THIS ASSERTS (results, not constants)
──────────────────────────────────────────
1. **Follow** — with its host scrolled above the pane, the round's card is
   pinned AT the pane's top edge (small gap, asserted as a range — the gap
   is a design token, not the contract); later rounds' cards stay parked at
   their own hosts.
2. **Same frame** — read synchronously right after the scroll position
   changes, before any frame could run a script, the card is already
   pinned: the travel belongs to the browser, so it cannot trail the
   transcript.
3. **Continuous handoff** — mid-push-out, the departing card's bottom is
   EXACTLY the arriving card's top (±1px): a gap means the previous round's
   context vanishes early, an overlap means it covers the next round's.
4. **Tail** — at the very bottom of the scroll, the LAST round's card is
   still pinned (streaming replies grow the content under it).
5. **Paint carve-out** — a rail host's computed `content-visibility` is
   `visible`: getBoundingClientRect measures layout boxes, NOT paint, so
   the pin/geometry assertions above cannot see a restored
   `content-visibility:auto` clipping the travelling card at its host's
   edge. This is asserted separately and explicitly, with the host's layout
   containment, which holds the track's overflow out of the scroll range.
6. **Fold complement** — below the rail-grant floor (narrow pane) every
   rail is display:none AND its track length is dropped — re-granting the
   rail must never resurrect a stale track.
"""

from __future__ import annotations

import pytest

from tests.support.context_rail_probe import RAIL_PROBE_STYLED, with_rail_probe_host

pytestmark = [pytest.mark.visual, pytest.mark.slow]

#: "At the pane's top edge" is asserted as a RANGE: the pin gap is a design
#: token, not the contract. The contract is that the card sits flush under
#: the pane's top edge, not at a named pixel.
_TOP_EDGE_RANGE_PX = 12

#: A modest snapshot — three tool chips + one workspace path. The follow
#: contract doesn't care about rail CONTENT, only that the card is tall
#: enough to be seen travelling (the fat-snapshot height bounds are the
#: geometry sweep's subject, not this file's).
_SNAPSHOT = """{
  model: 'claude-opus-5',
  depth: 'think',
  modes: [{label:'Autopilot', tone:'mode'}],
  tools: [
    {label:'Search ×N', tone:'search'}, {label:'Fetch', tone:'net'},
    {label:'Memory', tone:'ai'}
  ],
  roots: [
    {short:'INS/tofu', path:'/path/to/your/project', ro:false}
  ]
}"""

#: Plant THREE one-line input turns carrying rails through the PRODUCTION
#: renderer, each followed by a 1400px reply spacer (taller than the 900px
#: viewport, so every round can scroll its host fully out of view). Never a
#: hand-written copy of the rail markup; only the Surface's part wrapper.
_PLANT = with_rail_probe_host("""() => {
    const inner = __RAIL_PROBE_HOST__;
    if (!inner) return 'no .chat-inner';
    const renderTurnCtxNote = window.TofuModules.resolveAction('renderTurnCtxNote');
    if (typeof renderTurnCtxNote !== 'function') return 'no renderTurnCtxNote';
    document.querySelectorAll('.__sticky').forEach(n => n.remove());
    const parts = renderTurnCtxNote(%s);
    if (!parts) return 'renderer returned empty';
    for (let i = 1; i <= 3; i++) {
        const d = document.createElement('div');
        d.className = 'message user-msg __sticky __sticky-turn-' + i;
        d.innerHTML = '<div class="message-avatar"></div>'
                    + '<div class="message-content"><div class="message-header">'
                    + '<span class="message-role">You</span></div>'
                    + parts.fold
                    + '<div class="message-body"><div class="md-content user-content">'
                    + 'round ' + i + '</div></div></div>'
                    + __RAIL_PROBE_PART__(parts.rail);
        inner.appendChild(d);
        const gap = document.createElement('div');
        gap.className = 'message __sticky __sticky-gap-' + i;
        gap.style.height = '1400px';
        gap.innerHTML = '<div class="message-avatar"></div>'
                      + '<div class="message-content"><div class="message-body">'
                      + '<div class="md-content">reply ' + i + '</div></div></div>';
        inner.appendChild(gap);
    }
    return 'ok';
}""" % (_SNAPSHOT,))

#: Geometry dump: per rail — host position in SCROLL-CONTENT coordinates
#: (stable across scrolls) and in the viewport, the card's viewport-space
#: box, and the track's inline length. Plus the container's top edge and
#: scroll metrics.
_MEASURE = """() => {
    const cont = document.querySelector('.chat-container');
    const contR = cont.getBoundingClientRect();
    return {
        contTop: contR.top,
        scrollHeight: cont.scrollHeight,
        // Other browser cases can leave a restored conversation above the
        // probes. Measure only the three rails this specification planted.
        rails: [...cont.querySelectorAll('.__sticky > .turn-ctx')].map((r) => {
            const hr = r.parentElement.getBoundingClientRect();
            const cr = r.querySelector(':scope > .tctx-card').getBoundingClientRect();
            return {
                hostDocTop: hr.top - contR.top + cont.scrollTop,
                hostTop: hr.top,
                top: cr.top,
                bottom: cr.bottom,
                track: r.style.height,
                display: getComputedStyle(r).display,
            };
        }),
    };
}"""

#: Move the transcript and read the geometry in the SAME task: no frame,
#: animation callback or observer can run in between.
_SCROLL_AND_MEASURE = """(y) => {
    document.querySelector('.chat-container').scrollTop = y;
    return (%s)();
}""" % (_MEASURE,)


def _boot(page):
    page.wait_for_selector('#userInput', state='visible', timeout=20000)
    page.wait_for_function(
        "typeof window.TofuModules?.resolveAction('renderTurnCtxNote') === 'function'",
        timeout=20000)
    page.set_viewport_size({'width': 1600, 'height': 900})
    page.evaluate("() => { const s = document.querySelector('.sidebar');"
                  " if (s) s.classList.add('collapsed'); }")
    planted = page.evaluate(_PLANT)
    assert planted == 'ok', f'could not plant the probe turns: {planted}'
    page.wait_for_function(RAIL_PROBE_STYLED, timeout=10000)
    # The plant mutates the transcript → the track pass is scheduled for the
    # next frame; settle it before measuring the parked baseline.
    page.wait_for_timeout(120)


def _measure(page):
    return page.evaluate(_MEASURE)


def _scroll_to(page, y):
    page.evaluate(
        "(y) => { document.querySelector('.chat-container').scrollTop = y; }", y)
    page.wait_for_timeout(90)


def _pinned_at_top(card, cont_top):
    return 0 <= card['top'] - cont_top <= _TOP_EDGE_RANGE_PX


def _parked(card):
    return abs(card['top'] - card['hostTop']) <= 1.0


def test_rail_follows_scroll_until_the_next_round(page):
    _boot(page)
    m0 = _measure(page)
    assert len(m0['rails']) == 3, (
        f"expected the three planted rails, found {len(m0['rails'])} — "
        f"renderTurnCtxNote produced nothing, every assertion below would "
        f"be vacuous")
    assert all(r['track'] for r in m0['rails']), (
        f"every visible rail must carry its measured track: {m0['rails']}")
    doc = [r['hostDocTop'] for r in m0['rails']]

    # ── (1) Follow: round 1's host is 400px above the pane → card 1 pins ──
    _scroll_to(page, doc[0] + 400)
    m = _measure(page)
    r1, r2, r3 = m['rails']
    assert _pinned_at_top(r1, m['contTop']), (
        f"round 1's card did not follow the scroll: {r1}")
    assert _parked(r2), (
        f"round 2's card must stay PARKED at its host while the host is "
        f"below the pane top: {r2}")
    assert r1['bottom'] <= r2['top'] + 0.5, (
        f"pinned card 1 overlaps card 2: bottom={r1['bottom']} top={r2['top']}")

    # ── (2) Same frame: pinned before any script could run ──
    m = page.evaluate(_SCROLL_AND_MEASURE, doc[0] + 900)
    assert _pinned_at_top(m['rails'][0], m['contTop']), (
        f"card 1 was not pinned in the frame of the scroll — its travel "
        f"waits for a script and trails the transcript: {m['rails'][0]}")

    # ── (3) Handoff: card 1 is pushed out EXACTLY as card 2 arrives ──
    # Put round 2's host a little BELOW the pin line so card 2 is still
    # parked; card 1's track must land its bottom exactly on card 2's top.
    _scroll_to(page, doc[1] - 46)
    m = _measure(page)
    r1, r2, _ = m['rails']
    assert r1['top'] < m['contTop'], (
        f"card 1 should be mid-push-out during handoff: {r1}")
    assert _parked(r2), f"round 2's host is below the pin line: {r2}"
    assert abs(r1['bottom'] - r2['top']) <= 1.0, (
        f"handoff must be continuous — card 1 bottom {r1['bottom']} vs "
        f"card 2 top {r2['top']}: a gap means round 1's context vanishes "
        f"early, an overlap means it covers round 2's card")

    # ── (4) Deep in round 2: card 2 pinned, card 1 fully pushed out ──
    _scroll_to(page, doc[2] - 300)
    m = _measure(page)
    r1, r2, r3 = m['rails']
    assert _pinned_at_top(r2, m['contTop']), (
        f"round 2's card did not follow the scroll: {r2}")
    assert r1['bottom'] <= r2['top'] + 0.5
    assert _parked(r3), f"round 3's card must stay parked: {r3}"

    # ── (5) Tail: at the very bottom the LAST round's card stays pinned ──
    _scroll_to(page, m['scrollHeight'])
    m = _measure(page)
    assert _pinned_at_top(m['rails'][2], m['contTop']), (
        f"the last round's card must stay pinned at the scroll tail: "
        f"{m['rails'][2]}")

    # ── (6) Paint carve-out: the host must not clip the travelling card ──
    # getBoundingClientRect measures LAYOUT boxes, not paint — a restored
    # content-visibility:auto would pass every assertion above while
    # visually slicing the card at its host's border box. Pin the computed
    # style that un-clips it.
    cv = page.evaluate(
        "() => getComputedStyle(document.querySelector('.__sticky-turn-1'))"
        ".contentVisibility")
    assert cv == 'visible', (
        f"rail host computed content-visibility is {cv!r}, not 'visible' — "
        f"the paint-containment carve-out (.message:has(> .turn-ctx)) is "
        f"gone, so a pinned card is CLIPPED at its one-line host's edge")
    # The same host keeps layout containment, which holds the track's
    # overflow out of the transcript's scroll range.
    contain = page.evaluate(
        "() => getComputedStyle(document.querySelector('.__sticky-turn-1')).contain")
    assert 'layout' in contain.split(), (
        f"rail host computed contain is {contain!r} — without layout "
        f"containment the track's overflow joins the transcript's scroll range")

    # ── (7) Fold complement: below the grant floor every rail parks ──
    page.set_viewport_size({'width': 1000, 'height': 900})
    page.wait_for_timeout(150)  # container query + ResizeObserver → rAF pass
    m = _measure(page)
    assert all(r['display'] == 'none' for r in m['rails']), (
        f"rails must fold away on a narrow pane: "
        f"{[r['display'] for r in m['rails']]}")
    assert all(r['track'] == '' for r in m['rails']), (
        f"a hidden rail kept its track — re-granting the rail would "
        f"resurrect a stale length: {m['rails']}")
