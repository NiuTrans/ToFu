"""VISUAL-INTEGRATION ACCEPTANCE (pixel delta, not coordinate delta).

The owner's standing objection to every prior pass: the disturbance-field tests
prove the flow dabs MOVE in their coordinates, but "measurability != visibility"
— a handful of faint same-green dabs shifting a few px against the fully-baked,
non-reactive buffer that dominates the 48px bar can still be imperceptible.

This test closes that gap with the acceptance evidence the owner actually asked
for: it renders the ACTUAL composited canvas to real pixels for two states —
pet parked at a fixed ground x vs. no pet — and pixel-diffs the FOOT REGION,
proving the pet's disturbance produces an ABOVE-NOISE delta in the composited
image, not merely in the dab-coordinate array.

How it's faithful (not a re-implementation):
  * The ENTIRE geometry/colour/alpha/composite stream comes from the REAL
    shipped static/js/tofu-scene.js, recorded under node by tests/
    _scene_pixeldiff.js (base gradient wash + every resolved brush-dab of the
    baked buffer AND the per-frame overlay: glow, sparks, flow-deform, wake).
  * Only the RASTERIZER is ours — and it's `cairocffi` (libcairo), the SAME
    engine node-canvas uses, doing true source-over / 'lighter' alpha
    compositing + real linear/radial gradients. So the pixels are what a canvas
    would composite, just without a browser.

A headless browser is NOT used here — but not because one is unavailable: that
claim was measured and found FALSE on 2026-07-29. All 10 GUI libs Chromium
needs are present in the env prefix (``describe_chromium_env()['issues']`` is
empty) and a real Playwright screenshot renders glyphs; the libs were never
missing, ``LD_LIBRARY_PATH`` simply has to be exported first, which
``chromium_env.ensure_chromium_env()`` now does for every entry point.

This file stays on cairo anyway, for a reason that has nothing to do with
availability: replaying the recorded draw stream is DETERMINISTIC and isolates
the compositing maths, so a pixel delta here can only come from the scene
module. Tests that need a real browser use the ``browser`` fixture
(tests/conftest.py) instead.

Env gate: requires `node` on PATH + `cairocffi` importable; skipped otherwise
(never a hard CI failure on a bare box).
"""
import json
import math
import subprocess
from pathlib import Path

import pytest

from tests._jsdom import frontend_module_guard

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tests" / "_scene_pixeldiff.js"

cairocffi = pytest.importorskip("cairocffi", reason="cairo rasterizer not available")
# P0-1: node absent → clean skip normally, collection-red under
# TOFU_REQUIRE_FRONTEND=1 (docs/TESTING_STRATEGY.md §4).
frontend_module_guard()

W, H = 360, 48
FOOT_X = 180.0            # pet foot-centre px (bar centre)
MS = 1800.0              # enough frames for the disturbance dome to build


SRC_PATH = REPO / "static" / "js" / "tofu-scene.js"


def _freeze_field(src: str) -> str:
    """Transform the module source so the disturbance field never presses and
    never lingers — i.e. the pet still exists (glow/wake unchanged) but the
    grass-parting DISTURBANCE mechanism is inert. Used to ISOLATE the
    disturbance's own pixel contribution (normal vs frozen, pet in both → every
    other layer cancels in the diff)."""
    a = src.replace("    var amp = g.drag ? 0.8 : 1;", "    var amp = 0;  /* no press */", 1)
    a = a.replace("var DISTURB_DECAY = 0.9;", "var DISTURB_DECAY = 0.0;", 1)
    assert a != src, "freeze transform matched nothing"
    return a


def _record(foot, freeze=False, decor="meadow"):
    """Record the real draw stream for a given scene. When freeze=True,
    temporarily patch the shipped source to inert-disturbance, record, and
    ALWAYS restore it (guarded + post-restore asserted, so a shipped file is
    never left neutered)."""
    original = None
    if freeze:
        original = SRC_PATH.read_text()
        SRC_PATH.write_text(_freeze_field(original))
    try:
        arg = "none" if foot is None else str(foot)
        out = subprocess.run(
            ["node", str(HARNESS), str(W), str(H), arg, str(MS), decor],
            capture_output=True, text=True, cwd=str(REPO), timeout=30)
    finally:
        if original is not None:
            SRC_PATH.write_text(original)
            assert SRC_PATH.read_text() == original, "failed to restore tofu-scene.js!"
    assert out.returncode == 0, f"harness failed: {out.stderr}\n{out.stdout[:500]}"
    return json.loads(out.stdout.strip().splitlines()[-1])


