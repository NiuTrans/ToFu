"""Guards for pt_3879f00e sub-part 3 slice C — defer tofu-pet.js (65KB)
+ tofu-scene.js (96KB) from the CORE boot bundle into _CLASSIC_ASSET_FILES.

The pair is the ~160KB decorative family (project-bar pet mascot +
procedural canvas backdrop) — zero first-paint necessity.

Pre-landed properties (census 2026-08-01, re-verified at slice time):
  * exactly one window namespace each (window.TofuPet / window.TofuScene),
  * ZERO external JS callers — the only cross-references are between the
    two modules themselves and ALL are window-guarded
    (window.TofuScene && typeof … / window.TofuPet && typeof …),
  * the sole external reference is index.html's sceneSwitchBtn onclick
    `window.TofuPet&&window.TofuPet.cycleDecor()` — natively absence-safe
    (&& short-circuit, no ReferenceError when the module is not yet loaded),
  * both IIFEs self-boot through the readyState guard
    (DOMContentLoaded OR immediate when already parsed), so a bundle that
    lands after boot still boots the pet — no one-time wiring to miss,
  * the app→pet signal seam is document.dispatchEvent(CustomEvent) —
    fire-and-forget; absent listeners are a no-op, no dispatch-side gate,
  * the mount target #projectBar starts display:none and fades itself in,
    so the pet appearing with the idle-prefetched feature bundle (~2s
    after boot) causes no layout shift.

NO feature-loader stub by design (same argument as health_stream_timer):
there is no one-time boot wiring — the modules self-boot whenever they
arrive, and the onclick entry point is already absence-safe.

Suite shape: manifest double-assertions (checks 1-4) are the failing-first
RED drivers; the rest are GREEN-now controls that pin the absence-safe
properties the deferral relies on (a future edit breaking one flips RED
even though the manifest move stays in place).
"""

from __future__ import annotations

import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLER_PY = ROOT / 'lib' / 'js_bundler.py'
INDEX_HTML = ROOT / 'index.html'
PET_JS = ROOT / 'static' / 'js' / 'tofu-pet.js'
SCENE_JS = ROOT / 'static' / 'js' / 'tofu-scene.js'
FEATURE_LOADER = ROOT / 'static' / 'js' / 'feature-bridge.js'


def _manifest():
    import lib.js_bundler as jb
    return jb._extract_manifest_from_source(str(BUNDLER_PY))


# ---------------------------------------------------------------------------
# 1. manifest move (the failing-first drivers)
# ---------------------------------------------------------------------------
def test_tofu_pet_in_deferred_files():
    _bf, deferred_files, _entry, _crit = _manifest()
    assert 'tofu-pet.js' in deferred_files, (
        "'tofu-pet.js' must be in _CLASSIC_ASSET_FILES — 65KB of decorative "
        'pet out of the render-blocking core')


def test_tofu_pet_not_in_core_bundle_files():
    bundle_files, _df, _entry, _crit = _manifest()
    assert 'tofu-pet.js' not in bundle_files, (
        "'tofu-pet.js' must NOT remain in _BUNDLE_FILES — listing it in "
        'both bundles would double-boot the pet (two mascots, doubled '
        'animation frames)')


def test_tofu_scene_in_deferred_files():
    _bf, deferred_files, _entry, _crit = _manifest()
    assert 'tofu-scene.js' in deferred_files, (
        "'tofu-scene.js' must be in _CLASSIC_ASSET_FILES — 96KB of decorative "
        'canvas backdrop out of the render-blocking core')


def test_tofu_scene_not_in_core_bundle_files():
    bundle_files, _df, _entry, _crit = _manifest()
    assert 'tofu-scene.js' not in bundle_files, (
        "'tofu-scene.js' must NOT remain in _BUNDLE_FILES — listing it in "
        'both bundles would double-mount the canvas backdrop')


# ---------------------------------------------------------------------------
# 2. the sole external reference is absence-safe (control)
# ---------------------------------------------------------------------------
def test_scene_switch_onclick_absence_safe():
    src = INDEX_HTML.read_text()
    assert 'window.TofuPet&&window.TofuPet.cycleDecor()' in src, (
        "index.html's sceneSwitchBtn onclick must keep the "
        '`window.TofuPet&&…` short-circuit — with the module deferred the '
        'handler fires before the pet arrives and must no-op, never '
        'ReferenceError')


def test_index_has_no_raw_decorative_scripts():
    src = INDEX_HTML.read_text()
    assert 'static/js/tofu-pet.js' not in src
    assert 'static/js/tofu-scene.js' not in src
    assert '<!-- TOFU_APP_ASSETS -->' in src


