"""Tests for lib/shutdown_marker.py — the OS-kill vs manual-shutdown dirty-bit.

The marker distinguishes a graceful shutdown (SIGTERM handler / manual button /
re-exec) from an untrappable OS SIGKILL/OOM. The load-bearing property: a marker
left in state="running" at the next boot proves the previous process died
WITHOUT any clean path → an OS kill.

Run standalone (the env pytest transitively imports a broken napari/vispy GL
plugin at collection — see JOURNAL): ``python3 tests/test_shutdown_marker.py``.
"""

import importlib
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest

# Ensure repo root on path for standalone runs.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _restore_data_root_after_test():
    """Do not leak this module's import-time path override to later tests."""
    original = os.environ.get('TOFU_DATA_DIR')
    yield
    if original is None:
        os.environ.pop('TOFU_DATA_DIR', None)
    else:
        os.environ['TOFU_DATA_DIR'] = original
    import lib.runtime_paths as rp
    importlib.reload(rp)
    import lib.shutdown_marker as sm
    importlib.reload(sm)


def _fresh_marker_module(data_dir):
    """Import lib.shutdown_marker with data_root pinned to a temp dir.

    runtime_paths resolves _BASE once at import, so we set TOFU_DATA_DIR and
    reload both modules to bind the temp dir.
    """
    os.environ['TOFU_DATA_DIR'] = data_dir
    import lib.runtime_paths as rp
    importlib.reload(rp)
    import lib.shutdown_marker as sm
    importlib.reload(sm)
    return sm


def test_first_boot_is_not_unclean():
    with tempfile.TemporaryDirectory() as d:
        sm = _fresh_marker_module(d)
        # No marker file yet.
        assert not os.path.exists(sm.marker_path())
        cls = sm.classify_previous_shutdown()
        assert cls['verdict'] == sm.VERDICT_FIRST_BOOT
        assert cls['manual'] is False
        print('OK first_boot')


def test_armed_then_killed_is_unclean():
    """arm() but NO mark_clean() = the OS-kill case → unclean at next boot."""
    with tempfile.TemporaryDirectory() as d:
        sm = _fresh_marker_module(d)
        sm.arm()
        assert os.path.exists(sm.marker_path())
        # Simulate a fresh boot reading the marker the killed process left.
        cls = sm.classify_previous_shutdown()
        assert cls['verdict'] == sm.VERDICT_UNCLEAN, cls
        assert cls['manual'] is False
        print('OK armed->killed = unclean')


def test_mark_clean_signal_is_clean_not_manual():
    with tempfile.TemporaryDirectory() as d:
        sm = _fresh_marker_module(d)
        sm.arm()
        sm.mark_clean('signal')
        cls = sm.classify_previous_shutdown()
        assert cls['verdict'] == sm.VERDICT_CLEAN, cls
        # signal drain is a controlled exit; 'manual' flag means DELIBERATE.
        assert cls['manual'] is True
        assert cls['reason'] == 'signal'
        print('OK mark_clean(signal) = clean')


def test_mark_clean_manual_button():
    with tempfile.TemporaryDirectory() as d:
        sm = _fresh_marker_module(d)
        sm.arm()
        sm.mark_clean('manual')
        cls = sm.classify_previous_shutdown()
        assert cls['verdict'] == sm.VERDICT_CLEAN
        assert cls['manual'] is True
        assert cls['reason'] == 'manual'
        print('OK mark_clean(manual)')


def test_generic_signal_cannot_overwrite_stronger_exit_intent():
    with tempfile.TemporaryDirectory() as d:
        sm = _fresh_marker_module(d)
        for reason in ('manual', 'restart', 'memory_recycle'):
            sm.arm()
            sm.mark_clean(reason)
            sm.mark_clean('signal')
            cls = sm.classify_previous_shutdown()
            assert cls['verdict'] == sm.VERDICT_CLEAN
            assert cls['reason'] == reason


