#!/usr/bin/env python3
"""tests/test_motion_video_assets.py — the scene asset channel + CJK typeface.

Why this channel exists (measured 2026-07-28). Every authored scene could only
ever be "a gradient plus text", and the cause was structural, not a missing
tool. Two rules combined to outlaw non-text assets:

  * ``guide/COMPOSITION_CONTRACT.md`` forbids render-time network fetches —
    correct, that is what makes a render deterministic;
  * ``_scene_author.py`` withheld any filesystem-write tool so a composition
    "can never reference a local asset path".

Net effect: the only legal asset form was a string inlined into the HTML, i.e.
inline SVG. Bitmaps, screenshots and background video were structurally
impossible, not merely unimplemented.

Measured constraints this suite pins (all against the real hyperframes CLI):

  * a reference escaping the project root is REJECTED (``rc=1``: "asset
    path(s) traversing above the project root") — so a global library cannot
    be referenced directly; a symlink / hardlink / subdirectory all pass;
  * ``os.link`` fails with **EPERM** on this host's dolphinfs even for two
    paths on the SAME device, so a same-device check would wrongly conclude
    hardlinks work — the tier fallback is load-bearing TODAY;
  * ``fc-list :lang=zh`` returns exactly one face: ``Noto Serif CJK SC``. Every
    Chinese frame shipped so far was set in a serif nobody chose, because
    naming an absent family silently substitutes rather than erroring.
"""

from __future__ import annotations

import os

import pytest

from lib.motion_video import _assets as A
from lib.motion_video import _fonts as F

pytestmark = pytest.mark.unit

#: A tiny but REAL png (8x8, valid header) — the store refuses empty payloads.
PNG_BYTES = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x08\x00\x00\x00\x08'
    b'\x08\x06\x00\x00\x00\xc4\x0f\xbe\x8b\x00\x00\x00\x15IDATx\xda\x63'
    b'\xfc\xcf\xc0\xc0\xc0\xc8\x00\x03\x10\x06\x18\x60\x00\x00\xc7\x00\x01'
    b'\xf6T\x19\x8f\x00\x00\x00\x00IEND\xaeB`\x82')


@pytest.fixture
def library(tmp_path, monkeypatch):
    """Point the asset library at a temp dir so tests never touch real data."""
    root = tmp_path / 'lib'
    root.mkdir()
    monkeypatch.setattr(A, 'asset_root', lambda: str(root))
    return str(root)


# ══════════════════════════════════════════════════════════
#  content-addressed library
# ══════════════════════════════════════════════════════════

def test_identical_bytes_stored_once(library):
    """De-duplication is the point of content addressing: the same generated
    background across scenes/jobs must not multiply on disk."""
    a = A.store_bytes(PNG_BYTES, suffix='.png')
    b = A.store_bytes(PNG_BYTES, suffix='.png')
    assert a == b
    assert len([n for n in os.listdir(library) if n.endswith('.png')]) == 1


def test_different_bytes_get_different_entries(library):
    a = A.store_bytes(PNG_BYTES, suffix='.png')
    b = A.store_bytes(PNG_BYTES + b'\x00', suffix='.png')
    assert a != b


def test_bare_suffix_is_accepted(library):
    """``store_bytes(suffix='.png')`` passes a BARE suffix, and
    ``os.path.splitext('.png')`` returns an EMPTY extension — an early version
    refused every call for that reason."""
    assert A.store_bytes(PNG_BYTES, suffix='.png').endswith('.png')
    assert A.store_bytes(PNG_BYTES, suffix='png').endswith('.png')


def test_empty_and_unknown_assets_are_refused(library):
    with pytest.raises(A.AssetError):
        A.store_bytes(b'', suffix='.png')
    with pytest.raises(A.AssetError):
        A.store_bytes(PNG_BYTES, suffix='.exe')
    with pytest.raises(A.AssetError):
        A.store_bytes(PNG_BYTES, suffix='.html')


# ══════════════════════════════════════════════════════════
#  three-tier materialisation
# ══════════════════════════════════════════════════════════

def test_materialise_returns_a_scene_relative_path(library, tmp_path):
    src = A.store_bytes(PNG_BYTES, suffix='.png')
    scene = tmp_path / 'scene-001'
    scene.mkdir()
    rel, tier = A.materialise(src, str(scene))
    assert not os.path.isabs(rel)
    assert not rel.startswith('..')
    assert rel.startswith(A.SCENE_ASSET_SUBDIR + os.sep)
    assert (scene / rel).is_file()
    assert tier in ('hardlink', 'symlink', 'copy')


