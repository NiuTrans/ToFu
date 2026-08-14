"""lib/conv_config/_legacy.py — legacy preset → canonical model_id migration.

Old configs stored brand keys like "qwen", "gemini", "opus", and
thinking-effort labels like "medium" / "high" / "max" in the same field. The
new design stores the actual model_id directly. This module mirrors the JS
``_LEGACY_PRESET_TO_MODEL`` constant in ``static/js/core.js`` so a config
posted with a legacy preset name resolves to the same model the UI would have
applied.
"""

from __future__ import annotations

from typing import Any, Optional

from lib.log import get_logger

logger = get_logger(__name__)


# ── Legacy preset → canonical model_id migration ─────────────────────
#
# Old configs stored brand keys like "qwen", "gemini", "opus", and
# thinking-effort labels like "medium" / "high" / "max" in the same
# field. The new design stores the actual model_id directly. This
# table mirrors the JS ``_LEGACY_PRESET_TO_MODEL`` constant in
# ``static/js/core.js`` so a config posted with a legacy preset name
# resolves to the same model the UI would have applied.

_LEGACY_PRESET_TO_MODEL: dict[str, str] = {
    'qwen': 'qwen3.6-plus',
    'low': 'qwen3.6-plus',
    'gemini': 'gemini-3-flash-preview',
    'gemini_flash': 'gemini-3-flash-preview',
    'minimax': 'MiniMax-M2.7',
    'doubao': 'Doubao-Seed-2.0-pro',
    'opus': 'aws.claude-opus-4.7',
    # Early GPT-5.6 integration mistakenly represented Pro reasoning as a
    # separate SKU. The public API keeps the normal model ID and uses
    # ``reasoning.mode='pro'`` instead.
    'gpt-5.6-pro': 'gpt-5.6',
    # Compound preset → both a model AND a thinking depth. The model
    # choice falls back to opus; the depth is extracted separately
    # by ``extract_legacy_thinking_depth``.
    'medium': 'aws.claude-opus-4.7',
    'high': 'aws.claude-opus-4.7',
    'xhigh': 'aws.claude-opus-4.7',
    'max': 'aws.claude-opus-4.7',
}

# Compound preset → thinking depth label. Used to back-fill
# ``thinkingDepth`` when a config carried only the legacy preset.
_LEGACY_PRESET_TO_DEPTH: dict[str, str] = {
    'medium': 'medium',
    'high': 'high',
    'xhigh': 'xhigh',
    'max': 'max',
}


def canonicalise_model_id(value: Any) -> str:
    """Rewrite a legacy preset name to its canonical model_id.

    Pass-through for any value that's already a real model_id (or empty).
    Returns ``''`` for non-string input — same defensive contract as
    the JS impl.
    """
    if not isinstance(value, str) or not value:
        return ''
    return _LEGACY_PRESET_TO_MODEL.get(value, value)


def extract_legacy_thinking_depth(value: Any) -> Optional[str]:
    """Return a thinking-depth label when ``value`` is a compound legacy
    preset (``medium`` / ``high`` / ``xhigh`` / ``max``); else None.

    Used by ``resolve_conv_config`` to backfill ``thinkingDepth`` when
    the caller still ships a config with a legacy preset string in
    ``model``.
    """
    if not isinstance(value, str):
        return None
    return _LEGACY_PRESET_TO_DEPTH.get(value)
