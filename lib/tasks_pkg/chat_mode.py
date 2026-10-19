"""Everyday chat capability defaults; workspace identity is not a chat mode.

The conversation application resolves an explicit project or managed private
workspace before accepting a turn. Studio, Air and Pro are legacy aliases of
chat, not capability gates. Unlabelled SDK/headless callers retain their explicit
atomic flags unchanged; the agent kernel never creates an application workspace.
Frontend _CHAT_MODE_DEFAULTS mirrors this one table; the parity test guards it.
"""
from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['CHAT_MODES', 'DEFAULT_CHAT_MODE', 'normalize_chat_mode',
           'chat_mode_defaults', 'is_lean_mode', 'apply_chat_mode']

CHAT_MODES: tuple[str, ...] = ('chat',)
DEFAULT_CHAT_MODE = 'chat'
_LEGACY_ALIASES: dict[str, str] = {'air': 'chat', 'pro': 'chat', 'studio': 'chat'}


def normalize_chat_mode(cfg: dict | None) -> str | None:
    """Return explicitly selected chat, or None for unlabelled host config."""
    if not isinstance(cfg, dict):
        return None
    mode = cfg.get('chatMode')
    if not isinstance(mode, str):
        return None
    norm = mode.strip().lower()
    if norm in CHAT_MODES:
        return norm
    return _LEGACY_ALIASES.get(norm)


def chat_mode_defaults(mode: str) -> dict[str, Any]:
    """Canonical everyday flags. Physical/optional extras remain orthogonal."""
    return {'searchMode': 'multi', 'fetchEnabled': True,
            'codeExecEnabled': True, 'memoryEnabled': True}


def is_lean_mode(mode: str | None) -> bool:
    """Retained capability-policy seam; no user-facing lean tier exists."""
    return False


def apply_chat_mode(cfg: dict | None) -> dict:
    """Expand explicit chat intent, preserving unlabelled host flags exactly."""
    base = dict(cfg or {})
    mode = normalize_chat_mode(base)
    if mode is None:
        return base
    defaults = chat_mode_defaults(mode)
    effective = {**base, **defaults, 'chatMode': mode}
    changed = [key for key in defaults if base.get(key) != defaults[key]]
    if changed:
        logger.info('[ChatMode] applied mode=%s — set %d flag(s): %s',
                    mode, len(changed), ', '.join(sorted(changed)))
    return effective