def _hex(c):
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return (int(c[0:2], 16) / 255.0, int(c[2:4], 16) / 255.0, int(c[4:6], 16) / 255.0)


def _apply_grad(ctx, g):
    if g["kind"] == "linear":
        pat = cairocffi.LinearGradient(g["x0"], g["y0"], g["x1"], g["y1"])
    else:
        pat = cairocffi.RadialGradient(g["x0"], g["y0"], g["r0"], g["x1"], g["y1"], g["r1"])
    for off, col in g["stops"]:
        # canvas gradient stops may be rgba(...) strings (glow) or hex.
        if col.startswith("rgba") or col.startswith("rgb"):
            nums = col[col.index("(") + 1:col.index(")")].split(",")
            r, gg_, b = (float(nums[0]) / 255, float(nums[1]) / 255, float(nums[2]) / 255)
            a = float(nums[3]) if len(nums) > 3 else 1.0
            pat.add_color_stop_rgba(off, r, gg_, b, a)
        else:
            r, gg_, b = _hex(col)
            pat.add_color_stop_rgba(off, r, gg_, b, 1.0)
    return pat


def _replay(ops_list):
    surf = cairocffi.ImageSurface(cairocffi.FORMAT_ARGB32, W, H)
    ctx = cairocffi.Context(surf)
    # opaque white ground so alpha compositing has a defined backdrop
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()
    for ops in ops_list:
        for o in ops:
            comp = o.get("comp", "source-over")
            ctx.set_operator(cairocffi.OPERATOR_ADD if comp == "lighter"
                             else cairocffi.OPERATOR_OVER)
            if o["t"] == "rect":
                if o.get("grad"):
                    ctx.set_source(_apply_grad(ctx, o["grad"]))
                    ctx.rectangle(o["x"], o["y"], o["w"], o["h"])
                    ctx.fill()
                else:
                    r, g, b = _hex(o["color"])
                    ctx.set_source_rgba(r, g, b, o.get("alpha", 1.0))
                    ctx.rectangle(o["x"], o["y"], o["w"], o["h"])
                    ctx.fill()
            elif o["t"] == "dab":
                r, g, b = _hex(o["color"])
                ctx.save()
                ctx.translate(o["x"], o["y"])
                ctx.rotate(o["ang"])
                # ellipse via unit-circle scale (rx,ry) — cairo has no ellipse
                sx = max(o["rx"], 1e-3)
                sy = max(o["ry"], 1e-3)
                ctx.scale(sx, sy)
                ctx.arc(0, 0, 1.0, 0, 2 * math.pi)
                ctx.restore()
                ctx.set_source_rgba(r, g, b, min(1.0, o.get("alpha", 1.0)))
                ctx.fill()
            # 'blit' is the baked-buffer draw; we composite the buffer ops
            # directly instead, so it's a no-op marker here.
    surf.flush()
    return surf


def _pixels(surf):
    data = bytes(surf.get_data())
    stride = surf.get_stride()
    px = []
    for y in range(H):
        row = []
        for x in range(W):
            i = y * stride + x * 4
            b, g, r, a = data[i], data[i + 1], data[i + 2], data[i + 3]
            row.append((r, g, b))
        px.append(row)
    return px


def _region_delta(pa, pb, x0, x1):
    """Mean AND peak per-pixel L1 difference (0..765) over the column band
    [x0,x1). Mean shows the average change; peak shows the strongest single
    pixel (a small dark crease can be visible while barely moving the mean)."""
    tot = 0
    n = 0
    peak = 0
    for y in range(H):
        for x in range(x0, x1):
            ra, ga, ba = pa[y][x]
            rb, gb, bb = pb[y][x]
            d = abs(ra - rb) + abs(ga - gb) + abs(ba - bb)
            tot += d
            peak = max(peak, d)
            n += 1
    return (tot / max(1, n) / 3.0, peak)   # (mean per-channel 0..255, peak L1 0..765)


def _band_luma(px, x0, x1, y0, y1):
    """Mean luminance (0..255, Rec.601) over a rectangular band."""
    tot = 0.0
    n = 0
    for y in range(max(0, y0), min(H, y1)):
        for x in range(max(0, x0), min(W, x1)):
            r, g, b = px[y][x]
            tot += 0.299 * r + 0.587 * g + 0.114 * b
            n += 1
    return tot / max(1, n)


