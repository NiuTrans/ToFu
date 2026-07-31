"""RENDER-BUDGET INVARIANTS for the project-bar scene (static/js/tofu-scene.js).

The scene is an always-on animation living behind an ordinary UI control, so its
per-frame cost is a FEATURE REQUIREMENT, not a nice-to-have: if it regresses, it
regresses on every user, on every frame, forever, in the background of whatever
they were actually trying to do.

Three properties keep it cheap, and each is easy to silently undo with an
innocent-looking edit. So each is pinned here with a biting NEUTER:

  1. BATCHED DABS — dabs are queued by (colour, alpha bucket) and flushed as one
     path + one fill per bucket, with the rotation carried in the ellipse() args.
     The naive alternative (save/globalAlpha/fillStyle/translate/rotate/
     beginPath/ellipse/fill/restore per dab) is ~9 canvas calls and one
     rasterizer flush EACH. Guard: fills must be a small fraction of dabs.

  2. LIVE-POPULATION CAPS — live elements are re-resolved every frame, so their
     COUNT is capped (survivors widen to hold the painted area). Guard: the dab
     count must plateau with bar width. (The dab AREA is deliberately preserved —
     a wider bar has more grass — so the count, not the area, is what is bounded.)

  3. FRAME PACING + OFF-SCREEN PARK — the scene is slow ambient weather, so it
     paints on a ~30fps cadence rather than every vsync, and stops entirely
     while the bar is scrolled out of view.

  4. NO FULL-CANVAS OVERHEAD PASSES — measured in DEVICE PIXELS (area-weighted,
     the quantity the rasterizer bills, not call count): the sun glow is a baked
     tile (O(h²), width-independent) instead of a full-canvas gradient fill; the
     fg clear is confined to its painted band; the bg full-canvas clear is gone
     (the opaque blit covers it); the painting is FULL-BLEED (no canvas-side
     margin cut/clip at all — the rounded-shell crop is a single CSS clip-path).
     These were ~80% of the frame's pixel budget yet only ONE call each, so a
     call-count test can never see them.

The painting used to be torn to a deckle (handmade-paper) edge; the owner
rejected that look (2026-07-26: "the irregular outer border with a white
background behind it is not appealing"), so the canvas is now full-bleed.

Everything runs the REAL shipped module under node against a counting mock 2d
context — no re-implementation.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCENE_JS = REPO / "static" / "js" / "tofu-scene.js"

if shutil.which("node") is None:  # pragma: no cover - env gate
    pytest.skip("node not on PATH", allow_module_level=True)

pytestmark = pytest.mark.unit


# ── Harness: a mock 2d context that TALLIES both the canvas CALLS and, more
# importantly, the DEVICE-PIXEL area each operation touches, so we can measure
# the real per-frame cost of the shipped module at a chosen bar width. Call
# count is a proxy; pixel area is the bill (the rasterizer charges by area).
# It also records rAF scheduling, clip() usage and the deckle path, so pacing /
# parking / the torn outline are observable from the same run. ──
_PERF_HARNESS = r"""
'use strict';
const W = __W__, H = 48;
const DPR = 2;
const DECOR = "__DECOR__";
const OFFSCREEN = __OFFSCREEN__;

