#!/usr/bin/env python3
# Incident anchor: born in commit 433e836a — refactor(orchestrator): pt_03f4cdf1 slice 11 — extract per-round swar...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
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

Failing-first — this test asserts (RED before extraction, GREEN after):
  1. Module ``lib.tasks_pkg.orchestrator._swarm_inbox`` exists and
     exports ``drain_and_inject_inbox`` as a callable.
  2. ``_run.py`` imports the helper AND calls it.
  3. The inline body pivots (the unmatched-tool_call guard, the
     ``_swarm_key_for``/``_drain_inbox`` calls, the ``mark_delivered``
     persist, the SWARM_INBOX_INJECT build, the two sidecar stashes)
     are all GONE from ``_run.py``.
  4. Helper signature accepts the run_task locals crossing the seam.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart
sys.modules.setdefault('flask', _quart)

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@_unit
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


@_unit
def test_run_task_delegates_to_drain_and_inject_inbox():
    """Slice 11: _run.py must import the helper and call it inline in
    run_task's body."""
    with open(os.path.join(_ROOT, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    assert ('from lib.tasks_pkg.orchestrator._swarm_inbox import'
            in src), (
        '_run.py must import from _swarm_inbox after slice 11')
    import re as _re
    assert _re.search(r'\bdrain_and_inject_inbox\s*\(', src), (
        '_run.py must CALL drain_and_inject_inbox(...) — a bare '
        'reference in a comment does not satisfy slice 11')


@_unit
def test_inbox_drain_inline_body_removed_from_run_py():
    """Slice 11: the inline BODY of the swarm-inbox drain block (the
    pivotal drain / persist / event / sidecar-stash calls) MUST be gone
    from _run.py. A silent revert would put every pivot back inline.

    The section-header comment MAY stay as a call-site landmark.
    """
    with open(os.path.join(_ROOT, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    for pivot in (
        # inline imports that only appear inside this block
        'from lib.agent_inbox import drain as _drain_inbox',
        'from lib.swarm.integration import swarm_key_for as _swarm_key_for',
        'from lib.swarm import persistence as _swarm_persist',
        # the three drain lane calls with their distinct signatures
        "_swarm_key, exclude_modes=['peer-msg', 'user-steer']",
        "_drain_inbox(_peer_key, modes=['peer-msg'])",
        "modes=['user-steer']",
        # persist + event + sidecar pivots
        '_swarm_persist.mark_delivered(',
        'EventType.SWARM_INBOX_INJECT',
        "'_inboxInjects', [])",
        "'_peer_inject_pending', [])",
        "'_steer_inject_pending', [])",
    ):
        assert pivot not in src, (
            f'_run.py must NOT re-carry inline swarm-inbox drain pivot '
            f'{pivot!r} — extracted to _swarm_inbox.py'
        )


@_unit
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


if __name__ == '__main__':
    for fn in [
        test_swarm_inbox_module_exists_and_exposes_helper,
        test_run_task_delegates_to_drain_and_inject_inbox,
        test_inbox_drain_inline_body_removed_from_run_py,
        test_drain_and_inject_inbox_signature_matches_seam,
    ]:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
