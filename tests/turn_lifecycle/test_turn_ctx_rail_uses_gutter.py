"""A tall turn-ctx rail USES the free gutter — one fully-visible block.

WHY THIS FILE EXISTS
────────────────────
The rail (`.turn-ctx`) sits in the empty gutter to the right of the reply
column. That gutter is hundreds of px tall while the round's reply fills it,
but the rail's height used to be capped two ways that threw the space away:

  * parked — `max-height:100%` of its host message. An input turn is often
    ONE line, so the rail got ~1 line of height and its facts scrolled
    internally.
  * stuck — a fixed `60vh`.

Either way a fat rail (many tools + a workspace + a branch + an expanded
checklist) became a scrollable box: the head (model/tools/workspace) slid
off one side while the tail (branch/todos) stayed, reading as "two halves
that don't move together" (2026-09-23 owner report: "there is so much space
on the side — why can't it be one integrated block?").

The follower's height budget is now the SAME geometry that clamps its
travel — `nextTop − visibleTop`, written as an inline `max-height` every
pass — so the rail fills the free gutter beside the reply instead of an
arbitrary share of the viewport. The CSS caps are gone.

WHAT THIS ASSERTS (results, not constants)
──────────────────────────────────────────
With a TALL rail pinned at the pane top and room to spare before the next
round's rail:
  1. **One block, fully used** — the rail grows to fill the free track
     (well past the old `60vh` cap), so its WHOLE content height is visible
     (`scrollHeight ≤ clientHeight`, no internal scroll).
  2. **No squeeze** — the visible box is far taller than a one-line host
     would ever allow, proving the host-relative cap is gone.
  3. **Contracts intact** — it is still `.tctx-stuck`, its transform is the
     clamp, and its bottom never overlaps the next round's rail top.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.slow]

#: A FAT rail: many tool chips (two "+N" groups), a workspace, a git branch,
#: and a six-item checklist. This is the shape that overflowed worst — the
#: checklist is expanded by clicking its "+N" so the rail is at its tallest.
_SNAPSHOT = """{
  model: 'claude-opus-5',
  depth: 'think',
  modes: [{label:'Autopilot', tone:'mode'}],
  tools: [
    {label:'Search ×N', tone:'search'}, {label:'Fetch', tone:'net'},
    {label:'Code Exec', tone:'code'}, {label:'Memory', tone:'ai'},
    {label:'Translate', tone:'ai'}, {label:'MCP: github ×26', tone:'mcp'},
    {label:'MCP: hope ×63', tone:'mcp'}, {label:'MCP: a ×8', tone:'mcp'},
    {label:'MCP: b ×2', tone:'mcp'}, {label:'MCP: c ×4', tone:'mcp'},
    {label:'MCP: d ×7', tone:'mcp'}, {label:'MCP: e ×3', tone:'mcp'}
  ],
  roots: [
    {short:'INS/tofu', path:'/path/to/your/project', ro:false}
  ],
  git: {branch:'eval/opencode-glm53-tb21', branches:[
    {branch:'eval/opencode-glm53-tb21', path:'/x', short:'INS/tofu'}]},
  todos: [
    {status:'in_progress', content:'Discovery: locate browser_click implementation'},
    {status:'pending', content:'Fix 1: tighten browser_download_url_to_server description'},
    {status:'pending', content:'Fix 2: browser_click trusted CDP events for MTD components'},
    {status:'pending', content:'Fix 3: tab_id stale fallback to active tab instead of error'},
    {status:'pending', content:'Test ladder: narrow tests then neighboring contracts'},
    {status:'pending', content:'Update owning docs + memory'}
  ]
}"""

#: Plant TWO one-line input turns carrying the fat rail, each followed by a
#: tall reply spacer, so round 1 can pin with real room before round 2's
#: rail. Then expand round 1's checklist "+N" so its rail is at its tallest.
_PLANT = """() => {
    const inner = document.querySelector('.chat-inner');
    if (!inner) return 'no .chat-inner';
    const renderTurnCtxNote = window.TofuModules.resolveAction('renderTurnCtxNote');
    if (typeof renderTurnCtxNote !== 'function') return 'no renderTurnCtxNote';
    document.querySelectorAll('.__gutter').forEach(n => n.remove());
    const parts = renderTurnCtxNote(%s);
    if (!parts) return 'renderer returned empty';
    for (let i = 1; i <= 2; i++) {
        const d = document.createElement('div');
        d.className = 'message user-msg __gutter __gutter-turn-' + i;
        d.innerHTML = '<div class="message-avatar"></div>'
                    + '<div class="message-content"><div class="message-header">'
                    + '<span class="message-role">You</span></div>'
                    + parts.fold
                    + '<div class="message-body"><div class="md-content user-content">'
                    + 'round ' + i + '</div></div></div>' + parts.rail;
        inner.appendChild(d);
        const gap = document.createElement('div');
        gap.className = 'message __gutter __gutter-gap-' + i;
        gap.style.height = '2600px';
        gap.innerHTML = '<div class="message-avatar"></div>'
                      + '<div class="message-content"><div class="message-body">'
                      + '<div class="md-content">reply ' + i + '</div></div></div>';
        inner.appendChild(gap);
    }
    /* Expand round 1's checklist: click every "+N" in its rail so the full
     * todo list + tool overflow render — the rail's tallest state. */
    const rail1 = document.querySelector('.__gutter-turn-1 .turn-ctx');
    rail1.querySelectorAll('[data-tctx-more]').forEach(b => b.click());
    return 'ok';
}""" % (_SNAPSHOT,)

_MEASURE = """() => {
    const cont = document.querySelector('.chat-container');
    const contR = cont.getBoundingClientRect();
    const out = { contTop: contR.top, scrollHeight: cont.scrollHeight, rails: [] };
    document.querySelectorAll('.__gutter-turn-1 .turn-ctx, .__gutter-turn-2 .turn-ctx')
      .forEach((r) => {
        const rr = r.getBoundingClientRect();
        const hr = r.parentElement.getBoundingClientRect();
        out.rails.push({
            hostDocTop: hr.top - contR.top + cont.scrollTop,
            top: rr.top, bottom: rr.bottom, height: rr.height,
            stuck: r.classList.contains('tctx-stuck'),
            transform: r.style.transform || '',
            maxHeight: r.style.maxHeight || '',
            scrollHeight: r.scrollHeight, clientHeight: r.clientHeight,
        });
    });
    return out;
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
    page.wait_for_timeout(150)


def _scroll_to(page, y):
    page.evaluate(
        "(y) => { document.querySelector('.chat-container').scrollTop = y; }", y)
    page.wait_for_timeout(90)


def test_tall_rail_uses_the_free_gutter(page):
    _boot(page)
    m0 = page.evaluate(_MEASURE)
    assert len(m0['rails']) == 2, f'expected the two planted rails: {m0}'
    doc1 = m0['rails'][0]['hostDocTop']

    # Pin round 1's rail at the pane top with its host well above, leaving
    # real room before round 2's rail (a 2600px reply — plenty of gutter).
    _scroll_to(page, doc1 + 600)
    m = page.evaluate(_MEASURE)
    r1, r2 = m['rails']

    # (3) Contracts: stuck, translated by the clamp, never overlapping round 2.
    assert r1['stuck'], f"round 1's rail did not pin: {r1}"
    assert r1['transform'].startswith('translateY('), (
        f"the pinned rail must carry the follower's translateY clamp: {r1}")
    assert not r2['stuck'], f"round 2's rail must stay parked: {r2}"
    assert r1['bottom'] <= r2['top'] + 0.5, (
        f"rail 1 overlaps rail 2: bottom={r1['bottom']} top={r2['top']}")

    # (2) No squeeze: far taller than a one-line host (the old 100% cap gave
    # ~24px). The threshold is a multiple of that host height, NOT a fixed
    # pixel count: the 2026-09-24 adaptive rail WIDENS the track at roomy
    # panes, so the same fat content wraps into fewer lines and the rail is
    # shorter at 1600px than at the old fixed 232px width — a constant 300px
    # would flag the wider (better) layout as a squeeze even though the
    # whole content is fully visible (assertion 1). 8× the one-line host is
    # unreachable while any host-relative cap survives.
    assert r1['height'] > 8 * 24, (
        f"the rail is still squeezed to {r1['height']}px — the host-relative "
        f"cap is not gone, the gutter's space is still thrown away: {r1}")

    # (1) One block, fully used: the whole content height is visible — the
    # rail shows its facts without an internal scrollbar (the "two halves"
    # complaint), because the budget grew to fit them.
    assert r1['scrollHeight'] <= r1['clientHeight'] + 1, (
        f"the rail still scrolls internally "
        f"(scrollHeight={r1['scrollHeight']} > clientHeight={r1['clientHeight']}) "
        f"despite free gutter space — the head and tail are still split: {r1}")
