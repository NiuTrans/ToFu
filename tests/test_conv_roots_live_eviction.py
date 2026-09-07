#!/usr/bin/env python3
"""Item #3 (patch->fundamental-fix epic pt_e02044f4ab084dff): pin a LIVE
conversation's workspace-root registry against LRU eviction.

THE BUG (latent, fires under the SWE-bench concurrency profile the cap was
sized for — MAX_CONV_ROOTS=512, comment cites "1236 unique convs")
------------------------------------------------------------------------
``set_conv_roots`` evicted the strict-oldest ``_conv_roots`` entry via
``popitem(last=False)`` with NO task-liveness check. So a conversation whose
task is still MID-FLIGHT could have its root registry evicted by newer convs'
cap pressure. Its next ``name:rel/path`` tool call then resolves against the
(concurrency-clobbered) GLOBAL ``_roots``:
  * if the global lacks that name -> UnknownWorkspaceRootError (the tools.py
    self-heal recovers via base_path basename match); but
  * if the global holds a COLLIDING-BASENAME root at a DIFFERENT path -> the
    resolve SUCCEEDS and returns the WRONG base -> the write silently lands in
    another conversation's tree. The self-heal is exception-only and NEVER
    sees this silent-misroute case.

THE FIX (lib/project_mod/config.py)
-----------------------------------
``_evict_conv_roots_over_cap`` evicts the oldest IDLE conv (no pending/running
task per ``_conv_has_live_task``, which reuses manager._latest_task_for_conv +
the chat TaskRuntime), preserving live ones. Only if EVERY over-cap candidate
is live does it force-evict oldest + WARN (memory bound wins; self-heal covers
the displaced conv). The self-heal STAYS for the two legitimately-defensive
cases (model typo / basename-match, genuinely-absent conv_id).

Tests (deterministic — do not depend on the quiet production logs):
  1. ``test_live_conv_survives_cap_pressure_no_misroute`` — ★ the corruption
     case. Two roots with a COLLIDING basename 'src': live conv A -> /wsA/src,
     decoy global _roots['src'] -> /wsB/src. Fill past a small cap with idle
     convs. Post-fix: A's registry SURVIVES and A resolves 'src:foo.py' to
     /wsA/src, never /wsB/src.
  2. ``test_idle_conv_still_evicted`` — an idle over-cap conv IS evicted
     (behaviour preserved; the fix must not turn eviction OFF).
  3. ``test_all_live_force_evicts_oldest`` — pathological all-live case bounds
     memory (does not grow _conv_roots unbounded).

Double-neuter (on-disk, restored byte-identical):
  NC-1 (disable the live-pin): make ``_conv_has_live_task`` always return False
        -> the live conv A is evicted under cap pressure -> A resolves the
        colliding name against the global decoy -> SILENT MISROUTE to /wsB/src
        -> test #1 FAILS. #2/#3 still pass. Proves the pin is load-bearing.
  NC-2 (invariant — no unbounded growth): make ``_evict_conv_roots_over_cap``
        a no-op (return immediately) -> #3's len(_conv_roots) exceeds the cap
        -> test #3 FAILS. #1/#2 still pass (a no-op eviction also fails #2's
        eviction assertion? no — #2 checks the idle conv is GONE, which a
        no-op also breaks -> #2 also FAILS). So NC-2 asserts #3 fails and the
        eviction-happens control (#2) fails, while the misroute test (#1)
        depends on eviction too. To keep NC-2 a CLEAN single-target neuter we
        assert on #3 only and tolerate #2 co-failing (documented below).
"""

import os
from tests.support.chat_tasks import chat_task_registry
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_THIS = os.path.abspath(__file__)
_ROOT = os.path.dirname(os.path.dirname(_THIS))
_TARGET = os.path.join(_ROOT, 'lib', 'project_mod', 'config.py')


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ─────────────────────────── shared fixture helpers ───────────────────────────

def _fresh_workspace():
    """Two sibling trees with the SAME basename 'src' at different paths."""
    tmp = tempfile.mkdtemp(prefix='conv-evict-')
    ws_a = os.path.join(tmp, 'wsA', 'src')
    ws_b = os.path.join(tmp, 'wsB', 'src')
    os.makedirs(ws_a)
    os.makedirs(ws_b)
    return tmp, ws_a, ws_b


def _reset_registries(cfg):
    cfg._conv_roots.clear()
    cfg._conv_primary.clear()
    cfg._roots.clear()


def _install_live_probe(cfg, live_ids):
    """Force ``_conv_has_live_task`` to report exactly *live_ids* as live,
    bypassing the real TaskRuntime (deterministic, no manager dependency)."""
    live = set(live_ids)
    cfg._conv_has_live_task = lambda cid: cid in live  # type: ignore


# ─────────────────────────── the three positive tests ───────────────────────────

