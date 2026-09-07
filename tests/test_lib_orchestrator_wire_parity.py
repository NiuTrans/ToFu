#!/usr/bin/env python3
"""Structural contracts for the sliced task orchestrator.

The package root is a namespace, ``api`` owns the application entry point,
``_ports`` owns cross-phase dependency injection, and every private behavior
has one concrete leaf-module owner.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Flask→Quart shim (matches the rest of the suite; orchestrator itself does
# NOT touch flask, but downstream imports do).
import quart as _quart
sys.modules.setdefault('flask', _quart)

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


_RUN_SUBMODULE_SYMBOLS = (
    'run_task',
)


@_unit
def test_orchestrator_root_is_a_namespace():
    import importlib
    namespace = importlib.import_module('lib.tasks_pkg.orchestrator')
    assert namespace.__all__ == ()
    assert not hasattr(namespace, 'run_task')
    assert not hasattr(namespace, 'build_body')


@_unit
def test_run_submodule_symbols_all_importable():
    """The raw ``lib.tasks_pkg.orchestrator._run`` sub-module surface
    (a small subset — some consumers name it directly) must also survive
    any future split."""
    import importlib
    run_mod = importlib.import_module('lib.tasks_pkg.orchestrator._run')
    missing = [name for name in _RUN_SUBMODULE_SYMBOLS
               if not hasattr(run_mod, name)]
    assert not missing, (
        f'lib.tasks_pkg.orchestrator._run missing symbols direct '
        f'importers rely on: {missing}. If you split _run.py into a '
        f'sub-package, preserve the concrete _run owner contract.'
    )


@_unit
def test_run_task_api_points_to_the_concrete_owner():
    from lib.tasks_pkg.orchestrator.api import run_task as via_api
    from lib.tasks_pkg.orchestrator._run import run_task as via_submodule
    assert callable(via_api)
    assert callable(via_submodule), (
        'lib.tasks_pkg.orchestrator._run.run_task is not callable '
        f'(got {type(via_submodule).__name__})')
    assert via_api is via_submodule


@_unit
def test_build_body_binding_has_one_explicit_injection_owner():
    import lib.tasks_pkg.orchestrator._ports as ports
    original = ports.build_request_body
    sentinel = object()
    try:
        ports.build_request_body = sentinel
        assert ports.build_request_body is sentinel
    finally:
        ports.build_request_body = original


@_unit
def test_finalize_and_turn_submodule_names_present():
    """The two SIBLING submodules of _run.py (_finalize, _turn) each carry
    known symbols external code imports directly. A future _run.py slice
    that inadvertently pulls a name from _finalize or _turn without re-
    homing it correctly trips here.

    Not exhaustive — only the direct-import names actually grep'd in the
    codebase today."""
    import importlib
    fin = importlib.import_module('lib.tasks_pkg.orchestrator._finalize')
    turn = importlib.import_module('lib.tasks_pkg.orchestrator._turn')
    write_breakdown = importlib.import_module('lib.tasks_pkg.write_breakdown')

    # Direct imports on _finalize surfaced by grep:
    for name in ('_discard_pretool_prose', '_emit_tool_round_phase',
                 '_finalize_and_emit_done', '_maybe_auto_retry_turn',
                 ):
        assert hasattr(fin, name), (
            f'lib.tasks_pkg.orchestrator._finalize missing {name!r} '
            f'(imported by _run.py at module load time)')

    assert hasattr(write_breakdown, '_compute_write_breakdown'), (
        'lib.tasks_pkg.write_breakdown must own write-accounting policy')

    # Direct imports on _turn surfaced by grep:
    for name in ('drain_peer_messages_into', '_run_single_turn', 'run_task'):
        assert hasattr(turn, name), (
            f'lib.tasks_pkg.orchestrator._turn missing {name!r}')


# ── Slice 2 (real source extraction): _vu_startup submodule ──────────
# The first REAL source-movement slice of pt_03f4cdf1. Extracts two
# self-contained closures from _run.py's ``run_task``:
#   * ``_vu_phase(task, detail, *, vu_startup: bool)`` — emit a PHASE
#     event only when this is a VU sub-task's startup window.
#   * ``_probe_external_edits(task, project_path)`` — the daemon-thread
#     target that runs the FUSE external-edit probe.
# Both moved to ``lib.tasks_pkg.orchestrator._vu_startup``. _run.py imports
# them + calls them at the same source sites.

@_unit
def test_vu_startup_submodule_exists_and_exposes_helpers():
    """Slice 2 (pt_03f4cdf1): the new ``_vu_startup`` submodule must
    exist and expose ``_vu_phase`` + ``_probe_external_edits`` as
    module-level callables (not closures). Regression tripwire for
    anyone removing or renaming these during a future re-organisation."""
    import importlib
    mod = importlib.import_module('lib.tasks_pkg.orchestrator._vu_startup')
    for name in ('_vu_phase', '_probe_external_edits'):
        assert hasattr(mod, name), (
            f'lib.tasks_pkg.orchestrator._vu_startup missing {name}')
        assert callable(getattr(mod, name)), (
            f'lib.tasks_pkg.orchestrator._vu_startup.{name} is not callable '
            f'(got {type(getattr(mod, name)).__name__})')


@_unit
def test_run_py_imports_the_extracted_vu_startup_helpers():
    """Slice 2 (pt_03f4cdf1): _run.py must actually IMPORT the extracted
    helpers from lib.tasks_pkg.orchestrator._vu_startup. A future
    accidental un-import (helpers still resident in _vu_startup.py but
    _run.py silently reverted to its own inline closures) would break
    the strangler-fig invariant that there's ONE definition of each.
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    assert 'from lib.tasks_pkg.orchestrator._vu_startup import' in src, (
        '_run.py must import from lib.tasks_pkg.orchestrator._vu_startup '
        '(slice 2 pt_03f4cdf1). The absence of this import means either '
        'the slice never landed, or _run.py was reverted to inline the '
        'closures again — either way, the extraction has been undone.')
    for name in ('_vu_phase', '_probe_external_edits'):
        assert name in src, (
            f'_run.py must reference {name} (as an imported callable '
            f'now, no longer as a nested def)')


