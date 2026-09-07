#!/usr/bin/env python3
"""Wire-parity for pt_03f4cdf1 slice 11 — per-round swarm/peer/steer
inbox drain.

Scope: run_task's per-round "★ Drain swarm inbox …" block (~177 lines,
one try/except at ~L585 in _run.py just before ``_tools_this_round``
resolution). The block:

  1. Refuses to drain when the previous message is an unmatched assistant
     tool_call (the tool_call ↔ tool_result pair must close before
     another role can speak).
  2. Drains the swarm-scoped inbox (``swarm_key_for(task)``) into three
     lanes with lane-specific mode filters — swarm items excluding
     peer-msg/user-steer, peer items ONLY when the driver has not claimed
     peer delivery via ``_peer_driver_owned`` and under the possibly-
     different ``_peer_drain_key``, and user-steer items on the swarm
     key.
  3. Coalesces every drained payload into ONE user-role message
     (``\\n\\n``-joined).
  4. For swarm items: persists ``mark_delivered`` for restart safety,
     emits SWARM_INBOX_INJECT with previews, and accumulates the
     display-only sidecar ``task['_inboxInjects']``.
  5. For peer items: stashes ``task['_peer_inject_pending']`` for the
     DEFERRED post-LLM confirm-then-emit-chip flush (never-zero
     delivery).
  6. For steer items: stashes ``task['_steer_inject_pending']`` for the
     same deferred-confirm flush.
  7. Never raises — a drain failure logs an error and the task continues
     without notifications.

Extract to
``lib/tasks_pkg/orchestrator/_swarm_inbox.py::drain_and_inject_inbox``.

Contract:

  drain_and_inject_inbox(
      *, task, messages, round_num, tid,
  ) -> None

  Mutates ``task`` (sidecars: _inboxInjects, _peer_inject_pending,
  _steer_inject_pending; events via append_event) and ``messages``
  (appends ONE coalesced user message). Never raises.

This focused file pins the public extraction seam (module, callable and
explicit arguments). Actual root-loop delegation and call order are exercised
through monkeypatched behavior in ``test_root_orchestrator_agent_loop_adapter``;
the inbox delivery semantics are covered by the inbox/steer roundtrip suites.
Keeping those assertions behavioral avoids a second source-text mirror of the
root loop that would go stale on harmless refactors.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_swarm_inbox_module_exists_and_exposes_helper():
    """Slice 11: lib.tasks_pkg.orchestrator._swarm_inbox exists and
    exposes drain_and_inject_inbox as a callable."""
    import importlib
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._swarm_inbox')
    assert hasattr(mod, 'drain_and_inject_inbox'), (
        'lib.tasks_pkg.orchestrator._swarm_inbox missing '
        'drain_and_inject_inbox')
    assert callable(mod.drain_and_inject_inbox)


def test_drain_and_inject_inbox_signature_matches_seam():
    """Slice 11: the helper's signature accepts every run_task local
    crossing the seam. Enumerated so a future edit that swaps to a
    global-reading variant flips this test."""
    import importlib
    import inspect
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._swarm_inbox')
    sig = inspect.signature(mod.drain_and_inject_inbox)
    params = set(sig.parameters.keys())
    required = {'task', 'messages', 'round_num', 'tid'}
    missing = required - params
    assert not missing, (
        f'drain_and_inject_inbox missing required parameters: '
        f'{sorted(missing)}. All run_task-side locals crossing the seam '
        f'MUST be explicit args.'
    )
