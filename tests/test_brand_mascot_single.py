"""Frontend test — ONE brand mascot, no switching (core/brand_logo.js).

This file replaces tests/test_frontend_brand_logo_skin.py, whose subject (a
runtime skin registry + Settings picker) was removed on owner instruction,
2026-07-29: "I only want the original version … the others are too ugly and
there is really no need to switch."

Deleting the old suite outright would have thrown away the one invariant that
is still load-bearing — CACHE-BUST — so the guards were rewritten around what
survives, plus a ratchet holding the removal in place:

1. ONE MASCOT (ratchet). The module must resolve exactly one URL, the shipped
   ``tofu-welcome.svg``, and must NOT re-export a skin registry. Three separate
   mascot redesigns were approved from a contact sheet and then reversed once
   live; the answer the owner settled on is "ship one and stop asking", so a
   reintroduced picker should turn a test red, not reach the UI.

2. CACHE-BUST. Icons ship with ``max-age=86400``, so a bare path made a logo
   change invisible for 24h — that is exactly how a rollback once looked like
   it "didn't happen". Every resolved URL must carry ``?v=``.

3. VERSION-TOKEN PARITY (new — a real gap the removal exposed). index.html
   ships THREE static mascot tags with the token written out by hand, while
   brand_logo.js holds ``LOGO_VER``. Nothing used to check they agree, so
   bumping one and not the others would leave a partly-stale logo: the surfaces
   that read the constant update, the hand-written ones keep serving yesterday's
   art from cache. This is the same "two copies of one fact drift apart" family
   the project has been collapsing all week, and it costs nothing to pin.

4. THE ASSET REACHES THE USER. Existence on disk plus survival through all
   three export tiers (charter #13/#14 — what must arrive alive needs a guard,
   not an assumption).

Neuters prove each assertion is load-bearing.
"""
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from lib.mcp.registry import is_opensource_build

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
BRAND_LOGO = ROOT / "static" / "js" / "core" / "brand_logo.js"
INDEX = ROOT / "index.html"
PANEL = ROOT / "static" / "settings_panels" / "general.html"
CORE_PANEL = ROOT / "static" / "js" / "settings" / "core_panel.js"
I18N = ROOT / "static" / "js" / "i18n.js"
STYLES = ROOT / "static" / "styles.css"

MASCOT = "tofu-welcome.svg"

#: Symbols the skin mechanism used to export. Re-adding any of them means a
#: wardrobe is coming back; that is the thing this file exists to prevent.
REMOVED_SKIN_SYMBOLS = (
    "listLogoSkins", "setLogoSkin", "getLogoSkin",
    "applyLogoSkin", "defaultLogoUrl", "onBrandLogoError",
)


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not available")
    return exe


def _has_jsdom() -> bool:
    try:
        subprocess.run([_node(), "-e", "require('jsdom')"],
                       cwd=ROOT, capture_output=True, check=True)
        return True
    except Exception:
        return False


HARNESS = textwrap.dedent("""
    const {{ JSDOM }} = require('jsdom');
    const dom = new JSDOM(`<!DOCTYPE html><body></body>`,
                          {{ url: 'http://localhost:15000/' }});
    global.window = dom.window;
    global.document = dom.window.document;
    global.localStorage = dom.window.localStorage;
    global.BASE_PATH = '';

    // ---- BEGIN real shipped module ----
    {source}
    // ---- END real shipped module ----

    const out = {{}};
    out.url = window.logoUrl();
    out.ver = window.LOGO_VER;
    out.attrs64 = window.brandLogoImgAttrs(64);
    out.attrsDefault = window.brandLogoImgAttrs();
    // A skin mechanism would have to surface SOMETHING on window to be usable.
    out.exported = {removed}.filter(n => typeof window[n] !== 'undefined');
    // Wearing a candidate used to be a stored preference; a stale value from a
    // user who wore one before the removal must not change what is served.
    localStorage.setItem('tofu_logo_skin', 'handdrawn');
    out.urlWithStaleStorage = window.logoUrl();

    console.log(JSON.stringify(out));
""")


