"""tests/test_orchestrator_pending_swarm_seam.py — the pending-swarm
force-enable guard, exercised through the REAL orchestrator code path.

Rather than stand up a full ``run_task`` (DB + LLM), this extracts the guard
block VERBATIM from ``lib/tasks_pkg/orchestrator.py`` source and executes it in
a namespace mirroring the orchestrator's locals at that point, then asserts the
resulting ``task['_tool_schema']`` (the source of truth for
``_known_tool_names`` / the hallucination gate).

This makes the test byte-couple to the actual guard: if the guard is reverted
(deleted from orchestrator.py), the extraction finds nothing to run and the
schema stays swarm-tool-free → the assertion FAILS. That is the NC bite the
task requires.
"""

from __future__ import annotations

import os
import re

import pytest

from lib.tasks_pkg.tool_dispatch import _known_tool_names

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
# The orchestrator was split into a facade-preserving package
# (``lib/tasks_pkg/orchestrator/``).  ``run_task`` — and the pending-swarm
# guard block this test byte-couples to — moved to ``_run.py``, then to
# ``_tool_assembly_prep.py`` (pt_03f4cdf1 slice 29, 2026-07-31).  Fall
# back through the older locations for older checkouts.
ORCH = os.path.join(ROOT, 'lib', 'tasks_pkg', 'orchestrator',
                    '_tool_assembly_prep.py')
if not os.path.exists(ORCH):
    ORCH = os.path.join(ROOT, 'lib', 'tasks_pkg', 'orchestrator', '_run.py')
if not os.path.exists(ORCH):
    ORCH = os.path.join(ROOT, 'lib', 'tasks_pkg', 'orchestrator.py')

_SWARM_NAMES = {'spawn_agents', 'await_agents', 'get_agent_result'}


def _extract_guard_block() -> str:
    """Pull the guard block from the orchestrator source.

    Delimited by the sentinel comment the fix introduced and the
    ``task['_tool_schema'] = tool_list`` assignment that follows it. Returns
    the source text of the ``if not swarm_enabled:`` guard only.
    Indent-agnostic: the block lived at 8-space indent inside run_task
    and now lives at 4-space indent inside
    ``_tool_assembly_prep.assemble_round_tools`` (slice 29) — the regex
    tolerates both, and textwrap.dedent normalises.
    """
    src = open(ORCH, encoding='utf-8').read()
    m = re.search(
        r'( +if not swarm_enabled:\n.*?)\n\n +# Stash the assembled',
        src, re.DOTALL)
    if not m:
        return ''
    import textwrap
    return textwrap.dedent(m.group(1))


def _run_guard(*, swarm_enabled: bool, pending: bool, base_tools):
    """Execute the extracted guard block with orchestrator-shaped locals.

    Monkeypatches ``lib.swarm.integration.has_live_or_pending_swarm`` via the
    injected ``task`` so the guard's ``_has_pending_swarm(task)`` returns
    ``pending`` deterministically (no live session / inbox needed).
    """
    block = _extract_guard_block()
    if not block:
        # Guard reverted/absent → simulate the orchestrator WITHOUT it: the
        # schema is just the assembled base list. This is the NC state.
        return list(base_tools) if base_tools else None

    import lib.swarm.integration as integ
    orig = integ.has_live_or_pending_swarm
    integ.has_live_or_pending_swarm = lambda task: pending
    try:
        ns = {
            'swarm_enabled': swarm_enabled,
            'tool_list': list(base_tools) if base_tools is not None else None,
            'has_real_tools': bool(base_tools),
            'max_tool_rounds': (999_999_999 if base_tools else 0),
            'task': {'id': 'deadbeef' * 5, 'convId': 'convX'},
            'logger': __import__('lib.log', fromlist=['get_logger']).get_logger('test'),
        }
        exec(block, {}, ns)  # noqa: S102 — running our own source, test-only
        return ns['tool_list']
    finally:
        integ.has_live_or_pending_swarm = orig


def _dummy(name):
    return {'type': 'function', 'function': {'name': name, 'parameters': {}}}


def _schema_known(tool_list):
    """Mirror _known_tool_names over a stamped _tool_schema."""
    return _known_tool_names({'_tool_schema': tool_list or []})


def test_guard_block_present_in_source():
    """The fix must be present as a recognisable, revertible block."""
    block = _extract_guard_block()
    assert block, 'pending-swarm guard block not found in orchestrator.py'
    assert 'has_live_or_pending_swarm' in block
    assert 'resolve_turn_swarm_tools' in block


def test_pending_swarm_turn_gets_swarm_tools_in_schema():
    """swarmEnabled False + pending swarm → schema carries the follow-up tools,
    so get_agent_result / await_agents are KNOWN (not hallucinated)."""
    tool_list = _run_guard(swarm_enabled=False, pending=True,
                           base_tools=[_dummy('read_files')])
    known = _schema_known(tool_list)
    assert _SWARM_NAMES <= known, \
        f'pending-swarm turn must expose swarm tools; got {sorted(known)}'


def test_no_pending_swarm_leaves_schema_alone():
    tool_list = _run_guard(swarm_enabled=False, pending=False,
                           base_tools=[_dummy('read_files')])
    known = _schema_known(tool_list)
    assert not (_SWARM_NAMES & known), \
        'no live/pending swarm → swarm tools must NOT be forced in'


def test_swarm_enabled_turn_unchanged_by_guard():
    """When swarmEnabled is True the guard is skipped (assembly already added
    the tools upstream — here base has them). Guard must not touch it."""
    base = [_dummy('read_files')] + [_dummy(n) for n in _SWARM_NAMES]
    tool_list = _run_guard(swarm_enabled=True, pending=True, base_tools=base)
    known = _schema_known(tool_list)
    assert _SWARM_NAMES <= known


def test_bare_turn_pending_swarm_lifts_round_cap():
    """A turn with NO other tools (max_tool_rounds=0) still gets a usable
    schema — the guard lifts the cap so the forced tools aren't dead."""
    tool_list = _run_guard(swarm_enabled=False, pending=True, base_tools=None)
    known = _schema_known(tool_list)
    assert _SWARM_NAMES <= known
