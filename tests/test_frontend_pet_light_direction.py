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

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
PIPELINE = REPO / "static" / "icons" / "_gen" / "tofu-pet" / "process_ai_frames.py"
FRAME_DIR = REPO / "static" / "icons" / "pet" / "tofu"
PET_JS = REPO / "static" / "js" / "tofu-pet.js"
CSS = REPO / "static" / "styles.css"

WALK_FRAMES = ["walk1", "walk2", "walk3", "walk4", "walk5", "walk6", "walk7", "walk8"]


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
    """8 slots at 75ms = 13.3fps over the same 600ms stride (the four authored
    poses play twice). Below ~12fps a cycle reads as flicker between drawings;
    the stride DURATION stays ~600ms so travel speed and the CSS bob stay in
    sync."""
    js = _js_code()
    m = re.search(r"var WALK_FRAME_MS = (\d+)", js)
    assert m, "WALK_FRAME_MS not found"
    per = int(m.group(1))
    fps = 1000.0 / per
    assert fps >= 12.0, (
        f"walk cycle runs at {fps:.1f}fps ({per}ms/frame), below the ~12fps floor."
    )
    stride = per * len(WALK_FRAMES)
    assert 520 <= stride <= 700, (
        f"stride is {stride}ms; it must stay ~600ms to remain in sync with the "
        "CSS travel bob (tofuPetWalk .3s = two bounces per cycle)."
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
#  3. FRAMES DECODE ONCE; THE SCENE LIGHT KEEPS ITS ART-INDEPENDENT CHANNELS
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
    exactly — a hand-edited frame desynchronises the character between poses."""
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
