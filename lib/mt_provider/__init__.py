"""lib/mt_provider — Machine Translation provider adapters.

Provides a unified interface for dedicated machine translation APIs
(NiuTrans, etc.) that are faster and cheaper than using LLMs for translation.

When a provider is configured in Settings → 通用 → 机器翻译, translation
routes use the MT API directly instead of the LLM cheap model.
No LLM prompt is needed — the MT API handles translation directly.

If nothing is configured, the LLM-based translation path is used as before.

Decomposed from the original 537-line ``lib/mt_provider.py`` into focused,
acyclic submodules. This package facade re-exports every public symbol so
``from lib.mt_provider import X`` keeps working byte-identically — including
the privates (``_niutrans_v1``, ``_niutrans_v2``, ``_normalize_lang``) that
``routes/api_v1/translate.py`` imports.

Usage:
    from lib.mt_provider import mt_translate_chunked, is_mt_configured

    if is_mt_configured():
        translated = mt_translate_chunked(text, source='en', target='zh')
"""

from lib.log import get_logger

# Config + language mapping (constants + config readers)
from ._config import (
    _LANG_MAP,
    _NIUTRANS_MAX_CHARS,
    _REQUEST_TIMEOUT,
    _normalize_lang,
    _get_mt_config,
    is_mt_configured,
)

# Markdown / code-block preservation (pure functions + regex constants)
from ._markdown import (
    _CODE_BLOCK_RE,
    _INLINE_CODE_RE,
    _extract_code_blocks,
    _restore_code_blocks,
    _MD_PREFIX_RE,
    _MD_STRUCTURAL_LINE_RE,
    _extract_md_structure,
    _restore_md_structure,
)

# NiuTrans v1/v2 API adapters
from ._niutrans import (
    _niutrans_translate,
    _niutrans_v1,
    _niutrans_v2,
)

# Public translate entrypoints
from ._translate import (
    mt_translate,
    mt_translate_chunked,
)

logger = get_logger(__name__)

__all__ = [
    # public entrypoints
    'mt_translate', 'mt_translate_chunked', 'is_mt_configured',
    # config
    '_get_mt_config', '_normalize_lang',
    '_LANG_MAP', '_NIUTRANS_MAX_CHARS', '_REQUEST_TIMEOUT',
    # markdown / code-block preservation
    '_CODE_BLOCK_RE', '_INLINE_CODE_RE',
    '_extract_code_blocks', '_restore_code_blocks',
    '_MD_PREFIX_RE', '_MD_STRUCTURAL_LINE_RE',
    '_extract_md_structure', '_restore_md_structure',
    # niutrans adapters (privates consumed by routes/api_v1/translate.py)
    '_niutrans_translate', '_niutrans_v1', '_niutrans_v2',
]