function mkCtx(rec){
  const bump = k => () => { rec[k] = (rec[k]||0)+1; rec.total++; };
  // Area is tracked in CSS px and scaled by DPR² at readout, bucketed by cost
  // class: blit (source-over image copy), clear (memset), dab (tiny fills),
  // glow (additive 'lighter' blend — the most expensive per pixel).
  const area = (a, cls) => {
    rec.area = (rec.area||0) + a;
    if (cls === 'blit') rec.areaBlit = (rec.areaBlit||0) + a;
    else if (cls === 'clear') rec.areaClear = (rec.areaClear||0) + a;
    else if (cls === 'dab') rec.areaDab = (rec.areaDab||0) + a;
    else if (cls === 'glow') rec.areaGlow = (rec.areaGlow||0) + a;
  };
  let path = null;
  let cur = { comp: 'source-over' };
  const stack = [];
  return {
    canvas:{width:W,height:H},
    setTransform:bump('setTransform'),
    clearRect(x,y,w,h){ rec.clearRect=(rec.clearRect||0)+1; rec.total++; area(w*h, 'clear'); },
    save(){ rec.save=(rec.save||0)+1; rec.total++; stack.push({comp:cur.comp}); },
    restore(){ rec.restore=(rec.restore||0)+1; rec.total++; const p=stack.pop(); if(p)cur=p; },
    translate:bump('translate'), rotate:bump('rotate'),
    beginPath(){ rec.beginPath=(rec.beginPath||0)+1; rec.total++; path=[]; },
    moveTo(x,y){ rec.moveTo=(rec.moveTo||0)+1; rec.total++; if(path)path.push([x,y]); },
    lineTo(x,y){ rec.lineTo=(rec.lineTo||0)+1; rec.total++; if(path)path.push([x,y]); },
    closePath(){ rec.total++; },
    rect(x,y,w,h){ rec.rect=(rec.rect||0)+1; rec.total++; if(path)path.push(['RECT',x,y,w,h]); },
    ellipse(x,y,rx,ry){ rec.ellipse=(rec.ellipse||0)+1; rec.total++; if(path)path.push(['E',rx,ry]); },
    arc(){ rec.total++; },
    clip(){
      rec.clip=(rec.clip||0)+1; rec.total++;
      if (path && path.length >= 3 && !path.some(p=>p[0]==='RECT')) rec.clipOutline = path.slice();
    },
    fill(rule){
      rec.fill=(rec.fill||0)+1; rec.total++;
      if (!path) return;
      if (rule === 'evenodd') {
        // deckle cut: only the perimeter margin band is actually rasterised
        rec.deckleCuts = (rec.deckleCuts||0)+1;
        rec.deckle = path.filter(p => p[0] !== 'RECT');
        rec.deckleHadRect = path.some(p => p[0] === 'RECT');
        area(2*(W+H)*4, 'clear');
      } else {
        // Only ellipse subpaths carry area; the moveTo() points that precede
        // each batched dab are 2-element [x,y] entries with no radius.
        let a = 0;
        for (const p of path){ if (p[0]==='E') a += Math.PI*p[1]*p[2]; }
        area(a, cur.comp === 'lighter' ? 'glow' : 'dab');
      }
      path = [];
    },
    fillRect(x,y,w,h){ rec.fillRect=(rec.fillRect||0)+1; rec.total++; area(w*h, cur.comp === 'lighter' ? 'glow' : 'dab'); },
    stroke:bump('stroke'),
    drawImage(img, dx, dy, dw, dh){
      rec.drawImage=(rec.drawImage||0)+1; rec.total++;
      // A bare blit of the full buffer covers the canvas; a scaled tile blit
      // covers only its own footprint. 'lighter' blits (the sun glow) are the
      // expensive additive-blend class.
      area((dw != null ? dw : W) * (dh != null ? dh : H), cur.comp === 'lighter' ? 'glow' : 'blit');
    },
    createLinearGradient(){ rec.total++; return {addColorStop(){}}; },
    createRadialGradient(){ rec.radial=(rec.radial||0)+1; rec.total++; return {addColorStop(){}}; },
    set fillStyle(v){ rec.fillStyle=(rec.fillStyle||0)+1; rec.total++; },
    get fillStyle(){ return ''; },
    set globalAlpha(v){ rec.globalAlpha=(rec.globalAlpha||0)+1; rec.total++; },
    get globalAlpha(){ return 1; },
    set globalCompositeOperation(v){ rec.gco=(rec.gco||0)+1; rec.total++; cur.comp = v; },
    get globalCompositeOperation(){ return cur.comp; },
    set strokeStyle(v){}, get strokeStyle(){ return ''; },
    set lineWidth(v){}, get lineWidth(){ return 1; },
  };
}
const vis={total:0}, buf={total:0}, fg={total:0}, glow={total:0};

let rafCb = null, rafRequests = 0;
global.requestAnimationFrame = function(cb){ rafCb = cb; rafRequests++; return rafRequests; };
global.cancelAnimationFrame = function(){};
global.devicePixelRatio = DPR;

// Capture the IntersectionObserver the module installs so we can drive it.
let ioCb = null;
global.window = {
  matchMedia(){ return {matches:false, addEventListener(){}, addListener(){}}; },
  addEventListener(){},
  ResizeObserver: function(){ return {observe(){}, disconnect(){}}; },
  MutationObserver: function(){ return {observe(){}, disconnect(){}}; },
  IntersectionObserver: function(cb){ ioCb = cb; return {observe(){}, disconnect(){}}; },
  devicePixelRatio: DPR,
};
global.ResizeObserver = global.window.ResizeObserver;
global.MutationObserver = global.window.MutationObserver;
global.IntersectionObserver = global.window.IntersectionObserver;

