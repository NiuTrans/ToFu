"""Procedural Impressionist canvas backdrop (static/js/tofu-scene.js).

The project-bar background used to be a flat SVG tile repeated across the width
("a simple illustration that extends endlessly"). tofu-scene.js replaces that
look with an asset-free <canvas> painter: thousands of oriented brush-dabs in
depth planes (Monet broken colour) baked into an offscreen buffer, plus a
slow-drifting sun glow + specular shimmer overlay each animation frame.

These tests run the REAL shipped module under node with a minimal DOM + a
recording mock 2d context, and assert:
  * mounting paints an OPAQUE base wash (a full-canvas fillRect) + MANY brush
    dabs (the painterly look, not a flat fill) into the offscreen buffer;
  * switching scenes re-bakes with a different palette;
  * the reduced-motion / off-scene / non-tofu-theme gates keep it STATIC (no
    rAF loop) — the standing energy/accessibility prefs;
  * it is registered in the JS bundler manifest + index.html dev fallback, and
    the CSS mounts the canvas at z0 (below the pet + controls).
A biting NEUTER removes the dab-drawing loop and proves the "is painterly"
assertion catches a flat-fill regression.
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCENE_JS = REPO / "static" / "js" / "tofu-scene.js"
BUNDLER = REPO / "lib" / "js_bundler.py"
INDEX = REPO / "index.html"
CSS = REPO / "static" / "styles.css"


# ── node harness: a recording 2d context counts fillRect (base wash) + ellipse
# (brush dabs) calls, freezes rAF (we pump it manually), and lets the test set
# the theme / reduced-motion / scene. Returns a JSON summary of what was painted
# and whether the animation loop is running. ──
_HARNESS = r"""
'use strict';
const REDUCED = __REDUCED__;
const THEME = "__THEME__";
const DECOR = "__DECOR__";

// recording 2d context — counts the painterly primitives
function mkCtx(rec){
  return {
    canvas: { width: 400, height: 48 },
    setTransform(){}, clearRect(){}, save(){}, restore(){}, translate(){},
    rotate(){}, beginPath(){}, fill(){}, fillRect(){ rec.fillRect++; },
    ellipse(){ rec.ellipse++; }, drawImage(){ rec.drawImage++; },
    createLinearGradient(){ return { addColorStop(){} }; },
    createRadialGradient(){ return { addColorStop(){} }; },
    set fillStyle(v){}, get fillStyle(){ return ''; },
    set globalAlpha(v){}, get globalAlpha(){ return 1; },
    set globalCompositeOperation(v){}, get globalCompositeOperation(){ return ''; },
  };
}
const bufRec = { fillRect:0, ellipse:0, drawImage:0 };
const visRec = { fillRect:0, ellipse:0, drawImage:0 };

let _rafCbs = [];
global.requestAnimationFrame = function(cb){ _rafCbs.push(cb); return _rafCbs.length; };
global.cancelAnimationFrame = function(){ };
global.devicePixelRatio = 2;

global.window = {
  matchMedia(){ return { matches: REDUCED, addEventListener(){}, addListener(){} }; },
  addEventListener(){},
  ResizeObserver: function(){ return { observe(){}, disconnect(){} }; },
  MutationObserver: function(){ return { observe(){}, disconnect(){} }; },
  devicePixelRatio: 2,
};
global.requestAnimationFrame = global.requestAnimationFrame;
global.ResizeObserver = global.window.ResizeObserver;
global.MutationObserver = global.window.MutationObserver;

function mkEl(){
  return { _attrs:{}, className:'', style:{}, width:0, height:0,
    setAttribute(k,v){ this._attrs[k]=v; }, getAttribute(k){ return this._attrs[k]; },
    appendChild(){}, insertBefore(){}, querySelector(){ return null; },
    firstChild:null,
    getBoundingClientRect(){ return {left:0,right:400,top:0,bottom:48,width:400,height:48}; },
  };
}
// the project bar carries the data-decor attr
const _bar = mkEl();
_bar._attrs['data-decor'] = DECOR;
_bar.getContext = undefined;

let _canvasN = 0;
global.document = {
  readyState:'complete', hidden:false,
  documentElement: { getAttribute(k){ return k==='data-theme' ? THEME : null; } },
  addEventListener(){},
  getElementById(id){ return id==='projectBar' ? _bar : null; },
  createElement(t){
    if (t === 'canvas'){
      _canvasN++;
      // first canvas created = the visible one (inserted into the bar);
      // second = the offscreen buffer.
      const isBuf = _canvasN >= 2;
      const rec = isBuf ? bufRec : visRec;
      const e = mkEl();
      e.getContext = function(){ return mkCtx(rec); };
      return e;
    }
    return mkEl();
  },
};
global.cancelAnimationFrame = global.cancelAnimationFrame;

