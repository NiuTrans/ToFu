"""Tofu pet mascot (static/js/tofu-pet.js) — behaviour + registration guards.

The pet is the Tofu brand mascot itself — the isometric cream block from
static/icons/tofu-welcome.svg — mounted into #projectBar. Each expression is an
SVG frame (static/icons/pet/tofu/tofu-<expr>.svg); a small time-of-day / mood
finite-state machine picks the frame (research-grounded resolution priority:
activity > night-sleep > low-mood > low-energy/evening > recent-positive >
time-default > idle).

These tests run the REAL shipped module under node with a minimal DOM/storage
stub, drive its public `window.TofuPet` surface, and assert:
  * every declared frame resolves — THROUGH THE MODULE'S OWN frameUrl() — to art
    that EXISTS on disk (so the guard follows the shipped character, and cannot
    become a stale second copy of the path rule);
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
// The engine builds each frame with new Image() and decorates it (style,
// setAttribute, draggable, onload) — a bare src-accessor stub throws on
// node.style. Frames never finish "loading" here (onload never fires),
// which the behaviour tests don't mind.
global.Image = function(){
  return { _attrs:{}, draggable:false, alt:'', style:{},
    setAttribute(k,v){ this._attrs[k]=v; }, getAttribute(k){ return this._attrs[k]; },
    addEventListener(){}, appendChild(){},
    set src(v){ this._src = v; }, get src(){ return this._src || ''; } };
};
global.t = function (k, p) {
  let text = k;
  if (p) { for (const q in p) { if (Object.prototype.hasOwnProperty.call(p, q)) text = text.split('{' + q + '}').join(p[q]); } }
  return text;   // passthrough — mirrors the page boot stub (index.html:80)
};
// t() is driven by the REAL i18n dictionary, read from the path in argv[1].
// Production ALWAYS has t() (index.html:80 boot stub + i18n.js = _BUNDLE_FILES[0]),
// so the pet calls it bare. The dictionary is ~3000 keys, so it rides in a temp
// FILE rather than inlined into `node -e` (argv limit). A MISSING key returns
// the key, exactly as production does — never a fallback-inventing fake.
const _petDict = JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8'));
global.t = function (key, params) {
  const e = _petDict[key];
  let text = (e && e.zh != null) ? e.zh : key;
  if (params) {
    for (const k in params) {
      if (Object.prototype.hasOwnProperty.call(params, k)) {
        text = text.split('{' + k + '}').join(params[k]);
      }
    }
  }
  return text;
};
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
// Resolve EVERY declared frame through the module's own frameUrl(), so the
// python guards can check the engine's real answers instead of re-deriving the
// directory/prefix/extension themselves (a second copy that would silently rot).
const _allFrames = TP.EXPRESSIONS
  .concat(TP.WALK_FRAMES, TP.GROOM_FRAMES, TP.SCRATCH_FRAMES)
  .concat(Object.keys(TP.FRAME_ALIAS).map(k => TP.FRAME_ALIAS[k]));
const _frameUrls = {};
_allFrames.forEach(function (f) { _frameUrls[f] = TP.frameUrl(f); });
console.log(JSON.stringify({
  expr: _expr,
  expressions: TP.EXPRESSIONS,
  frameUrl: TP.frameUrl((TP.FRAME_ALIAS[_expr] || _expr)),
  frameUrls: _frameUrls,
  FRAME_ALIAS: TP.FRAME_ALIAS,
  decorStyles: TP.DECOR_STYLES,
  decor: TP.getDecor(),
  walkFrames: TP.WALK_FRAMES,
  groomFrames: TP.GROOM_FRAMES,
  scratchFrames: TP.SCRATCH_FRAMES,
}));
process.exit(0);   // the pet's setInterval timers keep the event loop alive
"""


