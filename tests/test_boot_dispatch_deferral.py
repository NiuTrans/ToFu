#!/usr/bin/env python3
"""Regression: killed-recovery / autopilot-resume must NOT spawn carriers on
the STARTUP event loop.

WHY (the 297s-boot incident, 2026-07-11)
----------------------------------------
``recover_stale_tasks_on_startup`` runs on MainThread INSIDE
``asyncio.run(_startup())``. Its inline billed re-dispatch called
``spawn_task``, which — because a loop was running — scheduled the carrier as
``asyncio.ensure_future`` ON the startup loop. ``asyncio.run`` then would not
return until that carrier's whole run finished (a ~4.5-minute LLM+swarm turn),
so "Ready" printed 297s late, a ``^C`` pressed meanwhile only queued (nothing
awaited the shutdown flag until ``_serve()`` existed), and PG teardown then
raced the still-live carrier → the ``database system is shutting down`` cascade.

THE FIX
-------
The synchronous DB cleanup stays in ``recover_stale_tasks_on_startup``, but the
BILLED dispatch is split into ``run_deferred_boot_dispatch`` which the server
invokes from the SERVING loop after ``hypercorn_serve`` starts, gated on a
``should_continue`` predicate (so a shutdown during startup skips it entirely).

Tests (pure — the dispatch helpers are monkeypatched to record calls, so no DB
and no LLM are needed):
  1. ``dispatch=False`` returns a descriptor and does NOT call the billed
     dispatchers. ★ THE FIX (nothing fires on the startup loop).
  2. ``run_deferred_boot_dispatch`` DOES call both dispatchers with the
     descriptor's data. ★ the deferred work still happens post-serving.
  3. ``should_continue()==False`` skips dispatch entirely. ★ ^C-during-boot.
  4. NC: force ``dispatch=True`` (legacy) → the billed dispatch fires inline
     again (proves the deferral is load-bearing, not incidental).
  5. DEFAULT-OFF gate: with ``TOFU_BOOT_AUTO_DISPATCH`` unset, run_deferred_
     boot_dispatch does NOT auto-execute either billed lane (crash recovery is
     display-only); setting the env var re-enables it. ★ the owner requirement.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _patch_recovery_internals(monkeypatch, *, killed=('cX',), storm=False):
    """Stub the SYNCHRONOUS DB half so recover_stale_tasks_on_startup returns a
    fixed descriptor without a real DB, and record deferred-dispatch calls."""
    from lib.tasks_pkg import manager

    # Make the body's DB work a no-op that jumps straight to the return/dispatch
    # branch by monkeypatching get_thread_db to raise — the outer try/except
    # would swallow it and return None, which is NOT what we want. Instead we
    # test run_deferred_boot_dispatch directly (units 2-4) and, for unit 1,
    # patch the deferred dispatcher and drive a minimal descriptor.
    calls = {'deferred': [], 'inline_dispatch': 0}

    _orig_deferred = manager.run_deferred_boot_dispatch

    def _spy_deferred(recovery_result, **kw):
        calls['deferred'].append((recovery_result, kw))
        # do NOT call the real billed dispatchers in the spy
    monkeypatch.setattr(manager, 'run_deferred_boot_dispatch', _spy_deferred)
    return manager, calls, _orig_deferred


def test_dispatch_false_returns_descriptor_and_defers(monkeypatch):
    """dispatch=False: the synchronous half returns a descriptor and the billed
    dispatch is NOT invoked inline."""
    from lib.tasks_pkg import manager

    spy = {'called': 0}
    monkeypatch.setattr(manager, 'run_deferred_boot_dispatch',
                        lambda *a, **k: spy.__setitem__('called', spy['called'] + 1))

    # Drive the real function with dispatch=False. Its DB queries run against
    # the (test) DB; with no stale tasks it recovers nothing and returns a
    # descriptor with empty lists. The KEY assertion is that the billed
    # dispatcher was NOT called.
    result = manager.recover_stale_tasks_on_startup(prev_shutdown=None, dispatch=False)
    assert isinstance(result, dict), f'expected descriptor dict, got {result!r}'
    assert 'killed_conv_ids' in result and 'recovered_conv_ids' in result, result
    assert spy['called'] == 0, 'dispatch=False must NOT invoke the billed dispatcher'
    _ok('dispatch=False → returns descriptor, billed dispatch deferred (not inline)')


def test_dispatch_true_calls_deferred_inline(monkeypatch):
    """dispatch=True (legacy default): the billed dispatch IS invoked inline —
    the deferral is opt-in, back-compat preserved for direct callers/tests."""
    from lib.tasks_pkg import manager

    spy = {'called': 0, 'arg': None}

    def _spy(recovery_result, **kw):
        spy['called'] += 1
        spy['arg'] = recovery_result
    monkeypatch.setattr(manager, 'run_deferred_boot_dispatch', _spy)

    manager.recover_stale_tasks_on_startup(prev_shutdown=None, dispatch=True)
    assert spy['called'] == 1, 'dispatch=True must invoke the billed dispatch inline'
    assert isinstance(spy['arg'], dict) and 'killed_conv_ids' in spy['arg'], spy['arg']
    _ok('dispatch=True → billed dispatch invoked inline (legacy preserved)')


def test_deferred_dispatch_runs_both_billed_lanes(monkeypatch):
    """run_deferred_boot_dispatch calls autopilot-resume + killed-recovery with
    the descriptor's data."""
    from lib.tasks_pkg import manager
    import lib.tasks_pkg.autopilot as ap
    import lib.tasks_pkg.killed_recovery as kr

    # The billed dispatch is now GATED OFF by default; opt in for this lane test.
    monkeypatch.setenv('TOFU_BOOT_AUTO_DISPATCH', '1')
    seen = {'resume': None, 'scan': 0, 'run': None}
    monkeypatch.setattr(ap, 'resume_armed_autopilot_after_crash',
                        lambda ids: seen.__setitem__('resume', list(ids)) or [])
    monkeypatch.setattr(kr, 'list_killed_turn_convs',
                        lambda *a, **k: (seen.__setitem__('scan', seen['scan'] + 1) or []))
    monkeypatch.setattr(kr, 'run_killed_recovery',
                        lambda cands, storm=False, **kw: (seen.__setitem__('run', (list(cands), storm))
                        or {'redispatched': 1, 'deferred': 0, 'exhausted': 0,
                            'storm_held': 0, 'skipped': 0}))

    manager.run_deferred_boot_dispatch(
        {'recovered_conv_ids': ['r1'], 'killed_conv_ids': ['k1'], 'restart_storm': False})
    assert seen['resume'] == ['r1'], seen
    assert seen['scan'] == 1, seen
    assert seen['run'] is not None and 'k1' in seen['run'][0], seen
    _ok('run_deferred_boot_dispatch runs both autopilot-resume and killed-recovery')