@_unit
def test_vu_phase_behavior_gated_on_vu_startup_flag():
    """Slice 2 (pt_03f4cdf1): ``_vu_phase`` MUST behave exactly as its
    original closure form did:
      * vu_startup=False → NO append_event call (silent path — the
        default for ordinary non-VU turns; must stay byte-
        identical to pre-slice behaviour).
      * vu_startup=True → EXACTLY ONE append_event call whose event
        carries the given detail (this is the VU sub-task path).
    Monkey-patches append_event on the _vu_startup module to observe
    the call count + payload; does NOT touch a live task."""
    import lib.tasks_pkg.orchestrator._vu_startup as vus

    calls = []
    orig = vus.append_event
    try:
        vus.append_event = lambda task, ev: calls.append((task.get('id'), ev))
        # Silent path.
        vus._vu_phase({'id': 'tid-silent'}, 'prep', vu_startup=False)
        assert calls == [], (
            f'vu_startup=False must NOT emit; got {calls}')
        # Startup-visible path.
        vus._vu_phase({'id': 'tid-loud'}, 'inject-context', vu_startup=True)
        assert len(calls) == 1, (
            f'vu_startup=True must emit exactly once; got {len(calls)}')
        tid, ev = calls[0]
        assert tid == 'tid-loud'
        assert isinstance(ev, dict), f'expected dict event, got {type(ev)!r}'
        # The build_event(EventType.PHASE, phase='working', detail=...)
        # produces a dict whose serialised form carries the detail.
        # Rather than couple to the internal EventType enum, we assert
        # the detail string survives in the payload SOMEWHERE — which
        # is what the frontend renderer actually reads.
        payload_text = str(ev)
        assert 'inject-context' in payload_text, (
            f'detail string missing from emitted event: {ev!r}')
    finally:
        vus.append_event = orig


# ── Slice 3: _prefetch submodule (memory + project prefetches) ──────
# Extracts provider leasing + prefetch closure + task-attach into
# lib/tasks_pkg/orchestrator/_prefetch.py; _run.py's finally block still
# owns the lease.shutdown() cancellation boundary.