def _fg_only_pixels(rec):
    """Rasterize ONLY the foreground occlusion plane (over a neutral mid-grey so
    its own value is measured, not the bg)."""
    surf = cairocffi.ImageSurface(cairocffi.FORMAT_ARGB32, W, H)
    ctx = cairocffi.Context(surf)
    ctx.set_source_rgb(0.5, 0.5, 0.5)
    ctx.paint()
    surf2 = _replay([rec.get("fg", [])])
    # composite fg over the grey
    ctx.set_source_surface(surf2, 0, 0)
    ctx.paint()
    surf.flush()
    return _pixels(surf)


def _isolated_disturbance_pixels(decor="meadow"):
    """The KEY isolation: render the pet-present scene with the disturbance
    field LIVE vs FROZEN. Every other layer (baked field, sun glow, sparks, the
    pet-attention glow, wake marks) is identical in both — the pet exists in
    both — so the pixel diff is the LIVE-LAYER MECHANISM'S OWN contribution,
    not a pet-vs-no-pet confound (which would also capture the attention glow)."""
    live = _record(FOOT_X, freeze=False, decor=decor)
    frozen = _record(FOOT_X, freeze=True, decor=decor)
    pa = _pixels(_replay([live["buffer"], live["frame"]]))
    pb = _pixels(_replay([frozen["buffer"], frozen["frame"]]))
    return pa, pb


SCENES = ["meadow", "pool", "sky"]


@pytest.mark.visual
@pytest.mark.parametrize("decor", SCENES)
def test_disturbance_produces_visible_pixel_delta_isolated(decor):
    """ACCEPTANCE (the evidence the owner asked for), for ALL THREE scenes — the
    live near-layer's OWN contribution must change the COMPOSITED PIXELS in the
    foot region well above the ambient motion elsewhere, in meadow AND pool AND
    sky. Isolated by diffing live-field vs frozen-field with the pet present in
    BOTH, so the pet-attention glow and every baked layer cancel and only the
    live-layer interaction (grass flatten / water splash / cloud shove) remains.

    Bands: foot = ±22px around FOOT_X; control = a same-width window far from
    the foot. The foot delta must clear an absolute floor AND a comfortable
    multiple of the control — proving a LOCAL, visible reaction under the pet."""
    pa, pb = _isolated_disturbance_pixels(decor=decor)
    fx = int(FOOT_X)
    fmean, fpeak = _region_delta(pa, pb, max(0, fx - 22), min(W, fx + 22))
    cmean, cpeak = _region_delta(pa, pb, 4, 4 + 44)
    print(f"\n[{decor}] ISOLATED live-layer — foot mean={fmean:.3f}/255 peak={fpeak}/765   "
          f"control mean={cmean:.3f} peak={cpeak}   mean-ratio={fmean / max(0.02, cmean):.1f}x")
    # absolute floor: a real, above-noise change (mean ≥1.5/255) with a clearly
    # visible strongest pixel (peak ≥ 40/765 ≈ a ~13-level per-channel shift).
    assert fmean >= 1.5, \
        f"[{decor}] live-layer interaction too faint to see: mean {fmean:.3f}/255 (the 'floats on top' bug)"
    assert fpeak >= 40, \
        f"[{decor}] no visibly-strong pixel under the foot: peak {fpeak}/765"
    # locality: the foot change must dominate the ambient control window
    assert fmean >= 4.0 * max(0.02, cmean), \
        f"[{decor}] foot mean {fmean:.3f} not clearly local vs ambient control {cmean:.3f}"


@pytest.mark.visual
@pytest.mark.parametrize("decor", SCENES)
def test_NEUTER_isolation_collapses_when_field_already_frozen(decor):
    """NEUTER of the TEST'S OWN isolation, per scene — frozen-field vs
    frozen-field (both pet-present) MUST collapse the foot delta to ~0. This
    proves each scene's acceptance signal is produced by the LIVE mechanism, not
    by pet presence, frame timing, or a rasterizer artefact."""
    frozen_a = _record(FOOT_X, freeze=True, decor=decor)
    frozen_b = _record(FOOT_X, freeze=True, decor=decor)
    pa = _pixels(_replay([frozen_a["buffer"], frozen_a["frame"]]))
    pb = _pixels(_replay([frozen_b["buffer"], frozen_b["frame"]]))
    fx = int(FOOT_X)
    fmean, fpeak = _region_delta(pa, pb, max(0, fx - 22), min(W, fx + 22))
    # identical mechanism state + identical frame phase → essentially no delta.
    assert fmean < 0.5 and fpeak < 40, \
        f"[{decor}] frozen-vs-frozen still showed a foot delta (mean={fmean:.3f} peak={fpeak}) — " \
        f"the acceptance test isn't actually measuring the live mechanism"


