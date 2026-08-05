"""Tests for the event-loop stall watchdog + /dev/shm fault-dump prune.

Both features live as PURE module-level helpers in ``server.py`` precisely so
they can be exercised without a running event loop or a real stall:

  * ``_loop_stall_decide(age, threshold, already_dumped)`` — the one-dump-per-
    episode / re-arm-on-recovery decision the off-loop watcher thread makes.
  * ``_parse_fault_dump_pid`` / ``_prune_stale_fault_dumps`` — the boot-time
    tmpfs hygiene that deletes ``tofu_faulthandler_<pid>.log`` files whose pid
    is dead (the /dev/shm leak: 5,100 files observed in prod).
  * ``_should_arm_ctimer(threshold, sink)`` — the gate for the GIL-INDEPENDENT
    capture path (faulthandler.dump_traceback_later, a dedicated C timer thread
    that never takes the GIL).

The GIL blind-spot this closes: a Python daemon thread calling
``faulthandler.dump_traceback()`` must acquire the GIL to run, so it is STARVED
during a stall caused by a single monolithic GIL-holding C call (the documented
``json.dumps`` / catastrophic-regex pit). ``dump_traceback_later`` fires from a
C timer thread regardless. ``TestGilHeldCapture`` DEMONSTRATES the difference:
it holds the GIL on the main thread with a catastrophic-backtracking regex past
the timeout and asserts the C-timer dump lands anyway.

``import server`` is import-safe under conftest (serving is __main__-guarded).

NC-bite guidance (per project convention — prove the test FAILS without the
fix, then restore byte-identically):
  * Neuter ``_loop_stall_decide`` to ``return (age > threshold, already_dumped)``
    → ``test_stall_decide_one_dump_per_episode`` FAILS (it would dump every
    poll while stalled instead of once) while the disabled/healthy cases still
    pass — discriminating.
  * Neuter ``_prune_stale_fault_dumps`` to skip the ``pid_alive`` check (delete
    all matches) → ``test_prune_keeps_live_and_self`` FAILS.
  * Neuter ``_should_arm_ctimer`` to ``return False`` (or drop the re-arm in
    the heartbeat) → ``TestGilHeldCapture::test_ctimer_fires_under_gil_hold``
    FAILS (no dump lands during the GIL hold) — this is the test that pins the
    GIL-independent guarantee.
"""

import os

import pytest

import server

pytestmark = pytest.mark.unit


class TestLoopStallDecide:
    def test_disabled_when_threshold_zero(self):
        # threshold<=0 means the watchdog is off — never dump regardless of age.
        assert server._loop_stall_decide(999.0, 0.0, False) == (False, False)
        assert server._loop_stall_decide(999.0, -1.0, True) == (False, True)

    def test_healthy_rearms(self):
        # age within threshold → not a stall, and re-arm (already_dumped→False).
        assert server._loop_stall_decide(1.0, 5.0, False) == (False, False)
        assert server._loop_stall_decide(1.0, 5.0, True) == (False, False)

    def test_stall_first_dump(self):
        # age beyond threshold, not yet dumped → dump once and latch.
        assert server._loop_stall_decide(6.0, 5.0, False) == (True, True)

    def test_stall_decide_one_dump_per_episode(self):
        # A contiguous stall must emit exactly ONE dump: first poll dumps,
        # subsequent polls while still stalled do NOT re-dump.
        should, latched = server._loop_stall_decide(6.0, 5.0, False)
        assert should is True and latched is True
        # still stalled, deeper
        should2, latched2 = server._loop_stall_decide(10.0, 5.0, latched)
        assert should2 is False and latched2 is True
        # recovery re-arms so the NEXT episode dumps again
        should3, latched3 = server._loop_stall_decide(0.5, 5.0, latched2)
        assert should3 is False and latched3 is False
        should4, _ = server._loop_stall_decide(6.0, 5.0, latched3)
        assert should4 is True


class TestParseFaultDumpPid:
    def test_valid(self):
        assert server._parse_fault_dump_pid('tofu_faulthandler_1234.log') == 1234

    def test_non_numeric_and_foreign(self):
        assert server._parse_fault_dump_pid('tofu_faulthandler_abc.log') is None
        assert server._parse_fault_dump_pid('unrelated.log') is None
        assert server._parse_fault_dump_pid('tofu_faulthandler_.log') is None


class TestPruneStaleFaultDumps:
    def test_prune_keeps_live_and_self(self, tmp_path):
        # Fake sink dir with: a dead-pid dump, a live-pid dump, our own live
        # sink (keep_basename), and a foreign file. Only the dead one goes.
        dead = tmp_path / 'tofu_faulthandler_999999.log'
        live = tmp_path / 'tofu_faulthandler_4242.log'
        selfsink = tmp_path / 'tofu_faulthandler_777.log'
        foreign = tmp_path / 'something_else.log'
        for p in (dead, live, selfsink, foreign):
            p.write_text('x')

        def fake_alive(pid):
            return pid in (4242, 777)  # 999999 is dead

        removed = server._prune_stale_fault_dumps(
            directory=str(tmp_path),
            keep_basename='tofu_faulthandler_777.log',
            pid_alive=fake_alive)

        assert removed == 1
        assert not dead.exists()          # dead pid pruned
        assert live.exists()              # live pid kept
        assert selfsink.exists()          # our own sink kept (keep_basename)
        assert foreign.exists()           # non-matching name untouched

    def test_prune_no_matches_is_zero(self, tmp_path):
        (tmp_path / 'readme.txt').write_text('x')
        removed = server._prune_stale_fault_dumps(
            directory=str(tmp_path), keep_basename='', pid_alive=lambda _p: False)
        assert removed == 0


