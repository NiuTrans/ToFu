"""The pet's facing must agree with its travel, and its walk must read as a walk.

THE REPORT THIS SUITE NOW GUARDS (2026-07-30)
─────────────────────────────────────────────
"the pet walks backward — facing left while moving right". The previous art was
an ISOMETRIC cube whose three faces baked a sun in; mirroring the whole sprite
would teleport that sun, so the flip had been confined to a character-space
subgroup (face+feet turned, body never did). That kept the light consistent —
and froze the body's 3/4 orientation: half the time the cube's depth face
pointed against the direction of travel. One-sun consistency and facing
consistency are STRUCTURALLY mutually exclusive for a 3/4 body, so the revamp
replaced the art with a FRONT-FACING AI-designed character (soft symmetric
shading, no baked sun). With nothing to keep in world space, the WHOLE frame
mirrors on the <img> elements and body + gaze + stride always agree with
travel. The scene-sun coupling survives where it is art-independent: the
::after cast shadow + the CSS filter form light.

Older defects this file guarded, and where they now stand:

1. LIGHT TELEPORTED ON TURN (2026-07-29) — moot: no baked sun, nothing to
   teleport. The form light is a CSS filter (drop-shadow/brightness), which
   follows the sprite's own alpha at any facing.
2. THE GAIT NEVER ALTERNATED (2026-07-29) — still guarded, now on the shipped
   PIXELS: the two contact beats must show a wide split with the pair shifted
   between beats, and the passing beats must gather.
3. THE CYCLE RAN AT 6.7fps (2026-07-29) — still guarded: 8 slots at 75ms.

No node, no browser here: CSS/JS source contracts (comments stripped per
charter #24) + PIL pixel proofs on the shipped PNGs.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
PIPELINE = REPO / "static" / "icons" / "_gen" / "tofu-pet" / "process_ai_frames.py"
FRAME_DIR = REPO / "static" / "icons" / "pet" / "tofu"
sys.path.insert(0, str(REPO))
from tests._runtime_sections import runtime_section_path

# The classic js tree is gone; sections materialize from the retained
# runtime (lazy chunks are concatenated back into the virtual view).
PET_JS = Path(runtime_section_path('tofu-pet.js'))
CSS = REPO / "static" / "styles.css"

WALK_FRAMES = ["walk1", "walk2", "walk3", "walk4", "walk5", "walk6", "walk7", "walk8"]

#: The pixel-morphology proofs (connected-component body masks) need
#: scipy.ndimage — an optional imaging dep that is NOT part of the public CI
#: footprint. Skip those tests loudly where it is absent; every pure
#: source-contract guard in this file still runs everywhere.
_requires_scipy = pytest.mark.skipif(
    importlib.util.find_spec('scipy') is None,
    reason='scipy not installed — the ndimage body-mask pixel proofs require '
           'it (optional imaging dep, absent on public CI)')


def _js_code() -> str:
    """tofu-pet.js with comments stripped (charter #24: a guard scanning source
    text strips comments first — this module's own comments quote the very
    idioms being asserted-absent, so a raw scan would fire on the explanation)."""
    sys.path.insert(0, str(REPO / "tests"))
    from _source_scan import strip_comments

    return strip_comments(PET_JS.read_text(encoding="utf-8"), lang="js")


def _css_code() -> str:
    sys.path.insert(0, str(REPO / "tests"))
    from _source_scan import strip_comments

    return strip_comments(CSS.read_text(encoding="utf-8"), lang="css")


# ══════════════════════════════════════════════════════════════════════
#  1. THE WHOLE FRAME TURNS — no partial-mirror machinery may come back
# ══════════════════════════════════════════════════════════════════════

def test_mirror_is_whole_frame_on_the_img_elements():
    """The facing flip must mirror the ENTIRE visible frame: body + gaze +
    stride turn together, so the pet can never moonwalk again.

    The mirror lives on the <img> frame ELEMENTS — children of the animated
    .tofu-pet-img layer — because an animation always wins over a static
    transform on the SAME element: put the flip on the layer that breathes and
    it silently stops applying.
    """
    css = _css_code()
    m = re.search(r"\.tofu-pet \.tofu-pet-img > img\{([^}]*)\}", css)
    assert m, "no `.tofu-pet-img > img` rule — the raster frames are unstyled"
    body = m.group(1)
    assert "scaleX(var(--pet-face-flip" in body, (
        "the facing flip is not on the <img> frame elements. A partial mirror "
        "(only the face turns) is exactly how the pet moonwalked."
    )
    assert "object-fit:contain" in body, (
        "frames must object-fit:contain — a fixed 30px box would otherwise "
        "STRETCH every pose to the same box and the squash/stretch contrast "
        "the poses carry would be flattened."
    )
    assert "object-position" in body and "bottom" in body, (
        "frames must bottom-anchor (object-position … bottom): a wide squash "
        "pose is shorter than the box, and the centre default would float its "
        "feet off the ground."
    )
    assert "--pet-face-flip:1" in css, (
        "--pet-face-flip has no default — a pet that has not turned yet would "
        "resolve the var to nothing."
    )


def test_the_flip_is_a_bare_property_write_written_by_face():
    """_face() must write --pet-face-flip (±dir) directly, with no transition
    anywhere near it — tweening +1→−1 passes through scaleX(0) and smears the
    frame (the paper-flip bug, mechanism-independent)."""
    js = _js_code()
    m = re.search(r"function _face\(dir, pivot\)\s*\{(.*?)\n  \}", js, re.S)
    assert m, "could not isolate _face"
    assert re.search(r"setProperty\('--pet-face-flip',\s*String\(dir\)\)", m.group(1)), (
        "_face no longer writes --pet-face-flip from dir — nothing turns the pet."
    )
    css = _css_code()
    for sel, body in re.findall(r"(\.[^{]*)\{([^}]*)\}", css):
        if "--pet-face-flip" in body or "pet-face-flip" in sel:
            assert "transition" not in body, (
                f"rule {sel!r} touches the facing flip AND declares a transition "
                "— the flip must be instant or the frame smears through 0."
            )


def test_no_character_space_machinery_remains():
    """The SVG two-space split (world/char) is gone for good: its whole point
    was protecting a baked sun the new art does not have, and its cost was the
    frozen body that moonwalked. Absence asserted on COMMENT-STRIPPED code —
    the module's own comments legitimately narrate this history."""
    js = _js_code()
    assert "data-space" not in js, (
        "character-space mirror machinery is back in the engine. With raster "
        "frames there is no baked sun to protect — a partial mirror can only "
        "re-introduce the moonwalk."
    )
    for var in ("--pet-front-a", "--pet-front-b", "--pet-right-a", "--pet-right-b",
                "--pet-top-a", "--pet-top-b", "--pet-sheen", "--pet-rim"):
        assert var not in js, (
            f"{var} is written again — the per-face recolour channel was SVG "
            "gradient machinery; on raster art it is dead code pretending to "
            "light something."
        )
    stray = sorted(FRAME_DIR.glob("*.svg"))
    assert not stray, (
        f"SVG frames are back on disk: {[p.name for p in stray]} — the shipped "
        "art is the AI-designed PNG set."
    )


