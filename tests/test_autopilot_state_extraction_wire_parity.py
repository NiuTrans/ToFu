#!/usr/bin/env python3
"""Wire-parity baseline for autopilot.py's slice-1 decomposition
(board epic ``pt_00459503f23b4c0e``).

**Context**: ``lib/tasks_pkg/autopilot.py`` (2196L) is being decomposed per
``docs/modules/task_engine.md`` in strangler-fig order that respects
the ``pt_8dc03017`` (owner-parked VU-as-independent-stream) sequencing gate.

Slice 1 target: extract the objective + run-id + budget + resolver helpers to
``lib/tasks_pkg/autopilot_state.py`` — the smallest, safest, ZERO-overlap
subset with the pt_8dc03017 cutover touch-points. Chose a sibling module
(``autopilot_state.py``) rather than a full module→package conversion
(``autopilot/_state.py``) for slice 1: converting a heavily-imported module
into a package on a shared-HEAD cross-sibling worktree carries much bigger
merge risk than adding one new sibling file, and the wire-parity contract
(re-export identity) is byte-equivalent either way. A future dispatch (after
the pt_8dc03017 cutover clears the owner-parked gate) can consolidate the
siblings into an ``autopilot/`` package. Symbols moved:

  * ``_extract_objective`` — pure helper over a message list.
  * ``_extract_objective_from_db`` — DB-only read.
  * ``_get_or_persist_objective`` — read-through mint against
    ``settings.autopilotObjective``.
  * ``_get_or_persist_run_id`` — read-through mint against
    ``settings.autopilotRunId``.
  * ``_record_vu_turn_and_check_budget`` — budget-guard RMW.
  * ``_clear_run_id`` — run-end cleanup of the pinned run bookkeeping.
  * ``_resolve_recent_run_id`` — DB reader for the most-recent run id.
  * ``_resolve_run_anchor_turn_id`` — DB reader for the run's boundary
    ``_turnId``.
  * ``_VU_HISTORY_CAP`` / ``_PROGRESS_LEDGER_CAP`` — module constants used
    inside ``_record_vu_turn_and_check_budget``.

**Zero pt_8dc03017 overlap**: none of the extracted symbols are on the
step-3 cutover mutation list (``_VUEventForwarder`` / ``_autopilot_deciding``
latch / VU ``convId=''`` opt-out / ``test_autopilot_poll_handoff.py``).

**Wire-parity contract** (what this test enforces):

  1. Every extracted symbol MUST be importable from
     ``lib.tasks_pkg.autopilot._state`` after the split (the new home).
  2. Every extracted symbol MUST STILL be importable from
     ``lib.tasks_pkg.autopilot`` (the re-export facade) — every existing
     ``from lib.tasks_pkg.autopilot import _X`` call site keeps working.
  3. The two import handles MUST resolve to the SAME object (facade
     re-exports, not re-implementations).

**Why failing-first**: an inline extraction without a re-export facade
would break the 4+ existing sibling tests that monkeypatch on
``lib.tasks_pkg.autopilot`` (``monkeypatch.setattr(ap, '_get_or_persist_...', ...)``).
This test locks the identity contract BEFORE the code move so the facade
is proven load-bearing on the strangler pattern.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Flask→Quart shim (matches the rest of the suite).
import quart as _quart
sys.modules.setdefault('flask', _quart)

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


# The exact symbols moved this slice — the pt_00459503 audit's "Objective +
# budget" cluster PLUS the "Resolvers" cluster it explicitly folded into
# _state.py.
_STATE_SYMBOLS = (
    '_extract_objective',
    '_extract_objective_from_db',
    '_get_or_persist_objective',
    '_get_or_persist_run_id',
    '_record_vu_turn_and_check_budget',
    '_clear_run_id',
    '_resolve_recent_run_id',
    '_resolve_run_anchor_turn_id',
    '_VU_HISTORY_CAP',
    '_PROGRESS_LEDGER_CAP',
)


@_unit
def test_state_module_exists_and_exposes_slice_1_symbols():
    """Slice 1 (pt_00459503): a new ``lib.tasks_pkg.autopilot_state``
    module MUST exist and expose every extracted symbol as a module-level
    attribute. This is the NEW home the extraction created."""
    import importlib
    mod = importlib.import_module('lib.tasks_pkg.autopilot_state')
    missing = [n for n in _STATE_SYMBOLS if not hasattr(mod, n)]
    assert not missing, (
        f'lib.tasks_pkg.autopilot_state is missing extracted symbols: {missing}. '
        f'Slice 1 of pt_00459503 must land all objective/budget/resolver '
        f'helpers under this module.'
    )


@_unit
def test_autopilot_facade_reexports_state_symbols():
    """Slice 1: ``lib.tasks_pkg.autopilot`` MUST remain a valid re-export
    facade for every extracted symbol.

    External callers (all 17 sibling test files + orchestrator + FlowExecutor
    engine path) import via ``from lib.tasks_pkg.autopilot import _extract_objective``
    OR ``monkeypatch.setattr(ap, '_get_or_persist_...', ...)`` — both patterns
    require the facade to keep exposing the symbol. A naive move without the
    re-export bricks every monkeypatch call site (they'd patch the facade
    but the code would read directly from ``_state``)."""
    import importlib
    ap = importlib.import_module('lib.tasks_pkg.autopilot')
    missing = [n for n in _STATE_SYMBOLS if not hasattr(ap, n)]
    assert not missing, (
        f'lib.tasks_pkg.autopilot is missing re-exported symbols after '
        f'slice 1: {missing}. Preserve the facade or existing '
        f'from-imports / monkeypatches break silently.'
    )


@_unit
def test_state_and_facade_resolve_to_same_object():
    """Slice 1: the facade attribute and the _state module attribute
    MUST be IDENTICAL objects (``is`` comparison).

    This is the load-bearing invariant for monkeypatches: patching
    ``lib.tasks_pkg.autopilot._get_or_persist_objective`` must steer the
    live code path. That only works if the callers dereference the FACADE
    at call time (LOAD_GLOBAL / attribute access), not if they cache a
    private ``_state`` reference. Enforcing identity here is the strongest
    contract: even if the audit's future slices change the reexport idiom,
    the identity property survives.
    """
    import importlib
    ap = importlib.import_module('lib.tasks_pkg.autopilot')
    st = importlib.import_module('lib.tasks_pkg.autopilot_state')
    mismatches = []
    for n in _STATE_SYMBOLS:
        if getattr(ap, n) is not getattr(st, n):
            mismatches.append(n)
    assert not mismatches, (
        f'Facade re-export identity broken for: {mismatches}. Each '
        f'lib.tasks_pkg.autopilot.<name> MUST be the SAME OBJECT as '
        f'lib.tasks_pkg.autopilot_state.<name> — otherwise monkeypatches '
        f'on the facade silently miss the live code path.'
    )


@_unit
def test_extract_objective_still_functional_after_extraction():
    """Slice 1: the PURE ``_extract_objective`` function keeps its
    documented semantics through the extraction — first real user message
    text, skipping VU directives, meta carriers, and synthetic virtual
    users. Behaviour-parity guard for the extraction (not a duplicate of
    tests/test_autopilot_verify.py's more comprehensive suite — that one
    still runs against the facade)."""
    from lib.tasks_pkg.autopilot import _extract_objective
    msgs = [
        {'role': 'user', '_isMeta': True, 'content': 'runtime carrier'},
        {'role': 'user', '_isVuDirective': True, 'content': 'directive'},
        {'role': 'user', '_isVirtualUser': True, 'content': 'vu turn'},
        {'role': 'user', 'content': 'The real ask.'},
        {'role': 'user', 'content': 'Later human turn'},
    ]
    assert _extract_objective(msgs) == 'The real ask.', (
        '_extract_objective must return the first real (non-meta, '
        'non-directive, non-VU) user message text.')


@_unit
def test_state_extraction_pt_8dc03017_isolation():
    """Slice 1 must NOT drag any pt_8dc03017 cutover touch-points into
    _state.py — that's the sequencing constraint the audit locked in.

    Concretely, ``_VUEventForwarder``, ``_emit_vu_setup_phase``, VU
    ``convId=''`` opt-out logic, and the ``_autopilot_deciding`` latch
    must NOT appear inside ``_state.py``. If any of them ends up there,
    the future pt_8dc03017 cutover will collide with a file it didn't
    author.
    """
    import inspect
    import importlib
    st = importlib.import_module('lib.tasks_pkg.autopilot_state')
    src = inspect.getsource(st)
    # Strip docstrings / comments so we only inspect CODE (the module's
    # own docstring legitimately NAMES the pt_8dc03017 tokens to explain
    # what it deliberately does NOT carry — that is signal, not a
    # violation). A cheap two-pass strip is enough:
    #   1) drop lines whose first non-space char is '#' (comments)
    #   2) drop triple-quoted docstring blocks (module + function).
    import re
    code_lines = []
    in_docstring = False
    docstring_quote = None
    for line in src.splitlines():
        stripped = line.lstrip()
        if in_docstring:
            code_lines.append('')  # keep line count consistent for grep-ability
            if docstring_quote in line:
                in_docstring = False
                docstring_quote = None
            continue
        # Comment-only line?
        if stripped.startswith('#'):
            code_lines.append('')
            continue
        # Docstring opening?
        m = re.match(r'^\s*(?P<q>"""|\'\'\')', line)
        if m:
            q = m.group('q')
            # Same-line close?
            rest = line[m.end():]
            if q in rest:
                # Single-line docstring → drop this line entirely.
                code_lines.append('')
            else:
                in_docstring = True
                docstring_quote = q
                code_lines.append('')
            continue
        code_lines.append(line)
    code_only = '\n'.join(code_lines)
    # Symbol-level: none of these classes/functions/latches belong in _state.
    assert 'class _VUEventForwarder' not in code_only, (
        'autopilot_state.py must NOT contain _VUEventForwarder (pt_8dc03017 target).')
    assert 'def _emit_vu_setup_phase' not in code_only, (
        'autopilot_state.py must NOT contain _emit_vu_setup_phase (event forwarding).')
    # Sentinel keywords that only appear in the mutation-targeted code:
    for latch in ('_autopilot_deciding', "convId=''"):
        assert latch not in code_only, (
            f'autopilot_state.py must NOT reference pt_8dc03017 cutover point {latch!r} '
            f'in CODE — that logic stays with the VU/baton path until owner cutover. '
            f'(Comments / docstrings that NAME the token to explain the constraint '
            f'are OK — this check strips them.)')


if __name__ == '__main__':
    tests = [
        test_state_module_exists_and_exposes_slice_1_symbols,
        test_autopilot_facade_reexports_state_symbols,
        test_state_and_facade_resolve_to_same_object,
        test_extract_objective_still_functional_after_extraction,
        test_state_extraction_pt_8dc03017_isolation,
    ]
    for fn in tests:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
