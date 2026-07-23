"""lib/conv_config/_translate.py — unified auto-translate trigger decision.

The single backend source of truth for the per-conversation ``autoTranslate``
flag. Every trigger path (input send-path, server-side safety net, incremental
per-round gate, headless API path) resolves through :func:`resolve_auto_translate`
so the layers can never disagree.
"""

from __future__ import annotations

from typing import Mapping, Optional

from lib.log import get_logger

logger = get_logger(__name__)


#: Canonical default for the per-conversation ``autoTranslate`` flag when no
#: explicit value exists anywhere. Translation costs an extra LLM round-trip
#: plus latency on every turn, so it is OPT-IN (OFF). This single constant is
#: the source of truth — every trigger-path read (input send-path, the
#: server-side safety net, the incremental per-round gate, the headless API
#: path) MUST resolve through :func:`resolve_auto_translate` so the three
#: layers can never disagree (the historical three-way default split that made
#: auto-translate fire unpredictably).
AUTO_TRANSLATE_DEFAULT = False


def resolve_auto_translate(*sources: Optional[Mapping]) -> bool:
    """Resolve the effective ``autoTranslate`` decision from one or more dicts.

    Each ``source`` is a settings/config-shaped mapping that MAY carry an
    ``autoTranslate`` key. Sources are consulted left-to-right; the FIRST one
    that defines the key (value is not ``None``) wins and its truthiness is
    returned. When no source defines it, the canonical
    :data:`AUTO_TRANSLATE_DEFAULT` (OFF) is returned.

    This is the ONE backend entry point for the trigger decision — callers
    (``routes/chat.py`` send path, ``lib/chat/turn_builder`` input path,
    ``lib/message_queue``, the ``lib/tasks_pkg/auto_translate`` safety net, the
    ``lib/translate/incremental`` gate, and the headless ``routes/api_v1/chat``
    path) pass whichever dict they hold and never embed a literal default
    again. Passing several sources lets a caller express precedence (e.g. an
    explicit per-request config overriding stored conv settings) without
    duplicating the "first-defined-wins, else OFF" rule.
    """
    for src in sources:
        if not src:
            continue
        val = src.get('autoTranslate')
        if val is not None:
            return bool(val)
    return AUTO_TRANSLATE_DEFAULT