def test_shutdown_gate_skips_dispatch(monkeypatch):
    """should_continue()==False → the whole billed dispatch is skipped (a ^C
    during startup must never fire a fresh carrier we are about to tear down)."""
    from lib.tasks_pkg import manager
    import lib.tasks_pkg.autopilot as ap
    import lib.tasks_pkg.killed_recovery as kr

    # Isolate the should_continue gate: enable auto-dispatch so the ONLY reason
    # dispatch is skipped is should_continue()==False, not the default-off flag.
    monkeypatch.setenv('TOFU_BOOT_AUTO_DISPATCH', '1')
    fired = {'resume': 0, 'run': 0}
    monkeypatch.setattr(ap, 'resume_armed_autopilot_after_crash',
                        lambda ids: fired.__setitem__('resume', fired['resume'] + 1) or [])
    monkeypatch.setattr(kr, 'list_killed_turn_convs', lambda *a, **k: ['k1'])
    monkeypatch.setattr(kr, 'run_killed_recovery',
                        lambda *a, **k: fired.__setitem__('run', fired['run'] + 1) or {})

    manager.run_deferred_boot_dispatch(
        {'recovered_conv_ids': ['r1'], 'killed_conv_ids': ['k1'], 'restart_storm': False},
        should_continue=lambda: False)
    assert fired == {'resume': 0, 'run': 0}, f'dispatch must be skipped, got {fired}'
    _ok('should_continue()==False → billed dispatch skipped (^C-during-boot safe)')


def test_default_off_gate_is_display_only(monkeypatch):
    """With TOFU_BOOT_AUTO_DISPATCH unset, run_deferred_boot_dispatch must NOT
    auto-execute EITHER billed lane (crash recovery is display-only). Setting
    the env var re-enables it. ★ the owner requirement."""
    from lib.tasks_pkg import manager
    import lib.tasks_pkg.autopilot as ap
    import lib.tasks_pkg.killed_recovery as kr

    fired = {'resume': 0, 'run': 0}
    monkeypatch.setattr(ap, 'resume_armed_autopilot_after_crash',
                        lambda ids: fired.__setitem__('resume', fired['resume'] + 1) or [])
    monkeypatch.setattr(kr, 'list_killed_turn_convs', lambda *a, **k: ['k1'])
    monkeypatch.setattr(kr, 'run_killed_recovery',
                        lambda *a, **k: fired.__setitem__('run', fired['run'] + 1) or {})

    # Default (env unset) → display-only, nothing fires.
    monkeypatch.delenv('TOFU_BOOT_AUTO_DISPATCH', raising=False)
    manager.run_deferred_boot_dispatch(
        {'recovered_conv_ids': ['r1'], 'killed_conv_ids': ['k1'], 'restart_storm': False})
    assert fired == {'resume': 0, 'run': 0}, \
        f'default-off gate must skip ALL billed dispatch, got {fired}'

    # Opt in → the lanes fire again (proves the gate, not a broken wiring).
    monkeypatch.setenv('TOFU_BOOT_AUTO_DISPATCH', '1')
    manager.run_deferred_boot_dispatch(
        {'recovered_conv_ids': ['r1'], 'killed_conv_ids': ['k1'], 'restart_storm': False})
    assert fired == {'resume': 1, 'run': 1}, \
        f'TOFU_BOOT_AUTO_DISPATCH=1 must re-enable both lanes, got {fired}'
    _ok('default-off gate → crash recovery display-only; env var re-enables it')


def main():
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_boot_dispatch_deferral.__main__')

    class _MP:
        def __init__(self): self._undo = []; self._env = []
        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        def setenv(self, name, val):
            self._env.append((name, os.environ.get(name)))
            os.environ[name] = val
        def delenv(self, name, raising=False):
            self._env.append((name, os.environ.get(name)))
            os.environ.pop(name, None)
        def undo(self):
            for obj, name, val in reversed(self._undo):
                setattr(obj, name, val)
            self._undo = []
            for name, val in reversed(self._env):
                if val is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = val
            self._env = []

    print()
    print(_color('═══ boot-dispatch deferral tests ═══', '36'))
    print()
    tests = [
        test_dispatch_false_returns_descriptor_and_defers,
        test_dispatch_true_calls_deferred_inline,
        test_deferred_dispatch_runs_both_billed_lanes,
        test_shutdown_gate_skips_dispatch,
        test_default_off_gate_is_display_only,
    ]
    for fn in tests:
        mp = _MP()
        try:
            fn(mp)
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
        finally:
            mp.undo()
    print()
    print(_color(f'═══ ALL {len(tests)} BOOT-DISPATCH DEFERRAL TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
