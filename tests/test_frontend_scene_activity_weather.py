"""ACTIVITY WEATHER for the project-bar scene (static/js/tofu-scene.js).

The pet already reacts to what the app is doing (TofuPet.setActivity, driven by
the 'tofu:activity' DOM event); the scene ignored it. Letting the weather carry
that signal turns ambient decoration into PERIPHERAL STATUS — readable out of the
corner of your eye without looking at it.

THE INVARIANT THIS FILE EXISTS TO PROTECT:

    Every effect decays to neutral on its own. No code path may leave the bar
    permanently stormy.

That is an owner requirement, not a style preference: a weather state pinned to
an error becomes ambient anxiety, and errors already surface in the chat where
they belong. The implementation has no "error mode" to get stuck in because it
has no modes at all — just scalars that ramp toward a target and decay to zero.
`success` and `error` are one-shot IMPULSES; only `loading` holds, and ANY
non-loading signal releases it (so a missed or unknown terminal event cannot
strand the bar under cloud).

These tests drive the REAL module through the REAL event and then simply let
time pass, asserting the scene returns to neutral by itself.
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

# Sentinel for "pump frames without dispatching anything" — distinct from a real
# event whose detail happens to be null (which must ALSO release the hold).
_IDLE = object()


# ── Harness: mounts the real module, dispatches real 'tofu:activity' events on
# the real document, pumps frames, and reports the weather scalars over time
# plus how many dabs the rain pass actually painted. ──
_WX_HARNESS = r"""
'use strict';
const W = 900, H = 48;
const SCRIPT = __SCRIPT__;          // [[kind|null, framesToPump], ...]

let dabCount = 0;
function mkCtx(isVisible){
  let path=null;
  return {
    canvas:{width:W,height:H},
    setTransform(){}, clearRect(){}, save(){}, restore(){},
    translate(){}, rotate(){},
    beginPath(){ path=[]; }, moveTo(){}, lineTo(){}, closePath(){}, rect(){},
    ellipse(){ if(path && isVisible) path.push(1); }, arc(){}, clip(){},
    fill(){ if(path && isVisible) dabCount += path.length; path=[]; },
    fillRect(){}, stroke(){}, drawImage(){},
    createLinearGradient(){ return {addColorStop(){}}; },
    createRadialGradient(){ return {addColorStop(){}}; },
    set fillStyle(v){}, get fillStyle(){ return ''; },
    set globalAlpha(v){}, get globalAlpha(){ return 1; },
    set globalCompositeOperation(v){}, get globalCompositeOperation(){ return ''; },
    set strokeStyle(v){}, get strokeStyle(){ return ''; },
    set lineWidth(v){}, get lineWidth(){ return 1; },
  };
}

let rafCb=null;
global.requestAnimationFrame=cb=>{rafCb=cb;return 1;};
global.cancelAnimationFrame=()=>{};
global.devicePixelRatio=1;
global.window={matchMedia(){return{matches:false,addEventListener(){},addListener(){}};},addEventListener(){},
 ResizeObserver:function(){return{observe(){},disconnect(){}};},MutationObserver:function(){return{observe(){},disconnect(){}};},
 IntersectionObserver:function(){return{observe(){},disconnect(){}};},devicePixelRatio:1};
global.ResizeObserver=global.window.ResizeObserver;
global.MutationObserver=global.window.MutationObserver;
global.IntersectionObserver=global.window.IntersectionObserver;

// a minimal but REAL event target, so the module's own addEventListener wiring
// is what gets exercised (not a stub we call directly)
const _listeners = {};
function mkEl(){return{_attrs:{},className:'',style:{},width:0,height:0,setAttribute(k,v){this._attrs[k]=v;},
 getAttribute(k){return this._attrs[k];},appendChild(){},insertBefore(){},querySelector(){return null;},firstChild:null,
 getBoundingClientRect(){return{left:0,right:W,top:0,bottom:H,width:W,height:H};}};}