def _run(source: str) -> dict:
    script = HARNESS.format(source=source, removed=json.dumps(list(REMOVED_SKIN_SYMBOLS)))
    proc = subprocess.run([_node(), "-e", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _source() -> str:
    return BRAND_LOGO.read_text(encoding="utf-8")


def _static_mascot_tokens() -> list:
    """Every hand-written `tofu-welcome.svg?v=<token>` in index.html."""
    return re.findall(rf"{re.escape(MASCOT)}\?v=([A-Za-z0-9._-]+)",
                      INDEX.read_text(encoding="utf-8"))


# ── 1. one mascot, no wardrobe ────────────────────────────────────────────

@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_the_module_resolves_exactly_one_mascot_url():
    out = _run(_source())
    assert MASCOT in out["url"], "the resolved mascot must be the shipped original"
    assert out["urlWithStaleStorage"] == out["url"], (
        "a leftover 'tofu_logo_skin' value from a user who wore a candidate "
        "before the removal must be inert — the original is served regardless"
    )


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_no_skin_registry_is_exported():
    """RATCHET (owner, 2026-07-29): mascot switching is not coming back."""
    out = _run(_source())
    assert out["exported"] == [], (
        "brand_logo.js re-exported skin-mechanism symbols "
        f"{out['exported']} — the owner removed mascot switching ('there is "
        "really no need to switch'). Judge new art by swapping the shipped "
        "asset behind a bumped LOGO_VER, not by shipping a picker."
    )


def test_no_settings_picker_is_shipped():
    """The UI half of the same ratchet — markup, renderer, strings and CSS."""
    offenders = []
    if "logoSkinPicker" in PANEL.read_text(encoding="utf-8"):
        offenders.append("general.html still ships the picker container")
    if "logoSkinPicker" in CORE_PANEL.read_text(encoding="utf-8"):
        offenders.append("core_panel.js still renders skin tiles")
    if "logoSkin" in I18N.read_text(encoding="utf-8"):
        offenders.append("i18n.js still defines settings.logoSkin* strings")
    if "#logoSkinPicker" in STYLES.read_text(encoding="utf-8"):
        offenders.append("styles.css still styles the picker")
    assert not offenders, f"the mascot picker is creeping back: {offenders}"


def test_no_candidate_assets_remain_registered():
    """The candidate SVGs were deleted; nothing may reference that directory.

    A registry entry pointing at a deleted file would fall back silently and
    look like a broken switch rather than a missing file.
    """
    src = _source()
    assert "/skins/" not in src, (
        "brand_logo.js references the removed candidate asset directory"
    )


# ── 2 + 3. cache-bust, and the token copies agreeing ──────────────────────

@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_every_resolved_url_is_cache_busted():
    """The reason this module exists: icons ship with max-age=86400."""
    out = _run(_source())
    assert "?v=" in out["url"], "the resolved URL must carry the cache-bust token"
    for key in ("attrs64", "attrsDefault"):
        assert "?v=" in out[key], (
            f"brandLogoImgAttrs() ({key}) must emit a cache-busted src — it is "
            "the helper every render site uses"
        )
    assert 'data-brand-logo="1"' in out["attrs64"], (
        "the marker attribute must ship with the URL so both can't drift apart"
    )


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_static_tags_carry_the_same_version_token_as_the_module():
    """index.html writes the token by hand; brand_logo.js holds the constant.

    Bump one and not the other and the cache-bust half-works: surfaces built
    from the helper get the new art, the static tags keep serving the old one
    for up to 24h. Nothing checked this before.
    """
    tokens = _static_mascot_tokens()
    assert tokens, (
        "no hand-written mascot tag found in index.html — if the static tags "
        "moved, re-anchor this guard rather than deleting it"
    )
    ver = _run(_source())["ver"]
    mismatched = sorted({tk for tk in tokens if tk != ver})
    assert not mismatched, (
        f"index.html serves the mascot at ?v={mismatched} while brand_logo.js "
        f"LOGO_VER is '{ver}'. Bump every copy together, or the logo updates on "
        "some surfaces and stays cached on others."
    )


# ── 4. the asset actually reaches the user ────────────────────────────────

def test_the_shipped_mascot_exists_on_disk():
    p = ROOT / "static" / "icons" / MASCOT
    assert p.is_file() and p.stat().st_size > 0, f"missing mascot asset: {p}"


@pytest.mark.skipif(is_opensource_build(),
                    reason='export.py is not shipped in opensource builds — '
                           'export-tier guards only run in the source tree')
def test_the_mascot_survives_every_export_level():
    """charter #13/#14: what must arrive alive needs an export survival guard."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("export_mod", ROOT / "export.py")
    export_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(export_mod)

    rel = f"static/icons/{MASCOT}"
    stripped = []
    for mode in ("personal", "internal", "opensource"):
        reason = export_mod._should_exclude(rel, Path(rel).name, mode)
        if reason:
            stripped.append(f"[{mode}] {rel}: {reason}")
    assert not stripped, f"the mascot is stripped from an export tier: {stripped}"


# ── Neuters ───────────────────────────────────────────────────────────────

@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NC_bumping_only_the_module_token_is_caught():
    """Neuter: bump LOGO_VER in the module alone → the parity guard must red."""
    src = _source()
    ver = _run(src)["ver"]
    poisoned = src.replace(f"var LOGO_VER = '{ver}';", "var LOGO_VER = 'NEUTERED';")
    assert poisoned != src, "neuter did not apply — re-anchor it"
    tokens = _static_mascot_tokens()
    mismatched = sorted({tk for tk in tokens if tk != _run(poisoned)["ver"]})
    assert mismatched, (
        "NEUTER must leak: with the module bumped and index.html left alone "
        "the parity guard has to notice the split"
    )


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NC_reintroducing_a_skin_export_is_caught():
    """Neuter: re-export one skin symbol → the ratchet must red."""
    src = _source()
    poisoned = src.replace(
        "  window.LOGO_VER = LOGO_VER;",
        "  window.listLogoSkins = function () { return []; };\n"
        "  window.LOGO_VER = LOGO_VER;",
    )
    assert poisoned != src, "neuter did not apply — re-anchor it"
    assert _run(poisoned)["exported"] == ["listLogoSkins"], (
        "NEUTER must leak: a re-exported skin registry has to be detected"
    )


@pytest.mark.skipif(not _has_jsdom(), reason="jsdom not installed")
def test_NC_dropping_the_cache_bust_is_caught():
    """Neuter: serve a bare path → the cache-bust guard must red."""
    src = _source()
    poisoned = src.replace("return _base() + LOGO_PATH + '?v=' + LOGO_VER;",
                           "return _base() + LOGO_PATH;")
    assert poisoned != src, "neuter did not apply — re-anchor it"
    assert "?v=" not in _run(poisoned)["url"], (
        "NEUTER must leak: without the token a logo change is invisible for 24h"
    )