function mkEl(){
  return {_attrs:{}, className:'', style:{}, width:0, height:0,
    setAttribute(k,v){this._attrs[k]=v;}, getAttribute(k){return this._attrs[k];},
    appendChild(){}, insertBefore(){}, querySelector(){return null;}, firstChild:null,
    getBoundingClientRect(){return {left:0,right:W,top:0,bottom:H,width:W,height:H};}};
}
let canvasW = 0;
const bar = mkEl(); bar._attrs['data-decor'] = DECOR;
let canvasN = 0;
let canvas1 = null;
global.document = {
  readyState:'complete', hidden:false,
  documentElement:{getAttribute(k){return k==='data-theme'?'tofu':null;}},
  addEventListener(){},
  getElementById(id){ return id==='projectBar' ? bar : null; },
  createElement(t){
    if (t==='canvas'){
      canvasN++;
      // #1 visible bg, #2 baked buffer, #3 foreground occlusion, #4 glow tile
      const rec = canvasN===1?vis:(canvasN===2?buf:(canvasN===3?fg:glow));
      const e = mkEl(); e.getContext = () => mkCtx(rec);
      if (canvasN===1) canvas1 = e;
      return e;
    }
    return mkEl();
  },
};
global.window.TofuPet = { getState(){ return {x: W/2 - 16, state:'walk'}; } };

// Pin the scene clock to 14:00 ('afternoon' = the neutral bucket, zero tint
// wash) so colour-keyed assertions are deterministic whatever hour CI runs at.
{ const _RealDate = Date;
  global.Date = function(...a){ return a.length ? new _RealDate(...a) : new _RealDate(2026, 0, 1, 14, 0, 0); };
  global.Date.now = _RealDate.now; global.Date.parse = _RealDate.parse; global.Date.UTC = _RealDate.UTC;
  global.Date.prototype = _RealDate.prototype; }
__SRC__

function reset(r){ for (const k of Object.keys(r)) delete r[k]; r.total = 0; }
function devPx(r){ return Math.round((r.area||0) * DPR * DPR); }
function catPx(r, k){ return Math.round((r[k]||0) * DPR * DPR); }

let t = 100;
if (rafCb) { const cb = rafCb; rafCb = null; cb(t); }     // t0 frame
// The visible canvas' backing-store width / CSS width IS the module's render
// ratio, whatever it clamped to.
if (canvas1) canvasW = canvas1.width;

if (OFFSCREEN && ioCb) {
  ioCb([{isIntersecting:false}]);                          // bar scrolls away
  const before = rafRequests;
  // Pump whatever tick is still in flight; the loop must NOT re-arm itself.
  if (rafCb) { const cb = rafCb; rafCb = null; cb(t += 40); }
  console.log(JSON.stringify({offscreenRearmed: rafRequests > before, parked: rafCb === null}));
  process.exit(0);
}