# ══════════════════════════════════════════════════════════════════════
#  2. THE WALK CYCLE READS AS A WALK (pixel proofs on the shipped PNGs)
# ══════════════════════════════════════════════════════════════════════

def _foot_zone_runs(name: str):
    """(runs, span) of the FEET at native resolution.

    The foot zone = the contiguous bottom rows whose ink width is under 55% of
    the frame's widest row (i.e. below the body silhouette). Inside it, runs =
    x-runs of alpha in the column projection, merging gaps ≤ 2px. A position-
    only check cannot see a foot that is hidden or fused — measured on the
    earlier art, four of eight frames rendered zero/one foot while the offset
    table said everything was fine.
    """
    import numpy as np
    from PIL import Image

    a = np.asarray(Image.open(FRAME_DIR / f"tofu-{name}.png").convert("RGBA"))
    alpha = a[..., 3]
    h = a.shape[0]
    maxw = max((alpha[y] > 40).sum() for y in range(h))
    zone_rows = []
    for y in range(h - 1, -1, -1):
        rw = (alpha[y] > 40).sum()
        if rw == 0:
            continue
        if rw < 0.55 * maxw:
            zone_rows.append(y)
        else:
            break
    assert zone_rows, f"tofu-{name}: no foot zone found (nothing below the body)"
    zone = alpha[min(zone_rows):max(zone_rows) + 1, :]
    proj = (zone > 40).sum(axis=0)
    xs = list(np.where(proj > 0)[0])
    runs = []
    if xs:
        s = p = xs[0]
        for x in xs[1:]:
            if x > p + 2:
                runs.append((int(s), int(p)))
                s = x
            p = x
        runs.append((int(s), int(p)))
    span = (runs[-1][1] - runs[0][0]) if runs else 0
    return runs, span


def test_every_walk_frame_renders_feet():
    """Each walk frame must actually DRAW feet below the body (alpha ink in the
    foot zone), natively AND at the shipped 30px — a stride with no feet is a
    hover, and a gap that vanishes at 30px is a fused blob."""
    import numpy as np
    from PIL import Image

    for name in WALK_FRAMES:
        runs, _ = _foot_zone_runs(name)
        assert runs, f"tofu-{name} renders no feet at native size"
        im = Image.open(FRAME_DIR / f"tofu-{name}.png").convert("RGBA")
        s = 30 / im.height
        small = np.asarray(im.resize((max(1, round(im.width * s)), 30), Image.LANCZOS))
        assert (small[..., 3] > 40).any(), f"tofu-{name} renders nothing at 30px"


def test_contact_beats_split_passing_beats_gather():
    """The stride's rhythm: feet SPLIT wide at the contact beats (walk1/walk3)
    and GATHER under the body at the passing beats (walk2/walk4). Measured as
    the foot-zone span: contact must be meaningfully wider than passing."""
    _, span1 = _foot_zone_runs("walk1")
    _, span2 = _foot_zone_runs("walk2")
    _, span3 = _foot_zone_runs("walk3")
    _, span4 = _foot_zone_runs("walk4")
    assert span1 > span2 * 1.25 and span3 > span4 * 1.25, (
        f"contact/passing spans do not alternate: walk1={span1} walk2={span2} "
        f"walk3={span3} walk4={span4}. A cycle whose feet never gather reads as "
        "scissoring in place, not stepping."
    )
    for contact in ("walk1", "walk3"):
        runs, _ = _foot_zone_runs(contact)
        assert len(runs) >= 2, (
            f"{contact} shows one fused foot blob ({runs}) — the two feet must "
            "separate at the contact beat or there is no stride to see."
        )


def test_the_leading_side_alternates_between_contact_beats():
    """ANTI-MOONWALK, at the pixels: the foot pair must SHIFT between the two
    contact beats — one beat reaches further left, the other further right.
    Both beats posing the same lead is the shuffle that read as sliding
    backwards. (Direction consistency itself is now structural: the whole
    frame mirrors, see test_mirror_is_whole_frame_on_the_img_elements.)"""
    runs1, _ = _foot_zone_runs("walk1")
    runs3, _ = _foot_zone_runs("walk3")
    mid1 = (runs1[0][0] + runs1[-1][1]) / 2
    mid3 = (runs3[0][0] + runs3[-1][1]) / 2
    assert abs(mid3 - mid1) > 3, (
        f"the foot pair sits at the same place on both contact beats "
        f"(mid {mid1:.1f} vs {mid3:.1f}) — the lead never swaps, so the feet "
        "slide as a pair."
    )