# ---------------------------------------------------------------------------
# 3. no-stub design pin (control; mirrors test_no_tw_stub_entries)
# ---------------------------------------------------------------------------
def test_no_tofu_stub_entries_in_either_list():
    """TofuPet/TofuScene/cycleDecor must NOT be feature-loader stubs: the
    modules self-boot on arrival and the one onclick is already
    absence-safe — a stub would only trigger the feature fetch on a
    decorative click the idle prefetch already makes instant."""
    _bf, _df, entry_points, _crit = _manifest()
    for name in ('TofuPet', 'TofuScene', 'cycleDecor', 'setDecor'):
        assert name not in entry_points, (
            f'{name} must NOT be a deferred entry point — self-booting '
            'modules with absence-safe callers need no stub')
    loader = FEATURE_LOADER.read_text()
    for name in ('TofuPet', 'TofuScene', 'cycleDecor', 'setDecor'):
        assert f"'{name}'" not in loader, (
            f'{name} must NOT be in feature-bridge.js stub list either')


# ---------------------------------------------------------------------------
# 4. self-boot guards — the property that makes zero-stub safe (control)
# ---------------------------------------------------------------------------
_BOOT_GUARD_RE = re.compile(
    r"document\.readyState\s*===\s*'loading'[\s\S]{0,200}?"
    r"DOMContentLoaded['\"]\s*,\s*_boot")


def test_tofu_pet_self_boot_ready_state_guard():
    assert _BOOT_GUARD_RE.search(PET_JS.read_text()), (
        'tofu-pet.js lost its readyState-guarded self-boot — a deferred '
        'module that arrives AFTER DOMContentLoaded would never boot '
        '(the else-branch immediate _boot() is what makes zero-stub safe)')


def test_tofu_scene_self_boot_ready_state_guard():
    assert _BOOT_GUARD_RE.search(SCENE_JS.read_text()), (
        'tofu-scene.js lost its readyState-guarded self-boot — a deferred '
        'module that arrives AFTER DOMContentLoaded would never boot')


# ---------------------------------------------------------------------------
# 5. cross-references between the pair are window-guarded (control)
# ---------------------------------------------------------------------------
def test_pet_reads_scene_guarded():
    guards = re.findall(
        r'window\.TofuScene\s*&&', PET_JS.read_text())
    assert len(guards) >= 3, (
        f'tofu-pet.js must window-guard every TofuScene read (lightInfo / '
        f'critterX / spook) — the scene may still be in flight; found '
        f'{len(guards)} guard(s)')


def test_scene_reads_pet_guarded():
    guards = re.findall(
        r'window\.TofuPet\s*&&', SCENE_JS.read_text())
    assert len(guards) >= 3, (
        f'tofu-scene.js must window-guard every TofuPet read '
        f'(getState ×3) — the pet may still be in flight; found '
        f'{len(guards)} guard(s)')


# ---------------------------------------------------------------------------
# 6. zero EXTERNAL callers census — the load-bearing census (control)
# ---------------------------------------------------------------------------
def test_no_external_tofu_callers_repo_wide():
    """Re-run of the slice census: outside tofu-pet.js / tofu-scene.js
    themselves (and built artifacts), no JS source may reference
    TofuPet./TofuScene. — a new unguarded external caller would
    ReferenceError in the pre-load window."""
    import os
    call_re = re.compile(r'\bTofu(?:Pet|Scene)\s*[.(]')
    built_re = re.compile(r'^(?:bundle|feature|i18n-(?:zh|en))-[0-9a-f]{8}\.js$')
    violations = []
    for dirpath, _dirs, files in os.walk(ROOT / 'static' / 'js'):
        for fn in files:
            if (not fn.endswith('.js') or built_re.match(fn)
                    or fn in ('tofu-pet.js', 'tofu-scene.js')):
                continue
            path = pathlib.Path(dirpath) / fn
            for i, line in enumerate(
                    path.read_text(encoding='utf-8').splitlines(), 1):
                stripped = line.lstrip()
                if (stripped.startswith('/*') or stripped.startswith('*')
                        or stripped.startswith('//')):
                    continue
                if call_re.search(line):
                    violations.append(f'{path.relative_to(ROOT)}:{i}')
    assert not violations, (
        'external TofuPet/TofuScene callers appeared (must be added '
        'absence-safe, or the deferral is no longer zero-gate):\n  '
        + '\n  '.join(violations))