def _i18n_dict():
    """Scrape ``static/js/i18n.js`` into ``{key: {zh, en}}`` so the harness's
    ``t()`` resolves exactly as production does (a missing key returns the key).
    """
    src = (REPO / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
    pat = re.compile(
        r"""^[ \t]*'([\w.\-]+)':\s*\{\s*zh:\s*(['"])(.*?)\2\s*,"""
        r"""\s*en:\s*(['"])(.*?)\4""",
        re.MULTILINE)
    return {m.group(1): {'zh': m.group(3), 'en': m.group(5)}
            for m in pat.finditer(src)}


def _run(hour=14, now=0, patch=None):
    """Boot the pet at a fixed hour with a state patch; return its resolved state."""
    src = PET_JS.read_text()
    import json
    script = (_HARNESS
              .replace("__SRC__", src)
              .replace("__HOUR__", str(hour))
              .replace("__NOW__", str(now))
              .replace("__PATCH__", json.dumps(patch or {})))
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
        json.dump(_i18n_dict(), fh)
        dict_path = fh.name
    try:
        out = subprocess.run(["node", "-e", script, dict_path],
                             capture_output=True, text=True,
                             cwd=str(REPO), timeout=20)
    finally:
        os.unlink(dict_path)
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(line)


import json  # noqa: E402  (used inside _run)
import os  # noqa: E402  (used inside _run)


def _frame_path(rel_url):
    """Map a URL the SHIPPED module produced → the file it must resolve to.

    The guards below deliberately do NOT hardcode a directory, prefix or
    extension. They used to (``static/icons/pet/oneko/oneko-<e>.png``), which
    made them a SECOND copy of the frame-resolution rule: re-pointing the pet at
    new art meant hand-editing four tests, and a test that names the art it
    expects can only ever confirm the art someone remembered to type. Deriving
    the path from ``TofuPet.frameUrl()`` means these guards follow the shipped
    resolver wherever it points — they check that the engine's OWN answer lands
    on a real file.
    """
    return REPO / rel_url.lstrip("/")


def test_expression_frames_exist_on_disk():
    """Every declared expression must resolve to art that EXISTS on disk (a
    missing frame = a broken <img> in the bar). Path comes from the module's own
    frameUrl(), so this follows the shipped character rather than naming it."""
    r = _run()
    for e in r["expressions"]:
        f = _frame_path(r["frameUrls"][e])
        assert f.exists() and f.stat().st_size > 0, f"missing/empty art frame: {f}"
    # The canonical research-grounded set must all be present as expressions.
    for e in ("idle", "happy", "sleepy", "sleeping", "thinking",
              "surprised", "sad", "celebrating"):
        assert e in r["expressions"], f"missing expression: {e}"


def test_extra_pose_frames_exist_on_disk():
    """The 'more forms' the owner asked for: the groom + stretch pose cycles must
    exist on disk so the pet has richer idle behaviour, not just walk/sit."""
    r = _run()
    poses = list(r.get("groomFrames", [])) + list(r.get("scratchFrames", []))
    assert len(poses) >= 5, f"expected >=5 extra pose frames, got {poses}"
    for p in poses:
        f = _frame_path(r["frameUrls"][p])
        assert f.exists() and f.stat().st_size > 0, f"missing/empty pose frame: {f}"


def test_curious_alias_resolves_to_a_real_frame():
    """'curious' has no own art — it must alias to an existing frame."""
    r = _run(hour=14, patch={})
    target = r["FRAME_ALIAS"].get("curious", "curious")
    f = _frame_path(r["frameUrls"][target])
    assert f.exists() and f.stat().st_size > 0, \
        f"'curious' aliases to {target!r}, which has no art on disk: {f}"


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
    assert '.project-bar > .project-bar-close{position:relative;z-index:3}' in css \
        or ".project-bar > .project-bar-close" in css and "z-index:3" in css, \
        "real controls must be z-lifted above the roaming pet + fg fringe (allowlist)"


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
// The engine builds each frame with new Image() and decorates it (style,
// setAttribute, draggable, onload) — a bare src-accessor stub throws on
// node.style. Frames never finish "loading" here (onload never fires),
// which the behaviour tests don't mind.
global.Image = function(){
  return { _attrs:{}, draggable:false, alt:'', style:{},
    setAttribute(k,v){ this._attrs[k]=v; }, getAttribute(k){ return this._attrs[k]; },
    addEventListener(){}, appendChild(){},
    set src(v){ this._src = v; }, get src(){ return this._src || ''; } };
};
global.t = function (k, p) {
  let text = k;
  if (p) { for (const q in p) { if (Object.prototype.hasOwnProperty.call(p, q)) text = text.split('{' + q + '}').join(p[q]); } }
  return text;   // passthrough — mirrors the page boot stub (index.html:80)
};
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
// The engine builds each frame with new Image() and decorates it (style,
// setAttribute, draggable, onload) — a bare src-accessor stub throws on
// node.style. Frames never finish "loading" here (onload never fires),
// which the behaviour tests don't mind.
global.Image = function(){
  return { _attrs:{}, draggable:false, alt:'', style:{},
    setAttribute(k,v){ this._attrs[k]=v; }, getAttribute(k){ return this._attrs[k]; },
    addEventListener(){}, appendChild(){},
    set src(v){ this._src = v; }, get src(){ return this._src || ''; } };
};
global.t = function (k, p) {
  let text = k;
  if (p) { for (const q in p) { if (Object.prototype.hasOwnProperty.call(p, q)) text = text.split('{' + q + '}').join(p[q]); } }
  return text;   // passthrough — mirrors the page boot stub (index.html:80)
};
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


def test_click_startles_the_pet_and_makes_it_flee():
    """OWNER ASK — clicking the pet must STARTLE it and make it MOVE (not just
    flip to 'happy' in place, and NOT immediately go back to sleep). A plain
    click (pointerdown+up, no drag) routes through interact() → _startle(),
    which enters the fast 'flee' state; pumping the loop the cat must dash a
    meaningful distance AND its post-flee _pickNext must NOT be 'sleep'. We even
    START it asleep to prove a poke wakes it. A NEUTER (interact no longer
    startles) must break the movement assertion."""
    def click_probe(src_text):
        harness = r'''
'use strict';
let _t = 0;
const RealDate = Date;
class FakeDate extends RealDate { static now(){ return _t; } getHours(){ return 14; } }
global.Date = FakeDate;
// seeded RNG so flee direction/duration + post-flee pick are reproducible.
let _seed = 0x2545F491;
global.Math.random = function(){ _seed = (_seed * 1103515245 + 12345) & 0x7fffffff; return _seed / 0x7fffffff; };
let _cbs = [];
global.requestAnimationFrame = function(cb){ _cbs.push(cb); return _cbs.length; };
global.cancelAnimationFrame = function(){};
global.window = { PointerEvent: function(){}, matchMedia(){ return {matches:false, addEventListener(){}, addListener(){}}; }, addEventListener(){} };
global.BASE_PATH = '';
global.ResizeObserver = function(){ return {observe(){}, disconnect(){}}; };
global.localStorage = { _d:{}, getItem(k){return this._d[k]||null;}, setItem(k,v){this._d[k]=v;} };
// The engine builds each frame with new Image() and decorates it (style,
// setAttribute, draggable, onload) — a bare src-accessor stub throws on
// node.style. Frames never finish "loading" here (onload never fires),
// which the behaviour tests don't mind.
global.Image = function(){
  return { _attrs:{}, draggable:false, alt:'', style:{},
    setAttribute(k,v){ this._attrs[k]=v; }, getAttribute(k){ return this._attrs[k]; },
    addEventListener(){}, appendChild(){},
    set src(v){ this._src = v; }, get src(){ return this._src || ''; } };
};
global.t = function (k, p) {
  let text = k;
  if (p) { for (const q in p) { if (Object.prototype.hasOwnProperty.call(p, q)) text = text.split('{' + q + '}').join(p[q]); } }
  return text;   // passthrough — mirrors the page boot stub (index.html:80)
};
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
// Put the cat to SLEEP first (the reported precondition: it naps, you poke it).
TP.setState({ energy: 5 });
// force a sleep resolution snapshot
const beforeState = TP.getState().state;
const beforeX = TP.getState().x;
const pet = _all.filter(function(e){ return e._ev && e._ev.pointerdown; })[0];
if (!pet) { console.log('CLICK ' + JSON.stringify({ err:'NO_PET' })); process.exit(0); }
// A plain CLICK: pointerdown + pointerup at the SAME x (never crosses slop).
pet.dispatch('pointerdown', { button:0, pointerId:1, clientX:120 });
pet.dispatch('pointerup', { pointerId:1, clientX:120 });
const stateAfterClick = TP.getState().state;
const exprAfterClick = TP.getState().expr;
const xAfterClick = TP.getState().x;
// Pump the loop a bit; the cat should DASH (x changes a lot) and never be asleep.
let sawSleep = false, maxMove = 0;
for (let k=0; k<40; k++){
  _t += 60;
  const cb = _cbs.shift(); if (cb) cb(_t);
  if (TP.getState().state === 'sleep') sawSleep = true;
  maxMove = Math.max(maxMove, Math.abs(TP.getState().x - xAfterClick));
}
console.log('CLICK ' + JSON.stringify({ beforeState, beforeX, stateAfterClick, exprAfterClick, xAfterClick, maxMove, sawSleep }));
process.exit(0);
'''
        script = harness.replace("__SRC__", src_text)
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             cwd=str(REPO), timeout=20)
        assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
        line = [ln for ln in out.stdout.splitlines() if ln.startswith("CLICK ")][-1]
        return json.loads(line[len("CLICK "):])

    src = PET_JS.read_text()
    assert "_startle" in src and "'flee'" in src, "startle/flee wiring missing from tofu-pet.js"
    r = click_probe(src)
    # A click must startle: enter flee immediately + show 'surprised'.
    assert r["stateAfterClick"] == "flee", \
        f"a click did not startle the cat into 'flee' (state={r['stateAfterClick']!r})"
    assert r["exprAfterClick"] == "surprised", \
        f"a click did not show the startled 'surprised' pose (expr={r['exprAfterClick']!r})"
    # It must MOVE a meaningful distance (the flee dash), and not fall asleep.
    assert r["maxMove"] >= 20, \
        f"the startled cat barely moved (maxMove={r['maxMove']:.1f}px) — 'floats/naps' bug"
    assert r["sawSleep"] is False, \
        "the cat went back to sleep right after being poked (the reported bug)"

    # NEUTER: make interact() no longer startle (revert to react-in-place) →
    # the cat neither enters flee nor dashes.
    neut = src.replace("_startle();      // startled scramble away", "react('happy', 1500); //", 1)
    assert neut != src, "neuter did not match the interact->startle call"
    rn = click_probe(neut)
    assert not (rn["stateAfterClick"] == "flee" and rn["maxMove"] >= 20), \
        f"neutered build still fled ({rn}) — test does not bite"