def test_walk_cycle_frames_all_exist_and_the_replay_map_holds():
    """8 engine slots exist on disk; the back half of the list REPLAYS the four
    authored drawings (walk5..8 == walk1..4), and the four authored drawings
    are pairwise distinct (a cycle with duplicate keyposes stutters)."""
    js = _js_code()
    m = re.search(r"var WALK_FRAMES = \[(.*?)\];", js, re.S)
    assert m, "WALK_FRAMES not found in tofu-pet.js"
    listed = re.findall(r"'(walk\d+)'", m.group(1))
    assert listed == WALK_FRAMES, f"engine walk list {listed} != expected {WALK_FRAMES}"
    bodies = {}
    for name in listed:
        p = FRAME_DIR / f"tofu-{name}.png"
        assert p.exists() and p.stat().st_size > 0, f"engine plays {name} but {p} is missing"
        bodies[name] = p.read_bytes()
    for a, b in (("walk1", "walk5"), ("walk2", "walk6"), ("walk3", "walk7"), ("walk4", "walk8")):
        assert bodies[a] == bodies[b], (
            f"{b} no longer replays {a} — the stride's back half-cycle must be "
            "the same four drawings again (pipeline FRAME_SOURCES contract)."
        )
    uniq = ["walk1", "walk2", "walk3", "walk4"]
    for i, a in enumerate(uniq):
        for b in uniq[i + 1:]:
            assert bodies[a] != bodies[b], f"tofu-{a} and tofu-{b} are byte-identical"


def test_walk_cycle_clears_the_twelve_fps_floor():
    """The DERIVED cadence must clear the ~12fps flicker floor at the walk speed.

    REVERSED IN PLACE (2026-07-31), not deleted. This test used to read a literal
    `var WALK_FRAME_MS = 75`. That literal was the BUG: a fixed interval makes the
    gait's cadence independent of how fast the body actually travels, so the two
    silently disagreed and the feet churned 1.62× faster than the pet moved
    (foot-slip). Worse, ONE constant cannot be right for THREE speeds (walk 41 /
    chase 82 / flee 120 px/s). The engine now DERIVES the interval from the
    measured stride and the live speed, so the old assertion could only ever be
    satisfied by re-introducing the defect. The PROPERTY it cared about — the
    cycle must not read as a flicker between drawings — is asserted here on the
    derived value instead, which is strictly stronger.
    """
    js = _js_code()
    stride = float(re.search(r"var STRIDE_PX = ([\d.]+)", js).group(1))
    distinct = int(re.search(r"var WALK_DISTINCT = (\d+)", js).group(1))
    min_ms = float(re.search(r"var MIN_FRAME_MS = ([\d.]+)", js).group(1))
    speed = float(re.search(r"speed: ([\d.]+),", js).group(1))

    per = max(min_ms, stride / speed * 1000.0 / distinct)
    fps = 1000.0 / per
    assert fps >= 12.0, (
        f"the derived walk cadence is {fps:.1f}fps ({per:.1f}ms/keypose), below "
        "the ~12fps floor at which a keypose cycle stops reading as motion. "
        f"Tune W.speed (currently {speed}) — the interval is derived, not set."
    )
    assert fps <= 30.0, (
        f"the derived cadence is {fps:.1f}fps — faster than the art can justify; "
        "the pet would blur its four drawings."
    )


def test_the_gait_does_not_slip_against_travel():
    """FOOT-SLIP: the feet must advance at the speed the BODY travels.

    Measured on the shipped PNGs: one foot's travel from LEADING (walk1) to
    TRAILING (walk3) is the ground distance the art claims per half gait cycle.
    The body's real travel over that same time is speed × cycle_duration. If the
    art claims more ground than the body covers, the legs churn without purchase
    — the "hummingbird legs on a sliding body" read, measured at 1.62× before the
    interval became derived.

    This is deliberately measured from the ART, not from STRIDE_PX, so the guard
    also fails if someone edits STRIDE_PX away from what the drawings show.
    """
    import numpy as np
    from PIL import Image

    def foot_centres(name):
        runs, _ = _foot_zone_runs(name)
        canvas_w, canvas_h = Image.open(FRAME_DIR / f"tofu-{name}.png").size
        scale = 30.0 / max(canvas_w, canvas_h)     # CSS contain into the 30px box
        return [((a + b) / 2.0) * scale for a, b in runs]

    c1, c3 = foot_centres("walk1"), foot_centres("walk3")
    assert len(c1) >= 2 and len(c3) >= 2, "contact beats must show two feet"
    measured_stride = abs(max(c1) - min(c3))

    js = _js_code()
    declared = float(re.search(r"var STRIDE_PX = ([\d.]+)", js).group(1))
    assert abs(declared - measured_stride) <= 1.0, (
        f"STRIDE_PX={declared} but the shipped art measures "
        f"{measured_stride:.2f} rendered px of foot travel. The constant has "
        "drifted from the drawings, so the derived cadence is wrong."
    )

    distinct = int(re.search(r"var WALK_DISTINCT = (\d+)", js).group(1))
    min_ms = float(re.search(r"var MIN_FRAME_MS = ([\d.]+)", js).group(1))
    for speed_name, pattern in (("walk", r"speed: ([\d.]+),"),
                                ("chase", r"chaseSpeed: ([\d.]+),"),
                                ("flee", r"fleeSpeed: ([\d.]+),")):
        speed = float(re.search(pattern, js).group(1))
        per = max(min_ms, measured_stride / speed * 1000.0 / distinct)
        travel = speed * (per * distinct) / 1000.0
        slip = measured_stride / travel
        assert slip <= 1.15, (
            f"{speed_name} leg slips {slip:.2f}× (art claims "
            f"{measured_stride:.2f}px of foot travel, body covers {travel:.2f}px "
            f"per cycle at {speed}px/s). The feet must not outrun the body."
        )


