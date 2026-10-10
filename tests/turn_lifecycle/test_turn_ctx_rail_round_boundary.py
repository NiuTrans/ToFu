"""The rail stops at its own round; live patch never duplicates a row.

WHY THIS FILE EXISTS
────────────────────
Two 2026-09-23 owner reports on the per-turn context rail:

1. **Glue** — "the sidebar scroll must not run past the text area, or it
   sticks to the turn-ctx below." The follower's travel boundary was the
   next RAILED turn (`live[i+1]`), and the geometry-owned height budget
   grew the rail flush down to it. With the assistant reply between two
   human turns, a tall rail slid past its own round and painted flush
   against the NEXT round's rail — one glued block. The boundary is now
   the next ROUND (the next `.user-msg`), so a rail stops at its own
   round's reply and the two never touch.

2. **Duplicate branch** — the live patch (`_patchLiveCtxRails`) locates
   the git/todo rows by `[data-tctx-git]` / `[data-tctx-todo]`. The TS
   fallback renderer (`buildContextNodes`) tagged those rows only by
   class, so a rail rendered through that path carried no key; the patch
   missed the row and APPENDED a second one (the circled duplicate branch
   in the report). Both renderers now emit the same keys.

WHAT THIS ASSERTS
─────────────────
1. **Boundary** — with round 1's rail pinned and the next human turn far
   below, the rail's bottom never crosses the next `.user-msg` top, and
   its inline `maxHeight` budget equals exactly that gap.
2. **No duplicate key collision** — the fallback-rendered git/todo rows
   carry `data-tctx-git` / `data-tctx-todo`, the same keys the live patch
   replaces in place, so a refresh rebuilds rather than appends.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.slow]

_SNAPSHOT = """{
  model: 'claude-opus-5',
  depth: 'think',
  tools: [
    {label:'Search ×N', tone:'search'}, {label:'Fetch', tone:'net'},
    {label:'Memory', tone:'ai'}, {label:'MCP: github ×26', tone:'mcp'}
  ],
  roots: [
    {short:'INS/tofu', path:'/path/to/your/project', ro:false}
  ],
  git: {branch:'eval/opencode-glm53-tb21', branches:[
    {branch:'eval/opencode-glm53-tb21', path:'/x', short:'INS/tofu'}]},
  todos: [
    {status:'in_progress', content:'Discovery: locate browser_click'},
    {status:'pending', content:'Fix 1: tighten download description'}
  ]
}"""

#: Plant round 1 (user turn + rail + tall assistant reply) then round 2
#: (user turn + rail). Round 2 is the NEXT ROUND; round 1's rail must stop
#: at it, not glide through the reply into round 2's rail.
_PLANT = """() => {
    const inner = document.querySelector('.chat-inner');
    if (!inner) return 'no .chat-inner';
    const renderTurnCtxNote = window.TofuModules.resolveAction('renderTurnCtxNote');
    if (typeof renderTurnCtxNote !== 'function') return 'no renderTurnCtxNote';
    document.querySelectorAll('.__glue').forEach(n => n.remove());
    const parts = renderTurnCtxNote(%s);
    if (!parts) return 'renderer returned empty';
    const mkUser = (cls) => {
        const d = document.createElement('div');
        d.className = 'message user-msg __glue ' + cls;
        d.innerHTML = '<div class="message-avatar"></div>'
                    + '<div class="message-content"><div class="message-header">'
                    + '<span class="message-role">You</span></div>'
                    + parts.fold
                    + '<div class="message-body"><div class="md-content user-content">'
                    + 'round</div></div></div>' + parts.rail;
        return d;
    };
    const mkReply = () => {
        const gap = document.createElement('div');
        gap.className = 'message __glue __glue-reply';
        gap.style.height = '2000px';
        gap.innerHTML = '<div class="message-avatar"></div>'
                      + '<div class="message-content"><div class="message-body">'
                      + '<div class="md-content">reply</div></div></div>';
        return gap;
    };
    inner.appendChild(mkUser('__glue-u1'));
    inner.appendChild(mkReply());
    inner.appendChild(mkUser('__glue-u2'));
    inner.appendChild(mkReply());
    return 'ok';
}""" % (_SNAPSHOT,)

_MEASURE = """() => {
    const cont = document.querySelector('.chat-container');
    const contR = cont.getBoundingClientRect();
    const rail1 = document.querySelector('.__glue-u1 > .turn-ctx');
    const u2 = document.querySelector('.__glue-u2');
    if (!rail1 || !u2) return { ok: false };
    const rr = rail1.getBoundingClientRect();
    const u2r = u2.getBoundingClientRect();
    const host = rail1.parentElement.getBoundingClientRect();
    return {
        ok: true,
        contTop: contR.top,
        hostDocTop: host.top - contR.top + cont.scrollTop,
        u2DocTop: u2r.top - contR.top + cont.scrollTop,
        rail: {
            top: rr.top, bottom: rr.bottom, height: rr.height,
            stuck: rail1.classList.contains('tctx-stuck'),
            maxHeight: rail1.style.maxHeight || '',
            gitRows: rail1.querySelectorAll('[data-tctx-git]').length,
            todoRows: rail1.querySelectorAll('[data-tctx-todo]').length,
            gitKeyed: !!rail1.querySelector('.tctx-git-row[data-tctx-git]'),
            todoKeyed: !!rail1.querySelector('.tctx-todo-row[data-tctx-todo]'),
        },
        u2Top: u2r.top,
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
    page.wait_for_timeout(150)


def test_rail_stops_at_next_round_and_never_duplicates(page):
    _boot(page)
    m0 = page.evaluate(_MEASURE)
    assert m0['ok'], 'probe rails did not render'
    host = m0['hostDocTop']

    # (2) Keys: the fallback renderer tags its git/todo rows with the same
    # keys the live patch uses, and there is exactly ONE of each — a refresh
    # rebuilds in place instead of appending a duplicate.
    r0 = m0['rail']
    assert r0['gitKeyed'], f"git row missing data-tctx-git: {r0}"
    assert r0['todoKeyed'], f"todo row missing data-tctx-todo: {r0}"
    assert r0['gitRows'] == 1, f"expected exactly ONE branch row: {r0}"
    assert r0['todoRows'] == 1, f"expected exactly ONE checklist row: {r0}"

    # Pin round 1's rail at the pane top with round 2 (the next .user-msg)
    # far below — the state where it used to glide into round 2's rail.
    page.evaluate(
        "(y) => { document.querySelector('.chat-container').scrollTop = y; }",
        host + 500)
    page.wait_for_timeout(90)
    m = page.evaluate(_MEASURE)
    r = m['rail']
    assert r['stuck'], f"round 1's rail did not pin: {r}"

    # (1) Boundary: the rail's bottom never crosses the next round's top.
    assert r['bottom'] <= m['u2Top'] + 0.5, (
        f"the rail overruns its round and glues to the next turn-ctx: "
        f"bottom={r['bottom']} nextRoundTop={m['u2Top']} — {r}")

    # The height budget equals exactly that gap (nextRoundTop − visibleTop),
    # so the rail fills ITS round's gutter but stops at the round boundary.
    expected = round(m['u2Top'] - r['top'] - 1)
    got = int(r['maxHeight'].replace('px', '') or 0)
    assert abs(got - expected) <= 2, (
        f"the max-height budget {got}px is not the round gap {expected}px — "
        f"the rail is not bounded by its own round: {r}")