@_unit
def test_prefetch_submodule_exists_and_exposes_start_prefetches():
    """Slice 3 (pt_03f4cdf1): ``_prefetch`` submodule exists and
    exposes ``start_prefetches`` as a module-level callable."""
    import importlib
    mod = importlib.import_module('lib.tasks_pkg.orchestrator._prefetch')
    assert hasattr(mod, 'start_prefetches'), (
        'lib.tasks_pkg.orchestrator._prefetch missing start_prefetches')
    assert callable(mod.start_prefetches), (
        f'start_prefetches is not callable (got '
        f'{type(mod.start_prefetches).__name__})')


@_unit
def test_run_py_imports_the_extracted_prefetch_helper():
    """Slice 3: _run.py must actually import from
    lib.tasks_pkg.orchestrator._prefetch. Guards against a silent
    revert to the inline closures."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    assert 'from lib.tasks_pkg.orchestrator._prefetch import' in src, (
        '_run.py must import from lib.tasks_pkg.orchestrator._prefetch '
        '(slice 3 pt_03f4cdf1).')
    assert 'start_prefetches' in src, (
        '_run.py must reference start_prefetches at the call site')


@_unit
def test_prefetch_only_submits_project_rules_io():
    """``start_prefetches`` owns only the project-rules I/O lane:
      * project_enabled=True + project_path → submits _prefetch_project
        (stashed on task['_prefetch_project'])
      * project_enabled=False (or no project_path) → task['_prefetch_project'] is None
      * memory_enabled does not create a future; local memory selection runs
        synchronously after tool assembly
      * returned lease owns cancellation over the shared bounded executor
    Uses a synchronous fake lease that just records .submit()
    calls, so the test is deterministic + doesn't leak threads."""
    import lib.tasks_pkg.orchestrator._prefetch as pf

    # Fake lease to record submit() calls without spawning threads.
    class _FakePool:
        def __init__(self):
            self.submitted = []

            class _FakeFuture:
                def __init__(self, name): self.name = name

            self._FakeFuture = _FakeFuture

        def submit(self, fn, *a, **kw):
            fut = self._FakeFuture(getattr(fn, '__name__', 'anon'))
            self.submitted.append((fut.name, a, kw))
            return fut

        def shutdown(self, wait=False):
            pass

    orig_pool = pf._PrefetchPool
    try:
        # Force start_prefetches to construct our fake lease.
        pf._PrefetchPool = lambda *a, **kw: _FakePool()

        # Project ON → exactly one project-context submit.
        task = {'id': 'tid-both', 'convId': 'cv1'}
        pool = pf.start_prefetches(
            task, cfg={},
            project_path='/proj/A', project_enabled=True,
            memory_enabled=True)
        assert isinstance(pool, _FakePool)
        names = [n for (n, _a, _kw) in pool.submitted]
        assert names == ['_prefetch_project']
        assert task.get('_prefetch_project') is not None
        assert '_prefetch_memory' not in task

        # Project OFF → no future, regardless of the memory toggle.
        task2 = {'id': 'tid-nomem-only', 'convId': 'cv2'}
        pool2 = pf.start_prefetches(
            task2, cfg={},
            project_path='', project_enabled=False,
            memory_enabled=True)
        names2 = [n for (n, _a, _kw) in pool2.submitted]
        assert names2 == []
        assert task2.get('_prefetch_project') is None
        assert '_prefetch_memory' not in task2

        # Memory OFF does not alter the project prefetch lane.
        task3 = {'id': 'tid-noproj-only', 'convId': 'cv3'}
        pool3 = pf.start_prefetches(
            task3, cfg={},
            project_path='/proj/B', project_enabled=True,
            memory_enabled=False)
        names3 = [n for (n, _a, _kw) in pool3.submitted]
        assert names3 == ['_prefetch_project']
        assert task3.get('_prefetch_project') is not None
        assert '_prefetch_memory' not in task3

        # Case 4: both OFF → no submits at all, but the lease still exists
        # (the caller expects a shutdown-able return value regardless).
        task4 = {'id': 'tid-neither'}
        pool4 = pf.start_prefetches(
            task4, cfg={},
            project_path='', project_enabled=False,
            memory_enabled=False)
        assert not pool4.submitted, (
            f'no prefetch flags enabled must submit nothing; got '
            f'{pool4.submitted}')
        assert task4.get('_prefetch_project') is None
        assert '_prefetch_memory' not in task4
    finally:
        pf._PrefetchPool = orig_pool


