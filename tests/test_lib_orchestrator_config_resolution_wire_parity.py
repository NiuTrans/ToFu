"""Wire-parity guards for pt_03f4cdf1 slice 30 — extract the config
resolution + model-seed from _run.py's Section 1 into
lib.tasks_pkg.orchestrator._config_resolution
    .resolve_and_seed_model_config().

The unit runs once per run_task invocation at the top of Section 1:

    1. ``mcfg = _resolve_model_config(cfg, task['id'])`` — resolves the
       per-model config (model, thinking, preset, token/temperature,
       feature flags, project scope).
    2. Model seed (epic pt_8f6cbc753855415e): ``if model:
       task['model'] = model``. The loop tail re-stamps the model after
       each successful round, but a first-call DISPATCH failure
       (revoked-OAuth 401, all keys cooling, endpoint-unreachable
       exhaustion) raises BEFORE any round succeeds — the error row
       then persisted with metadata.model NULL (40 such rows in 14
       days), invisible to per-model failure stats. The post-round
       stamp still tracks fallback swaps; this seed is the floor.

The 17-field unpack stays inline in run_task as local-variable
binding (owner DONE definition: local binding is spine-legitimate);
only the resolve + seed branch moves. LOGIC BRANCH — pinned by
behavioural tests below (owner directive 2026-07-31).

Failing-first: written BEFORE the extraction; the module/signature/
delegation guards turn RED until the leaf exists and _run.py
delegates.
"""

from __future__ import annotations

import importlib
import pathlib

import pytest


pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
LEAF_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_config_resolution.py'


# ---------------------------------------------------------------------------
# 1. leaf module exists and exposes the helper by name
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_exposes_resolve_helper():
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._config_resolution')
    assert hasattr(mod, 'resolve_and_seed_model_config'), (
        'lib.tasks_pkg.orchestrator._config_resolution must export '
        'resolve_and_seed_model_config')
    assert callable(mod.resolve_and_seed_model_config)


# ---------------------------------------------------------------------------
# 2. helper signature (positional cfg + task only)
# ---------------------------------------------------------------------------
def test_resolve_helper_signature():
    import inspect
    from lib.tasks_pkg.orchestrator._config_resolution import (
        resolve_and_seed_model_config)
    sig = inspect.signature(resolve_and_seed_model_config)
    params = sig.parameters
    assert 'cfg' in params and 'task' in params, (
        'cfg and task must be parameters')
    assert len(params) == 2, (
        'resolve_and_seed_model_config takes exactly (cfg, task)')


# ---------------------------------------------------------------------------
# 3. _run.py imports and delegates to the extracted helper
# ---------------------------------------------------------------------------
def test_run_py_imports_resolve_helper():
    src = RUN_PY.read_text()
    assert ('from lib.tasks_pkg.orchestrator._config_resolution import'
            in src), (
        '_run.py must import the extracted resolve helper — expected a '
        '`from lib.tasks_pkg.orchestrator._config_resolution import ...` '
        'line at module scope')
    assert 'resolve_and_seed_model_config' in src


def test_run_task_delegates_to_resolve_helper():
    """Section 1 must obtain mcfg from a single call to
    ``resolve_and_seed_model_config(cfg, task)`` — no inline
    _resolve_model_config call or seed branch left behind."""
    src = RUN_PY.read_text()
    assert 'mcfg = resolve_and_seed_model_config(cfg, task)' in src, (
        '_run.py must delegate `mcfg = resolve_and_seed_model_config('
        'cfg, task)` in Section 1')


# ---------------------------------------------------------------------------
# 4. inline bodies are gone from _run.py (extraction really happened)
# ---------------------------------------------------------------------------
def test_run_py_no_longer_calls_resolve_model_config_inline():
    src = RUN_PY.read_text()
    assert '_resolve_model_config(' not in src, (
        '_resolve_model_config(...) must live in _config_resolution.py, '
        'not _run.py')


def test_run_py_no_longer_carries_model_seed_inline():
    """The `if model:` seed branch must live in the leaf. (The bare
    `model = mcfg['model']` local binding legitimately stays inline.)"""
    src = RUN_PY.read_text()
    assert "if model:\n            task['model'] = model" not in src, (
        'the model-seed branch must live in _config_resolution.py, '
        'not _run.py')


# ---------------------------------------------------------------------------
# 5. leaf carries the pivotal semantics
# ---------------------------------------------------------------------------
def test_leaf_calls_real_resolver():
    src = LEAF_PY.read_text()
    assert '_resolve_model_config(' in src, (
        'leaf must call the real _resolve_model_config')
    assert 'from lib.tasks_pkg.model_config import' in src, (
        'leaf must import _resolve_model_config from '
        'lib.tasks_pkg.model_config')


def test_leaf_carries_seed_branch_and_rationale():
    src = LEAF_PY.read_text()
    assert "task['model'] = model" in src, (
        'leaf must carry the model seed assignment')
    assert 'first-call DISPATCH failure' in src
    assert 'metadata.model NULL' in src


def test_leaf_returns_mcfg():
    src = LEAF_PY.read_text()
    assert 'return mcfg' in src, (
        'leaf must return the resolved mcfg dict')


# ---------------------------------------------------------------------------
# 6. BEHAVIOURAL: the seed branch (owner directive — logic-bearing
#    leaves must ship monkeypatch-driven tests)
# ---------------------------------------------------------------------------
def test_behaviour_seed_applied_when_model_truthy(monkeypatch):
    """mcfg with a truthy model → task['model'] is seeded immediately
    (the floor for first-call dispatch failures)."""
    import lib.tasks_pkg.orchestrator._config_resolution as leaf
    monkeypatch.setattr(
        leaf, '_resolve_model_config',
        lambda cfg, tid: {'model': 'claude-x', 'thinking_enabled': False})
    task = {'id': 'deadbeef' * 5}
    mcfg = leaf.resolve_and_seed_model_config({}, task)
    assert mcfg['model'] == 'claude-x', (
        'the resolved mcfg must be returned unchanged')
    assert task['model'] == 'claude-x', (
        "truthy model must seed task['model'] immediately")


def test_behaviour_seed_skipped_when_model_falsy(monkeypatch):
    """mcfg with a falsy model → task is NOT touched (the inline
    original guarded `if model:`)."""
    import lib.tasks_pkg.orchestrator._config_resolution as leaf
    monkeypatch.setattr(
        leaf, '_resolve_model_config',
        lambda cfg, tid: {'model': '', 'thinking_enabled': False})
    task = {'id': 'deadbeef' * 5}
    mcfg = leaf.resolve_and_seed_model_config({}, task)
    assert mcfg['model'] == ''
    assert 'model' not in task, (
        "falsy model must NOT create task['model']")
