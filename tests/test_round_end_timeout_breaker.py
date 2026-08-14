#!/usr/bin/env python3
"""Guard: the tool-timeout FORCE STOP exit pairs its ROUND_START with a
ROUND_END (epic pt_a1895646a571439d).

WHY
---
run_task emits ROUND_START at the top of EVERY round the model runs
(RENDER_CONTRACT Phase 3). Every exit path closed that boundary — budget
(x2), aborted-before-tools, natural tools end — EXCEPT the consecutive-
tool-timeout FORCE STOP, whose ``break`` skipped the emit, stranding the
round's START unpaired (the reducer's ``_currentRound`` would dangle for
any consumer that reconciles boundaries).

The fix emits ``ROUND_END reason='tool_timeout'`` right before the break.
That new reason value is wire-safe BECAUSE the frontend reducer's
``round_end`` case never reads ``ev.reason`` (it only clears
``_currentRound``) — reason is informational only. These four pins lock
both halves of that contract:

  1. The breaker branch emits ROUND_END reason='tool_timeout' BEFORE its
     break (failing-first: RED on the pre-fix source).
  2. Every ROUND_END emit site in _run.py uses a SANCTIONED reason — the
     closed enumeration, kept in sync with the events.py registry.
  3. The events.py ROUND_END registry entry documents 'tool_timeout' (the
     machine-discoverable contract, /api/v1/capabilities).
  4. TOLERANCE pin: stream_reducer.js's round_end case body does NOT
     branch on ``reason`` — if a future edit makes the reducer
     reason-sensitive, this pin forces a deliberate re-audit of the
     sanctioned-reason list in lockstep.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The ROUND_END sites moved out of _run.py into the orchestrator package
# (5+ sites, incl. _tool_timeout_breaker.py) — scan the whole package.
_ORCH_PKG = os.path.join(_ROOT, 'lib/tasks_pkg/orchestrator')
_BREAKER_PY = os.path.join(_ORCH_PKG, '_tool_timeout_breaker.py')
_EVENTS_PY = os.path.join(_ROOT, 'lib/agent_core/events.py')
from tests._runtime_sections import runtime_section_path

_REDUCER_JS = runtime_section_path('ui/stream_reducer.js')

# Closed enumeration of sanctioned ROUND_END reasons (registry-synced).
_SANCTIONED_REASONS = {'tools', 'final', 'aborted', 'budget', 'error',
                       'tool_timeout'}


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


@_unit
def test_breaker_branch_emits_round_end_before_break():
    """Pin 1 (failing-first): the FORCE STOP branch must append_event a
    ROUND_END reason='tool_timeout' before its break."""
    src = _read(_BREAKER_PY)
    i = src.index('FORCE STOP')
    # The breaker branch spans from FORCE STOP to its `return True`.
    branch = src[i:i + 2200]
    j = branch.index('return True')
    segment = branch[:j]
    assert "EventType.ROUND_END" in segment, (
        'FORCE STOP branch must emit ROUND_END before returning — the '
        'ROUND_START at this round\'s top is otherwise unpaired')
    assert "reason='tool_timeout'" in segment, (
        "breaker ROUND_END must carry reason='tool_timeout'")


@_unit
def test_all_round_end_sites_use_sanctioned_reasons():
    """Pin 2: every ROUND_END emit site in the orchestrator package uses a
    reason from the closed sanctioned set (no typo'd / undocumented
    reasons)."""
    src = '\n'.join(_read(os.path.join(_ORCH_PKG, name))
                    for name in sorted(os.listdir(_ORCH_PKG))
                    if name.endswith('.py'))
    hits = re.findall(
        r"EventType\.ROUND_END,\s*\n?\s*roundNum=round_num,\s*reason='([^']+)'",
        src)
    reasons = set(hits)
    assert len(hits) >= 5, (
        f'expected >=5 ROUND_END sites (budget x2/aborted/tools/'
        f'tool_timeout), found {sorted(reasons)} ({len(hits)} sites) — '
        'the scan may be blind')
    bad = reasons - _SANCTIONED_REASONS
    assert not bad, (
        f'unsanctioned ROUND_END reasons {sorted(bad)} — register them in '
        'lib/agent_core/events.py and add to _SANCTIONED_REASONS here')
    assert 'tool_timeout' in reasons, 'breaker site must emit tool_timeout'


@_unit
def test_events_registry_documents_tool_timeout_reason():
    """Pin 3: the machine-discoverable contract (events.py ROUND_END spec)
    documents the new reason."""
    src = _read(_EVENTS_PY)
    i = src.index('EventSpec(EventType.ROUND_END')
    spec = src[i:i + 1200]
    assert 'tool_timeout' in spec, (
        "events.py ROUND_END spec must list 'tool_timeout' in its reason "
        'enumeration — /api/v1/capabilities consumers read this')


@_unit
def test_reducer_round_end_is_reason_agnostic():
    """Pin 4 (tolerance contract): the frontend reducer's round_end case
    must NOT branch on ev.reason — the wire may add new reasons freely as
    long as closing stays unconditional."""
    src = _read(_REDUCER_JS)
    m = re.search(r"case 'round_end':\s*\{(.*?)\n    \}", src, re.S)
    assert m, 'stream_reducer.js round_end case not found — file drifted?'
    body = m.group(1)
    assert 'reason' not in body, (
        'reducer round_end case now READS ev.reason — new wire reasons are '
        'no longer automatically tolerated; re-audit _SANCTIONED_REASONS '
        'and the frontend close logic in lockstep')
    assert '_currentRound' in body, (
        'round_end case must keep clearing _currentRound (the close)')


if __name__ == '__main__':
    for fn in [
        test_breaker_branch_emits_round_end_before_break,
        test_all_round_end_sites_use_sanctioned_reasons,
        test_events_registry_documents_tool_timeout_reason,
        test_reducer_round_end_is_reason_agnostic,
    ]:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
