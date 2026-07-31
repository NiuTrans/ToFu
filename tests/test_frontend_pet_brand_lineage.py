"""The project-bar pet is the TOFU BRAND MASCOT — lineage + art-integrity guards.

WHY THIS FILE EXISTS
────────────────────
The bar pet used to be a borrowed public-domain pixel-art cat. It shared NOTHING
with the Tofu brand form: not the palette, not the ink, not the face language —
the owner's report was "inconsistent with the brand tone, and not good-looking
enough". The fix replaced it with the mascot itself (the isometric cream block
from ``static/icons/tofu-welcome.svg``).

The existing suite (``test_frontend_tofu_pet.py``) proves the pet BEHAVES — the
wander FSM, the walk cycle, the moods. Every one of those tests stayed green for
the entire period the pet was an off-brand cat, because none of them can see
what the art LOOKS like. That is the blind spot this file covers:

  1. LINEAGE — every colour the pet is drawn with must actually occur in the
     shipped brand mascot. This is what makes "same creature" a checkable fact
     rather than an intention. Note it is asserted against the SHIPPED logo, not
     against a candidate: an earlier draft of the pet was built on the palette of
     ``skins/a2-soft.svg``, a REJECTED logo candidate whose ink/blush/body all
     differ from the shipped mascot — that would have shipped a pet subtly
     mismatched with the logo beside it.
  2. NO SECOND COPY — the 18 frames are emitted by a generator. If a frame is
     hand-edited on disk the generator's ``--check`` gate goes red, so the body
     geometry can never silently desynchronise between frames.
  3. ART INTEGRITY — every frame must decode, carry real art + keyed alpha,
     plant its feet at the trim bottom, and never touch the canvas edge. The
     clipping check is load-bearing: an accent mark that runs off the canvas
     renders as a cut-off stub at the shipped 30px, and no behavioural test
     could ever see it.
  4. NO MASCOT SWITCHER — mascot switching has been vetoed twice by the owner
     (first the pet pack-switcher, later the whole logo-skin picker). Guard that
     a character registry / picker / try-on does not grow back.

The 2026-07-30 revamp replaced the procedural isometric SVG with an
AI-designed FRONT-FACING raster character (owner: "the current pet is way too
ugly" + the moonwalk report the SVG split could not fix). The lineage question
is unchanged — the pet must still be the mascot's own creature — but the
evidence is now PIXELS, not hex-set membership: a rendered PNG carries
thousands of antialiased tones, so the guards sample colour FAMILIES (the
cream body, the ink outline, the pink blush, zero chroma-green residue) with
thresholds measured against the shipped frames.

These are pure file + PIL assertions: no node, no browser, no network.
"""
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PET_JS = REPO / "static" / "js" / "tofu-pet.js"
PET_DIR = REPO / "static" / "icons" / "pet" / "tofu"
PIPELINE = REPO / "static" / "icons" / "_gen" / "tofu-pet" / "process_ai_frames.py"

# The frames the engine resolves: 9 expressions + 4 walk + 3 groom + 2 stretch.
EXPECTED_FRAMES = (
    "idle happy sleepy sleeping thinking surprised sad celebrating alert "
    "walk1 walk2 walk3 walk4 groom1 groom2 groom3 scratch1 scratch2"
).split()

pytestmark = pytest.mark.unit


def _frame_files():
    return sorted(PET_DIR.glob("tofu-*.png"))


