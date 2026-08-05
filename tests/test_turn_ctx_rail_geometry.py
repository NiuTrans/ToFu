"""The per-turn context rail can never be clipped, in ANY pane state.

WHY THIS FILE EXISTS
────────────────────
The rail (`.turn-ctx`, built by ``static/js/info-rail.js``) used to be an
absolutely-positioned capsule anchored at ``left:calc(100% + 24px)`` off the
centred reading column, made visible by ``@media (max-width:1280px)``.

That predicate asks about the WINDOW. The space the rail actually lands in is::

    viewport − sidebar − request-inspector-drawer

The sidebar has FOUR widths (0 collapsed / 280 plain / 332 rail-collapsed /
430 has-rail) and ``body.ri-open .chat-container{margin-right:min(780px,94vw)}``
removes up to another 780px. Measured in a real browser across all 64
combinations (8 widths × 4 sidebar states × drawer open/closed) BEFORE the fix:

    shown-but-clipped : 35 / 64   (bar 33, hover panel 35)
    hidden-but-room   :  0 / 64

Zero states were hidden while having room, so **no viewport threshold can be
correct** — the predicate was blind to two of the three variables. The fix
gives the rail a grid track the pane OWNS and drives visibility with a
``@container`` query on ``.chat-container`` (the element whose used width
actually equals the available space; a query on ``.chat-wrapper`` would be
blind in the 32 drawer-open cells, where wrapper and container widths differ
by exactly 780px).

WHAT THIS ASSERTS (charter: assert the RESULT, not the threshold)
────────────────────────────────────────────────────────────────
1. **The invariant** — in every one of the 72 pane states, no rail element's
   right
   edge exceeds the pane's right edge, INCLUDING after the "+N" overflow is
   expanded. (A collapsed-only assertion would have passed while the old hover
   panel was still broken in 35 states.)
2. **The complement** — in roomy states the rail is actually PRESENT and
   NON-EMPTY. Without this, "hide it everywhere" would satisfy (1) and stay
   green forever.
3. **The height invariant** — the rail is OUT OF FLOW (absolutely
   positioned into its track's box), so a turn's height is its content's
   height in EVERY state, collapsed or expanded. The in-flow rail this
   replaced let a one-line user turn be stretched by the rail's own height
   (~200px+ of dead space under the bubble when many MCP servers were
   connected — the 2026-08-05 owner report that motivated the redesign).
   Re-admitting an in-flow rail turns this red in every short-turn state.
4. **No context is lost** — where the rail has no track (narrow pane / drawer
   open), the compact `.tctx-fold` summary is shown instead — measured by its
   rendered BOX, not just `display`, after a zero-width fold once passed the
   display check while painting nothing (2026-08-03).

None of these name a CSS constant or a source literal, so re-tuning the
breakpoint, the rail width or the clamp keeps them green while a real
regression turns them red. The ONE width the test does name —
``_RAIL_MIN_PANE`` — is not a tunable: it is DERIVED from the measure floor
(``_MIN_MEASURE_PX``) plus the fixed furniture, and the CSS grant threshold
is derived from the same sum, so the two must move together or not at all.

CONVERSATION-STATUS STRIP (second subject, same sweep)
──────────────────────────────────────────────────────
The context gauge (`.ctx-health-bar`) and the turn-nav dots are
CONVERSATION-scoped chrome — one gauge for the whole context window, one dot
strip for the whole conversation — unlike the per-turn rail. They used to
float absolutely over the message flow (gauge `left:18px`, dots `right:8px`,
both `top:50%` of `.chat-wrapper`): the gauge genuinely overlapped the
message column below a ~1420px pane, and the dots vanished under a viewport
media query. Both now live in-flow in `#convStatusStrip` directly above the
composer, inside `.input-inner` (so `--input-area-h` already includes them).

The strip invariant is CONTAINMENT + PLACEMENT + SHARED TRACK, not any
constant: in every state the strip is visible directly above the composer,
both cells' rects are contained inside the strip's rect, neither cell
overlaps any message or is clipped by the viewport — AND the strip is a
child of `.input-inner` with edges exactly equal to the composer's, so the
gauge and the dots sit on the input box's own width track (`--toolbar-w`).
The strip previously carried its own `max-width` (--msg-measure + 52px):
a second width source beside the composer's dynamic one, measurably
misaligned in every state (26px/side at the 820px floor — the 2026-08-03
owner report). An absolute float restored by a future edit escapes
containment in EVERY state; an independent strip width restored breaks the
edge equality in every state where the composer isn't exactly that width —
those are the NEUTERs this guards against.
The complement: both cells are present and non-empty in every state, so
"hide the chrome everywhere" cannot satisfy the geometry either.

COMPOSER LEFT-EDGE LOCK (third subject, same sweep)
───────────────────────────────────────────────────
The composer used to float centred in the pane (`.input-inner{margin:0
auto}`), which put its left edge ~(band−820)/2 RIGHT of the avatar column
in every roomy state — the input box visibly "swam" under the message
column (2026-08-03 owner report), and the zone under the rail could never
host anything because the composer's right side reached into it. The
composer now locks its LEFT edge to the message column's left edge (the
avatar line) in EVERY pane state: `.input-inner`'s margin-left replays
`.chat-inner`'s own centering math (the same max-width formula, the same
24px inset), fed by `--rail-w`/`--rail-gap` mirrored from `.chat-inner`
by main.js (`_mirrorRailGeometry`) — the chatpane container query cannot
reach the composer band, and cannot be extended to it either
(`container-type`'s layout containment would re-parent the mobile
bottom-sheet dropdowns' `position:fixed` from the viewport to the band).
`.input-area` also takes the same `margin-right` the request-inspector
drawer imposes on `.chat-container`, so the two bands always share one
used width — before that, a wide drawer put the composer's right half
UNDER the fixed drawer (send button unreachable). The invariant is
asserted as |composer.left − message.left| ≤ 1 in every swept state;
restoring `margin:0 auto` breaks it in every roomy state — that is the
NEUTER this guards against.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.slow]

#: All three shipped themes. The measure cap used to live ONLY in the tofu
#: block, so a single-theme sweep stayed green while dark/light users read the
#: full content box. Theme is a real axis of the scanning surface, not a skin.
_THEMES = ('dark', 'light', 'tofu')

#: 1100 exercises the boundary band directly: with the sidebar collapsed the
#: pane is ~1100px — just above the derived 1056px rail-grant floor — while
#: every sidebar-open state at the same viewport falls below it. The rail's
#: appearance/disappearance at the grant boundary is exactly what the 1368px
#: luxury-buffer threshold got wrong for a ~1365px pane (2026-08-03 report).
_WIDTHS = (1100, 1280, 1366, 1440, 1512, 1600, 1728, 1920, 2560)

#: (label, JS that puts the sidebar into that state). Mirrors the four real
#: widths the sidebar can have — see `.sidebar` / `.has-rail` in styles.css.
_SIDEBAR_STATES = (
    ('collapsed', "s.classList.remove('has-rail','rail-collapsed'); s.classList.add('collapsed')"),
    ('plain', "s.classList.remove('collapsed','has-rail','rail-collapsed')"),
    ('rail-collapsed', "s.classList.remove('collapsed'); s.classList.add('has-rail','rail-collapsed')"),
    ('has-rail', "s.classList.remove('collapsed','rail-collapsed'); s.classList.add('has-rail')"),
)

#: A snapshot with MANY tools — the shape that used to overflow worst, and the
#: one that exercises the "+N" overflow toggle.
_FAT_SNAPSHOT = """{
  model: 'claude-opus-5',
  depth: 'think',
  modes: [{label:'Autopilot', tone:'mode'}, {label:'Swarm', tone:'mode'}],
  tools: [
    {label:'Search ×N', tone:'search'}, {label:'Fetch', tone:'net'},
    {label:'Memory', tone:'ai'}, {label:'MCP: 12306-train', tone:'mcp'},
    {label:'MCP: github ×26', tone:'mcp'}, {label:'MCP: github-batch ×2', tone:'mcp'},
    {label:'MCP: hope ×50', tone:'mcp'}, {label:'MCP: llm ×30', tone:'mcp'},
    {label:'MCP: overleaf ×21', tone:'mcp'}, {label:'MCP: xuecheng ×32', tone:'mcp'}
  ],
  roots: [
    {short:'INS/chatui', path:'/path/to/your/project', ro:false},
    {short:'team/lib', path:'/path/to/your/data', ro:false},
    {short:'data/sets', path:'/path/to/your/data', ro:true},
    {short:'scratch/tmp', path:'/path/to/your/data', ro:false},
    {short:'vendor/sdk', path:'/path/to/your/data', ro:true}
  ]
}"""

#: Long enough that the text box FILLS its track instead of shrink-wrapping.
#: A one-word probe made the measure assertion vacuous: `.md-content` reported
#: the width of the word (~720px of track read as 720px of text either way),
#: so a runaway track width would not have been visible. Real prose wraps and
#: therefore reports the TRACK width, which is what the ceiling is about.
_PROSE = (
    'This turn exists to measure the running text column. It has to be long '
    'enough to wrap across several lines so the rendered markdown box fills '
    'the available track rather than shrink-wrapping to a single short word, '
    'because the width we care about is the one the reader s eye traverses '
    'from the start of a line to its end.'
)

#: Plant one SHORT user turn carrying that snapshot, through the production
#: renderer (`renderTurnCtxNote`) — never a hand-written copy of its markup.
_PLANT = """() => {
    const inner = document.querySelector('.chat-inner');
    if (!inner) return 'no .chat-inner';
    /* The composer's max-width transition (.4s ease) animates every
     * pane-driven width change, so a sweep that measures 70ms after each
     * viewport change would read MID-ANIMATION values and fail on timing,
     * not geometry. The geometry contract asserts the STATIC value —
     * disable the transition for the sweep (a visual nicety, not the
     * geometry under test). */
    const __noTr = document.createElement('style');
    __noTr.textContent = '.input-inner{transition:none!important}';
    document.head.appendChild(__noTr);
    if (typeof renderTurnCtxNote !== 'function') return 'no renderTurnCtxNote';
    document.querySelectorAll('.message.__probe').forEach(n => n.remove());
    const parts = renderTurnCtxNote(%s);
    if (!parts) return 'renderer returned empty';
    /* Two surfaces, two DOM homes — the SAME structure chat_render.js
     * assembles: the fold INSIDE .message-content between header and body,
     * the rail as a direct .message child (its grid track). Splicing the
     * fold as a direct child is the zero-width bug this sweep must never
     * re-admit. */
    const d = document.createElement('div');
    d.className = 'message user-msg __probe';
    d.innerHTML = '<div class="message-avatar"></div>'
                + '<div class="message-content"><div class="message-header">'
                + '<span class="message-role">You</span></div>'
                + parts.fold
                + '<div class="message-body"><div class="md-content user-content">'
                + %s + '</div></div>'
                + '</div>' + parts.rail;
    inner.appendChild(d);
    /* Conversation-scoped chrome, through its PRODUCTION builders — never a
     * hand-written copy: the gauge from context-bar.js (rAF-coalesced, the
     * sweep's own waits cover creation; the test also waits for attach), the
     * dots from turn_nav.js with five fake turns so the nav renders (it
     * empties itself below two turns). */
    if (typeof updateContextBar === 'function') updateContextBar();
    if (typeof buildTurnNav === 'function') {
        const __msgs = [];
        for (let __t = 1; __t <= 5; __t++) {
            __msgs.push({role:'user', content:'probe turn ' + __t + ' — geometry sweep seed'});
            __msgs.push({role:'assistant', content:'probe answer ' + __t});
        }
        buildTurnNav({id:'__probe_conv', messages:__msgs});
    }
    return 'ok';
}""" % (_FAT_SNAPSHOT, repr(_PROSE))

#: Measure the pane and every rail element inside the probe turn.
_PROBE = """() => {
    const msg  = document.querySelector('.message.__probe');
    const cont = document.querySelector('.chat-container');
    if (!msg || !cont) return null;
    const contR = cont.getBoundingClientRect();
    const rail  = msg.querySelector('.turn-ctx');
    const fold  = msg.querySelector('.tctx-fold');
    const shown = (el) => !!el && getComputedStyle(el).display !== 'none';
    const railShown = shown(rail);
    // The fold's VISIBILITY must be width-aware: a fold auto-placed into the
    // zero-width rail track reports display:flex with the correct text while
    // painting nothing (the 2026-08-03 bug — the geometry sweep's own
    // display-only check is how it stayed invisible). Measure its box.
    const foldRect = fold ? fold.getBoundingClientRect() : null;
    let worst = null, worstSel = '';
    if (railShown) {
        const all = [rail, ...rail.querySelectorAll('*')];
        for (const el of all) {
            if (getComputedStyle(el).display === 'none') continue;
            const r = el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) continue;
            if (worst === null || r.right > worst) {
                worst = r.right;
                worstSel = el.className || el.tagName;
            }
        }
    }
    const body = msg.querySelector('.message-content');
    // The RUNNING TEXT column — what the reader's eye actually traverses.
    // Measured on the rendered markdown box, not on any CSS variable, so a
    // future refactor that moves the cap elsewhere still gets policed.
    const md = msg.querySelector('.md-content') || body;
    // The composer: main.js floors its --toolbar-w to --msg-measure (the
    // reading column), so any measure change moves it too. If it desyncs
    // from the message column the input box stops lining up with the text
    // above it. The band's max-width is 52px + --toolbar-w (avatar track +
    // reading track), so composerW − measure is exactly 52 in every state —
    // the ±120 tolerance in assertion #8 catches a REAL desync (e.g. the
    // floor read going stale). `.input-box` is the VISIBLE box the strip
    // must sit above; `.input-inner` is the width track the strip SHARES.
    const composer = document.querySelector('.input-inner');
    const inputBox = document.querySelector('.input-box');
    // ── Conversation-status strip (gauge + turn-nav) ──
    // CONTAINMENT is the invariant: a cell that escapes the strip's rect
    // (e.g. an absolute float restored) is the old bug wearing a new name,
    // regardless of whether it happens to intersect prose in today's layout.
    const strip = document.getElementById('convStatusStrip');
    const gauge = document.getElementById('contextHealthBar');
    const nav   = document.getElementById('turnNav');
    const rectOf  = (el) => el ? el.getBoundingClientRect() : null;
    const visible = (el) => !!el && getComputedStyle(el).display !== 'none'
                          && !!(el.offsetWidth || el.offsetHeight);
    const overlap = (a, b) => !!a && !!b && a.width > 0 && b.width > 0 &&
        !(a.right <= b.left + 0.5 || b.right <= a.left + 0.5 ||
          a.bottom <= b.top + 0.5 || b.bottom <= a.top + 0.5);
    const within = (inn, out) => !!inn && !!out &&
        inn.left >= out.left - 0.5 && inn.right <= out.right + 0.5 &&
        inn.top >= out.top - 0.5 && inn.bottom <= out.bottom + 0.5;
    const stripR = rectOf(strip), gaugeR = rectOf(gauge), navR = rectOf(nav);
    const boxR = rectOf(inputBox);
    /* Overlap must be judged on the VISIBLE part of the message: a message
     * scrolled out of the container still reports a raw rect (possibly right
     * under the strip), which would fake an overlap. Scroll the probe into
     * view (it is the last element), then clip to the container's rect — an
     * empty clip means "not visible", and an invisible message cannot be
     * overlapped by anything. */
    cont.scrollTop = cont.scrollHeight;
    const msgR = msg.getBoundingClientRect();
    const clipToCont = (r) => {
        if (!r) return null;
        const c = {
            left: Math.max(r.left, contR.left), right: Math.min(r.right, contR.right),
            top: Math.max(r.top, contR.top), bottom: Math.min(r.bottom, contR.bottom),
        };
        if (c.right <= c.left || c.bottom <= c.top) return null;
        c.width = c.right - c.left; c.height = c.bottom - c.top;
        return c;
    };
    const visMsgR = clipToCont(msgR);
    const mdR  = clipToCont(rectOf(md));
    const compR = rectOf(composer);
    /* The composer's own width driver — main.js sets it inline (floored to
     * --msg-measure, expanded for a fat toolbar, border-inclusive). The
     * width contract derives the expected composer width from THIS value:
     * composerW == min(max(measure, toolbarW) + 52, paneWidth − 48). */
    const twRaw = composer ? getComputedStyle(composer).getPropertyValue('--toolbar-w') : '';
    const toolbarW = parseFloat(twRaw) || 0;
    const clippedByVw = (r) => !!r && (r.right > window.innerWidth + 0.5 || r.left < -0.5);
    // The body's CONTENT box — the measure minus the bubble's own inset. The
    // tofu theme pads `.message-content` by 16px a side and draws a 2.5px
    // border, which is legitimate bubble inset, NOT a second measure;
    // comparing prose against the border box flagged that inset as a measure
    // split in 126 states (36px), and padding-only still left the 4px border.
    let contentBox = 0;
    if (body) {
        const cs = getComputedStyle(body);
        contentBox = body.getBoundingClientRect().width
                   - (parseFloat(cs.paddingLeft) || 0)
                   - (parseFloat(cs.paddingRight) || 0)
                   - (parseFloat(cs.borderLeftWidth) || 0)
                   - (parseFloat(cs.borderRightWidth) || 0);
    }
    return {
        vw: window.innerWidth,
        theme: document.documentElement.getAttribute('data-theme') || 'dark',
        paneRight: contR.right,
        paneWidth: contR.width,
        measure: body ? body.getBoundingClientRect().width : 0,
        contentBox: contentBox,
        prose: md ? md.getBoundingClientRect().width : 0,
        composerW: composer ? composer.getBoundingClientRect().width : 0,
        toolbarW: toolbarW,
        railShown: railShown,
        foldShown: shown(fold),
        foldW: foldRect ? foldRect.width : 0,
        foldText: fold ? (fold.textContent || '').trim() : '',
        railRight: worst,
        worstSel: String(worstSel),
        railChips: rail ? rail.querySelectorAll('.tctx-chip').length : 0,
        railText: rail ? (rail.textContent || '').trim().length : 0,
        msgHeight: msg.getBoundingClientRect().height,
        bodyHeight: body ? body.getBoundingClientRect().height : 0,
        stripShown: visible(strip),
        stripAboveComposer: !!stripR && !!boxR && stripR.bottom <= boxR.top + 1,
        /* LEFT-EDGE LOCK: the composer sits ON the message column's left
         * edge (the avatar line) — msgR.left IS .chat-inner's content-left
         * (the probe message spans its full grid). A centred composer
         * (margin:0 auto) drifts ~(band−820)/2 right of it in roomy
         * states; a missing drawer margin-right on .input-area drifts
         * ~drawer/2 in wide drawer-open states. */
        compLeft: compR ? compR.left : null,
        compRight: compR ? compR.right : null,
        msgLeft: msgR.left,
        /* SHARED TRACK: the strip must be a CHILD of .input-inner and span
         * it exactly — then gauge-left == composer-left and dots-right ==
         * composer-right by construction. An independent strip max-width
         * (the pre-fix 872px) only ever agrees by coincidence: measured
         * 26px/side of overhang at the 820px floor (2026-08-03 report). */
        stripInTrack: !!strip && strip.parentElement === composer,
        stripMatchesComposer: !!stripR && !!compR &&
            Math.abs(stripR.left - compR.left) <= 1 &&
            Math.abs(stripR.right - compR.right) <= 1,
        gaugeShown: visible(gauge),
        gaugeInStripRect: within(gaugeR, stripR),
        gaugeOverlapMsg: overlap(gaugeR, visMsgR),
        gaugeOverlapProse: overlap(gaugeR, mdR),
        gaugeClipped: clippedByVw(gaugeR),
        navShown: visible(nav),
        navInStripRect: within(navR, stripR),
        navOverlapMsg: overlap(navR, visMsgR),
        navOverlapProse: overlap(navR, mdR),
        navClipped: clippedByVw(navR),
        navDots: nav ? nav.querySelectorAll('.turn-dot').length : 0,
    };
}"""

#: Click every "+N" toggle so the EXPANDED state is measured too — the old
#: hover panel clipped in MORE states (35) than the collapsed bar (33), so a
#: collapsed-only sweep would have passed while the real breakage remained.
_EXPAND = """() => {
    const btns = document.querySelectorAll('.message.__probe [data-tctx-more]');
    btns.forEach(b => b.click());
    return btns.length;
}"""

#: Force every overflow group back to hidden. WITHOUT THIS the sweep is wrong:
#: the toggle persists across viewport changes, so every state measured after
#: the first expansion reports the EXPANDED geometry while claiming to be
#: collapsed — the observation-window defect, in state rather than in time.
_COLLAPSE = """() => {
    document.querySelectorAll('.message.__probe .tctx-overflow').forEach(o => {
        o.setAttribute('hidden', '');
    });
    document.querySelectorAll('.message.__probe [data-tctx-more]').forEach(b => {
        const o = b.parentNode.querySelector('.tctx-overflow');
        b.setAttribute('aria-expanded', 'false');
        if (o) b.textContent = '+' + o.children.length;
    });
}"""

#: The rail is out of flow (styles.css `.turn-ctx` is absolutely
#: positioned into the track's box, `max-height:100%`), so it contributes
#: NOTHING to the row's height — collapsed or expanded. The probe bubble is
#: a few lines of prose (~110px), so `msg == body` to within borders and
#: grid rounding; 8px of slack catches an in-flow rail being re-admitted
#: (which would add 60–300px) without flaking on sub-pixel rounding.
_MAX_RAIL_INFLATION_PX = 8

#: Upper bound on the RUNNING TEXT column. The point of reclaiming dead space
#: is breathing room, NOT a longer line: an earlier version let the text column
#: absorb every spare pixel and it reached ~1272px (~130 characters) at 2560,
#: against ~720px before. Typographic guidance (and this file's own comments
#: elsewhere) puts the ceiling near 90 characters, so the measure is capped and
#: the surplus goes to the outer margins instead.
_MAX_MEASURE_PX = 920

#: Lower bound, so "cap the measure" cannot degenerate into "shrink the text".
#: The pre-change measure was ~720px; on a roomy pane we must be at least that
#: good, otherwise the redesign made reading worse in the name of tidiness.
_MIN_MEASURE_PX = 700

#: The pane width at which the rail track is granted. DERIVED, not tuned:
#: the smallest pane that hosts the rail while keeping the measure at or
#: above its floor is
#:     700 text + 52 avatar-and-gap + 12 text→rail gap + 232 rail + 48 padding
#:   = 1044px
#: granted at 1056px (floor + 12px of rounding/scrollbar slack). This MUST
#: match the `@container chatpane (min-width: …)` in styles.css (three
#: queries: the `.chat-inner` track grant, `.turn-ctx` display, `.tctx-fold`
#: hide) — the CSS comment above the `.chat-inner` rule carries the same sum.
#: The previous 1368px was full-comfort PLUS a 192px luxury buffer, which hid
#: the rail from every pane in 1056–1367 for no geometric reason.
_RAIL_MIN_PANE = 1056

#: Floor under the composer width (the CSS `.input-inner{min-width}`). Below
#: this the track math would squeeze the band to a useless sliver in hopeless
#: drawer panes (drawer 780 + wide sidebar on a small window) and the status
#: strip's gauge/dots would spill out of it. Not a tunable: it is the smallest
#: width that keeps the textarea usable and the strip cells inside the track,
#: and it must match the stylesheet or assertion #8 goes red precisely in the
#: states the floor governs.
_COMPOSER_FLOOR_PX = 300


def _sweep(page):
    """Return one measurement row per (theme × drawer × sidebar × width) state."""
    rows = []
    for theme in _THEMES:
        page.evaluate("(t) => document.documentElement.setAttribute('data-theme', t)",
                      theme)
        page.wait_for_timeout(120)
        for drawer in (False, True):
            page.evaluate("() => { %s }" % (
                "openRequestInspector()" if drawer else "closeRequestInspector()"))
            page.wait_for_timeout(200)
            for sb_label, setup in _SIDEBAR_STATES:
                page.evaluate("() => { const s = document.querySelector('.sidebar');"
                              " if (s) { %s; } }" % setup)
                for w in _WIDTHS:
                    page.set_viewport_size({'width': w, 'height': 900})
                    page.wait_for_timeout(70)
                    for expanded in (False, True):
                        if expanded:
                            page.evaluate(_EXPAND)
                        else:
                            page.evaluate(_COLLAPSE)
                        page.wait_for_timeout(50)
                        m = page.evaluate(_PROBE)
                        assert m is not None, 'probe turn vanished from the DOM'
                        m['drawer'] = drawer
                        m['sidebar'] = sb_label
                        m['expanded'] = expanded
                        rows.append(m)
    return rows


def test_rail_never_clipped_in_any_pane_state(page):
    """THE INVARIANT + its complement + the height bound, over 72 states."""
    page.wait_for_selector('#userInput', state='visible', timeout=20000)
    page.wait_for_function("typeof renderTurnCtxNote === 'function'", timeout=20000)
    planted = page.evaluate(_PLANT)
    assert planted == 'ok', f'could not plant the probe turn: {planted}'
    # The conversation chrome must have been built by its PRODUCTION builders:
    # the gauge is rAF-coalesced, so wait for attach; the dots are sync.
    page.wait_for_selector('#contextHealthBar', state='attached', timeout=5000)
    _dots = page.evaluate(
        "() => document.querySelectorAll('#turnNav .turn-dot').length")
    assert _dots >= 2, (
        f'probe nav rendered {_dots} dots — buildTurnNav did not produce the '
        f'five fake turns, so every nav assertion below would be vacuous')

    rows = _sweep(page)
    pane_states = {(r['drawer'], r['sidebar'], r['vw']) for r in rows}
    states = {(r['theme'], r['drawer'], r['sidebar'], r['vw']) for r in rows}
    assert len(pane_states) == 72, (
        f'expected 72 pane states (2 drawers \u00d7 4 sidebars \u00d7 9 widths), '
        f'swept {len(pane_states)}')
    assert len(states) == 72 * len(_THEMES), (
        f'expected {72 * len(_THEMES)} theme\u00d7pane states, swept {len(states)} '
        f'\u2014 the measure cap used to be theme-scoped, so a sweep that misses a '
        f'theme cannot see the very bug this guards')

    # ── 1. No rail element may cross the pane's right edge, ever. ──
    clipped = [r for r in rows
               if r['railShown'] and r['railRight'] is not None
               and r['railRight'] > r['paneRight'] + 0.5]

    # ── 2. COMPLEMENT: wherever the pane clears the derived grant floor the
    #      rail must really be there and carry content. Without this, hiding
    #      it everywhere passes (1). ──
    roomy = [r for r in rows if r['paneWidth'] >= _RAIL_MIN_PANE]
    missing = [r for r in roomy if not r['railShown'] or r['railText'] < 10]

    # ── 3. Height invariant: the rail never inflates a turn — in ANY
    #      state, expanded included. The old carve-out ("expanding is an
    #      explicit request, so growth there is the feature") died with the
    #      in-flow rail: expansion now reveals the hidden chips inside the
    #      SAME message-bounded box, which scrolls. ──
    too_tall = [r for r in rows
                if r['bodyHeight'] > 0
                and r['msgHeight'] > r['bodyHeight'] + _MAX_RAIL_INFLATION_PX]

    # ── 4. No context lost: no rail track → the fold summary stands in —
    #      VISIBLY. display:flex is not enough: the fold once auto-placed
    #      into the zero-width rail track and painted nothing while every
    #      display/text check passed, so the assertion demands a real box.
    #      Scoped to panes that can host a READABLE fold: the fold lives in
    #      the content column, which is pane − ~100px of furniture − up to
    #      37px of theme bubble inset, so a 156–196px pane still leaves the
    #      fold a 10–60px strip (measured: contentBox=10 at pane 156) — the
    #      same sub-legible sliver class as the 0–62px drawer panes in the
    #      design's own "as little as 74px" note. 200px is the first width
    #      with prose (≥5 chars/line) and a ≥20px fold box in every theme. ──
    lost = [r for r in rows
            if not r['railShown'] and r['paneWidth'] >= 200
            and (not r['foldShown'] or r['foldW'] < 20
                 or len(r['foldText']) < 5)]

    # ── 5. MEASURE CEILING: reclaimed space must become margin, not line
    #      length. Asserted on the RENDERED text box in every state. ──
    too_wide = [r for r in rows if r['measure'] > _MAX_MEASURE_PX]
    # ── 6. MEASURE FLOOR (complement): granting the rail must not starve the
    #      text. Scoped to rows where the RAIL IS SHOWN — that is precisely
    #      the set the grant threshold endangers (a rail-less narrow pane may
    #      legitimately have any measure, and a 70px drawer pane has none). ──
    too_narrow = [r for r in rows
                  if r['railShown'] and r['measure'] < _MIN_MEASURE_PX]

    # ── 7. ONE MEASURE: prose and non-prose must share it. Choosing option A
    #      means code blocks / tables / tool cards lay out against the SAME
    #      number as the paragraphs, so a code block never runs wider than the
    #      text above it. Before this, prose sat at 720px (theme-scoped 72ch)
    #      while the body box was 892px — two measures, one of them invisible. ──
    split_measure = [r for r in rows
                     if r['prose'] > 0 and r['contentBox'] > 0
                     and abs(r['contentBox'] - r['prose']) > 2]

    # ── 8. COMPOSER WIDTH TRACK (right edge): the band's width is
    #      min(max(52px + --toolbar-w, reading track), pane − 48), floored
    #      at _COMPOSER_FLOOR_PX (the CSS min-width) for HOPELESS panes —
    #      drawer open beside a wide sidebar can drop the pane below 100px,
    #      where the track math would squeeze the composer to a useless
    #      sliver and the strip cells would spill out. Elsewhere the
    #      formula collapses to min(max(measure, toolbarW) + 52, pane − 48):
    #      the composer covers avatar track + text column (right edge ON
    #      the text's right edge), widens only when the toolbar genuinely
    #      outgrows the measure (growing RIGHT into the empty rail zone),
    #      and never crosses the pane. Asserted against the composer's OWN
    #      inline --toolbar-w, so a stale floor read or a furniture desync
    #      goes red in the exact states it breaks. ──
    composer_off = [r for r in rows
                    if r['composerW'] > 0 and r['measure'] > 0
                    and r['toolbarW'] > 0
                    and abs(r['composerW']
                            - max(_COMPOSER_FLOOR_PX,
                                  min(max(r['measure'], r['toolbarW']) + 52,
                                      r['paneWidth'] - 48))) > 2]

    # ── 8a. COMPLEMENT: every row must carry the composer's inline
    #      --toolbar-w (the reflow ran) — without it the width contract
    #      above skips the row and "never floored" stays invisible. ──
    tw_missing = [r for r in rows if r['toolbarW'] <= 0]

    # ── 8b. COMPOSER LEFT-EDGE LOCK: the composer must sit ON the message
    #      column's left edge (the avatar line) in EVERY state — a centred
    #      composer (margin:0 auto) "swims" ~(band−820)/2 right of it in
    #      roomy states, and a composer band that ignores the drawer
    #      margin-right swims ~drawer/2 in wide drawer-open states. Scoped
    #      to panes with a real reading column: below 200px the 24px inset
    #      floor is the whole story on both sides and the measure is a
    #      sliver (same scoping logic as the fold assertion). ──
    composer_left_off = [r for r in rows
                         if r['compLeft'] is not None
                         and r['paneWidth'] >= 200
                         and abs(r['compLeft'] - r['msgLeft']) > 1]

    # ── 9. CONVERSATION CHROME (gauge + turn-nav): in-flow in the status
    #      strip directly above the composer, CONTAINED by it, never over a
    #      message, never clipped. Containment is the invariant that makes
    #      "restore the absolute float" go red in EVERY state, not just the
    #      states where the float happens to hit prose today. ──
    strip_missing = [r for r in rows
                     if not r['stripShown'] or not r['stripAboveComposer']]
    # ── 9b. SHARED TRACK: the strip rides the composer's own width track
    #      (child of .input-inner, edges equal) — a restored independent
    #      strip width (the 872px that motivated this) fails the edge check
    #      in every state whose composer isn't exactly that wide, and a
    #      restored DOM move fails the parent check in every state. ──
    strip_misaligned = [r for r in rows
                        if not r['stripInTrack'] or not r['stripMatchesComposer']]
    chrome_escaped = [r for r in rows
                      if (r['gaugeShown'] and not r['gaugeInStripRect'])
                      or (r['navShown'] and not r['navInStripRect'])]
    chrome_overlaps = [r for r in rows
                       if r['gaugeOverlapMsg'] or r['navOverlapMsg']
                       or r['gaugeOverlapProse'] or r['navOverlapProse']]
    chrome_clipped = [r for r in rows if r['gaugeClipped'] or r['navClipped']]
    # ── 10. CHROME COMPLEMENT: both cells must actually BE there in every
    #      state — "hide the chrome everywhere" satisfies all of the above.
    #      The sweep planted five turns, so an empty nav is always a
    #      regression here, never a legitimate state. ──
    gauge_missing = [r for r in rows if not r['gaugeShown']]
    nav_missing = [r for r in rows if not r['navShown'] or r['navDots'] < 2]

    print(f'\n  states swept              : {len(states)} '
          f'({len(_THEMES)} themes \u00d7 {len(pane_states)} pane states \u00d7 2 = {len(rows)} rows)')
    print(f'  rail CLIPPED              : {len(clipped)}')
    print(f'  roomy states w/o rail     : {len(missing)} (of {len(roomy)} roomy rows)')
    print(f'  turns inflated > +{_MAX_RAIL_INFLATION_PX}px       : {len(too_tall)}')
    print(f'  no rail AND no fold       : {len(lost)}')
    if rows:
        _mw = max(r['measure'] for r in rows)
        _mn = min((r['measure'] for r in rows if r['railShown']),
                  default=0)
        print(f'  text measure (max / roomy-min): {_mw:.0f}px / {_mn:.0f}px'
              f'  [bounds {_MIN_MEASURE_PX}\u2013{_MAX_MEASURE_PX}]')
        print(f'  measure over ceiling      : {len(too_wide)}')
        print(f'  measure under floor       : {len(too_narrow)}')
        for _t in _THEMES:
            _tr = [r for r in rows if r['theme'] == _t]
            if not _tr:
                continue
            _tmax = max(r['measure'] for r in _tr)
            _tpr = max(r['prose'] for r in _tr)
            _tover = len([r for r in _tr if r['measure'] > _MAX_MEASURE_PX])
            _tunder = len([r for r in _tr if r['railShown']
                           and r['measure'] < _MIN_MEASURE_PX])
            print(f'    [{_t:<5}] body-max={_tmax:.0f}px prose-max={_tpr:.0f}px '
                  f'over={_tover} under={_tunder}')
        print(f'  prose/body disagreement   : {len(split_measure)}')
        print(f'  composer desynced         : {len(composer_off)}')
        print(f'  composer off the avatar line: {len(composer_left_off)}')
        print(f'  composer toolbar-w missing  : {len(tw_missing)}')
        print(f'  strip missing/misplaced   : {len(strip_missing)}')
        print(f'  strip off composer track  : {len(strip_misaligned)}')
        print(f'  chrome escaped strip      : {len(chrome_escaped)}')
        print(f'  chrome overlapping msgs   : {len(chrome_overlaps)}')
        print(f'  chrome clipped by viewport: {len(chrome_clipped)}')
        print(f'  gauge missing             : {len(gauge_missing)}')
        print(f'  nav missing/empty         : {len(nav_missing)}')
    if rows:
        _w = max(r['paneWidth'] for r in rows)
        _n = min(r['paneWidth'] for r in rows)
        print(f'  pane width range          : {_n:.0f} … {_w:.0f}px')

    assert not clipped, (
        'the rail crosses the pane\'s right edge in %d state(s) — this is the '
        'very overflow the container query is supposed to make impossible:\n  '
        % len(clipped)
        + '\n  '.join(
            'drawer=%s sidebar=%-14s vw=%-5d expanded=%-5s railRight=%.0f > paneRight=%.0f  (%s)'
            % (r['drawer'], r['sidebar'], r['vw'], r['expanded'],
               r['railRight'], r['paneRight'], r['worstSel'][:40])
            for r in clipped[:12]))

    assert not missing, (
        'the rail is absent or empty in %d roomy state(s) — "hide it '
        'everywhere" must NOT be a way to satisfy the no-clip invariant:\n  '
        % len(missing)
        + '\n  '.join(
            'drawer=%s sidebar=%-14s vw=%-5d paneWidth=%.0f railShown=%s textLen=%d'
            % (r['drawer'], r['sidebar'], r['vw'], r['paneWidth'],
               r['railShown'], r['railText'])
            for r in missing[:12]))

    assert not too_tall, (
        'the rail inflates a turn past its own content height (+%dpx) in %d '
        'state(s) — the rail is out of flow and must contribute NOTHING to '
        'the row\'s height, expanded or not; an in-flow rail is the '
        '2026-08-05 "huge gap between bubbles" bug:\n  '
        % (_MAX_RAIL_INFLATION_PX, len(too_tall))
        + '\n  '.join(
            'drawer=%s sidebar=%-14s vw=%-5d expanded=%-5s msg=%.0f body=%.0f'
            % (r['drawer'], r['sidebar'], r['vw'], r['expanded'],
               r['msgHeight'], r['bodyHeight'])
            for r in too_tall[:12]))

    assert not lost, (
        'in %d state(s) the rail is hidden AND no fold summary replaces it — '
        'the turn\'s context would be silently lost:\n  ' % len(lost)
        + '\n  '.join(
            'drawer=%s sidebar=%-14s vw=%-5d paneWidth=%.0f fold=%r'
            % (r['drawer'], r['sidebar'], r['vw'], r['paneWidth'], r['foldText'][:40])
            for r in lost[:12]))


    assert not too_wide, (
        'the running text column exceeds %dpx in %d state(s) — reclaimed dead '
        'space must become MARGIN, not line length; an over-long measure is a '
        'different readability bug, not a fix:\n  '
        % (_MAX_MEASURE_PX, len(too_wide))
        + '\n  '.join(
            'drawer=%s sidebar=%-14s vw=%-5d paneWidth=%.0f measure=%.0f'
            % (r['drawer'], r['sidebar'], r['vw'], r['paneWidth'], r['measure'])
            for r in too_wide[:12]))

    assert not tw_missing, (
        'the composer carries no inline --toolbar-w in %d state(s) — the '
        'width contract above skips those rows, so a dead _reflowToolbar '
        'would pass silently:\n  '
        % len(tw_missing)
        + '\n  '.join(
            'theme=%-5s drawer=%s sidebar=%-14s vw=%-5d'
            % (r['theme'], r['drawer'], r['sidebar'], r['vw'])
            for r in tw_missing[:12]))

    assert not composer_left_off, (
        'the composer\'s left edge leaves the message column\'s left edge '
        '(the avatar line) in %d state(s) — the input box "swims" under the '
        'conversation instead of aligning with it (2026-08-03 owner report); '
        'a restored margin:0 auto produces exactly this in every roomy '
        'state:\n  '
        % len(composer_left_off)
        + '\n  '.join(
            'drawer=%s sidebar=%-14s vw=%-5d paneWidth=%.0f compLeft=%.1f msgLeft=%.1f'
            % (r['drawer'], r['sidebar'], r['vw'], r['paneWidth'],
               r['compLeft'], r['msgLeft'])
            for r in composer_left_off[:12]))

    assert not too_narrow, (
        'the running text column is below %dpx in %d roomy state(s) — capping '
        'the measure must not become shrinking it:\n  '
        % (_MIN_MEASURE_PX, len(too_narrow))
        + '\n  '.join(
            'drawer=%s sidebar=%-14s vw=%-5d paneWidth=%.0f measure=%.0f'
            % (r['drawer'], r['sidebar'], r['vw'], r['paneWidth'], r['measure'])
            for r in too_narrow[:12]))


    assert not split_measure, (
        'prose and the message body disagree on the measure in %d state(s) — '
        'option A means ONE number for the whole body, so code blocks and '
        'tables cannot run wider than the paragraphs beside them:\n  '
        % len(split_measure)
        + '\n  '.join(
            'theme=%-5s drawer=%s sidebar=%-14s vw=%-5d contentBox=%.0f prose=%.0f'
            % (r['theme'], r['drawer'], r['sidebar'], r['vw'],
               r['contentBox'], r['prose'])
            for r in split_measure[:12]))

    assert not composer_off, (
        'the composer desynced from the message column in %d state(s) — '
        'its width must be max(%d, min(max(measure, toolbarW) + 52, '
        'pane − 48)): the right edge rides the text column\'s right edge, '
        'only a fat toolbar widens past it, and the %dpx floor keeps '
        'hopeless drawer panes usable:\n  '
        % (len(composer_off), _COMPOSER_FLOOR_PX, _COMPOSER_FLOOR_PX)
        + '\n  '.join(
            'theme=%-5s drawer=%s sidebar=%-14s vw=%-5d composer=%.0f expected≈%.0f (measure=%.0f tw=%.0f pane=%.0f)'
            % (r['theme'], r['drawer'], r['sidebar'], r['vw'], r['composerW'],
               max(_COMPOSER_FLOOR_PX,
                   min(max(r['measure'], r['toolbarW']) + 52,
                       r['paneWidth'] - 48)),
               r['measure'], r['toolbarW'], r['paneWidth'])
            for r in composer_off[:12]))

    assert not strip_missing, (
        'the conversation-status strip is hidden or NOT directly above the '
        'composer in %d state(s) — it is in-flow chrome, so a missing or '
        'misplaced strip means something positioned it again:\n  '
        % len(strip_missing)
        + '\n  '.join(
            'theme=%-5s drawer=%s sidebar=%-14s vw=%-5d shown=%s aboveComposer=%s'
            % (r['theme'], r['drawer'], r['sidebar'], r['vw'],
               r['stripShown'], r['stripAboveComposer'])
            for r in strip_missing[:12]))

    assert not strip_misaligned, (
        'the conversation-status strip left the composer\'s width track in '
        '%d state(s) — it must be a child of .input-inner spanning it '
        'exactly; an independent strip max-width (the pre-fix 872px) only '
        'agrees with the composer by coincidence (26px/side off at the '
        '820px floor, 2026-08-03 report):\n  '
        % len(strip_misaligned)
        + '\n  '.join(
            'theme=%-5s drawer=%s sidebar=%-14s vw=%-5d inTrack=%s edgesMatch=%s'
            % (r['theme'], r['drawer'], r['sidebar'], r['vw'],
               r['stripInTrack'], r['stripMatchesComposer'])
            for r in strip_misaligned[:12]))

    assert not chrome_escaped, (
        'a conversation-chrome cell ESCAPED the strip rect in %d state(s) — '
        'containment is the invariant; an absolute float restored by any '
        'future edit fails this in every state, which is the point:\n  '
        % len(chrome_escaped)
        + '\n  '.join(
            'theme=%-5s drawer=%s sidebar=%-14s vw=%-5d gaugeIn=%s navIn=%s'
            % (r['theme'], r['drawer'], r['sidebar'], r['vw'],
               r['gaugeInStripRect'], r['navInStripRect'])
            for r in chrome_escaped[:12]))

    assert not chrome_overlaps, (
        'the gauge or turn-nav OVERLAPS a message in %d state(s) — the '
        'pre-strip gauge genuinely covered the message column below a '
        '~1420px pane; that must never come back:\n  ' % len(chrome_overlaps)
        + '\n  '.join(
            'theme=%-5s drawer=%s sidebar=%-14s vw=%-5d gMsg=%s nMsg=%s gProse=%s nProse=%s'
            % (r['theme'], r['drawer'], r['sidebar'], r['vw'],
               r['gaugeOverlapMsg'], r['navOverlapMsg'],
               r['gaugeOverlapProse'], r['navOverlapProse'])
            for r in chrome_overlaps[:12]))

    assert not chrome_clipped, (
        'the gauge or turn-nav is CLIPPED by the viewport in %d state(s):\n  '
        % len(chrome_clipped)
        + '\n  '.join(
            'theme=%-5s drawer=%s sidebar=%-14s vw=%-5d gauge=%s nav=%s'
            % (r['theme'], r['drawer'], r['sidebar'], r['vw'],
               r['gaugeClipped'], r['navClipped'])
            for r in chrome_clipped[:12]))

    assert not gauge_missing, (
        'the context gauge is missing in %d state(s) — hiding the chrome '
        'everywhere must NOT be a way to satisfy the geometry:\n  '
        % len(gauge_missing)
        + '\n  '.join(
            'theme=%-5s drawer=%s sidebar=%-14s vw=%-5d'
            % (r['theme'], r['drawer'], r['sidebar'], r['vw'])
            for r in gauge_missing[:12]))

    assert not nav_missing, (
        'the turn-nav is missing or empty in %d state(s) — the sweep planted '
        'five turns, so an empty nav is a regression, not a state:\n  '
        % len(nav_missing)
        + '\n  '.join(
            'theme=%-5s drawer=%s sidebar=%-14s vw=%-5d shown=%s dots=%d'
            % (r['theme'], r['drawer'], r['sidebar'], r['vw'],
               r['navShown'], r['navDots'])
            for r in nav_missing[:12]))


def test_overflow_toggle_bounds_the_chip_count(page):
    """The "+N" toggle really gates chips, and expanding really reveals them.

    Guards the height bound's mechanism: if every chip rendered up-front the
    rail height would be driven by the number of connected MCP servers.
    """
    page.wait_for_selector('#userInput', state='visible', timeout=20000)
    page.wait_for_function("typeof renderTurnCtxNote === 'function'", timeout=20000)
    page.set_viewport_size({'width': 1920, 'height': 900})
    page.evaluate("() => { const s = document.querySelector('.sidebar');"
                  " if (s) s.classList.add('collapsed'); }")
    assert page.evaluate(_PLANT) == 'ok'
    page.wait_for_timeout(200)

    before = page.evaluate(
        "() => document.querySelectorAll('.message.__probe .tctx-chip:not(.tctx-overflow .tctx-chip)').length")
    total = page.evaluate(
        "() => document.querySelectorAll('.message.__probe .tctx-chip').length")
    btns = page.evaluate(_EXPAND)
    page.wait_for_timeout(150)
    visible_after = page.evaluate("""() => {
        const els = document.querySelectorAll('.message.__probe .tctx-chip');
        let n = 0;
        els.forEach(e => { if (e.offsetParent !== null) n++; });
        return n;
    }""")

    print(f'\n  chips total={total} visible-before={before} '
          f'toggles={btns} visible-after={visible_after}')

    assert btns >= 1, (
        'a 10-tool snapshot produced no "+N" toggle — the chip count is '
        'unbounded, so the rail height is driven by how many MCP servers '
        'happen to be connected')
    assert before < total, (
        f'all {total} chips rendered up-front ({before} visible) — the bound '
        f'is not in effect')
    assert visible_after > before, (
        f'clicking "+N" revealed nothing ({before} → {visible_after}); the '
        f'hidden chips are unreachable, so the information is lost rather '
        f'than collapsed')


def test_overflow_toggle_bounds_the_path_count(page):
    """The "+N" toggle bounds WORKSPACE paths the same way it bounds chips.

    Roots used to render unbounded — the same defect family as chips (the
    rail's height driven by how many roots the workspace happens to have).
    The probe snapshot carries five roots, so a bound must gate them and
    the toggle must really reveal the rest.
    """
    page.wait_for_selector('#userInput', state='visible', timeout=20000)
    page.wait_for_function("typeof renderTurnCtxNote === 'function'", timeout=20000)
    page.set_viewport_size({'width': 1920, 'height': 900})
    page.evaluate("() => { const s = document.querySelector('.sidebar');"
                  " if (s) s.classList.add('collapsed'); }")
    assert page.evaluate(_PLANT) == 'ok'
    page.wait_for_timeout(200)

    before = page.evaluate(
        "() => document.querySelectorAll('.message.__probe .tctx-path:not(.tctx-overflow .tctx-path)').length")
    total = page.evaluate(
        "() => document.querySelectorAll('.message.__probe .tctx-path').length")
    toggles = page.evaluate(
        "() => document.querySelectorAll('.message.__probe .tctx-paths [data-tctx-more]').length")
    page.evaluate(_EXPAND)
    page.wait_for_timeout(150)
    visible_after = page.evaluate("""() => {
        const els = document.querySelectorAll('.message.__probe .tctx-path');
        let n = 0;
        els.forEach(e => { if (e.offsetParent !== null) n++; });
        return n;
    }""")

    print(f'\n  paths total={total} visible-before={before} '
          f'toggles={toggles} visible-after={visible_after}')

    assert toggles >= 1, (
        'a 5-root snapshot produced no workspace "+N" toggle — the path '
        'count is unbounded, so the rail height is driven by how many '
        'roots the workspace happens to have')
    assert before < total, (
        f'all {total} paths rendered up-front ({before} visible) — the '
        f'bound is not in effect')
    assert visible_after > before, (
        f'clicking "+N" revealed no path ({before} → {visible_after}); '
        f'the hidden roots are unreachable, so the information is lost '
        f'rather than collapsed')
