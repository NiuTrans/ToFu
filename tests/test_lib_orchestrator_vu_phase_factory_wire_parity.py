# Incident anchor: born in commit c4a5e390 — refactor(orchestrator): pt_03f4cdf1 slice 37 — VU phase closure facto...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Wire-parity guards for pt_03f4cdf1 slice 37 — replace run_task's
inline VU-startup attribution + closure adapter with
lib.tasks_pkg.orchestrator._vu_startup.make_vu_phase().

The inline block computed ``_vu_startup = bool(task.get('_vu_subtask'))``
and defined a local closure ``_vu_phase(detail)`` forwarding to the
extracted module-level ``_extracted_vu_phase``. The captured ``task`` +
``_vu_startup`` are stable across the whole invocation (no rebind), so a
factory that binds them once is semantically identical — and lets the
spine read ``_vu_phase = make_vu_phase(task)``.

The VU sub-task carries ``_vu_event_transform`` (the append_event facade
seam), so any PHASE emitted through the closure is wrapped as
``autopilot_vu_event`` and lands in the synthetic-user bubble on BOTH
the carrier's own stream and the parent's. Gated on ``_vu_subtask`` so
the ordinary worker/endpoint path stays byte-identical (no new events).

Failing-first: written BEFORE the extraction; the factory/delegation
guards turn RED until the factory exists and _run.py delegates.
"""

from __future__ import annotations

import importlib
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
LEAF_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_vu_startup.py'


# ---------------------------------------------------------------------------
# 1. leaf exposes the factory
# ---------------------------------------------------------------------------
def test_vu_startup_exposes_factory():
    mod = importlib.import_module('lib.tasks_pkg.orchestrator._vu_startup')
    assert hasattr(mod, 'make_vu_phase') and callable(mod.make_vu_phase), (
        '_vu_startup must export make_vu_phase')


# ---------------------------------------------------------------------------
# 2. _run.py uses the universal localized startup emitter
# ---------------------------------------------------------------------------
def test_run_task_uses_shared_startup_phase_emitter():
    src = RUN_PY.read_text()
    for stage in ('config', 'tools', 'history', 'context'):
        assert f"_emit_startup_phase(task, '{stage}')" in src


def test_run_py_no_inline_vu_closure():
    src = RUN_PY.read_text()
    assert 'def _vu_phase(detail):' not in src, (
        'the inline closure must be replaced by the factory')
    assert "_vu_startup = bool(task.get('_vu_subtask'))" not in src, (
        'the _vu_startup computation must live in the factory')


# ---------------------------------------------------------------------------
# 3. leaf carries the pivotal semantics
# ---------------------------------------------------------------------------
def test_leaf_factory_binds_subtask_flag_and_forwards():
    src = LEAF_PY.read_text()
    assert "bool(task.get('_vu_subtask'))" in src
    assert 'def _bound(detail, *, detail_key=None, detail_args=None):' in src, (
        'the factory must return the closure (internally named _bound so '
        'the module-level _vu_phase stays resolvable — an inner '
        'def _vu_phase would shadow it across the whole factory scope)')
    assert 'detail_key=detail_key' in src and 'detail_args=detail_args' in src, (
        'the closure must forward to the module-level _vu_phase through '
        'the module namespace (resolved at call time)')


# ---------------------------------------------------------------------------
# 4. BEHAVIOURAL: the gate + forwarding (owner directive)
# ---------------------------------------------------------------------------
def test_behaviour_gate_and_forwarding(monkeypatch):
    """Non-VU task → closure calls the module fn with vu_startup=False;
    VU subtask → vu_startup=True. Detail forwards verbatim."""
    import lib.tasks_pkg.orchestrator._vu_startup as leaf
    calls = []
    monkeypatch.setattr(
        leaf, '_vu_phase',
        lambda task, detail, *, vu_startup, detail_key=None, detail_args=None:
            calls.append((task['id'], detail, vu_startup,
                          detail_key, detail_args)))
    plain = leaf.make_vu_phase({'id': 'a'})
    plain('step-1')
    vu = leaf.make_vu_phase({'id': 'b', '_vu_subtask': True})
    vu('step-2', detail_key='stream.phase.startup', detail_args={'step': 2})
    assert calls == [
        ('a', 'step-1', False, None, None),
        ('b', 'step-2', True, 'stream.phase.startup', {'step': 2}),
    ], (
        'the gate must read _vu_subtask once at factory time and forward '
        'detail verbatim with it')
