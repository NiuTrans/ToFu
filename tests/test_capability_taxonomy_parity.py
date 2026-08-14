#!/usr/bin/env python3
"""tests/test_capability_taxonomy_parity.py — SSOT parity guard.

Locks the invariants of ``lib/model_info/capability_taxonomy.py`` so a future
edit can't silently re-fork the classification and re-introduce the
"Doubao-Seed-ASR-2.0 in the chat preset dropdown" bug that motivated this
refactor.

Invariants asserted:

  1. The frontend picker set ``CHAT_EXCLUDED_CAPS`` is byte-identical to the
     hardcoded fallback array in ``static/js/core/model_caps.js``.
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

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_capability_taxonomy_parity.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODEL_CAPS_JS = os.path.join(
    _ROOT, 'frontend', 'src', 'runtime', 'app-runtime.js')


# ── Helpers ──────────────────────────────────────────────────────────────

def _read_frontend_fallback() -> list[str]:
    """Return the hardcoded fallback array literal from core/model_caps.js.

    We parse the literal by regex rather than executing the JS so this test
    stays pure-python. The JS keeps the array on a single line so a simple
    re.search is unambiguous.
    """
    with open(_MODEL_CAPS_JS, encoding='utf-8') as f:
        src = f.read()
    m = re.search(
        r'_FALLBACK_CHAT_EXCLUDED_CAPS\s*=\s*\[([^\]]+)\]', src)
    assert m, 'Could not locate _FALLBACK_CHAT_EXCLUDED_CAPS literal in ' \
              'frontend/src/runtime/app-runtime.js — did the array get reformatted?'
    inner = m.group(1)
    return [s.strip().strip("'\"") for s in inner.split(',') if s.strip()]


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Invariant 1: frontend fallback ══════════════════════════════════════

def test_frontend_fallback_matches_backend_chat_excluded_caps():
    """core/model_caps.js's hardcoded fallback == CHAT_EXCLUDED_CAPS."""
    from lib.model_info.capability_taxonomy import CHAT_EXCLUDED_CAPS
    fe = set(_read_frontend_fallback())
    assert fe == set(CHAT_EXCLUDED_CAPS), (
        'Frontend fallback drift! core/model_caps.js has %r, Python taxonomy '
        'has %r. Either update the .js fallback or the Python set — they MUST '
        'stay identical so a boot with no server response still filters '
        'correctly.' % (sorted(fe), sorted(CHAT_EXCLUDED_CAPS))
    )


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


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