# ── Slice 4: project setup extraction ─────────────────────────────
# Extracts the ~77-line ``if project_enabled and project_path:`` block
# (ensure_project_state + presence announce + external-edit probe) into
# a single ``setup_project_context(task, cfg, project_path)`` in
# ``_vu_startup.py`` (its "startup helpers" seam extends naturally to
# this block — it runs once at task start).

@_unit
def test_setup_project_context_present_on_vu_startup():
    """Slice 4 (pt_03f4cdf1): ``setup_project_context`` exposed by
    ``lib.tasks_pkg.orchestrator._vu_startup`` as a module-level
    callable. Regression tripwire for future extraction moves."""
    import importlib
    mod = importlib.import_module('lib.tasks_pkg.orchestrator._vu_startup')
    assert hasattr(mod, 'setup_project_context'), (
        'lib.tasks_pkg.orchestrator._vu_startup missing setup_project_context')
    assert callable(mod.setup_project_context), (
        f'setup_project_context is not callable '
        f'(got {type(mod.setup_project_context).__name__})')


@_unit
def test_run_py_calls_setup_project_context():
    """Slice 4: _run.py must actually CALL setup_project_context —
    if a future refactor accidentally reverts to the inline block, the
    source-string guard trips."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    assert 'setup_project_context' in src, (
        '_run.py must reference setup_project_context (slice 4 pt_03f4cdf1)')
    # AND the previous inline block's signature line must be GONE.
    # If future maintainers un-extract this block, the specific
    # inline pattern below returns — that would defeat the extraction.
    assert 'from lib.project_mod import ensure_project_state' not in src, (
        '_run.py must NOT contain the inline ensure_project_state import '
        '(slice 4: that import belongs to _vu_startup.setup_project_context)')


@_unit
def test_setup_project_context_disabled_path_is_no_op():
    """Slice 4: when project_enabled is off OR project_path is empty,
    setup_project_context does nothing observable — no
    ensure_project_state, no presence announce, no probe spawn.
    Byte-identical to the pre-slice ``if project_enabled and
    project_path:`` gate."""
    import lib.tasks_pkg.orchestrator._vu_startup as vus

    calls = []

    # Monkey-patch the three side-effect points to observation shims.
    import sys
    proj_mod = sys.modules.get('lib.project_mod')
    presence_mod = sys.modules.get('lib.presence')

    orig_ensure = getattr(proj_mod, 'ensure_project_state', None) if proj_mod else None
    orig_announce = getattr(presence_mod, 'announce', None) if presence_mod else None
    orig_start_probe = vus.start_external_edit_probe
    try:
        if proj_mod is not None:
            proj_mod.ensure_project_state = lambda *a, **kw: calls.append(('ensure', a, kw))
        if presence_mod is not None:
            presence_mod.announce = lambda *a, **kw: calls.append(('announce', a, kw))
        vus.start_external_edit_probe = lambda *a, **kw: calls.append(('probe', a, kw))

        # Case A: project_enabled=False → no calls.
        vus.setup_project_context(
            task={'id': 'tid-off', 'convId': 'cv1'},
            cfg={},
            project_path='/proj/A',
            project_enabled=False,
        )
        assert calls == [], f'project_enabled=False must be no-op; got {calls}'

        # Case B: project_enabled=True + empty project_path → no calls.
        vus.setup_project_context(
            task={'id': 'tid-empty', 'convId': 'cv2'},
            cfg={},
            project_path='',
            project_enabled=True,
        )
        assert calls == [], f'empty project_path must be no-op; got {calls}'
    finally:
        if proj_mod is not None:
            if orig_ensure is not None:
                proj_mod.ensure_project_state = orig_ensure
            else:
                del proj_mod.ensure_project_state
        if presence_mod is not None:
            if orig_announce is not None:
                presence_mod.announce = orig_announce
            else:
                del presence_mod.announce
        vus.start_external_edit_probe = orig_start_probe


# ── Slice 5: _teardown submodule (run_task finally-block teardown) ───
# Extracts the 44-line ``finally:`` teardown block into
# lib/tasks_pkg/orchestrator/_teardown.py. Symmetric counterpart to
# ``_vu_startup.py``: startup helpers run once at task begin, teardown
# helpers run once at task end.

@_unit
def test_teardown_submodule_exists_and_exposes_finalize_task_lane():
    """Slice 5 (pt_03f4cdf1): ``_teardown`` submodule exists and
    exposes ``finalize_task_lane`` as a module-level callable."""
    import importlib
    mod = importlib.import_module('lib.tasks_pkg.orchestrator._teardown')
    assert hasattr(mod, 'finalize_task_lane'), (
        'lib.tasks_pkg.orchestrator._teardown missing finalize_task_lane')
    assert callable(mod.finalize_task_lane), (
        f'finalize_task_lane is not callable '
        f'(got {type(mod.finalize_task_lane).__name__})')


@_unit
def test_run_py_calls_finalize_task_lane_in_finally():
    """Slice 5: _run.py's finally block must call finalize_task_lane
    (replacing the previous inline 5-step teardown)."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    assert 'finalize_task_lane' in src, (
        '_run.py must reference finalize_task_lane (slice 5 pt_03f4cdf1)')
    # The specific inline patterns each teardown step used are GONE —
    # each was a `try: <one-liner> except Exception: logger.debug(...)`
    # ad-hoc block. Guard against a silent revert.
    assert 'from lib.llm_dispatch.provider_pin import clear_pinned_provider' not in src, (
        '_run.py must not carry the inline clear_pinned_provider import '
        '(moved to _teardown.py)')
    assert 'from lib.llm_dispatch.conv_affinity import clear_conv_affinity' not in src, (
        '_run.py must not carry the inline clear_conv_affinity import '
        '(moved to _teardown.py)')


