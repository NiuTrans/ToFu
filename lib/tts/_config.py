"""lib/tts/_config.py — capability constant, slot selection, deployment config.

Mirrors lib/transcription/_config.py: a *capability on a slot* (``tts``)
selects the synthesis target — NO vendor branches (CLAUDE.md §3.5). A
deployment registers an OpenAI-compatible provider whose model entry carries
``capabilities: ['tts']`` (explicit per-cell caps win in the dispatcher);
well-known public TTS model names additionally have DEFAULT_SLOT_CONFIGS
reference entries so they route without a hand-written cell.

Model name and voice are NEVER hardcoded here (owner directive 2026-07-25):
the model comes from the selected slot; the voice resolves per-request →
``data/config/tts.json:default_voice`` → the ``_FALLBACK_VOICE`` constant
(OpenAI's documented default, only a last-resort so a bare deployment still
synthesizes — override it via the config file).
"""

from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)

#: The capability tag a slot carries to be a synthesis target. Lives in
#: lib/model_info/capability_taxonomy's CHAT_EXCLUDED_CAPS so a TTS model
#: never leaks into chat pickers.
TTS_CAP = 'tts'

# Sanity: the taxonomy must classify us as a non-chat cap. Import-time assert
# so a rename upstream can't silently drift the plumbing (transcription does
# the same for its caps).
from lib.model_info.capability_taxonomy import (  # noqa: E402
    CAPABILITY_SEMANTICS as _CAP_SEMANTICS,
    CHAT_EXCLUDED_CAPS as _CHAT_EXCLUDED_CAPS,
)
assert TTS_CAP in _CHAT_EXCLUDED_CAPS, (
    "TTS_CAP must be in CHAT_EXCLUDED_CAPS (frontend picker filter)"
)
assert _CAP_SEMANTICS.get(TTS_CAP, {}).get('endpoint') == 'audio_speech', (
    "CAPABILITY_SEMANTICS['tts'].endpoint must stay 'audio_speech'"
)

#: Last-resort voice when neither the request nor data/config/tts.json names
#: one. 'alloy' is OpenAI's documented default; a compatible endpoint that
#: doesn't know it rejects the request with a clear 400 the caller surfaces.
#: Deployments SHOULD set default_voice in data/config/tts.json.
_FALLBACK_VOICE = 'alloy'


def _load_tts_config() -> dict:
    """Read ``data/config/tts.json`` (JSONC-tolerant); {} when absent/broken."""
    try:
        from lib.config_dir import config_path as _config_path
        from lib.json_store import read_json
        cfg = read_json(_config_path('tts.json'))
        if isinstance(cfg, dict):
            return cfg
    except Exception as e:
        logger.debug('[TTS] tts.json unreadable, using defaults: %s', e)
    return {}


def _cfg() -> dict:
    """Load the deployment config THROUGH THE PACKAGE FACADE.

    Test monkeypatches land on ``lib.tts._load_tts_config`` (the facade
    contract this package shares with lib/transcription); resolving the
    loader here through the facade is what lets those patches take effect
    for every reader below.
    """
    from lib import tts as _facade
    return _facade._load_tts_config()


def default_voice() -> str:
    """The deployment's configured voice, else the fallback constant."""
    v = (_cfg().get('default_voice') or '').strip()
    return v or _FALLBACK_VOICE


def default_format() -> str:
    """Preferred response_format ('wav' lossless for stitching, or 'mp3')."""
    v = (_cfg().get('default_format') or '').strip().lower()
    return v or 'wav'


def default_speed() -> float:
    """Configured speech rate multiplier (1.0 = provider default)."""
    try:
        v = float(_cfg().get('speed') or 0)
        if 0.25 <= v <= 4.0:
            return v
    except (ValueError, TypeError) as e:
        logger.debug('[TTS] bad speed in tts.json, using 1.0: %s', e)
    return 1.0


def max_input_chars() -> int:
    """Max characters per synthesis call (chunking bound for long scripts).

    Config ``max_input_chars`` wins; env ``TOFU_TTS_MAX_INPUT_CHARS`` next;
    default 2000 (conservative vs OpenAI's 4096 ceiling; smaller chunks also
    bound per-call latency and let segment progress tick visibly).
    """
    v = _cfg().get('max_input_chars')
    if isinstance(v, int) and v > 0:
        return v
    try:
        env_v = int(os.environ.get('TOFU_TTS_MAX_INPUT_CHARS', '') or 0)
        if env_v > 0:
            return env_v
    except (ValueError, TypeError) as e:
        logger.debug('[TTS] bad TOFU_TTS_MAX_INPUT_CHARS, using default: %s', e)
    return 2000


def _tts_slots() -> list:
    """Return configured tts-capable slots (best score first).

    Scans ``dispatcher.slots`` for the ``tts`` capability exactly as
    ``lib/transcription._transcription_slots`` scans for its caps. OAuth
    subscription slots are excluded (their endpoints expose no /audio/speech).
    """
    try:
        from lib.llm_dispatch.factory import get_dispatcher
        dispatcher = get_dispatcher()
        dispatcher.initialize()
    except Exception as e:
        logger.warning('[TTS] dispatcher unavailable: %s', e)
        return []
    slots = [s for s in dispatcher.slots
             if (TTS_CAP in (s.capabilities or set())) and not s.oauth]
    slots.sort(key=lambda s: s.score())
    return slots


def tts_available() -> bool:
    """True when at least one tts-capable slot is configured.

    The podcast feature degrades to script-only when this is False (owner
    directive: no hard failure when no TTS slot is registered).
    """
    # Resolve through the package facade so test monkeypatches on
    # ``lib.tts._tts_slots`` take effect (facade parity with transcription).
    from lib import tts as _facade
    return bool(_facade._tts_slots())


def list_tts_models() -> list[dict]:
    """Return ``[{model, provider_id}]`` for configured tts slots (deduped)."""
    from lib import tts as _facade
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for s in _facade._tts_slots():
        key = (s.model, s.provider_id or 'default')
        if key in seen:
            continue
        seen.add(key)
        out.append({'model': s.model, 'provider_id': s.provider_id or 'default'})
    return out
