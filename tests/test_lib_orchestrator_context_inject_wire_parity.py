#!/usr/bin/env python3
"""Wire-parity for pt_03f4cdf1 slice 7 — Section 3 context injection extracted
to ``lib/tasks_pkg/orchestrator/_context_inject.py``.

**Scope of the slice** (byte-parity extraction, no semantic change):

  run_task's "Section 3: Context Injection" block (~83 lines) — after
  tool assembly + tool-history restoration, before the pre-loop init
  memory-prefetch step. The block currently does:

    1. Emit the ``Autopilot：注入系统上下文…`` VU phase.
    2. Build the ``_tool_names`` set from ``tool_list``.
    3. Call ``_inject_system_contexts(...)`` with the resolved
       project/memory/search/swarm capabilities + disabled prompt blocks.
    4. Emit ``PREFERENCES_APPLIED`` SSE if ``task['_appliedPreferences']``
       was populated by the injection.
    5. Emit ``RELATED_CONVERSATIONS`` SSE if ``task['_relatedConversations']``
       was populated by the injection.
    6. Pop the two prefetch futures + shutdown the prefetch executor.
    7. Stash the ``_t_prep_done`` timing anchor on the task and log the
       prep-duration line.
    8. Emit the ``Autopilot：上下文就绪，正在发送请求…`` VU phase.

The extraction moves these steps behind ``inject_context_and_emit_chips``
in the new module. The seam is a pure function (no closures captured) taking
the run_task locals it needs as explicit keyword args, returning
``_t_prep_done`` for the caller to keep as a local. Byte-parity guards:

  * ``_inject_system_contexts`` is still called with the SAME arguments in
    the SAME order.
  * The two SSE emits fire on the SAME predicates (task field truthiness).
  * The prefetch executor is still shut down and its two futures are still
    popped from task.

**Failing-first**: 3 tests, all RED before extraction:

  1. Module presence + surface: ``inject_context_and_emit_chips`` must
     exist and be callable.
  2. Delegation: ``_run.py`` must IMPORT + CALL the new helper (guards
     against a silent revert).
  3. Old inline markers gone: the Section 3 sentinel comment
     ``# ── Section 3: Context Injection ──`` must NOT remain in _run.py.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart
sys.modules.setdefault('flask', _quart)

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@_unit
def test_context_inject_module_exists_and_exposes_helper():
    """Slice 7 (pt_03f4cdf1): lib.tasks_pkg.orchestrator._context_inject
    must exist and export ``inject_context_and_emit_chips`` as callable."""
    import importlib
    mod = importlib.import_module('lib.tasks_pkg.orchestrator._context_inject')
    assert hasattr(mod, 'inject_context_and_emit_chips'), (
        'lib.tasks_pkg.orchestrator._context_inject must export '
        'inject_context_and_emit_chips (the extracted Section 3 helper)')
    assert callable(mod.inject_context_and_emit_chips)


@_unit
def test_run_task_delegates_to_context_inject_helper():
    """Slice 7: _run.py must IMPORT the helper and CALL it in the body of
    ``run_task``. A silent revert would leave the inline block back in place
    and the import gone."""
    with open(os.path.join(_ROOT, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    assert 'from lib.tasks_pkg.orchestrator._context_inject import' in src, (
        '_run.py must import from _context_inject after slice 7')
    import re as _re
    assert _re.search(r'\binject_context_and_emit_chips\s*\(', src), (
        '_run.py must CALL inject_context_and_emit_chips(...) — a bare '
        'reference in a comment does not satisfy slice 7')


@_unit
def test_section_3_inline_body_removed_from_run_py():
    """Slice 7: the inline BODY of Section 3 (the three pivotal calls
    that this extraction consolidated) MUST be gone from _run.py.

    The section-header comment IS preserved (retagged with
    ``(pt_03f4cdf1 slice 7)``) as a call-site landmark — that's a
    documentation win, not a violation. What must NOT survive are the
    inline pivots themselves:

      * ``_inject_system_contexts(`` call
      * ``_related_convs = task.get('_relatedConversations')`` chip guard
      * ``_prefetch_executor.shutdown(wait=False)`` cleanup

    A silent revert would put every one of them back.
    """
    with open(os.path.join(_ROOT, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    for pivot in (
        '_inject_system_contexts(',
        "_related_convs = task.get('_relatedConversations')",
        '_prefetch_executor.shutdown(wait=False)',
    ):
        assert pivot not in src, (
            f'_run.py must NOT re-carry inline Section 3 pivot {pivot!r} — '
            f'extracted to _context_inject.py')


@_unit
def test_context_inject_helper_arguments_include_run_task_locals():
    """Slice 7: the helper's signature must accept the run_task locals it
    needs — not silently rely on module-level state. Enumerated so a
    future edit that swaps the helper for a global-reading variant flips
    this test.

    The critical dependency-crossing arguments (from run_task's Section 3
    inline code) are: task, messages, cfg, project_path, project_enabled,
    memory_enabled, search_enabled, swarm_enabled, has_real_tools, model,
    tool_list, prefetch_executor, tid, t_run_start.
    """
    import importlib
    import inspect
    mod = importlib.import_module('lib.tasks_pkg.orchestrator._context_inject')
    sig = inspect.signature(mod.inject_context_and_emit_chips)
    params = set(sig.parameters.keys())
    required = {
        'task', 'messages', 'cfg', 'project_path', 'project_enabled',
        'memory_enabled', 'search_enabled', 'swarm_enabled',
        'has_real_tools', 'model', 'tool_list', 'prefetch_executor',
        'tid', 't_run_start',
    }
    missing = required - params
    assert not missing, (
        f'inject_context_and_emit_chips missing required parameters: '
        f'{sorted(missing)}. All run_task-side locals crossing the seam '
        f'MUST be explicit arguments (no module-level shortcuts).')


if __name__ == '__main__':
    for fn in [
        test_context_inject_module_exists_and_exposes_helper,
        test_run_task_delegates_to_context_inject_helper,
        test_section_3_inline_body_removed_from_run_py,
        test_context_inject_helper_arguments_include_run_task_locals,
    ]:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
