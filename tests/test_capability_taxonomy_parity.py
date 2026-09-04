#!/usr/bin/env python3
"""tests/test_capability_taxonomy_parity.py — SSOT parity guard.

Locks the invariants of ``lib/model_info/capability_taxonomy.py`` so a future
edit can't silently re-fork the classification and re-introduce the
"Doubao-Seed-ASR-2.0 in the chat preset dropdown" bug that motivated this
refactor.

Invariants asserted:

  1. The typed browser owner's public fallback is identical to
     ``CHAT_EXCLUDED_CAPS`` and its controller applies server replacements
     without exposing mutable internal state.
  2. The dispatcher's live ``_NON_CHAT_CAPS`` == the taxonomy's
     ``DISPATCHER_NON_CHAT_CAPS`` (NOT ``CHAT_EXCLUDED_CAPS`` — the two
     sets are deliberately different by exactly ``{'audio_chat'}``).
  3. The pricing module's ``_NON_CHAT_CAPS`` == ``DISPATCHER_NON_CHAT_CAPS``
     (same reason — pricing uses the same ``issubset`` shape).
  4. ``/api/v1/capabilities`` carries ``capability_taxonomy`` with the
     expected keys and matching values.
  5. Behavioral: ``is_chat_model({transcription})`` is False (the ASR case
     that motivated the refactor); ``is_chat_model({text, audio_chat})``
     stays True (the omni-chat case).
  6. NEUTER: temporarily patch ``CHAT_EXCLUDED_CAPS`` to drop
     ``transcription`` and verify the guard flips red (so the parity check
     really covers the classification, not just an equality tautology).
  7. ``KNOWN_CAPABILITIES`` covers exactly the ``CAPABILITY_SEMANTICS`` keys,
     is published ordered in the payload, the typed owner's ordered-list
     fallback matches it, and the settings toggle grids read the
     taxonomy-driven list instead of a hardcoded literal.
  8. NEUTER: dropping a tag from ``KNOWN_CAPABILITIES`` flips the coverage
     invariant red (load-bearing, not tautology).

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_capability_taxonomy_parity.py -v
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

from tests._runtime_sections import native_module_path

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_MODEL_CAPABILITY_OWNER = (
    ROOT / 'frontend/src/core/model-capability-taxonomy.ts')


# ── Helpers ──────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _frontend_contract() -> dict[str, object]:
    """Exercise the public typed controller through the production bundler."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for typed-owner evaluation')
    bundle = native_module_path(
        '.native/model-capability-taxonomy.js', _MODEL_CAPABILITY_OWNER)
    script = r'''
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8'));
const standard = createModelCapabilityTaxonomy();
const snapshot = standard.getChatExcludedCaps();
snapshot.clear();
const malformed = createModelCapabilityTaxonomy();
const malformedAccepted = malformed.applyCapabilityTaxonomy({
  chat_excluded_caps: ['video_gen', 7],
});
const updated = createModelCapabilityTaxonomy();
const updateAccepted = updated.applyCapabilityTaxonomy({
  chat_excluded_caps: ['video_gen'],
  dispatcher_non_chat_caps: ['video_gen', 'audio_chat'],
});
const knownProbe = createModelCapabilityTaxonomy();
const knownFallbackSize = knownProbe.getKnownCapabilities().length;
knownProbe.getKnownCapabilities().push('zzz');  // must not mutate state
const malformedKnown = createModelCapabilityTaxonomy();
const malformedKnownAccepted = malformedKnown.applyCapabilityTaxonomy({
  chat_excluded_caps: ['video_gen'],
  known_capabilities: ['video_gen', 7],
});
const updatedKnown = createModelCapabilityTaxonomy();
const updatedKnownAccepted = updatedKnown.applyCapabilityTaxonomy({
  chat_excluded_caps: ['video_gen'],
  known_capabilities: ['video_gen', 'text'],
});
process.stdout.write(JSON.stringify({
  fallback: [...CHAT_EXCLUDED_CAPS_FALLBACK],
  fallbackFrozen: Object.isFrozen(CHAT_EXCLUDED_CAPS_FALLBACK),
  defaults: {
    transcription: standard.isChatModel({capabilities: ['transcription']}),
    tts: standard.isChatModel({capabilities: ['tts']}),
    audioChat: standard.isChatModel({capabilities: ['text', 'audio_chat']}),
    empty: standard.isChatModel({capabilities: []}),
    missing: standard.isChatModel({}),
    malformed: standard.isChatModel({capabilities: 'transcription'}),
  },
  snapshotIsDefensive: !standard.isChatModel({capabilities: ['transcription']}),
  malformedAccepted,
  malformedPreservedFallback:
    !malformed.isChatModel({capabilities: ['transcription']}),
  updateAccepted,
  updated: {
    video: updated.isChatModel({capabilities: ['video_gen']}),
    transcription: updated.isChatModel({capabilities: ['transcription']}),
  },
  knownFallback: knownProbe.getKnownCapabilities(),
  knownFallbackFrozen: Object.isFrozen(KNOWN_CAPABILITIES_FALLBACK),
  knownDefensiveCopy:
    knownProbe.getKnownCapabilities().length === knownFallbackSize,
  malformedKnownAccepted,
  malformedKnownExcludedPreserved:
    [...malformedKnown.getChatExcludedCaps()].sort(),
  malformedKnownListPreserved: malformedKnown.getKnownCapabilities(),
  updatedKnownAccepted,
  updatedKnown: updatedKnown.getKnownCapabilities(),
}));
'''
    result = subprocess.run(
        [node, '-e', script, bundle],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ── Invariant 1: frontend fallback ══════════════════════════════════════

def test_frontend_fallback_matches_backend_chat_excluded_caps():
    """The typed browser fallback equals the backend taxonomy."""
    from lib.model_info.capability_taxonomy import CHAT_EXCLUDED_CAPS
    contract = _frontend_contract()
    fe = set(contract['fallback'])
    assert fe == set(CHAT_EXCLUDED_CAPS), (
        'Frontend fallback drift! typed owner has %r, Python taxonomy has %r. '
        'Either update the browser fallback or the Python set — they MUST '
        'stay identical so a boot with no server response still filters '
        'correctly.' % (sorted(fe), sorted(CHAT_EXCLUDED_CAPS))
    )
    assert contract['fallbackFrozen'] is True


def test_frontend_controller_classifies_and_applies_valid_server_taxonomy():
    contract = _frontend_contract()
    assert contract['defaults'] == {
        'transcription': False,
        'tts': False,
        'audioChat': True,
        'empty': True,
        'missing': True,
        'malformed': True,
    }
    assert contract['snapshotIsDefensive'] is True
    assert contract['malformedAccepted'] is False
    assert contract['malformedPreservedFallback'] is True
    assert contract['updateAccepted'] is True
    assert contract['updated'] == {
        'video': False,
        'transcription': True,
    }


# ── Invariant 2: dispatcher ═════════════════════════════════════════════

def test_dispatcher_non_chat_caps_matches_taxonomy_dispatcher_set():
    """LLMDispatcher._NON_CHAT_CAPS must equal DISPATCHER_NON_CHAT_CAPS."""
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    from lib.model_info.capability_taxonomy import DISPATCHER_NON_CHAT_CAPS
    assert LLMDispatcher._NON_CHAT_CAPS == DISPATCHER_NON_CHAT_CAPS, (
        'Dispatcher drift: dispatcher._NON_CHAT_CAPS=%r vs taxonomy '
        'DISPATCHER_NON_CHAT_CAPS=%r' % (
            sorted(LLMDispatcher._NON_CHAT_CAPS),
            sorted(DISPATCHER_NON_CHAT_CAPS)))


def test_dispatcher_set_is_strict_superset_of_chat_excluded():
    """Dispatcher set = CHAT_EXCLUDED_CAPS | {'audio_chat'}. The delta is
    intentional (frontend hides transcription/image_gen/embedding, dispatcher
    additionally guards against slots carrying ONLY {audio_chat})."""
    from lib.model_info.capability_taxonomy import (
        CHAT_EXCLUDED_CAPS, DISPATCHER_NON_CHAT_CAPS,
    )
    assert DISPATCHER_NON_CHAT_CAPS - CHAT_EXCLUDED_CAPS == {'audio_chat'}, (
        'The two sets must differ by exactly {audio_chat}. If this fails, '
        'either audio_chat became a chat-picker exclusion (frontend bug — '
        'omni chat models would disappear) or the dispatcher stopped '
        'guarding pure-audio_chat slots.')


# ── Invariant 3: pricing ════════════════════════════════════════════════

def test_pricing_non_chat_caps_matches_taxonomy_dispatcher_set():
    """lib.llm_dispatch.config._pricing._NON_CHAT_CAPS uses the same
    ``issubset`` shape as the dispatcher, so it must equal the dispatcher
    set, NOT the frontend chat-excluded set."""
    from lib.llm_dispatch.config._pricing import _NON_CHAT_CAPS as pricing_set
    from lib.model_info.capability_taxonomy import DISPATCHER_NON_CHAT_CAPS
    assert pricing_set == DISPATCHER_NON_CHAT_CAPS, (
        'Pricing drift: pricing._NON_CHAT_CAPS=%r vs taxonomy '
        'DISPATCHER_NON_CHAT_CAPS=%r' % (
            sorted(pricing_set), sorted(DISPATCHER_NON_CHAT_CAPS)))


# ── Invariant 4: API surface ═════════════════════════════════════════════

def test_capabilities_payload_carries_taxonomy():
    """``_build_capabilities()`` (the function backing /api/v1/capabilities)
    surfaces a well-formed ``capability_taxonomy`` dict.

    We drive the builder directly instead of going through the Quart test
    client because that would import the full route tree, which — on a shared
    HEAD with sibling WIP — routinely fails to import for reasons unrelated
    to this refactor. The builder is a pure function of the SSOT + saved
    config, so a direct call is the same wire contract minus the transport.
    """
    from routes.api_v1.capabilities import _build_capabilities
    from lib.model_info.capability_taxonomy import (
        CHAT_EXCLUDED_CAPS, DISPATCHER_NON_CHAT_CAPS,
    )
    payload = _build_capabilities()
    tax = payload.get('capability_taxonomy')
    assert isinstance(tax, dict) and tax, 'capability_taxonomy missing from payload'
    assert 'chat_excluded_caps' in tax
    assert 'dispatcher_non_chat_caps' in tax
    assert 'capability_semantics' in tax
    assert set(tax['chat_excluded_caps']) == set(CHAT_EXCLUDED_CAPS)
    assert set(tax['dispatcher_non_chat_caps']) == set(DISPATCHER_NON_CHAT_CAPS)
    sem = tax['capability_semantics']
    # audio_chat stays in the chat picker; transcription is hidden — the two
    # behaviours that motivated the refactor.
    assert sem.get('audio_chat', {}).get('in_chat_picker') is True
    assert sem.get('transcription', {}).get('in_chat_picker') is False


# ── Invariant 5: behavioral ══════════════════════════════════════════════

def test_is_chat_model_hides_asr_and_keeps_omni_chat():
    """The concrete cases that motivated the refactor.

    * Doubao-Seed-ASR-2.0 (caps={'transcription'}) → NOT a chat model.
    * LongCat-Flash-Omni (caps={'text','vision','audio_chat'}) → IS a chat model.
    * Image-gen models → NOT chat.
    * Embedding models → NOT chat.
    * Plain text / vision / thinking → chat.
    * Empty / missing caps → chat (matches legacy default).
    """
    from lib.model_info.capability_taxonomy import is_chat_model
    assert is_chat_model(['transcription']) is False
    assert is_chat_model(['text', 'vision', 'audio_chat']) is True
    assert is_chat_model(['image_gen']) is False
    assert is_chat_model(['embedding']) is False
    assert is_chat_model(['text']) is True
    assert is_chat_model(['text', 'thinking', 'cheap']) is True
    assert is_chat_model([]) is True
    assert is_chat_model(None) is True


# ── Invariant 6: NEUTER ══════════════════════════════════════════════════

def test_neuter_drops_transcription_and_asr_leaks_into_chat(monkeypatch):
    """If ``CHAT_EXCLUDED_CAPS`` accidentally loses ``transcription``, the ASR
    guard breaks — this NEUTER proves the guard is load-bearing, not tautology.

    We can't mutate a frozenset in place, so we monkeypatch the module symbol
    to a smaller set and re-check the behavioral assertion. is_chat_model reads
    the module attribute at call time (it's a closure over the module-level
    frozenset), so patching CHAT_EXCLUDED_CAPS is what matters.
    """
    import lib.model_info.capability_taxonomy as tax
    patched = tax.CHAT_EXCLUDED_CAPS - {'transcription'}
    monkeypatch.setattr(tax, 'CHAT_EXCLUDED_CAPS', patched)
    # Under the neuter, an ASR-only model FALSELY looks like a chat model.
    assert tax.is_chat_model(['transcription']) is True, (
        'NEUTER did not flip is_chat_model — the transcription guard is '
        'not actually being enforced by CHAT_EXCLUDED_CAPS')


# ── Invariant 7: KNOWN_CAPABILITIES (ordered toggle list) ═══════════════

def test_known_capabilities_cover_exactly_the_semantics_keys():
    """Every capability tag exists in BOTH the ordered list and the
    semantics descriptor — a tag in only one place is how the settings
    toggle grids used to drift from the backend."""
    from lib.model_info.capability_taxonomy import (
        CAPABILITY_SEMANTICS, KNOWN_CAPABILITIES,
    )
    assert len(KNOWN_CAPABILITIES) == len(set(KNOWN_CAPABILITIES)), (
        'duplicate tag in KNOWN_CAPABILITIES: %r' % (KNOWN_CAPABILITIES,))
    assert set(KNOWN_CAPABILITIES) == set(CAPABILITY_SEMANTICS), (
        'KNOWN_CAPABILITIES / CAPABILITY_SEMANTICS drift: list-only=%r '
        'semantics-only=%r' % (
            sorted(set(KNOWN_CAPABILITIES) - set(CAPABILITY_SEMANTICS)),
            sorted(set(CAPABILITY_SEMANTICS) - set(KNOWN_CAPABILITIES))))


def test_payload_carries_ordered_known_capabilities():
    from lib.model_info.capability_taxonomy import (
        KNOWN_CAPABILITIES, taxonomy_payload,
    )
    assert taxonomy_payload()['known_capabilities'] == list(KNOWN_CAPABILITIES)


def test_frontend_known_capabilities_fallback_and_projection():
    """The typed owner's ordered-list fallback equals the backend list, its
    getter is a defensive copy, a malformed known_capabilities rejects the
    WHOLE payload (no half-applied projection), and a valid server list is
    adopted verbatim."""
    from lib.model_info.capability_taxonomy import (
        CHAT_EXCLUDED_CAPS, KNOWN_CAPABILITIES,
    )
    contract = _frontend_contract()
    assert contract['knownFallback'] == list(KNOWN_CAPABILITIES), (
        'Frontend KNOWN_CAPABILITIES_FALLBACK drift! typed owner has %r, '
        'backend taxonomy has %r — they MUST stay identical so the toggle '
        'grids render correctly before the server payload arrives.' % (
            contract['knownFallback'], list(KNOWN_CAPABILITIES)))
    assert contract['knownFallbackFrozen'] is True
    assert contract['knownDefensiveCopy'] is True
    assert contract['malformedKnownAccepted'] is False
    assert contract['malformedKnownExcludedPreserved'] == sorted(CHAT_EXCLUDED_CAPS)
    assert contract['malformedKnownListPreserved'] == list(KNOWN_CAPABILITIES)
    assert contract['updatedKnownAccepted'] is True
    assert contract['updatedKnown'] == ['video_gen', 'text']


def test_v2_settings_read_offering_capabilities_without_legacy_toggle_grid():
    """Settings project v2 Offering capabilities and compile no legacy
    Provider model editor or key-by-alias matrix."""
    sections = ROOT / 'frontend' / 'src' / 'runtime' / 'sections'
    manifest = (sections / 'manifest.json').read_text(encoding='utf-8')
    provider_render = (sections / 'settings/provider_render.js').read_text(
        encoding='utf-8')
    core_panel = (sections / 'settings/core_panel.js').read_text(
        encoding='utf-8')
    assert 'settings/model_edit.js' not in manifest
    assert 'settings/providers/access_matrix.js' not in manifest
    assert 'row.capabilities || []' in provider_render
    assert 'offering.capabilities || []' in core_panel


# ── Invariant 8: NEUTER for the ordered-list coverage ═══════════════════

def test_neuter_known_capabilities_loses_a_semantics_key(monkeypatch):
    """If KNOWN_CAPABILITIES silently drops a tag the semantics still
    describe, the coverage invariant (Invariant 7) must flip red — proving
    the check is load-bearing, not a tautology."""
    import lib.model_info.capability_taxonomy as tax
    patched = tuple(c for c in tax.KNOWN_CAPABILITIES if c != 'tts')
    monkeypatch.setattr(tax, 'KNOWN_CAPABILITIES', patched)
    assert set(tax.KNOWN_CAPABILITIES) != set(tax.CAPABILITY_SEMANTICS), (
        'NEUTER did not break coverage — Invariant 7 would not catch a '
        'dropped capability tag')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
