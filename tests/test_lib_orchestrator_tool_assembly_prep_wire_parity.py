# Incident anchor: born in commit 53f33659 — refactor(orchestrator): pt_03f4cdf1 slice 29 — extract Section 2 tool...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Wire-parity guards for pt_03f4cdf1 slice 29 — extract Section 2
(tool assembly + pending-swarm force-enable + tool-schema stash) from
_run.py's pre-stream prep into
lib.tasks_pkg.orchestrator._tool_assembly_prep.assemble_round_tools().

The block runs once per run_task invocation, after config resolution
(Section 1) and prefetch kick:

    1. ``_assemble_tool_list`` — builds the per-turn tool schema from
       cfg + the mcfg feature flags,
    2. Pending-swarm force-enable guard (the get_agent_result /
       await_agents "非真实工具" rejection-desync root fix): if
       swarm_enabled is False but a live-or-pending swarm exists for
       this conversation, the follow-up tools are force-added so the injected
       <swarm-update> can be acted on. LOGIC BRANCH — pinned by behavioural tests below, not
       just source greps (owner directive 2026-07-31).
    3. ``task['_tool_schema'] = tool_list`` stash so the compaction
       token-gate accounts for the tool-schema cost.

Signature shape: all feature flags arrive via the ``mcfg`` dict
(produced by _resolve_model_config in Section 1) — no 12-kwarg
re-plumbing. Startup narration is owned by ``_run.py`` at the real stage
boundaries, independently of this tool-assembly helper.