@pytest.mark.visual
@pytest.mark.parametrize("decor", SCENES)
def test_foreground_plane_creates_a_near_plane_presence(decor):
    """OWNER ASK #1 (depth) reconciled with the follow-up (no ugly border) —
    the near occlusion plane must be a MEASURABLE closer plane in the bottom
    band, but the DIRECTION of the value shift is PER-SCENE:

      * meadow (dark grass understory is natural) → the band goes DARKER;
      * pool / sky (BRIGHT, airy) → a dark mass reads as a dirty BORDER, so the
        near plane is a GENTLE in-family tonal shift (deeper teal / warm sand),
        NOT a darkening. Here we require a real color change but CAP it so it
        can never regress into the heavy dark band the owner rejected.

    Measured on the bottom 14px band, no pet (isolate the plane's own value)."""
    rec = _record(None, decor=decor)
    with_fg = _pixels(_replay([rec["buffer"], rec["frame"], rec["fg"]]))
    without_fg = _pixels(_replay([rec["buffer"], rec["frame"]]))
    y0, y1 = H - 14, H
    luma_with = _band_luma(with_fg, 0, W, y0, y1)
    luma_without = _band_luma(without_fg, 0, W, y0, y1)
    drop = luma_without - luma_with
    # total color change (any channel) over the bottom 20px — the near plane's
    # presence (blade bodies + understory), direction-agnostic. `_region_delta`
    # averages the FULL height, so restrict to the bottom band by hand.
    def _band_change(a, b, y0b, y1b):
        tot = 0
        for y in range(y0b, y1b):
            for x in range(W):
                tot += abs(a[y][x][0] - b[y][x][0]) + abs(a[y][x][1] - b[y][x][1]) + abs(a[y][x][2] - b[y][x][2])
        return tot / ((y1b - y0b) * W) / 3.0
    change = _band_change(with_fg, without_fg, H - 20, H)
    print(f"\n[{decor}] near-band: bg-only luma={luma_without:.1f} with-fg={luma_with:.1f} "
          f"Δluma={drop:.1f} colorΔ={change:.1f}/255")
    if decor == "meadow":
        assert drop >= 12.0, \
            f"[meadow] dark-grass understory too weak (Δluma {drop:.1f}/255) — no depth"
    else:
        # a real near-plane presence …
        assert change >= 3.5, \
            f"[{decor}] no near-plane presence in the bottom band (colorΔ {change:.1f}/255)"
        # … but NOT a heavy dark band (the border regression): airy scenes must
        # stay within a gentle darkening budget.
        assert drop <= 16.0, \
            f"[{decor}] near plane too dark (Δluma {drop:.1f}/255) — reads as an ugly border, not haze"


@pytest.mark.visual
def test_NEUTER_pale_foreground_has_no_near_plane_presence():
    """NEUTER of owner ask #1 — flatten the fg seed (near-zero mix + very
    translucent blades + near-invisible understory) → the near-band presence in
    MEADOW collapses below the depth floor, proving the shade/alpha seed is what
    creates the plane (not merely the presence of more dabs)."""
    original = SRC_PATH.read_text()
    # near-transparent, no-shade blades (meadow path uses fgShade @ fgMix)
    pale = original.replace(
        "color: _mixHex(fgColors[(R() * fgColors.length) | 0], fgShade, fgMix),",
        "color: _mixHex(fgColors[(R() * fgColors.length) | 0], fgShade, 0.03),", 1)
    pale = pale.replace("alpha: fgDark ? lerp(0.92, 1.0, R()) : lerp(0.66, 0.82, R()),",
                        "alpha: lerp(0.06, 0.12, R()),", 1)
    # and near-invisible understory
    pale = pale.replace(
        "var uc = _mixHex(fgColors[(R() * fgColors.length) | 0], uShadeSeed, uMix);",
        "var uc = _mixHex(fgColors[(R() * fgColors.length) | 0], uShadeSeed, 0.02);  /* NEUTER understory */", 1)
    assert "NEUTER understory" in pale, "pale neuter did not match the understory seed line"
    assert pale != original, "pale neuter matched nothing"
    SRC_PATH.write_text(pale)
    try:
        rec = _record(None, decor="meadow")
        with_fg = _pixels(_replay([rec["buffer"], rec["frame"], rec["fg"]]))
        without_fg = _pixels(_replay([rec["buffer"], rec["frame"]]))
        drop = _band_luma(without_fg, 0, W, H - 14, H) - _band_luma(with_fg, 0, W, H - 14, H)
    finally:
        SRC_PATH.write_text(original)
        assert SRC_PATH.read_text() == original, "failed to restore tofu-scene.js!"
    assert drop < 12.0, \
        f"pale fg still darkened meadow by {drop:.1f}/255 — the depth test would not bite"