__SRC__
const TS = window.TofuScene;
// pump one animation frame if the loop scheduled one
const running = _rafCbs.length > 0;
if (_rafCbs.length){ const cb=_rafCbs.shift(); cb(16); }
console.log(JSON.stringify({
  scene: TS.getScene(),
  scenes: TS.SCENES,
  bufFillRect: bufRec.fillRect,
  bufEllipse: bufRec.ellipse,
  visDrawImage: visRec.drawImage,
  loopRunning: running,
  sceneCanvasMarker: _bar.getAttribute('data-scene-canvas') || null,
}));
process.exit(0);
"""


def _run(theme="tofu", decor="meadow", reduced="false", src=None):
    import json
    src = src if src is not None else SCENE_JS.read_text()
    script = (_HARNESS
              .replace("__SRC__", src)
              .replace("__THEME__", theme)
              .replace("__DECOR__", decor)
              .replace("__REDUCED__", reduced))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         cwd=str(REPO), timeout=20)
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(line)


import json  # noqa: E402


def test_mount_paints_opaque_base_and_many_dabs():
    """Mounting on the tofu theme must bake the static scene: an opaque base
    wash (fillRect) PLUS many oriented brush-dabs (ellipse) — the painterly
    Impressionist look, not a flat fill."""
    r = _run(theme="tofu", decor="meadow")
    assert r["scene"] == "meadow"
    assert r["bufFillRect"] >= 1, f"no base wash painted: {r}"
    assert r["bufEllipse"] >= 60, \
        f"scene is not painterly — only {r['bufEllipse']} dabs (expected many)"


def test_animation_loop_runs_when_active():
    """On the tofu theme with a real scene and motion allowed, the living
    overlay loop must run (rAF scheduled) and blit the baked buffer."""
    r = _run(theme="tofu", decor="meadow", reduced="false")
    assert r["loopRunning"] is True, f"animation loop did not start: {r}"
    assert r["visDrawImage"] >= 1, f"frame did not blit the baked buffer: {r}"


def test_reduced_motion_is_static_no_loop():
    """prefers-reduced-motion → the scene is painted ONCE, no rAF loop
    (standing energy/accessibility pref)."""
    r = _run(theme="tofu", decor="meadow", reduced="true")
    assert r["loopRunning"] is False, f"reduced-motion still ran a loop: {r}"
    assert r["bufEllipse"] >= 60, "reduced-motion must still paint the static scene"


def test_scene_off_paints_nothing_and_no_loop():
    """decor 'off' → no painting, no loop."""
    r = _run(theme="tofu", decor="off")
    assert r["scene"] == "off"
    assert r["loopRunning"] is False
    assert r["bufEllipse"] == 0, f"'off' should paint no dabs: {r}"


def test_non_tofu_theme_does_not_animate():
    """The canvas is a tofu-theme feature — on another theme the loop must not
    run (CSS also hides it), so it never burns cycles elsewhere."""
    r = _run(theme="dark", decor="meadow")
    assert r["loopRunning"] is False, f"loop ran off-theme: {r}"


def test_every_scene_paints_dabs():
    """Each of the three scenes must produce a dense painterly bake."""
    for s in ("meadow", "pool", "sky"):
        r = _run(theme="tofu", decor=s)
        assert r["scene"] == s
        assert r["bufEllipse"] >= 60, f"scene {s} not painterly: {r}"


def test_NEUTER_flat_fill_is_caught():
    """NEUTER: remove the dab-drawing call inside the layer loop → the buffer is
    a flat gradient fill with NO brush-dabs. The 'is painterly' assertion must
    then fail (proving it bites)."""
    src = SCENE_JS.read_text()
    neut = src.replace("dab(b, x, y, len, wid, ang, color, alpha);",
                       "/* neutered dab */", 1)
    assert neut != src, "neuter did not match the buffer dab call"
    r = _run(theme="tofu", decor="meadow", src=neut)
    assert r["bufFillRect"] >= 1, "base wash should still paint"
    assert r["bufEllipse"] == 0, \
        f"neutered build still drew dabs ({r['bufEllipse']}) — test does not bite"


def test_mount_stamps_scene_canvas_marker():
    """A successful mount must stamp data-scene-canvas="on" on the bar — this is
    the runtime signal the CSS uses to suppress the SVG ground so the canvas is
    the visible layer (fixing the equal-z0 ::after-paints-on-top collision)."""
    r = _run(theme="tofu", decor="meadow")
    assert r["sceneCanvasMarker"] == "on", \
        f"mount did not stamp the canvas marker: {r}"


def test_no_2d_context_leaves_marker_off():
    """If getContext('2d') yields no context (no-canvas browser), mount must
    return before stamping the marker, so the SVG fallback stays visible."""
    src = SCENE_JS.read_text()
    # force the 2d context to be null → mount bails at the `if (!_ctx)` guard
    neut = src.replace("_canvas.getContext('2d')", "null", 1)
    assert neut != src, "could not neuter the 2d-context acquisition"
    r = _run(theme="tofu", decor="meadow", src=neut)
    assert r["sceneCanvasMarker"] is None, \
        f"marker was stamped despite no 2d context (SVG fallback would be hidden): {r}"
    assert r["bufEllipse"] == 0, "no-context mount should paint nothing"


def test_css_gate_hides_svg_ground_when_canvas_live():
    """The CSS must hide BOTH the SVG scene GROUND (::after) and the SVG top-edge
    CREST (::before) under the runtime marker, so the canvas experience is a
    clean gallery-framed rectangle (no equal-z0 occlusion, no hard clip-art
    garland on the rim) — WITHOUT deleting the base SVG rules (they remain the
    no-canvas fallback)."""
    css = CSS.read_text()
    # the ground gate rule: marker → ::after display:none
    m = re.search(
        r'\[data-theme="tofu"\]\s*\.project-bar\[data-scene-canvas="on"\]::after\s*\{\s*display:\s*none\s*\}',
        css)
    assert m, "no rule hides ::after under [data-scene-canvas=on] — canvas would be occluded"
    # the crest gate rule: marker → ::before display:none
    mc = re.search(
        r'\[data-theme="tofu"\]\s*\.project-bar\[data-scene-canvas="on"\]::before\s*\{\s*display:\s*none\s*\}',
        css)
    assert mc, "no rule hides ::before under [data-scene-canvas=on] — the hard SVG crest still pokes out"
    # the base SVG rules MUST still exist (they're the fallback, not deleted)
    assert re.search(r'\[data-theme="tofu"\]\s*\.project-bar::after\s*\{', css), \
        "base SVG ::after ground rule was deleted — the no-canvas fallback is gone"
    assert re.search(r'\[data-theme="tofu"\]\s*\.project-bar::before\s*\{', css), \
        "base SVG ::before crest rule was deleted — the no-canvas fallback is gone"


def test_NEUTER_missing_css_gate_is_caught():
    """NEUTER: strip the gate rule from the CSS text → the gate test's core
    assertion must fail, proving 'canvas replaces the SVG ground' is guaranteed
    by the CSS, not merely assumed."""
    css = CSS.read_text()
    neut = re.sub(
        r'\[data-theme="tofu"\]\s*\.project-bar\[data-scene-canvas="on"\]::after\s*\{\s*display:\s*none\s*\}',
        '', css, count=1)
    assert neut != css, "neuter did not remove the gate rule"
    present = bool(re.search(
        r'\[data-theme="tofu"\]\s*\.project-bar\[data-scene-canvas="on"\]::after\s*\{\s*display:\s*none\s*\}',
        neut))
    assert present is False, "gate rule survived the neuter — test would not bite"


def test_NEUTER_missing_crest_gate_is_caught():
    """NEUTER: strip the ::before crest gate → the crest-hide assertion must
    fail, proving 'the hard SVG garland is removed under the canvas' is
    guaranteed by CSS, not assumed."""
    css = CSS.read_text()
    neut = re.sub(
        r'\[data-theme="tofu"\]\s*\.project-bar\[data-scene-canvas="on"\]::before\s*\{\s*display:\s*none\s*\}',
        '', css, count=1)
    assert neut != css, "neuter did not remove the crest gate rule"
    present = bool(re.search(
        r'\[data-theme="tofu"\]\s*\.project-bar\[data-scene-canvas="on"\]::before\s*\{\s*display:\s*none\s*\}',
        neut))
    assert present is False, "crest gate rule survived the neuter — test would not bite"


def test_scene_exposes_critter_and_spook_for_pet_interaction():
    """PET ⋈ SCENE seam: the scene must spawn a drifting critter and expose its
    x (TofuScene.critterX) + a spook() hook so the pet can chase it. On a live
    tofu scene critterX() is a number; spook() must not throw. Under a non-tofu
    theme (loop parked) critterX() returns null so an off-scene pet never
    chases a frozen critter."""
    src = SCENE_JS.read_text()
    assert "critterX" in src and "spook" in src, "critter interaction seam missing"
    # exercise the real surface through the harness by appending a probe
    probe = r"""
