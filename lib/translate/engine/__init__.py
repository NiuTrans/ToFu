"""lib.translate.engine — single-chunk translation engine (facade).

Decomposed from the original 634-line ``lib/translate/engine.py`` into focused,
acyclic submodules. This package facade re-exports every symbol so
``from lib.translate.engine import X`` keeps working byte-identically —
including the private ``_translate_freetext`` that ``lib/translate/incremental.py``
imports directly.

Submodules
----------
* ``_split``  — sentence-completeness primitives (``_SENTENCE_END_CHARS`` +
  ``_ends_midsentence``) the truncation detector relies on. Pure, no I/O.
* ``_engine`` — the engine proper: the cache/MT/LLM routing, the aggressive
  retry loop with truncation / no-op / wrong-language detection, plus the
  ``_translate_freetext`` whole-document wrapper and the ``_build_trace``
  provenance helper.

Import direction is acyclic: ``_engine`` depends on ``_split``; nothing
depends on ``_engine`` within the package.
"""

# Re-exported so ``engine.translate_cache`` resolves exactly as it did when
# the module did ``from lib import translate_cache`` at top level (tests
# monkeypatch ``engine.translate_cache.get`` / ``.put``).
from lib import translate_cache  # noqa: F401
# Same parity for the refusal-marker store (tests monkeypatch
# ``engine.translate_refusal.get`` / ``.put``).
from lib import translate_refusal  # noqa: F401
from lib.log import get_logger

# Sentence-completeness helpers (pure primitives)
from ._split import (
    _SENTENCE_END_CHARS,
    _ends_midsentence,
)

# Engine proper: routing + retry loop + entrypoints
from ._engine import (
    TranslationContentRefused,
    _build_trace,
    _translate_freetext,
    _translate_one_chunk,
)

logger = get_logger(__name__)

__all__ = [
    # public / consumed entrypoints
    '_translate_one_chunk',   # routes/translate.py, lib.translate facade
    '_translate_freetext',    # lib/translate/incremental.py:171 (PRIVATE consumer)
    # sentence-completeness primitives
    '_SENTENCE_END_CHARS',
    '_ends_midsentence',
    # provenance helper
    '_build_trace',
    # typed content-guard refusal (REST surface maps it to 502 + envelope)
    'TranslationContentRefused',
    # module ref re-exported for parity with the original top-level import
    'translate_cache',
    'translate_refusal',
]
