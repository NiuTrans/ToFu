#!/usr/bin/env python3
"""tests/test_desktop_tk_host.py — the tk UI host thread (tray-first topology).

Owner report 2026-08-04: clicking "Minimize to tray" vanished the role
window with NO tray icon anywhere. Root cause: the tray only started AFTER
the first window closed (blocking window mainloop, THEN icon.run()), so
minimizing during the first window session had no tray to go to.
desktop/_tk_host.py is the root fix — a dedicated tk thread that owns
every window, so pystray's icon.run() occupies the main thread from
second zero and the tray exists BEFORE any window can hide.

Headless discipline: no real tk root can exist on CI (no display). The
suites therefore pin:

* **The platform gate** — the host is win32-only (macOS demands the main
  thread for BOTH pystray and tk, so the legacy sequence must stand
  there); start() never spawns a thread off-win32.
* **Failure shapes of start()** — a broken tk (no display) degrades to
  False and every post/call then inlines, never hangs.
* **Queue semantics** — _drain_once driven by a STUB root: results land,
  exceptions are captured not raised into the drain, done-events fire,
  the poll re-arms.
* **Short-circuits** — call/post from the host thread itself run inline
  (the role-window button path); without them the host would deadlock on
  its own queue.
* **Wiring ratchets** — both launchers start the host BEFORE icon.run(),
  the startup window and the control-panel re-entry are marshalled
  through it, and the role window converts the title-bar minimize into a
  tray-ward withdraw. Source-level: the behaviour itself needs Windows +
  a display.
"""

import ast
import os
import queue
import sys
import threading
import types
import unittest
from unittest import mock

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

pytestmark = pytest.mark.unit


def _host_mod():
    import desktop._tk_host as host
    return host


def _reset(host):
    host._host['thread'] = None
    host._host['queue'] = None
    host._host['root'] = None
    host._host['tid'] = None
    host._host['ready'].clear()


def _src(rel):
    with open(os.path.join(_REPO, rel), encoding='utf-8') as f:
        return f.read()


class _StubRoot:
    """Duck-typed root for _drain_once: records the after() re-arm."""

    def __init__(self):
        self.after_calls = []

    def after(self, ms, fn, *args):
        self.after_calls.append((ms, fn, args))


class HostTestCase(unittest.TestCase):

    def setUp(self):
        self.host = _host_mod()
        self._snapshot = dict(self.host._host)
        _reset(self.host)

    def tearDown(self):
        self.host._host['thread'] = self._snapshot['thread']
        self.host._host['queue'] = self._snapshot['queue']
        self.host._host['root'] = self._snapshot['root']
        self.host._host['tid'] = self._snapshot['tid']


class SupportGateTest(HostTestCase):

    def test_supported_only_on_win32(self):
        for platform, expected in (('win32', True), ('linux', False),
                                   ('darwin', False)):
            with mock.patch.object(sys, 'platform', platform):
                self.assertIs(self.host.supported(), expected, platform)

    def test_start_never_spawns_off_win32(self):
        with mock.patch.object(sys, 'platform', 'linux'):
            self.assertFalse(self.host.start())
        self.assertIsNone(self.host._host['thread'],
                          'a host thread spawned on an unsupported platform')

    def test_start_false_when_tk_root_fails(self):
        """No display / broken tk → False, thread exits, NO hang — and the
        callers then use the legacy inline path. NEUTER target: drop the
        try/except around Tk() and this hangs or raises instead of
        returning False."""
        fake_tk = types.ModuleType('tkinter')

        def _boom():
            raise RuntimeError('no display')

        fake_tk.Tk = _boom
        with mock.patch.object(sys, 'platform', 'win32'):
            with mock.patch.dict(sys.modules, {'tkinter': fake_tk}):
                self.assertFalse(self.host.start())
        self.assertFalse(self.host.available())
        t = self.host._host['thread']
        self.assertIsNotNone(t)
        t.join(timeout=3)
        self.assertFalse(t.is_alive(), 'a failed host thread lingered')