const R = { hasCritterFn: typeof window.TofuScene.critterX === 'function',
            hasSpookFn: typeof window.TofuScene.spook === 'function' };
try { window.TofuScene.spook(); R.spookThrew = false; } catch(e){ R.spookThrew = true; }
R.critterX = window.TofuScene.critterX();
console.log('PROBE ' + JSON.stringify(R));
"""
    import subprocess as _sp
    script = (_HARNESS.replace("__SRC__", src + probe)
              .replace("__THEME__", "tofu").replace("__DECOR__", "meadow").replace("__REDUCED__", "false"))
    out = _sp.run(["node", "-e", script], capture_output=True, text=True, cwd=str(REPO), timeout=20)
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    probe_line = [ln for ln in out.stdout.splitlines() if ln.startswith("PROBE ")][-1]
    R = json.loads(probe_line[len("PROBE "):])
    assert R["hasCritterFn"] and R["hasSpookFn"], "critterX/spook not exposed"
    assert R["spookThrew"] is False, "spook() threw"
    assert isinstance(R["critterX"], (int, float)), f"live scene critterX must be a number, got {R['critterX']}"


# ── PET ⋈ SCENE WAKE + GLOW-DIM harness ─────────────────────────────────────
# A second harness that (a) can install a MOVABLE window.TofuPet before pumping
# frames, and (b) pumps SEVERAL frames advancing the pet's foot, so the wake
# spawner (which only fires once the foot has travelled WAKE_STEP_PX) actually
# runs. The recording context also captures every RADIAL-gradient colour-stop
# on the VISIBLE canvas so we can assert the sun/pet glows were DIMMED. ──
_WAKE_HARNESS = r"""
'use strict';
const THEME = "tofu";
const DECOR = "__DECOR__";
const PET_MODE = "__PET__";   // 'moving' | 'none' | 'drag'

function mkCtx(rec){
  return {
    canvas: { width: 400, height: 48 },
    setTransform(){}, clearRect(){}, save(){}, restore(){}, translate(){},
    rotate(){}, beginPath(){}, fill(){}, fillRect(){ rec.fillRect++; },
    ellipse(){ rec.ellipse++; }, stroke(){ rec.stroke++; }, drawImage(){ rec.drawImage++; },
    createLinearGradient(){ return { addColorStop(){} }; },
    createRadialGradient(){ return { addColorStop(s,col){ rec.radialStops.push(col); } }; },
    set fillStyle(v){}, get fillStyle(){ return ''; },
    set strokeStyle(v){}, get strokeStyle(){ return ''; },
    set lineWidth(v){}, get lineWidth(){ return 1; },
    set globalAlpha(v){}, get globalAlpha(){ return 1; },
    set globalCompositeOperation(v){}, get globalCompositeOperation(){ return ''; },
  };
}
const bufRec = { fillRect:0, ellipse:0, stroke:0, drawImage:0, radialStops:[] };
const visRec = { fillRect:0, ellipse:0, stroke:0, drawImage:0, radialStops:[] };

