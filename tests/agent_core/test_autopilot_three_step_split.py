#!/usr/bin/env python3
"""tests/agent_core/test_autopilot_three_step_split.py — retirement pin.

The historical three-step split (register VU carrier → parent ``done`` → VU
LLM decision) ran a second standalone interpreter on the finalize thread.
That interpreter is retired: finalization must never register or run it, and
old kickoff carriers settle without dispatch (see
``tests/tasks_pkg/test_legacy_autopilot_retirement.py``). This pin locks the source
ordering in ``_finalize_and_emit_done``: no ``register_autopilot_turn`` /
``maybe_run_autopilot`` call sites may reappear.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
FINALIZE_PATH = os.path.join(ROOT, 'lib', 'tasks_pkg', 'orchestrator', '_finalize.py')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


# ── Source-order contract ────────────────────────────────────────────

def test_finalize_does_not_dispatch_retired_carriers():
    """Current finalization never registers or runs a second interpreter."""
    src = _read(FINALIZE_PATH)
    assert 'register_autopilot_turn(task)' not in src
    assert 'maybe_run_autopilot(task)' not in src
    assert 'append_event(task, done_evt)' in src


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
