"""Translation entry points and compatibility exports.

General text and paper translation share this engine. The whole-document
``_translate_freetext`` wrapper keeps one provider call per attempt; paper
source slicing and artifact publication belong to ``lib.paper``.

Submodules
----------
* ``_split``  — sentence-completeness primitives (``_SENTENCE_END_CHARS`` +
  ``_ends_midsentence``) the truncation detector relies on. Pure, no I/O.
* ``_quality_checks`` — pure normalization primitives and ordered content
  verdicts; no dispatch, cache mutation or retry state.
* ``_candidate`` — normalize one provider result, assess it, record quality
  feedback and apply the typed refusal policy. Returns explicit retry state.
* ``_engine`` — cache/identity/MT routing, physical dispatch, deadlines,
  cancellation, retry budgets, accepted-result commit and provenance.

Import direction is acyclic: ``_engine`` → ``_candidate`` → ``_quality_checks``
→ ``_split``. The engine passes its over-generation predicate explicitly to
retain the existing threshold fault-injection seam.
"""

# Re-exported so ``engine.translate_cache`` resolves exactly as it did when
# the module did ``from lib import translate_cache`` at top level (tests
# monkeypatch ``engine.translate_cache.get`` / ``.put``).
from lib import translate_cache  # noqa: F401
# Same parity for the refusal-marker store (tests monkeypatch
# ``engine.translate_refusal.get`` / ``.put``).
from lib import translate_refusal  # noqa: F401
from lib.log import get_logger
from lib.translate.errors import TranslationContentRefused

# Sentence-completeness helpers (pure primitives)
from ._split import (
    _SENTENCE_END_CHARS,
    _ends_midsentence,
)

# Engine proper: routing + retry loop + entrypoints
from ._engine import (
    _build_trace,
    _translate_freetext,
    _translate_one_chunk,
    translation_attempt_timeout_seconds,
    translation_first_byte_timeout_seconds,
    translation_total_deadline_seconds,
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
    # size-derived wall budget shared with the async worker
    'translation_total_deadline_seconds',
    # per-attempt bound handed to the transport (must stay under the total)
    'translation_attempt_timeout_seconds',
    'translation_first_byte_timeout_seconds',
    # typed content-guard refusal (REST surface maps it to 502 + envelope)
    'TranslationContentRefused',
    # module ref re-exported for parity with the original top-level import
    'translate_cache',
    'translate_refusal',
]