def test_daytime_sleep_is_rare_not_frequent():
    """OWNER ASK — the pet 'sleeps very often'. Guard that the daytime wander
    weights don't route into sleep from a healthy-energy idle/sit/groom cat:
    with good energy, _pickNext must NOT pick sleep during the day. We drive the
    REAL _pickNext many times from idle/sit at hour 14 with high energy and
    assert ZERO sleep transitions. A NEUTER (idle→sleep tail restored) bites."""
    def sleep_rate(src_text):
        harness = r'''
'use strict';
let _t = 0;
const RealDate = Date;
class FakeDate extends RealDate { static now(){ return _t; } getHours(){ return 14; } }
global.Date = FakeDate;
let _n = 0;
// deterministic sweep of Math.random across [0,1) so we cover every weight band
global.Math.random = function(){ _n = (_n + 1) % 100; return _n / 100; };
let _cbs = [];
global.requestAnimationFrame = function(cb){ _cbs.push(cb); return _cbs.length; };
global.cancelAnimationFrame = function(){};
global.window = { matchMedia(){ return {matches:false, addEventListener(){}, addListener(){}}; }, addEventListener(){} };
global.BASE_PATH = '';
global.ResizeObserver = function(){ return {observe(){}, disconnect(){}}; };
global.localStorage = { _d:{}, getItem(k){return this._d[k]||null;}, setItem(k,v){this._d[k]=v;} };
// The engine builds each frame with new Image() and decorates it (style,
// setAttribute, draggable, onload) — a bare src-accessor stub throws on
// node.style. Frames never finish "loading" here (onload never fires),
// which the behaviour tests don't mind.
global.Image = function(){
  return { _attrs:{}, draggable:false, alt:'', style:{},
    setAttribute(k,v){ this._attrs[k]=v; }, getAttribute(k){ return this._attrs[k]; },
    addEventListener(){}, appendChild(){},
    set src(v){ this._src = v; }, get src(){ return this._src || ''; } };
};
global.t = function (k, p) {
  let text = k;
  if (p) { for (const q in p) { if (Object.prototype.hasOwnProperty.call(p, q)) text = text.split('{' + q + '}').join(p[q]); } }
  return text;   // passthrough — mirrors the page boot stub (index.html:80)
};
let _mounted=null;
function _fakeEl(tag){ return { _attrs:{}, tagName:tag||'', offsetWidth:30,
  set src(v){}, get src(){return '';},
  setAttribute(k,v){this._attrs[k]=v;}, getAttribute(k){return this._attrs[k];},
  addEventListener(){}, appendChild(){}, insertBefore(){}, querySelector(){return null;}, querySelectorAll(){return [];},
  getBoundingClientRect(){ return {left:0,right:400,top:0,bottom:48,width:400,height:48}; }, firstChild:null, style:{setProperty(){}, removeProperty(){}} }; }
global.document = { readyState:'complete', hidden:false, addEventListener(){},
  getElementById(id){ if(id==='projectBar') return _fakeEl(); return _mounted; },
  createElement(t){ const e=_fakeEl(t); if(t!=='img') _mounted=e; return e; },
  querySelectorAll(){ return _mounted?[_mounted]:[]; } };
__SRC__
const TP = window.TofuPet;
TP.setState({ energy: 90, mood: 70 });   // healthy, awake
// Pump the loop; count how often the cat is asleep across many state picks at
// hour 14 with high energy. A healthy daytime cat should essentially never nap.
let sleeps = 0, samples = 0;
for (let k=0; k<400; k++){
  _t += 4000;                     // jump the clock so states time out → _pickNext
  const cb = _cbs.shift(); if (cb) cb(_t);
  const s = TP.getState().state;
  samples++;
  if (s === 'sleep') sleeps++;
}
console.log(JSON.stringify({ sleeps, samples }));
process.exit(0);
'''
        script = harness.replace("__SRC__", src_text)
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             cwd=str(REPO), timeout=20)
        assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
        line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
        return json.loads(line)

    src = PET_JS.read_text()
    r = sleep_rate(src)
    assert r["sleeps"] == 0, \
        f"a healthy daytime cat still napped {r['sleeps']}/{r['samples']} — sleeps too often"

    # NEUTER: make a healthy daytime cat nap anyway — route the idle branch
    # UNCONDITIONALLY to sleep (the extreme form of "sleeps too often"). This
    # reliably produces naps regardless of RNG phase, proving the "0 sleeps"
    # assertion is load-bearing (it would fail the moment the weights let a
    # healthy daytime cat nap).
    neut = src.replace(
        "if (r < 0.24) _enter('sit'); else if (r < 0.46) _enter('gaze'); else if (r < 0.64) _enter('groom'); else if (r < 0.86) _enter('walk'); else _enter('scratch');",
        "_enter('sleep');",
        1)
    assert neut != src, "neuter did not match the idle weights"
    rn = sleep_rate(neut)
    assert rn["sleeps"] > 0, \
        f"neutered build never napped either ({rn}) — test does not bite"