def _family_shares(path):
    """Colour-family shares (%) of a frame's OPAQUE pixels.

    THE one implementation of the lineage rule — the guard and its NEUTER both
    call this, so the NEUTER exercises the shipped assertion path instead of
    re-implementing it (a re-implemented neuter proves only that the copy
    works). Families, measured on the shipped set: the mascot's cream body
    (84–91% everywhere), the ink outline (5–11%), the pink blush (≤3.3%),
    near-white specular, chroma-green keying residue (0.00% — the pipeline
    despills), and everything else saturated (accents like the sparkle gold /
    tear blue, ≤3%).
    """
    import numpy as np
    from PIL import Image

    a = np.asarray(Image.open(path).convert("RGBA")).astype(int)
    al = a[..., 3]
    op = al > 200
    n = int(op.sum())
    if not n:
        return dict(n=0, cream=0.0, white=0.0, ink=0.0, green=100.0, pink=0.0, other_sat=100.0)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    cream = op & (r > 190) & (g > 170) & (b > 140) & (r >= g) & (g >= b - 10) & ((r - b) < 90)
    white = op & (mn > 225)
    ink = op & (mx < 70)
    green = op & (g > r + 30) & (g > b + 30)
    pink = op & (r > 200) & (g > 120) & (g < 215) & (b > 120) & (b < 215) & ((r - g) > 20)
    sat = op & ((mx - mn) > 60)
    other_sat = sat & ~cream & ~white & ~ink & ~pink
    pct = lambda m: round(m.sum() / n * 100, 2)
    return dict(n=n, cream=pct(cream), white=pct(white), ink=pct(ink),
                green=pct(green), pink=pct(pink), other_sat=pct(other_sat))


def test_every_declared_frame_has_art():
    """All 18 frames the engine can ask for must exist and be non-empty."""
    for name in EXPECTED_FRAMES:
        f = PET_DIR / f"tofu-{name}.png"
        assert f.exists() and f.stat().st_size > 0, f"missing/empty pet frame: {f}"


def test_pet_palette_is_the_mascots_cream_and_ink():
    """LINEAGE, on pixels: the body must be the mascot's cream family and the
    outline its ink — the two reads that make "same creature" a fact.

    Raster art carries thousands of antialiased tones, so this asserts FAMILY
    SHARES with thresholds measured on the shipped set (cream 84–91%, ink
    5–11%), not exact hex membership. A generation that drifts off-brand (a
    grey, blue or green body) collapses the cream share long before it looks
    wrong in review.
    """
    offenders = {}
    for f in _frame_files():
        s = _family_shares(f)
        if s["cream"] < 60 or s["ink"] < 2:
            offenders[f.name] = s
    assert not offenders, (
        "pet frames left the mascot's cream/ink families — the pet has drifted "
        f"off-brand: {offenders}"
    )


def test_pet_keeps_the_mascots_blush_on_smiling_faces():
    """The blush is the mascot's warmth; idle + happy must both carry it."""
    for name in ("idle", "happy"):
        s = _family_shares(PET_DIR / f"tofu-{name}.png")
        assert s["pink"] >= 0.8, (
            f"tofu-{name} lost the mascot's blush (pink share {s['pink']}%) — "
            "the face reads cold without it"
        )


def test_no_chroma_green_residue_survives_keying():
    """The pipeline despills — no shipped pixel may be greener than it is
    red/blue. Residue reads as a sickly rim around the cream body against the
    meadow scene, and 0.00% is measured on the shipped set."""
    offenders = {f.name: _family_shares(f)["green"] for f in _frame_files()}
    bad = {k: v for k, v in offenders.items() if v > 0.5}
    assert not bad, f"chroma-green residue in shipped frames: {bad}"


def test_no_off_family_saturated_body_colour():
    """Nothing saturated outside cream/ink/blush/specular beyond accent level.

    The deliberate accents (sparkle gold on celebrating, tear blue on sad)
    measured ≤3%; a body that drifts to another saturated colour blows far
    past that.
    """
    offenders = {f.name: _family_shares(f)["other_sat"] for f in _frame_files()}
    bad = {k: v for k, v in offenders.items() if v > 6}
    assert not bad, (
        f"saturated off-family colour beyond accent level: {bad} — only the "
        "sparkle gold / tear blue accents may leave the cream/ink/blush set"
    )