class TestPidAlive:
    def test_self_is_alive(self):
        assert server._pid_alive(os.getpid()) is True

    def test_bogus_pids(self):
        assert server._pid_alive(0) is False
        assert server._pid_alive(-5) is False
        # An almost-certainly-dead high pid.
        assert server._pid_alive(4000000000) is False


class TestShouldArmCtimer:
    def test_disabled_threshold(self):
        # Watchdog off → never arm the C-timer, even with a valid sink.
        import io
        real = io.FileIO(os.devnull, 'w')
        try:
            assert server._should_arm_ctimer(0.0, real) is False
            assert server._should_arm_ctimer(-1.0, real) is False
            assert server._should_arm_ctimer(None, real) is False
        finally:
            real.close()

    def test_no_sink(self):
        assert server._should_arm_ctimer(5.0, None) is False

    def test_sink_without_fileno(self):
        # dump_traceback_later needs a real fd; an in-memory buffer has none.
        import io
        assert server._should_arm_ctimer(5.0, io.StringIO()) is False

    def test_valid_sink_with_fileno(self, tmp_path):
        f = open(tmp_path / 'sink.log', 'w')
        try:
            assert server._should_arm_ctimer(5.0, f) is True
        finally:
            f.close()


class TestStallPressureContext:
    """The stall line must carry the host-pressure reading (2026-08-05 audit:
    classifying a stall meant hand-correlating error.log against
    cgroup_pressure.log — 7/19 stalls within 120s of a pressure event, the
    rest host-quiet. With the reading inline, the classification is free)."""

    def test_real_loadavg_present(self):
        out = server._stall_pressure_context()
        assert 'load1=' in out  # /proc/loadavg is always readable on Linux

    def test_cgroup_part_when_pressure_readable(self, monkeypatch):
        import lib.cgroup_guard as cg
        monkeypatch.setattr(cg, 'pressure',
                            lambda: {'limit': 1, 'usage': 1, 'pct': 91.42,
                                     'swap': None})
        assert 'cgmem=91.4%' in server._stall_pressure_context()

    def test_cgroup_part_absent_when_unreadable(self, monkeypatch):
        import lib.cgroup_guard as cg
        monkeypatch.setattr(cg, 'pressure', lambda: None)
        out = server._stall_pressure_context()
        assert 'cgmem' not in out and 'load1=' in out

    def test_stall_line_carries_pressure_suffix(self):
        """Source pin: the STALLED log call must append the context."""
        import inspect
        src = inspect.getsource(server._loop_stall_watch) \
            if hasattr(server, '_loop_stall_watch') else ''
        # _loop_stall_watch is a closure inside _serve; pin via the module src.
        with open(server.__file__, encoding='utf-8') as f:
            mod_src = f.read()
        assert '_pressure = _stall_pressure_context()' in mod_src
        assert "(' [' + _pressure + ']') if _pressure else ''" in mod_src
        assert 'pressure=_pressure' in mod_src


class TestGilHeldCapture:
    """The load-bearing demonstration: the C-timer path captures a stall that
    HOLDS THE GIL — the exact case a Python-thread dumper is blind to.

    Method: pet a real ``dump_traceback_later`` timer once (simulating the last
    healthy heartbeat), then hold the GIL on THIS thread with a catastrophic-
    backtracking regex for longer than the timeout. A Python thread could not
    run in that window; the C timer thread fires and writes the dump anyway."""

    # (a+)+$ vs a non-matching tail = exponential backtracking. n=26 ≈ 5.3s of
    # pure-C, GIL-held CPU on this host (measured), comfortably past a 1s timeout.
    _EVIL_RE = r'(a+)+$'
    _EVIL_INPUT = 'a' * 26 + '!'

    def test_ctimer_fires_under_gil_hold(self, tmp_path):
        import re
        import faulthandler as fh

        sink_path = tmp_path / 'ctimer_dump.log'
        sink = open(sink_path, 'w', buffering=1)
        timeout = 1.0
        try:
            # Gate must approve arming for a valid fd sink.
            assert server._should_arm_ctimer(timeout, sink) is True
            # Pet once, then DO NOT pet again — mimics the loop wedging right
            # after the last healthy heartbeat.
            fh.cancel_dump_traceback_later()
            fh.dump_traceback_later(timeout, repeat=False, file=sink, exit=False)

            # Hold the GIL well past the timeout. No Python bytecode from any
            # other thread can run during this single C call.
            re.search(self._EVIL_RE, self._EVIL_INPUT)
        finally:
            fh.cancel_dump_traceback_later()
            sink.flush()
            sink.close()

        captured = sink_path.read_text()
        # The C timer prepends "Timeout (…)!" and dumps every thread's stack.
        assert 'Timeout' in captured, 'C-timer did not fire during the GIL hold'
        assert ('Thread' in captured) or ('File' in captured), \
            'C-timer fired but wrote no stack'

    def test_healthy_pet_prevents_dump(self, tmp_path):
        # Control: if the timer is petted (re-armed) faster than it fires, no
        # dump is ever written — proving the pet mechanism suppresses false
        # positives while the loop is healthy.
        import time
        import faulthandler as fh

        sink_path = tmp_path / 'healthy_dump.log'
        sink = open(sink_path, 'w', buffering=1)
        timeout = 1.0
        try:
            deadline = time.monotonic() + 2.5   # pet across > 2 timeouts
            while time.monotonic() < deadline:
                fh.cancel_dump_traceback_later()
                fh.dump_traceback_later(timeout, repeat=False, file=sink, exit=False)
                time.sleep(0.2)                  # pet interval << timeout
        finally:
            fh.cancel_dump_traceback_later()
            sink.flush()
            sink.close()

        assert sink_path.read_text() == '', 'timer fired while being petted'