// ── steady-state: isolate ONE painted frame and its cost breakdown ──
// Reset before each 16ms tick and record the area the frame touched; paced-out
// ticks leave area≈0, so the max over the window is one fully-painted frame.
reset(vis); reset(fg);
let painted = 0, ticks = 0;
let frame = { bg: 0, fg: 0, blit: 0, clear: 0, dab: 0, glow: 0 };
for (let i = 0; i < 14; i++) {
  t += 16; ticks++;
  if (!rafCb) break;
  vis.area = 0; fg.area = 0; vis.areaBlit = 0; vis.areaClear = 0; vis.areaDab = 0; vis.areaGlow = 0;
  fg.areaClear = 0; fg.areaDab = 0;
  const before = vis.total;
  const cb = rafCb; rafCb = null; cb(t);
  if (vis.total > before) {
    painted++;
    if (vis.area > frame.bg) {
      frame = { bg: vis.area, fg: fg.area,
                blit: vis.areaBlit, clear: (vis.areaClear||0) + (fg.areaClear||0),
                dab: (vis.areaDab||0) + (fg.areaDab||0), glow: vis.areaGlow||0 };
    }
  }
}
console.log(JSON.stringify({
  bg: vis, fg: fg, buf: buf, glow: glow,
  callsPerFrame: vis.total + fg.total,
  // The module CLAMPS its render ratio (SCENE_DPR_CAP) below the device ratio,
  // so the real pixel bill uses the clamped value — read it off the canvas the
  // module actually sized rather than assuming DPR.
  renderDpr: (canvasW > 0 ? canvasW / W : DPR),
  frame: (function(){ var d = (canvasW > 0 ? canvasW / W : DPR), s = d*d;
    return { bg: Math.round(frame.bg*s), fg: Math.round(frame.fg*s),
             blit: Math.round(frame.blit*s), clear: Math.round(frame.clear*s),
             dab: Math.round(frame.dab*s), glow: Math.round(frame.glow*s) }; })(),
  ticks: ticks, painted: painted,
}));
process.exit(0);
"""


def _run_perf(width=900, decor="meadow", offscreen=False, src=None):
    src = src if src is not None else SCENE_JS.read_text()
    script = (_PERF_HARNESS
              .replace("__SRC__", src)
              .replace("__W__", str(width))
              .replace("__DECOR__", decor)
              .replace("__OFFSCREEN__", "true" if offscreen else "false"))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         cwd=str(REPO), timeout=30)
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(line)


# ══════════════════════════════════════════════════════════════════════════
#  1. BATCHED DABS
# ══════════════════════════════════════════════════════════════════════════

def test_dabs_are_batched_into_few_fills():
    """The dominant per-frame cost is one rasterizer flush per fill(). Dabs are
    bucketed by (colour, alpha) and flushed as ONE path per bucket, so a scene
    painting hundreds of dabs must issue only DOZENS of fills. A regression to
    fill-per-dab shows up here as a ~1:1 ratio."""
    r = _run_perf()
    for plane in ("bg", "fg"):
        dabs = r[plane].get("ellipse", 0)
        fills = r[plane].get("fill", 0)
        assert dabs > 100, f"{plane}: too few dabs to be a meaningful check ({dabs})"
        assert fills * 4 <= dabs, (
            f"{plane}: dabs are not batched — {fills} fills for {dabs} dabs. "
            f"Each fill is a rasterizer flush; batching must collapse them.")


def test_no_per_dab_transform_churn():
    """Rotation rides in the ellipse() args, so the per-dab
    save/translate/rotate/restore quartet must be GONE. A handful of
    save/restore pairs remain for the composite-op scopes (sun glow, critter) —
    but they must not scale with the dab count."""
    r = _run_perf()
    dabs = r["bg"].get("ellipse", 0) + r["fg"].get("ellipse", 0)
    churn = sum(r[p].get(k, 0) for p in ("bg", "fg")
                for k in ("translate", "rotate", "save", "restore"))
    assert dabs > 100
    assert churn < dabs / 10, (
        f"per-dab transform churn is back: {churn} transform calls for {dabs} dabs "
        f"(expected a fixed handful of composite scopes, not one set per dab)")


def test_NEUTER_unbatched_dabs_is_caught():
    """NEUTER: flush the queue after EVERY enqueued dab (i.e. one fill per dab,
    the pre-optimization behaviour) → the batching ratio assertion must fail."""
    src = SCENE_JS.read_text()
    neut = src.replace("    arr.push(x, y, len, wid, ang);",
                       "    arr.push(x, y, len, wid, ang);\n    flushDabs();  /* NEUTER: unbatched */", 1)
    assert neut != src, "neuter did not match the dab enqueue"
    r = _run_perf(src=neut)
    dabs = r["bg"].get("ellipse", 0)
    fills = r["bg"].get("fill", 0)
    assert fills * 4 > dabs, (
        f"neutered (unbatched) build still looked batched: {fills} fills / {dabs} dabs "
        f"— the batching guard would not bite")


# ══════════════════════════════════════════════════════════════════════════
#  2. WIDTH PLATEAU — per-frame cost must not scale with the bar's width
# ══════════════════════════════════════════════════════════════════════════

def test_scene_render_dpr_is_capped():
    """The scene renders at a CAPPED device-pixel ratio, not the panel's full
    ratio. Every pixel cost in the renderer scales with dpr², so a Retina panel
    would otherwise cost 4× (and some phones 9×) for detail this art style
    cannot use: Monet broken colour is thousands of soft translucent ellipses
    with no hard edges and no text — there is no high-frequency detail for the
    extra samples to resolve, and the torn deckle rim reads BETTER soft.

    This is the single cheapest win in the renderer and the most likely thing a
    future 'the canvas looks blurry, bump it to 2' commit will innocently
    revert, so it is pinned here. It is also asserted to be SCENE-SCOPED: the
    pet sprite and the bar's labels are DOM (not canvas), so nothing legible is
    softened by it."""
    src = SCENE_JS.read_text()
    m = re.search(r"var SCENE_DPR_CAP = ([0-9.]+);", src)
    assert m, "SCENE_DPR_CAP is gone — the scene renders at full device ratio again"
    cap = float(m.group(1))
    assert cap <= 1.5, (
        f"the scene DPR cap was raised to {cap} — that multiplies EVERY pixel "
        f"cost in the renderer by (cap/1.5)². If a real visual defect motivated "
        f"this, fix the defect, don't buy resolution the art style cannot use.")
    # it must actually be WIRED to the resize path, not just declared
    assert "Math.min(SCENE_DPR_CAP, window.devicePixelRatio" in src, (
        "SCENE_DPR_CAP is declared but no longer clamps _dpr in _resize()")
    # and it must be scene-scoped: nothing legible is drawn on these canvases.
    # Strip comments first — this file DISCUSSES fillText in the rationale above.
    code = re.sub(r"//[^\n]*", "", src)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    assert "fillText" not in code and "strokeText" not in code, (
        "the scene canvas now draws TEXT — a capped DPR would make it fuzzy; "
        "either move the text to DOM or reconsider the cap")


def test_capped_dpr_actually_lowers_the_pixel_bill():
    """Behavioural proof, not just a constant check: with the harness reporting
    devicePixelRatio=2, the frame's measured device-pixel cost must reflect the
    1.5 cap (2.25× a CSS pixel), not 2's 4×. Guards against the cap being
    declared, wired, and then defeated by something else re-reading
    window.devicePixelRatio directly."""
    r = _run_perf(width=900)
    dpr = r["renderDpr"]
    assert dpr <= 1.5 + 1e-6, (
        f"the scene sized its canvas at dpr {dpr} despite the 1.5 cap — something "
        f"on the real render path re-reads window.devicePixelRatio directly")
    blit = r["frame"]["blit"]
    css_area = 900 * 48
    ratio = blit / float(css_area)
    assert ratio < 3.0, (
        f"the frame is rendering at dpr²≈{ratio:.2f} (≈dpr {ratio ** 0.5:.2f}) — "
        f"the 1.5 cap (dpr²=2.25) is not taking effect on the real render path")


def test_sun_glow_cost_is_width_independent():
    """The additive sun glow used to be a full-canvas radial fill every frame —
    its pixel cost grew with the bar's width. Baked to a tile and blitted at its
    own 2R×2R footprint (R = h*1.6), the glow's additive-blend area is now
    O(h²): constant in the bar's width. A 2400px bar must spend ~the same glow
    pixels as a 900px one."""
    n = _run_perf(width=900)["frame"]["glow"]
    wd = _run_perf(width=2400)["frame"]["glow"]
    assert n > 0 and wd > 0, f"no glow work measured: {n} {wd}"
    assert wd <= n * 1.15, (
        f"the sun glow's pixel cost scales with width again (900px={n} devPx vs "
        f"2400px={wd} devPx) — it is back to a full-canvas fill, not a tile blit")


def test_no_full_canvas_overhead_pass_per_frame():
    """The removed overheads were all FULL-CANVAS passes that scaled with width
    while adding nothing per pixel: a bg clear (the blit already covers), a
    full-canvas additive glow fill (now a tile), and a deckle cut/clip (now
    GONE entirely — the painting is full-bleed). Assert none is present in a
    steady frame:
      * the bg does NOT clear at all (its areaClear is 0);
      * the fg clears only its painted BAND, so its clear area is strictly less
        than one full canvas (w*h), never a full-canvas wipe;
      * no even-odd destination-out cut and no deckle clip ANYWHERE (buffer or
        live) — the margin-cutting machinery was removed with the torn edge.
    The dab pixel AREA is intentionally NOT asserted to plateau — the live-
    population budget widens surviving strokes to preserve painted area, so the
    grass on a wider bar is content, not overhead. What must NOT scale is the
    fixed full-canvas passes."""
    r = _run_perf(width=900)
    f = r["frame"]
    # bg full-canvas clear is gone (steady state: the opaque blit covers it)
    assert r["bg"].get("clearRect", 0) == 0, (
        f"bg still clears the full canvas in steady state ({r['bg'].get('clearRect')} "
        f"clearRect calls)")
    # fg clear is confined to its band → strictly less than a full canvas
    clear_css = f["clear"] / 4.0          # devPx → CSS px (DPR=2)
    full_css = 900 * 48
    assert clear_css < full_css, (
        f"the clear area {clear_css:.0f} CSS px² is a FULL canvas wipe "
        f"({full_css}) — the band-confined clear regressed")
    # no destination-out cut ANYWHERE (the deckle cut was removed, not moved)
    for plane in ("bg", "fg", "buf"):
        assert r[plane].get("deckleCuts", 0) == 0, (
            f"{plane} is still paying a full-canvas destination-out cut — the "
            f"torn deckle edge was removed; the painting must be full-bleed")
    # and no deckle clip on the live canvases either (that machinery is gone)
    assert r["bg"].get("clip", 0) == 0 and r["fg"].get("clip", 0) == 0, (
        "a live canvas is still clipping to the removed deckle outline every "
        "frame — the full-bleed painting must not clip at all in JS")


def test_narrow_bar_is_not_thinned_by_the_caps():
    """The caps must sit ABOVE a normal bar's natural population, so a typical
    project bar is painted at full density and the optimization is invisible.
    A 360px bar must still paint a rich field."""
    r = _run_perf(width=360)
    assert r["bg"].get("ellipse", 0) >= 150, \
        f"a normal-width bar lost density: only {r['bg'].get('ellipse')} bg dabs"
    assert r["fg"].get("ellipse", 0) >= 100, \
        f"the near plane thinned out: only {r['fg'].get('ellipse')} fg dabs"


def test_NEUTER_uncapped_live_population_is_caught():
    """NEUTER: remove the live-population cap (let _budget always return the
    wanted count) → the dab COUNT (ellipses drawn) resumes scaling with width.
    (The dab AREA is intentionally preserved by the widened-survivor budget even
    when capped, so the count — not the area — is what the cap actually bounds.)"""
    src = SCENE_JS.read_text()
    neut = src.replace(
        "    if (wanted <= cap) return { n: wanted, scale: 1 };",
        "    return { n: wanted, scale: 1 };  /* NEUTER: uncapped */\n"
        "    if (wanted <= cap) return { n: wanted, scale: 1 };", 1)
    assert neut != src, "neuter did not match the _budget cap"
    nDabs = _run_perf(width=900, src=neut)["bg"].get("ellipse", 0) + \
            _run_perf(width=900, src=neut)["fg"].get("ellipse", 0)
    wBg = _run_perf(width=2400, src=neut)
    wDabs = wBg["bg"].get("ellipse", 0) + wBg["fg"].get("ellipse", 0)
    assert wDabs > nDabs * 1.3, (
        f"neutered (uncapped) build's dab count still plateaued (900px={nDabs}, "
        f"2400px={wDabs}) — the population cap guard would not bite")


# ══════════════════════════════════════════════════════════════════════════
#  3. FRAME PACING + OFF-SCREEN PARK
# ══════════════════════════════════════════════════════════════════════════

def test_loop_paces_below_display_refresh():
    """The scene is slow ambient weather; painting it every vsync spent half the
    work on frames the eye cannot separate. Driven at 60Hz (16ms ticks), it must
    PAINT on only a fraction of them."""
    r = _run_perf()
    assert r["ticks"] >= 10, f"harness did not deliver enough ticks: {r['ticks']}"
    assert r["painted"] < r["ticks"], (
        f"the loop painted on every one of {r['ticks']} 60Hz ticks — frame pacing "
        f"is not in effect")
    assert r["painted"] >= 2, (
        f"the loop barely painted ({r['painted']}/{r['ticks']}) — pacing must slow "
        f"the scene, not freeze it")


def test_NEUTER_unpaced_loop_is_caught():
    """NEUTER: paint on every tick (pace interval 0) → the pacing assertion must
    fail, proving it is the pacing gate being measured and not tick starvation."""
    src = SCENE_JS.read_text()
    neut = src.replace("var SCENE_FRAME_MS = 1000 / SCENE_FPS - 1;",
                       "var SCENE_FRAME_MS = 0;  /* NEUTER: unpaced */", 1)
    assert neut != src, "neuter did not match the pacing constant"
    r = _run_perf(src=neut)
    assert r["painted"] == r["ticks"], (
        f"neutered (unpaced) build still skipped frames ({r['painted']}/{r['ticks']}) "
        f"— the pacing guard would not bite")


def test_loop_parks_when_the_bar_scrolls_out_of_view():
    """An off-screen painting is invisible work. When the IntersectionObserver
    reports the bar has left the viewport the rAF chain must stop entirely — not
    merely skip painting, which would still wake per vsync."""
    r = _run_perf(offscreen=True)
    assert r["offscreenRearmed"] is False, \
        "the loop re-armed rAF after the bar went off-screen — it never parks"


# ══════════════════════════════════════════════════════════════════════════
#  3b. THE FULL-CANVAS PASSES — the 80% the call count never saw
# ══════════════════════════════════════════════════════════════════════════

def test_sun_glow_is_baked_not_rebuilt_per_frame():
    """The drifting sun used to be a `createRadialGradient` rebuilt EVERY frame
    and fillRect-ed across the whole canvas under 'lighter' — a w×h additive
    blend plus a gradient-object rebuild, to move the sun 0.6px. It is now baked
    to a tile ONCE and blitted at the sun's position. Per frame, the VISIBLE
    canvas must do ZERO radial-gradient builds and ZERO full-canvas gradient
    fills; the glow's pixel footprint must be the tile's, not the bar's."""
    r = _run_perf()
    assert r["bg"].get("radial", 0) == 0, (
        f"the sun gradient is still rebuilt per frame ({r['bg'].get('radial')} "
        f"createRadialGradient calls in one frame)")
    # The tile is blitted, so the glow shows up as a bounded drawImage, and the
    # bar must NOT do a full-canvas additive fillRect for the sun any more.
    assert r["bg"].get("drawImage", 0) >= 2, (
        "expected the buffer blit AND the glow-tile blit — the glow is not being "
        "drawn from its baked tile")
    # the glow tile exists and is small (2R×2R, R = h*1.6 ≈ 77px → ~24k CSS px²),
    # NOT the whole canvas (w*h = 43k+ CSS px² and growing with width)
    assert r["glow"].get("radial", 0) >= 1, "the glow tile was never baked"