def test_the_gait_interval_is_derived_from_the_live_speed():
    """STRUCTURAL: there must be no hard-coded frame interval, and every moving
    leg must advance the gait through the ONE speed-aware advancer.

    Three copies of the advance loop against one literal is how walk / chase /
    flee came to share a cadence that could only be correct for one of them.
    """
    js = _js_code()
    assert "WALK_FRAME_MS" not in js, (
        "a fixed WALK_FRAME_MS is back. A literal interval cannot be right for "
        "walk, chase AND flee simultaneously, and it decouples the cadence from "
        "travel — which is exactly the foot-slip defect."
    )
    assert re.search(r"function _gaitMs\(speed\)", js), (
        "_gaitMs(speed) is gone — the interval is no longer derived from speed."
    )
    m = re.search(r"function _gaitMs\(speed\) \{(.*?)\n  \}", js, re.S)
    assert "STRIDE_PX" in m.group(1) and "speed" in m.group(1), (
        "_gaitMs no longer computes from STRIDE_PX and speed."
    )
    adv = re.search(r"function _advanceGait\(dt, speed\) \{(.*?)\n  \}", js, re.S)
    assert adv, "_advanceGait(dt, speed) is gone — the shared advancer was the fix"
    assert "_gaitMs(speed)" in adv.group(1), (
        "_advanceGait ignores the speed it was handed — the cadence would again "
        "be independent of travel."
    )
    # every moving leg routes through it, with the speed that leg actually uses
    for speed_expr in ("W.speed", "W.chaseSpeed", "W.fleeSpeed"):
        assert f"_advanceGait(dt, {speed_expr})" in js, (
            f"the leg travelling at {speed_expr} does not advance its gait "
            "through _advanceGait — it would slip against its own speed."
        )


def test_entering_a_walk_asserts_facing():
    """A walk must set its facing, not inherit whatever the last state left.

    'gaze' deliberately flips facing every GAZE_TURN_MS while standing still, so
    a walk entered straight out of a glance could set off facing away from its
    direction of travel.
    """
    js = _js_code()
    m = re.search(r"if \(state === 'walk'\) \{(.*?)\} else if", js, re.S)
    assert m, "could not isolate the _enter('walk') branch"
    assert "_face(" in m.group(1), (
        "entering 'walk' no longer asserts facing — reachable in one hop: the "
        "'gaze' state flips facing on a timer, then _pickNext() can enter 'walk'."
    )


# ══════════════════════════════════════════════════════════════════════
#  4. THE CHARACTER IS THE SAME SIZE, IN THE SAME PLACE, IN EVERY FRAME
#
#  THE DEFECT THIS SECTION EXISTS FOR (measured 2026-07-31, previously
#  UNGUARDED — all 82 pet guards were green while it shipped):
#  process_ai_frames.py trimmed EACH frame to its own alpha bbox and scaled it
#  so ITS OWN longest side hit MAX_SIDE. The scale factor was therefore a
#  FUNCTION OF THE POSE (0.1837–0.2319, a 26% spread), which broke the
#  character three ways at once:
#    · SIZE WOBBLE — blush cheek-to-cheek span varied 16.5% across the frames
#      that share the character's stance, and 9.7% within walk1..4, i.e. the pet
#      breathed a tenth of its own width 13 times a second while walking. This
#      is what "glitching out" looked like. (Population matters: over all 22
#      frames the figure was 73.8% before / 50.0% after, but scratch1/scratch2
#      are deliberate stretch/squash extremes whose size SHOULD differ — they
#      are excluded from the stance set below for exactly that reason, and
#      test_the_pipeline_scales_every_frame_by_the_SAME_factor covers all 22
#      with the criterion that actually applies to them.)
#    · LATERAL TELEPORT — re-centring on the INK bbox let asymmetric FX
#      (thinking bubble, sparkles) shove the BODY sideways by up to 2.27px on a
#      30px sprite: an 8%-of-width jump fired by a MOOD change, nothing moving.
#    · FX PAID FOR THEMSELVES IN BODY SIZE — a pose with bigger sparkles got
#      scaled down, shrinking the body to make room for them.
#  The masters were innocent (walk body width varies 1.2% in the raws vs 6.4%
#  shipped), so the fix was one global scale + a body-centre/foot-line anchor.
#  These guards assert the RESULT on the shipped pixels, so the defect cannot
#  return through any future edit to the pipeline.
# ══════════════════════════════════════════════════════════════════════

# The character's rigid features must not change size between poses BEYOND what
# the drawings themselves vary. Two different tolerances, because they measure
# two different things:
#   · _SCALE_FIDELITY_PX — how far a shipped frame may deviate from EXACT
#     global scaling of its master. This is the real anti-regression bound: the
#     old per-frame pipeline blew it by ~2px, and integer rounding costs ~0.15px.
#   · _RIGID_TOL_PX — the absolute spread allowed across same-stance poses. It
#     must accommodate the artist's own line-width variation (measured 0.74px:
#     the raw masters' body width spans 651..674 canvas px), which a faithful
#     pipeline is REQUIRED to preserve, not iron out.
# Asserting the artist drew every pose pixel-identically would be asserting
# something false, so the load-bearing guard is scale FIDELITY, not equality.
_SCALE_FIDELITY_PX = 0.35
_RIGID_TOL_PX = 1.0


# ── The MASTERS are private, so master-dependent guards must degrade to a skip ──
# The raw 1024² poses live under a `_candidates` path segment, which export.py
# lists in ALWAYS_EXCLUDE_DIRS ("raw AI-gen asset candidates, review-only,
# multi-MB"). They are deliberately NOT published: 5.7MB of review art that the
# public build has no use for, since it ships the finished frames.
#
# But tests/ IS published, and so is the pipeline. A guard that re-derives the
# global scale from the masters therefore has no subject in the exported tree
# and raised FileNotFoundError there — 3 hard failures introduced by the very
# batch that added them (measured: 1 pre-existing failure before, 4 after).
#
# The masters cannot simply be shipped (that reverses a deliberate size/privacy
# decision) and the guards must not be deleted (they are the anti-regression
# bound for the whole size-constancy fix). So the split is by SUBJECT
# AVAILABILITY: assertions that read only the SHIPPED frames run everywhere;
# assertions that need the masters run wherever the masters exist — which is
# every private checkout, i.e. every place the pipeline can actually be re-run
# and thus every place the invariant could be broken.
def _require_masters():
    """Skip when the raw masters are absent (an exported tree).

    NOT a blanket try/except: it checks the ONE precondition and says why. A
    guard silently passing on a tree where its subject is missing would be the
    worse outcome — that is how a green suite stops meaning anything.
    """
    sys.path.insert(0, str(PIPELINE.parent))
    import process_ai_frames as P

    if not P.RAW_DIR.is_dir() or not any(P.RAW_DIR.glob("*.png")):
        pytest.skip(
            f"raw masters absent ({P.RAW_DIR}) — export.py strips `_candidates` "
            "from every published build, so this master-derived assertion has "
            "no subject here. It runs in every private checkout, which is "
            "everywhere the pipeline can be re-run.")
    return P


