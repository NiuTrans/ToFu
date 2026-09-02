"""tests/test_fs_keepalive_data_root.py — the FUSE keepalive must follow the
RESOLVED data root, not the code tree.

After the data/code physical-separation change (lib/runtime_paths), a source
checkout can place its live DB on a DIFFERENT mount than the repo (fresh-clone
XDG default, or an explicit $TOFU_DATA_DIR). The keepalive daemon exists to keep
the mount holding the live DB awake; if it kept probing the in-tree ``data/`` it
would poke the wrong mount and let the real DB mount stale — the exact FUSE
freeze it was written to prevent. So its probe paths AND its activation gate
must key on ``lib.runtime_paths.data_root()`` / ``logs_root()``.

Ground-truth (subprocess so a fresh import picks up env cleanly):
  * probe paths follow $TOFU_DATA_DIR to a location distinct from the repo;
  * NEUTER — the paths track data_root(), not _BASE_DIR (point data elsewhere →
    the in-tree path must NOT appear);
  * the activation gate keys on the resolved data root's mount, not the repo's.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.unit


def _run_py(code: str, env_extra=None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env['PYTHONPATH'] = _REPO + os.pathsep + env.get('PYTHONPATH', '')
    for k in ('TOFU_DATA_DIR', 'CHATUI_DATA_DIR', 'TOFU_DATA_LAYOUT',
              'CHATUI_DATA_LAYOUT', 'TOFU_DB_PATH'):
        env.pop(k, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, '-c', textwrap.dedent(code)],
                          capture_output=True, text=True, env=env)


class ProbePathsFollowDataRootTest(unittest.TestCase):

    def test_probe_paths_follow_tofu_data_dir(self):
        """$TOFU_DATA_DIR at a location distinct from the repo → probe paths are
        <that>/data and <that>/logs, and the repo tree does NOT appear."""
        base = os.path.realpath(tempfile.mkdtemp(prefix='tofu-extern-'))
        data = os.path.join(base, 'data')
        r = _run_py("""
            import lib.fs_keepalive as ka
            paths = ka._resolve_probe_paths()
            for p in paths:
                print('PROBE', p)
            print('DATAROOT', ka._resolve_data_root())
        """, env_extra={'TOFU_DATA_DIR': data})
        self.assertEqual(r.returncode, 0, r.stderr)
        probes = [ln[6:] for ln in r.stdout.splitlines() if ln.startswith('PROBE ')]
        self.assertEqual(len(probes), 2, r.stdout)
        self.assertEqual(probes[0], os.path.join(base, 'data'))
        self.assertEqual(probes[1], os.path.join(base, 'logs'))
        self.assertIn('DATAROOT %s' % data, r.stdout)
        # Nothing points back into the repo tree.
        for p in probes:
            self.assertFalse(p.startswith(_REPO),
                             'probe path leaked into the code tree: %s' % p)

    def test_neuter_reads_data_root_not_base_dir(self):
        """NEUTER: probe paths must derive from data_root(), NOT _BASE_DIR.

        Point data elsewhere via $TOFU_DATA_DIR; if the resolver were still
        keyed on ``_BASE_DIR`` (the repo), the in-tree ``<repo>/data`` would
        appear in the probes. Assert it does NOT — proving the fix reads the
        resolver. (Also directly checks _BASE_DIR is untouched but unused for
        probing.)"""
        base = os.path.realpath(tempfile.mkdtemp(prefix='tofu-extern-'))
        data = os.path.join(base, 'data')
        r = _run_py("""
            import os
            import lib.fs_keepalive as ka
            intree = os.path.join(ka._BASE_DIR, 'data')
            probes = ka._resolve_probe_paths()
            print('INTREE', intree)
            for p in probes:
                print('PROBE', p)
            # The would-be legacy path must NOT be probed.
            print('LEAK', str(intree in probes))
        """, env_extra={'TOFU_DATA_DIR': data})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('LEAK False', r.stdout,
                      'probe paths still contain the in-tree <repo>/data — the '
                      'resolver is keyed on _BASE_DIR, not data_root()')

    def test_activation_gate_keys_on_data_root_mount(self):
        """The activation gate must test the RESOLVED data root's mount, not the
        repo's. Point data at a fake /mnt/ root → gate says activate (probe
        paths get populated) even though the repo tree is on local disk.

        We monkeypatch ``_is_network_mount`` to only treat our fake data root as
        'network', so the decision provably follows data_root(), not _BASE_DIR.
        """
        fake_mnt = os.path.realpath(tempfile.mkdtemp(prefix='tofu-fakemnt-'))
        data = os.path.join(fake_mnt, 'data')
        r = _run_py("""
            import os
            import lib.fs_keepalive as ka
            target = ka._resolve_data_root()   # == our $TOFU_DATA_DIR
            # Only the resolved data root counts as 'network'; the repo does not.
            ka._is_network_mount = lambda p: os.path.abspath(p) == os.path.abspath(target)
            ka._IS_LINUX = True
            ka.start_fs_keepalive()
            print('RUNNING', ka._running)
            for p in ka._PROBE_PATHS:
                print('PROBE', p)
            ka.stop_fs_keepalive()
        """, env_extra={'TOFU_DATA_DIR': data})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('RUNNING True', r.stdout,
                      'gate did not activate on the network-mounted DATA ROOT')
        probes = [ln[6:] for ln in r.stdout.splitlines() if ln.startswith('PROBE ')]
        self.assertEqual(probes, [data, os.path.join(fake_mnt, 'logs')], r.stdout)

    def test_gate_skips_when_data_root_local_even_if_repo_networkish(self):
        """Symmetric NEUTER: if only the REPO looked network-ish but the data
        root is local, the daemon must SKIP — proving it doesn't key on the
        repo. We flip the fake mount predicate to match _BASE_DIR only."""
        localbase = os.path.realpath(tempfile.mkdtemp(prefix='tofu-local-'))
        data = os.path.join(localbase, 'data')
        r = _run_py("""
            import os
            import lib.fs_keepalive as ka
            # Pretend ONLY the repo tree is 'network'; the data root is local.
            ka._is_network_mount = lambda p: os.path.abspath(p) == os.path.abspath(ka._BASE_DIR)
            ka._IS_LINUX = True
            ka.start_fs_keepalive()
            print('RUNNING', ka._running)
            ka.stop_fs_keepalive()
        """, env_extra={'TOFU_DATA_DIR': data})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('RUNNING False', r.stdout,
                      'daemon activated on a LOCAL data root because it keyed on '
                      'the repo mount, not the resolved data root')


if __name__ == '__main__':
    unittest.main()


def test_probe_runtime_reuses_one_inflight_worker(monkeypatch):
    import lib.fs_keepalive as keepalive

    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocking_probe(path):
        calls.append(path)
        entered.set()
        release.wait(timeout=1.0)

    monkeypatch.setattr(keepalive, '_probe_path', blocking_probe)
    runtime = keepalive._ProbeRuntime()
    runtime.start()
    try:
        generation = runtime.request(('data', 'logs'))
        assert entered.wait(timeout=1.0)
        results, active_path = runtime.wait(generation, timeout=0.01)
        assert results is None and active_path == 'data'

        same_generation = runtime.request(('data', 'logs'))
        assert same_generation == generation
        assert calls == ['data'], 'a timeout must not spawn another stat thread'

        release.set()
        results, active_path = runtime.wait(generation, timeout=1.0)
        assert active_path == ''
        assert results is not None and [row[0] for row in results] == [
            'data', 'logs']
    finally:
        release.set()
        runtime.request_stop()
        assert runtime.join(1.0)


def test_keepalive_uses_one_interruptible_interval_deadline(monkeypatch):
    import lib.fs_keepalive as keepalive

    class StopAfterFirstDeadline:
        def __init__(self):
            self.stopped = False
            self.waits = []

        def is_set(self):
            return self.stopped

        def wait(self, timeout):
            self.waits.append(timeout)
            self.stopped = True
            return True

    class ProbeRuntime:
        def __init__(self):
            self.requests = 0
            self.stopped = False

        def request(self, _paths):
            self.requests += 1
            return self.requests

        def wait(self, _generation, _timeout):
            return [('data', True, 0.001)], ''

        def request_stop(self):
            self.stopped = True

    stop_event = StopAfterFirstDeadline()
    runtime = ProbeRuntime()
    monkeypatch.setattr(keepalive, '_stop_event', stop_event)
    monkeypatch.setattr(keepalive, '_PROBE_PATHS', ['data'])

    keepalive._keepalive_loop(runtime)

    assert runtime.requests == 1
    assert runtime.stopped is True
    assert stop_event.waits == [keepalive.KEEPALIVE_INTERVAL_S]
    old_timer_wakes_per_day = 86_400 / 0.5
    new_timer_wakes_per_day = 86_400 / keepalive.KEEPALIVE_INTERVAL_S
    assert 1 - new_timer_wakes_per_day / old_timer_wakes_per_day >= 0.96
