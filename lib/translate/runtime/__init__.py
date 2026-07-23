"""Async translation TaskRuntime + worker (facade package).

The TaskRuntime owns the task registry, locking, push channel ('translate'),
TTL cleanup, and ``audit_log``-style error reporting. ``_do_translate`` is
the actual worker thread invoked via ``_translate_runtime.spawn(...)``.

Compatibility shims ``_translate_tasks`` / ``_translate_tasks_lock`` exist
because callers in lib.tasks_pkg.manager and tests import them by name.
New code should use the runtime directly.

This file is a pure re-export facade — the implementation lives in the
submodules (``._state`` / ``._segments`` / ``._worker``). The import path
``lib.translate.runtime`` is UNCHANGED, so ``from lib.translate.runtime
import X`` works byte-identically for every previously-public symbol.
``_translate_runtime`` / ``_translate_tasks`` / ``_translate_tasks_lock``
have a SINGLE home in ``._state`` — the aliases below are the same objects.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ── Shared state (single home — the TaskRuntime singleton + its aliases) ──
from ._state import (  # noqa: E402,F401
    _translate_runtime,
    _translate_tasks,
    _translate_tasks_lock,
    _cleanup_translate_tasks,
)

# ── Segment-level translation map builders (retro + backfill shared core) ──
from ._segments import (  # noqa: E402,F401
    _read_message_segments,
    _build_segment_translation_map,
    _translate_segments_to_map,
)

# ── The background worker thread ──
from ._worker import _do_translate  # noqa: E402,F401

# The pre-split single module had ``_translate_freetext`` as a module global
# (imported from ..engine), and callers/tests monkeypatch
# ``lib.translate.runtime._translate_freetext`` to stub the LLM call. Re-export
# it here — ``._segments`` / ``._worker`` resolve it dynamically off this
# facade module at call time, so patching it takes effect byte-identically.
from ..engine import _translate_freetext  # noqa: E402,F401

__all__ = [
    # shared state
    '_translate_runtime',
    '_translate_tasks',
    '_translate_tasks_lock',
    '_cleanup_translate_tasks',
    # segments
    '_read_message_segments',
    '_build_segment_translation_map',
    '_translate_segments_to_map',
    # worker
    '_do_translate',
    # engine passthrough (monkeypatch target)
    '_translate_freetext',
]