def test_live_conv_survives_cap_pressure_no_misroute():
    """★ Corruption case: a live conv must not be evicted into a colliding
    global-root silent misroute."""
    from lib.project_mod import config as cfg
    tmp, ws_a, ws_b = _fresh_workspace()
    try:
        _reset_registries(cfg)
        cfg.MAX_CONV_ROOTS = 3
        _install_live_probe(cfg, live_ids={'convA'})

        # Decoy: the GLOBAL registry holds a 'src' root at a DIFFERENT path.
        # (Simulates another task's set_project having populated the global.)
        cfg._roots['src'] = cfg._make_root_state(ws_b)

        # Register the LIVE conv A first (oldest) -> its own /wsA/src.
        cfg.set_conv_roots('convA', ws_a)
        # Now flood with idle convs to blow past the cap of 3.
        idle_ws = []
        for i in range(6):
            d = os.path.join(tmp, f'idle{i}')
            os.makedirs(d)
            idle_ws.append(d)
            cfg.set_conv_roots(f'idle{i}', d)

        # A must SURVIVE (pinned live) despite being the oldest.
        assert 'convA' in cfg._conv_roots, (
            'live conv A was EVICTED under cap pressure — this is the bug')
        # And A must resolve its OWN root, never the colliding global decoy.
        base, rel = cfg.resolve_namespaced_path('src:foo.py', conv_id='convA')
        assert base == ws_a, (
            f'SILENT MISROUTE: convA resolved src:foo.py to {base!r} '
            f'(expected {ws_a!r}; decoy was {ws_b!r})')
        assert rel == 'foo.py'
        # Cap is still respected among the idle entries.
        assert len(cfg._conv_roots) <= cfg.MAX_CONV_ROOTS, (
            f'cap exceeded: {len(cfg._conv_roots)} > {cfg.MAX_CONV_ROOTS}')
    finally:
        cfg.MAX_CONV_ROOTS = 512
        _reset_registries(cfg)
        shutil.rmtree(tmp, ignore_errors=True)
    _ok('★ live conv survives cap pressure AND resolves to its OWN root (no misroute)')


def test_idle_conv_still_evicted():
    """The fix must NOT disable eviction — an idle over-cap conv is dropped."""
    from lib.project_mod import config as cfg
    tmp, ws_a, ws_b = _fresh_workspace()
    try:
        _reset_registries(cfg)
        cfg.MAX_CONV_ROOTS = 2
        _install_live_probe(cfg, live_ids=set())  # nobody live -> all idle

        cfg.set_conv_roots('old1', ws_a)   # oldest idle
        cfg.set_conv_roots('mid2', ws_b)
        cfg.set_conv_roots('new3', tmp)    # registering this evicts old1
        assert 'old1' not in cfg._conv_roots, 'oldest idle conv was NOT evicted'
        assert len(cfg._conv_roots) == 2, (
            f'expected 2 entries after eviction, got {len(cfg._conv_roots)}')
    finally:
        cfg.MAX_CONV_ROOTS = 512
        _reset_registries(cfg)
        shutil.rmtree(tmp, ignore_errors=True)
    _ok('idle over-cap conv is still evicted (eviction not disabled)')


def test_all_live_force_evicts_oldest():
    """Pathological all-live: memory must stay bounded (no unbounded growth)."""
    from lib.project_mod import config as cfg
    tmp, ws_a, _ = _fresh_workspace()
    try:
        _reset_registries(cfg)
        cfg.MAX_CONV_ROOTS = 2
        # EVERY conv is live.
        cfg._conv_has_live_task = lambda cid: True  # type: ignore
        for i in range(5):
            d = os.path.join(tmp, f'c{i}')
            os.makedirs(d)
            cfg.set_conv_roots(f'c{i}', d)
        # Even though all are live, the cap must still bound the dict.
        assert len(cfg._conv_roots) <= cfg.MAX_CONV_ROOTS, (
            f'UNBOUNDED GROWTH: {len(cfg._conv_roots)} > cap {cfg.MAX_CONV_ROOTS} '
            'when all convs are live — force-evict fallback missing')
    finally:
        cfg.MAX_CONV_ROOTS = 512
        _reset_registries(cfg)
        shutil.rmtree(tmp, ignore_errors=True)
    _ok('all-live pathological case still bounds memory (force-evict oldest)')


_POSITIVE = [test_live_conv_survives_cap_pressure_no_misroute,
             test_idle_conv_still_evicted,
             test_all_live_force_evicts_oldest]


