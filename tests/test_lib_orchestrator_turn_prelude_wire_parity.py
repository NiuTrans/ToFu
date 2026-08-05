# Incident anchor: born in commit 8f37b141 — refactor(orchestrator): pt_03f4cdf1 slice 33 — extract turn prelude t...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Wire-parity guards for pt_03f4cdf1 slice 33 — extract the pre-Section-1
turn prelude from _run.py into
lib.tasks_pkg.orchestrator._turn_prelude.run_turn_prelude().

Three independent preamble steps, in the original order:

    1. Swarm autocontinue chain reset on HUMAN turns: a human-initiated
       turn (NOT carrying ``cfg['_swarmAutoContinue']``) means the user
       is back in the loop, so the consecutive-auto-continue ceiling
       starts fresh. Fail-soft (debug log on error). See
       lib/swarm/integration.py.
    2. Capability profile merge: named profile defaults merge UNDER the
       explicit cfg (explicit caller values always win); no-op for
       'default'. Rebinds cfg AND task['config'] — the leaf RETURNS the
       merged cfg so the caller rebinds identically.
    3. Per-client browser routing: ``cfg['browserClientId']`` sets the
       thread-local client ID so all browser commands from this task
       thread route to the correct device's extension.

Branches (behavioural-pinned): step 1 skips when _swarmAutoContinue is
set; step 2 rebinds cfg only when the profile is non-default; step 3
only routes when browserClientId is truthy.

Failing-first: written BEFORE the extraction; the module/signature/
delegation guards turn RED until the leaf exists and _run.py delegates.
"""

from __future__ import annotations

import importlib
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
LEAF_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_turn_prelude.py'


# ---------------------------------------------------------------------------
# 1. leaf module exists and exposes the helper
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_exposes_prelude():
    mod = importlib.import_module('lib.tasks_pkg.orchestrator._turn_prelude')
    assert hasattr(mod, 'run_turn_prelude') and callable(mod.run_turn_prelude), (
        'lib.tasks_pkg.orchestrator._turn_prelude must export run_turn_prelude')


def test_prelude_signature():
    import inspect
    from lib.tasks_pkg.orchestrator._turn_prelude import run_turn_prelude
    params = inspect.signature(run_turn_prelude).parameters
    for name in ('task', 'cfg', 'tid'):
        assert name in params, f'run_turn_prelude missing {name}'


# ---------------------------------------------------------------------------
# 2. _run.py imports and delegates; cfg rebind preserved
# ---------------------------------------------------------------------------
def test_run_py_imports_prelude():
    src = RUN_PY.read_text()
    assert ('from lib.tasks_pkg.orchestrator._turn_prelude import'
            in src), '_run.py must import run_turn_prelude at module scope'


def test_run_task_delegates_with_cfg_rebind():
    src = RUN_PY.read_text()
    assert 'cfg = run_turn_prelude(task, cfg, tid)' in src, (
        '_run.py must delegate with the cfg rebind — the profile merge '
        'can replace cfg, and the inline original rebound the local')


# ---------------------------------------------------------------------------
# 3. inline bodies are gone from _run.py
# ---------------------------------------------------------------------------
def test_run_py_no_inline_prelude_steps():
    src = RUN_PY.read_text()
    for needle in ('reset_autocontinue_chain(', 'apply_profile(',
                   '_set_active_client('):
        assert needle not in src, (
            f'{needle} must live in _turn_prelude.py, not _run.py')


# ---------------------------------------------------------------------------
# 4. leaf carries the pivotal semantics
# ---------------------------------------------------------------------------
def test_leaf_carries_all_three_steps():
    src = LEAF_PY.read_text()
    assert 'reset_autocontinue_chain(' in src, 'leaf must reset the chain'
    assert '_swarmAutoContinue' in src, 'leaf must gate on _swarmAutoContinue'
    assert 'apply_profile(' in src and 'resolve_profile_name(' in src, (
        'leaf must merge the capability profile')
    assert "_set_active_client(" in src and 'browserClientId' in src, (
        'leaf must route the browser client')
    assert 'return cfg' in src, 'leaf must return the (possibly merged) cfg'


# ---------------------------------------------------------------------------
# 5. BEHAVIOURAL: the three branches (owner directive)
# ---------------------------------------------------------------------------
def test_behaviour_human_turn_resets_chain_and_merges_profile(monkeypatch):
    """Human turn (no _swarmAutoContinue) → chain reset fires; non-default
    profile → cfg replaced + task['config'] updated; browserClientId set →
    routing fires."""
    import lib.agent_core.profiles as prof
    import lib.swarm.integration as swi
    import lib.tasks_pkg.orchestrator._turn_prelude as leaf
    calls = {'reset': [], 'client': []}
    monkeypatch.setattr(swi, 'reset_autocontinue_chain',
                        lambda k: calls['reset'].append(k))
    monkeypatch.setattr(swi, 'swarm_key_for', lambda t: 'K')
    monkeypatch.setattr(prof, 'resolve_profile_name', lambda c: 'studio')
    monkeypatch.setattr(prof, 'apply_profile',
                        lambda c: {**c, '_profiled': True})
    import lib.browser as br
    monkeypatch.setattr(br, '_set_active_client',
                        lambda cid: calls['client'].append(cid))

    task = {'config': {'model': 'm'}}
    cfg = {'model': 'm', 'browserClientId': 'client-abc'}
    out = leaf.run_turn_prelude(task, cfg, 'deadbeef')
    assert calls['reset'] == ['K'], 'human turn must reset the chain'
    assert out.get('_profiled') is True and task['config'].get('_profiled') is True, (
        'non-default profile must rebind cfg AND task[\'config\']')
    assert calls['client'] == ['client-abc'], 'browser routing must fire'


def test_behaviour_auto_continue_turn_skips_reset_default_profile_noop(monkeypatch):
    """_swarmAutoContinue turn → NO chain reset (the ceiling bounds the
    runaway loop); 'default' profile → cfg returned unchanged, task
    untouched; no browserClientId → no routing."""
    import lib.agent_core.profiles as prof
    import lib.swarm.integration as swi
    import lib.tasks_pkg.orchestrator._turn_prelude as leaf
    calls = {'reset': [], 'client': []}
    monkeypatch.setattr(swi, 'reset_autocontinue_chain',
                        lambda k: calls['reset'].append(k))
    monkeypatch.setattr(prof, 'resolve_profile_name', lambda c: 'default')
    import lib.browser as br
    monkeypatch.setattr(br, '_set_active_client',
                        lambda cid: calls['client'].append(cid))

    cfg = {'model': 'm', '_swarmAutoContinue': True}
    task = {'config': cfg}
    out = leaf.run_turn_prelude(task, cfg, 'deadbeef')
    assert calls['reset'] == [], (
        'auto-continue turn must NOT reset the chain (the ceiling '
        'bounds the runaway unattended loop)')
    assert out is cfg, "default profile must return the SAME cfg object"
    assert calls['client'] == []
