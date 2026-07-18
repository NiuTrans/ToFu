"""LLM call with automatic fallback to Opus on failure.

Extracted from ``orchestrator.py`` to keep the main orchestration loop
focused on control flow.  The single public entry-point is
:func:`_llm_call_with_fallback`, which streams one LLM round and
transparently retries with Claude Opus 4 (medium preset) when the
primary model errors out.

This package is a FACADE: it re-exports every public/private symbol that
previously lived in the flat ``lib.tasks_pkg.llm_fallback`` module, so all
existing ``from lib.tasks_pkg.llm_fallback import X`` imports keep working
byte-identically. Implementations live in the sibling sub-modules:

    ._state   - shared reactive-compaction retry state + cleanup
    ._retry   - _get_fallback_model / _flag_empty_stop_for_retry
    ._usage   - _emit_round_usage
    ._call    - _llm_call_with_fallback (core entrypoint)

CRITICAL: ``_reactive_compact_attempts`` is defined ONCE in ``._state`` and
re-exported here by reference. ``cleanup_reactive_compact_state`` mutates
that SAME dict, so a caller holding either reference sees the mutation.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# Collaborators re-exported for backwards-compatible monkeypatching.
# The original flat module imported these at module level, so tests could
# patch lib.tasks_pkg.llm_fallback.stream_llm_response / append_event.
# Preserve that by re-exporting the real functions here; ._call resolves
# them through this facade at call time.
from lib.tasks_pkg.manager import append_event, stream_llm_response  # noqa: F401

# Shared reactive-compaction retry state (single object, by reference).
from lib.tasks_pkg.llm_fallback._state import (  # noqa: F401
    _reactive_compact_attempts,
    _REACTIVE_COMPACT_MAX_RETRIES,
    cleanup_reactive_compact_state,
)

# Fallback-model resolution + empty-stop retry flagging.
from lib.tasks_pkg.llm_fallback._retry import (  # noqa: F401
    _get_fallback_model,
    _flag_empty_stop_for_retry,
)

# Per-round usage emission.
from lib.tasks_pkg.llm_fallback._usage import _emit_round_usage  # noqa: F401

# Core entry point.
from lib.tasks_pkg.llm_fallback._call import _llm_call_with_fallback  # noqa: F401

__all__ = [
    '_llm_call_with_fallback',
    'cleanup_reactive_compact_state',
    '_emit_round_usage',
    '_flag_empty_stop_for_retry',
    '_get_fallback_model',
    '_reactive_compact_attempts',
    '_REACTIVE_COMPACT_MAX_RETRIES',
    'append_event',
    'stream_llm_response',
]