let _rafCbs = [];
global.requestAnimationFrame = function(cb){ _rafCbs.push(cb); return _rafCbs.length; };
global.cancelAnimationFrame = function(){ };
global.devicePixelRatio = 2;
global.window = {
  matchMedia(){ return { matches:false, addEventListener(){}, addListener(){} }; },
  addEventListener(){},
  ResizeObserver: function(){ return { observe(){}, disconnect(){} }; },
  MutationObserver: function(){ return { observe(){}, disconnect(){} }; },
  devicePixelRatio: 2,
};
global.ResizeObserver = global.window.ResizeObserver;
global.MutationObserver = global.window.MutationObserver;

function mkEl(){
  return { _attrs:{}, className:'', style:{}, width:0, height:0,
    setAttribute(k,v){ this._attrs[k]=v; }, getAttribute(k){ return this._attrs[k]; },
    appendChild(){}, insertBefore(){}, querySelector(){ return null; }, firstChild:null,
    getBoundingClientRect(){ return {left:0,right:400,top:0,bottom:48,width:400,height:48}; },
  };
}
const _bar = mkEl();
_bar._attrs['data-decor'] = DECOR;
_bar.getContext = undefined;
let _canvasN = 0;
global.document = {
  readyState:'complete', hidden:false,
  documentElement: { getAttribute(k){ return k==='data-theme' ? THEME : null; } },
  addEventListener(){},
  getElementById(id){ return id==='projectBar' ? _bar : null; },
  createElement(t){
    if (t === 'canvas'){
      _canvasN++;
      const rec = (_canvasN >= 2) ? bufRec : visRec;
      const e = mkEl();
      e.getContext = function(){ return mkCtx(rec); };
      return e;
    }
    return mkEl();
  },
};

__SRC__

// install a movable pet AFTER the module loaded (it reads window.TofuPet lazily)
let petX = 40;
if (PET_MODE !== 'none'){
  global.window.TofuPet = { getState(){ return { x: petX, state: (PET_MODE==='drag'?'drag':'walk') }; } };
}
// pump several frames, advancing the foot each frame so wake marks spawn
let ts = 100;
for (let f=0; f<12; f++){
  if (_rafCbs.length){ const cb=_rafCbs.shift(); cb(ts); }
  ts += 100;
  petX += 28;   // > WAKE_STEP_PX per frame → the cat is walking
}
console.log(JSON.stringify({
  scene: window.TofuScene.getScene(),
  visEllipse: visRec.ellipse,
  visStroke: visRec.stroke,
  glowStops: visRec.radialStops,
}));
process.exit(0);
"""


def _run_wake(decor="meadow", pet="moving", src=None):
    src = src if src is not None else SCENE_JS.read_text()
    script = (_WAKE_HARNESS
              .replace("__SRC__", src)
              .replace("__DECOR__", decor)
              .replace("__PET__", pet))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         cwd=str(REPO), timeout=20)
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(line)


def _max_glow_alpha(stops):
    """Highest alpha among the palette-glow radial stops (rgba(...,A))."""
    mx = 0.0
    for s in stops:
        m = re.search(r",\s*([0-9.]+)\s*\)\s*$", str(s))
        if m:
            try:
                mx = max(mx, float(m.group(1)))
            except ValueError:
                pass
    return mx


def test_pet_wake_marks_the_scene_when_walking():
    """OWNER ASK — the pet must DISTURB the painted scene, not float over it. A
    WALKING pet (foot advancing each frame) must add canvas dabs (grass kicked
    up / splash / puff) BEYOND what the scene paints on its own — proving the
    wake is rendered on the SAME canvas as the background."""
    walking = _run_wake(decor="meadow", pet="moving")
    idle = _run_wake(decor="meadow", pet="none")
    assert walking["visEllipse"] > idle["visEllipse"], \
        f"walking pet added no scene-level wake dabs: walk={walking['visEllipse']} vs none={idle['visEllipse']}"


def test_pet_wake_fires_for_every_scene():
    """Each scene flavour reacts underfoot: grass (meadow), splash (pool), puff
    (sky). All must add dabs over their no-pet baseline."""
    for s in ("meadow", "pool", "sky"):
        walking = _run_wake(decor=s, pet="moving")
        idle = _run_wake(decor=s, pet="none")
        assert walking["visEllipse"] > idle["visEllipse"], \
            f"scene {s}: no wake dabs (walk={walking['visEllipse']} none={idle['visEllipse']})"


def test_dragged_pet_leaves_no_wake():
    """A pet being DRAGGED (lifted, state='drag') is in the air, not walking —
    it must NOT press grass/splash under thin air. Wake dab-count must match the
    no-pet baseline."""
    dragged = _run_wake(decor="meadow", pet="drag")
    idle = _run_wake(decor="meadow", pet="none")
    assert dragged["visEllipse"] == idle["visEllipse"], \
        f"dragged (airborne) pet still left a wake: drag={dragged['visEllipse']} vs none={idle['visEllipse']}"


def test_wake_is_dab_only_no_stroke():
    """The wake (incl. the pool splash) must be dab-only — no ctx.stroke() —
    matching the critter convention, so it renders under any 2d context."""
    for s in ("meadow", "pool", "sky"):
        r = _run_wake(decor=s, pet="moving")
        assert r["visStroke"] == 0, f"scene {s} wake used stroke(): {r['visStroke']}"


def test_background_glow_is_dimmed():
    """OWNER ASK — the moving light was too glaring. The drifting sun glow AND
    the pet-attention ground glow are additive radials; their peak alpha must be
    DIMMED well below the old 0.30 / 0.26 values."""
    r = _run_wake(decor="meadow", pet="moving")
    assert r["glowStops"], "no radial glow stops recorded — frame did not paint the glow"
    mx = _max_glow_alpha(r["glowStops"])
    assert mx <= 0.20, f"glow still too bright — peak alpha {mx} (want <= 0.20; old was 0.30)"


def test_NEUTER_wake_spawn_is_caught():
    """NEUTER: remove the wake-mark spawn → a walking pet adds NO scene dabs, so
    the 'wake marks the scene' assertion must fail (proving it bites)."""
    src = SCENE_JS.read_text()
    neut = re.sub(r"_wake\.push\(\{[^;]*\}\);", "/* neutered wake spawn */", src, count=1)
    assert neut != src, "neuter did not match the wake spawn"
    walking = _run_wake(decor="meadow", pet="moving", src=neut)
    idle = _run_wake(decor="meadow", pet="none", src=neut)
    assert walking["visEllipse"] == idle["visEllipse"], \
        "neuter kept some wake — test would not bite"


def test_NEUTER_bright_glow_is_caught():
    """NEUTER: restore the old glaring sun-glow peak (0.30) → the dim assertion
    must fail (proving the dim is guarded, not assumed)."""
    src = SCENE_JS.read_text()
    neut = src.replace("var SUN_GLOW_PEAK = 0.16;", "var SUN_GLOW_PEAK = 0.30;", 1)
    assert neut != src, "neuter did not match the sun-glow constant"
    r = _run_wake(decor="meadow", pet="moving", src=neut)
    mx = _max_glow_alpha(r["glowStops"])
    assert mx > 0.20, f"neutered (bright) glow still read as dim ({mx}) — test would not bite"


# ── DRAG-INTERACTION harness ─────────────────────────────────────────────
# Owner ask: dragging the pet must STILL compress the background (it used to
# only react to a grounded, walking foot — a lifted cat floated over an inert
# scene). The flow-deform now reads a GROUND anchor (_petGroundX) that stays
# live during a drag, instead of the foot anchor (_petFootX, null while lifted).
# This harness records every brush-dab's translate (x,y) on the VISIBLE canvas
# for ONE deterministic frame with a STATIONARY pet in a given mode. At ms=0 the
# only pet-dependent contributor to dab positions is the flow-deform (wake is
# foot-gated → null in both drag & none; sparks/critter/glow don't move dabs),
# so the dab-coordinate set differs between 'drag' and 'none' IFF the deform
# fires under a dragged (airborne) cat. Meadow's sway deform sinks blades in y,
# which is directly observable in the translate coords.
_DRAG_HARNESS = r"""
'use strict';
const DECOR = "__DECOR__";
const PET_MODE = "__PET__";     // 'none' | 'drag' | 'walk'
const PET_X = __PETX__;
function mkCtx(rec){
  return {
    canvas:{width:400,height:48},
    setTransform(){}, clearRect(){}, save(){}, restore(){},
    translate(x,y){ rec.tr.push([Math.round(x*100)/100, Math.round(y*100)/100]); },
    rotate(){}, beginPath(){}, fill(){}, fillRect(){}, ellipse(){}, stroke(){}, drawImage(){},
    createLinearGradient(){ return {addColorStop(){}}; },
    createRadialGradient(){ return {addColorStop(){}}; },
    set fillStyle(v){}, get fillStyle(){return '';},
    set strokeStyle(v){}, get strokeStyle(){return '';},
    set lineWidth(v){}, get lineWidth(){return 1;},
    set globalAlpha(v){}, get globalAlpha(){return 1;},
    set globalCompositeOperation(v){}, get globalCompositeOperation(){return '';},
  };
}
const bufRec={tr:[]}, visRec={tr:[]};
let _rafCbs=[];
global.requestAnimationFrame=function(cb){_rafCbs.push(cb);return _rafCbs.length;};
global.cancelAnimationFrame=function(){};
global.devicePixelRatio=2;
global.window={ matchMedia(){return {matches:false,addEventListener(){},addListener(){}};},
  addEventListener(){}, ResizeObserver:function(){return{observe(){},disconnect(){}};},
  MutationObserver:function(){return{observe(){},disconnect(){}};}, devicePixelRatio:2 };