def test_foreground_clear_is_confined_to_its_painted_band():
    """The near plane is rooted at the bottom and reaches up into the pet's
    ankles — measured ~29% of a 48px bar — but it was cleared full-height every
    frame, which (with the bg clear) was the single largest pixel cost. The
    clear must be confined to the band the plane actually paints."""
    # Direct assertion on the clear call: the module now calls
    # clearRect(0, _fgTop, w, h-_fgTop+2), so the cleared height is the band, not
    # the full canvas. If this reverts to clearRect(0,0,w,h) the largest single
    # pixel cost is back.
    assert "c.clearRect(0, _fgTop, w, h - _fgTop + 2)" in SCENE_JS.read_text(), (
        "the foreground clear is no longer band-confined (clearRect signature "
        "reverted to full-height) — the largest single pixel cost is back")


def test_no_fullcanvas_clear_in_steady_state():
    """The baked buffer is opaque over the whole canvas (full-bleed), so
    blitting it each frame already overwrites the previous overlay — a
    full-canvas clearRect in steady state is pure waste. One full clear is
    allowed ONLY on the first frame after a re-bake (the geometry changed);
    subsequent frames must not clear the whole canvas."""
    r = _run_perf()
    # steady state (after the t0 frame which absorbs the one-shot clear):
    # bg must not issue a full-canvas clearRect
    assert r["bg"].get("clearRect", 0) == 0, (
        f"the bg still clears the full canvas in steady state "
        f"({r['bg'].get('clearRect')} clearRect calls) — the blit already covers it")