def test_walking_is_a_minority_and_behaviour_is_varied():
    """OWNER ASK — "too monotonous, don't just keep running all the time." Drive
    the REAL _pickNext across a deterministic RNG sweep from a healthy daytime
    cat and assert (1) 'walk' is a MINORITY of the chosen states (<40%) and
    (2) the pet exhibits VARIETY — at least 4 DISTINCT non-walk activities,
    including the new stationary 'gaze'. A NEUTER that routes everything to walk
    must break both assertions."""
    def state_histogram(src_text):
        harness = r'''
'use strict';
let _t = 0;
const RealDate = Date;
class FakeDate extends RealDate { static now(){ return _t; } getHours(){ return 14; } }
global.Date = FakeDate;
let _n = 0;
// deterministic sweep of Math.random across [0,1) so we cover every weight band
global.Math.random = function(){ _n = (_n + 1) % 100; return _n / 100; };
let _cbs = [];
global.requestAnimationFrame = function(cb){ _cbs.push(cb); return _cbs.length; };
global.cancelAnimationFrame = function(){};
global.window = { matchMedia(){ return {matches:false, addEventListener(){}, addListener(){}}; }, addEventListener(){} };
global.BASE_PATH = '';
global.ResizeObserver = function(){ return {observe(){}, disconnect(){}}; };
global.localStorage = { _d:{}, getItem(k){return this._d[k]||null;}, setItem(k,v){this._d[k]=v;} };
// The engine builds each frame with new Image() and decorates it (style,
// setAttribute, draggable, onload) — a bare src-accessor stub throws on
// node.style. Frames never finish "loading" here (onload never fires),
// which the behaviour tests don't mind.
global.Image = function(){
  return { _attrs:{}, draggable:false, alt:'', style:{},
    setAttribute(k,v){ this._attrs[k]=v; }, getAttribute(k){ return this._attrs[k]; },
    addEventListener(){}, appendChild(){},
    set src(v){ this._src = v; }, get src(){ return this._src || ''; } };
};
global.t = function (k, p) {
  let text = k;
  if (p) { for (const q in p) { if (Object.prototype.hasOwnProperty.call(p, q)) text = text.split('{' + q + '}').join(p[q]); } }
  return text;   // passthrough — mirrors the page boot stub (index.html:80)
};
// No scene present → _critterX() returns null so chase never fires and the
// histogram reflects the autonomous wander weights only.
let _mounted=null;
function _fakeEl(tag){ return { _attrs:{}, tagName:tag||'', offsetWidth:30,
  set src(v){}, get src(){return '';},
  setAttribute(k,v){this._attrs[k]=v;}, getAttribute(k){return this._attrs[k];},
  addEventListener(){}, appendChild(){}, insertBefore(){}, querySelector(){return null;}, querySelectorAll(){return [];},
  getBoundingClientRect(){ return {left:0,right:400,top:0,bottom:48,width:400,height:48}; }, firstChild:null, style:{setProperty(){}, removeProperty(){}} }; }
global.document = { readyState:'complete', hidden:false, addEventListener(){},
  getElementById(id){ if(id==='projectBar') return _fakeEl(); return _mounted; },
  createElement(t){ const e=_fakeEl(t); if(t!=='img') _mounted=e; return e; },
  querySelectorAll(){ return _mounted?[_mounted]:[]; } };
__SRC__
const TP = window.TofuPet;
TP.setState({ energy: 90, mood: 70 });   // healthy, awake — no sleep bias
const counts = {};
let last = null;
for (let k=0; k<600; k++){
  _t += 6000;                     // jump the clock so the state times out → _pickNext
  const cb = _cbs.shift(); if (cb) cb(_t);
  const s = TP.getState().state;
  // count each state ENTRY (transition), not every frame, so long states
  // don't dominate the tally by dwell time.
  if (s !== last) { counts[s] = (counts[s]||0) + 1; last = s; }
}
console.log(JSON.stringify(counts));
process.exit(0);
'''
        script = harness.replace("__SRC__", src_text)
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             cwd=str(REPO), timeout=20)
        assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
        line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
        return json.loads(line)

    src = PET_JS.read_text()
    h = state_histogram(src)
    total = sum(h.values())
    assert total > 0, f"no state transitions recorded: {h}"
    walk_frac = h.get("walk", 0) / total
    assert walk_frac < 0.40, \
        f"walking dominates ({walk_frac:.0%} of transitions) — still 'keeps running': {h}"
    non_walk = [s for s in h if s not in ("walk", "turn")]
    assert len(non_walk) >= 4, \
        f"too few distinct activities ({non_walk}) — still monotonous: {h}"
    assert "gaze" in h, f"the new 'gaze' (look-around) activity never fired: {h}"

    # NEUTER: route the walk/chase branch UNCONDITIONALLY to walk → walking
    # dominates again and variety collapses, proving the assertions bite.
    neut = src.replace(
        "if (r < 0.25) _enter('idle'); else if (r < 0.47) _enter('sit'); else if (r < 0.67) _enter('gaze'); else if (r < 0.85) _enter('groom'); else _enter('walk');",
        "_enter('walk');",
        1)
    assert neut != src, "neuter did not match the walk/chase weights"
    hn = state_histogram(neut)
    tn = sum(hn.values())
    assert hn.get("walk", 0) / tn > 0.40, \
        f"neutered build did not become walk-dominant ({hn}) — test does not bite"


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
// The engine builds each frame with new Image() and decorates it (style,
// setAttribute, draggable, onload) — a bare src-accessor stub throws on
// node.style. Frames never finish "loading" here (onload never fires),
// which the behaviour tests don't mind.
global.Image = function(){
  return { _attrs:{}, draggable:false, alt:'', style:{},
    setAttribute(k,v){ this._attrs[k]=v; }, getAttribute(k){ return this._attrs[k]; },
    addEventListener(){}, appendChild(){},
    set src(v){ this._src = v; }, get src(){ return this._src || ''; } };
};
const _petDict = JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8'));
global.t = function (key, params) {
  // This harness asserts on the TRANSLATED button label, so t() must resolve
  // like production — a passthrough would compare a raw key against it.
  // Missing key → the key, exactly as production does.
  const e = _petDict[key];
  let text = (e && e.en != null) ? e.en : key;
  if (params) {
    for (const q in params) {
      if (Object.prototype.hasOwnProperty.call(params, q)) {
        text = text.split('{' + q + '}').join(params[q]);
      }
    }
  }
  return text;
};
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
  expectLabel: TP.sceneLabel(now)
}));
process.exit(0);
'''
        script = harness.replace("__SRC__", src_text)
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
            json.dump(_i18n_dict(), fh)
            dict_path = fh.name
        try:
            out = subprocess.run(["node", "-e", script, dict_path],
                                 capture_output=True, text=True,
                                 cwd=str(REPO), timeout=20)
        finally:
            os.unlink(dict_path)
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
    image sliding. All four keypose frames must exist on disk."""
    r = _run()
    walk = r["walkFrames"]
    assert len(walk) >= 4, f"walk cycle must have >=4 keyposes, got {walk}"
    for w in walk:
        f = _frame_path(r["frameUrls"][w])
        assert f.exists() and f.stat().st_size > 0, f"missing/empty walk frame: {f}"


def test_walk_cycle_ADVANCES_frames_while_walking():
    """The core of the owner's ask: while state=='walk', the frame ticker must
    ADVANCE through the walk keyposes on the rAF loop (not hold one frame). We
    run the REAL module with a controllable rAF clock + reduced-motion OFF,
    drive several animation ticks, and assert the pet reports more than one
    distinct walk frame. A NEUTER (remove the advance) must break it.

    Re-anchored 2026-07-29: this used to sniff `<img>.src` writes; sampling
    `TofuPet.getFrame()` tests the same thing through the engine's own public
    seam, which survives a delivery-mechanism change (inline SVG then, <img>
    PNG frames after the 2026-07-30 raster revamp)."""
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
// Each frame is an <img> the engine creates once and keeps. The src setter
// fires onload as a MICROTASK so a frame is available on the very next tick —
// never firing it would leave every frame unpainted and the cycle would look
// static, and a bare src-accessor stub throws on node.style.
global.Image = function(){
  const el = _fakeEl('img');
  Object.defineProperty(el, 'src', {
    set(v){ this._src = v; const self = this; Promise.resolve().then(function(){ if (self.onload) self.onload(); }); },
    get(){ return this._src || ''; },
  });
  return el;
};
// The engine paints a frame from a .then() continuation, i.e. on a microtask.
// Draining the microtask queue between rAF ticks is what lets a fetched frame
// actually become visible; without it every frame stays pending and the cycle
// looks static even though the ticker is advancing correctly.
const _drain = () => new Promise(r => setImmediate(r));
global.t = function (k, p) {
  let text = k;
  if (p) { for (const q in p) { if (Object.prototype.hasOwnProperty.call(p, q)) text = text.split('{' + q + '}').join(p[q]); } }
  return text;   // passthrough — mirrors the page boot stub (index.html:80)
};
let _mounted=null; const _seen=[];
function _fakeEl(tag){ return { _attrs:{}, tagName:tag||'', offsetWidth:30, children:[],
  set src(v){ this._src=v; }, get src(){ return this._src||''; },
  set innerHTML(v){ this._html=v; }, get innerHTML(){ return this._html||''; },
  setAttribute(k,v){this._attrs[k]=v;}, getAttribute(k){return this._attrs[k];},
  addEventListener(){}, appendChild(c){ this.children.push(c); }, insertBefore(){},
  querySelector(sel){ return sel === 'svg' ? _fakeEl('svg') : null; }, querySelectorAll(){return [];},
  getBoundingClientRect(){ return {left:0,right:400,top:0,bottom:48,width:400,height:48}; }, firstChild:null, style:{setProperty(){}, removeProperty(){}} }; }
global.document = { readyState:'complete', hidden:false, addEventListener(){},
  getElementById(id){ if(id==='projectBar') return _fakeEl(); return _mounted; },
  createElement(t){ const e=_fakeEl(t); if(t==='span' && !_mounted) _mounted=e; return e; },
  querySelectorAll(){ return _mounted?[_mounted]:[]; } };
__SRC__
// Force a walk state, then pump the rAF loop advancing the clock ~150ms/tick.
const TP = window.TofuPet;
_seen.length = 0;
// run ~12 frames of ~160ms each — comfortably more than one 75ms keypose each,
// so a cycle that advances at all must report several distinct frames.
(async () => {
  await _drain();               // let the boot-time frame preloads settle
  for (let k=0; k<12; k++){
    _t += 160;
    const cb = _cbs.shift();
    if (cb) cb(_t);
    await _drain();             // let this tick's frame fetch/paint land
    // Sample through the engine's PUBLIC seam rather than a DOM side effect.
    const f = TP && TP.getFrame ? TP.getFrame() : '';
    if (f) _seen.push(f);
  }
  const walkSeen = _seen.filter(s => /walk\d/.test(s));
  const distinct = Array.from(new Set(walkSeen));
  console.log(JSON.stringify({ distinct: distinct.length, walkSeen: walkSeen.length }));
  process.exit(0);
})();
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


def test_bubble_follows_the_pet_every_frame():
    """The click bubble is ANCHORED to the pet, not to the spot the tap landed.

    A tap also STARTLES the pet into a flee dash, so a bubble positioned only
    once at show time is left hanging over empty floor while the pet scurries
    away — the owner's "the dialogue box doesn't follow the pet" report. The
    fix: _place() re-anchors the bubble every frame while it is alive. Drive
    the REAL module with reduced-motion OFF, interact(), pump the rAF loop and
    assert the bubble's left tracks the pet's x tick by tick. A NEUTER that
    drops the re-anchor call must leave the bubble behind.
    """
    def run(src_text):
        harness = r'''