def _body_mask(path):
    """The BODY as a boolean mask: the largest opaque connected component.

    Largest-component, not the alpha bbox: detached FX (sparkles, the thinking
    bubble, Zzz) are separate components, and letting them into the measurement
    is precisely the mistake that made the body's size and position pose-
    dependent.
    """
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    a = np.asarray(Image.open(path).convert("RGBA"))
    solid = a[..., 3] > 60
    labels, n = ndimage.label(solid)
    assert n >= 1, f"{path.name}: no opaque ink at all"
    sizes = ndimage.sum(solid, labels, range(1, n + 1))
    return labels == (int(np.argmax(sizes)) + 1), a


def _all_frames():
    return sorted(p for p in FRAME_DIR.glob("tofu-*.png"))


def test_every_frame_shares_one_canvas():
    """All frames must be the SAME pixel size.

    This is what makes the CSS `object-fit:contain` a no-op scale rather than a
    per-frame renormalisation: differently-sized canvases are re-fitted
    individually by the browser, which reintroduces pose-dependent scaling in
    the BROWSER even if the pipeline were perfect.
    """
    sizes = {}
    from PIL import Image

    for p in _all_frames():
        sizes.setdefault(Image.open(p).size, []).append(p.name)
    assert len(sizes) == 1, (
        "frames do not share one canvas — object-fit:contain will then scale "
        f"each pose differently in the browser: {sizes}"
    )


@_requires_scipy
def test_the_character_is_the_same_size_in_every_frame():
    """A rigid feature (the BODY's width) must be constant across every frame
    that shares the character's stance.

    Squash/stretch poses legitimately change the body's shape, so they are
    measured against their own group: the resting/expression poses and the walk
    cycle all show the block head-on and must agree. scratch1/scratch2 are the
    deliberate stretch/squash extremes and are excluded by name, not by a
    tolerance loose enough to hide a real wobble.
    """
    import numpy as np

    STANCE = [
        "idle", "happy", "sad", "sleepy", "thinking", "alert", "surprised",
        "walk1", "walk2", "walk3", "walk4", "walk5", "walk6", "walk7", "walk8",
    ]
    widths = {}
    for name in STANCE:
        mask, a = _body_mask(FRAME_DIR / f"tofu-{name}.png")
        ys, xs = np.nonzero(mask)
        scale = 30.0 / max(a.shape[1], a.shape[0])
        widths[name] = (xs.max() - xs.min() + 1) * scale

    lo, hi = min(widths.values()), max(widths.values())
    assert hi - lo <= _RIGID_TOL_PX, (
        f"the character's body width varies {hi - lo:.2f} rendered px across "
        f"same-stance poses (tolerance {_RIGID_TOL_PX}px) — the pet visibly "
        f"changes size when it changes pose. Per-frame normalisation is back in "
        f"the pipeline. Measured: "
        f"{ {k: round(v, 2) for k, v in sorted(widths.items())} }"
    )

    walk = {k: v for k, v in widths.items() if k.startswith("walk")}
    wlo, whi = min(walk.values()), max(walk.values())
    assert whi - wlo <= _RIGID_TOL_PX, (
        f"body width swings {whi - wlo:.2f} px WITHIN the walk cycle, which "
        "plays at ~13fps — this reads directly as glitching/shimmering."
    )


@_requires_scipy
def test_the_pipeline_scales_every_frame_by_the_SAME_factor():
    """THE LOAD-BEARING GUARD: shipped size must be the master's size × ONE
    constant, for every frame.

    This is what actually distinguishes a correct pipeline from the broken one,
    and it does so WITHOUT assuming the artist drew every pose identically. The
    old pipeline gave each frame its own scale (0.1837–0.2319, a 26% spread), so
    the character's size became a function of its pose. Here we recompute the
    global scale from the pipeline's own _layout() and require every shipped
    frame to match `master × scale` to within integer rounding.

    Residual variation that survives this check is the DRAWINGS' own variation,
    which a faithful pipeline must preserve rather than iron out.
    """
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    P = _require_masters()

    def body_width(a):
        solid = a[..., 3] > 60
        labels, n = ndimage.label(solid)
        sizes = ndimage.sum(solid, labels, range(1, n + 1))
        _ys, xs = np.nonzero(labels == (int(np.argmax(sizes)) + 1))
        return xs.max() - xs.min() + 1

    metrics, keyed = {}, {}
    for name, (raw, deg) in P.FRAME_SOURCES.items():
        keyed[name] = P._keyed(P.RAW_DIR / f"{raw}.png", deg)
        metrics[name] = P._anchors(keyed[name])
    scale, canvas_w, canvas_h, _half = P._layout(metrics)

    errs = {}
    for name, im in keyed.items():
        expected = body_width(np.asarray(im.convert("RGBA"))) * scale
        actual = body_width(np.asarray(
            Image.open(FRAME_DIR / f"tofu-{name}.png").convert("RGBA")))
        errs[name] = abs(actual - expected) * (30.0 / max(canvas_w, canvas_h))

    worst_name = max(errs, key=errs.get)
    assert errs[worst_name] <= _SCALE_FIDELITY_PX, (
        f"tofu-{worst_name} deviates {errs[worst_name]:.3f} rendered px from "
        f"EXACT global scaling (tolerance {_SCALE_FIDELITY_PX}px). Frames are "
        "no longer scaled by one shared factor — the character's size has become "
        f"a function of its pose again. All: "
        f"{ {k: round(v, 3) for k, v in sorted(errs.items()) if v > 0.05} }"
    )


