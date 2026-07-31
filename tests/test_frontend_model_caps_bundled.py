#!/usr/bin/env python3
"""tests/test_frontend_model_caps_bundled.py — bundle integrity guard.

Motivated by a real production incident (2026-07-24): commit e0a49243 added
``core/model_caps.js`` (the SSOT for ``window.isChatModel``) and appended it
to ``lib/js_bundler.py::_BUNDLE_FILES``. A long-running server had already
imported the old ``_BUNDLE_FILES`` list into memory, so its subsequent
bundle rebuilds used the STALE manifest — the shipped bundle was missing
``model_caps.js`` entirely. Every model picker that called the bare
identifier ``isChatModel(m)`` then threw ``ReferenceError`` and the
dropdown rendered empty.

The guards asserted here:

  1. **Presence** — ``build_bundle()`` produces a bundle whose bytes contain
     both ``window.isChatModel`` and ``window.applyCapabilityTaxonomy``
     (the two globals that ``main_toolbar_ui.js`` / ``visibility_defaults.js``
     / ``paper/report.js`` / ``template_actions.js`` depend on).

  2. **Manifest listing** — ``core/model_caps.js`` is in ``_BUNDLE_FILES``.
     Independent of what the bundler produced, ensures the manifest itself
     stays honest so a rebuild in a fresh process still ships it.

  3. **NEUTER** — temporarily monkey-patch ``_BUNDLE_FILES`` to REMOVE
     ``core/model_caps.js`` and prove the presence guard flips red. This is
     the "does this test actually cover something?" check — if it stays green
     after neutering, the guard is a tautology.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_frontend_model_caps_bundled.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_built_bundle_bytes() -> str:
    """Force a bundle build and return the resulting bytes as a string.

    Reads the ACTUAL file on disk that ``build_bundle()`` publishes, so we
    validate the exact artifact the request path would serve — the minify /
    esbuild pipeline can rewrite identifiers, so grepping the concatenated
    source pre-minify would give a false positive.
    """
    from lib import js_bundler
    filename = js_bundler.build_bundle()
    assert filename, 'build_bundle() returned None — core bundle failed to build'
    path = os.path.join(js_bundler.JS_DIR, filename)
    assert os.path.exists(path), (
        f'build_bundle() reported {filename} but no such file exists at {path}'
    )
    with open(path, encoding='utf-8') as f:
        return f.read()


# ── Guard 1: presence in the shipped bundle ═════════════════════════════

# Markers UNIQUE to core/model_caps.js. We deliberately do NOT check
# ``window.isChatModel`` / ``window.applyCapabilityTaxonomy`` here because
# main_toolbar_ui.js carries a defensive FALLBACK that installs those same
# two globals when the SSOT is missing — a substring match on either name
# would then be a false positive (guard stays green even when model_caps.js
# is gone). ``getChatExcludedCaps`` and ``CHAT_EXCLUDED_CAPS_FALLBACK`` are
# defined ONLY in model_caps.js (grep-verified) and, as top-level
# ``window.X = ...`` assignments, survive esbuild's script-mode minifier
# and _minify_js verbatim (top-level globals are never renamed — see
# lib/js_bundler.py::_esbuild_minify's safety argument).
_MODEL_CAPS_SIGNATURES = (
    'window.getChatExcludedCaps',
    'window.CHAT_EXCLUDED_CAPS_FALLBACK',
)


def test_bundle_contains_model_caps_signature_globals():
    """The bundle bytes MUST contain both signature globals from
    core/model_caps.js — proves the file was actually concatenated, not
    silently dropped (as happened 2026-07-24 with a stale bundler
    manifest)."""
    bundle = _read_built_bundle_bytes()
    for sig in _MODEL_CAPS_SIGNATURES:
        assert sig in bundle, (
            'core/model_caps.js contents missing from bundle — %s '
            'assignment not found. This is the exact failure that stranded '
            'the model dropdown on 2026-07-24. Check '
            'lib/js_bundler.py::_BUNDLE_FILES includes core/model_caps.js '
            'and that the file itself is on disk.' % sig
        )


# ── Guard 2: manifest listing ══════════════════════════════════════════

def test_bundle_manifest_lists_model_caps():
    """core/model_caps.js must be in _BUNDLE_FILES.

    Independent of the built artifact, ensures the manifest source of truth
    stays honest — a fresh process re-reading this list still ships it.
    """
    from lib.js_bundler import _BUNDLE_FILES
    assert 'core/model_caps.js' in _BUNDLE_FILES, (
        'core/model_caps.js was removed from lib/js_bundler.py::_BUNDLE_FILES. '
        'The SSOT for window.isChatModel MUST be in the manifest or every '
        'model picker throws ReferenceError.'
    )


# ── Guard 3: NEUTER ═════════════════════════════════════════════════════

def test_neuter_removing_model_caps_from_manifest_breaks_presence(monkeypatch, tmp_path):
    """Prove guard 1 is load-bearing by neutering the manifest.

    If we monkey-patch _BUNDLE_FILES to drop ``core/model_caps.js`` and
    force a rebuild, the resulting bundle MUST no longer contain
    ``window.isChatModel`` — proving the presence assertion actually tracks
    the manifest entry rather than being a tautology.

    We rebuild into an isolated JS_DIR so the neutered bundle can't
    accidentally get served by another test / a live server sharing the
    working tree.
    """
    from lib import js_bundler

    # Copy the real _BUNDLE_FILES minus the target, then re-run the internal
    # assembler against a private output dir so we don't publish a broken
    # bundle to static/js/.
    neutered = [f for f in js_bundler._BUNDLE_FILES if f != 'core/model_caps.js']
    assert len(neutered) == len(js_bundler._BUNDLE_FILES) - 1, (
        'Neuter setup failed — core/model_caps.js was already missing from '
        '_BUNDLE_FILES, so the guard cannot be tested.'
    )

    # We want the assemble path but into a scratch dir. Simplest: patch
    # JS_DIR to a copy that contains every source EXCEPT model_caps.js, run
    # _assemble_bundle, then read what it produced.
    import shutil
    scratch = tmp_path / 'js'
    shutil.copytree(js_bundler.JS_DIR, str(scratch))
    # Remove the neutered file so a stray hard-coded reference can't smuggle
    # its bytes back in via some other bundle entry.
    caps_path = scratch / 'core' / 'model_caps.js'
    if caps_path.exists():
        caps_path.unlink()

    monkeypatch.setattr(js_bundler, 'JS_DIR', str(scratch))
    filename, _size = js_bundler._assemble_bundle(
        neutered, 'bundle-neuter-', critical=False,
    )
    assert filename, 'Neutered assemble returned None — cannot verify neuter'

    with open(scratch / filename, encoding='utf-8') as f:
        neutered_bytes = f.read()

    for sig in _MODEL_CAPS_SIGNATURES:
        assert sig not in neutered_bytes, (
            'NEUTER did not neuter: removing core/model_caps.js from the '
            'manifest left %s in the bundle anyway. The presence guard above '
            'is therefore not actually tracking the manifest — some OTHER '
            'file must be defining this global (find it, or update the '
            'signature set to a marker unique to model_caps.js).' % sig
        )


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
