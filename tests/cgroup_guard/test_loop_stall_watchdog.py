"""Tests for the event-loop stall watchdog + /dev/shm fault-dump prune.

Both features live as PURE module-level helpers in ``server.py`` precisely so
they can be exercised without a running event loop or a real stall:

  * ``_loop_stall_decide(age, threshold, already_dumped)`` — the one-dump-per-
    episode / re-arm-on-recovery decision the off-loop watcher thread makes.
  * ``_parse_fault_dump_pid`` / ``_prune_stale_fault_dumps`` — the boot-time
    tmpfs hygiene that deletes ``tofu_faulthandler_<pid>.log`` files whose pid
    is dead (the /dev/shm leak: 5,100 files observed in prod).
``import server`` is import-safe under conftest (serving is __main__-guarded).

NC-bite guidance (per project convention — prove the test FAILS without the
fix, then restore byte-identically):
  * Neuter ``_loop_stall_decide`` to ``return (age > threshold, already_dumped)``
    → ``test_stall_decide_one_dump_per_episode`` FAILS (it would dump every
    poll while stalled instead of once) while the disabled/healthy cases still
    pass — discriminating.
  * Neuter ``_prune_stale_fault_dumps`` to skip the ``pid_alive`` check (delete
    all matches) → ``test_prune_keeps_live_and_self`` FAILS.
"""

import os

import pytest

import server

pytestmark = pytest.mark.unit


def test_plain_import_does_not_arm_or_write_crash_sinks():
    """Tools/tests importing helpers must have zero faulthandler file side effects."""
    assert server._fault_log is None
    assert server._fault_shm_log is None


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


class TestBoundedFaultDumps:
    def test_budget_keeps_newest_dead_evidence_and_live_file(self, tmp_path):
        old = tmp_path / 'tofu_faulthandler_101.log'
        newest = tmp_path / 'tofu_faulthandler_102.log'
        live = tmp_path / 'tofu_faulthandler_103.log'
        for path in (old, newest, live):
            path.write_bytes(b'x' * 16)
        os.utime(old, ns=(1, 1))
        os.utime(newest, ns=(2, 2))
        os.utime(live, ns=(3, 3))

        removed = server._prune_fault_dump_budget(
            str(tmp_path), pid_alive=lambda pid: pid == 103,
            max_dead_files=1, max_dead_bytes=32)

        assert removed == 1
        assert not old.exists()
        assert newest.exists(), 'newest crash evidence must survive next boot'
        assert live.exists(), 'a live process sink must never be retention-pruned'

    def test_live_sink_is_truncated_in_place_only_after_cap(self, tmp_path):
        path = tmp_path / 'tofu_faulthandler_777.log'
        with open(path, 'w+', buffering=1) as sink:
            sink.write('x' * 33)
            fd = sink.fileno()
            assert server._trim_fault_sink_if_oversize(
                sink, 32, header='retained\n') is True
            assert sink.fileno() == fd
            assert path.read_text() == 'retained\n'
            assert server._trim_fault_sink_if_oversize(sink, 32) is False


class TestPidAlive:
    def test_self_is_alive(self):
        assert server._pid_alive(os.getpid()) is True

    def test_bogus_pids(self):
        assert server._pid_alive(0) is False
        assert server._pid_alive(-5) is False
        # An almost-certainly-dead high pid.
        assert server._pid_alive(4000000000) is False


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
        """Source pin: the extracted owner still appends pressure context."""
        import inspect
        from lib.server_loop_watchdog import LoopWatchdog

        source = inspect.getsource(LoopWatchdog._stall_watch)
        assert 'pressure = self.hooks._stall_pressure_context()' in source
        assert "(' [' + pressure + ']') if pressure else ''" in source
        assert 'pressure=pressure' in source