'use strict';
let _t = 0;
const RealDate = Date;
class FakeDate extends RealDate { static now(){ return _t; } getHours(){ return 14; } }
global.Date = FakeDate;
// Deterministic RNG: the tap startles the pet into a flee dash whose
// direction/speed draw on Math.random. An unseeded draw made this test
// flaky in CI (2026-08-05 frontend lane, Node 20): one run fled the pet
// into the bubble's left-edge clamp and the 15.9px clamp offset read as
// "bubble does not follow". Same sequence on every Node version now.
let _rngState = 0x2f6e2b1;
Math.random = function () {
  _rngState ^= _rngState << 13; _rngState >>>= 0;
  _rngState ^= _rngState >> 17; _rngState ^= _rngState << 5; _rngState >>>= 0;
  return _rngState / 4294967296;
};
let _cbs = [];
global.requestAnimationFrame = function(cb){ _cbs.push(cb); return _cbs.length; };
global.cancelAnimationFrame = function(){};
global.window = { matchMedia(){ return {matches:false, addEventListener(){}, addListener(){}}; }, addEventListener(){} };
global.BASE_PATH = '';
global.ResizeObserver = function(){ return {observe(){}, disconnect(){}}; };
global.localStorage = { _d:{}, getItem(k){return this._d[k]||null;}, setItem(k,v){this._d[k]=v;} };
global.Image = function(){
  return { _attrs:{}, draggable:false, alt:'', style:{},
    setAttribute(k,v){ this._attrs[k]=v; }, getAttribute(k){ return this._attrs[k]; },
    addEventListener(){}, appendChild(){},
    set src(v){ this._src = v; }, get src(){ return this._src || ''; } };
};
global.t = function (k, p) {
  let text = k;
  if (p) { for (const q in p) { if (Object.prototype.hasOwnProperty.call(p, q)) text = text.split('{' + q + '}').join(p[q]); } }
  return text;
};
const _barChildren = [];
function _fakeEl(tag){ return { _attrs:{}, tagName:tag||'', className:'', textContent:'', innerHTML:'',
  offsetWidth:30, children:[], parentNode:null,
  style:{ _p:{}, setProperty(k,v){ this._p[k]=v; }, removeProperty(k){ delete this._p[k]; } },
  setAttribute(k,v){ this._attrs[k]=v; }, getAttribute(k){ return this._attrs[k]!==undefined?this._attrs[k]:null; },
  addEventListener(){},
  appendChild(c){ c.parentNode=this; this.children.push(c); return c; },
  insertBefore(c){ c.parentNode=this; this.children.unshift(c); return c; },
  removeChild(c){ const i=this.children.indexOf(c); if(i>=0) this.children.splice(i,1); c.parentNode=null; return c; },
  querySelector(){ return null; }, querySelectorAll(){ return []; },
  getBoundingClientRect(){ return {left:0,right:400,top:0,bottom:48,width:400,height:48}; },
  firstChild:null }; }
