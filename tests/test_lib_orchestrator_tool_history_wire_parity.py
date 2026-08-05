#!/usr/bin/env python3
# Incident anchor: born in commit 800691ce — refactor(orchestrator): pt_03f4cdf1 slice 8 - extract Section 2.5 (To...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Wire-parity for pt_03f4cdf1 slice 8 — Section 2.5 tool-history restoration.

Scope: run_task's Section 2.5 block (~35 lines) — the gated
``keepToolHistory`` rebuild that replaces frontend's summary-only messages
with the full tool_use/tool_result history from the server-side store.
Runs after Section 2 (tool assembly) and before Section 3 (context injection).

The block does:

  1. Read ``cfg.get('keepToolHistory', True)`` + ``task['convId']``.
  2. If both truthy: emit VU phase ``Autopilot：重建工具调用历史…``.
  3. Call ``_rebuild_messages_with_history(conv_id, messages)`` and check
     ``_rebuild_stats['used_store']``.
  4. On hit: compute token overhead via ``_estimate_token_overhead``,
     log the ``TOOL HISTORY RESTORED`` line, replace ``messages`` with
     ``rebuilt``, refresh ``original_messages``, emit a diagnostic PHASE
     event ``tool_history_restored`` with stats + overhead.
  5. On miss: log at debug ``keepToolHistory enabled but no stored messages``.

Extract to ``lib/tasks_pkg/orchestrator/_tool_history.py::restore_tool_history``.

Contract:

  restore_tool_history(*, task, cfg, messages, tid, vu_phase=None)
      -> (messages: list, original_messages: list, used_store: bool)

  Returns a possibly-replaced messages list and a fresh original_messages
  slice; the caller reassigns its two locals from the tuple. Never raises;
  a rebuild error at that layer already logs and falls back to the input.

Failing-first: this test asserts (RED before extraction, GREEN after):
  1. Module ``lib.tasks_pkg.orchestrator._tool_history`` exists and exports
     ``restore_tool_history`` as a callable.
  2. ``_run.py`` imports the helper AND calls it.
  3. The inline body markers (Section 2.5 header, TOOL HISTORY RESTORED
     log line, ``_rebuild_messages_with_history(`` call, ``_estimate_token_overhead(``
     call, ``tool_history_restored`` event phase name) are all GONE from
     ``_run.py``.
  4. Helper signature accepts the run_task locals it needs.
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
def test_tool_history_module_exists_and_exposes_helper():
    """Slice 8: lib.tasks_pkg.orchestrator._tool_history exists and
    exposes restore_tool_history as a callable."""
    import importlib
    mod = importlib.import_module('lib.tasks_pkg.orchestrator._tool_history')
    assert hasattr(mod, 'restore_tool_history'), (
        'lib.tasks_pkg.orchestrator._tool_history missing restore_tool_history')
    assert callable(mod.restore_tool_history)


@_unit
def test_run_task_delegates_to_restore_tool_history():
    """Slice 8: _run.py must import the helper and call it inline in
    run_task's body."""
    with open(os.path.join(_ROOT, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    assert 'from lib.tasks_pkg.orchestrator._tool_history import' in src, (
        '_run.py must import from _tool_history after slice 8')
    import re as _re
    assert _re.search(r'\brestore_tool_history\s*\(', src), (
        '_run.py must CALL restore_tool_history(...) — a bare reference in '
        'a comment does not satisfy slice 8')


@_unit
def test_section_25_inline_body_removed_from_run_py():
    """Slice 8: the inline BODY of Section 2.5 (the pivotal calls +
    log line + event phase name that this extraction consolidated) MUST
    be gone from _run.py. The section-header comment MAY stay (retagged
    with slice 8) as a call-site landmark.

    A silent revert would put every pivot back inline.
    """
    with open(os.path.join(_ROOT, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    for pivot in (
        '_rebuild_messages_with_history(',
        '_estimate_token_overhead(',
        'TOOL HISTORY RESTORED',
        "phase='tool_history_restored'",
    ):
        assert pivot not in src, (
            f'_run.py must NOT re-carry inline Section 2.5 pivot {pivot!r} — '
            f'extracted to _tool_history.py')


@_unit
def test_restore_tool_history_signature_matches_seam():
    """Slice 8: the helper's signature accepts the run_task locals crossing
    the seam. Enumerated so a future edit that swaps to a global-reading
    variant flips this test."""
    import importlib
    import inspect
    mod = importlib.import_module('lib.tasks_pkg.orchestrator._tool_history')
    sig = inspect.signature(mod.restore_tool_history)
    params = set(sig.parameters.keys())
    required = {'task', 'cfg', 'messages', 'tid'}
    missing = required - params
    assert not missing, (
        f'restore_tool_history missing required parameters: {sorted(missing)}. '
        f'All run_task-side locals crossing the seam MUST be explicit args.')


if __name__ == '__main__':
    for fn in [
        test_tool_history_module_exists_and_exposes_helper,
        test_run_task_delegates_to_restore_tool_history,
        test_section_25_inline_body_removed_from_run_py,
        test_restore_tool_history_signature_matches_seam,
    ]:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