def test_restart_reexec_is_clean():
    with tempfile.TemporaryDirectory() as d:
        sm = _fresh_marker_module(d)
        sm.arm()
        sm.mark_clean('restart')
        cls = sm.classify_previous_shutdown()
        assert cls['verdict'] == sm.VERDICT_CLEAN
        assert cls['reason'] == 'restart'
        print('OK mark_clean(restart)')


def test_report_and_arm_rearms_dirty():
    """report_and_arm classifies the PREVIOUS marker, then re-arms dirty.

    So a clean exit followed by an OS kill is correctly detected on the 3rd
    boot: boot2 sees clean, re-arms running; kill; boot3 sees running=unclean.
    """
    with tempfile.TemporaryDirectory() as d:
        sm = _fresh_marker_module(d)
        # boot1: arm, then clean exit.
        sm.arm()
        sm.mark_clean('manual')
        # boot2: report_and_arm sees clean, then re-arms dirty.
        cls2 = sm.report_and_arm()
        assert cls2['verdict'] == sm.VERDICT_CLEAN
        # Now the marker is armed dirty again (no clean exit this time = kill).
        cls3 = sm.classify_previous_shutdown()
        assert cls3['verdict'] == sm.VERDICT_UNCLEAN, cls3
        print('OK report_and_arm re-arms dirty')


def test_corrupt_marker_treated_as_unclean():
    """A missing/garbled state field must fail SAFE = unclean (never hide a kill)."""
    with tempfile.TemporaryDirectory() as d:
        sm = _fresh_marker_module(d)
        with open(sm.marker_path(), 'w') as f:
            f.write('{"pid": 123}')   # no "state" key
        cls = sm.classify_previous_shutdown()
        assert cls['verdict'] == sm.VERDICT_UNCLEAN, cls
        print('OK corrupt marker = unclean (fail-safe)')


def test_boot_ring_records_and_bounds():
    with tempfile.TemporaryDirectory() as d:
        sm = _fresh_marker_module(d)
        boots = []
        for _ in range(sm._BOOTS_KEEP + 10):
            boots = sm.record_boot()
        assert len(boots) == sm._BOOTS_KEEP, len(boots)
        print('OK boot ring bounded at %d' % sm._BOOTS_KEEP)


def test_concurrent_boot_records_preserve_the_bounded_newest_ring():
    with tempfile.TemporaryDirectory() as d:
        sm = _fresh_marker_module(d)
        stamps = [1000.0 + index for index in range(sm._BOOTS_KEEP)]
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(sm.record_boot, stamps))

        assert all(results)
        persisted = sm.read_json(sm._boots_path())
        assert len(persisted) == sm._BOOTS_KEEP
        assert set(persisted) == set(stamps)
        assert not os.path.exists(sm._boots_path() + '.tmp')


def test_restart_storm_detection():
    with tempfile.TemporaryDirectory() as d:
        sm = _fresh_marker_module(d)
        now = 1000.0
        # A few well-spaced boots (normal restarts) → NOT a storm.
        calm = [now - 5000, now - 4000, now - 3000]
        assert sm.is_restart_storm(calm, now=now) is False
        # Many boots inside the window → storm.
        storm = [now - i for i in range(sm._STORM_THRESHOLD + 2)]
        assert sm.is_restart_storm(storm, now=now) is True
        print('OK storm detection: calm=False, rapid=True')


def test_report_and_arm_sets_restart_storm_flag():
    with tempfile.TemporaryDirectory() as d:
        sm = _fresh_marker_module(d)
        # Prime the ring with enough recent boots to trip the storm on next arm.
        import time as _t
        now = _t.time()
        sm.write_json_atomic(sm._boots_path(),
                             [now - i for i in range(sm._STORM_THRESHOLD)])
        cls = sm.report_and_arm()
        assert cls.get('restart_storm') is True, cls
        print('OK report_and_arm surfaces restart_storm=True')


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            print('FAIL %s: %s' % (fn.__name__, e))
    print('\n%d/%d passed' % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