global.ResizeObserver=global.window.ResizeObserver;
global.MutationObserver=global.window.MutationObserver;
function mkEl(){ return {_attrs:{},className:'',style:{},width:0,height:0,
  setAttribute(k,v){this._attrs[k]=v;}, getAttribute(k){return this._attrs[k];},
  appendChild(){}, insertBefore(){}, querySelector(){return null;}, firstChild:null,
  getBoundingClientRect(){return {left:0,right:400,top:0,bottom:48,width:400,height:48};} }; }
const _bar=mkEl(); _bar._attrs['data-decor']=DECOR; _bar.getContext=undefined;
let _canvasN=0;
global.document={ readyState:'complete', hidden:false,
  documentElement:{getAttribute(k){return k==='data-theme'?'tofu':null;}},
  addEventListener(){}, getElementById(id){return id==='projectBar'?_bar:null;},
  createElement(t){ if(t==='canvas'){_canvasN++;const rec=(_canvasN>=2)?bufRec:visRec;const e=mkEl();e.getContext=function(){return mkCtx(rec);};return e;} return mkEl(); } };
__SRC__
if (PET_MODE !== 'none'){
  global.window.TofuPet = { getState(){ return { x: PET_X, state: (PET_MODE==='drag'?'drag':'walk') }; } };
}
visRec.tr = [];   // discard the offscreen bake; capture only the per-frame visible dabs
if (_rafCbs.length){ const cb=_rafCbs.shift(); cb(0); }
console.log(JSON.stringify({ tr: visRec.tr }));
process.exit(0);
"""


def _run_drag(decor="meadow", pet="drag", petx=40, src=None):
    src = src if src is not None else SCENE_JS.read_text()
    script = (_DRAG_HARNESS
              .replace("__SRC__", src)
              .replace("__DECOR__", decor)
              .replace("__PET__", pet)
              .replace("__PETX__", str(petx)))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         cwd=str(REPO), timeout=20)
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(line)["tr"]


def test_dragged_pet_compresses_the_scene():
    """OWNER ASK — a DRAGGED (lifted) cat must still interact with the
    background, not float over an inert scene. The flow-deform reads the GROUND
    anchor (live during drag), so the meadow blades under a held cat sink/part —
    changing their dab positions vs a scene with no pet. Since the wake is
    foot-gated (silent under drag) and nothing else pet-dependent moves a dab,
    ANY coordinate difference proves the drag deform fired."""
    drag = _run_drag(decor="meadow", pet="drag")
    none = _run_drag(decor="meadow", pet="none")
    assert len(drag) == len(none), \
        f"drag must not add/remove dabs, only shift them: drag={len(drag)} none={len(none)}"
    assert drag != none, \
        "a dragged pet did not disturb the scene — it still floats over an inert background"


def test_NEUTER_drag_deform_uses_foot_anchor_is_caught():
    """NEUTER: revert the flow-deform to the FOOT anchor (_petFootX, null while
    lifted) → a dragged cat deforms nothing, so the drag scene is byte-identical
    to the no-pet scene. The 'drag compresses the scene' assertion must then
    fail (proving the ground-anchor fix is what makes drag interact)."""
    src = SCENE_JS.read_text()
    neut = src.replace("var px = _petGroundX();", "var px = _petFootX();", 1)
    assert neut != src, "neuter did not match the flow-deform ground anchor"
    drag = _run_drag(decor="meadow", pet="drag", src=neut)
    none = _run_drag(decor="meadow", pet="none", src=neut)
    assert drag == none, \
        "neutered (foot-anchor) build still deformed under a dragged cat — test would not bite"


def test_css_pet_has_contact_shadow_that_detaches_on_drag():
    """OWNER ASK — the pet must read as PLANTED on the ground, not floating on
    top. A contact shadow lives on the POSITION layer (.tofu-pet::after) so it
    stays on the ground while the sprite bobs; when the cat is picked up
    ([data-state=drag]) it shrinks + fades (object rises, shadow detaches)."""
    css = CSS.read_text()
    m = re.search(r"\.tofu-pet::after\{[^}]*\}", css)
    assert m, "no .tofu-pet::after contact-shadow rule"
    block = m.group(0)
    assert "position:absolute" in block, "shadow must be absolutely positioned on the ground"
    assert "z-index:-1" in block, "shadow must sit BEHIND the sprite (z-index:-1)"
    assert "radial-gradient" in block or "background" in block, "shadow needs a soft fill"
    # picking the cat up must visibly detach the shadow (shrink/fade)
    d = re.search(r'\.tofu-pet\[data-state="drag"\]::after\{[^}]*\}', css)
    assert d, "no drag-state override — the shadow wouldn't detach when the cat is lifted"


def test_NEUTER_missing_contact_shadow_is_caught():
    """NEUTER: strip the .tofu-pet::after shadow rule → the contact-shadow
    assertion must fail (proving the grounding is guaranteed by CSS, not
    assumed)."""
    css = CSS.read_text()
    neut = re.sub(r"\.tofu-pet::after\{[^}]*\}", "", css, count=1)
    assert neut != css, "neuter did not remove the contact-shadow rule"
    present = bool(re.search(r"\.tofu-pet::after\{[^}]*\}", neut))
    assert present is False, "shadow rule survived the neuter — test would not bite"


# ── FIELD-MOTION harness ─────────────────────────────────────────────────
# Owner ask (the third, still-open complaint): "the background doesn't move,
# only the light spots do." The scene-count / single-frame tests above prove
# the field is PAINTED but NOT that it is ANIMATED — a regression that froze the
# flow layer (grass/water/clouds) while leaving the sun-glow + specular SPARKS
# twinkling would pass every one of them and reproduce the exact bug. This
# harness pumps TWO frames at different timestamps (ms=0 then ms=2000) with NO
# pet, records every visible dab's (fill, x, y, ang), and lets the test assert
# the FIELD dabs DISPLACE between frames — separably from the light.
#
# ISOLATION (why this is specifically about the field, not the light): dab()
# always sets fillStyle → translate(x,y) → rotate(ang), and _paintFrame paints
# in a FIXED order — sparks (fill == pal.spark) → flow field (near-layer colors)
# → critter (ALWAYS the final 3 dabs; no wake fires with no pet). So the test
# drops the trailing 3 (critter) and excludes the spark colour → a PURE flow
# field set. The neuter freezes ONLY the flow (injects ms=0 inside _paintFlow,
# which runs AFTER the spark loop) so the sparks still move — proving a frozen
# field is caught even while the light keeps twinkling (the reported symptom).
_MOTION_HARNESS = r"""
'use strict';
const DECOR = "__DECOR__";
function mkCtx(rec){
  return {
    canvas:{width:400,height:48},
    setTransform(){}, clearRect(){}, save(){}, restore(){},
    translate(x,y){ rec.dabs.push([rec._fill, Math.round(x*100)/100, Math.round(y*100)/100, null]); },
    rotate(a){ const d=rec.dabs[rec.dabs.length-1]; if(d) d[3]=Math.round(a*1000)/1000; },
    beginPath(){}, fill(){}, fillRect(){}, ellipse(){}, stroke(){}, drawImage(){},
    createLinearGradient(){ return {addColorStop(){}}; },
    createRadialGradient(){ return {addColorStop(){}}; },
    set fillStyle(v){ rec._fill = v; }, get fillStyle(){ return rec._fill||''; },
    set strokeStyle(v){}, get strokeStyle(){return '';},
    set lineWidth(v){}, get lineWidth(){return 1;},
    set globalAlpha(v){}, get globalAlpha(){return 1;},
    set globalCompositeOperation(v){}, get globalCompositeOperation(){return '';},
  };
}
const bufRec={dabs:[],_fill:''}, visRec={dabs:[],_fill:''};
let _rafCbs=[];
global.requestAnimationFrame=function(cb){_rafCbs.push(cb);return _rafCbs.length;};
global.cancelAnimationFrame=function(){};
global.devicePixelRatio=2;
global.window={ matchMedia(){return {matches:false,addEventListener(){},addListener(){}};},
  addEventListener(){}, ResizeObserver:function(){return{observe(){},disconnect(){}};},
  MutationObserver:function(){return{observe(){},disconnect(){}};}, devicePixelRatio:2 };
