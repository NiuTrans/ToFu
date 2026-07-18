"""Shared, xdist-SAFE negative-control (NC) harness.

## Why this exists

A large family of NC tests prove a load-bearing source line by NEUTERING it and
asserting the behavioral test then FAILS. The original mechanism
(``_patch_restore``) did this by **writing a neutered variant of the shipped
source file to disk**, ``importlib.reload``-ing the canonical module, running
the assertion, then restoring the file in a ``finally``. That is fundamentally
hostile to ``pytest -n`` (xdist):

  * **On-disk poisoning.** If the test is killed mid-patch (a per-test timeout,
    a worker crash, KeyboardInterrupt), the ``finally`` never runs and the
    SHIPPED source is left NEUTERED — cascading into every later importer for
    the rest of the session (and leaving the working tree dirty).
  * **Reload cross-pollution.** ``importlib.reload`` swaps the canonical module
    object in ``sys.modules``; any OTHER already-imported module holding a
    reference — or a sibling NC test reloading a related module on the same
    worker — sees a half-updated graph and fails nondeterministically.

## What this does instead

``neutered_source`` / ``patch_restore`` compile the neutered variant IN MEMORY
into a throwaway module object and swap THAT into ``sys.modules`` under the
canonical dotted name for the duration of ``run()`` only, then restore the
original module object. Consequences:

  * The shipped file is opened **read-only** — there is nothing to restore, so a
    crash can never poison the tree.
  * The canonical module object is **never mutated** (no reload) — the original
    is put back verbatim, so no other module's cached references are disturbed.
  * A function defined in the throwaway module closes over THAT module's dict,
    so its intra-module calls resolve to the neutered siblings; and any module
    that lazily does ``from <name> import <sym>`` INSIDE a function body
    resolves the swapped module via ``sys.modules`` at call time — so
    cross-module neuters (e.g. dispatch→board, board→feed) work too, with no
    reload anywhere.

## Usage

Drop-in replacement for the old per-file ``_patch_restore(path, old, new, run)``
— same signature. Migrate a test file by:

  1. ``from tests._nc_harness import patch_restore as _patch_restore``
     (delete the file's local ``def _patch_restore``).
  2. Inside each ``run()`` closure, DELETE the ``importlib.reload(...)`` line(s)
     — the neutered module is already live in ``sys.modules``; a reload would
     re-read the (un-neutered) file and defeat the neuter.
  3. Delete the trailing post-call ``importlib.reload(...)`` "un-poison" lines —
     the harness restores the original module on ``with`` exit; nothing to undo.

The module name is derived from the source path relative to the repo root, so
callers keep passing the same ``_BOARD_SRC`` / ``_FEED_SRC`` constants.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))


@contextlib.contextmanager
def _neutralized_route_registration():
    """Neutralize Flask/Quart Blueprint route registration for the duration of a
    module re-exec.

    A route module (e.g. ``routes/conversations.py``) runs ``@bp.route(...)`` at
    IMPORT time. Re-execing its source to build the neutered variant would
    (a) raise ``AssertionError: setup method 'route' can no longer be called``
    because the shared blueprint is already registered onto the app, and
    (b) MUTATE that shared blueprint's deferred-function list. We no-op the
    registration seam (``_check_setup_finished`` → allow, ``add_url_rule`` →
    drop) ONLY during the exec, then restore it verbatim. The neutered module's
    pure functions still compile normally; only their route WIRING is skipped
    (which the NC never needs — it calls the functions directly). Best-effort:
    if Flask isn't importable the exec proceeds unchanged (pure modules).
    """
    patched = []
    for _mod_path, _cls_name in (('flask.sansio.scaffold', 'Scaffold'),
                                 ('flask.sansio.blueprints', 'Blueprint')):
        try:
            _m = importlib.import_module(_mod_path)
            _cls = getattr(_m, _cls_name)
            # Only patch the attribute if the class DEFINES it (not inherits) —
            # Blueprint overrides Scaffold's _check_setup_finished, and the
            # bound decorator resolves via the instance's own class.
            if '_check_setup_finished' in _cls.__dict__:
                _orig = _cls.__dict__['_check_setup_finished']
                _cls._check_setup_finished = lambda self, f_name: None
                patched.append((_cls, '_check_setup_finished', _orig))
            if 'add_url_rule' in _cls.__dict__:
                _orig_add = _cls.__dict__['add_url_rule']
                _cls.add_url_rule = lambda self, *a, **k: None
                patched.append((_cls, 'add_url_rule', _orig_add))
        except Exception:
            pass
    try:
        yield
    finally:
        for obj, name, orig in patched:
            setattr(obj, name, orig)


def module_name_from_path(src_path: str) -> str:
    """Map an absolute/relative ``lib/foo/bar.py`` path to its dotted module
    name ``lib.foo.bar`` (relative to the repo root)."""
    rel = os.path.relpath(os.path.abspath(src_path), _ROOT)
    if rel.endswith('.py'):
        rel = rel[:-3]
    return rel.replace(os.sep, '.')


@contextlib.contextmanager
def neutered_source(src_path: str, old: str, new: str, count: int = 1):
    """Context manager: for its duration, the module defined by ``src_path`` is
    replaced in ``sys.modules`` by a compile of its source with ``old`` → ``new``.

    The shipped file is READ-ONLY. Yields the neutered module object (so a test
    can call the neutered module's OWN functions directly if it wants). On exit
    the original ``sys.modules`` entry is restored verbatim.
    """
    module_name = module_name_from_path(src_path)
    with open(src_path, encoding='utf-8') as f:
        original_src = f.read()
    assert old in original_src, f'NC anchor not found in {src_path}: {old[:70]!r}'
    patched_src = original_src.replace(old, new, count)
    assert patched_src != original_src, 'NC replacement was a no-op'

    # Ensure the CANONICAL module is fully imported BEFORE we build the neutered
    # variant. Two reasons: (1) a package member with a circular parent-package
    # import (e.g. compaction/__init__ does ``from ._persist import <sym>``) must
    # be satisfiable while we re-exec the child — if the child were being imported
    # for the FIRST time via our exec, the parent __init__ would re-run against the
    # half-built module and raise ImportError; importing canonically first leaves
    # the parent fully cached. (2) it gives us the original module dict to seed the
    # neutered namespace from (below), so those circular ``from <this> import`` land.
    if module_name not in sys.modules:
        with contextlib.suppress(Exception):
            importlib.import_module(module_name)

    saved = sys.modules.get(module_name)
    spec = importlib.util.spec_from_file_location(module_name, src_path)
    mod = importlib.util.module_from_spec(spec)
    code = compile(patched_src, src_path, 'exec')

    # Pre-seed the fresh module's namespace with the ALREADY-IMPORTED original's
    # globals (when present). Rationale: re-execing the source can re-trigger the
    # parent package __init__ (e.g. ``lib.tasks_pkg.compaction/__init__`` does
    # ``from ._persist import _generate_web_search_preview``). During exec the
    # neutered module is only PARTIALLY built, so that circular ``from <this> import
    # <sym>`` would raise ImportError for a symbol defined later in the file. Seeding
    # the original's globals first means the circular import resolves against the
    # already-defined symbol; the exec then overwrites every name with its neutered
    # value, so the neuter still wins. Harmless for modules with no circular init.
    if saved is not None:
        mod.__dict__.update({
            k: v for k, v in saved.__dict__.items()
            if not k.startswith('__') or k in ('__doc__',)
        })

    # Also rebind the PARENT package's attribute for the child. ``import
    # a.b.c as w`` binds ``w`` via attribute access on the parent package
    # (``a.b``.c), NOT via ``sys.modules['a.b.c']`` alone — so a fresh
    # ``import ... as pb`` inside a run() closure would otherwise still see the
    # canonical child. Swap both, restore both.
    parent_name, _, child_attr = module_name.rpartition('.')
    parent_mod = sys.modules.get(parent_name) if parent_name else None
    had_attr = parent_mod is not None and hasattr(parent_mod, child_attr)
    saved_attr = getattr(parent_mod, child_attr, None) if parent_mod is not None else None

    sys.modules[module_name] = mod
    if parent_mod is not None:
        setattr(parent_mod, child_attr, mod)
    try:
        with _neutralized_route_registration():
            exec(code, mod.__dict__)
        yield mod
    finally:
        if saved is not None:
            sys.modules[module_name] = saved
        else:
            sys.modules.pop(module_name, None)
        if parent_mod is not None:
            if had_attr:
                setattr(parent_mod, child_attr, saved_attr)
            else:
                with contextlib.suppress(AttributeError):
                    delattr(parent_mod, child_attr)


def patch_restore(path: str, old: str, new: str, run, count: int = 1):
    """Drop-in for the legacy ``_patch_restore(path, old, new, run)``.

    Neuters the module defined by ``path`` in ``sys.modules`` (NOT on disk),
    calls ``run()`` (which asserts the behavioral property is now BROKEN), then
    restores the original module. ``run`` is called with the neutered module as
    its single positional arg IF it accepts one; otherwise with no args (so the
    common ``def run():`` closures using canonical imports keep working).
    """
    with neutered_source(path, old, new, count) as mod:
        try:
            run(mod)
        except TypeError as e:
            # ``run`` takes no positional args → call it bare. Guard against
            # masking a real TypeError raised INSIDE a 0-arg run() by only
            # retrying when the signature itself rejected the argument.
            if 'positional argument' in str(e) or 'takes 0' in str(e):
                run()
            else:
                raise
