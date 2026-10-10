"""The turn-ctx rail FOLLOWS the scroll until the next round arrives.

WHY THIS FILE EXISTS
────────────────────
The rail (`.turn-ctx`) is absolutely positioned at the TOP of its own input
turn's box — out of flow so it never inflates the row (that contract is
swept by tests/turn_lifecycle/test_turn_ctx_rail_geometry.py). But an input turn is often
ONE line, so a purely box-bound rail scrolled away with its bubble while
the round's reply filled screens below it (2026-09-08 owner report: "when
a conversation is scrolling, the turn ctx should move along until it
reaches the next round").

The follower in info-rail.js re-implements sticky-header stacking WITHOUT a
DOM restructure: on scroll/resize it translates each rail down by
``clamp(stickTop − hostTop, 0, nextRailTop − railHeight − hostTop)`` —
pinned to the pane's top inside its round, pushed out exactly when the next
round's rail arrives, in one continuous motion. The travel carries the rail
BEYOND its host's border box, which is why rail hosts drop
content-visibility's paint containment (`.message:has(> .turn-ctx)` in
17-compaction-context-rail.css).

WHAT THIS ASSERTS (results, not constants)
──────────────────────────────────────────
1. **Follow** — with its host scrolled above the pane, the round's rail is
   pinned AT the pane's top edge (small pad, asserted as a range — the pad
   is a tunable), carrying `.tctx-stuck`; later rounds' rails stay parked
   (no class, no transform).
2. **Continuous handoff** — mid-push-out, the departing rail's bottom is
   EXACTLY the arriving rail's top (±1px): a gap means the previous round's
   context vanishes early, an overlap means it covers the next round's.
3. **Tail** — at the very bottom of the scroll, the LAST round's rail is
   still pinned (streaming replies grow the content under it).
4. **Paint carve-out** — a rail host's computed `content-visibility` is
   `visible`: getBoundingClientRect measures layout boxes, NOT paint, so
   the pin/geometry assertions above cannot see a restored
   `content-visibility:auto` clipping the travelling rail at its host's
   edge. This is asserted separately and explicitly.
5. **Fold complement** — below the rail-grant floor (narrow pane) every
   rail is display:none AND has its travel parked (no transform, no
   `.tctx-stuck`) — re-granting the track must never resurrect a stale
   offset far from the host.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.slow]

#: "At the pane's top edge" is asserted as a RANGE: the stick pad is a
#: visual tunable, not the contract. The contract is that the rail sits
#: flush under the pane's top edge, not at a named pixel.
_TOP_EDGE_RANGE_PX = 12

#: A modest snapshot — three tool chips + one workspace path. The follow
#: contract doesn't care about rail CONTENT, only that the rail is tall
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
#: hand-written copy of the rail markup.
_PLANT = """() => {
    const inner = document.querySelector('.chat-inner');
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
                    + 'round ' + i + '</div></div></div>' + parts.rail;
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
}""" % (_SNAPSHOT,)

#: Geometry dump: per rail — host position in SCROLL-CONTENT coordinates
#: (stable across scrolls), the rail's own viewport-space box, and its
#: follower state. Plus the container's top edge and scroll metrics.
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
            const rr = r.getBoundingClientRect();
            return {
                hostDocTop: hr.top - contR.top + cont.scrollTop,
                top: rr.top,
                bottom: rr.bottom,
                stuck: r.classList.contains('tctx-stuck'),
                transform: r.style.transform || '',
                display: getComputedStyle(r).display,
            };
        }),
    };
}"""


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
    # The plant grows chat-inner → the follower's ResizeObserver schedules a
    # pass; settle it before measuring the parked baseline.
    page.wait_for_timeout(120)


def _measure(page):
    return page.evaluate(_MEASURE)


def _scroll_to(page, y):
    page.evaluate(
        "(y) => { document.querySelector('.chat-container').scrollTop = y; }", y)
    # scroll event → rAF-coalesced follower pass; 90ms ≈ 5 frames at 60fps.
    page.wait_for_timeout(90)


def _pinned_at_top(rail, cont_top):
    return (rail['stuck']
            and 0 <= rail['top'] - cont_top <= _TOP_EDGE_RANGE_PX)


