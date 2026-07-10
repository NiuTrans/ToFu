"""Tofu project-bar folder-badge redesign guard (styles.css).

The "floating badge" on the project bar is the folder chip (`.folder-badge`,
e.g. `chatui` / `tofu-experiment`, built in static/js/project.js). It used to
wear the old "SNES 3D item tag" look — a 4-colour hard bevel + a `0 2px 0` block
drop shadow — which read as a pasted game button floating ON the painted diorama
instead of blending with it. The redesign reworks each chip into a frosted-glass
capsule that (a) BLENDS: the scene blurs through it (backdrop-filter blur +
saturate, a single warm hairline, soft feathered elevation, a glass inner top
highlight — the bar frame's "gallery-hung painting" language, NOT a bevel), and
(b) stays READABLE over the busy field: a substantial cream frost + a white ink
text-shadow scrim for per-letter figure-ground separation.

These are env-independent parses of static/styles.css (same harness idiom as the
sibling input-box / pet drift-guards). A biting NEUTER restores the old bevel to
prove each guard is load-bearing.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CSS = REPO / "static" / "styles.css"


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _rule_body(css: str, selector: str) -> str:
    """Return the declaration body of the first rule whose prelude is exactly
    `selector` (comments stripped, whitespace-normalised prelude match)."""
    css = _strip_comments(css)
    i = 0
    want = re.sub(r"\s+", " ", selector).strip()
    while True:
        brace = css.find("{", i)
        if brace == -1:
            return ""
        prelude = re.sub(r"\s+", " ", css[i:brace]).strip()
        end = css.find("}", brace)
        if end == -1:
            return ""
        if prelude == want:
            return css[brace + 1:end]
        i = end + 1


def test_folder_badge_is_frosted_glass_not_bevel():
    """The tofu writable chip must be frosted glass hung on the diorama: a
    backdrop blur so the scene shows through (blend), a capsule radius, and a
    SINGLE warm hairline — NOT the old 4-colour bevel or the hard `0 2px 0`
    block drop."""
    body = _rule_body(CSS.read_text(), '[data-theme="tofu"] .folder-badge')
    assert body, "no [data-theme=tofu] .folder-badge rule found"
    assert "backdrop-filter:blur" in body.replace(" ", ""), \
        "chip lost its backdrop blur — the scene can't show through, it won't blend"
    # capsule shape (pill), distinct from the control buttons' rounded-rect.
    assert "border-radius:999px" in body.replace(" ", ""), \
        "chip should be a capsule (border-radius:999px) to read as a tag, not a button"
    # NO 4-colour bevel: the old look set all four border-*-color separately.
    for bevel in ("border-top-color:", "border-left-color:",
                  "border-right-color:", "border-bottom-color:"):
        assert bevel not in body, \
            f"chip still carries a per-side bevel ({bevel}) — that's the pasted-button tell"
    # NO hard block drop `0 2px 0 <color>` (the SNES tell). A feathered shadow
    # uses negative spread / blur; the block drop has a 0 blur + 0 spread.
    assert not re.search(r"box-shadow:[^;]*\b0 2px 0 ", body), \
        "chip still uses the hard `0 2px 0` block drop instead of a feathered shadow"


def test_folder_badge_has_readability_scrim():
    """Over the busy meadow the label needs per-letter figure-ground separation:
    a substantial cream frost fill + a white ink text-shadow scrim + a glass
    inner top highlight (the light-edge that reads as real glass)."""
    body = _rule_body(CSS.read_text(), '[data-theme="tofu"] .folder-badge').replace(" ", "")
    assert "text-shadow:" in body, \
        "chip lost the white ink scrim — text loses contrast over the painted scene"
    assert "inset0 1px0".replace(" ", "") in body or "inset0 1px 0".replace(" ", "") in body \
        or re.search(r"inset0 ?1px", body), \
        "chip lost the inner top highlight (glass light-edge)"


def test_folder_badge_readonly_is_distinct_glass():
    """The read-only chip must be the SAME glass material but visibly cooler /
    de-saturated so 'locked' still reads at a glance — and its selector must
    out-specify the writable chip (own [data-theme=tofu] .folder-badge.ro rule
    with a blur), not merely inherit."""
    body = _rule_body(
        CSS.read_text(),
        '[data-theme="tofu"] .folder-badge.folder-badge-ro').replace(" ", "")
    assert body, "no tofu read-only folder-badge rule — locked chip falls back to generic slate"
    assert "backdrop-filter:blur" in body, "read-only chip must keep the frosted-glass material"
    # de-saturated (saturate < 1) to cool it vs the warm writable chip.
    m = re.search(r"saturate\(([0-9.]+)\)", body)
    assert m and float(m.group(1)) < 1.0, \
        "read-only chip should be de-saturated (saturate<1) so it reads as cooler/locked"


def test_NC_bevel_is_flagged():
    """NEUTER: restore the old 4-colour bevel + hard block drop on a COPY of the
    CSS and prove the blend guard bites."""
    css = CSS.read_text()
    orig = '[data-theme="tofu"] .folder-badge{'
    assert orig in css, "anchor for the tofu folder-badge rule moved"
    poisoned = css.replace(
        orig,
        '[data-theme="tofu"] .folder-badge{border-top-color:rgba(221,214,196,0.8);'
        'box-shadow:0 2px 0 rgba(168,152,120,0.45);', 1)
    body = _rule_body(poisoned, '[data-theme="tofu"] .folder-badge')
    bit = False
    try:
        assert "border-top-color:" not in body
        assert not re.search(r"box-shadow:[^;]*\b0 2px 0 ", body)
    except AssertionError:
        bit = True
    assert bit, "neuter did not bite — the bevel/block-drop guard is not load-bearing"


if __name__ == "__main__":
    for fn in [test_folder_badge_is_frosted_glass_not_bevel,
               test_folder_badge_has_readability_scrim,
               test_folder_badge_readonly_is_distinct_glass,
               test_NC_bevel_is_flagged]:
        fn()
        print("PASS", fn.__name__)
    print("ALL GREEN")