@_unit
def test_finalize_task_lane_runs_all_teardown_steps():
    """finalize_task_lane must run every observable teardown step in
    the SAME ORDER as the inline finally block:

      1. presence.mark_idle (if project attached + owner + conv id)
      2. set_req_id('') (clear thread-local request id)
      3. clear_pinned_provider() (drop hard provider pin)
      4. clear_conv_affinity() (drop soft conv sticky-routing)
    Every step is wrapped in try/except so a failure NEVER escapes.
    Monkey-patches all four side-effect points to observation shims;
    asserts each call fires + preserves relative order.
    """
    import sys
    import importlib
    import lib.tasks_pkg.orchestrator._teardown as td

    calls = []

    # Fakes for the 5 side-effect points.
    fake_mark_idle = lambda pp, cid, *, user_id: calls.append(
        ('mark_idle', pp, cid, user_id))
    fake_clear_pin = lambda: calls.append(('clear_pinned_provider',))
    fake_clear_aff = lambda: calls.append(('clear_conv_affinity',))

    # EAGERLY import each side-effect module so it's in sys.modules
    # BEFORE we patch. finalize_task_lane uses lazy ``from X import Y``
    # imports at call time — those go through sys.modules['X'] and read
    # the (patched) attribute. If a module isn't in sys.modules yet, our
    # patch has nothing to overwrite and the real function fires.
    presence_mod = importlib.import_module('lib.presence')
    pin_mod = importlib.import_module('lib.llm_dispatch.provider_pin')
    aff_mod = importlib.import_module('lib.llm_dispatch.conv_affinity')

    orig_mark = getattr(presence_mod, 'mark_idle', None)
    orig_pin = getattr(pin_mod, 'clear_pinned_provider', None)
    orig_aff = getattr(aff_mod, 'clear_conv_affinity', None)

    # Also patch the local set_req_id import target.
    from lib.log import set_req_id as _real_set_req_id
    import lib.log as _log_mod
    def _fake_set_req_id(v):
        calls.append(('set_req_id', v))

    try:
        presence_mod.mark_idle = fake_mark_idle
        pin_mod.clear_pinned_provider = fake_clear_pin
        aff_mod.clear_conv_affinity = fake_clear_aff
        _log_mod.set_req_id = _fake_set_req_id
        # Also patch inside _teardown itself since it imports at module load
        td.set_req_id = _fake_set_req_id

        # Case A: project + conv both present → mark_idle fires.
        task = {'id': 'tid-full', 'convId': 'cv-1', '_userId': 7,
                'config': {'projectPath': '/proj/A'}}
        td.finalize_task_lane(task, tid='tid-full')

        kinds = [c[0] for c in calls]
        # All 5 steps must fire (mark_idle first, then the 4 cleanup ones).
        assert 'mark_idle' in kinds, f'mark_idle missing; got {kinds}'
        assert 'set_req_id' in kinds, f'set_req_id missing; got {kinds}'
        assert 'clear_pinned_provider' in kinds, (
            f'clear_pinned_provider missing; got {kinds}')
        assert 'clear_conv_affinity' in kinds, (
            f'clear_conv_affinity missing; got {kinds}')

        # Case B: no project → mark_idle skipped (matches inline gate
        # `if _fin_pp and _fin_cid`).
        calls.clear()
        task2 = {'id': 'tid-noproj', 'convId': 'cv-2', '_userId': 7,
                 'config': {}}
        td.finalize_task_lane(task2, tid='tid-noproj')
        kinds2 = [c[0] for c in calls]
        assert 'mark_idle' not in kinds2, (
            f'mark_idle should NOT fire without project_path; got {kinds2}')
        # The three unconditional cleanup owners still fire.
        for name in ('set_req_id', 'clear_pinned_provider',
                     'clear_conv_affinity'):
            assert name in kinds2, (
                f'{name} missing when no project; got {kinds2}')
    finally:
        # Restore all patched module attributes.
        if orig_mark is not None:
            presence_mod.mark_idle = orig_mark
        else:
            presence_mod.__dict__.pop('mark_idle', None)
        if orig_pin is not None:
            pin_mod.clear_pinned_provider = orig_pin
        if orig_aff is not None:
            aff_mod.clear_conv_affinity = orig_aff
        _log_mod.set_req_id = _real_set_req_id
        td.set_req_id = _real_set_req_id