const bar=mkEl(); bar._attrs['data-decor']='meadow';
let canvasN=0;
global.document={readyState:'complete',hidden:false,
 documentElement:{getAttribute(k){return k==='data-theme'?'tofu':null;}},
 addEventListener(type, fn){ (_listeners[type] = _listeners[type] || []).push(fn); },
 dispatch(type, detail){ for (const fn of (_listeners[type]||[])) fn({detail}); },
 getElementById(id){return id==='projectBar'?bar:null;},
 createElement(t){ if(t==='canvas'){ canvasN++; const vis=(canvasN===1);
   const e=mkEl(); e.getContext=()=>mkCtx(vis); return e; } return mkEl(); }};
global.window.TofuPet={getState(){return{x:W/2-16,state:'walk'};}};

__SRC__

const S = window.TofuScene;
let t = 100;
function frame(){ t += 34; if (rafCb) { const cb = rafCb; rafCb = null; cb(t); } }

const timeline = [];
frame();                             // boot
for (const [kind, frames] of SCRIPT) {
  // "__SKIP__" means: dispatch NOTHING this step (just let time pass).
  // Anything else — including null — is dispatched as a real event detail, so
  // the undefined/unknown-detail path is genuinely exercised.
  if (kind !== "__SKIP__") document.dispatch('tofu:activity', kind);
  for (let i = 0; i < frames; i++) frame();
  const w = S.weatherInfo();
  timeline.push({ after: kind, overcast: w.overcast, burst: w.burst,
                  rain: w.rain, target: w.target, enabled: w.enabled });
}
console.log(JSON.stringify({ timeline, dabCount,
  hasSetWeather: typeof S.setWeather === 'function',
  listensToActivity: !!(_listeners['tofu:activity'] || []).length }));