global.ResizeObserver=global.window.ResizeObserver;
global.MutationObserver=global.window.MutationObserver;
function mkEl(){ return {_attrs:{},className:'',style:{},width:0,height:0,
  setAttribute(k,v){this._attrs[k]=v;}, getAttribute(k){return this._attrs[k];},
  appendChild(){}, insertBefore(){}, querySelector(){return null;}, firstChild:null,
  getBoundingClientRect(){return {left:0,right:400,top:0,bottom:48,width:400,height:48};} }; }
const _bar=mkEl(); _bar._attrs['data-decor']=DECOR; _bar.getContext=undefined;
let _canvasN=0;
global.document={ readyState:'complete', hidden:false,
  documentElement:{getAttribute(k){return k==='data-theme'?'tofu':null;}},
  addEventListener(){}, getElementById(id){return id==='projectBar'?_bar:null;},
  createElement(t){ if(t==='canvas'){_canvasN++;const rec=(_canvasN>=2)?bufRec:visRec;const e=mkEl();e.getContext=function(){return mkCtx(rec);};return e;} return mkEl(); } };
__SRC__
// NO pet — this is about the field moving on its own, not a pet interaction.
function pump(ms){
  visRec.dabs = [];
  if (_rafCbs.length){ const cb=_rafCbs.shift(); cb(ms); }
  return visRec.dabs.slice();
}
// NB: pump at NON-ZERO ts. _loop guards with `if(!_t0)` and _t0 starts 0, so a
// first cb(0) sets _t0=0 (falsy) and a later cb re-triggers the guard, resetting
// elapsed to 0. cb(100) sets _t0=100 (truthy) → frame A elapsed 0 (at rest),
// cb(2100) → frame B elapsed 2000 (mid-motion).
const frameA = pump(100);
const frameB = pump(2100);
console.log(JSON.stringify({ a: frameA, b: frameB }));
process.exit(0);
"""

# spark (light) colour per scene — excluded so the assertion is about the FIELD,
# not the twinkling light. Sourced from PALETTES[...].spark in tofu-scene.js.
_SPARK = {"meadow": "#F2F6C8", "pool": "#EAF7F4", "sky": "#FFF7E6"}


def _run_motion(decor="meadow", src=None):
    src = src if src is not None else SCENE_JS.read_text()
    script = _MOTION_HARNESS.replace("__SRC__", src).replace("__DECOR__", decor)
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         cwd=str(REPO), timeout=20)
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(line)


def _field_dabs(frame, decor):
    """Isolate the FLOW-FIELD dabs from a frame's full dab list: drop the
    trailing 3 (the critter, always painted last with no pet), then exclude the
    spark (light) colour. What's left is the grass/water/cloud field."""
    body = frame[:-3] if len(frame) >= 3 else frame
    spark = _SPARK[decor]
    return [d for d in body if d[0] != spark]


