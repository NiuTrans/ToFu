"""Server-side auto-translate safety net (assistant + endpoint-critic messages).

Extracted from ``lib/tasks_pkg/manager.py`` (2026-06-24). This is the
server-side guarantee that an assistant reply (or endpoint-mode critic review)
gets translated even when the frontend is offline / switched away / the SSE
stream closed early. It honours the per-conversation ``autoTranslate`` setting,
dedups against an already-running frontend translate task, detects + re-does
stale partial translations, short-circuits already-Chinese content, and hands
off to the incremental per-round translator when one is active.

Called from ``manager._sync_result_to_conversation`` (single-turn safety net)
and ``endpoint._trigger_*_auto_translate`` (per-turn + final). ``manager`` and
``endpoint`` import these back, so call sites are unchanged. Dependency is
one-directional: this module imports DB helpers from ``lib.database`` and the
translate engine lazily from ``lib.translate``/``lib.text_lang`` — never
``manager``.

────────────────────────────────────────────────────────────────────────
This module is a FACADE PACKAGE. The implementation was split out of the
former single-file ``lib/tasks_pkg/auto_translate.py`` into cohesive
sub-modules, but the import path is UNCHANGED — every
``from lib.tasks_pkg.auto_translate import X`` call site keeps working
byte-identically, including monkeypatches that target
``lib.tasks_pkg.auto_translate._maybe_auto_translate_assistant``.

Sub-modules:
  * ``._assistant`` — the core ``_maybe_auto_translate_assistant`` safety net.
  * ``._critic``    — ``_maybe_auto_translate_critic`` (delegates to assistant).
"""

# ``threading`` is re-exported at the facade level because the phase-2 tests
# monkeypatch ``at.threading.Thread`` (a mutation on the shared threading module
# object). ``_assistant`` does its own ``import threading`` of the SAME module
# singleton, so the patch is observed there — but the attribute must exist on
# this facade for ``monkeypatch.setattr(at.threading, ...)`` to resolve.
import threading  # noqa: F401

from lib.log import get_logger

logger = get_logger(__name__)

from lib.tasks_pkg.auto_translate._assistant import (  # noqa: E402,F401
    _maybe_auto_translate_assistant,
)
from lib.tasks_pkg.auto_translate._critic import (  # noqa: E402,F401
    _maybe_auto_translate_critic,
)

__all__ = [
    '_maybe_auto_translate_assistant',
    '_maybe_auto_translate_critic',
]