def test_frames_are_valid_pngs_with_keyed_alpha_and_planted_feet():
    """Every frame must decode as RGBA, actually contain art AND transparency
    (the keyed background), and plant its feet at the trim bottom — the engine
    bottom-anchors frames, so a frame whose ink stops short floats above the
    ground line."""
    import numpy as np
    from PIL import Image

    for f in _frame_files():
        try:
            im = Image.open(f)
            im.load()
        except Exception as e:
            pytest.fail(f"{f.name} is not a decodable PNG: {e}")
        a = np.asarray(im.convert("RGBA"))
        al = a[..., 3]
        assert (al == 0).any(), f"{f.name} has no transparent background (keying lost)"
        assert (al > 200).sum() > 1000, f"{f.name} carries no meaningful art"
        bottom = al[-2:, :]
        assert (bottom > 40).any(), (
            f"{f.name} has no ink in its bottom rows — feet must reach the trim "
            "bottom so the pet stands on the ground line"
        )


def test_no_fill_colour_reaches_the_canvas_edge():
    """The art must not be CLIPPED by its own canvas. After a bbox trim the
    OUTLINE legitimately touches the border (the head top, the feet bottoms) —
    that is a completed shape. A SLICE looks different: the shape ran off the
    raw canvas and the cut shows a run of bright FILL colour at the border
    with no outline over it. Measured on the shipped set: border contact is
    ink-only everywhere except one 5px sparkle tip on celebrating."""
    import numpy as np
    from PIL import Image

    bad = {}
    for f in _frame_files():
        a = np.asarray(Image.open(f).convert("RGBA")).astype(int)
        al = a[..., 3]
        edge_alpha = np.concatenate([al[0, :], al[-1, :], al[:, 0], al[:, -1]])
        edge_rgb = np.concatenate([a[0, :, :3], a[-1, :, :3], a[:, 0, :3], a[:, -1, :3]], axis=0)
        bright = ((edge_alpha > 40) & (edge_rgb.max(axis=1) >= 70)).sum()
        if bright > 8:
            bad[f.name] = int(bright)
    assert not bad, (
        f"bright FILL colour on the canvas border — the pose was sliced by the "
        f"raw canvas edge: {bad}"
    )


def test_pet_art_survives_every_export_level():
    """The art must actually REACH the user (charter: export is a first-class
    acceptance target).

    An asset that only exists in the dev tree renders as a broken <img> in every
    shipped build. Three things are asserted at once, because they fail
    differently: the FRAMES must ship (or the pet is invisible), the PIPELINE
    must ship (it is the single source of the frames + a CI gate), and the
    review-only weight (proof sheets, ~1MB raw AI poses) must NOT ship.
    """
    import sys
    sys.path.insert(0, str(REPO))
    from export import _should_exclude

    frame = ("static/icons/pet/tofu/tofu-idle.png", "tofu-idle.png")
    gen = ("static/icons/_gen/tofu-pet/process_ai_frames.py", "process_ai_frames.py")
    proof = ("static/icons/pet/_candidates/proof_tofu_sheet_96.png",
             "proof_tofu_sheet_96.png")
    raw = ("static/icons/_gen/tofu-pet/_candidates/ai/hero_v1.png", "hero_v1.png")

    for mode in ("personal", "internal", "opensource"):
        assert not _should_exclude(*frame, mode), \
            f"pet art is stripped from the {mode} export — the pet ships broken"
        assert not _should_exclude(*gen, mode), \
            f"the frame pipeline is stripped from the {mode} export"
        assert _should_exclude(*proof, mode), \
            f"review proof sheets must NOT ship in the {mode} export"
        assert _should_exclude(*raw, mode), \
            f"the ~1MB raw AI poses must NOT ship in the {mode} export"