def test_materialise_degrades_when_a_tier_is_unavailable(library, tmp_path,
                                                         monkeypatch):
    """Hardlink refused → the asset still lands.

    Not hypothetical: dolphinfs answers ``os.link`` with EPERM on this host
    even same-device, so if the chain did not fall through, EVERY
    materialisation would fail in production.
    """
    src = A.store_bytes(PNG_BYTES, suffix='.png')
    scene = tmp_path / 'scene-002'
    scene.mkdir()

    def no_link(a, b):
        raise OSError(1, 'Operation not permitted')

    monkeypatch.setattr(os, 'link', no_link)
    rel, tier = A.materialise(src, str(scene))
    assert tier != 'hardlink'
    assert (scene / rel).is_file()


def test_materialise_falls_all_the_way_to_copy(library, tmp_path, monkeypatch):
    """Both link tiers refused (a cross-device, no-symlink filesystem) → copy."""
    src = A.store_bytes(PNG_BYTES, suffix='.png')
    scene = tmp_path / 'scene-003'
    scene.mkdir()
    monkeypatch.setattr(os, 'link',
                        lambda a, b: (_ for _ in ()).throw(OSError(18, 'EXDEV')))
    monkeypatch.setattr(os, 'symlink',
                        lambda a, b: (_ for _ in ()).throw(OSError(1, 'EPERM')))
    rel, tier = A.materialise(src, str(scene))
    assert tier == 'copy'
    target = scene / rel
    assert target.is_file() and not target.is_symlink()
    assert target.read_bytes() == PNG_BYTES


def test_materialise_replaces_a_dangling_link(library, tmp_path):
    """A symlink left by a pruned library must not be adopted — the renderer
    would report a missing file for it."""
    src = A.store_bytes(PNG_BYTES, suffix='.png')
    scene = tmp_path / 'scene-004'
    dest_dir = scene / A.SCENE_ASSET_SUBDIR
    dest_dir.mkdir(parents=True)
    dangling = dest_dir / os.path.basename(src)
    os.symlink(str(tmp_path / 'gone.png'), dangling)
    assert not dangling.is_file()          # dangling by construction
    rel, _tier = A.materialise(src, str(scene))
    assert (scene / rel).is_file()


def test_materialise_refuses_a_missing_library_entry(library, tmp_path):
    with pytest.raises(A.AssetError):
        A.materialise(os.path.join(library, 'nope.png'), str(tmp_path))


# ══════════════════════════════════════════════════════════
#  pre-Chrome reference verification — THREE refusals
# ══════════════════════════════════════════════════════════

@pytest.fixture
def scene_with_asset(library, tmp_path):
    src = A.store_bytes(PNG_BYTES, suffix='.png')
    scene = tmp_path / 'scene-refs'
    scene.mkdir()
    rel, _ = A.materialise(src, str(scene), name='ok.png')
    return str(scene), rel


def test_a_good_scene_local_reference_is_clean(scene_with_asset):
    """The complement that stops the checker from being trivially strict.

    A correctly materialised asset is usually a SYMLINK into the shared
    library, so its ``realpath`` points OUTSIDE the scene by design — an
    early version resolved the path first and therefore flagged every
    legitimate asset as an escape.
    """
    scene, rel = scene_with_asset
    assert A.verify_asset_refs(f'<img src="{rel}">', scene) == []


def test_refusal_1_parent_traversal(scene_with_asset):
    scene, _ = scene_with_asset
    out = A.verify_asset_refs('<img src="../../assets/x.png">', scene)
    assert len(out) == 1 and 'escapes the project root' in out[0]


def test_refusal_2_absolute_path(scene_with_asset):
    scene, _ = scene_with_asset
    out = A.verify_asset_refs('<img src="/etc/hosts">', scene)
    assert len(out) == 1 and 'ABSOLUTE path' in out[0]


def test_refusal_3_referenced_but_missing(scene_with_asset):
    """The most common one once a model starts inventing filenames."""
    scene, _ = scene_with_asset
    out = A.verify_asset_refs('<img src="assets/invented.png">', scene)
    assert len(out) == 1 and 'does not exist' in out[0]


def test_css_url_references_are_checked_too(scene_with_asset):
    scene, _ = scene_with_asset
    html = "<style>.hero{background-image:url('assets/missing.jpg')}</style>"
    out = A.verify_asset_refs(html, scene)
    assert len(out) == 1 and 'does not exist' in out[0]


