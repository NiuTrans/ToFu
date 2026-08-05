#!/usr/bin/env python3
"""Source-invariant guard: the inbox/peer/steer SSE inject handlers are
null-safe ONLY because ``connectToTask`` establishes a non-null ``assistantMsg``
before any event dispatches. This test PINS that coupling.

WHY (board epic pt_909102262244497a)
------------------------------------
A reviewer flagged that ``_handleSwarmInboxInject`` / ``_handlePeerInboxInject``
/ ``_handleUserSteerInject`` in ``static/js/ui/sse_handlers_lifecycle.js``
dereference ``c.assistantMsg`` UNCONDITIONALLY (``assistantMsg._inboxInjects``
/ ``.toolRounds`` / ``._userSteerInjects``) and worried about a null during a
"Phase-2 reconcile race". Static analysis was DECISIVE that no null path exists:

  ``connectToTask`` (static/js/ui/sse_pipeline.js) runs, BEFORE wiring the
  dispatcher, an UNCONDITIONAL guard:

      if (!assistantMsg || assistantMsg.role !== "assistant") {
          ... push a fresh assistant message ...
      }

  whose own comment names the exact cited scenario ("loadConversationMessages
  Phase 2 overwrote conv.messages during a race"). Every subsequent dispatcher
  reassignment of ``assistantMsg`` is guarded to a truthy value, so ``_hctx()``
  can never hand a handler a null ``assistantMsg`` in production.

Adding ``if(!assistantMsg)return`` to the three handlers would be the
speculative defensive code the epic explicitly forbids ("needs RUNTIME REPRO
before adding a guard"). Instead we make the false-positive close DURABLE:
lock the invariant that keeps the handlers safe, so a future edit that removes
the ``connectToTask`` guard (re-opening a real null path) trips HERE.

Pure source-text assertions — no node/jsdom needed; tracks the shipped files.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_PIPELINE = os.path.join(ROOT, 'static', 'js', 'ui', 'sse_pipeline.js')
_HANDLERS = os.path.join(ROOT, 'static', 'js', 'ui', 'sse_handlers_lifecycle.js')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def test_connecttask_guarantees_non_null_assistantmsg_before_dispatch():
    """The load-bearing invariant: connectToTask must retain the unconditional
    non-null/assistant guard that pushes a fresh assistant message. This is the
    ONLY reason the inject handlers can deref assistantMsg without a guard —
    if it's removed, a real null path (the reviewer's Phase-2 race) re-opens."""
    src = _read(_PIPELINE)
    guard = 'if (!assistantMsg || assistantMsg.role !== "assistant") {'
    assert guard in src, (
        'connectToTask lost its non-null assistantMsg guard — the inbox/peer/'
        'steer inject handlers deref assistantMsg unconditionally and would now '
        'NPE on the Phase-2 reconcile race. Restore the guard, or add explicit '
        'null guards to the three handlers (see board epic pt_909102262244497a).')
    # The guard must actually push a fresh assistant message (not merely log),
    # else "non-null before dispatch" is not truly guaranteed. Scope the pin
    # to the guard's OWN BLOCK (its 2-space closing brace — inner blocks all
    # close at 4+), NOT a fixed char window: a 1200-char window broke the
    # first time the block grew (the _ensureMsgId hardening pushed
    # conv.messages.push past the cutoff while the invariant stood intact).
    idx = src.index(guard)
    block = src[idx:src.index('\n  }\n', idx)]
    assert 'role: "assistant"' in block and 'conv.messages.push(assistantMsg)' in block, (
        'the guard no longer establishes a fresh assistant message — the '
        'non-null-before-dispatch guarantee is broken')


def test_inject_handlers_still_deref_assistantmsg_unconditionally():
    """Documents the coupling this invariant protects: the three handlers DO
    deref assistantMsg without their own null guard (by design — the dispatcher
    guarantees non-null). If someone later adds a guard here, this test is the
    breadcrumb explaining why it was unnecessary; if the deref pattern changes,
    re-evaluate the coupling against the connectToTask guarantee above."""
    src = _read(_HANDLERS)
    # Each handler binds `const assistantMsg = c.assistantMsg` then derefs it.
    assert src.count('const assistantMsg = c.assistantMsg') >= 3, (
        'expected the 3 inject handlers to each bind assistantMsg from ctx')
    # The unconditional derefs the reviewer flagged (proves the coupling is real).
    assert 'assistantMsg._inboxInjects' in src
    assert 'assistantMsg._userSteerInjects' in src
    # None of the three handlers should have grown a speculative early-return
    # null guard (the epic forbids it without a runtime repro). If this ever
    # trips, a guard WAS added — update the epic/board note accordingly.
    assert not re.search(r'if\s*\(\s*!assistantMsg\s*\)\s*return', src), (
        'a speculative `if(!assistantMsg)return` guard was added to a lifecycle '
        'handler — the epic requires a RUNTIME REPRO first; either land the '
        'repro test alongside it or revert (board epic pt_909102262244497a)')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