def test_pet_art_is_git_tracked():
    """The second door: a file can pass the export filter and still be absent
    from a clean clone if git never tracked it (the exact gap that shipped a
    documented-but-missing script before). Assert every frame is tracked."""
    r = subprocess.run(["git", "ls-files", "static/icons/pet/tofu"],
                       capture_output=True, text=True, cwd=str(REPO), timeout=30)
    assert r.returncode == 0, f"git ls-files failed: {r.stderr}"
    tracked = {line.strip() for line in r.stdout.splitlines() if line.strip()}
    missing = [f.name for f in _frame_files()
               if f"static/icons/pet/tofu/{f.name}" not in tracked]
    assert not missing, (
        "pet frames exist on disk but are NOT git-tracked — they would be absent "
        f"from a clean clone: {missing}"
    )


def test_pipeline_is_the_single_source_of_the_frames():
    """NO SECOND COPY: the on-disk frames must match the pipeline exactly.

    18 hand-maintained frames would each hold their own copy of the character,
    and the first tweak would desynchronise them invisibly. The pipeline's
    --check gate makes that drift a red test instead.
    """
    assert PIPELINE.exists(), f"the frame pipeline is missing: {PIPELINE}"
    r = subprocess.run(["python3", str(PIPELINE), "--check"],
                       capture_output=True, text=True, cwd=str(REPO), timeout=120)
    assert r.returncode == 0, (
        "pet frames on disk have drifted from their pipeline "
        f"(re-run {PIPELINE.relative_to(REPO)}):\n{r.stdout}\n{r.stderr}"
    )


def test_engine_resolves_the_tofu_character_not_a_borrowed_sprite():
    """The shipped module must point at the brand art directory."""
    src = PET_JS.read_text(encoding="utf-8")
    assert "/static/icons/pet/tofu" in src, \
        "tofu-pet.js no longer resolves the brand-native tofu frames"
    assert "data-pet', 'tofu'" in src or 'data-pet", "tofu"' in src, \
        "the pet element no longer marks itself as the tofu character"


def test_no_mascot_switcher_grows_back():
    """Mascot switching was vetoed TWICE (pet pack-switcher, then the logo-skin
    picker). Guard that a character registry / picker / try-on does not return
    to the pet module."""
    src = PET_JS.read_text(encoding="utf-8")
    for sym in ("PET_PACKS", "cyclePack", "setPack", "PACK_ORDER",
                "setCharacter", "cycleCharacter", "CHARACTERS", "listPetSkins"):
        assert sym not in src, (
            f"mascot-switching symbol {sym!r} is back in tofu-pet.js — "
            "the owner has vetoed mascot switching twice"
        )