class QueueSemanticsTest(HostTestCase):

    def test_drain_once_runs_results_and_rearms(self):
        stub = _StubRoot()
        q = queue.Queue()
        box1, box2 = {}, {}
        done2 = threading.Event()
        q.put((lambda: 42, None, box1))

        def _boom():
            raise ValueError('kaput')

        q.put((_boom, done2, box2))
        self.host._drain_once(stub, q, lambda m: None)

        self.assertEqual(box1.get('result'), 42)
        self.assertIsInstance(box2.get('error'), ValueError,
                              'an fn exception must be CAPTURED, not raised '
                              'into the drain loop')
        self.assertTrue(done2.is_set())
        self.assertTrue(q.empty())
        self.assertEqual(len(stub.after_calls), 1,
                         'the poll did not re-arm — requests would stop '
                         'being processed after one drain')
        ms, fn, args = stub.after_calls[0]
        self.assertEqual(ms, self.host._POLL_MS)
        self.assertIs(fn, self.host._drain_once)
        self.assertEqual(args[:2], (stub, q))

    def test_post_and_call_inline_without_host(self):
        called = []
        self.assertFalse(self.host.post(lambda: called.append(1)),
                         'post() with no host must say "run it yourself"')
        self.assertEqual(called, [])
        self.assertEqual(self.host.call(lambda: 7), 7,
                         'call() with no host must inline, never hang')

    def test_host_thread_call_short_circuits(self):
        """The role-window button path: a caller already ON the host thread
        must never queue — that is a self-deadlock by construction. NEUTER
        target: drop the on_host_thread check → this deadlocks (test would
        time out at the join below) or raises on the None queue."""
        host = self.host
        host._host['root'] = object()           # pretend the root is live
        host._host['tid'] = threading.get_ident()  # and WE are the host
        host._host['queue'] = None              # a real queue put would crash
        self.assertEqual(host.call(lambda: 99), 99)
        self.assertFalse(host.post(lambda: None),
                         'post() on the host thread must say "inline"')
        ran = []
        host.post_or_call(lambda: ran.append(1))
        self.assertEqual(ran, [1])

    def test_call_roundtrip_through_the_queue(self):
        """End-to-end marshalling: caller on thread A, fn executes on the
        drainer thread B and the result comes back — the tray→host pattern."""
        host = self.host
        q = queue.Queue()
        stub = _StubRoot()
        stop = threading.Event()
        ran_on = []

        def _drainer():
            host._host['tid'] = threading.get_ident()
            while not stop.is_set():
                self.host._drain_once(stub, q, lambda m: None)
                stop.wait(0.005)

        t = threading.Thread(target=_drainer, daemon=True)
        host._host['root'] = object()
        host._host['queue'] = q
        t.start()
        try:
            result = host.call(
                lambda: ran_on.append(threading.get_ident()) or 'done',
                timeout=5)
        finally:
            stop.set()
            t.join(timeout=3)
        self.assertEqual(result, 'done')
        self.assertEqual(ran_on, [t.ident],
                         'fn ran on the CALLING thread — the marshalling '
                         'is a sham')

    def test_call_reraises_fn_exception_on_the_caller(self):
        host = self.host
        q = queue.Queue()
        stub = _StubRoot()
        stop = threading.Event()

        def _drainer():
            host._host['tid'] = threading.get_ident()
            while not stop.is_set():
                self.host._drain_once(stub, q, lambda m: None)
                stop.wait(0.005)

        t = threading.Thread(target=_drainer, daemon=True)
        host._host['root'] = object()
        host._host['queue'] = q
        t.start()

        def _boom():
            raise KeyError('dialog exploded')

        try:
            with self.assertRaises(KeyError):
                host.call(_boom, timeout=5)
        finally:
            stop.set()
            t.join(timeout=3)

    def test_call_timeout_raises_instead_of_hanging(self):
        host = self.host
        host._host['root'] = object()
        host._host['queue'] = queue.Queue()
        host._host['tid'] = 999999  # somebody else, and nobody drains
        with self.assertRaises(TimeoutError):
            host.call(lambda: None, timeout=0.05)