def test_remote_and_inline_references_are_allowed(scene_with_asset):
    """A CDN script and a data: URI carry no local-file risk — flagging them
    would make the checker unusable."""
    scene, _ = scene_with_asset
    html = ('<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/'
            'gsap.min.js"></script>'
            '<img src="data:image/png;base64,AAAA">'
            '<a href="#anchor">x</a>')
    assert A.verify_asset_refs(html, scene) == []


def test_dangling_symlink_is_reported_as_missing(library, tmp_path):
    """Existence must FOLLOW the link: a broken symlink renders nothing."""
    scene = tmp_path / 'scene-dangle'
    (scene / A.SCENE_ASSET_SUBDIR).mkdir(parents=True)
    os.symlink(str(tmp_path / 'absent.png'),
               scene / A.SCENE_ASSET_SUBDIR / 'ghost.png')
    out = A.verify_asset_refs('<img src="assets/ghost.png">', str(scene))
    assert len(out) == 1 and 'does not exist' in out[0]


# ══════════════════════════════════════════════════════════
#  the CJK typeface — the silent-substitution trap
# ══════════════════════════════════════════════════════════

def test_undeclared_family_is_reported():
    """Naming a face the host lacks is NOT an error — fontconfig substitutes
    silently, so the only way to catch it is to read the document."""
    html = "<style>h1{font-family:'PingFang SC',sans-serif}</style>"
    assert F.undeclared_font_families(html) == ['PingFang SC']


def test_renderer_resolvable_families_are_accepted():
    """guide/MOTION_CRAFT.md lists the families the renderer resolves itself;
    flagging those would be a false alarm."""
    for fam in ('Inter', 'Roboto', 'Noto Sans', 'sans-serif', 'system-ui'):
        html = f'<style>h1{{font-family:{fam},sans-serif}}</style>'
        assert F.undeclared_font_families(html) == [], fam


def test_a_declared_face_is_accepted():
    """Declaring the family from a stored asset is the sanctioned path."""
    html = (f"<style>{F.font_face_css('assets/cjk-sans.woff2')}"
            f"h1{{font-family:'{F.CJK_SANS_FAMILY}',sans-serif}}</style>")
    assert F.undeclared_font_families(html) == []


def test_font_face_css_points_at_a_scene_relative_path():
    css = F.font_face_css('assets/cjk-sans.woff2')
    assert "url('assets/cjk-sans.woff2')" in css
    assert "format('woff2')" in css
    assert F.CJK_SANS_FAMILY in css
    otf = F.font_face_css('assets/cjk-sans.otf')
    assert "format('opentype')" in otf


def test_this_host_really_lacks_a_cjk_sans():
    """The premise of the whole font slice, asserted rather than assumed.

    If a sans CJK face ever gets installed system-wide this test turns red —
    which is the correct signal to revisit whether shipping our own is still
    necessary, not a failure.
    """
    fams = F.installed_cjk_families()
    if not fams:
        pytest.skip('fc-list unavailable on this host')
    assert fams, 'expected at least one CJK face'
    assert not F.has_installed_cjk_sans(), (
        f'a CJK sans appears to be installed now ({fams}) — re-evaluate '
        'whether the bundled face is still needed')


# ══════════════════════════════════════════════════════════
#  wiring: the author can reach the channel, the gate enforces it
# ══════════════════════════════════════════════════════════

def test_author_toolset_exposes_asset_generation():
    from lib.motion_video._scene_author import SCENE_AUTHOR_TOOLS
    names = {t['function']['name'] for t in SCENE_AUTHOR_TOOLS}
    assert 'generate_asset' in names
    # …and still no render / shell path.
    for banned in ('motion_video_render', 'run_command', 'write_file'):
        assert banned not in names


