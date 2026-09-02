"""Wire-parity guards for pt_03f4cdf1 slice 31 — extract the hard
provider pin + conversation-sticky routing from _run.py into
lib.tasks_pkg.orchestrator._provider_binding
    .bind_provider_and_affinity().

The unit runs once per run_task invocation, right before Section 1:

    1. Hard provider pin (multi-tenant isolation): when
       ``task['_pinned_provider_id']`` is set (inline `provider` block
       or a registered @prov_xxx BYO endpoint), bind THIS worker thread
       to that provider so every LLM dispatch on it can only pick that
       provider's slot — never silently fall back to an operator key
       and eat a 429. Cleared in the finally block (pooled threads).
    2. Conversation-sticky routing: ``set_convaffinity(convId)`` binds
       the thread to the conversation so the dispatcher prefers the
       API key that last served this conv (Anthropic per-key prompt
       cache warmth). Called UNCONDITIONALLY — an empty convId clears
       any stale affinity from the pooled thread's previous task.

Both are thread-local side effects with one real branch (pin only when
the id is truthy) — pinned by behavioural tests below (owner directive
2026-07-31: logic-bearing leaves ship monkeypatch-driven tests).

Failing-first: written BEFORE the extraction; the module/signature/
delegation guards turn RED until the leaf exists and _run.py delegates.
"""

from __future__ import annotations

import importlib
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
LEAF_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_provider_binding.py'


# ---------------------------------------------------------------------------
# 1. leaf module exists and exposes the helper by name
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_exposes_bind_helper():
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._provider_binding')
    assert hasattr(mod, 'bind_provider_and_affinity'), (
        'lib.tasks_pkg.orchestrator._provider_binding must export '
        'bind_provider_and_affinity')
    assert callable(mod.bind_provider_and_affinity)


# ---------------------------------------------------------------------------
# 2. helper signature (positional task + tid)
# ---------------------------------------------------------------------------
def test_bind_helper_signature():
    import inspect
    from lib.tasks_pkg.orchestrator._provider_binding import (
        bind_provider_and_affinity)
    sig = inspect.signature(bind_provider_and_affinity)
    params = sig.parameters
    assert 'task' in params and 'tid' in params, (
        'task and tid must be parameters')
    assert len(params) == 2, (
        'bind_provider_and_affinity takes exactly (task, tid)')


# ---------------------------------------------------------------------------
# 3. _run.py imports and delegates to the extracted helper
# ---------------------------------------------------------------------------
def test_run_py_imports_bind_helper():
    src = RUN_PY.read_text()
    assert ('from lib.tasks_pkg.orchestrator._provider_binding import'
            in src), (
        '_run.py must import the extracted binding helper — expected a '
        '`from lib.tasks_pkg.orchestrator._provider_binding import ...` '
        'line at module scope')
    assert 'bind_provider_and_affinity' in src


def test_run_task_delegates_to_bind_helper():
    src = RUN_PY.read_text()
    assert 'bind_provider_and_affinity(task, tid)' in src, (
        '_run.py must delegate `bind_provider_and_affinity(task, tid)`')


# ---------------------------------------------------------------------------
# 4. inline bodies are gone from _run.py (extraction really happened)
# ---------------------------------------------------------------------------
def test_run_py_no_longer_pins_inline():
    src = RUN_PY.read_text()
    assert 'set_pinned_provider(' not in src, (
        'set_pinned_provider(...) must live in _provider_binding.py, '
        'not _run.py')


def test_run_py_no_longer_affinities_inline():
    src = RUN_PY.read_text()
    assert 'set_conv_affinity(' not in src, (
        'set_conv_affinity(...) must live in _provider_binding.py, '
        'not _run.py')


# ---------------------------------------------------------------------------
# 5. leaf carries the pivotal semantics
# ---------------------------------------------------------------------------
def test_leaf_calls_real_binders():
    src = LEAF_PY.read_text()
    assert 'set_pinned_provider(' in src, (
        'leaf must call the real set_pinned_provider')
    assert 'set_conv_affinity(' in src, (
        'leaf must call the real set_conv_affinity')
    assert 'lib.llm_dispatch.provider_pin' in src
    assert 'lib.llm_dispatch.conv_affinity' in src


def test_leaf_pin_branch_and_unconditional_affinity():
    src = LEAF_PY.read_text()
    assert "task.get('_pinned_provider_id') or ''" in src, (
        'leaf must carry the pin-id read with empty-string default')
    assert "task.get('convId') or ''" in src, (
        'leaf must carry the convId read with empty-string default — '
        'affinity is set UNCONDITIONALLY so a pooled thread never keeps '
        'a stale conversation binding')


# ---------------------------------------------------------------------------
# 6. BEHAVIOURAL: the pin branch + unconditional affinity (owner directive)
# ---------------------------------------------------------------------------
def test_behaviour_pin_applied_when_id_present(monkeypatch):
    """A truthy _pinned_provider_id → set_pinned_provider called with it;
    affinity always set with the conv id. (The leaf deliberately keeps the
    original's lazy in-function imports, so the patch lands on the SOURCE
    modules, resolved at call time.)"""
    import lib.llm_dispatch.conv_affinity as ca
    import lib.llm_dispatch.provider_pin as pp
    import lib.tasks_pkg.orchestrator._provider_binding as leaf
    calls = {'pin': [], 'aff': []}
    monkeypatch.setattr(pp, 'set_pinned_provider',
                        lambda pid: calls['pin'].append(pid))
    monkeypatch.setattr(ca, 'set_conv_affinity',
                        lambda cid: calls['aff'].append(cid))
    leaf.bind_provider_and_affinity(
        {'_pinned_provider_id': 'prov_acme', 'convId': 'conv-1'}, 'deadbeef')
    assert calls['pin'] == ['prov_acme'], (
        'truthy pin id must reach set_pinned_provider')
    assert calls['aff'] == ['conv-1'], (
        'convId must reach set_conv_affinity')


def test_behaviour_pin_skipped_when_absent_affinity_still_set(monkeypatch):
    """No pin id → set_pinned_provider NOT called; affinity still set
    (with '' when no convId — clearing stale pooled-thread affinity)."""
    import lib.llm_dispatch.conv_affinity as ca
    import lib.llm_dispatch.provider_pin as pp
    import lib.tasks_pkg.orchestrator._provider_binding as leaf
    calls = {'pin': [], 'aff': []}
    monkeypatch.setattr(pp, 'set_pinned_provider',
                        lambda pid: calls['pin'].append(pid))
    monkeypatch.setattr(ca, 'set_conv_affinity',
                        lambda cid: calls['aff'].append(cid))
    leaf.bind_provider_and_affinity({}, 'deadbeef')
    assert calls['pin'] == [], (
        'absent pin id must NOT call set_pinned_provider')
    assert calls['aff'] == [''], (
        'affinity must be set unconditionally (empty string clears the '
        'pooled thread\'s stale conversation binding)')