class HeadlessImportRatchetTest(unittest.TestCase):
    """Module-level tkinter imports would break headless CI and the agent
    smoke gate (which imports role_window with no display)."""

    def _top_level_tk_imports(self, rel):
        tree = ast.parse(_src(rel), filename=rel)
        bad = []
        for node in tree.body:  # TOP LEVEL only — function bodies are fine
            if isinstance(node, ast.Import):
                bad.extend(a.name for a in node.names
                           if a.name.startswith('tkinter'))
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith('tkinter'):
                    bad.append(node.module)
        return bad

    def test_host_module_imports_tkinter_only_inside_functions(self):
        self.assertEqual(self._top_level_tk_imports('desktop/_tk_host.py'),
                         [])

    def test_role_window_imports_tkinter_only_inside_functions(self):
        self.assertEqual(
            self._top_level_tk_imports('desktop/role_window.py'), [])


class TrayFirstWiringRatchetTest(unittest.TestCase):
    """Source-level pins for the root fix. NEUTER target: restore the old
    window-then-tray order in either launcher and the ORDER test goes red;
    strip the marshalling and the MARSHAL tests go red."""

    _LAUNCHERS = ('desktop/launcher.py', 'desktop/agent_launcher.py')

    def test_host_starts_before_icon_run(self):
        import re
        call_re = re.compile(r'^\s+icon\.run\(\)', re.M)
        for rel in self._LAUNCHERS:
            src = _src(rel)
            self.assertIn('_tk_host.start(', src,
                          '%s never starts the tk host' % rel)
            m = call_re.search(src)
            self.assertIsNotNone(m, '%s never calls icon.run()' % rel)
            self.assertLess(
                src.index('_tk_host.start('), m.start(),
                '%s starts the host AFTER icon.run() — the tray is late '
                'again, the 2026-08-04 minimize bug is back' % rel)

    def test_startup_window_is_marshalled_to_the_host(self):
        for rel in self._LAUNCHERS:
            self.assertIn(
                '_tk_host.post_or_call(lambda: role_window.show_role_window(',
                _src(rel),
                '%s shows the startup window inline again — the blocking '
                'window-then-tray sequence is back' % rel)

    def test_role_window_minimize_withdraws_to_the_tray(self):
        """The title-bar "_" must not iconify to the taskbar (the vanished-
        window half of the owner report). AST-level: a substring check would
        match a COMMENTED-OUT binding (measured neuter-miss 2026-08-04).
        NEUTER target: delete the bind call or the withdraw call."""
        tree = ast.parse(_src('desktop/role_window.py'),
                         filename='desktop/role_window.py')
        binds, withdraws = [], []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else ''
            if attr == 'bind' and node.args and isinstance(
                    node.args[0], ast.Constant) \
                    and node.args[0].value == '<Unmap>':
                binds.append(node.lineno)
            if attr == 'withdraw':
                withdraws.append(node.lineno)
        self.assertTrue(binds, 'the <Unmap> interception is gone — "_" '
                               'iconifies to the taskbar again')
        self.assertTrue(withdraws, 'no withdraw call — minimize cannot '
                                   'reach the tray')

    def test_role_window_rides_the_host_root(self):
        src = _src('desktop/role_window.py')
        self.assertIn('parent_or_none', src)
        self.assertIn('tk.Toplevel(parent)', src,
                      'the window is a bare Tk() again — two interpreters '
                      'on two threads is back on the table')

    def test_dialogs_ride_the_host(self):
        for rel in ('desktop/connect_ui.py', 'desktop/post_install.py'):
            src = _src(rel)
            self.assertIn('parent_or_none', src,
                          '%s does not ride the tk host' % rel)
            self.assertIn('wait_window', src,
                          '%s lost the modal wait — a second interpreter '
                          'mainloop on the host thread' % rel)

    def test_specs_bundle_the_host(self):
        for rel in ('tofu.spec', 'tofu-agent.spec'):
            self.assertIn('desktop._tk_host', _src(rel),
                          '%s does not freeze desktop._tk_host — the '
                          'packaged app falls back to the legacy sequence '
                          'forever' % rel)


if __name__ == '__main__':
    unittest.main()