@pytest.mark.visual
def test_foreground_parting_travels_with_the_pet():
    """OWNER ASK #2 — the front parting must MOVE with the cat, not just 'differ'.
    Render the fg plane with the pet parked LEFT (x=110) vs RIGHT (x=250). The
    near-band pixels must change strongly UNDER-LEFT when the pet is left and
    UNDER-RIGHT when the pet is right — a travelling dent, not a global wobble.
    Diffs the fg-only raster so only the occlusion plane's own motion counts."""
    left = _fg_only_pixels(_record(110.0, decor="meadow"))
    right = _fg_only_pixels(_record(250.0, decor="meadow"))
    y0, y1 = H - 16, H
    # change in the LEFT foot window vs the RIGHT foot window
    def band_l1(a, b, x0, x1):
        tot = 0
        for y in range(y0, y1):
            for x in range(x0, x1):
                ra, ga, ba = a[y][x]; rb, gb, bb = b[y][x]
                tot += abs(ra - rb) + abs(ga - gb) + abs(ba - bb)
        return tot / max(1, (y1 - y0) * (x1 - x0)) / 3.0
    left_win = band_l1(left, right, 110 - 24, 110 + 24)
    right_win = band_l1(left, right, 250 - 24, 250 + 24)
    mid_win = band_l1(left, right, 180 - 24, 180 + 24)
    print(f"\n[meadow] fg parting travel — left-foot Δ={left_win:.2f} right-foot Δ={right_win:.2f} "
          f"mid Δ={mid_win:.2f}")
    # both foot windows must show a real change (the dent parted there in one of
    # the two frames), and clearly more than the untouched middle band.
    assert left_win >= 3.0 and right_win >= 3.0, \
        f"parting did not register at the foot windows (L={left_win:.2f} R={right_win:.2f})"
    assert (left_win + right_win) >= 2.0 * mid_win, \
        f"parting is a global wobble, not a travelling dent (L+R={left_win + right_win:.2f} mid={mid_win:.2f})"


@pytest.mark.visual
@pytest.mark.parametrize("decor", SCENES)
def test_foreground_base_is_not_a_solid_border_strip(decor):
    """OWNER ASK (regression) — the understory must NOT fully cover the very
    bottom rim: the rounded shell clips a full-width opaque strip into an ugly
    hard BORDER line. The DECISIVE metric is bottom-row coverage: render the
    fg-only plane over mid-grey (127) and count how many columns of the BOTTOM
    row still show the grey through a gap (luma>110). Irregular mounds rooted
    BELOW the rim leave the bottom row largely transparent (meadow 38% / pool
    81% / sky 100% see-through); a solid strip covers it (~3% gap). This exact
    metric cleanly separates the two."""
    px = _fg_only_pixels(_record(None, decor=decor))
    row = [0.299 * px[H - 1][x][0] + 0.587 * px[H - 1][x][1] + 0.114 * px[H - 1][x][2]
           for x in range(W)]
    gap = sum(1 for v in row if v > 110)     # columns where grey shows through
    frac = gap / W
    print(f"\n[{decor}] fg bottom-row gap coverage = {gap}/{W} ({100 * frac:.0f}%)")
    # Floor 0.20 cleanly separates the irregular mounds (meadow 38%, pool 81%,
    # sky 100% see-through) from the old opaque strip (~3%), while tolerating the
    # densest scene (meadow) whose many tall blades + mounds fill more of the base.
    assert frac >= 0.20, \
        f"[{decor}] fg base covers the bottom rim (only {100 * frac:.0f}% gap) — reads as a hard border line"


if __name__ == "__main__":
    for _s in SCENES:
        test_disturbance_produces_visible_pixel_delta_isolated(_s)
        test_NEUTER_isolation_collapses_when_field_already_frozen(_s)
        test_foreground_plane_creates_a_near_plane_presence(_s)
        test_foreground_base_is_not_a_solid_border_strip(_s)
    test_NEUTER_pale_foreground_has_no_near_plane_presence()
    test_foreground_parting_travels_with_the_pet()
    print("OK")