@_requires_scipy
def test_every_frame_registers_on_the_body_centre_and_foot_line():
    """The two anchors must hold on every shipped frame.

    Body centre on the canvas midline: the CSS facing flip is scaleX on the
    whole frame, so it pivots about the canvas midline — if the body is not
    centred there, TURNING translates the character sideways. Foot line on the
    bottom row: object-position bottom then plants every pose on one ground
    line instead of letting a short pose hover.
    """
    import numpy as np

    off_px, foot_gaps = {}, {}
    for p in _all_frames():
        mask, a = _body_mask(p)
        ys, xs = np.nonzero(mask)
        h, w = a.shape[0], a.shape[1]
        scale = 30.0 / max(w, h)
        body_cx = (xs.min() + xs.max()) / 2.0
        off_px[p.stem] = (body_cx - w / 2.0) * scale
        iy = np.nonzero(a[..., 3] > 60)[0]
        foot_gaps[p.stem] = h - 1 - int(iy.max())

    worst = max(abs(v) for v in off_px.values())
    assert worst <= _RIGID_TOL_PX, (
        f"the body sits up to {worst:.2f} rendered px off the sprite midline "
        f"(tolerance {_RIGID_TOL_PX}px). Frames are being centred on the INK "
        "bbox, so asymmetric FX shove the body sideways and the pet appears to "
        f"jump when its mood changes. Measured: "
        f"{ {k: round(v, 2) for k, v in sorted(off_px.items()) if abs(v) > 0.1} }"
    )
    bad = {k: v for k, v in foot_gaps.items() if v != 0}
    assert not bad, (
        f"these frames' lowest ink is not on the canvas bottom row: {bad}. The "
        "foot line is the anchor that keeps every pose planted on one ground "
        "line; a gap makes that pose hover."
    )


def test_the_pipeline_uses_one_global_scale_not_per_frame_normalisation():
    """STRUCTURAL: the pipeline must derive ONE scale for all frames.

    The pixel guards above would also catch a regression, but only after
    someone regenerates the art. This asserts the mechanism, so the intent
    survives a reading of the code alone.
    """
    sys.path.insert(0, str(REPO / "tests"))
    from _source_scan import strip_comments

    src = strip_comments(PIPELINE.read_text(encoding="utf-8"), lang="py")
    assert re.search(r"def _layout\(", src), (
        "_layout() is gone — the global scale/canvas derivation was the fix."
    )
    assert re.search(r"def _anchors\(", src), (
        "_anchors() is gone — body-centre/foot-line registration was the fix."
    )
    m = re.search(r"def _keyed\(.*?\n(?=def )", src, re.S)
    assert m, "_keyed() not found"
    assert "resize" not in m.group(0), (
        "_keyed() scales again. Per-frame scaling is the defect: it makes the "
        "character's size a function of its pose. ALL sizing must happen once, "
        "globally, in _layout()."
    )
    layout = re.search(r"def _layout\(.*?\n(?=def )", src, re.S).group(0)
    assert "max(" in layout and "MAX_SIDE" in layout, (
        "_layout no longer derives one scale from the extremes across frames."
    )


def test_the_turn_pivot_actually_wins_the_cascade():
    """The pivot animation must BEAT the walk animation, or it never plays.

    It previously sat ABOVE `[data-state="walk"] .tofu-pet-img` at EQUAL
    specificity (0,2,0 each), so the later walk rule won — and because a facing
    flip only ever happens while walking/turning/chasing, the plant-and-spin hop
    was unreachable in EVERY state. `animation` is a shorthand, so the winner
    replaces the whole list: there is no merging, one rule simply wins.
    """
    css = _css_code()

    def specificity(sel):
        ids = len(re.findall(r"#[\w-]+", sel))
        cls = len(re.findall(r"\.[\w-]+|\[[^\]]*\]|:(?!not\b|:)[a-z-]+", sel))
        typ = len(re.findall(r"(?:^|[\s>+~])[a-z]+(?![\w-]*[\(\[])", sel))
        return (ids, cls, typ)

    rules = []
    for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        if "animation" not in m.group(2):
            continue
        for sel in (s.strip() for s in m.group(1).split(",")):
            if ".tofu-pet-img" in sel and ".tofu-pet" in sel:
                rules.append((m.start(), sel, m.group(2)))

    pivot = [r for r in rules if "data-turning" in r[1]]
    assert len(pivot) == 1, (
        f"expected exactly ONE pivot rule, found {len(pivot)}. Two overlapping "
        "pivot rules at different specificities is how this broke before."
    )
    walk = [r for r in rules if 'data-state="walk"' in r[1]]
    assert walk, "the walk animation rule vanished"

    p_idx, p_sel, p_decl = pivot[0]
    assert "tofuPetPivot" in p_decl, "the pivot rule no longer plays tofuPetPivot"
    for w_idx, w_sel, _ in walk:
        p_spec, w_spec = specificity(p_sel), specificity(w_sel)
        wins = p_spec > w_spec or (p_spec == w_spec and p_idx > w_idx)
        assert wins, (
            f"the pivot rule {p_sel!r} spec{p_spec} @{p_idx} does NOT beat "
            f"{w_sel!r} spec{w_spec} @{w_idx}. A facing flip only happens while "
            "walking/turning, so losing here means the pivot never plays at all."
        )
    assert "!important" not in p_decl, (
        "the pivot reaches for !important. It must win on specificity + order so "
        "a future rule can still override it cleanly."
    )
    assert "data-state" not in p_sel, (
        "the pivot's specificity bump depends on a data-state ATTRIBUTE, but "
        "mount() sets no initial data-state and _startle() can pivot before the "
        "first _enter() — the bump must be structural (e.g. :not(:root))."
    )


# ── NEUTER proofs for section 4: each must bite its OWN test, and only its own ──

