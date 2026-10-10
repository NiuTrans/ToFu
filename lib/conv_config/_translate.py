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

#: Canonical fallback target language for the OUTPUT side (assistant reply →
#: human). Before this was configurable the whole app hard-pinned Chinese, so
#: an unresolved/absent UI language keeps that behaviour — nothing regresses.
TRANSLATE_TARGET_DEFAULT = 'Chinese'

#: UI-language code (``_i18nLang`` / ``settings.uiLang``) → the language NAME
#: the translate engine prompt expects (``_build_translate_prompt`` builds
#: "Translate ... to {target}"). The UI currently ships only ``zh`` / ``en``;
#: extra rows are here so adding a locale is a one-line change, not a code hunt.
_UILANG_TO_TARGET = {
    'zh': 'Chinese',
    'zh-cn': 'Chinese',
    'zh-tw': 'Chinese',
    'en': 'English',
    'ja': 'Japanese',
    'ko': 'Korean',
    'fr': 'French',
    'de': 'German',
    'es': 'Spanish',
    'ru': 'Russian',
}

#: The full language NAME → its detector code (``detect_language().code``),
#: used by the already-in-target-language skip gate so it compares like with
#: like. Inverse of the coarse target mapping; only the codes the detector can
#: emit are listed.
_TARGET_TO_CODE = {
    'Chinese': 'zh',
    'English': 'en',
    'Japanese': 'ja',
    'Korean': 'ko',
    'French': 'fr',
    'German': 'de',
    'Spanish': 'es',
    'Russian': 'ru',
}


def resolve_translate_target(*sources: Optional[Mapping]) -> str:
    """Resolve the OUTPUT-side translate target language NAME from settings.

    Each ``source`` is a settings/config-shaped mapping that MAY carry a
    ``uiLang`` key (the frontend ``_i18nLang`` piped through
    ``_buildConvSettings`` / ``_buildConvConfig``). Sources are consulted
    left-to-right; the first that defines ``uiLang`` wins. The code is mapped to
    the language NAME the translate engine prompt expects. When no source
    defines it (headless caller, old frontend, unknown code) the canonical
    :data:`TRANSLATE_TARGET_DEFAULT` (Chinese) is returned so behaviour is
    byte-identical to the pre-UI-lang hard-pin.

    This is the OUTPUT side only (model → human). The INPUT side (human →
    model) stays hard-pinned to English in ``lib/chat/turn_builder`` — English
    is the model's strongest language — and is intentionally NOT resolved here.
    """
    for src in sources:
        if not src:
            continue
        code = src.get('uiLang')
        if code:
            name = _UILANG_TO_TARGET.get(str(code).strip().lower())
            if name:
                return name
    return TRANSLATE_TARGET_DEFAULT


def target_lang_code(target_name: str) -> str:
    """Map a translate target language NAME to its detector code.

    Used by the already-in-target-language skip gate to compare
    ``detect_language(content).code`` against the target. Unknown names fall
    back to ``'zh'`` (the historical hard-pin), so a mis-mapped target can
    never make the gate stop skipping already-target-language content.
    """
    return _TARGET_TO_CODE.get(target_name, 'zh')


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
