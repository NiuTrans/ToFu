#!/usr/bin/env python3
"""Wire-parity for pt_03f4cdf1 slice 10 — Resume-state hydration.

Scope: run_task's Content-Prefix + Resume-Prefill + Checkpoint-stash block
(~80 lines, between the Section 3.5 eligibility-drift guard and the
`await_memory_prefetch(task)` join). All three sub-blocks operate on
``cfg``'s continue-checkpoint keys and hydrate ``task[]`` / ``messages``:

  1. ``cfg['contentPrefix']`` → ``task['content']`` (under content_lock)
     — bookkeeping seed so a resumed response displays [preserved text] +
     [freshly generated continuation]. NEVER re-injected into ``messages``
     as a trailing assistant turn (Anthropic Messages API rejects it).
  2. ``cfg['resumePrefill']`` → append ``{role:'assistant', content:...}``
     to ``messages`` **iff** ``model_supports_assistant_prefill(model)``
     is truthy (Claude → False, so Claude never reaches the append). Also
     stashes ``task['_resumePrefill']`` for merge-into-done accounting.
  3. ``checkpointToolRounds`` are canonically replayed onto the model wire,
     then all checkpoint authorities are stashed (``checkpointToolRounds`` /
     ``checkpointUsage`` / ``checkpointApiRounds`` / ``checkpointModifiedFiles``
     / ``checkpointModifiedFileList``) copied verbatim from cfg onto the task,
     so post-loop DB persistence merges them. The replay is byte-identical to
     the settled-history reconstructor, keeping the following turn cacheable.

Extract to ``lib/tasks_pkg/orchestrator/_resume_state.py::
apply_resume_state``.

Contract:

  apply_resume_state(
      *, task, cfg, messages, model, tid,
  ) -> None

  Mutates ``task`` (``content`` under lock, ``_resumePrefill``,
  ``_checkpointToolRounds`` / ``_checkpointUsage`` / ``_checkpointApiRounds``
  / ``_checkpointModifiedFiles`` / ``_checkpointModifiedFileList``) and,
  ``messages`` (checkpoint replay, followed by a supported resume prefill).
  ``tid`` is the 8-char task-id prefix used in log lines
  — carried in so the extracted logs stay grep-identical to the pre-slice
  form. Never raises.

Failing-first — this test asserts (RED before extraction, GREEN after):
  1. Module ``lib.tasks_pkg.orchestrator._resume_state`` exists and
     exports ``apply_resume_state`` as a callable.
  2. ``_run.py`` imports the helper AND calls it.
  3. The inline body pivots (the ``with task['content_lock']:`` block that
     writes ``task['content'] = _content_prefix``; the
     ``messages.append({'role': 'assistant', 'content': _resume_prefill})``
     line; the ``task['_checkpointToolRounds'] = list(_checkpoint_tr)``
     stash; the inline ``from lib.model_info import
     model_supports_assistant_prefill`` import) are all GONE from
     ``_run.py``.
  4. Helper signature accepts every run_task local crossing the seam.
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
def test_resume_state_module_exists_and_exposes_helper():
    """Slice 10: lib.tasks_pkg.orchestrator._resume_state exists and
    exposes apply_resume_state as a callable."""
    import importlib
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._resume_state')
    assert hasattr(mod, 'apply_resume_state'), (
        'lib.tasks_pkg.orchestrator._resume_state missing '
        'apply_resume_state')
    assert callable(mod.apply_resume_state)


@_unit
def test_run_task_delegates_to_apply_resume_state():
    """Slice 10: _run.py must import the helper and call it inline in
    run_task's body."""
    with open(os.path.join(_ROOT, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    assert ('from lib.tasks_pkg.orchestrator._resume_state import'
            in src), (
        '_run.py must import from _resume_state after slice 10')
    import re as _re
    assert _re.search(r'\bapply_resume_state\s*\(', src), (
        '_run.py must CALL apply_resume_state(...) — a bare '
        'reference in a comment does not satisfy slice 10')


@_unit
def test_resume_state_inline_body_removed_from_run_py():
    """Slice 10: the inline BODY of the resume-state block (three sub-
    clusters — content-prefix write / resume-prefill append / checkpoint
    stashes) MUST be gone from _run.py.

    Landmark comments MAY stay as call-site anchors. A silent revert
    would put every pivot back inline.
    """
    with open(os.path.join(_ROOT, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    for pivot in (
        # content_lock write of the resumed-response seed
        "task['content'] = _content_prefix",
        # trailing-assistant prefill append (capability-gated)
        "messages.append({'role': 'assistant', 'content': _resume_prefill})",
        # inline provider-capability import used ONLY by the prefill block
        'from lib.model_info import model_supports_assistant_prefill',
        # first checkpoint stash — the "did we do it inline" tell
        "task['_checkpointToolRounds'] = list(_checkpoint_tr)",
        # _resumePrefill stash on the task (accounting seed for done event)
        "task['_resumePrefill'] = _resume_prefill",
    ):
        assert pivot not in src, (
            f'_run.py must NOT re-carry inline resume-state pivot '
            f'{pivot!r} — extracted to _resume_state.py'
        )


@_unit
def test_apply_resume_state_signature_matches_seam():
    """Slice 10: the helper's signature accepts every run_task local
    crossing the seam. Enumerated so a future edit that swaps to a
    global-reading variant flips this test."""
    import importlib
    import inspect
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._resume_state')
    sig = inspect.signature(mod.apply_resume_state)
    params = set(sig.parameters.keys())
    required = {
        'task', 'cfg', 'messages', 'model', 'tid',
    }
    missing = required - params
    assert not missing, (
        f'apply_resume_state missing required parameters: '
        f'{sorted(missing)}. All run_task-side locals crossing the seam '
        f'MUST be explicit args.'
    )


if __name__ == '__main__':
    for fn in [
        test_resume_state_module_exists_and_exposes_helper,
        test_run_task_delegates_to_apply_resume_state,
        test_resume_state_inline_body_removed_from_run_py,
        test_apply_resume_state_signature_matches_seam,
    ]:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
