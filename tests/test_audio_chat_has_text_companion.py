#!/usr/bin/env python3
"""tests/test_audio_chat_has_text_companion.py — audio_chat 'text' companion guard.

Locks a latent invariant: **every slot carrying the `audio_chat` capability
MUST also carry `text`.**

Rationale (why this test is load-bearing):

  * The dispatcher's `_is_chat_compatible` uses
    ``slot.capabilities.issubset(DISPATCHER_NON_CHAT_CAPS)`` where
    ``DISPATCHER_NON_CHAT_CAPS = {image_gen, embedding, transcription, audio_chat}``.
  * A slot with caps EXACTLY ``{'audio_chat'}`` (no ``text``) is a subset of
    that non-chat set → silently excluded from chat dispatch, even though
    the intent of ``audio_chat`` is "omni chat model that also takes audio".
  * Every currently-shipped omni model (``gemini-3-flash-preview``,
    ``LongCat-Flash-Omni-2603`` — both in meituan.json AND
    DEFAULT_SLOT_CONFIGS) carries ``text`` alongside ``audio_chat`` so the
    guard is presently unreachable.
  * This test prevents a future edit from dropping ``text`` and silently
    reintroducing the exclusion.

Two independent sources are audited:
  1. Every ``static/provider_templates/*.json`` model list.
  2. Every entry in ``lib.llm_dispatch.config.DEFAULT_SLOT_CONFIGS``.

Both must be clean.

Split off from pt_e355b7fbb6bb43ae (capability taxonomy SSOT); see
epic pt_13862a83926f4e7f on the project board.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_audio_chat_has_text_companion.py -v
"""
from __future__ import annotations

import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib.mcp.registry import is_opensource_build

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _template_json_paths() -> list[str]:
    return sorted(glob.glob(os.path.join(
        _ROOT, 'static', 'provider_templates', '*.json')))


def test_shipped_provider_templates_have_audio_chat_with_text():
    """Every model in shipped provider templates that carries ``audio_chat``
    must also carry ``text``, else the dispatcher silently drops it."""
    violations: list[tuple[str, str, list[str]]] = []
    scanned = 0
    for path in _template_json_paths():
        with open(path, encoding='utf-8') as f:
            tpl = json.load(f)
        for m in tpl.get('models') or []:
            caps = set(m.get('capabilities') or [])
            if 'audio_chat' not in caps:
                continue
            scanned += 1
            if 'text' not in caps:
                violations.append((
                    os.path.basename(path),
                    m.get('model_id', '<unknown>'),
                    sorted(caps),
                ))
    assert not violations, (
        'audio_chat slot(s) missing `text` companion — the dispatcher will '
        'silently exclude them from chat via issubset(DISPATCHER_NON_CHAT_CAPS):\n'
        + '\n'.join('  %s / %s caps=%r' % v for v in violations)
    )
    # Sanity: the audit isn't vacuously green — some audio_chat models
    # MUST exist in shipped templates. (2026-07 baseline: gemini-3-flash-preview
    # + LongCat-Flash-Omni-2603 in meituan.json.) Scoped to the internal
    # tree: opensource exports do NOT ship the internal provider template,
    # so their shipped set legitimately contains zero audio_chat models —
    # the violations audit above still covers every template that IS there.
    if not is_opensource_build():
        assert scanned > 0, (
            'No audio_chat models found in static/provider_templates/ — did '
            'the audit path change? If audio_chat was removed everywhere this '
            'test can be deleted; otherwise it means the scan is broken.')


def test_default_slot_configs_have_audio_chat_with_text():
    """Same invariant applied to the ``DEFAULT_SLOT_CONFIGS`` in-code table.

    ``DEFAULT_SLOT_CONFIGS`` seeds the slot pool before benchmark data loads
    and can OVERRIDE a template's capability list at build time (see
    ``LLMDispatcher._build_slots_from_providers``), so it MUST satisfy the
    invariant on its own even if the templates do.
    """
    from lib.llm_dispatch.config._slots import DEFAULT_SLOT_CONFIGS
    violations: list[tuple[str, list[str]]] = []
    scanned = 0
    for model_id, cfg in DEFAULT_SLOT_CONFIGS.items():
        caps = set(cfg.get('caps') or [])
        if 'audio_chat' not in caps:
            continue
        scanned += 1
        if 'text' not in caps:
            violations.append((model_id, sorted(caps)))
    assert not violations, (
        'DEFAULT_SLOT_CONFIGS audio_chat entry missing `text` companion:\n'
        + '\n'.join('  %s caps=%r' % v for v in violations)
    )
    assert scanned > 0, (
        'No audio_chat entries in DEFAULT_SLOT_CONFIGS — did the audit path '
        'change? gemini-3-flash-preview + LongCat-Flash-Omni-2603 should be '
        'here as of 2026-07.')


def test_neuter_removing_text_from_audio_chat_flips_this_guard():
    """NEUTER: verifies the guard is load-bearing.

    Simulate a would-be-buggy shipped config by constructing a caps list
    ``{'audio_chat'}`` (no text) and asserting the reusable predicate
    treats it as a violation. If future refactoring drops this predicate
    to a tautology, this test flips red.
    """
    def _would_be_dropped_by_dispatcher(caps):
        # Mirrors LLMDispatcher._is_chat_compatible's negation.
        from lib.model_info.capability_taxonomy import DISPATCHER_NON_CHAT_CAPS
        return set(caps).issubset(DISPATCHER_NON_CHAT_CAPS)

    # {'audio_chat'} alone IS a subset of the non-chat set → dropped.
    assert _would_be_dropped_by_dispatcher({'audio_chat'}) is True, (
        'Neuter failed: an audio_chat-only slot should be subset of '
        'DISPATCHER_NON_CHAT_CAPS. If this assertion no longer holds, the '
        'dispatcher exclusion has been changed and this guard should be '
        'reviewed accordingly.')
    # {text, audio_chat, ...} is NOT a subset → NOT dropped (chat-eligible).
    assert _would_be_dropped_by_dispatcher(
        {'text', 'vision', 'audio_chat'}) is False


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
