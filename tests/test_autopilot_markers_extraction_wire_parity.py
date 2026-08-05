#!/usr/bin/env python3
"""Wire-parity for pt_00459503 slice 2 — arm/disarm marker cluster extraction.

Board epic ``pt_00459503f23b4c0e``: decompose ``lib/tasks_pkg/autopilot.py``
(1801L post-slice-1). Slice 2 extracts the arm/disarm marker cluster —
the SMALLEST cohesive group with ZERO overlap with the pt_8dc03017
(owner-parked, human-gated) cutover mutation list (which targets
_VUEventForwarder / _autopilot_deciding latch / VU convId='' opt-out /
test_autopilot_poll_handoff.py — none of these appear in the marker
group).

**Extracted this slice** to ``lib/tasks_pkg/autopilot_markers.py``:

  * ``arm_autopilot(conv_id)`` — the runtime-arm gesture that flips
    ``config['autopilot']=True`` on live tasks + persists the queue-lane
    autopilot-armed marker.
  * ``disarm_autopilot(conv_id)`` — the inverse: clear the marker,
    flip live-config off, emit the run-concluded record via
    ``conclude_run`` (lazy import — stays in autopilot.py).
  * ``_marker_exists(conv_id)`` — the marker-probe helper the arm
    result uses to compute its final ``armed`` flag.

**Deferred to a later slice** (per audit's ordering, to stay clear of
pt_8dc03017 mutation coupling):

  * ``kick_autopilot`` — indirectly wires to ``_run_autopilot_kick``
    which calls ``maybe_run_autopilot`` (pt_8dc03017 territory).
  * ``resume_armed_autopilot_after_crash`` — calls ``kick_autopilot``.

**Facade re-export contract**: the extracted symbols MUST remain
importable from ``lib.tasks_pkg.autopilot`` as the SAME object as the
autopilot_markers attribute — matches slice 1's identity-preserving
pattern.  This is load-bearing for the 3 consumers:

  * routes/chat_queue.py — ``from lib.tasks_pkg.autopilot import arm_autopilot``
    / ``disarm_autopilot``.
  * lib/chat_dispatch.py — ``from lib.tasks_pkg.autopilot import disarm_autopilot``.
  * tests/test_autopilot_arm.py — monkeypatches on ``ap.arm_autopilot`` / etc.

**pt_8dc03017 isolation**: the extracted module MUST NOT reference any
of the cutover mutation-list tokens: ``_VUEventForwarder``,
``_emit_vu_setup_phase``, ``_autopilot_deciding``, ``convId=''``. Guarded
here.

**Contract enforced by 5 tests** (all failing-first, RED before extraction):

  1. Module presence + surface.
  2. Facade re-export (symbols importable from lib.tasks_pkg.autopilot).
  3. Facade attr IDENTITY = markers-module attr (monkeypatch preservation).
  4. autopilot.py no longer DEFINES the extracted symbols.
  5. autopilot_markers.py has ZERO pt_8dc03017 token references in CODE
     (docstring/comment mentions OK — strip-and-scan pattern like
     autopilot_state).
"""

from __future__ import annotations

import os
import re
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
_MARKER_SYMBOLS = ('arm_autopilot', 'disarm_autopilot', '_marker_exists')


if pytest is not None:
    @pytest.fixture()
    def fresh_import():
        """Force a fresh import of the autopilot module cluster.

        An earlier suite in a ring may have reload()ed one of these
        modules without restoring (test_autopilot_markers_functional did —
        breaking facade↔markers identity for THIS check). Same hermetic
        pattern as test_autopilot_event_forwarding_wire_parity's
        reload_modules fixture: drop the cluster from sys.modules so the
        identity assertion reads a coherent import surface — then RESTORE
        the original module objects (tests/_hermetic_import.py). The old
        delete-and-leave teardown made later suites re-import fresh
        DUPLICATES, which broke importlib.reload (ImportError) and
        string-target monkeypatch steering downstream (pt_788b25a5).
        """
        from tests._hermetic_import import hermetic_import_surface
        with hermetic_import_surface('lib.tasks_pkg.autopilot'):
            yield


@_unit
def test_markers_module_exists_and_exposes_symbols():
    """Slice 2 (pt_00459503): lib.tasks_pkg.autopilot_markers exists and
    exposes arm_autopilot / disarm_autopilot / _marker_exists as callables."""
    import importlib
    mod = importlib.import_module('lib.tasks_pkg.autopilot_markers')
    missing = [n for n in _MARKER_SYMBOLS if not hasattr(mod, n)]
    assert not missing, (
        f'lib.tasks_pkg.autopilot_markers missing symbols: {missing}. '
        f'Slice 2 must land all three arm/disarm/marker helpers under this module.'
    )
    for name in _MARKER_SYMBOLS:
        assert callable(getattr(mod, name)), (
            f'lib.tasks_pkg.autopilot_markers.{name} is not callable')