def test_NEUTER_no_offscreen_park_is_caught():
    """NEUTER: ignore the off-screen flag in the loop gate → the loop keeps
    re-arming while invisible and the park assertion must fail."""
    src = SCENE_JS.read_text()
    neut = src.replace("    if (_offscreen) return;                       "
                       "// bar not on screen → nothing to paint",
                       "    /* NEUTER: no offscreen park */", 1)
    assert neut != src, "neuter did not match the offscreen gate in _loop"
    neut2 = neut.replace(
        "    if (_offscreen) { if (_raf) { cancelAnimationFrame(_raf); _raf = 0; } return; }",
        "    /* NEUTER: no offscreen park */", 1)
    assert neut2 != neut, "neuter did not match the offscreen gate in _ensureLoop"
    r = _run_perf(offscreen=True, src=neut2)
    assert r["offscreenRearmed"] is True, \
        "neutered build still parked off-screen — the park guard would not bite"


# ══════════════════════════════════════════════════════════════════════════
#  4. FULL-BLEED PAINTING — the torn deckle edge was removed (owner, 2026-07-26)
# ══════════════════════════════════════════════════════════════════════════

def test_painting_is_full_bleed_no_torn_edge():
    """OWNER, 2026-07-26: 'the irregular outer border shape with a white
    background behind it is not very appealing.' The deckle edge (a torn
    silhouette + the bar's cream body showing through as a white margin) is
    REMOVED — the painting must fill the canvas edge to edge, with the rounded
    crop done once in CSS. So the source must carry NONE of the torn-edge
    machinery, and the steady frame must do no deckle clip/cut at all."""
    src = SCENE_JS.read_text()
    for gone in ("_seedDeckle", "_cutDeckle", "_paintDeckleRim", "_clipDeckle",
                 "DECKLE_BITE", "_deckle"):
        assert gone not in src, (
            f"the torn deckle edge is partially back ({gone}) — the painting "
            f"must be full-bleed, cropped only by the CSS rounded-shell clip")
    r = _run_perf(width=900)
    assert r["bg"].get("clip", 0) == 0, "bg still clips to the removed deckle outline"
    assert r["fg"].get("clip", 0) == 0, "fg still clips to the removed deckle outline"
    for plane in ("bg", "fg", "buf"):
        assert r[plane].get("deckleCuts", 0) == 0, \
            f"{plane} still pays a deckle destination-out cut — the edge was removed"