def _fraction_moved(a, b):
    """Fraction of index-aligned field dabs that DISPLACED between two frames
    (x/y by > 0.5px or angle by > 0.05rad). Order is stable (loop over the same
    _flow array), so index k in A corresponds to index k in B."""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0, 0
    moved = 0
    for i in range(n):
        fa, xa, ya, aa = a[i]
        fb, xb, yb, ab = b[i]
        da = abs((aa or 0) - (ab or 0))
        if abs(xa - xb) > 0.5 or abs(ya - yb) > 0.5 or da > 0.05:
            moved += 1
    return moved / n, n


def _total_disp(a, b):
    """Sum of index-aligned euclidean (x,y) displacement across dabs — a
    sensitive 'did this layer move at all' metric that doesn't depend on any
    per-dab threshold (used for the small-amplitude light control)."""
    n = min(len(a), len(b))
    tot = 0.0
    for i in range(n):
        _, xa, ya, _ = a[i]
        _, xb, yb, _ = b[i]
        tot += ((xa - xb) ** 2 + (ya - yb) ** 2) ** 0.5
    return tot


def test_field_animates_between_frames_for_every_scene():
    """OWNER ASK (3rd complaint) — the BACKGROUND FIELD itself must MOVE, not
    just the light spots. Pumping two frames (ms=0 → ms=2000) with no pet, a
    meaningful fraction of the flow-field dabs (grass sway / water drift / cloud
    glide) must displace — for all three scenes. The spark (light) dabs are
    excluded so this is specifically the FIELD moving, not the twinkle."""
    for s in ("meadow", "pool", "sky"):
        r = _run_motion(decor=s)
        a, b = _field_dabs(r["a"], s), _field_dabs(r["b"], s)
        assert a, f"scene {s}: no field dabs captured"
        frac, n = _fraction_moved(a, b)
        assert frac >= 0.5, \
            f"scene {s}: field is nearly STATIC — only {frac:.0%} of {n} flow dabs moved " \
            f"between frames (the 'background doesn't move, only light does' bug)"


