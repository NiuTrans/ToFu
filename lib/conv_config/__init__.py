"""lib/conv_config — Conversation config + settings resolution.

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

This module is a pure re-export facade — all implementations live in the
sub-modules (``_legacy``, ``_translate``, ``_flow``, ``_util``, ``_resolve``).
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


from lib.conv_config._legacy import (  # noqa: E402,F401
    _LEGACY_PRESET_TO_MODEL,
    _LEGACY_PRESET_TO_DEPTH,
    canonicalise_model_id,
    extract_legacy_thinking_depth,
)

from lib.conv_config._translate import (  # noqa: E402,F401
    AUTO_TRANSLATE_DEFAULT,
    TRANSLATE_TARGET_DEFAULT,
    resolve_auto_translate,
    resolve_translate_target,
    target_lang_code,
)

from lib.conv_config._flow import (  # noqa: E402,F401
    _KNOWN_FLOW_BUILTINS,
    _parse_active_flow,
)

from lib.conv_config._util import (  # noqa: E402,F401
    _coerce_bool,
    _first_defined,
    _pick,
)

from lib.conv_config._resolve import (  # noqa: E402,F401
    resolve_conv_config,
    resolve_conv_settings,
)


__all__ = [
    'resolve_conv_config', 'resolve_conv_settings',
    'canonicalise_model_id', 'extract_legacy_thinking_depth',
    'resolve_auto_translate', 'AUTO_TRANSLATE_DEFAULT',
    'resolve_translate_target', 'target_lang_code', 'TRANSLATE_TARGET_DEFAULT',
]
