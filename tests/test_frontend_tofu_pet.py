"""Tofu pet mascot (static/js/tofu-pet.js) — behaviour + registration guards.

The pet is an emoting cube-of-tofu mascot mounted into #projectBar. Each
expression is a pre-rendered art frame (static/icons/pet/tofu-pet-<expr>.png);
a small time-of-day / mood finite-state machine picks the frame (research-
grounded resolution priority: activity > night-sleep > low-mood >
low-energy/evening > recent-positive > time-default > idle).

These tests run the REAL shipped module under node with a minimal DOM/storage
stub, drive its public `window.TofuPet` surface, and assert:
  * every declared EXPRESSION resolves to an art frame that EXISTS on disk;
  * the time/mood FSM resolves to the right expression at representative
    hours + moods (the core state logic);
  * loading activity wins over the clock (thinking), success celebrates;
  * the module is wired into the JS bundler manifest + index.html dev fallback.
A biting NEUTER breaks the night-sleep branch and proves the FSM test catches it.
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PET_JS = REPO / "static" / "js" / "tofu-pet.js"
BUNDLER = REPO / "lib" / "js_bundler.py"
INDEX = REPO / "index.html"


# ── node harness: eval the module with a fake window/document/localStorage,
# freeze the clock to a chosen hour, then interrogate window.TofuPet. ──
_HARNESS = r"""
'use strict';
const HOUR = __HOUR__;
const RealDate = Date;
class FakeDate extends RealDate {
  constructor(...a){ if(a.length){ super(...a); } else { super(); } }
  getHours(){ return HOUR; }
  static now(){ return __NOW__; }
}
global.Date = FakeDate;
global.window = {
  // reduced-motion ON → the pet stands (state 'idle'), so _resolve() drives the
  // frame and the FSM expression tests can read it (no roaming 'walk' override).
  matchMedia(){ return { matches:true, addEventListener(){}, addListener(){} }; },
  addEventListener(){},
};
global.BASE_PATH = '';
global.requestAnimationFrame = function(){ return 0; };   // don't actually run the loop
global.cancelAnimationFrame = function(){};
global.ResizeObserver = function(){ return { observe(){}, disconnect(){} }; };
global.localStorage = { _d:{}, getItem(k){return this._d[k]||null;}, setItem(k,v){this._d[k]=v;} };
global.Image = function(){ return { set src(v){}, get src(){return '';} }; };
let _mounted = null;
function _fakeEl(){ return { _attrs:{}, tagName:'', className:'', alt:'', src:'', offsetWidth:30,
  setAttribute(k,v){this._attrs[k]=v;}, getAttribute(k){return this._attrs[k];},
  addEventListener(){}, appendChild(){}, insertBefore(){},
  querySelector(){ return null; }, querySelectorAll(){ return []; },
  getBoundingClientRect(){ return {left:0,right:400,top:0,bottom:48,width:400,height:48}; },
  firstChild:null, style:{setProperty(){}, removeProperty(){}} }; }
