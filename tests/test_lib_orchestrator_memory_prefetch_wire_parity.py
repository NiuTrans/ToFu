#!/usr/bin/env python3
"""Wire-parity for pt_03f4cdf1 slice 9 — Section 3.5 memory prefetch gate.

Scope: run_task's Section 3.5 block (~70 lines) — the BM25 + cheap-LLM
memory prefetch that injects <relevant_memories> into `messages` when
enabled, plus the sibling `_profileConsolidateEligible` metadata stash
that gates the post-done profile-consolidation spawner in _finalize.py.

Runs after Section 3 (context injection) and before the content-prefix /
resume-prefill block. The block does:

  1. Always stash ``task['_profileConsolidateEligible'] = bool(
     memory_enabled and has_real_tools)``.
  2. If ``memory_enabled and has_real_tools and not _injected_tool_calls``:
     import ``run_memory_prefetch``; derive active-tools names from
     ``tool_list``; derive extra memory-scope paths from
     ``cfg['projectPaths'][1:]``; call ``run_memory_prefetch(messages, ...)``.
  3. Never raise — a failure is logged as a warning and the task
     continues without memories.

Extract to ``lib/tasks_pkg/orchestrator/_memory_prefetch.py::
maybe_run_memory_prefetch``.

Contract:

  maybe_run_memory_prefetch(
      *, task, cfg, messages, tool_list, project_path,
      project_enabled, memory_enabled, has_real_tools,
      injected_tool_calls,
  ) -> None

  Mutates `task` (sets ``_profileConsolidateEligible``) and, when
  eligible, `messages` (via ``run_memory_prefetch``). Never raises.

Failing-first — this test asserts (RED before extraction, GREEN after):
  1. Module ``lib.tasks_pkg.orchestrator._memory_prefetch`` exists and
     exports ``maybe_run_memory_prefetch`` as a callable.
  2. ``_run.py`` imports the helper AND calls it.
  3. The inline body pivots (Section 3.5 markers — the
     ``run_memory_prefetch(`` call, the ``from lib.memory.prefetch import
     run_memory_prefetch`` inline import, the ``_profileConsolidateEligible``
     assignment, the ``_mem_extra_paths`` derivation) are all GONE from
     ``_run.py``.
  4. Helper signature accepts the run_task locals crossing the seam.
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
def test_memory_prefetch_module_exists_and_exposes_helper():
    """Slice 9: lib.tasks_pkg.orchestrator._memory_prefetch exists and
    exposes maybe_run_memory_prefetch as a callable."""
    import importlib
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._memory_prefetch')
    assert hasattr(mod, 'maybe_run_memory_prefetch'), (
        'lib.tasks_pkg.orchestrator._memory_prefetch missing '
        'maybe_run_memory_prefetch')
    assert callable(mod.maybe_run_memory_prefetch)


@_unit
def test_run_task_delegates_to_maybe_run_memory_prefetch():
    """Slice 9: _run.py must import the helper and call it inline in
    run_task's body."""
    with open(os.path.join(_ROOT, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    assert ('from lib.tasks_pkg.orchestrator._memory_prefetch import'
            in src), (
        '_run.py must import from _memory_prefetch after slice 9')
    import re as _re
    assert _re.search(r'\bmaybe_run_memory_prefetch\s*\(', src), (
        '_run.py must CALL maybe_run_memory_prefetch(...) — a bare '
        'reference in a comment does not satisfy slice 9')


@_unit
def test_section_35_inline_body_removed_from_run_py():
    """Slice 9: the inline BODY of Section 3.5 (the pivotal call + inline
    import + the _profileConsolidateEligible stash + the
    _mem_extra_paths derivation) MUST be gone from _run.py.

    The section-header comment MAY stay as a call-site landmark. A silent
    revert would put every pivot back inline.
    """
    with open(os.path.join(_ROOT, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    for pivot in (
        'from lib.memory.prefetch import run_memory_prefetch',
        "task['_profileConsolidateEligible'] = bool(",
        '_mem_extra_paths',
        # unique-in-inline emit_event lambda closes over `task`
        'emit_event=lambda ev: append_event(task, ev)',
    ):
        assert pivot not in src, (
            f'_run.py must NOT re-carry inline Section 3.5 pivot '
            f'{pivot!r} — extracted to _memory_prefetch.py'
        )


@_unit
def test_maybe_run_memory_prefetch_signature_matches_seam():
    """Slice 9: the helper's signature accepts every run_task local
    crossing the seam. Enumerated so a future edit that swaps to a
    global-reading variant flips this test."""
    import importlib
    import inspect
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._memory_prefetch')
    sig = inspect.signature(mod.maybe_run_memory_prefetch)
    params = set(sig.parameters.keys())
    required = {
        'task', 'cfg', 'messages', 'tool_list', 'project_path',
        'project_enabled', 'memory_enabled', 'has_real_tools',
        'injected_tool_calls',
    }
    missing = required - params
    assert not missing, (
        f'maybe_run_memory_prefetch missing required parameters: '
        f'{sorted(missing)}. All run_task-side locals crossing the seam '
        f'MUST be explicit args.'
    )


if __name__ == '__main__':
    for fn in [
        test_memory_prefetch_module_exists_and_exposes_helper,
        test_run_task_delegates_to_maybe_run_memory_prefetch,
        test_section_35_inline_body_removed_from_run_py,
        test_maybe_run_memory_prefetch_signature_matches_seam,
    ]:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