@_requires_scipy
def test_NEUTER_per_frame_normalisation_is_caught(tmp_path):
    """Reintroduce the ORIGINAL defect — trim each frame to its own bbox and
    scale it so its own longest side hits MAX_SIDE — and the size-constancy
    measurement must fail. Rendered in memory/tmp; the shipped art is untouched.
    """
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    P = _require_masters()

    def old_style(name):
        raw, deg = P.FRAME_SOURCES[name]
        im = P._keyed(P.RAW_DIR / f"{raw}.png", deg)     # keyed + ink-cropped
        s = P.MAX_SIDE / max(im.size)                     # ← per-frame scale (the bug)
        return im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))),
                         Image.LANCZOS)

    widths = {}
    for name in ("idle", "walk1", "walk2", "thinking", "alert"):
        im = old_style(name)
        im.save(tmp_path / f"tofu-{name}.png")
        a = np.asarray(im.convert("RGBA"))
        solid = a[..., 3] > 60
        labels, n = ndimage.label(solid)
        sizes = ndimage.sum(solid, labels, range(1, n + 1))
        ys, xs = np.nonzero(labels == (int(np.argmax(sizes)) + 1))
        scale = 30.0 / max(a.shape[1], a.shape[0])
        widths[name] = (xs.max() - xs.min() + 1) * scale

    spread = max(widths.values()) - min(widths.values())
    assert spread > _RIGID_TOL_PX, (
        f"per-frame normalisation produced only {spread:.2f}px of body-width "
        f"spread, within the {_RIGID_TOL_PX}px tolerance — the guard would NOT "
        "have caught the original defect and is a rubber stamp."
    )


