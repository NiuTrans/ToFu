"""tests/_hermetic_import.py — save/restore wrapper for hermetic-import fixtures.

WHY THIS EXISTS (the pt_788b25a5 batch-pollution incident, 2026-08-02)

The naive "hermetic" fixture pattern — ``del sys.modules[name]`` for a
prefix on setup AND teardown — leaves the pytest session with NO
``sys.modules`` entry for the deleted modules, while every test file
still holds the ORIGINAL module object from its collection-time import.
Two casualty families follow downstream:

  * ``importlib.reload(mod)`` requires the ``sys.modules`` entry →
    ``ImportError: module ... not in sys.modules``
    (test_autopilot_markers_functional).
  * A string-target ``monkeypatch.setattr('pkg.mod.attr', ...)``
    RE-IMPORTS a fresh duplicate module and patches THAT, while the
    code under test still runs on the collection-time original — the
    patch silently misses (test_autopilot_yield_not_destroy).

The fix is not to stop giving wire-parity tests a fresh import surface
— it is to PUT THE ORIGINALS BACK afterwards, including the parent
package's child attributes, which a fresh import rebinds to the
duplicates (``import a.b.c`` binds ``c`` on ``a.b`` by ATTRIBUTE, not
via sys.modules alone — swap both, restore both).
"""

from __future__ import annotations

import contextlib
import sys


@contextlib.contextmanager
def hermetic_import_surface(prefix: str):
    """Yield a fresh-import surface for ``prefix``; restore originals after.

    Setup: snapshot + delete every ``sys.modules`` entry matching
    ``prefix`` — inside the window the test re-imports a coherent,
    duplicate-free surface (facade↔leaf identity assertions read fresh
    bindings).  Teardown: drop whatever the window imported, then
    RESTORE the original module objects and the parent-package child
    attributes, so the rest of the session sees the same
    single-module-instance world it had before the test ran.
    """
    saved = {name: mod for name, mod in list(sys.modules.items())
             if name.startswith(prefix)}
    for name in saved:
        del sys.modules[name]
    try:
        yield
    finally:
        for name in list(sys.modules):
            if name.startswith(prefix):
                del sys.modules[name]
        sys.modules.update(saved)
        for name, mod in saved.items():
            parent_name, _, child_attr = name.rpartition('.')
            if not parent_name:
                continue
            parent = sys.modules.get(parent_name)
            if parent is not None and getattr(parent, child_attr, None) is not mod:
                setattr(parent, child_attr, mod)