# ══════════════════════════════════════════════════════════════════════════
#  5. THE PET's per-frame cost (static/js/tofu-pet.js)
# ══════════════════════════════════════════════════════════════════════════

PET_JS = REPO / "static" / "js" / "tofu-pet.js"


def test_pet_does_not_write_a_dead_parallax_property_every_frame():
    """The pet's _place() runs on every rAF tick. It used to write
    `--bar-scene-x` on .project-bar unconditionally — but the ONLY rule reading
    that property is the SVG ground (::after) `background-position`, and that
    ::after is set to display:none whenever the canvas is live (the normal case,
    gated on data-scene-canvas="on").

    So the write was invalidating the bar's custom-property inheritance every
    frame to scroll the background of a box that is not rendered. It must be
    gated on the SVG ground actually being the thing on screen. The property is
    still maintained for the no-JS / no-canvas fallback, which is its only real
    reader."""
    src = PET_JS.read_text()
    m = re.search(r"function _place\(\)\s*\{(.*?)\n  \}", src, re.S)
    assert m, "could not isolate _place()"
    body = m.group(1)
    assert "--bar-scene-x" in body, "the parallax property write vanished entirely"
    assert "data-scene-canvas" in body, (
        "_place() writes --bar-scene-x unconditionally again — that is a per-frame "
        "style invalidation feeding a display:none rule whenever the canvas is live")
    # and the CSS half must still be the thing that makes it dead
    css = (REPO / "static" / "styles.css").read_text()
    assert 'data-scene-canvas="on"]::after{display:none}' in css.replace(" ", ""), (
        "the canvas-wins gate that makes the SVG ground (and thus --bar-scene-x) "
        "dead is gone — re-check whether the _place() gate is still correct")


def test_pet_light_writes_are_deduped():
    """_applyLight() runs every rAF tick (deliberately — it must keep working
    during a drag). It must only touch the DOM when a ROUNDED light value
    actually changed, else it writes four custom properties per frame forever."""
    src = PET_JS.read_text()
    m = re.search(r"function _applyLight\(\)\s*\{(.*?)\n  \}", src, re.S)
    assert m, "could not isolate _applyLight()"
    body = m.group(1)
    assert "_lastLightK" in body and "if (k === _lastLightK) return;" in body, (
        "_applyLight lost its change-detection guard — it now writes 4 custom "
        "properties on every animation frame")