@_requires_scipy
def test_NEUTER_ink_centred_frames_are_caught(tmp_path):
    """Anchor on the INK centre instead of the BODY centre (the original
    registration bug) and the lateral-teleport guard must fail. The frames with
    asymmetric FX are the ones that move."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    P = _require_masters()

    keyed, metrics = {}, {}
    for name in ("idle", "thinking", "alert", "celebrating"):
        raw, deg = P.FRAME_SOURCES[name]
        im = P._keyed(P.RAW_DIR / f"{raw}.png", deg)
        keyed[name] = im
        metrics[name] = P._anchors(im)
    scale, cw, ch, _half = P._layout(metrics)

    offs = {}
    for name, im in keyed.items():
        body_cx, foot_y, ix0, ix1, _iy0 = metrics[name]
        ink_cx = (ix0 + ix1) / 2.0                        # ← the bug: ink, not body
        scaled = im.resize((max(1, round(im.width * scale)),
                            max(1, round(im.height * scale))), Image.LANCZOS)
        canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        canvas.alpha_composite(scaled, (round(cw / 2.0 - ink_cx * scale),
                                        round(ch - foot_y * scale)))
        a = np.asarray(canvas)
        solid = a[..., 3] > 60
        labels, n = ndimage.label(solid)
        sizes = ndimage.sum(solid, labels, range(1, n + 1))
        ys, xs = np.nonzero(labels == (int(np.argmax(sizes)) + 1))
        offs[name] = ((xs.min() + xs.max()) / 2.0 - cw / 2.0) * (30.0 / max(cw, ch))

    worst = max(abs(v) for v in offs.values())
    assert worst > _RIGID_TOL_PX, (
        f"ink-centring produced only {worst:.2f}px of body offset, inside the "
        f"{_RIGID_TOL_PX}px tolerance — the registration guard is blind to the "
        "very defect it exists for."
    )


def test_NEUTER_mismatched_stride_constant_is_caught():
    """Put the gait's cadence back on a literal that disagrees with travel and
    the slip guard must fire — proven by driving its own arithmetic.

    The neuter must reproduce the ACTUAL shipped defect, which was a PAIR:
    a hard-coded 75ms interval AND the old 34px/s walk speed. (Part of this fix
    was re-pinning the speed to 41px/s precisely so that the DERIVED interval
    lands at ~75ms — so 75ms is now the correct answer at the current speed, and
    neutering the interval alone proves nothing. That near-miss is the point of
    this test: a neuter has to model the defect, not merely perturb a number.)
    """
    js = _js_code()
    distinct = int(re.search(r"var WALK_DISTINCT = (\d+)", js).group(1))
    measured = float(re.search(r"var STRIDE_PX = ([\d.]+)", js).group(1))

    per, old_speed = 75.0, 34.0        # the shipped pair, before this fix
    travel = old_speed * (per * distinct) / 1000.0
    slip = measured / travel
    assert slip > 1.15, (
        f"the original 75ms-at-{old_speed}px/s pair yields slip {slip:.2f}×, "
        "which the guard's 1.15 threshold would ACCEPT — the threshold is too "
        "loose to catch the foot-slip defect that shipped."
    )

    # And the CURRENT derived cadence must sit comfortably inside the bound.
    cur_speed = float(re.search(r"speed: ([\d.]+),", js).group(1))
    min_ms = float(re.search(r"var MIN_FRAME_MS = ([\d.]+)", js).group(1))
    cur_per = max(min_ms, measured / cur_speed * 1000.0 / distinct)
    cur_slip = measured / (cur_speed * (cur_per * distinct) / 1000.0)
    assert cur_slip <= 1.15, f"the derived cadence itself slips {cur_slip:.2f}×"


def test_NEUTER_pivot_losing_the_cascade_is_caught():
    """Move the pivot rule back ABOVE the walk rule at equal specificity (the
    original bug) and the cascade guard must fail. In memory only."""
    css = _css_code()
    m = re.search(r"(\.tofu-pet:not\(:root\)\[data-turning\] \.tofu-pet-img\{[^}]*\})", css)
    assert m, "could not locate the pivot rule to neuter"
    pivot_rule = m.group(1)
    weakened = pivot_rule.replace(":not(:root)", "")     # back to (0,2,0)
    poisoned = css.replace(pivot_rule, "")
    walk = re.search(r"(\.tofu-pet\[data-state=\"walk\"\] \.tofu-pet-img,?)", poisoned)
    assert walk, "walk rule not found in the poisoned copy"
    poisoned = poisoned.replace(walk.group(1), weakened + "\n" + walk.group(1), 1)

    def specificity(sel):
        cls = len(re.findall(r"\.[\w-]+|\[[^\]]*\]|:(?!not\b|:)[a-z-]+", sel))
        typ = len(re.findall(r"(?:^|[\s>+~])[a-z]+(?![\w-]*[\(\[])", sel))
        return (0, cls, typ)

    rules = []
    for mm in re.finditer(r"([^{}]+)\{([^}]*)\}", poisoned):
        if "animation" not in mm.group(2):
            continue
        for sel in (s.strip() for s in mm.group(1).split(",")):
            if ".tofu-pet-img" in sel and ".tofu-pet" in sel:
                rules.append((mm.start(), sel, mm.group(2)))
    pv = [r for r in rules if "data-turning" in r[1]][0]
    wk = [r for r in rules if 'data-state="walk"' in r[1]][0]
    wins = specificity(pv[1]) > specificity(wk[1]) or (
        specificity(pv[1]) == specificity(wk[1]) and pv[0] > wk[0])
    assert not wins, (
        "the weakened+reordered pivot still wins the cascade, so the guard "
        "cannot detect the dead-pivot defect it exists for"
    )


# ══════════════════════════════════════════════════════════════════════
#  5. FRAMES DECODE ONCE; THE SCENE LIGHT KEEPS ITS ART-INDEPENDENT CHANNELS
# ══════════════════════════════════════════════════════════════════════

def test_frames_decode_once_and_toggle_thereafter():
    """Frame changes must toggle visibility on kept <img> nodes, never re-decode
    or rebuild markup 13×/s — the churn shows up as stutter on a busy page."""
    js = _js_code()
    m = re.search(r"function _paintFrame\(name\) \{(.*?)\n  \}", js, re.S)
    assert m, "_paintFrame not found"
    assert "innerHTML" not in m.group(1), (
        "_paintFrame writes innerHTML — every walk frame would reparse markup."
    )
    assert "display" in m.group(1), "_paintFrame no longer toggles visibility"
    m2 = re.search(r"function _loadFrame\(name\) \{(.*?)\n  function ", js, re.S)
    assert m2, "_loadFrame not found"
    body = m2.group(1)
    assert "_frameNode" in body and "_frameFetching" in body, (
        "the create-once guards are gone — frames would be re-created per ask."
    )
    assert "new Image()" in body, "frames are no longer <img> elements"


def test_apply_light_keeps_only_art_independent_channels():
    """The scene sun still drives what it can drive without reaching into the
    art: the cast shadow (offset/length) and the filter form light (shade/warm).
    The SVG gradient recolour is gone and must not be half-reanimated."""
    js = _js_code()
    m = re.search(r"function _applyLight\(\) \{(.*?)\n  \}", js, re.S)
    assert m, "_applyLight not found"
    body = m.group(1)
    for var in ("--pet-shadow-dx", "--pet-shadow-scale", "--pet-shade-dx", "--pet-light-warm"):
        assert var in body, f"_applyLight no longer writes {var}"
    assert "TofuScene.lightInfo" in js, "_applyLight no longer reads the scene's live sun"


def test_shipped_frames_match_the_pipeline():
    """NO SECOND COPY: the on-disk frames must match the processing pipeline
    exactly — a hand-edited frame desynchronises the character between poses.

    Master-dependent: `--check` re-renders every frame from the raw poses, so it
    cannot run where export.py stripped them (see _require_masters). This one
    failed in exported trees even BEFORE the size-constancy batch — it is the
    original instance of the same subject-availability problem.
    """
    _require_masters()
    r = subprocess.run(
        [sys.executable, str(PIPELINE), "--check"],
        capture_output=True, text=True, cwd=str(REPO), timeout=120,
    )
    assert r.returncode == 0, (
        "on-disk frames drifted from process_ai_frames.py "
        f"(re-run it):\n{r.stdout}\n{r.stderr}"
    )


# ── NEUTER proofs, on COPIES / in-memory art, never left on disk ──

def test_NEUTER_partial_mirror_is_caught():
    """Move the flip OFF the img elements (in memory) → the whole-frame guard fires."""
    css = _css_code()
    poisoned = css.replace("scaleX(var(--pet-face-flip, 1))", "/* no flip */", 1)
    assert poisoned != css, "neuter did not match the img flip declaration"
    m = re.search(r"\.tofu-pet \.tofu-pet-img > img\{([^}]*)\}", poisoned)
    assert m and "scaleX(var(--pet-face-flip" not in m.group(1), (
        "neuter failed to remove the flip — the guard would not bite"
    )


def test_NEUTER_shuffle_gait_is_caught(tmp_path):
    """A cycle whose contact beats never swap lead must fail the alternation
    check — proven by feeding the SHIPPED measurement a shuffled pair."""
    import shutil

    for name in ("walk1", "walk3"):
        shutil.copy(FRAME_DIR / "tofu-walk1.png", tmp_path / f"tofu-{name}.png")
    # Drive the same measurement the guard uses, but against tmp_path:
    import numpy as np
    from PIL import Image

    def mid(path):
        a = np.asarray(Image.open(path).convert("RGBA"))
        alpha = a[..., 3]
        h = a.shape[0]
        maxw = max((alpha[y] > 40).sum() for y in range(h))
        rows = []
        for y in range(h - 1, -1, -1):
            rw = (alpha[y] > 40).sum()
            if rw == 0:
                continue
            if rw < 0.55 * maxw:
                rows.append(y)
            else:
                break
        zone = alpha[min(rows):max(rows) + 1, :]
        xs = list(np.where((zone > 40).sum(axis=0) > 0)[0])
        return (xs[0] + xs[-1]) / 2

    assert abs(mid(tmp_path / "tofu-walk3.png") - mid(tmp_path / "tofu-walk1.png")) <= 3, (
        "a shuffled (never-alternating) pair must NOT pass the alternation "
        "threshold — the guard is blind"
    )
