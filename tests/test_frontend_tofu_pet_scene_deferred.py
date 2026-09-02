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

import pytest

from tests._runtime_sections import (
    runtime_section_names,
    runtime_section_path,
    runtime_sections_dir,
)

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / 'index.html'
# lib/js_bundler.py and the static/js tree are gone with the Vite migration:
# the pair now lives in frontend/src/runtime/scene/ as idle-scheduled lazy
# chunks (imported from app-runtime.js after app-ready), and the retained
# runtime sections are materialized by tests/_runtime_sections.py.
PET_JS = pathlib.Path(runtime_section_path('tofu-pet.js'))
SCENE_JS = pathlib.Path(runtime_section_path('tofu-scene.js'))
FEATURE_LOADER = pathlib.Path(runtime_section_path('feature-bridge.js'))
ACTION_REGISTRY = ROOT / 'frontend' / 'src' / 'action-registry.ts'


def _manifest():
    """Return the logical sections owned by the retained Vite runtime."""
    return runtime_section_names()


# ---------------------------------------------------------------------------
# 1. manifest move (the failing-first drivers)
# ---------------------------------------------------------------------------
def test_tofu_pet_in_deferred_files():
    assert 'tofu-pet.js' in _manifest(), (
        "'tofu-pet.js' must be present in the Vite runtime — 65KB of "
        'decorative pet shipped as an idle-scheduled lazy chunk out of the '
        'render-blocking core')


def test_tofu_pet_not_in_core_bundle_files():
    assert _manifest().count('tofu-pet.js') == 1, (
        "'tofu-pet.js' must occur exactly once in the Vite runtime — a "
        'second copy would double-boot the pet (two mascots, doubled '
        'animation frames)')


def test_tofu_scene_in_deferred_files():
    assert 'tofu-scene.js' in _manifest(), (
        "'tofu-scene.js' must be present in the Vite runtime — 96KB of "
        'decorative canvas backdrop shipped as an idle-scheduled lazy chunk '
        'out of the render-blocking core')


def test_tofu_scene_not_in_core_bundle_files():
    assert _manifest().count('tofu-scene.js') == 1, (
        "'tofu-scene.js' must occur exactly once in the Vite runtime — a "
        'second copy would double-mount the canvas backdrop')


# ---------------------------------------------------------------------------
# 2. the sole external reference is absence-safe (control)
# ---------------------------------------------------------------------------
def test_scene_switch_onclick_absence_safe():
    src = INDEX_HTML.read_text()
    # The inline `window.TofuPet&&…` onclick became a delegated
    # data-tofu-action with the Vite migration. Absence-safety is preserved
    # by the mechanism, not the pin: the action-registry dispatcher resolves
    # TofuPet through the runtime service port and CATCHES resolution
    # failures, so a click in the pre-load window is a logged no-op, never a
    # ReferenceError escaping the handler.
    assert 'data-tofu-action="TofuPet.cycleDecor()"' in src, (
        "index.html's sceneSwitchBtn must reference the pet ONLY through "
        'the delegated data-tofu-action mechanism — with the module '
        'deferred the handler fires before the pet arrives and must no-op')
    assert not re.search(r'onclick="[^"]*TofuPet', src), (
        'a raw inline onclick referencing TofuPet would ReferenceError in '
        'the pre-load window — keep the reference on the delegated '
        'data-tofu-action dispatcher')
    registry = ACTION_REGISTRY.read_text()
    assert '[actions] refused' in registry, (
        'the action-registry dispatcher lost its try/catch refusal path — '
        'an unresolved TofuPet action would escape as an uncaught error '
        'instead of a logged no-op')


def test_index_has_no_raw_decorative_scripts():
    src = INDEX_HTML.read_text()
    assert 'static/js/tofu-pet.js' not in src
    assert 'static/js/tofu-scene.js' not in src
    assert '<!-- TOFU_APP_ASSETS -->' in src


# ---------------------------------------------------------------------------
# 3. no-stub design pin (control; mirrors test_no_tw_stub_entries)
# ---------------------------------------------------------------------------
def test_no_tofu_stub_entries_in_either_list():
    """TofuPet/TofuScene/cycleDecor/setDecor must NOT be feature-loader stubs: the
    modules self-boot on arrival and the one action reference is already
    absence-safe — a stub would only trigger the feature fetch on a
    decorative click the idle prefetch already makes instant.

    The bundler manifest half of this guard retired with lib/js_bundler.py;
    the surviving stub list is feature-bridge.js's _FEATURE_ENTRY_POINTS."""
    loader = FEATURE_LOADER.read_text()
    for name in ('TofuPet', 'TofuScene', 'cycleDecor', 'setDecor'):
        assert f"'{name}'" not in loader, (
            f'{name} must NOT be in feature-bridge.js stub list')


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
# The migrated sections read the sibling namespace through `runtimeScope`
# (the scope prelude's `typeof window !== "undefined" ? window : globalThis`)
# instead of a bare `window` — the guard semantics are identical.
_WINDOWED_GUARD = r'(?:window|runtimeScope)\.'


def test_pet_reads_scene_guarded():
    guards = re.findall(
        _WINDOWED_GUARD + r'TofuScene\s*&&', PET_JS.read_text())
    assert len(guards) >= 3, (
        f'tofu-pet.js must window-guard every TofuScene read (lightInfo / '
        f'critterX / spook) — the scene may still be in flight; found '
        f'{len(guards)} guard(s)')


def test_scene_reads_pet_guarded():
    guards = re.findall(
        _WINDOWED_GUARD + r'TofuPet\s*&&', SCENE_JS.read_text())
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
    ReferenceError in the pre-load window.

    The census walks the materialized migrated-runtime view
    (tests/_runtime_sections.py): the old ``static/js`` tree is gone, and
    walking a deleted directory made this guard pass VACUOUSLY."""
    import os
    js_root = pathlib.Path(runtime_sections_dir())
    call_re = re.compile(r'\bTofu(?:Pet|Scene)\s*[.(]')
    built_re = re.compile(r'^(?:bundle|feature|i18n-(?:zh|en))-[0-9a-f]{8}\.js$')
    violations = []
    for dirpath, dirs, files in os.walk(js_root):
        dirs[:] = [d for d in dirs if not d.startswith(('.', '__'))]
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
                    violations.append(f'{path.relative_to(js_root)}:{i}')
    assert not violations, (
        'external TofuPet/TofuScene callers appeared (must be added '
        'absence-safe, or the deferral is no longer zero-gate):\n  '
        + '\n  '.join(violations))
