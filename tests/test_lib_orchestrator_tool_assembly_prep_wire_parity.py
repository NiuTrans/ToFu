"""Wire-parity guards for pt_03f4cdf1 slice 29 — extract Section 2
(tool assembly + tool-schema stash) from _run.py's pre-stream prep into
lib.tasks_pkg.orchestrator._tool_assembly_prep.assemble_round_tools().

The block runs once per run_task invocation, after config resolution
(Section 1) and prefetch kick:

    1. ``_assemble_tool_list`` — builds the per-turn tool schema from
       cfg + the mcfg feature flags,
    2. ``task['_tool_schema'] = tool_list`` stash so the compaction
       token-gate accounts for the tool-schema cost.

(The pending-swarm force-enable guard was RETIRED 2026-08-23: swarm tools
are now default tools — always assembled — so a turn can never lack the
follow-up surface the guard used to force in.)

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