process.exit(0);
"""


def _run_wx(script, src=None):
    # Callers write None for "no event, just pump frames"; translate it to the
    # harness sentinel so a literal null detail can still be tested explicitly.
    script = [[("__SKIP__" if k is _IDLE else k), n] for k, n in script]
    src = src if src is not None else SCENE_JS.read_text()
    js = (_WX_HARNESS.replace("__SRC__", src)
          .replace("__SCRIPT__", json.dumps(script)))
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True,
                         cwd=str(REPO), timeout=30)
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(line)


def _neutral(step, eps=0.02):
    return (step["overcast"] <= eps and step["burst"] <= eps
            and step["rain"] <= eps and step["target"] <= eps)


# ══════════════════════════════════════════════════════════════════════════
#  THE INVARIANT: nothing can leave the bar stormy
# ══════════════════════════════════════════════════════════════════════════

def test_error_is_a_brief_pass_that_self_clears():
    """OWNER REQUIREMENT — error must NOT rain persistently. It is a one-shot
    impulse: it appears, then decays to neutral with no further events. A held
    storm tied to an error is ambient anxiety, and errors already surface in the
    chat."""
    r = _run_wx([["error", 1], [_IDLE, 3], [_IDLE, 120]])
    assert r["timeline"][0]["rain"] > 0.5, "the error impulse never fired"
    assert r["timeline"][1]["rain"] < r["timeline"][0]["rain"], "rain is not decaying"
    assert _neutral(r["timeline"][2]), (
        f"the bar is STILL stormy after the error and no further events: "
        f"{r['timeline'][2]} — some path can pin the weather")


def test_success_is_a_brief_pass_that_self_clears():
    """Success is the same shape as error: a one-shot burst that decays out."""
    r = _run_wx([["success", 1], [_IDLE, 3], [_IDLE, 120]])
    assert r["timeline"][0]["burst"] > 0.5, "the success burst never fired"
    assert r["timeline"][1]["burst"] < r["timeline"][0]["burst"], "burst is not decaying"
    assert _neutral(r["timeline"][2]), \
        f"the bar never returned to neutral after success: {r['timeline'][2]}"


def test_loading_holds_but_any_terminal_signal_releases_it():
    """`loading` is the ONLY holding state — work really can be in flight for a
    while. But ANY non-loading signal releases the hold, so a missed or unknown
    terminal event cannot strand the bar under permanent cloud."""
    r = _run_wx([["loading", 40], [_IDLE, 5], ["success", 1], [_IDLE, 120]])
    assert r["timeline"][0]["overcast"] > 0.2, "loading did not raise any overcast"
    assert r["timeline"][1]["overcast"] > 0.2, "the overcast hold did not persist while loading"
    assert _neutral(r["timeline"][3]), \
        f"the bar stayed overcast after work finished: {r['timeline'][3]}"


def test_unknown_activity_kind_still_releases_the_hold():
    """Defensive: an activity value the scene does not recognise (a future kind,
    a typo, an undefined detail) must still be treated as 'not loading' and
    release the hold — never leave the bar stuck under cloud."""
    for junk in ["none", "wat", "", None]:
        r = _run_wx([["loading", 40], [junk, 1], [_IDLE, 120]])
        assert _neutral(r["timeline"][2]), (
            f"activity={junk!r} left the bar non-neutral: {r['timeline'][2]} — an "
            f"unrecognised terminal signal must still release the overcast hold")


def test_abandoned_loading_expires_on_its_own():
    """The one hole in "nothing can leave the bar stormy": `loading` is held by
    an event we do NOT control. If the terminal signal never arrives — crashed
    task, dropped stream, tab closed mid-request — the hold would never release
    and the bar would sit under cloud indefinitely.

    Found by driving adversarial sequences rather than by reading the code: a
    plain loading-then-silence run stayed at overcast=0.55 forever. The hold now
    expires on its own, while still lasting long enough not to fight a genuinely
    slow request."""
    # while work is plausibly still in flight, the hold MUST persist
    working = _run_wx([["loading", 30], [_IDLE, 300]])["timeline"][-1]
    assert working["overcast"] > 0.2, (
        "the overcast released while work was still plausibly in flight — the "
        "watchdog is too aggressive to represent a real request")
    # but an abandoned hold must heal itself
    abandoned = _run_wx([["loading", 30], [_IDLE, 4000]])["timeline"][-1]
    assert _neutral(abandoned), (
        f"an ABANDONED loading state left the bar permanently overcast: "
        f"{abandoned} — no code path may leave the bar stormy")


def test_NEUTER_missing_watchdog_is_caught():
    """NEUTER: disable the hold watchdog → an abandoned loading pins the bar and
    the self-healing assertion must fail."""
    src = SCENE_JS.read_text()
    neut = src.replace("    if (_overcastSince >= 0 && (ms - _overcastSince) > OVERCAST_MAX_HOLD_MS) {",
                       "    if (false) {  /* NEUTER: no watchdog */", 1)
    assert neut != src, "neuter did not match the watchdog condition"
    abandoned = _run_wx([["loading", 30], [_IDLE, 4000]], src=neut)["timeline"][-1]
    assert not _neutral(abandoned), \
        "neutered build still healed itself — the watchdog guard would not bite"


def test_weather_decays_to_neutral_from_every_state():
    """The general property, stated directly: whatever happened, if the app goes
    quiet the scene returns to neutral by itself."""
    for kind in ("loading", "success", "error"):
        r = _run_wx([[kind, 2], ["none", 1], [_IDLE, 150]])
        assert _neutral(r["timeline"][2]), \
            f"after {kind} the scene did not return to neutral: {r['timeline'][2]}"


def test_NEUTER_held_error_state_is_caught():
    """NEUTER: make the error impulse a HELD state instead of a decaying one →
    the self-clearing assertions must fail, proving they bite."""
    src = SCENE_JS.read_text()
    neut = src.replace("    _wx.rain = Math.max(0, _wx.rain - IMPULSE_DECAY * dt);",
                       "    /* NEUTER: rain never decays */", 1)
    assert neut != src, "neuter did not match the rain decay"
    r = _run_wx([["error", 1], [_IDLE, 120]], src=neut)
    assert not _neutral(r["timeline"][1]), \
        "neutered (non-decaying) build still went neutral — the guard would not bite"


# ══════════════════════════════════════════════════════════════════════════
#  WIRING + KILL SWITCH
# ══════════════════════════════════════════════════════════════════════════

def test_scene_listens_to_the_activity_event_directly():
    """The scene subscribes to 'tofu:activity' itself rather than having the pet
    forward it: the two stay decoupled, and the weather scalars live outside the
    baked buffer so a re-bake cannot lose the current weather."""
    r = _run_wx([[_IDLE, 1]])
    assert r["listensToActivity"], \
        "the scene never subscribed to 'tofu:activity' — weather can't be driven"
    src = SCENE_JS.read_text()
    assert "document.addEventListener('tofu:activity'" in src, \
        "the scene no longer listens for the activity event directly"


def test_weather_has_an_instant_kill_switch():
    """Owner requirement: default on, instantly killable. Disabling must zero
    every scalar so the bar is neutral on the very next frame."""
    src = SCENE_JS.read_text()
    assert "var WEATHER_ENABLED = true;" in src, "weather is not defaulting on"
    m = re.search(r"setWeather: function \(on\) \{(.*?)\n    \}", src, re.S)
    assert m, "no setWeather kill switch on the public API"
    body = m.group(1)
    assert "_wx.overcast = _wx.burst = _wx.rain = 0" in body, \
        "the kill switch does not zero the live scalars — the bar could stay stormy"


def test_disabled_weather_never_leaves_residue():
    """With weather disabled, activity events must be inert."""
    src = SCENE_JS.read_text()
    m = re.search(r"function _stepWeather\(dt, ms\) \{(.*?)\n  \}", src, re.S)
    assert m, "could not isolate _stepWeather"
    assert "if (!WEATHER_ENABLED)" in m.group(1), \
        "_stepWeather does not honour the kill switch"
    m2 = re.search(r"function _onActivity\(kind, ms\) \{(.*?)\n  \}", src, re.S)
    assert m2 and "if (!WEATHER_ENABLED) return;" in m2.group(1), \
        "_onActivity does not honour the kill switch"


# ══════════════════════════════════════════════════════════════════════════
#  COST — weather must not compete with the UI or the pixel budget
# ══════════════════════════════════════════════════════════════════════════

def test_glow_weather_is_free_it_modulates_an_existing_blit():
    """Overcast and the success burst ride the ALPHA of a blit that already
    happens every frame, so they cost zero extra pixels. If they ever became
    their own pass, the pixel budget the perf work bought would regress."""
    src = SCENE_JS.read_text()
    assert "var wxGlow = 1 - _wx.overcast" in src, \
        "the glow weather is no longer a modulation of the existing glow blit"
    # there must be exactly ONE glow tile blit per frame
    assert src.count("c.drawImage(_glowTile") == 1, \
        "the glow tile is blitted more than once per frame — weather added a pass"


def test_rain_reuses_the_capped_dab_budget():
    """The rain pass must be bounded by the same live-population cap as
    everything else, not seeded by area — otherwise a wide bar would rain
    proportionally harder."""
    src = SCENE_JS.read_text()
    m = re.search(r"if \(_wx\.rain > 0\.01\) \{(.*?)\n    \}", src, re.S)
    assert m, "could not isolate the rain pass"
    body = m.group(1)
    assert "LIVE_CAP_SPARK" in body, \
        "the rain pass is not bounded by the live-population cap"
    assert "* w" not in body.replace("/ 100 * w", ""), \
        "the rain count appears to be area/width-seeded rather than capped"


def test_rain_paints_nothing_once_decayed():
    """The pass is gated on the decaying scalar, so a SETTLED scene pays nothing
    at all for the weather feature. Measured PER FRAME, because the harness
    accumulates dabs across however many frames it pumps — a raw total would
    just reward whichever run pumped longer."""
    F = 6
    quiet = _run_wx([[_IDLE, F]])["dabCount"] / F
    stormy = _run_wx([["error", 1], [_IDLE, F - 1]])["dabCount"] / F
    assert stormy > quiet, (
        f"the rain pass painted nothing while active "
        f"({stormy:.0f} vs quiet {quiet:.0f} dabs/frame)")
    # Let it fully decay: over a long run the per-frame average must fall back
    # to the quiet baseline, i.e. the settled scene pays ~nothing for weather.
    LONG = 200
    settled = _run_wx([["error", 1], [_IDLE, LONG - 1]])["dabCount"] / LONG
    assert settled < quiet * 1.05, (
        f"a settled scene still pays for weather ({settled:.0f} vs quiet "
        f"{quiet:.0f} dabs/frame) — the rain pass is not fully gated off")
