#!/usr/bin/env python3
"""A dragged pet must be PICKED UP, not slid along the floor.

THE REPORT THIS SUITE GUARDS (owner, 2026-07-31)
────────────────────────────────────────────────
"this draggable pet doesn't have that feeling of being picked up; there is no
vertical change."

Measured before the fix, and the cause was structural rather than a matter of
tuning: the pet had **no vertical channel at all**.

  * ``_place()`` wrote ``transform = 'translateX(' + W.x + 'px)'`` and nothing
    else. There was no ``W.y``.
  * The whole module contained ZERO references to ``clientY`` / ``movementY`` /
    ``offY`` / ``startY`` — the pointer's entire Y component was discarded, so
    lifting the mouse to the top of the screen moved the sprite not one pixel.
  * The drag state changed exactly four things: ``cursor:grabbing``,
    ``z-index:5``, and a cast shadow that shrank to 12x3 at 55% opacity.

That last one is why the result read as actively wrong rather than merely
static: **the shadow was performing a lift that the body never performed.** A
shrinking shadow under a sprite that stays glued to the baseline is a stronger
cue for "sliding" than no shadow change at all, because the two channels
disagree.

WHAT IS ASSERTED HERE
─────────────────────
The full arc — lift → carry → land — driven through the SHIPPED module with a
real pointer sequence and a real rAF pump, reading the transform the engine
actually wrote. No re-implementation of the physics in the test: every number
below is read back out of ``_el.style.transform`` / ``--pet-lift``.

Deliberately NOT asserted: exact pixel heights or easing curves. Those are
taste and will be retuned; pinning them would make the guard a change-detector.
What is pinned is the PROPERTY the owner reported missing — that a grab
produces height, that the height tracks the pointer, and that a release returns
the pet to the ground rather than leaving it floating.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
PET_JS = REPO / "static" / "js" / "tofu-pet.js"
CSS = REPO / "static" / "styles.css"
_NODE = __import__("shutil").which("node")


def _js_code() -> str:
    """tofu-pet.js with comments stripped (charter #24) — this module's own
    comments quote the idioms being asserted absent."""
    sys.path.insert(0, str(REPO / "tests"))
    from _source_scan import strip_comments

    return strip_comments(PET_JS.read_text(encoding="utf-8"), lang="js")


def _css_code() -> str:
    sys.path.insert(0, str(REPO / "tests"))
    from _source_scan import strip_comments

    return strip_comments(CSS.read_text(encoding="utf-8"), lang="css")


# ══════════════════════════════════════════════════════════════════════
#  BEHAVIOURAL: drive a real drag through the shipped engine
# ══════════════════════════════════════════════════════════════════════

_HARNESS = r"""
'use strict';
// Minimal DOM good enough for the pet's mount + drag path. Mirrors the harness
// in test_frontend_tofu_pet.py; the pointer handlers and the rAF loop are the
// parts that matter here, so both are REAL (not stubbed away).
let __NOW__ = 1767200000000;         // fixed clock: 2026-01-01, midday-ish
class FakeDate extends Date {
  constructor(...a){ super(...(a.length ? a : [__NOW__])); }
  static now(){ return __NOW__; }
}
global.Date = FakeDate;

const REDUCED = __REDUCED__;
global.window = {
  matchMedia(){ return { matches: REDUCED, addEventListener(){}, addListener(){} }; },
  addEventListener(){},
  PointerEvent: function(){},          // engine takes the Pointer Events branch
};
global.BASE_PATH = '';
global.ResizeObserver = function(){ return { observe(){}, disconnect(){} }; };
global.localStorage = { _d:{}, getItem(k){return this._d[k]||null;}, setItem(k,v){this._d[k]=v;} };
global.Image = function(){
  return { _attrs:{}, draggable:false, alt:'', style:{},
    setAttribute(k,v){ this._attrs[k]=v; }, getAttribute(k){ return this._attrs[k]; },
    addEventListener(){}, appendChild(){},
    set src(v){ this._src = v; }, get src(){ return this._src || ''; } };
};
global.t = function (k){ return k; };

// rAF: a PUMP we drive by hand, so the fall can be integrated deterministically
// instead of racing a real clock.
let _rafCbs = [];
global.requestAnimationFrame = function(cb){ _rafCbs.push(cb); return _rafCbs.length; };
global.cancelAnimationFrame = function(){};
function pump(frames, dtMs){
  for (let i = 0; i < frames; i++) {
    __NOW__ += dtMs;
    const due = _rafCbs; _rafCbs = [];
    due.forEach(function(cb){ cb(__NOW__); });
  }
}
// setTimeout must not fire on its own here (the landing squash clears itself);
// keep the handle bookkeeping but never invoke.
global.setTimeout = function(){ return 0; };
global.clearTimeout = function(){};
global.setInterval = function(){ return 0; };

let _mounted = null;
const _listeners = {};
function _fakeEl(kind){
  return { _attrs:{}, tagName:'', className:'', alt:'', src:'', offsetWidth:30,
    setAttribute(k,v){ this._attrs[k]=v; }, getAttribute(k){ return this._attrs[k]; },
    removeAttribute(k){ delete this._attrs[k]; },
    addEventListener(ev,fn){ if(kind==='pet') _listeners[ev]=fn; },
    appendChild(){}, insertBefore(){}, removeChild(){},
    setPointerCapture(){}, releasePointerCapture(){},
    querySelector(){ return null; }, querySelectorAll(){ return []; },
    getBoundingClientRect(){ return {left:0,right:400,top:0,bottom:48,width:400,height:48}; },
    firstChild:null,
    style:{ _p:{}, setProperty(k,v){ this._p[k]=v; }, removeProperty(k){ delete this._p[k]; } } };
}
global.document = {
  readyState:'complete', hidden:false, addEventListener(){},
  getElementById(id){ if(id==='projectBar') return _fakeEl('bar'); return _mounted; },
  createElement(tag){
    const e=_fakeEl(tag==='span' && !_mounted ? 'pet' : tag);
    e.tagName=tag; if(tag!=='img' && !_mounted) _mounted=e; return e;
  },
  querySelectorAll(){ return _mounted ? [_mounted] : []; },
};

__SRC__

const TP = window.TofuPet;
// Read the Y the ENGINE wrote, straight out of the transform string.
function ty(){
  const m = /translateY\(([-0-9.]+)px\)/.exec(_mounted.style.transform || '');
  return m ? parseFloat(m[1]) : null;
}
function lift(){ const v=_mounted.style._p['--pet-lift']; return v==null?null:parseFloat(v); }
const out = { reduced: REDUCED, samples: [] };
function snap(label){
  out.samples.push({ at: label, ty: ty(), lift: lift(),
                     state: TP.getState().state, landing: _mounted.getAttribute('data-landing') || null });
}

out.hasPointerDown = typeof _listeners.pointerdown === 'function';
out.transformAtRest = _mounted.style.transform || '';
snap('rest');

// ── a real drag: press, cross the slop, carry sideways, then lift the hand ──
_listeners.pointerdown({ button:0, pointerId:1, clientX:100, clientY:40 });
snap('after_press');                        // still a click candidate

_listeners.pointermove({ pointerId:1, clientX:140, clientY:40, movementX:40, cancelable:false });
snap('after_grab');                         // crossed slop: must be OFF the floor

_listeners.pointermove({ pointerId:1, clientX:160, clientY:25, movementX:20, cancelable:false });
snap('carried_up');                         // pointer rose 15px

_listeners.pointermove({ pointerId:1, clientX:180, clientY:40, movementX:20, cancelable:false });
snap('carried_down');                       // pointer back to start height

_listeners.pointerup({ pointerId:1, clientX:180, clientY:40 });
snap('released');                           // now falling (or planted if reduced)

pump(40, 16);                               // ~0.64s of rAF
snap('settled');

console.log(JSON.stringify(out));
"""


def _run(reduced: bool):
    src = PET_JS.read_text(encoding="utf-8")
    code = (_HARNESS
            .replace("__SRC__", src)
            .replace("__REDUCED__", "true" if reduced else "false"))
    r = subprocess.run([_NODE, "-e", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:3000]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_grabbing_the_pet_lifts_it_off_the_ground():
    """THE REPORTED DEFECT: a grab must produce HEIGHT.

    Asserted on a purely-sideways carry (clientY never changes between press
    and grab), because that is the case the old code could not express at all:
    with the pointer's Y constant there is nothing to derive a lift from, so a
    motion-derived rise would still leave the pet skating.
    """
    out = _run(reduced=False)
    assert out["hasPointerDown"], "the drag path is not wired — nothing to test"
    at = {s["at"]: s for s in out["samples"]}

    assert at["rest"]["ty"] in (0.0, None) or at["rest"]["ty"] == 0.0, (
        f"the pet is not on the ground at rest (translateY={at['rest']['ty']})")
    assert at["after_grab"]["state"] == "drag", "crossing the slop did not start a drag"
    assert at["after_grab"]["ty"] is not None, (
        "the engine writes no translateY at all — the sprite has no vertical "
        "channel, which is exactly the reported defect")
    assert at["after_grab"]["ty"] < 0, (
        f"grabbing the pet left it at translateY={at['after_grab']['ty']}; it must "
        "rise (negative Y = up) so the grab reads as picking it UP rather than "
        "sliding it along the floor")


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_the_held_pet_tracks_the_pointers_height():
    """Carrying: raising the hand must raise the pet, lowering it must lower it.

    A fixed hop on grab would satisfy the test above while still feeling dead in
    the hand, so the HEIGHT has to be a live function of the pointer, not a
    one-shot constant.
    """
    out = _run(reduced=False)
    at = {s["at"]: s for s in out["samples"]}
    grabbed, up, down = at["after_grab"]["ty"], at["carried_up"]["ty"], at["carried_down"]["ty"]
    assert up < grabbed, (
        f"raising the pointer 15px did not raise the pet (ty {grabbed} → {up}) — "
        "the held sprite ignores the pointer's vertical motion")
    assert down > up, (
        f"lowering the pointer did not lower the pet (ty {up} → {down}) — the "
        "lift is one-way, so the pet cannot be set back down by hand")


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_releasing_returns_the_pet_to_the_ground():
    """Landing: the pet must come back DOWN, and must not be left floating.

    The complement of the lift. A pet that rises on grab but never returns is a
    worse bug than the one being fixed, and it is reachable: the fall is driven
    by the rAF loop, so anything that stops that leg mid-air (a state timeout
    firing, a missing `until`) strands it.
    """
    out = _run(reduced=False)
    at = {s["at"]: s for s in out["samples"]}
    assert at["settled"]["ty"] == 0.0, (
        f"after release + 0.64s of animation the pet is at translateY="
        f"{at['settled']['ty']}, not on the ground — it is stuck in the air")
    assert at["settled"]["state"] != "drag", "the pet is still in the drag state"
    assert at["settled"]["state"] != "fall", (
        "the pet is still falling after 0.64s — the drop never terminates")


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_the_cast_shadow_tracks_the_height_continuously():
    """The shadow must agree with the body, at every height.

    This is the channel that was previously LYING: it shrank on [data-state=drag]
    while the body stayed planted. Now it is driven by --pet-lift, so the two
    can only ever tell the same story.
    """
    out = _run(reduced=False)
    at = {s["at"]: s for s in out["samples"]}
    assert at["rest"]["lift"] in (None, 0.0), (
        f"the shadow is already shrunken at rest (--pet-lift={at['rest']['lift']})")
    assert at["after_grab"]["lift"] > 0, (
        "the pet is lifted but --pet-lift is 0, so the shadow still paints as if "
        "it were on the ground")
    assert at["carried_up"]["lift"] > at["after_grab"]["lift"], (
        "carrying the pet higher did not shrink the shadow further — the shadow "
        "is not tracking the height")
    assert at["settled"]["lift"] == 0.0, (
        f"the pet has landed but --pet-lift={at['settled']['lift']} — the shadow "
        "stays detached under a grounded pet")


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_reduced_motion_keeps_the_pet_planted():
    """ACCESSIBILITY (WCAG 2.3.3): under prefers-reduced-motion the pet does not
    roam or chase, and it must not be flung into the air either — the drag still
    works, it just stays on the baseline."""
    out = _run(reduced=True)
    at = {s["at"]: s for s in out["samples"]}
    for label in ("after_grab", "carried_up", "settled"):
        assert at[label]["ty"] == 0.0, (
            f"under reduced-motion the pet left the ground at {label} "
            f"(translateY={at[label]['ty']})")


# ══════════════════════════════════════════════════════════════════════
#  STRUCTURAL: one transform writer, and the squash cannot clobber it
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_a_long_hold_still_lands_when_the_previous_state_expires():
    """LANDING, after holding long enough that the pre-drag state times out.

    Found because a neuter MISSED. Removing the ``fall`` exclusion from the
    generic state-timeout at the end of _step left the quick-drag test green,
    so on that evidence the exclusion looked like dead defensiveness.

    It is not — the scenario simply never reached it. A drag normally begins
    from 'walk', whose ``until`` is 1400-3000ms in the FUTURE, so the timeout
    branch cannot fire during a brief drag no matter how it is written. Hold the
    pet a few seconds (an entirely ordinary thing to do) and that stale ``until``
    lapses; then, without the exclusion, the first airborne frame calls
    _pickNext() and the pet is captured into a resting state MID-AIR.

    Measured with the exclusion removed and a 6s hold: the pet settles at
    translateY=-12 in state 'sleep' — floating, permanently, with no leg left to
    bring it down. The quick-drag tests above cannot see that, which is exactly
    why this case is pinned separately.
    """
    src = PET_JS.read_text(encoding="utf-8")
    code = (_HARNESS
            .replace("_listeners.pointerdown({ button:0, pointerId:1, clientX:100, clientY:40 });",
                     "_listeners.pointerdown({ button:0, pointerId:1, clientX:100, clientY:40 });\n"
                     "__NOW__ += 6000;   // hold long enough for the pre-drag state's `until` to lapse")
            .replace("__SRC__", src)
            .replace("__REDUCED__", "false"))
    r = subprocess.run([_NODE, "-e", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:2000]}"
    out = json.loads(r.stdout.strip().splitlines()[-1])
    at = {s["at"]: s for s in out["samples"]}

    assert at["released"]["ty"] < 0, "the held pet was not airborne at release"
    assert at["settled"]["ty"] == 0.0, (
        f"after a LONG hold the pet settled at translateY={at['settled']['ty']} "
        f"in state {at['settled']['state']!r} — it was captured by the generic "
        "state timeout while still in the air and can never come down")
    assert at["settled"]["state"] not in ("fall", "drag"), (
        f"the pet is stuck in {at['settled']['state']!r} after landing")


@pytest.mark.skipif(_NODE is None, reason="node not installed")
@pytest.mark.parametrize("terminator,label", [
    ("_listeners.pointercancel({ pointerId:1 });", "pointercancel"),
    ("_listeners.lostpointercapture({ pointerId:1 });", "lostpointercapture"),
])
def test_an_interrupted_drag_still_lands(terminator, label):
    """EVERY way a drag can end must put the pet back on the ground.

    ``pointercancel`` is NOT an error path. The browser fires it on
    touch-scroll, on window blur / alt-tab mid-drag, and whenever the OS takes
    over the gesture — on a touch device it is a ROUTINE way for a drag to end.
    ``lostpointercapture`` fires when capture is taken away, which matters here
    because setPointerCapture sits inside a try/except: if capture is refused,
    a pointerup landing outside the element never reaches the handler.

    Measured BEFORE the fix, settled state after each terminator:

        pointerup           ty=0    lift=0      idle    OK
        pointercancel       ty=-12  lift=0.353  drag    STUCK
        lostpointercapture  ty=-12  lift=0.353  drag    STUCK (not even wired)

    Stuck is permanent, not transient: _step early-returns while the state is
    'drag', so no later frame can bring the pet down AND the whole wander loop
    is dead too. The pet hangs in the air until the page is reloaded — strictly
    worse than the missing-lift defect this feature set out to fix.

    Parametrized rather than written twice because the POINT is that the
    behaviour is identical across entry points: they share one implementation
    (_endDrag), so a future fourth entry point cannot quietly diverge.
    """
    src = PET_JS.read_text(encoding="utf-8")
    code = (_HARNESS
            .replace("_listeners.pointerup({ pointerId:1, clientX:180, clientY:40 });",
                     terminator)
            .replace("__SRC__", src)
            .replace("__REDUCED__", "false"))
    r = subprocess.run([_NODE, "-e", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:2000]}"
    out = json.loads(r.stdout.strip().splitlines()[-1])
    at = {s["at"]: s for s in out["samples"]}

    assert at["after_grab"]["ty"] < 0, "the pet was never lifted — precondition failed"
    assert at["settled"]["ty"] == 0.0, (
        f"after {label} the pet settled at translateY={at['settled']['ty']} in "
        f"state {at['settled']['state']!r} — it is stranded in mid-air with no "
        "leg left to bring it down, recoverable only by reloading the page")
    assert at["settled"]["state"] != "drag", (
        f"after {label} the pet is still in the drag state, so _step keeps "
        "early-returning and the wander loop never resumes")
    assert at["settled"]["lift"] == 0.0, (
        f"after {label} --pet-lift is {at['settled']['lift']}, so the shadow "
        "still paints detached under a pet that is back on the ground")


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_ending_a_drag_twice_is_harmless():
    """A cancel arriving AFTER a normal release must not re-trigger anything.

    Browsers do emit both (pointerup then pointercancel) in some gesture
    hand-offs, and _endDrag is reachable from three listeners, so it has to be
    idempotent — otherwise the second call would credit a second interaction or
    restart a fall the pet already finished.
    """
    src = PET_JS.read_text(encoding="utf-8")
    code = (_HARNESS
            .replace("_listeners.pointerup({ pointerId:1, clientX:180, clientY:40 });",
                     "_listeners.pointerup({ pointerId:1, clientX:180, clientY:40 });\n"
                     "_listeners.pointercancel({ pointerId:1 });")
            .replace("__SRC__", src)
            .replace("__REDUCED__", "false"))
    r = subprocess.run([_NODE, "-e", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:2000]}"
    out = json.loads(r.stdout.strip().splitlines()[-1])
    at = {s["at"]: s for s in out["samples"]}
    assert at["settled"]["ty"] == 0.0 and at["settled"]["state"] != "drag", (
        "a duplicate terminator left the pet airborne or stuck in drag")


def test_every_drag_terminator_routes_through_one_release_path():
    """STRUCTURAL: three listeners, ONE implementation.

    The defect was not that pointercancel had the wrong body — it was that a
    second entry point existed with its OWN body, which did half the job. Any
    future fourth entry point must reuse the same function rather than
    re-deriving what "the drag ended" means.
    """
    js = _js_code()
    assert re.search(r"function _endDrag\(", js), (
        "_endDrag is gone — the release semantics have been inlined again")
    for ev in ("pointerup", "pointercancel", "lostpointercapture"):
        assert f"'{ev}'" in js, f"the {ev} listener is not wired at all"
    m = re.search(r"function _wireDrag\(\) \{(.*?)\n  \}", js, re.S)
    assert m, "could not isolate _wireDrag"
    body = m.group(1)
    for ev in ("pointercancel", "lostpointercapture"):
        seg = re.search(re.escape(ev) + r"'.*?\}\s*\)", body, re.S)
        assert seg and "_endDrag" in seg.group(0), (
            f"the {ev} listener does not route through _endDrag — it is "
            "re-implementing termination and will drift out of sync again")


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_a_held_pet_cannot_be_lifted_out_of_its_own_bar():
    """The lift ceiling must keep the sprite inside the bar's visual band.

    The pet is a GRABBABLE element raised to z-index 5 while held, and the bar
    is `overflow:visible`, so nothing clips it — an overhang paints over
    whatever sits above the bar and can cover a hit target. (The speech bubble
    also pokes above the rim, but it is `pointer-events:none` and therefore
    cannot steal a click; the pet is not.)

    Measured with the previous hand-written ceiling of 34px: the sprite's top
    reached 65px against a ~46-48px bar — roughly 19px of overhang, purely as
    an accident of an untested constant. The ceiling is now DERIVED in
    _measure() from the bar's own height, so re-padding the bar or resizing the
    sprite cannot silently re-open it.

    Driven by yanking the pointer far above the bar, which is what a real flick
    does.
    """
    src = PET_JS.read_text(encoding="utf-8")
    code = (_HARNESS
            .replace("clientX:160, clientY:25", "clientX:160, clientY:-400")
            .replace("__SRC__", src)
            .replace("__REDUCED__", "false"))
    r = subprocess.run([_NODE, "-e", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:2000]}"
    out = json.loads(r.stdout.strip().splitlines()[-1])
    at = {s["at"]: s for s in out["samples"]}

    lift = -at["carried_up"]["ty"]
    assert lift > 0, "the pet did not lift at all when yanked upward"
    # harness bar rect: height 48; .tofu-pet { bottom:1px; height:30px }
    bar_h, bottom, pet_h = 48, 1, 30
    top = bottom + pet_h + lift
    assert top <= bar_h, (
        f"a hard upward flick lifts the sprite's top to {top}px against a "
        f"{bar_h}px bar — {top - bar_h}px of overhang, painted at z-index 5 over "
        "whatever sits above the project bar")


def test_the_lift_ceiling_is_derived_not_hand_written():
    """STRUCTURAL: the ceiling must come from the measured box.

    A literal was measured wrong exactly once (34px against a ~46px bar). The
    behavioural test above only catches that after someone runs it against the
    harness's bar size; asserting the mechanism keeps the intent visible in the
    source, where the next person editing the padding will see it.
    """
    js = _js_code()
    m = re.search(r"function _measure\(\) \{(.*?)\n  \}", js, re.S)
    assert m, "could not isolate _measure"
    assert "LIFT_MAX_PX =" in m.group(1), (
        "LIFT_MAX_PX is no longer recomputed in _measure() — it has gone back "
        "to being a constant that cannot track the bar it is supposed to fit")
    assert re.search(r"LIFT_MAX_PX\s*=\s*Math\.max\(", m.group(1)), (
        "the derived ceiling lost its floor; on a very short bar the lift would "
        "round to zero and the original 'no vertical change' defect returns")


def test_position_layer_has_exactly_one_transform_writer():
    """Both axes must be written in ONE declaration.

    `transform` is a single property, not two channels: a second write of
    `translateY` on .tofu-pet would silently drop the translateX the wander loop
    just set, teleporting the pet to the bar's left edge. So the engine must
    compose both in the same assignment — which is why the drop integrates into
    W.y and lets _place() do the writing, instead of animating the position
    layer from CSS.
    """
    js = _js_code()
    writes = re.findall(r"_el\.style\.transform\s*=", js)
    assert len(writes) == 1, (
        f"{len(writes)} writers of .tofu-pet's transform — with more than one, "
        "whichever runs last erases the other axis")
    m = re.search(r"_el\.style\.transform\s*=\s*([^;]+);", js)
    decl = m.group(1)
    assert "translateX" in decl and "translateY" in decl, (
        f"the single transform write does not carry both axes: {decl!r}")


def test_landing_squash_lives_on_the_frame_layer():
    """The landing animation must NOT be on the position layer.

    An animation always wins over an inline transform on the SAME element, so a
    keyframe on .tofu-pet would override the drop's per-frame translateY and the
    pet would visibly jump. Same reasoning that put the facing mirror on the
    <img> children and the pivot on .tofu-pet-img.
    """
    css = _css_code()
    m = re.search(r"([^{}]*\[data-landing\][^{}]*)\{([^}]*)\}", css)
    assert m, "no [data-landing] rule — the landing squash is not wired"
    sel, body = m.group(1).strip(), m.group(2)
    assert ".tofu-pet-img" in sel, (
        f"the landing squash is on {sel!r}, the POSITION layer — it would "
        "override the drop's translateY and make the landing jump")
    assert "tofuPetLand" in body, "the [data-landing] rule plays no landing animation"
    assert re.search(r"@keyframes\s+tofuPetLand\b", css), "tofuPetLand keyframes missing"


def test_the_shadow_is_height_driven_not_state_driven():
    """The cast shadow must read --pet-lift, not [data-state="drag"].

    A state-gated shadow stops applying the instant the pointer is released —
    but the pet is still airborne then (it is falling), so the shadow would pop
    back to full size under a sprite that has not landed yet.
    """
    css = _css_code()
    assert "var(--pet-lift" in css, "the shadow no longer reads the lift height"
    m = re.search(r'\.tofu-pet\[data-state="drag"\]::after\{([^}]*)\}', css)
    assert not m, (
        "the cast shadow is sized by the drag STATE again — it will snap between "
        "two fixed sizes and will be wrong during the release fall")


def test_a_grab_asserts_lift_rather_than_deriving_it_from_motion():
    """The grab impulse must be applied on STATE ENTRY.

    Deriving height purely from pointer movement means a horizontal drag never
    lifts the pet — the precise complaint. So the engine sets a baseline lift at
    the moment the press becomes a drag, and the pointer's own rise adds to it.
    """
    js = _js_code()
    assert re.search(r"LIFT_GRAB_PX", js), "no grab impulse constant"
    m = re.search(r"if \(!D\.moved\) \{(.*?)\n    \}", js, re.S)
    assert m, "could not isolate the slop-crossing branch"
    assert "baseY" in m.group(1), (
        "crossing the drag slop does not set a baseline lift, so a purely "
        "sideways drag would leave the pet on the floor")