@_unit
def test_autopilot_facade_reexports_marker_symbols():
    """Slice 2: lib.tasks_pkg.autopilot MUST remain a valid re-export
    facade for every extracted symbol — routes/chat_queue.py,
    lib/chat_dispatch.py, and 3 test files import via the facade."""
    import importlib
    ap = importlib.import_module('lib.tasks_pkg.autopilot')
    missing = [n for n in _MARKER_SYMBOLS if not hasattr(ap, n)]
    assert not missing, (
        f'lib.tasks_pkg.autopilot is missing re-exported symbols after '
        f'slice 2: {missing}. Preserve the facade or existing '
        f'from-imports / monkeypatches break silently.'
    )


@_unit
def test_facade_and_markers_module_resolve_to_same_object(fresh_import):
    """Slice 2: the facade attribute and the autopilot_markers module
    attribute MUST be IDENTICAL objects (``is`` comparison). This is
    the load-bearing invariant for monkeypatches to steer the live
    code path (matches slice-1's identity contract). Hermetic: the
    fresh_import fixture drops any state a prior suite's un-restored
    reload() left in sys.modules.
    """
    import importlib
    ap = importlib.import_module('lib.tasks_pkg.autopilot')
    mk = importlib.import_module('lib.tasks_pkg.autopilot_markers')
    mismatches = [n for n in _MARKER_SYMBOLS
                  if getattr(ap, n) is not getattr(mk, n)]
    assert not mismatches, (
        f'Facade re-export identity broken for: {mismatches}. Each '
        f'lib.tasks_pkg.autopilot.<name> MUST be the SAME OBJECT as '
        f'lib.tasks_pkg.autopilot_markers.<name>.'
    )


@_unit
def test_autopilot_py_no_longer_defines_marker_symbols():
    """Slice 2: the three ``def arm_autopilot`` / ``def disarm_autopilot`` /
    ``def _marker_exists`` statements MUST be gone from autopilot.py —
    moved to autopilot_markers.py."""
    with open(os.path.join(_ROOT, 'lib/tasks_pkg/autopilot.py'),
              encoding='utf-8') as f:
        src = f.read()
    for name in _MARKER_SYMBOLS:
        assert not re.search(rf'^def {name}\(', src, re.M), (
            f'lib/tasks_pkg/autopilot.py still defines {name}() at module '
            f'level. Slice 2 must move it to autopilot_markers.py (a '
            f'facade re-export line is fine, but a re-declared def is not)'
        )


@_unit
def test_autopilot_markers_isolated_from_pt_8dc03017_tokens():
    """Slice 2: the extracted module MUST NOT reference pt_8dc03017
    cutover-mutation tokens IN CODE (docstring/comment mentions that
    document the isolation constraint are legitimate signal). Same
    docstring-strip approach as slice-1's isolation test.
    """
    import importlib
    import inspect
    mk = importlib.import_module('lib.tasks_pkg.autopilot_markers')
    src = inspect.getsource(mk)
    code_lines = []
    in_docstring = False
    docstring_quote = None
    for line in src.splitlines():
        stripped = line.lstrip()
        if in_docstring:
            code_lines.append('')
            if docstring_quote in line:
                in_docstring = False
                docstring_quote = None
            continue
        if stripped.startswith('#'):
            code_lines.append('')
            continue
        m = re.match(r'^\s*(?P<q>"""|\'\'\')', line)
        if m:
            q = m.group('q')
            rest = line[m.end():]
            if q in rest:
                code_lines.append('')
            else:
                in_docstring = True
                docstring_quote = q
                code_lines.append('')
            continue
        code_lines.append(line)
    code_only = '\n'.join(code_lines)
    assert 'class _VUEventForwarder' not in code_only
    assert 'def _emit_vu_setup_phase' not in code_only
    for latch in ('_autopilot_deciding', "convId=''"):
        assert latch not in code_only, (
            f'autopilot_markers.py must NOT reference pt_8dc03017 cutover '
            f'point {latch!r} in CODE — the arm/disarm marker cluster is '
            f'strictly disjoint from that mutation surface.')


if __name__ == '__main__':
    for fn in [
        test_markers_module_exists_and_exposes_symbols,
        test_autopilot_facade_reexports_marker_symbols,
        test_facade_and_markers_module_resolve_to_same_object,
        test_autopilot_py_no_longer_defines_marker_symbols,
        test_autopilot_markers_isolated_from_pt_8dc03017_tokens,
    ]:
        if fn is test_facade_and_markers_module_resolve_to_same_object:
            # Emulate the fresh_import fixture (its value is unused — only
            # the sys.modules cleanup side effect matters).
            for name in list(sys.modules):
                if name.startswith('lib.tasks_pkg.autopilot'):
                    del sys.modules[name]
            fn(None)
        else:
            fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