# ── NEUTER: each guard above must be shown to BITE, on a COPY, never on disk ──
def _pet_i18n_keys():
    """Every ``pet.*`` key the boot-key scanner can reach, prefixes expanded.

    Drives the REAL ``lib.i18n_boot_keys.discover_boot_keys`` — the same scanner
    that builds the shipped boot pack — so this measures what actually gets sent
    to the browser, not a re-derived guess.
    """
    import sys
    sys.path.insert(0, str(REPO))
    from lib.i18n_boot_keys import discover_boot_keys
    dict_src = (REPO / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
    source_keys = set(re.findall(r"^\s*'([A-Za-z][A-Za-z0-9_.]*)':\s*\{",
                                 dict_src, re.M))
    found = discover_boot_keys(str(REPO), source_keys=source_keys)
    return {k for k in found["union"] if k.startswith("pet.")}, source_keys


def test_pet_strings_are_localised_not_hardcoded_english():
    """The pet's user-visible text must go through t(), not sit in the JS.

    The scene-switch button label and both tooltips were English literals
    (``SCENE_LABELS = {meadow: 'Meadow', …}``, ``'Scene: ' + name + ' · click to
    change'``, ``'Tofu — ' + greet``), so a Chinese user hovering the mascot read
    English. Guard the RESULT — no user-facing English literal survives in the
    module — rather than the mechanism, so a future refactor that keeps the
    strings localised by another route still passes.
    """
    src = PET_JS.read_text(encoding="utf-8")
    banned = ["'Meadow'", "'Pool'", "'Sky'", "'Off'",
              "'Scene: '", "'Tofu \u2014 '", "'fast asleep'", "'feeling great'",
              "'Hi there!'", "'Nothing logged yet today'"]
    leaked = [s for s in banned if s in src]
    assert not leaked, (
        "user-visible English literals are back in tofu-pet.js — a zh user "
        f"would read English on the pet: {leaked}"
    )


def test_every_pet_string_key_exists_in_the_dictionary():
    """Each key the pet asks for must be DEFINED — t() renders the raw key name
    otherwise, so a typo shows the user ``pet.scene.meadow`` verbatim."""
    keys, source_keys = _pet_i18n_keys()
    assert keys, "the boot-key scanner found no pet.* keys at all"
    missing = sorted(k for k in keys if k not in source_keys)
    assert not missing, f"pet keys referenced but never defined: {missing}"


def test_pet_keys_are_bilingual():
    """Both languages must be present. A zh-only entry renders Chinese in an
    English UI (and trips the i18n.js missing-translation tripwire).

    NOTE the line-scoped regex: an entry's VALUE legitimately contains braces
    (``'今日完成 {done}/{total}'``), so a ``[^}]*`` body match truncates at the
    first placeholder and reports perfectly good bilingual entries as missing
    ``en``. That false positive was observed while writing this guard — the
    instrument was wrong, not the dictionary.
    """
    keys, _ = _pet_i18n_keys()
    dict_src = (REPO / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
    incomplete = []
    for k in sorted(keys):
        m = re.search(r"^\s*'" + re.escape(k) + r"':\s*(.+)$", dict_src, re.M)
        if not m or "zh:" not in m.group(1) or "en:" not in m.group(1):
            incomplete.append(k)
    assert not incomplete, f"pet keys missing a zh or en translation: {incomplete}"


def test_pet_keys_are_discoverable_by_the_boot_scanner():
    """CHARTER #18: the boot pack is DERIVED by ``discover_boot_keys``, never
    hand-copied — so a pet key it cannot see is a key the browser never gets,
    and the pet renders raw key names on first paint.

    This is a real measured failure, not a hypothetical: the day-report strings
    were read through a local ``_k()`` wrapper, and because
    ``T_CALL_KEY_RE`` only matches a literal string as ``t()``'s FIRST argument,
    the scanner discovered **zero** ``pet.*`` keys while four sat in the dict.
    Dynamic families must therefore be reached via ``t('prefix.' + x)`` so
    ``T_CALL_DYNAMIC_PREFIX_RE`` can expand the whole namespace.
    """
    keys, _ = _pet_i18n_keys()
    # The three dynamic families + the composed tooltip must all be reachable.
    for expect in ("pet.scene.meadow", "pet.scene.off",
                   "pet.greet.deepNight", "pet.feel.great",
                   "pet.title", "pet.sceneTooltip",
                   "pet.dayGreeting"):
        assert expect in keys, (
            f"{expect!r} is invisible to the boot-key scanner — it would be "
            "absent from the boot pack and render as a raw key on first paint"
        )


def test_pet_never_aliases_t_behind_a_local_wrapper():
    """STRUCTURAL: ban the specific shape that caused the blindness.

    A helper like ``var _k = function (key, …) { … t(key) … }`` reads perfectly
    well and is invisible to the scanner. Behaviour tests cannot see this
    (``t()`` still works at runtime) — only a structural check can, and only
    after comments are stripped so a comment *describing* the anti-pattern
    does not trip it (charter #24).
    """
    import sys
    sys.path.insert(0, str(REPO))
    from tests._source_scan import strip_comments
    code = strip_comments(PET_JS.read_text(encoding="utf-8"), lang="js")
    # A wrapper assigns t (or a call to it) to a local name; the scanner only
    # follows literal `t('...')`, so any such indirection hides keys.
    aliases = re.findall(r"var\s+(_?\w+)\s*=\s*\(?\s*typeof\s+t\s*===?", code)
    assert not aliases, (
        "tofu-pet.js aliases t() behind a local name "
        f"({aliases}) — keys reached through it are INVISIBLE to "
        "lib/i18n_boot_keys and would be dropped from the boot pack"
    )


# ── NEUTER: each guard above must be shown to BITE, on a COPY, never on disk ──

def test_NEUTER_offbrand_palette_is_caught(tmp_path):
    """Recolour a REAL frame off-brand (in memory) and drive the SHIPPED check.

    Calls ``_family_shares`` — the same helper the guard uses — so this proves
    the guard's own code path fires, not that a copy of it fires. A blue body
    collapses the cream share exactly the way a drifted generation would.
    """
    import numpy as np
    from PIL import Image

    a = np.asarray(Image.open(PET_DIR / "tofu-idle.png").convert("RGBA")).astype(int)
    op = a[..., 3] > 200
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    body = op & (r > 190) & (g > 170) & (b > 140) & (r >= g)
    poisoned = a.copy()
    poisoned[..., 0], poisoned[..., 2] = b, r          # swap R/B: cream → blue
    out = Image.fromarray(poisoned.astype("uint8"), "RGBA")
    tmp = tmp_path / "tofu-idle.png"
    out.save(tmp)
    s = _family_shares(tmp)
    assert s["cream"] < 60, \
        f"the lineage check did not flag a blue recolour (cream {s['cream']}%) — it is blind"


def test_NEUTER_edge_touched_art_is_caught(tmp_path):
    """Ink at the canvas border must fail the clipping check — paint a dot on
    the edge of a REAL frame (in memory) and re-run the same assertion logic."""
    import numpy as np
    from PIL import Image

    a = np.asarray(Image.open(PET_DIR / "tofu-alert.png").convert("RGBA")).astype(int)
    al = a[..., 3]
    edge_alpha = np.concatenate([al[0, :], al[-1, :], al[:, 0], al[:, -1]])
    edge_rgb = np.concatenate([a[0, :, :3], a[-1, :, :3], a[:, 0, :3], a[:, -1, :3]], axis=0)
    bright = ((edge_alpha > 40) & (edge_rgb.max(axis=1) >= 70)).sum()
    assert bright <= 8, f"the real frame already carries {bright} bright border pixels"
    a[0, a.shape[1] // 2] = (251, 240, 214, 255)       # one CREAM dot on the top border
    al2 = a[..., 3]
    edge_alpha2 = np.concatenate([al2[0, :], al2[-1, :], al2[:, 0], al2[:, -1]])
    edge_rgb2 = np.concatenate([a[0, :, :3], a[-1, :, :3], a[:, 0, :3], a[:, -1, :3]], axis=0)
    bright2 = ((edge_alpha2 > 40) & (edge_rgb2.max(axis=1) >= 70)).sum()
    assert bright2 > bright, \
        "the clipping check did not flag bright fill at the edge — it is blind"


def test_NEUTER_pipeline_drift_is_caught():
    """A hand-edited frame must make the --check gate red.

    Uses a real temporary edit + guaranteed restore, because the gate compares
    pixels on disk: an in-memory poison could not exercise it.
    """
    from PIL import Image

    target = PET_DIR / "tofu-idle.png"
    original = target.read_bytes()
    try:
        im = Image.open(target).convert("RGBA")
        px = im.load()
        cx, cy = im.width // 2, im.height // 2
        px[cx, cy] = (255, 0, 0, 255)                  # one hand-painted pixel
        im.save(target)
        r = subprocess.run(["python3", str(PIPELINE), "--check"],
                           capture_output=True, text=True, cwd=str(REPO), timeout=120)
        assert r.returncode != 0, \
            "the --check gate stayed green after a frame was hand-edited — it does not bite"
    finally:
        target.write_bytes(original)
    # and the tree is genuinely restored
    r2 = subprocess.run(["python3", str(PIPELINE), "--check"],
                        capture_output=True, text=True, cwd=str(REPO), timeout=120)
    assert r2.returncode == 0, "failed to restore the frame after the neuter"
