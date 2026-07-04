"""tests/test_swarm_pending_tool_force.py — root fix for the swarm follow-up
tool "非真实工具" (hallucinated-tool) rejection desync (conv mr2ysg473scxv8).

WHY
---
The swarm inbox drain (orchestrator.py ~L1607) is UNGATED: it injects a
``<swarm-update>`` telling the model to call ``await_agents`` /
``get_agent_result`` even on a turn whose ``swarmEnabled`` is False (e.g. a
manual "continue" turn after an interrupted spawn turn). But the per-turn tool
schema — the source of truth for ``_known_tool_names`` / the hallucination
gate — only carries the swarm tools when ``swarm_enabled``. So the model obeyed
the injected instruction and got rejected as a hallucinator, stranding the
completed agent work.

ROOT FIX (pure, injectable): ``resolve_turn_swarm_tools(tool_list,
swarm_enabled, has_pending_or_live)`` — when a swarm is live-or-pending but
``swarmEnabled`` is False, force the three master swarm tools into the turn's
list so they ARE in ``_known`` and the injected instruction can be acted on.

These tests exercise the pure helper directly (no DB / session harness needed)
and cross-check that the forced names are exactly what the hallucination
classifier (``_known_tool_names`` → ``classify_tool_call``) needs.
"""

from __future__ import annotations

import pytest

from lib.swarm.tools import (
    MASTER_CONTROL_TOOLS,
    augment_with_swarm_tools,
    resolve_turn_swarm_tools,
)

pytestmark = pytest.mark.unit

_SWARM_NAMES = {'spawn_agents', 'await_agents', 'get_agent_result'}


def _names(tool_list):
    return {(t.get('function') or {}).get('name') for t in (tool_list or [])}


def _dummy_tool(name):
    return {'type': 'function', 'function': {'name': name, 'parameters': {}}}


# ── augment_with_swarm_tools ────────────────────────────────────────────

def test_augment_adds_all_three_to_empty():
    merged, added = augment_with_swarm_tools([])
    assert set(added) == _SWARM_NAMES
    assert _SWARM_NAMES <= _names(merged)


def test_augment_adds_only_missing():
    base = [_dummy_tool('read_files'), MASTER_CONTROL_TOOLS[0]]  # has spawn_agents
    merged, added = augment_with_swarm_tools(base)
    assert set(added) == {'await_agents', 'get_agent_result'}
    assert _names(merged) == {'read_files'} | _SWARM_NAMES


def test_augment_idempotent_when_all_present():
    base = list(MASTER_CONTROL_TOOLS)
    merged, added = augment_with_swarm_tools(base)
    assert added == []
    assert merged is base  # unchanged object → no prompt-cache churn


def test_augment_preserves_existing_tools():
    base = [_dummy_tool('read_files'), _dummy_tool('grep_search')]
    merged, _ = augment_with_swarm_tools(base)
    assert {'read_files', 'grep_search'} <= _names(merged)


# ── resolve_turn_swarm_tools (the decision the orchestrator makes) ──────

def test_resolve_forces_tools_when_pending_and_disabled():
    """swarmEnabled False + a live-or-pending swarm → force the tools in.

    THIS is the mr2ysg473scxv8 continuation turn: the tools MUST become real
    so the injected <swarm-update> instruction isn't a hallucination trap.
    """
    tool_list = [_dummy_tool('read_files')]
    out, forced = resolve_turn_swarm_tools(
        tool_list, swarm_enabled=False, has_pending_or_live=True)
    assert set(forced) == _SWARM_NAMES
    assert _SWARM_NAMES <= _names(out)


def test_resolve_noop_when_disabled_and_no_pending():
    """No swarm anywhere → leave the schema exactly as assembled."""
    tool_list = [_dummy_tool('read_files')]
    out, forced = resolve_turn_swarm_tools(
        tool_list, swarm_enabled=False, has_pending_or_live=False)
    assert forced == []
    assert out is tool_list


def test_resolve_noop_when_enabled():
    """swarmEnabled True → assembly already added them; don't double-add."""
    tool_list = list(MASTER_CONTROL_TOOLS)
    out, forced = resolve_turn_swarm_tools(
        tool_list, swarm_enabled=True, has_pending_or_live=True)
    assert forced == []
    assert out is tool_list


def test_resolve_handles_none_tool_list():
    """A bare turn (no other tools) still gets the swarm tools forced in."""
    out, forced = resolve_turn_swarm_tools(
        None, swarm_enabled=False, has_pending_or_live=True)
    assert set(forced) == _SWARM_NAMES
    assert _SWARM_NAMES <= _names(out)


# ── The bite: forced tools become "known" → NOT rejected as hallucinated ─

def test_forced_tools_are_recognised_by_hallucination_gate():
    """End-to-end contract with the real classifier.

    Simulate the orchestrator's ``_known_tool_names`` result (the names in the
    turn's ``_tool_schema``) BEFORE vs AFTER the force. Before: get_agent_result
    is classified as a hallucination. After: it is a real tool (no rejection).
    """
    from lib.tool_input_repair import classify_tool_call

    # BEFORE the fix — schema lacks swarm tools (the buggy continuation turn).
    known_before = _names([_dummy_tool('read_files'), _dummy_tool('grep_search')])
    assert classify_tool_call('get_agent_result', known_before) is not None, \
        'precondition: without the fix get_agent_result IS flagged hallucinated'
    assert classify_tool_call('await_agents', known_before) is not None

    # AFTER the fix — force the swarm tools in, rebuild the known-set.
    forced_list, _ = resolve_turn_swarm_tools(
        [_dummy_tool('read_files'), _dummy_tool('grep_search')],
        swarm_enabled=False, has_pending_or_live=True)
    known_after = _names(forced_list)
    assert classify_tool_call('get_agent_result', known_after) is None, \
        'after the fix get_agent_result must be a REAL tool (not hallucinated)'
    assert classify_tool_call('await_agents', known_after) is None
    assert classify_tool_call('spawn_agents', known_after) is None


# ── NEGATIVE CONTROL (byte-revertible) ──────────────────────────────────
# If the guard's ``has_pending_or_live`` were dropped (i.e. we NEVER force the
# tools), this asserts the observable regression: get_agent_result stays
# hallucinated. Reverting the orchestrator guard reproduces exactly this state
# (the tool omitted from the schema), so this test documents the failure the
# fix prevents. It PASSES on the FIXED tree because the force path exists; it is
# the discriminator the NC-bite reverts.

def test_nc_without_force_get_agent_result_would_be_rejected():
    """NC anchor: with force DISABLED (has_pending_or_live=False) the tool is
    absent → classified as a hallucination. This is the pre-fix behaviour the
    orchestrator guard eliminates for pending-swarm turns."""
    from lib.tool_input_repair import classify_tool_call

    out, forced = resolve_turn_swarm_tools(
        [_dummy_tool('read_files')],
        swarm_enabled=False, has_pending_or_live=False)  # force OFF
    assert forced == []
    known = _names(out)
    # The very rejection the user saw in mr2ysg473scxv8:
    assert classify_tool_call('get_agent_result', known) is not None
    assert classify_tool_call('await_agents', known) is not None