def test_rail_follows_scroll_until_the_next_round(page):
    _boot(page)
    m0 = _measure(page)
    assert len(m0['rails']) == 3, (
        f"expected the three planted rails, found {len(m0['rails'])} — "
        f"renderTurnCtxNote produced nothing, every assertion below would "
        f"be vacuous")
    assert all(not r['stuck'] and r['transform'] == '' for r in m0['rails']), (
        f"rails must start PARKED at scroll 0: {m0['rails']}")
    doc = [r['hostDocTop'] for r in m0['rails']]

    # ── (1) Follow: round 1's host is 400px above the pane → rail 1 pins ──
    _scroll_to(page, doc[0] + 400)
    m = _measure(page)
    r1, r2, r3 = m['rails']
    assert _pinned_at_top(r1, m['contTop']), (
        f"round 1's rail did not follow the scroll: {r1}")
    assert not r2['stuck'] and r2['transform'] == '', (
        f"round 2's rail must stay PARKED while its host is below the pane "
        f"top: {r2}")
    assert r1['bottom'] <= r2['top'] + 0.5, (
        f"pinned rail 1 overlaps rail 2: bottom={r1['bottom']} top={r2['top']}")

    # ── (2) Handoff: rail 1 is pushed out EXACTLY as rail 2 arrives ──
    # Put rail 2's host a little BELOW the stick line so rail 2 is still
    # parked; rail 1's clamp must land its bottom exactly on rail 2's top.
    _scroll_to(page, doc[1] - 46)
    m = _measure(page)
    r1, r2, _ = m['rails']
    assert r1['stuck'], f"rail 1 should be mid-push-out during handoff: {r1}"
    assert not r2['stuck'], f"rail 2's host is below the stick line: {r2}"
    assert abs(r1['bottom'] - r2['top']) <= 1.0, (
        f"handoff must be continuous — rail 1 bottom {r1['bottom']} vs "
        f"rail 2 top {r2['top']}: a gap means round 1's context vanishes "
        f"early, an overlap means it covers round 2's rail")

    # ── (3) Deep in round 2: rail 2 pinned, rail 1 fully pushed out ──
    _scroll_to(page, doc[2] - 300)
    m = _measure(page)
    r1, r2, r3 = m['rails']
    assert _pinned_at_top(r2, m['contTop']), (
        f"round 2's rail did not follow the scroll: {r2}")
    assert r1['bottom'] <= r2['top'] + 0.5
    assert not r3['stuck'], f"round 3's rail must stay parked: {r3}"

    # ── (4) Tail: at the very bottom the LAST round's rail stays pinned ──
    _scroll_to(page, m['scrollHeight'])
    m = _measure(page)
    assert _pinned_at_top(m['rails'][2], m['contTop']), (
        f"the last round's rail must stay pinned at the scroll tail: "
        f"{m['rails'][2]}")

    # ── (5) Paint carve-out: the host must not clip the travelling rail ──
    # getBoundingClientRect measures LAYOUT boxes, not paint — a restored
    # content-visibility:auto would pass every assertion above while
    # visually slicing the rail at its host's border box. Pin the computed
    # style that un-clips it.
    cv = page.evaluate(
        "() => getComputedStyle(document.querySelector('.__sticky-turn-1'))"
        ".contentVisibility")
    assert cv == 'visible', (
        f"rail host computed content-visibility is {cv!r}, not 'visible' — "
        f"the paint-containment carve-out (.message:has(> .turn-ctx)) is "
        f"gone, so a stuck rail is CLIPPED at its one-line host's edge")

    # ── (6) Fold complement: below the grant floor every rail parks ──
    page.set_viewport_size({'width': 1000, 'height': 900})
    page.wait_for_timeout(150)  # container query + ResizeObserver → rAF pass
    m = _measure(page)
    assert all(r['display'] == 'none' for r in m['rails']), (
        f"rails must fold away on a narrow pane: "
        f"{[r['display'] for r in m['rails']]}")
    assert all(r['transform'] == '' and not r['stuck'] for r in m['rails']), (
        f"a hidden rail kept its translateY — re-granting the track would "
        f"resurrect a stale offset far from its host: {m['rails']}")