def _run(fn):
    try:
        fn()
        return True
    except AssertionError as e:
        print(' ', _color('✗', '31'), f'{fn.__name__}: {e}')
        return False
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(' ', _color('✗', '31'), f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
        return False


def _neuter_ctx(find, repl):
    """In-memory neuter of _TARGET (lib.project_mod.config) via the shared
    xdist-safe harness — the shipped file is opened read-only, never written."""
    from tests._nc_harness import neutered_source
    return neutered_source(_TARGET, find, repl)


def _subrun(test_fn):
    """Run ONE positive test IN-PROCESS (under the active in-memory neuter) and
    return (passed, ''). No subprocess, no on-disk source mutation."""
    return _run(test_fn), ''


def main():
    print()
    print(_color('═══ live-task conv-root eviction pin — double-neuter ═══', '36'))
    print()

    print(_color('Baseline (shipped code):', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('baseline failed — fix the implementation before neutering')

    # ── NC-1: disable the live-pin -> live conv evicted -> silent misroute ──
    print()
    print(_color('NC-1 — disable live-pin (_conv_has_live_task body → return False):', '36'))
    # The NC-1 dedicated variant does NOT install a cfg._conv_has_live_task stub
    # (it registers a real running task instead), so the SOURCE-level neuter of
    # _conv_has_live_task governs. Under the in-memory harness, cfg is the
    # neutered module for the ``with`` duration, so cfg._conv_has_live_task is
    # the return-False variant — no subprocess, no on-disk write.
    with _neuter_ctx(
            'def _conv_has_live_task(conv_id):\n    ',
            'def _conv_has_live_task(conv_id):\n    return False  # NC-1 neuter\n    '):
        ok_mis, _ = _subrun(_nc1_live_conv_misroutes_when_pin_disabled)
        ok_idle, _ = _subrun(test_idle_conv_still_evicted)
    if ok_mis:
        _fail('NC-1: misroute test PASSED with pin disabled — pin not load-bearing!')
    if not ok_idle:
        _fail('NC-1: idle-eviction control failed — unintended blast radius')
    _ok('NC-1: live conv MISROUTES with pin disabled; idle-eviction control holds')

    # ── NC-2: make eviction a no-op -> unbounded growth (invariant) ──
    print()
    print(_color('NC-2 — neuter eviction (all-live force-evict removed → unbounded):', '36'))
    with _neuter_ctx(
            "        if victim is None:\n"
            "            # All entries are live — evict strict-oldest to bound memory.\n"
            "            victim, _ = _conv_roots.popitem(last=False)",
            "        if victim is None:\n"
            "            break  # NC-2 neuter: drop the force-evict fallback\n"
            "            victim, _ = _conv_roots.popitem(last=False)"):
        ok_bound, _ = _subrun(test_all_live_force_evicts_oldest)
        ok_mis2, _ = _subrun(test_live_conv_survives_cap_pressure_no_misroute)
    if ok_bound:
        _fail('NC-2: all-live bound test PASSED with force-evict removed — not load-bearing!')
    if not ok_mis2:
        _fail('NC-2: misroute control failed — unintended blast radius')
    _ok('NC-2: unbounded growth when all live + force-evict removed; misroute control holds')

    # ── post-neuter baseline (source never mutated on disk) ──
    print()
    print(_color('Post-restore baseline:', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('post-restore baseline failed — file not restored correctly')

    print()
    print(_color('═══ ALL LIVE-EVICTION-PIN TESTS + DOUBLE-NEUTER PASSED ═══', '32'))
    print()


# ── NC-1 dedicated variant: does NOT install a liveness stub, so the SOURCE
#    _conv_has_live_task governs. Instead it registers the live task in the
#    REAL runtime so the shipped probe reports it live; with the source
#    neutered to `return False`, the pin is gone and A misroutes. ──
def _nc1_live_conv_misroutes_when_pin_disabled():
    from lib.project_mod import config as cfg
    import lib.tasks_pkg.manager.runtime as _mgr
    tmp, ws_a, ws_b = _fresh_workspace()
    task_id = 'tk-evict-nc1'
    try:
        _reset_registries(cfg)
        cfg.MAX_CONV_ROOTS = 3
        # Real runtime liveness for convA (shipped probe reads this).
        _mgr._record_latest_task('convA', task_id)
        chat_task_registry[task_id] = {
            'id': task_id,
            'status': 'running',
            'convId': 'convA',
        }
        cfg._roots['src'] = cfg._make_root_state(ws_b)  # colliding decoy
        cfg.set_conv_roots('convA', ws_a)
        for i in range(6):
            d = os.path.join(tmp, f'idle{i}')
            os.makedirs(d)
            cfg.set_conv_roots(f'idle{i}', d)
        # With the SHIPPED pin, A survives and resolves to ws_a. With the
        # NC-1 source neuter (_conv_has_live_task→False), A is evicted and
        # resolve falls to the global decoy ws_b (silent misroute).
        if 'convA' in cfg._conv_roots:
            base, _ = cfg.resolve_namespaced_path('src:foo.py', conv_id='convA')
            assert base == ws_a, f'misroute to {base!r}'
        else:
            base, _ = cfg.resolve_namespaced_path('src:foo.py', conv_id='convA')
            assert base == ws_a, (
                f'live conv evicted AND misrouted to {base!r} (expected {ws_a!r})')
    finally:
        chat_task_registry.pop(task_id, None)
        with _mgr._conv_latest_task_lock:
            _mgr._conv_latest_task.pop('convA', None)
        try:
            from lib.runtime_state_store import get_store
            get_store().set_value('latest', 'convA', None, 1)
        except Exception:
            pass
        cfg.MAX_CONV_ROOTS = 512
        _reset_registries(cfg)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