def test_light_moves_even_when_field_frozen_control():
    """CONTROL — proves the field/light split is real: the SPARK (light) dabs
    DISPLACE between frames on their own. If this failed, the field-motion test
    could be accidentally measuring the light instead of the field."""
    r = _run_motion(decor="meadow")
    spark = _SPARK["meadow"]
    la = [d for d in r["a"][:-3] if d[0] == spark]
    lb = [d for d in r["b"][:-3] if d[0] == spark]
    assert la and lb, "no spark (light) dabs captured"
    assert _total_disp(la, lb) > 0.5, \
        "the light itself did not move between frames — control invalid"


def test_NEUTER_frozen_field_is_caught():
    """NEUTER — the EXACT reported regression: freeze the flow field (inject
    ms=0 inside _paintFlow, which runs AFTER the spark loop) so the grass/water/
    clouds sit still while the sun-glow + sparks keep twinkling. The field-
    motion assertion MUST then fail for every scene (proving it bites), while
    the light-moves control still holds (proving the test targets the field)."""
    src = SCENE_JS.read_text()
    # ms=0 only inside _paintFlow → field static; sparks (painted earlier in
    # _paintFrame with the real ms) keep moving.
    anchor = "  function _paintFlow(c, pal, ms, w, h) {\n    if (!_flow.length) return;"
    neut = src.replace(anchor, anchor + "\n    ms = 0;  /* NEUTER: freeze the field */", 1)
    assert neut != src, "neuter did not match the _paintFlow entry"
    caught_any = False
    for s in ("meadow", "pool", "sky"):
        r = _run_motion(decor=s, src=neut)
        a, b = _field_dabs(r["a"], s), _field_dabs(r["b"], s)
        frac, _ = _fraction_moved(a, b)
        if frac < 0.5:
            caught_any = True
        # the light must STILL move in the neutered build (field-specific bite)
        spark = _SPARK[s]
        la = [d for d in r["a"][:-3] if d[0] == spark]
        lb = [d for d in r["b"][:-3] if d[0] == spark]
        assert _total_disp(la, lb) > 0.5, \
            f"scene {s}: neuter also froze the light — bite not field-specific"
    assert caught_any, \
        "neutered (frozen-field) build still animated the field — the motion test would not bite"


def test_registered_in_bundler_after_pet():
    """Must be in _BUNDLE_FILES (else it's a silent no-op) and load after the
    pet (which owns the data-decor attribute it mirrors)."""
    b = BUNDLER.read_text()
    assert "'tofu-scene.js'" in b, "tofu-scene.js missing from _BUNDLE_FILES"
    from lib.js_bundler import _BUNDLE_FILES
    assert _BUNDLE_FILES.index("tofu-scene.js") > _BUNDLE_FILES.index("tofu-pet.js"), \
        "tofu-scene.js must load after tofu-pet.js"


def test_registered_in_index_html_fallback():
    assert "tofu-scene.js" in INDEX.read_text(), \
        "tofu-scene.js missing from index.html dev-fallback <script> tags"


def test_css_mounts_canvas_below_pet_and_controls():
    """The canvas must be positioned at z0 (below the pet z1 + controls z2) and
    clipped to the rounded shell so it never spills the corners."""
    css = CSS.read_text()
    assert ".tofu-scene-canvas" in css, "no canvas CSS rule"
    m = re.search(r"\.tofu-scene-canvas\{[^}]*\}", css)
    assert m, "could not isolate the canvas rule"
    block = m.group(0)
    assert "z-index:0" in block, "canvas must sit at z0 (below pet/controls)"
    assert "pointer-events:none" in block, "canvas must never steal a click"
    assert "clip-path" in block or "border-radius" in block, \
        "canvas must be clipped to the rounded shell"


if __name__ == "__main__":
    for fn in [test_mount_paints_opaque_base_and_many_dabs,
               test_scene_exposes_critter_and_spook_for_pet_interaction,
               test_animation_loop_runs_when_active,
               test_reduced_motion_is_static_no_loop,
               test_scene_off_paints_nothing_and_no_loop,
               test_non_tofu_theme_does_not_animate,
               test_every_scene_paints_dabs,
               test_NEUTER_flat_fill_is_caught,
               test_mount_stamps_scene_canvas_marker,
               test_no_2d_context_leaves_marker_off,
               test_css_gate_hides_svg_ground_when_canvas_live,
               test_NEUTER_missing_css_gate_is_caught,
               test_NEUTER_missing_crest_gate_is_caught,
               test_pet_wake_marks_the_scene_when_walking,
               test_pet_wake_fires_for_every_scene,
               test_dragged_pet_leaves_no_wake,
               test_wake_is_dab_only_no_stroke,
               test_background_glow_is_dimmed,
               test_NEUTER_wake_spawn_is_caught,
               test_NEUTER_bright_glow_is_caught,
               test_dragged_pet_compresses_the_scene,
               test_NEUTER_drag_deform_uses_foot_anchor_is_caught,
               test_css_pet_has_contact_shadow_that_detaches_on_drag,
               test_NEUTER_missing_contact_shadow_is_caught,
               test_field_animates_between_frames_for_every_scene,
               test_light_moves_even_when_field_frozen_control,
               test_NEUTER_frozen_field_is_caught,
               test_registered_in_bundler_after_pet,
               test_registered_in_index_html_fallback,
               test_css_mounts_canvas_below_pet_and_controls]:
        fn()
        print("PASS", fn.__name__)
    print("ALL GREEN")