Failing-first: written BEFORE the extraction; the module/signature/
delegation guards turn RED until the leaf exists and _run.py
delegates.
"""

from __future__ import annotations

import importlib
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
LEAF_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_tool_assembly_prep.py'


# ---------------------------------------------------------------------------
# 1. leaf module exists and exposes the helper by name
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_exposes_assembly_helper():
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._tool_assembly_prep')
    assert hasattr(mod, 'assemble_round_tools'), (
        'lib.tasks_pkg.orchestrator._tool_assembly_prep must export '
        'assemble_round_tools')
    assert callable(mod.assemble_round_tools)


# ---------------------------------------------------------------------------
# 2. helper signature (positional cfg/task/mcfg only)
# ---------------------------------------------------------------------------
def test_assembly_helper_signature():
    import inspect
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import (
        assemble_round_tools)
    sig = inspect.signature(assemble_round_tools)
    params = sig.parameters
    for name in ('cfg', 'task', 'mcfg'):
        assert name in params, f'{name} must be a parameter'
    assert tuple(params) == ('cfg', 'task', 'mcfg')


# ---------------------------------------------------------------------------
# 3. _run.py imports and delegates to the extracted helper
# ---------------------------------------------------------------------------
def test_run_py_imports_assembly_helper():
    src = RUN_PY.read_text()
    assert ('from lib.tasks_pkg.orchestrator._tool_assembly_prep import'
            in src), (
        '_run.py must import the extracted assembly helper — expected a '
        '`from lib.tasks_pkg.orchestrator._tool_assembly_prep import ...` '
        'line at module scope')
    assert 'assemble_round_tools' in src


def test_run_task_delegates_to_assembly_helper():
    """Section 2 must unpack the 2-tuple from a single call to
    ``assemble_round_tools(cfg, task, mcfg)`` — no inline
    body left behind."""
    src = RUN_PY.read_text()
    assert ('tool_list, has_real_tools = '
            'assemble_round_tools(' in src), (
        '_run.py must unpack `tool_list, has_real_tools '
        '= assemble_round_tools(...)` in Section 2')


# ---------------------------------------------------------------------------
# 4. inline bodies are gone from _run.py (extraction really happened)
# ---------------------------------------------------------------------------
def test_run_py_no_longer_calls_assemble_tool_list_inline():
    src = RUN_PY.read_text()
    assert '_assemble_tool_list(' not in src, (
        '_assemble_tool_list(...) must live in _tool_assembly_prep.py, '
        'not _run.py')


def test_run_py_no_longer_carries_swarm_guard_inline():
    src = RUN_PY.read_text()
    assert 'has_live_or_pending_swarm' not in src, (
        'the pending-swarm force-enable guard must live in '
        '_tool_assembly_prep.py, not _run.py')


def test_run_py_no_longer_stashes_schema_inline():
    src = RUN_PY.read_text()
    assert "task['_tool_schema'] = tool_list" not in src, (
        "task['_tool_schema'] = tool_list must live in "
        '_tool_assembly_prep.py, not _run.py')


# ---------------------------------------------------------------------------
# 5. leaf carries the pivotal semantics
# ---------------------------------------------------------------------------
def test_leaf_calls_real_assembler():
    src = LEAF_PY.read_text()
    assert '_assemble_tool_list(' in src, (
        'leaf must call the real _assemble_tool_list from model_config')
    assert 'from lib.tasks_pkg.model_config import' in src, (
        'leaf must import _assemble_tool_list from '
        'lib.tasks_pkg.model_config')


def test_leaf_carries_swarm_force_enable_guard():
    src = LEAF_PY.read_text()
    assert 'has_live_or_pending_swarm' in src, (
        'leaf must carry the pending-swarm probe')
    assert 'resolve_turn_swarm_tools' in src, (
        'leaf must carry resolve_turn_swarm_tools')
    assert 'max_tool_rounds' not in src


def test_leaf_stashes_tool_schema_on_task():
    src = LEAF_PY.read_text()
    assert "task['_tool_schema'] = tool_list" in src, (
        "leaf must stash task['_tool_schema'] for the compaction "
        'token-gate')


def test_leaf_reads_flags_from_mcfg_with_same_defaults():
    """The feature flags must be read from the mcfg dict with the SAME
    access shapes as the inline original: subscript for the guaranteed
    keys, .get(..., False) for human_guidance / scheduler."""
    src = LEAF_PY.read_text()
    assert "mcfg.get('human_guidance_enabled', False)" in src, (
        'leaf must read human_guidance_enabled via .get(..., False)')
    assert "mcfg.get('scheduler_enabled', False)" in src, (
        'leaf must read scheduler_enabled via .get(..., False)')
    assert "mcfg['swarm_enabled']" in src, (
        'leaf must read swarm_enabled via subscript (guaranteed key)')


# ---------------------------------------------------------------------------
# 6. BEHAVIOURAL: pending-swarm force-enable branch (owner directive —
#    logic-bearing leaves must ship monkeypatch-driven tests, not just
#    source greps)
# ---------------------------------------------------------------------------
def _mcfg(**overrides):
    base = {
        'project_path': '/tmp/proj', 'project_enabled': False,
        'search_mode': 'off', 'search_enabled': False,
        'fetch_enabled': False, 'code_exec_enabled': False,
        'browser_enabled': False, 'desktop_enabled': False,
        'swarm_enabled': False, 'image_gen_enabled': False,
        'human_guidance_enabled': False, 'scheduler_enabled': False,
    }
    base.update(overrides)
    return base


def test_behaviour_pending_swarm_forces_tools(monkeypatch):
    """swarm_enabled=False + live-or-pending swarm → the follow-up
    tools land in the schema and has_real_tools flips True."""
    import lib.tasks_pkg.orchestrator._tool_assembly_prep as leaf
    import lib.swarm.integration as integ

    monkeypatch.setattr(
        leaf, '_assemble_tool_list',
        lambda *a, **k: ([], False))
    monkeypatch.setattr(
        integ, 'has_live_or_pending_swarm', lambda task: True)

    task = {'id': 'deadbeef' * 5, 'convId': 'convX', 'messages': []}
    tool_list, has_real_tools = leaf.assemble_round_tools(
        {}, task, _mcfg())

    names = {
        (t.get('function') or {}).get('name')
        for t in (tool_list or [])
        if isinstance(t, dict)
    }
    assert {'spawn_agents', 'await_agents', 'get_agent_result'} <= names, (
        f'pending-swarm turn must expose the swarm follow-up tools; '
        f'got {sorted(n for n in names if n)}')
    assert has_real_tools is True, (
        'forced swarm tools must flip has_real_tools to True')
    assert task['_tool_schema'] is tool_list, (
        "task['_tool_schema'] must be stashed to the returned list")


def test_behaviour_no_pending_swarm_leaves_schema_alone(monkeypatch):
    """swarm_enabled=False + NO pending swarm → the assembler's output
    passes through untouched (no forced tools)."""
    import lib.tasks_pkg.orchestrator._tool_assembly_prep as leaf
    import lib.swarm.integration as integ

    monkeypatch.setattr(
        leaf, '_assemble_tool_list',
        lambda *a, **k: ([], False))
    monkeypatch.setattr(
        integ, 'has_live_or_pending_swarm', lambda task: False)

    task = {'id': 'deadbeef' * 5, 'convId': 'convX', 'messages': []}
    tool_list, has_real_tools = leaf.assemble_round_tools(
        {}, task, _mcfg())

    names = {
        (t.get('function') or {}).get('name')
        for t in (tool_list or [])
        if isinstance(t, dict)
    }
    assert not ({'spawn_agents', 'await_agents', 'get_agent_result'} & names), (
        'no pending swarm → swarm tools must NOT be forced in')
    assert has_real_tools is False
    assert task['_tool_schema'] == (tool_list or [])