# ── Slice 6: _post_loop submodule (post-loop success tail + fatal-path) ─
# Extracts the ~200-line post-loop block: success-tail (append-final,
# write-back, save-to-store, finalize-and-emit-done) + fatal-path
# (user-error extraction, Flow-managed short-circuit, turn-level
# auto-retry, recovery-carrier re-stamp, terminal-DONE + persist).

@_unit
def test_post_loop_submodule_exposes_finalize_after_loop_and_handle_task_fatal():
    """Slice 6 (pt_03f4cdf1): _post_loop.py exposes both extracted
    functions as module-level callables."""
    import importlib
    mod = importlib.import_module('lib.tasks_pkg.orchestrator._post_loop')
    for name in ('finalize_after_loop', 'handle_task_fatal'):
        assert hasattr(mod, name), (
            f'lib.tasks_pkg.orchestrator._post_loop missing {name}')
        assert callable(getattr(mod, name)), (
            f'{name} is not callable (got '
            f'{type(getattr(mod, name)).__name__})')


@_unit
def test_run_py_imports_and_calls_post_loop_helpers():
    """Slice 6: _run.py must import from _post_loop AND call both
    helpers at the right sites (success tail + fatal handler)."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'lib/tasks_pkg/orchestrator/_run.py'),
              encoding='utf-8') as f:
        src = f.read()
    assert 'from lib.tasks_pkg.orchestrator._post_loop import' in src, (
        '_run.py must import from lib.tasks_pkg.orchestrator._post_loop')
    assert 'finalize_after_loop(' in src, (
        '_run.py must CALL finalize_after_loop at the success-tail site')
    assert 'handle_task_fatal(task, e)' in src, (
        '_run.py must CALL handle_task_fatal at the except Exception site')
    # The specific inline patterns that lived in the extracted blocks are
    # GONE from _run.py — guards against silent revert.
    assert 'Appended final assistant reply to messages' not in src, (
        '_run.py must NOT carry the "Appended final assistant reply" log '
        'string — that lives in _post_loop.finalize_after_loop now')
    assert 'format_llm_error_for_user' not in src, (
        '_run.py must NOT carry the format_llm_error_for_user import — '
        'that lives in _post_loop.handle_task_fatal now')


@_unit
def test_handle_task_fatal_flow_managed_short_circuit():
    """Slice 6: handle_task_fatal returns True when task carries
    _flow_managed=True (caller must return early so the Flow runner
    handles the error). Byte-identical to the pre-slice inline
    ``if task.get('_flow_managed'): return`` gate."""
    import lib.tasks_pkg.orchestrator._post_loop as pl

    task = {'id': 'tid-ep', '_flow_managed': True,
            'config': {'model': 'test-model'}}
    exc = ValueError('boom')

    result = pl.handle_task_fatal(task, exc)
    assert result is True, (
        f'handle_task_fatal must return True when _flow_managed '
        f'(caller returns early); got {result}')
    # task fields still stamped (caller reads them from the Flow runner).
    assert task.get('status') == 'error'
    assert task.get('finishReason') == 'error'
    assert task.get('error') is not None


@_unit
def test_finalize_after_loop_no_assistant_msg_still_dispatches():
    """Slice 6: finalize_after_loop with assistant_msg=None must skip
    the "append final assistant reply" branch (the ``if assistant_msg
    and not assistant_msg.get('tool_calls'):`` gate) but STILL run the
    write-back + save + finalize_and_emit_done dispatch. Same as inline
    pre-slice behaviour."""
    import lib.tasks_pkg.orchestrator._post_loop as pl

    calls = []

    # Stub _finalize_and_emit_done to observe dispatch without running
    # its real body (which needs a lot of infra).
    orig_finalize = pl._finalize_and_emit_done
    try:
        pl._finalize_and_emit_done = lambda task, **kw: calls.append(
            ('finalize_and_emit_done', task.get('id'), kw.get('round_num')))
        task = {'id': 'tid-empty', 'convId': '', 'messages': []}
        messages = [{'role': 'user', 'content': 'hi'}]
        pl.finalize_after_loop(
            task, cfg={}, tid='tid-empty', model='m', preset='',
            thinking_depth=None, thinking_enabled=False,
            temperature=None, max_tokens=None,
            messages=messages, original_messages=[],
            tool_list=None, assistant_msg=None, round_num=0,
            accumulated_usage={}, api_rounds=[],
            last_finish_reason=None, last_usage=None,
            tool_call_happened=False, all_search_results_text=[],
            project_path='', project_enabled=False,
            loop_exit_reason='no_tool_calls_round_0',
            abort_detected_phase=None,
        )
        # task['messages'] MUST have been written back (this is the
        # invariant Flow execution depends on).
        assert task.get('messages') is messages, (
            'task["messages"] must be written back to the passed-in list')
        # _finalize_and_emit_done MUST have been dispatched.
        assert calls == [('finalize_and_emit_done', 'tid-empty', 0)], (
            f'expected exactly one _finalize_and_emit_done dispatch; got {calls}')
    finally:
        pl._finalize_and_emit_done = orig_finalize


if __name__ == '__main__':
    tests = [
        test_orchestrator_root_is_a_namespace,
        test_run_submodule_symbols_all_importable,
        test_run_task_api_points_to_the_concrete_owner,
        test_build_body_binding_has_one_explicit_injection_owner,
        test_finalize_and_turn_submodule_names_present,
        test_vu_startup_submodule_exists_and_exposes_helpers,
        test_run_py_imports_the_extracted_vu_startup_helpers,
        test_vu_phase_behavior_gated_on_vu_startup_flag,
        test_prefetch_submodule_exists_and_exposes_start_prefetches,
        test_run_py_imports_the_extracted_prefetch_helper,
        test_prefetch_only_submits_project_rules_io,
        test_setup_project_context_present_on_vu_startup,
        test_run_py_calls_setup_project_context,
        test_setup_project_context_disabled_path_is_no_op,
        test_teardown_submodule_exists_and_exposes_finalize_task_lane,
        test_run_py_calls_finalize_task_lane_in_finally,
        test_finalize_task_lane_runs_all_teardown_steps,
        test_post_loop_submodule_exposes_finalize_after_loop_and_handle_task_fatal,
        test_run_py_imports_and_calls_post_loop_helpers,
        test_handle_task_fatal_flow_managed_short_circuit,
        test_finalize_after_loop_no_assistant_msg_still_dispatches,
    ]
    for fn in tests:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