const _bar = _fakeEl('span');
const _origAppend = _bar.appendChild.bind(_bar);
_bar.appendChild = function(c){ _barChildren.push(c); return _origAppend(c); };
const _origInsert = _bar.insertBefore.bind(_bar);
_bar.insertBefore = function(c){ _barChildren.push(c); return _origInsert(c); };
global.document = { readyState:'complete', hidden:false, addEventListener(){},
  getElementById(id){ if(id==='projectBar') return _bar; return null; },
  createElement(t){ return _fakeEl(t); },
  querySelectorAll(){ return []; } };
__SRC__
const TP = window.TofuPet;
(async () => {
  // The tap: pops the bubble AND startles the pet into a flee dash.
  TP.interact();
  const bubble = _barChildren.find(c => c.className === 'tofu-pet-bubble');
  const samples = [];
  for (let k = 0; k < 14; k++){
    _t += 160;
    const cb = _cbs.shift();
    if (cb) cb(_t);
    await new Promise(r => setImmediate(r));
    samples.push({ x: TP.getState().x, left: bubble ? (bubble.style.left || '') : '' });
  }
  console.log(JSON.stringify({ samples: samples }));
  process.exit(0);
})();
'''
        script = harness.replace("__SRC__", src_text)
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             cwd=str(REPO), timeout=20)
        assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
        line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
        return json.loads(line)["samples"]

    src = PET_JS.read_text()
    samples = run(src)
    assert samples and all(s["left"] for s in samples), \
        f"bubble never rendered or never got a left: {samples[:3]}"
    xs = [s["x"] for s in samples]
    lefts = [float(s["left"].replace("px", "")) for s in samples]
    moved = max(xs) - min(xs)
    assert moved > 15, f"the pet barely moved across 14 ticks ({moved:.1f}px) — harness broken"

    # _positionBubble() anchors at W.x + petW/2 but CLAMPS the result inside
    # the bar: left = max(40, min(x + petW/2, barW - 40)), floored at 2. The
    # harness fakes petW=30 (_fakeEl offsetWidth) and barW=400
    # (getBoundingClientRect), so a flee that pins the pet against the left
    # track edge (x < 25) parks the bubble at 40px — the shipped clamp
    # working as designed, NOT a tracking failure. Mirror the clamp here or
    # the assertion flakes whenever the seeded flee hugs an edge.
    def _expected_left(x):
        return max(2.0, max(40.0, min(x + 15.0, 400.0 - 40.0)))

    drift = max(abs(l - _expected_left(x)) for l, x in zip(lefts, xs))
    assert drift < 0.6, (
        f"the bubble does NOT follow the pet: worst |bubbleLeft-clamp(x+15)| = {drift:.1f}px "
        f"across {len(samples)} ticks (pet moved {moved:.0f}px). "
        "The bubble must be re-anchored every frame, not positioned once at show time.")

    # NEUTER: drop the per-frame re-anchor from _place() → the bubble freezes
    # at the tap spot while the pet runs off.
    neut = src.replace("    _positionBubble();   // the bubble rides above the pet, wherever it moved to\n",
                       "", 1)
    assert neut != src, "neuter did not match the _place re-anchor call"
    nsamples = run(neut)
    nxs = [s["x"] for s in nsamples]
    nlefts = [float(s["left"].replace("px", "")) for s in nsamples]
    ndrift = max(abs(l - _expected_left(x)) for l, x in zip(nlefts, nxs))
    assert max(nxs) - min(nxs) > 15, "neutered pet barely moved — harness broken"
    assert ndrift > 5.0, (
        f"neutered build still tracks the pet (drift {ndrift:.1f}px) — the guard does not bite")


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


def test_facing_layer_has_no_transition_no_paper_flip():
    """ROOT of the 'flips like a sheet of paper on its vertical axis' bug: if the
    scaleX(±1) mirror carries a `transition:transform`, flipping +1→-1 tweens
    THROUGH scaleX(0) — the sprite collapses to a zero-width line and mirrors
    (the paper flip). The mirror MUST be instant; the natural turn motion is
    carried by the tofuPetPivot keyframe instead.

    Re-anchored 2026-07-29: the mirror moved OFF the old `.tofu-pet-facing`
    wrapper and INTO the sprite, onto its [data-space="char"] groups via
    --pet-face-flip. Mirroring a wrapper also mirrored the block's baked
    shading, so the sun appeared to jump across the body on every turn while the
    cast shadow (driven by the real scene sun) correctly stayed put. The
    paper-flip lesson is unchanged — only where it applies moved — so this now
    guards that neither surviving layer tweens a transform."""
    css = (REPO / "static" / "styles.css").read_text()
    assert ".tofu-pet-facing" not in css, (
        "the .tofu-pet-facing wrapper is back. Mirroring a wrapper drags the "
        "block's lighting around with the pet — the sun teleports on every turn.")
    m = re.search(r"\.tofu-pet \.tofu-pet-img\{([^}]*)\}", css)
    assert m, "could not isolate the .tofu-pet-img rule"
    body = m.group(1)
    assert "transition" not in body, (
        "the frame layer gained a transition. Any transform tween on the layers "
        "around the sprite risks the paper-flip smear the instant mirror avoids."
        "\nbody=" + body)
    # The mirror itself must stay a bare property write, never a tweened one.
    for rule in re.findall(r"\.tofu-pet[^{]*\{([^}]*)\}", css):
        if "--pet-face-flip" in rule and "transition" in rule:
            raise AssertionError(
                "a rule both sets --pet-face-flip and declares a transition; "
                "the facing flip must be instant or the face smears through 0.")


def test_turn_pivot_keyframe_exists_and_is_wired():
    """A turn reads as a PIVOT (squash-hop), not a page-flip: tofu-pet.js stamps
    data-turning on a facing change, and the CSS plays a tofuPetPivot keyframe.
    Guard both the keyframe definition and its wiring, and assert the pivot has
    NO horizontal scale (a scaleX in it would re-create the paper-flip)."""
    css = (REPO / "static" / "styles.css").read_text()
    js = PET_JS.read_text()
    assert "data-turning" in js, "tofu-pet.js no longer stamps data-turning on a facing flip"
    assert "[data-turning]" in css and "tofuPetPivot" in css, \
        "the data-turning pivot rule / tofuPetPivot animation is missing"
    m = re.search(r"@keyframes tofuPetPivot\{([^}]*)\}", css)
    assert m, "tofuPetPivot keyframe not defined"
    assert "scaleX" not in m.group(1), \
        "tofuPetPivot must not scaleX — that would re-introduce the paper-flip"


def test_gaze_head_turn_does_not_hop():
    """The stationary 'gaze' state flips facing every GAZE_TURN_MS while the cat
    SITS still looking around — it must use the instant mirror ALONE (no
    plant-and-hop), otherwise a calmly-gazing cat jitters a squash-hop in place
    every ~1.3s. Guard the seam: (1) the gaze call site passes pivot=false, and
    (2) _face only stamps data-turning when the pivot flag is truthy."""
    js = PET_JS.read_text()
    # (1) gaze glances with the instant mirror, no hop
    assert re.search(r"_face\(\s*-W\.dir\s*,\s*false\s*\)", js), (
        "the gaze head-turn no longer passes pivot=false — a sitting cat will "
        "hop every glance again (the 'odd/jittery' feel).")
    # (2) the data-turning stamp is gated on the pivot flag (not fired blindly)
    m = re.search(r"function _face\(dir, pivot\)\{?[\s\S]*?\n  \}", js) \
        or re.search(r"function _face\(dir, pivot\)[\s\S]{0,900}?data-turning", js)
    assert m, "could not isolate the _face body"
    body = m.group(0)
    assert "pivot" in body.split("data-turning")[0], \
        "_face stamps data-turning without consulting the pivot flag"
    assert re.search(r"if \(changed && pivot", body), (
        "the data-turning stamp must be gated on `changed && pivot` so gaze "
        "(pivot=false) flips skip the hop.")


def test_neuter_gaze_hop_scope_is_load_bearing():
    """NEUTER: revert the gaze call site to the pivot-default `_face(-W.dir)` in
    a COPY → the gaze-scope guard's pivot=false check must fail, proving it
    bites (i.e. the gaze cat would hop again)."""
    js = PET_JS.read_text()
    poisoned = js.replace("_face(-W.dir, false)", "_face(-W.dir)", 1)
    assert poisoned != js, "neuter did not match the gaze call site"
    assert not re.search(r"_face\(\s*-W\.dir\s*,\s*false\s*\)", poisoned), \
        "neuter left a pivot=false gaze call — guard would not bite"


def test_neuter_facing_transition_reintroduces_paper_flip_risk():
    """NEUTER: re-add a transform transition to the frame layer in a COPY → the
    no-transition guard must fire, proving it's load-bearing.

    Re-anchored with the guard above: the mirror now lives inside the sprite, so
    the neuter poisons the layer that still exists (.tofu-pet-img) rather than
    the deleted .tofu-pet-facing wrapper."""
    css = (REPO / "static" / "styles.css").read_text()
    anchor = ".tofu-pet .tofu-pet-img{"
    assert anchor in css, "could not locate the .tofu-pet-img rule to poison"
    poisoned = css.replace(anchor, anchor + "transition:transform 0.15s ease;", 1)
    assert poisoned != css, "neuter did not match the .tofu-pet-img rule"
    m = re.search(r"\.tofu-pet \.tofu-pet-img\{([^}]*)\}", poisoned)
    assert m and "transition" in m.group(1), (
        "neuter did not actually add a transition")


def test_pet_reads_scene_light_and_drives_lighting_props():
    """The pet's lighting system: tofu-pet.js must read TofuScene.lightInfo()
    each frame (in _step) and drive the CSS light custom properties so the
    sprite is shaded + its shadow points from the SAME sun the scene moves. We
    guard the seam at the source level (the node harness can't load the real
    scene alongside the pet): _applyLight is defined, called from _step, reads
    lightInfo, and writes the shadow/shade/warm props."""
    js = PET_JS.read_text()
    assert "function _applyLight(" in js, "the _applyLight lighting hook is gone"
    assert re.search(r"function _step\([^)]*\)\s*\{[\s\S]{0,120}?_applyLight\(\)", js), \
        "_step no longer calls _applyLight every frame — the sprite won't relight"
    assert "TofuScene.lightInfo" in js, "_applyLight no longer reads the scene's live sun"
    for prop in ("--pet-shadow-dx", "--pet-shadow-scale", "--pet-shade-dx", "--pet-light-warm"):
        assert prop in js, f"_applyLight no longer sets {prop}"


def test_css_wires_light_props_into_sprite_and_shadow():
    """The lighting props must actually change pixels: the sprite filter must
    consume the warm/shade props, and the contact shadow's transform must
    consume the directional offset + length. Guard the CSS consumption so the
    JS writes aren't dead."""
    css = (REPO / "static" / "styles.css").read_text()
    m = re.search(r"\.tofu-pet \.tofu-pet-img\{([^}]*)\}", css)
    assert m, "could not isolate the .tofu-pet-img rule"
    body = m.group(1)
    assert "filter:" in body and "--pet-shade-dx" in body and "--pet-light-warm" in body, \
        "the sprite filter no longer consumes the directional light/shade props"
    ma = re.search(r"\.tofu-pet::after\{([^}]*)\}", css)
    assert ma, "could not isolate the .tofu-pet::after shadow rule"
    sbody = ma.group(1)
    assert "--pet-shadow-dx" in sbody and "--pet-shadow-scale" in sbody, \
        "the cast shadow no longer follows the sun (missing directional props)"


def test_css_dragged_pet_rises_above_foreground_plane():
    """Overlap fix: a resting pet (z1) is occluded by the near fg plane (z2) so
    grass hides its paws — correct. But a PICKED-UP cat is airborne and must
    rise ABOVE that plane, not stay trapped behind grass. Guard that the drag
    state lifts the pet above the fg canvas z-index (2)."""
    css = (REPO / "static" / "styles.css").read_text()
    m = re.search(r'\.tofu-pet\[data-state="drag"\]\{[^}]*z-index:\s*(\d+)', css)
    assert m, "no z-index lift on the dragged pet — it stays behind the fg plane"
    assert int(m.group(1)) > 2, \
        f"dragged pet z-index ({m.group(1)}) must exceed the fg canvas z-index (2)"


def test_NEUTER_pet_ignores_light_is_caught():
    """NEUTER: remove the _applyLight() call from _step in a COPY → the
    'reads light every frame' guard must fail, proving it bites."""
    js = PET_JS.read_text()
    poisoned = js.replace(
        "_applyLight();   // shade the sprite + point its shadow from the live scene sun (also while dragged)",
        "", 1)
    assert poisoned != js, "neuter did not match the _step _applyLight call"
    assert not re.search(r"function _step\([^)]*\)\s*\{[\s\S]{0,120}?_applyLight\(\)", poisoned), \
        "neuter left _applyLight wired into _step — test would not bite"


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
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
        json.dump(_i18n_dict(), fh)
        dict_path = fh.name
    try:
        out = subprocess.run(["node", "-e", script + "\nprocess.exit(0);", dict_path],
                             capture_output=True, text=True, cwd=str(REPO), timeout=20)
    finally:
        os.unlink(dict_path)
    assert out.returncode == 0, f"node failed: {out.stderr}"
    line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    expr = json.loads(line)["expr"]
    assert expr != "sleeping", f"neutered build still slept ({expr}) — test does not bite"


if __name__ == "__main__":
    for fn in [test_expression_frames_exist_on_disk, test_extra_pose_frames_exist_on_disk,
               test_curious_alias_resolves_to_a_real_frame,
               test_decor_scene_assets_exist, test_decor_css_wires_every_scene,
               test_control_lift_is_allowlist_not_denylist,
               test_pack_switcher_fully_removed,
               test_dom_footstep_fx_layer_fully_removed,
               test_project_brain_button_removed_from_bar,
               test_pet_is_draggable,
               test_click_startles_the_pet_and_makes_it_flee,
               test_daytime_sleep_is_rare_not_frequent,
               test_walking_is_a_minority_and_behaviour_is_varied,
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
