"""lib/conv_config.py — Conversation config + settings resolution.

Server-side port of ``static/js/main.js:_buildConvConfig`` and
``_buildConvSettings``. Centralised so:

* The headless API can produce the same config the UI ships to chat
  endpoints — no "what fields does the chat task expect?" guessing
  for SDK callers.
* The merge policy ("active conv reads global toolbar; inactive conv
  reads stored conv settings") is documented and tested in one place.
* Adding a new config field means editing one Python file, not two
  JS functions + their 8 callsites.
* Legacy preset names (``opus`` / ``high`` / ``qwen`` / etc.) get
  canonicalised to real model_ids automatically — SDK callers passing
  the old strings work transparently.

Public API
----------

  resolve_conv_config(conv_settings, overrides, server_defaults, *, is_active)
      → dict (32 fields) — drop-in for `_buildConvConfig` output

  resolve_conv_settings(conv_settings, overrides)
      → dict (19 fields) — drop-in for `_buildConvSettings` output

  canonicalise_model_id(model_or_preset)
      → str — rewrites legacy preset keys (``opus`` → ``aws.claude-opus-4.7``,
              etc.) to canonical model_ids. Pass-through for already-canonical
              strings.

The inputs are plain dicts:
  * ``conv_settings`` — per-conversation stored settings (from DB row's
                        `settings` column, or `conversation.settings`).
  * ``overrides`` — fields the user has changed in the toolbar this
                    session (the JS impl reads these from globals like
                    ``config.model``, ``searchMode``, etc.).
  * ``server_defaults`` — fallback values when neither overrides nor
                          per-conv has a value (e.g. ``serverModel``).

This pattern matches the JS impl's logic exactly while making the
boundary clean: client sends the small "what changed" delta;
server merges it with persistent state and returns the canonical
config that goes to the chat task.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


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
    'gemini': 'gemini-3.1-flash-lite-preview',
    'gemini_flash': 'gemini-3-flash-preview',
    'minimax': 'MiniMax-M2.7',
    'doubao': 'Doubao-Seed-2.0-pro',
    'opus': 'aws.claude-opus-4.7',
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


def _coerce_bool(v: Any, default: bool = False) -> bool:
    """JS-compatible truthiness check.

    The JS impl uses ``!!conv.X`` everywhere; in Python ``bool(0) == False``,
    ``bool('') == False``, ``bool(None) == False``, which matches.
    """
    if v is None:
        return default
    return bool(v)


def _first_defined(*candidates):
    """Return the first non-``None`` candidate, falling back to ``None``."""
    for c in candidates:
        if c is not None:
            return c
    return None


def _pick(active: Any, inactive: Any, *, is_active: bool):
    """Pick ``active`` when current conv is active, else ``inactive``."""
    return active if is_active else inactive


def resolve_conv_config(
    conv_settings: Optional[Mapping] = None,
    overrides: Optional[Mapping] = None,
    server_defaults: Optional[Mapping] = None,
    *,
    is_active: bool = True,
) -> dict:
    """Resolve the runtime config dict that goes to chat task endpoints.

    Mirrors the JS ``_buildConvConfig`` field-by-field.

    Returns a fresh dict (caller may mutate freely).
    """
    conv = dict(conv_settings or {})
    ov = dict(overrides or {})
    defaults = dict(server_defaults or {})

    # Convenience:
    server_model = defaults.get('serverModel') or ''

    # Model: active uses ov.model || serverModel; inactive uses
    # conv.model || serverModel.
    model_active_raw = _first_defined(ov.get('model'), server_model) or ''
    model_inactive_raw = _first_defined(conv.get('model'), server_model) or ''
    model_raw = _pick(model_active_raw, model_inactive_raw,
                       is_active=is_active)
    # Canonicalise legacy preset names ("opus" / "high" / "qwen" / etc.)
    # to actual model_ids so chat-pipeline routing always sees a real id.
    model = canonicalise_model_id(model_raw)
    # Extract a depth from the same value when it was a compound preset
    # (matches JS: `if (['medium','high','xhigh','max'].includes(config.preset)
    # && !config.thinkingDepth) config.thinkingDepth = config.preset`).
    legacy_depth = extract_legacy_thinking_depth(model_raw)

    # ── Field-by-field, matching the JS impl exactly ──
    out = {
        'maxTokens': ov.get('maxTokens', defaults.get('maxTokens')),
        'thinkingEnabled': _coerce_bool(ov.get('thinkingEnabled'),
                                          defaults.get('thinkingEnabled', False)),
        'model': model,
        'preset': model,
        'systemPrompt': ov.get('systemPrompt') or defaults.get('systemPrompt') or '',
        'thinkingDepth': _pick(
            ov.get('thinkingDepth') or legacy_depth,
            conv.get('thinkingDepth') or legacy_depth or None,
            is_active=is_active,
        ),
        'temperature': ov.get('temperature', defaults.get('temperature')),
        'searchMode': _pick(
            ov.get('searchMode'),
            conv.get('searchMode') or 'multi',
            is_active=is_active,
        ),
        'fetchEnabled': _pick(
            _coerce_bool(ov.get('fetchEnabled'), True),
            _coerce_bool(conv.get('fetchEnabled'), False),
            is_active=is_active,
        ),
        'codeExecEnabled': _pick(
            _coerce_bool(ov.get('codeExecEnabled')),
            _coerce_bool(conv.get('codeExecEnabled')),
            is_active=is_active,
        ),
        'memoryEnabled': _pick(
            _coerce_bool(ov.get('memoryEnabled'), True),
            (_coerce_bool(conv.get('memoryEnabled'), True)
             if conv.get('memoryEnabled') is not None
             else True),
            is_active=is_active,
        ),
        'schedulerEnabled': _pick(
            _coerce_bool(ov.get('schedulerEnabled')),
            _coerce_bool(conv.get('schedulerEnabled')),
            is_active=is_active,
        ),
        'swarmEnabled': _pick(
            _coerce_bool(ov.get('swarmEnabled')),
            _coerce_bool(conv.get('swarmEnabled')),
            is_active=is_active,
        ),
        'projectPath': ov.get('projectPath') or conv.get('projectPath') or '',
        'projectPaths': list(conv.get('projectPaths') or []),
        'readOnlyPaths': list(conv.get('readOnlyPaths') or []),
        'autoApply': _coerce_bool(ov.get('autoApply'), False),
        'browserEnabled': _pick(
            _coerce_bool(ov.get('browserEnabled')),
            _coerce_bool(conv.get('browserEnabled')),
            is_active=is_active,
        ),
        'desktopEnabled': _pick(
            _coerce_bool(ov.get('desktopEnabled')),
            _coerce_bool(conv.get('desktopEnabled')),
            is_active=is_active,
        ),
        'imageGenEnabled': _pick(
            _coerce_bool(ov.get('imageGenEnabled')),
            _coerce_bool(conv.get('imageGenEnabled')),
            is_active=is_active,
        ),
        'humanGuidanceEnabled': _pick(
            _coerce_bool(ov.get('humanGuidanceEnabled')),
            _coerce_bool(conv.get('humanGuidanceEnabled')),
            is_active=is_active,
        ),
        'endpointMode': _pick(
            _coerce_bool(ov.get('endpointMode')),
            _coerce_bool(conv.get('endpointEnabled')),
            is_active=is_active,
        ),
        'autopilot': _pick(
            _coerce_bool(ov.get('autopilot')),
            _coerce_bool(conv.get('autopilotEnabled')),
            is_active=is_active,
        ),
        'agentBackend': ov.get('agentBackend') or 'builtin',
        'autoTranslate': (
            _coerce_bool(conv.get('autoTranslate'),
                          _coerce_bool(ov.get('autoTranslate'), False))
            if conv.get('autoTranslate') is not None
            else _coerce_bool(ov.get('autoTranslate'), False)
        ),
        'browserClientId': None,  # populated below
        'keepToolHistory': ov.get('keepToolHistory') is not False,
    }
    # browserClientId is gated on the resolved browserEnabled flag.
    if out['browserEnabled']:
        out['browserClientId'] = ov.get('browserClientId') or None
    return out


def resolve_conv_settings(
    conv_settings: Optional[Mapping] = None,
    overrides: Optional[Mapping] = None,
) -> dict:
    """Resolve the per-conversation settings dict for server persistence.

    Mirrors the JS ``_buildConvSettings`` field-by-field. Used as the
    ``settings`` payload on chat-send / regenerate / continue / branch
    POSTs and on PATCH ``/api/chat/tool-state``.
    """
    conv = dict(conv_settings or {})
    ov = dict(overrides or {})
    server_model = ov.get('serverModel') or ''

    raw_model = conv.get('model') or ov.get('model') or server_model
    model = canonicalise_model_id(raw_model)
    legacy_depth = extract_legacy_thinking_depth(raw_model)

    out = {
        'model': model,
        'preset': model,
        'thinkingDepth': (conv.get('thinkingDepth')
                          or ov.get('thinkingDepth')
                          or legacy_depth),
        'searchMode': conv.get('searchMode') or ov.get('searchMode') or 'multi',
        'fetchEnabled': _coerce_bool(conv.get('fetchEnabled')),
        'codeExecEnabled': _coerce_bool(conv.get('codeExecEnabled')),
        'browserEnabled': _coerce_bool(conv.get('browserEnabled')),
        'desktopEnabled': _coerce_bool(conv.get('desktopEnabled')),
        'memoryEnabled': (
            _coerce_bool(conv.get('memoryEnabled'), True)
            if conv.get('memoryEnabled') is not None else True
        ),
        'schedulerEnabled': _coerce_bool(conv.get('schedulerEnabled')),
        'swarmEnabled': _coerce_bool(conv.get('swarmEnabled')),
        'endpointEnabled': _coerce_bool(conv.get('endpointEnabled')),
        'autopilotEnabled': _coerce_bool(conv.get('autopilotEnabled')),
        'imageGenEnabled': _coerce_bool(conv.get('imageGenEnabled')),
        'humanGuidanceEnabled': _coerce_bool(conv.get('humanGuidanceEnabled')),
        'projectPath': conv.get('projectPath') or '',
        'projectPaths': list(conv.get('projectPaths') or []),
        'readOnlyPaths': list(conv.get('readOnlyPaths') or []),
        'autoTranslate': (
            _coerce_bool(conv.get('autoTranslate'),
                          _coerce_bool(ov.get('autoTranslate'), False))
            if conv.get('autoTranslate') is not None
            else _coerce_bool(ov.get('autoTranslate'), False)
        ),
        'folderId': conv.get('folderId') or None,
    }
    return out


__all__ = [
    'resolve_conv_config', 'resolve_conv_settings',
    'canonicalise_model_id', 'extract_legacy_thinking_depth',
]