def test_generate_asset_tells_the_model_a_background_must_recede():
    """Measured on the first REAL-model run, not hypothesized.

    Both scenes autonomously called generate_asset — the channel works. But
    scene-001 pushed its background back (``opacity:.6`` PLUS
    ``filter:brightness(.4) saturate(.8)``) while scene-002 used
    ``opacity:.25`` and NO filter, so the asset's hard-edged blocks stayed
    fully legible and cut straight through the bar chart in front of them.
    The tool description said nothing about recession, so which behaviour you
    got was luck. This pins the instruction, since the defect is invisible to
    every existing gate: the frame is well-formed, in-bounds and
    contrast-compliant — just badly layered.
    """
    from lib.motion_video._scene_author import SCENE_AUTHOR_TOOLS

    desc = next(t['function']['description'] for t in SCENE_AUTHOR_TOOLS
                if t['function']['name'] == 'generate_asset').lower()
    assert 'recede' in desc, (
        'the model is never told a background must recede behind the content')
    assert 'filter' in desc and 'brightness' in desc, (
        'no concrete darkening remedy is given — opacity alone does not make '
        'a busy image recede, which is the measured failure')
    assert 'opacity alone' in desc, (
        'the specific trap (opacity without a filter) is not named')
    # …and the complement: a hero/subject asset must NOT be dimmed.
    assert 'subject' in desc, (
        'without the exemption the model will also dim assets that ARE the '
        'subject, which is the opposite defect')


def test_gate_rejects_a_bad_asset_reference_before_chrome(library, tmp_path,
                                                          monkeypatch):
    """The asset check must run in the PURE phase of _full_gate.

    Ordering is the point: a render costs ~3.5x realtime, so an invented
    filename has to be caught before a browser boots. Proven by making the
    real gates explode — the finding must still come back.
    """
    from lib.motion_video import _scene_author as sa

    scene = tmp_path / 'scene-gate'
    scene.mkdir()
    monkeypatch.setattr('lib.motion_video._render.check_project',
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError('Chrome must not boot')))
    html = (
        '<!doctype html><html><head><meta charset="UTF-8"></head><body>'
        '<div id="root" data-composition-id="main" data-start="0" '
        'data-duration="4.0" data-width="1080" data-height="1440">'
        '<img src="assets/invented.png"></div>'
        '<script>window.__timelines={};</script></body></html>')
    findings = sa._full_gate(html, str(scene))
    assert any('does not exist' in f for f in findings), findings


def test_NEUTER_without_ref_verification_a_broken_asset_ships(library,
                                                              tmp_path,
                                                              monkeypatch):
    """NEUTER: amputate verify_asset_refs → an invented filename sails through
    the pure phase, which is how a frame with a blank hole reaches a render."""
    from lib.motion_video import _scene_author as sa

    scene = tmp_path / 'scene-neuter'
    scene.mkdir()
    monkeypatch.setattr('lib.motion_video._assets.verify_asset_refs',
                        lambda html, d: [])
    monkeypatch.setattr('lib.motion_video._render.check_project',
                        lambda *a, **k: {'ok': True, 'errors': [],
                                         'fix_hints': [], 'category': ''})
    html = (
        '<!doctype html><html><head><meta charset="UTF-8"></head><body>'
        '<div id="root" data-composition-id="main" data-start="0" '
        'data-duration="4.0" data-width="1080" data-height="1440">'
        '<img src="assets/invented.png"></div>'
        '<script>window.__timelines={};</script></body></html>')
    findings = sa._full_gate(html, str(scene))
    assert not any('does not exist' in f for f in findings), (
        'NEUTER did not bite: with the asset check removed a reference to a '
        'non-existent file must pass, which is the defect we are guarding')


def test_NEUTER_without_font_check_a_ghost_family_ships(library, tmp_path,
                                                        monkeypatch):
    """NEUTER: amputate the undeclared-family check → naming an absent face
    passes, restoring the silent serif substitution."""
    from lib.motion_video import _scene_author as sa

    scene = tmp_path / 'scene-font-neuter'
    scene.mkdir()
    monkeypatch.setattr('lib.motion_video._render.check_project',
                        lambda *a, **k: {'ok': True, 'errors': [],
                                         'fix_hints': [], 'category': ''})
    html = (
        '<!doctype html><html><head><meta charset="UTF-8">'
        "<style>h1{font-family:'PingFang SC',sans-serif}</style></head><body>"
        '<div id="root" data-composition-id="main" data-start="0" '
        'data-duration="4.0" data-width="1080" data-height="1440"></div>'
        '<script>window.__timelines={};</script></body></html>')
    with_check = sa._full_gate(html, str(scene))
    assert any('PingFang SC' in f for f in with_check), with_check

    monkeypatch.setattr('lib.motion_video._fonts.undeclared_font_families',
                        lambda html: [])
    without = sa._full_gate(html, str(scene))
    assert not any('PingFang SC' in f for f in without), (
        'NEUTER did not bite: without the font check a ghost family must pass')