global.document = {
  readyState:'complete',
  hidden:false,
  addEventListener(){},
  getElementById(id){ if(id==='projectBar') return _fakeEl(); return _mounted; },
  createElement(t){ const e=_fakeEl(); e.tagName=t; if(t!=='img') _mounted=e; return e; },
  querySelectorAll(){ return _mounted ? [_mounted] : []; },   // for _applyDecor
};
__SRC__
const TP = window.TofuPet;
// Apply the requested state patch. An `activity` value routes through the
// public setActivity() path (that's where success→celebrate / error→surprise
// live); mood/energy go through setState().
const _patch = __PATCH__;
if (typeof _patch.activity === 'string') { TP.setActivity(_patch.activity); delete _patch.activity; }
// Only re-run setState (which re-resolves) if there's still something to set —
// an empty setState would re-resolve and clobber a transient reaction (celebrate).
if (Object.keys(_patch).length) TP.setState(_patch);
const _expr = TP.getState().expr;
console.log(JSON.stringify({
  expr: _expr,
  expressions: TP.EXPRESSIONS,
  frameUrl: TP.frameUrl((TP.FRAME_ALIAS[_expr] || _expr)),
  decorStyles: TP.DECOR_STYLES,
  decor: TP.getDecor(),
  walkFrames: TP.WALK_FRAMES,
  groomFrames: TP.GROOM_FRAMES,
  scratchFrames: TP.SCRATCH_FRAMES,
}));
process.exit(0);   // the pet's setInterval timers keep the event loop alive
"""


def _run(hour=14, now=0, patch=None):
    """Boot the pet at a fixed hour with a state patch; return its resolved state."""
    src = PET_JS.read_text()
    import json
    script = (_HARNESS
              .replace("__SRC__", src)
              .replace("__HOUR__", str(hour))
              .replace("__NOW__", str(now))
              .replace("__PATCH__", json.dumps(patch or {})))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         cwd=str(REPO), timeout=20)
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(line)


import json  # noqa: E402  (used inside _run)


def test_expression_frames_exist_on_disk():
    """Every declared expression must have its kitten art frame present in
    static/icons/pet/oneko/ (a missing frame = a broken <img> in the bar). The
    pet is now a single public-domain oneko cat (no swappable packs)."""
    r = _run()
    pet_dir = REPO / "static" / "icons" / "pet" / "oneko"
    for e in r["expressions"]:
        f = pet_dir / f"oneko-{e}.png"
        assert f.exists() and f.stat().st_size > 0, f"missing/empty art frame: {f}"
    # The canonical research-grounded set must all be present as expressions.
    for e in ("idle", "happy", "sleepy", "sleeping", "thinking",
              "surprised", "sad", "celebrating"):
        assert e in r["expressions"], f"missing expression: {e}"


def test_extra_cat_pose_frames_exist_on_disk():
    """The 'more forms' the owner asked for: the groom + wall-scratch pose
    cycles must exist on disk so the cat has richer idle behaviour, not just
    walk/sit."""
    r = _run()
    pet_dir = REPO / "static" / "icons" / "pet" / "oneko"
    poses = list(r.get("groomFrames", [])) + list(r.get("scratchFrames", []))
    assert len(poses) >= 5, f"expected >=5 extra pose frames, got {poses}"
    for p in poses:
        f = pet_dir / f"oneko-{p}.png"
        assert f.exists() and f.stat().st_size > 0, f"missing/empty pose frame: {f}"


def test_curious_alias_resolves_to_a_real_frame():
    """'curious' has no own art — it must alias to an existing frame."""
    r = _run(hour=14, patch={})
    # frameUrl for the resolved expr always points at a real file; assert the
    # alias target (alert) exists so curious never yields a broken image.
    assert (REPO / "static" / "icons" / "pet" / "oneko" / "oneko-alert.png").exists()


def test_decor_scene_assets_exist():
    """Every non-'off' scene style must have its seamless ground strip on disk
    (a missing strip = an empty --bar-scene → no diorama ground)."""
    r = _run()
    pet_dir = REPO / "static" / "icons" / "pet"
    styles = [s for s in r["decorStyles"] if s != "off"]
    assert styles, "no scene styles declared"
    for s in styles:
        f = pet_dir / f"scene-{s}.svg"
        assert f.exists() and f.stat().st_size > 0, f"missing/empty scene: {f}"
        # each world also ships an organic top-edge overlay
        e = pet_dir / f"edge-{s}.svg"
        assert e.exists() and e.stat().st_size > 0, f"missing/empty edge overlay: {e}"
    assert "sky" in styles, "multi-world set must include the sky scene"
    assert r["decor"] in r["decorStyles"], "default decor not in DECOR_STYLES"


def test_decor_css_wires_every_scene():
    """styles.css must map each scene style to a --bar-scene ground image via
    [data-decor], paint it on the bar's ::after ground layer, and keep the
    controls ABOVE the roaming pet (occlusion invariant, so it never blocks a
    hit target)."""
    css = (REPO / "static" / "styles.css").read_text()
    for s in ("meadow", "pool", "sky"):
        assert f'[data-decor="{s}"]' in css, f"no [data-decor=\"{s}\"] rule in CSS"
        assert f"scene-{s}.svg" in css, f"scene-{s}.svg not referenced in CSS"
        assert f"edge-{s}.svg" in css, f"edge-{s}.svg (top-edge overlay) not referenced in CSS"
    assert "--bar-scene" in css, "scene ground must be driven by --bar-scene"
    assert "--bar-edge" in css, "top-edge overlay must be driven by --bar-edge"
    assert ".project-bar::after" in css, "scene ground must paint on ::after"
    # The organic top edge pokes ABOVE the rim on ::before with a negative top
    # offset (the deliberate 'less neat / artistic edge' — now a GENTLE poke).
    assert ".project-bar::before" in css, "organic top-edge overlay must paint on ::before"
    assert "top:-6px" in css, "top-edge overlay must poke ABOVE the rim (negative top)"
    # Occlusion: the REAL controls are lifted above the pet so it passes BEHIND
    # them. This is an ALLOWLIST (only real controls), NOT a `*:not(.tofu-pet)`
    # denylist — the denylist forced the speech bubble / fx / canvas overlays
    # in-flow, reserving an empty flex slot (the "blank box right of the kitten"
    # bug). See test_control_lift_is_allowlist_not_denylist below.
    assert '.project-bar > .project-bar-close{position:relative;z-index:2}' in css \
        or ".project-bar > .project-bar-close" in css and "z-index:2" in css, \
        "real controls must be z-lifted above the roaming pet (allowlist)"


def test_control_lift_is_allowlist_not_denylist():
    """ROOT-CAUSE guard for the "blank space to the right of the kitten" bug.

    The bug: `[data-theme="tofu"] .project-bar > *:not(.tofu-pet){position:
    relative;z-index:2}` (specificity 0,3,0) matched the speech bubble — a
    direct child that isn't .tofu-pet — and beat the bubble's own
    `position:absolute` (0,2,0), forcing it in-flow so it reserved a ~74px
    empty flex slot after the × close button. The robust fix is to INVERT the
    rule to an allowlist of the real controls, so every decorative/overlay
    child (bubble, fx, canvas, and any FUTURE overlay) is absolute-by-default.

    This test asserts (a) the fragile denylist is GONE and (b) the allowlist
    names the real controls. A NEUTER that restores the denylist must fail (a)."""
    css = (REPO / "static" / "styles.css").read_text()
    assert ".project-bar > *:not(.tofu-pet)" not in css, \
        "the fragile denylist rule is back — overlay children get an in-flow slot again"
    for ctrl in (".project-bar-icon", ".project-bar-folders", ".project-autoapply-toggle",
                 ".project-bar-btn", ".project-bar-close"):
        assert f'.project-bar > {ctrl}' in css, \
            f"control-lift allowlist is missing {ctrl}"
    # NEUTER: reintroduce the denylist → assertion (a) must bite.
    neut = css + '\n[data-theme="tofu"] .project-bar > *:not(.tofu-pet){position:relative;z-index:2}\n'
    assert ".project-bar > *:not(.tofu-pet)" in neut, "neuter did not add the denylist"


def test_pack_switcher_fully_removed():
    """The pet is a single kitten now — the swappable pack system + its switch
    button were removed. Guard that the dead surface is gone so it can't rot
    back in: no pack-switch button in index.html, no pack API on TofuPet, no
    [data-pack] CSS rule."""
    html = INDEX.read_text()
    assert "pack-switch-btn" not in html and "cyclePack" not in html, \
        "the pet pack-switch button is still in index.html"
    src = PET_JS.read_text()
    for sym in ("PET_PACKS", "cyclePack", "setPack", "PACK_ORDER"):
        assert sym not in src, f"dead pack symbol {sym} still in tofu-pet.js"
    css = (REPO / "static" / "styles.css").read_text()
    assert '[data-pack=' not in css, "dead [data-pack] CSS rule still present"



def test_dom_footstep_fx_layer_fully_removed():
    """The pet's footstep disturbance is now drawn ON the scene canvas
    (tofu-scene.js _trackPet/_paintWake). The earlier FLOATING DOM particle
    layer (_emitFootstep + a .tofu-fx overlay) was a SECOND footstep system on a
    different layer than the thing it disturbed — the "two stacked layers" smell
    — so it was retired in favour of the single canvas surface.

    Guard the dead surface stays gone so it can't rot back in: no fx symbols in
    tofu-pet.js, no .tofu-fx* CSS rules or keyframes, and no fxCount on the
    public getState() surface. A NEUTER (re-add a fx symbol) must fail."""
    src = PET_JS.read_text()
    for sym in ("_emitFootstep", "_fxLayer", "FX_SCENE", "_fxScene",
                "_emitAccum", "FX_MAX", "fxCount"):
        assert sym not in src, f"dead footstep-fx symbol {sym!r} still in tofu-pet.js"
    # The canvas wake that SUPERSEDES it must be present in tofu-scene.js.
    scene = (REPO / "static" / "js" / "tofu-scene.js").read_text()
    for sym in ("_trackPet", "_paintWake", "_petFootX"):
        assert sym in scene, f"canvas wake {sym!r} missing from tofu-scene.js — nothing supersedes the removed DOM fx"
    # No orphaned CSS: the whole .tofu-fx surface (rules + keyframes + vars).
    css = (REPO / "static" / "styles.css").read_text()
    for token in ("tofu-fx", "tofuFxGrass", "tofuFxRipple", "tofuFxWisp",
                  "--fx-life", "--fx-dir"):
        assert token not in css, f"orphaned footstep-fx CSS {token!r} still in styles.css"
    # NEUTER: reintroduce a fx symbol → the JS "symbol gone" assertion must bite.
    neut = src + "\n  var _fxLayer = null;\n"
    bit = False
    try:
        for sym in ("_emitFootstep", "_fxLayer", "FX_SCENE", "_fxScene",
                    "_emitAccum", "FX_MAX", "fxCount"):
            assert sym not in neut, f"dead footstep-fx symbol {sym!r} still in tofu-pet.js"
    except AssertionError:
        bit = True
    assert bit, "neuter did not bite — re-adding a fx symbol went undetected"


def test_cat_chases_the_scene_critter():
    """PET ⋈ SCENE interaction: when a scene critter drifts near, the wander
    FSM must be able to enter a 'chase' state and dash toward it, and on catch
    call TofuScene.spook(). We stub a TofuScene whose critter sits at a fixed x
    and pump the rAF loop with reduced-motion OFF; the pet must reach the
    critter and fire spook(). A NEUTER (never entering chase) must break it."""
    def chase_result(src_text):
        harness = r'''
'use strict';
let _t = 0;
const RealDate = Date;
class FakeDate extends RealDate { static now(){ return _t; } getHours(){ return 14; } }
global.Date = FakeDate;
// DETERMINISM: seed Math.random with a stubbed LCG so the wander timing +
// start position are reproducible and the pump reliably converges on a catch.
// (Chase ENTRY in _pickNext is already deterministic given a critter in range;
// the flake was that random state durations / start x sometimes kept the cat
// from reaching a catch inside the 160-frame window.) Seeded → same sequence
// every run, for BOTH the real and the neutered build, so the neuter stays
// honest: if chase entry is removed, no seed can produce a spook.
let _seed = 0x2545F491;
global.Math.random = function(){ _seed = (_seed * 1103515245 + 12345) & 0x7fffffff; return _seed / 0x7fffffff; };
let _cbs = [];
global.requestAnimationFrame = function(cb){ _cbs.push(cb); return _cbs.length; };
global.cancelAnimationFrame = function(){};
global.window = { matchMedia(){ return {matches:false, addEventListener(){}, addListener(){}}; }, addEventListener(){} };
global.BASE_PATH = '';
global.ResizeObserver = function(){ return {observe(){}, disconnect(){}}; };
global.localStorage = { _d:{}, getItem(k){return this._d[k]||null;}, setItem(k,v){this._d[k]=v;} };
global.Image = function(){ return { set src(v){}, get src(){return '';} }; };
// A stub scene with a critter parked mid-track that records spook() calls.
let _spooks = 0;
global.window.TofuScene = { critterX(){ return 150; }, spook(){ _spooks++; }, critterInfo(){ return {x:150}; } };
let _mounted=null;
function _fakeEl(tag){ return { _attrs:{}, tagName:tag||'', offsetWidth:30,
  set src(v){ this._src=v; }, get src(){ return this._src||''; },
  setAttribute(k,v){this._attrs[k]=v;}, getAttribute(k){return this._attrs[k];},
  addEventListener(){}, appendChild(){}, insertBefore(){}, querySelector(){return null;}, querySelectorAll(){return [];},
  getBoundingClientRect(){ return {left:0,right:400,top:0,bottom:48,width:400,height:48}; }, firstChild:null, style:{setProperty(){}, removeProperty(){}} }; }
global.document = { readyState:'complete', hidden:false, addEventListener(){},
  getElementById(id){ if(id==='projectBar') return _fakeEl(); return _mounted; },
  createElement(t){ const e=_fakeEl(t); if(t!=='img') _mounted=e; return e; },
  querySelectorAll(){ return _mounted?[_mounted]:[]; } };
__SRC__
const TP = window.TofuPet;
// Force the pet into chase and pump the loop; it should home on x=150 (critter
// - petcentre offset) and spook it. We give it many frames to converge.
let sawChase = false;
for (let k=0; k<160; k++){
  _t += 60;
  const cb = _cbs.shift();
  if (cb) cb(_t);
  if (TP.getState().state === 'chase') sawChase = true;
}
console.log(JSON.stringify({ sawChase, spooks: _spooks, x: TP.getState().x }));
process.exit(0);
'''
        script = harness.replace("__SRC__", src_text)
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             cwd=str(REPO), timeout=20)
        assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
        line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
        return json.loads(line)

    src = PET_JS.read_text()
    real = chase_result(src)
    assert real["spooks"] >= 1, \
        f"the cat never caught + spooked the critter (spooks={real['spooks']}) — no interaction"

    # NEUTER: make _pickNext never enter chase → no spook ever fires.
    neut = src.replace("_enter('chase'); return;", "/* neutered chase */", 1)
    assert neut != src, "neuter did not match the chase entry"
    broken = chase_result(neut)
    assert broken["spooks"] == 0, \
        f"neutered build still spooked (spooks={broken['spooks']}) — test does not bite"


def test_project_brain_button_removed_from_bar():
    """The owner asked to delete the Project Brain toggle button from the
    project bar. Guard it stays gone: no #projectBrainBtn markup, and it's out
    of the tofu control-lift allowlist. (The feature itself is still reachable
    via the collab/presence strip → openProjectBrain, so we don't touch
    project-brain.js.)"""
    html = INDEX.read_text()
    assert 'id="projectBrainBtn"' not in html and "toggleProjectBrain()" not in html, \
        "the Project Brain toggle button is still in the project bar"
    css = (REPO / "static" / "styles.css").read_text()
    assert '.project-bar > .project-brain-toggle' not in css, \
        "removed brain button must not linger in the control-lift allowlist"


def test_pet_is_draggable():
    """The kitten must be DRAGGABLE: a pointerdown + a pointermove past the slop
    lifts it into the 'drag' state and moves W.x to follow the pointer; a
    pointerup after real movement drops it (NOT a click → no interact bubble).
    A press with NO movement is still a click (interact → bubble). We run the
    REAL module with a handler-recording DOM and drive synthetic pointer events.
    A NEUTER (drag never engages) must break the 'moved' assertion."""
    def drag_probe(src_text):
        harness = r'''
'use strict';
let _t = 0;
const RealDate = Date;
class FakeDate extends RealDate { static now(){ return _t; } getHours(){ return 14; } }
global.Date = FakeDate;
// DETERMINISM: seed Math.random with a stubbed LCG. _startWander sets the pet's
// start x to _rand(min,max); the drag grab is RELATIVE to that start, so an
// unseeded random start made draggedX (= start + pointer delta) sometimes fall
// below the assertion threshold (~1-in-4 flake). Seeded → fixed start → the
// same drag result every run, for BOTH the real and neutered build.
let _seed = 0x2545F491;
global.Math.random = function(){ _seed = (_seed * 1103515245 + 12345) & 0x7fffffff; return _seed / 0x7fffffff; };
let _cbs = [];
global.requestAnimationFrame = function(cb){ _cbs.push(cb); return _cbs.length; };
global.cancelAnimationFrame = function(){};
global.window = { PointerEvent: function(){}, matchMedia(){ return {matches:false, addEventListener(){}, addListener(){}}; }, addEventListener(){} };
global.BASE_PATH = '';
global.ResizeObserver = function(){ return {observe(){}, disconnect(){}}; };
global.localStorage = { _d:{}, getItem(k){return this._d[k]||null;}, setItem(k,v){this._d[k]=v;} };
global.Image = function(){ return { set src(v){}, get src(){return '';} }; };
let _mounted=null; const _all=[];
function _fakeEl(tag){ const el = { _attrs:{}, _ev:{}, tagName:tag||'', className:'', offsetWidth:30,
  set src(v){ this._src=v; }, get src(){ return this._src||''; },
  setAttribute(k,v){this._attrs[k]=v;}, getAttribute(k){return this._attrs[k]!==undefined?this._attrs[k]:null;},
  addEventListener(ev,fn){ this._ev[ev]=fn; }, dispatch(ev,e){ if(this._ev[ev]) this._ev[ev](e); },
  setPointerCapture(){}, releasePointerCapture(){},
  appendChild(){}, insertBefore(){}, querySelector(){return null;}, querySelectorAll(){return [];},
  getBoundingClientRect(){ return {left:0,right:400,top:0,bottom:48,width:400,height:48}; }, firstChild:null, style:{setProperty(){}, removeProperty(){}} }; _all.push(el); return el; }
let _bar=null;
function _mkBar(){ if(!_bar){ _bar=_fakeEl('div'); } return _bar; }
_mkBar();
global.document = { readyState:'complete', hidden:false, addEventListener(){},
  getElementById(id){ if(id==='projectBar') return _mkBar(); return _mounted; },
  createElement(t){ const e=_fakeEl(t); if(t!=='img') _mounted=e; return e; },
  querySelectorAll(){ return _mounted?[_mounted]:[]; } };
__SRC__
const TP = window.TofuPet;
// The pet element is the one that got a 'pointerdown' handler wired (_wireDrag).
const pet = _all.filter(function(e){ return e._ev && e._ev.pointerdown; })[0];
if (!pet) { console.log('DRAG ' + JSON.stringify({ draggedX:-1, stateDuringDrag:'NO_PET' })); process.exit(0); }
// 1) DRAG: down at x=100, move to x=180 (well past slop), then up.
pet.dispatch('pointerdown', { button:0, pointerId:1, clientX:100 });
pet.dispatch('pointermove', { pointerId:1, clientX:180, movementX:80, cancelable:true, preventDefault(){} });
const draggedX = TP.getState().x;
const stateDuringDrag = TP.getState().state;
pet.dispatch('pointerup', { pointerId:1, clientX:180 });
console.log('DRAG ' + JSON.stringify({ draggedX, stateDuringDrag }));
process.exit(0);
'''
        script = harness.replace("__SRC__", src_text)
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             cwd=str(REPO), timeout=20)
        assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
        line = [ln for ln in out.stdout.splitlines() if ln.startswith("DRAG ")][-1]
        return json.loads(line[len("DRAG "):])

    src = PET_JS.read_text()
    assert "pointerdown" in src and "'drag'" in src, "drag wiring missing from tofu-pet.js"
    r = drag_probe(src)
    assert r["stateDuringDrag"] == "drag", \
        f"moving past the slop must enter the 'drag' state, got {r['stateDuringDrag']!r}"
    # x followed the pointer: down grabbed at ~100, moved to 180 → x should be near 180.
    assert r["draggedX"] > 150, \
        f"the kitten did not follow the pointer while dragging (x={r['draggedX']})"

    # NEUTER: make the slop infinite so a move never becomes a drag.
    neut = src.replace("var DRAG_SLOP = 4;", "var DRAG_SLOP = 99999;", 1)
    assert neut != src, "neuter did not match DRAG_SLOP"
    rn = drag_probe(neut)
    assert rn["stateDuringDrag"] != "drag", \
        f"neutered build still entered drag (state={rn['stateDuringDrag']}) — test does not bite"


def test_scene_switch_button_in_index_invokes_cycleDecor():
    """The visible scene-switcher must exist in index.html as a real control
    that calls TofuPet.cycleDecor() (NOT a hidden right-click), carries a label
    span + an inline SVG icon (no emoji/glyph, per CLAUDE.md §3.4), and uses the
    .icon-box centring utility."""
    html = INDEX.read_text()
    assert 'class="project-bar-btn scene-switch-btn icon-box"' in html or \
           'scene-switch-btn' in html and 'icon-box' in html, \
        "scene-switch button missing / not using .icon-box"
    # It must invoke the EXISTING cycle path, not a new state mechanism.
    assert "TofuPet" in html and "cycleDecor()" in html, \
        "scene switcher must call TofuPet.cycleDecor()"
    # The label + inline SVG (no emoji glyph).
    assert 'class="scene-switch-label"' in html, "scene switcher needs a visible label"
    # The button's own markup is an <svg>, never a raw glyph.
    m = re.search(r'scene-switch-btn[^>]*>(.*?)</button>', html, re.S)
    assert m, "could not isolate scene-switch button markup"
    assert "<svg" in m.group(1), "scene switcher icon must be inline SVG"


def test_scene_switch_button_label_syncs_through_single_state_path():
    """Driving TofuPet.cycleDecor() must update the visible button's label,
    data-scene marker, and tooltip via the ONE _applyDecor state path (no second
    source of truth). We stub a fake button with a label span, cycle the scene,
    and assert the button reflects the new scene name. A NEUTER (remove the
    _syncSceneButton() call from _applyDecor) must break the sync."""
    def button_after_cycle(src_text):
        harness = r'''
'use strict';
const RealDate = Date;
class FakeDate extends RealDate { static now(){ return 0; } getHours(){ return 14; } }
global.Date = FakeDate;
global.window = { matchMedia(){ return {matches:true, addEventListener(){}, addListener(){}}; }, addEventListener(){} };
global.BASE_PATH = '';
global.requestAnimationFrame = function(){ return 0; };
global.cancelAnimationFrame = function(){};
global.ResizeObserver = function(){ return {observe(){}, disconnect(){}}; };
global.localStorage = { _d:{}, getItem(k){return this._d[k]||null;}, setItem(k,v){this._d[k]=v;} };
global.Image = function(){ return { set src(v){}, get src(){return '';} }; };
// A fake scene-switch button carrying a label span.
const _label = { textContent: '' };
const _btn = { _attrs:{}, setAttribute(k,v){this._attrs[k]=v;}, getAttribute(k){return this._attrs[k];},
  querySelector(sel){ return sel === '.scene-switch-label' ? _label : null; } };
let _mounted=null;
function _fakeEl(){ return { _attrs:{}, tagName:'', offsetWidth:30,
  set src(v){}, get src(){return '';},
  setAttribute(k,v){this._attrs[k]=v;}, getAttribute(k){return this._attrs[k];},
  addEventListener(){}, appendChild(){}, insertBefore(){}, querySelector(){return null;}, querySelectorAll(){return [];},
  getBoundingClientRect(){ return {left:0,right:400,top:0,bottom:48,width:400,height:48}; }, firstChild:null, style:{setProperty(){}, removeProperty(){}} }; }
global.document = { readyState:'complete', hidden:false, addEventListener(){},
  getElementById(id){ if(id==='projectBar') return _fakeEl(); return _mounted; },
  createElement(t){ const e=_fakeEl(); if(t!=='img') _mounted=e; return e; },
  querySelectorAll(sel){ if(sel === '.scene-switch-btn') return [_btn]; return _mounted?[_mounted]:[]; } };
__SRC__
const TP = window.TofuPet;
TP.setDecor('meadow');            // known start
const start = { label:_label.textContent, scene:_btn.getAttribute('data-scene') };
const now = TP.cycleDecor();      // meadow -> pool
console.log(JSON.stringify({
  now, label:_label.textContent, scene:_btn.getAttribute('data-scene'),
  title:_btn.getAttribute('title'), start,
  expectLabel: TP.SCENE_LABELS[now]
}));
process.exit(0);
'''
        script = harness.replace("__SRC__", src_text)
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             cwd=str(REPO), timeout=20)
        assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
        line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
        return json.loads(line)

    src = PET_JS.read_text()
    r = button_after_cycle(src)
    assert r["now"] == "pool", f"cycle meadow->pool expected, got {r['now']}"
    assert r["label"] == r["expectLabel"] == "Pool", \
        f"button label did not sync to the cycled scene: {r}"
    assert r["scene"] == "pool", f"data-scene marker did not sync: {r}"
    assert "Pool" in (r["title"] or ""), f"tooltip did not sync: {r}"

    # NEUTER: drop the _syncSceneButton() call from _applyDecor → label frozen.
    neut = src.replace("    _syncSceneButton();\n  }", "    /* neutered sync */\n  }", 1)
    assert neut != src, "neuter did not match the _syncSceneButton call in _applyDecor"
    rn = button_after_cycle(neut)
    assert rn["label"] != "Pool", \
        f"neutered build still synced the label ({rn['label']}) — test does not bite"


def test_project_bar_close_button_is_svg_not_glyph():
    """The project-bar close button must be an inline SVG, not a raw ✕ glyph
    (CLAUDE.md §3.4 bans glyph controls)."""
    html = INDEX.read_text()
    m = re.search(r'<button class="project-bar-close[^>]*>(.*?)</button>', html, re.S)
    assert m, "could not find the project-bar close button"
    inner = m.group(1)
    assert "<svg" in inner, "close button must contain an inline SVG"
    assert "\u2715" not in inner and "\u2716" not in inner and "\u00d7" not in inner, \
        "close button still uses a raw glyph"


def test_walk_cycle_has_four_keypose_frames_on_disk():
    """The walk is a real multi-frame cycle (contact/down/passing/up), NOT one
    PNG sliding. All four keypose frames must exist on disk."""
    r = _run()
    pet_dir = REPO / "static" / "icons" / "pet" / "oneko"
    walk = r["walkFrames"]
    assert len(walk) >= 4, f"walk cycle must have >=4 keyposes, got {walk}"
    for w in walk:
        f = pet_dir / f"oneko-{w}.png"
        assert f.exists() and f.stat().st_size > 0, f"missing/empty walk frame: {f}"


def test_walk_cycle_ADVANCES_frames_while_walking():
    """The core of the owner's ask: while state=='walk', the frame ticker must
    ADVANCE through the walk keyposes on the rAF loop (not hold one frame). We
    run the REAL module with a controllable rAF clock + reduced-motion OFF,
    drive several animation ticks, and assert the <img> src cycles through more
    than one distinct walk frame. A NEUTER (remove the advance) must break it."""
    def frames_seen(src_text):
        harness = r'''
'use strict';
let _t = 0;
const RealDate = Date;
class FakeDate extends RealDate { static now(){ return _t; } getHours(){ return 14; } }
global.Date = FakeDate;
let _cbs = [];
global.requestAnimationFrame = function(cb){ _cbs.push(cb); return _cbs.length; };
global.cancelAnimationFrame = function(){};
global.window = { matchMedia(){ return {matches:false, addEventListener(){}, addListener(){}}; }, addEventListener(){} };
global.BASE_PATH = '';
global.ResizeObserver = function(){ return {observe(){}, disconnect(){}}; };
global.localStorage = { _d:{}, getItem(k){return this._d[k]||null;}, setItem(k,v){this._d[k]=v;} };
global.Image = function(){ return { set src(v){}, get src(){return '';} }; };
let _mounted=null; const _seen=[];
function _fakeEl(tag){ return { _attrs:{}, tagName:tag||'', offsetWidth:30,
  set src(v){ if(this.tagName==='img'){ _seen.push(v); } this._src=v; }, get src(){ return this._src||''; },
  setAttribute(k,v){this._attrs[k]=v;}, getAttribute(k){return this._attrs[k];},
  addEventListener(){}, appendChild(){}, insertBefore(){}, querySelector(){return null;}, querySelectorAll(){return [];},
  getBoundingClientRect(){ return {left:0,right:400,top:0,bottom:48,width:400,height:48}; }, firstChild:null, style:{setProperty(){}, removeProperty(){}} }; }
global.document = { readyState:'complete', hidden:false, addEventListener(){},
  getElementById(id){ if(id==='projectBar') return _fakeEl(); return _mounted; },
  createElement(t){ const e=_fakeEl(t); if(t!=='img') _mounted=e; return e; },
  querySelectorAll(){ return _mounted?[_mounted]:[]; } };
__SRC__
// Force a walk state, then pump the rAF loop advancing the clock ~150ms/tick.
const TP = window.TofuPet;
_seen.length = 0;
// run ~12 frames of ~150ms each = enough to cross multiple 150ms keyposes
for (let k=0; k<12; k++){
  _t += 160;
  const cb = _cbs.shift();
  if (cb) cb(_t);
}
const walkSeen = _seen.filter(s => /walk\d/.test(s));
const distinct = Array.from(new Set(walkSeen));
console.log(JSON.stringify({ distinct: distinct.length, walkSeen: walkSeen.length }));
process.exit(0);
'''
        script = harness.replace("__SRC__", src_text)
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             cwd=str(REPO), timeout=20)
        assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
        line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
        return json.loads(line)

    src = PET_JS.read_text()
    real = frames_seen(src)
    assert real["distinct"] >= 2, \
        f"walk did not cycle through multiple frames (distinct={real['distinct']}) — it's a static slide"

    # NEUTER: remove the frame-advance calls (walk + chase share the mechanism)
    # → the walk holds one frame.
    neut = src.replace("_setWalkFrame(_walkIdx + adv);", "/* neutered advance */")
    assert neut != src, "neuter did not match the frame-advance call"
    broken = frames_seen(neut)
    assert broken["distinct"] <= 1, \
        f"neutered build still cycled (distinct={broken['distinct']}) — test does not bite"


def test_wander_clamps_to_a_safe_track():
    """The wander engine must clamp the pet's x into [min,max] so it can never
    roam under the controls. NEUTER the clamp → x is allowed past max."""
    src = PET_JS.read_text()
    assert "W.max" in src and "W.min" in src, "wander bounds missing"
    # the loop must turn the pet around at the bounds
    assert "_enter('turn')" in src, "pet must turn at soft bounds, not clamp-freeze"


def test_deep_night_sleeps():
    """0–5h → sleeping regardless of a healthy mood."""
    r = _run(hour=2, patch={"mood": 80, "energy": 90})
    assert r["expr"] == "sleeping", r


def test_night_is_sleepy():
    r = _run(hour=22, patch={"mood": 70, "energy": 70})
    assert r["expr"] == "sleepy", r


def test_morning_daytime_defaults():
    """Morning is upbeat; afternoon is calm idle (neutral mood, awake)."""
    assert _run(hour=9, patch={"mood": 60, "energy": 80})["expr"] == "happy"
    assert _run(hour=14, patch={"mood": 60, "energy": 80})["expr"] == "idle"


def test_low_mood_is_sad_during_day():
    """A daytime pet with a depleted mood shows sad (mood branch beats time)."""
    r = _run(hour=14, patch={"mood": 10, "energy": 80})
    assert r["expr"] == "sad", r


def test_loading_activity_beats_the_clock():
    """Activity has top priority — even at 2am, 'loading' shows thinking."""
    r = _run(hour=2, patch={"activity": "loading"})
    assert r["expr"] == "thinking", r


def test_success_celebrates():
    r = _run(hour=14, patch={"activity": "success"})
    assert r["expr"] == "celebrating", r


def test_registered_in_bundler_manifest():
    assert "'tofu-pet.js'" in BUNDLER.read_text(), \
        "tofu-pet.js missing from _BUNDLE_FILES — it would load as a silent no-op"


def test_registered_in_index_html_fallback():
    assert "tofu-pet.js" in INDEX.read_text(), \
        "tofu-pet.js missing from index.html dev-fallback <script> tags"


def test_neuter_breaks_night_sleep():
    """NEUTER: remove the deep-night `return 'sleeping'` branch → a 2am pet no
    longer sleeps (falls through to a daytime expression), proving the FSM
    night test actually bites."""
    src = PET_JS.read_text()
    neutered = src.replace("if (tb.expr === 'sleeping') return 'sleeping';",
                           "/* neutered */", 1)
    assert neutered != src, "neuter did not match the sleep branch"
    import json
    script = (_HARNESS
              .replace("__SRC__", neutered)
              .replace("__HOUR__", "2")
              .replace("__NOW__", "0")
              .replace("__PATCH__", json.dumps({"mood": 80, "energy": 90})))
    out = subprocess.run(["node", "-e", script + "\nprocess.exit(0);"],
                         capture_output=True, text=True, cwd=str(REPO), timeout=20)
    assert out.returncode == 0, f"node failed: {out.stderr}"
    line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    expr = json.loads(line)["expr"]
    assert expr != "sleeping", f"neutered build still slept ({expr}) — test does not bite"


if __name__ == "__main__":
    for fn in [test_expression_frames_exist_on_disk, test_extra_cat_pose_frames_exist_on_disk,
               test_curious_alias_resolves_to_a_real_frame,
               test_decor_scene_assets_exist, test_decor_css_wires_every_scene,
               test_control_lift_is_allowlist_not_denylist,
               test_pack_switcher_fully_removed,
               test_dom_footstep_fx_layer_fully_removed,
               test_project_brain_button_removed_from_bar,
               test_pet_is_draggable,
               test_cat_chases_the_scene_critter,
               test_scene_switch_button_in_index_invokes_cycleDecor,
               test_scene_switch_button_label_syncs_through_single_state_path,
               test_project_bar_close_button_is_svg_not_glyph,
               test_walk_cycle_has_four_keypose_frames_on_disk,
               test_walk_cycle_ADVANCES_frames_while_walking,
               test_wander_clamps_to_a_safe_track,
               test_deep_night_sleeps,
               test_night_is_sleepy, test_morning_daytime_defaults,
               test_low_mood_is_sad_during_day, test_loading_activity_beats_the_clock,
               test_success_celebrates, test_registered_in_bundler_manifest,
               test_registered_in_index_html_fallback, test_neuter_breaks_night_sleep]:
        fn()
        print("PASS", fn.__name__)
    print("ALL GREEN")
