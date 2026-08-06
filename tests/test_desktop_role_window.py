"""tests/test_desktop_role_window.py — the startup role window's pure core.

Owner directive 2026-08-03: the desktop app must SAY whether it is the
client or the server at startup, and the client controls must not live
ONLY in the system tray. Design: docs/DESKTOP_STARTUP_ROLE_UX_DESIGN.md
(S2 = this module + agent wiring; S3 = full-app wiring).

The tk renderer cannot run on headless CI, so every FACT the window shows
comes from pure builders (role_state_full / role_state_agent) and the
startup gate (should_show_at_startup) — these suites pin the SENTENCES
and the gate, not pixels:

* **Role sentences** — full app declares the server role, agent declares
  the controlled role, in BOTH languages. NEUTER target: neuter the role
  line and these go red.
* **Dual-role fact** — the full app ALSO admits when it is a controlled
  endpoint of a remote server (the exact invisibility behind the
  2026-08-02 tunnel incident). NEUTER target: delete dual_role.
* **Startup gate** — absent config key (fresh install AND upgrades from
  builds before this window existed) shows the window; an explicit
  "don't show" persists and is honored. NEUTER target: flip the default.
* **Wiring ratchets** — both launchers import role_window, show it at
  startup, and the tray gained the "Control panel…" re-entry, so the
  panel is never tray-hidden again.
"""

import ast
import os
import sys
import unittest
from unittest import mock

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

pytestmark = pytest.mark.unit


def _rw():
    import desktop.role_window as rw
    return rw


def _src(rel):
    with open(os.path.join(_REPO, rel), encoding='utf-8') as f:
        return f.read()


class FullRoleStateTest(unittest.TestCase):

    def test_role_sentence_declares_server_both_languages(self):
        rw = _rw()
        st = rw.role_state_full(15000, {}, '', lang='zh')
        self.assertIn('服务器', st['role'])
        st = rw.role_state_full(15000, {}, '', lang='en')
        self.assertIn('server', st['role'].lower())

    def test_server_url_uses_the_actual_port(self):
        rw = _rw()
        st = rw.role_state_full(14963, {}, '', lang='en')
        self.assertEqual(st['server_url'], 'http://127.0.0.1:14963')

    def test_dual_role_only_when_attached(self):
        """NEUTER target: delete the dual_role computation and the attached
        case goes red — a full app silently acting as someone else's
        controlled endpoint must be VISIBLE (2026-08-02 tunnel incident)."""
        rw = _rw()
        self.assertFalse(rw.role_state_full(15000, {}, '', lang='en')
                         ['dual_role'])
        st = rw.role_state_full(15000, {}, 'http://nas:15000', lang='en')
        self.assertTrue(st['dual_role'])
        self.assertEqual(st['attached_url'], 'http://nas:15000')

    def test_cc_facts_pass_through(self):
        rw = _rw()
        cc = {'enabled': True, 'perms': {'allow_write': True,
                                         'allow_exec': False}}
        st = rw.role_state_full(15000, cc, '', lang='en')
        self.assertTrue(st['cc_enabled'])
        self.assertEqual(st['perms'], {'allow_write': True,
                                       'allow_exec': False})
        self.assertEqual(st['tiers'],
                         ['allow_write', 'allow_exec', 'allow_gui'])

    def test_show_flag_defaults_true_in_state(self):
        rw = _rw()
        self.assertTrue(rw.role_state_full(15000, {}, '', lang='en')
                        ['show_at_startup'])
        self.assertFalse(rw.role_state_full(15000, {}, '', show_flag=False,
                                            lang='en')['show_at_startup'])


class AgentRoleStateTest(unittest.TestCase):

    def test_role_sentence_declares_controlled_both_languages(self):
        rw = _rw()
        st = rw.role_state_agent('http://s:1', {}, None, lang='zh')
        self.assertIn('受控端', st['role'])
        st = rw.role_state_agent('http://s:1', {}, None, lang='en')
        self.assertIn('controlled', st['role'].lower())

    def test_attachment_fact(self):
        rw = _rw()
        st = rw.role_state_agent('http://nas:15000', {}, None, lang='en')
        self.assertEqual(st['server_url'], 'http://nas:15000')
        self.assertTrue(st['attached'])
        st = rw.role_state_agent('', {}, None, lang='en')
        self.assertFalse(st['attached'])

    def test_autostart_none_means_unsupported(self):
        rw = _rw()
        st = rw.role_state_agent('http://s:1', {}, None, lang='en')
        self.assertIsNone(st['autostart'])
        st = rw.role_state_agent('http://s:1', {}, True, lang='en')
        self.assertTrue(st['autostart'])

    def test_agent_has_four_tiers_including_egress(self):
        rw = _rw()
        st = rw.role_state_agent('http://s:1', {}, None, lang='en')
        self.assertEqual(st['tiers'],
                         ['allow_write', 'allow_exec', 'allow_gui',
                          'allow_egress'])


class ShowAtStartupGateTest(unittest.TestCase):
    """The gate reads the agent config blob. NEUTER target: flip the
    absent-key default to False and the first test goes red — the window
    must appear for fresh installs AND for upgrades from pre-window builds
    (their config has no key either), or nobody ever learns it exists."""

    def _with_tmp_config(self, tmp, initial=None):
        import json
        path = os.path.join(tmp, 'desktop_agent.json')
        if initial is not None:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(initial, f)
        return mock.patch.dict(os.environ, {'TOFU_DESKTOP_CONFIG': path})

    def test_absent_key_shows(self):
        rw = _rw()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self._with_tmp_config(tmp):
                self.assertTrue(rw.should_show_at_startup())

    def test_persisted_false_hides(self):
        rw = _rw()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self._with_tmp_config(tmp, {'show_role_window': False}):
                self.assertFalse(rw.should_show_at_startup())

    def test_persist_round_trip(self):
        rw = _rw()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self._with_tmp_config(tmp):
                rw.persist_show_at_startup(False)
                self.assertFalse(rw.should_show_at_startup())
                rw.persist_show_at_startup(True)
                self.assertTrue(rw.should_show_at_startup())

    def test_persist_does_not_clobber_other_keys(self):
        rw = _rw()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self._with_tmp_config(tmp, {'agent_id': 'abc'}):
                rw.persist_show_at_startup(False)
                from lib.desktop_agent.config import load_config
                self.assertEqual(load_config().get('agent_id'), 'abc')


class RendererDesignRatchetTest(unittest.TestCase):
    """The 2026-08-04 redesign anchors: the window is centred and branded
    (no more tk-feather icon in the taskbar), and every tier carries its
    one-line explanation — the fact the tray never had room for. NEUTER
    target: delete a desc key from _TIER_DESC_KEYS or let one go missing
    from STRINGS."""

    def test_window_is_centered_and_branded(self):
        src = _src('desktop/role_window.py')
        self.assertIn('theme.center_on_screen(', src,
                      'the window opens wherever the WM drops it again')
        self.assertIn('theme.set_window_icon(', src,
                      'the tk-feather taskbar icon is back')

    def test_every_tier_desc_key_exists_both_languages(self):
        rw = _rw()
        import desktop._tk_theme as theme
        for tier, desc_key in rw._TIER_DESC_KEYS.items():
            self.assertIn(tier, rw._TIER_KEYS,
                          '%s has a desc but no label key' % tier)
            pair = theme.STRINGS.get(desc_key)
            self.assertIsNotNone(pair, '%s missing from STRINGS' % desc_key)
            self.assertIn('en', pair, '%s missing en' % desc_key)
            self.assertIn('zh', pair, '%s missing zh' % desc_key)

    def test_tier_keys_cover_both_apps_tiers(self):
        rw = _rw()
        full = rw.role_state_full(15000, {}, '', lang='en')['tiers']
        agent = rw.role_state_agent('http://s:1', {}, None,
                                    lang='en')['tiers']
        for tier in set(full) | set(agent):
            self.assertIn(tier, rw._TIER_KEYS,
                          '%s lost its label key' % tier)
            self.assertIn(tier, rw._TIER_DESC_KEYS,
                          '%s lost its desc key' % tier)


class WiringRatchetTest(unittest.TestCase):
    """Source-level pins so the panel can never silently vanish again."""

    def _imports_role_window(self, rel):
        tree = ast.parse(_src(rel), filename=rel)
        full = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                full.add(node.module)
                full.update('%s.%s' % (node.module, a.name)
                            for a in node.names)
            elif isinstance(node, ast.Import):
                full.update(a.name for a in node.names)
        return any('role_window' in m for m in full)

    def test_both_launchers_import_role_window(self):
        for rel in ('desktop/launcher.py', 'desktop/agent_launcher.py'):
            self.assertTrue(self._imports_role_window(rel),
                            '%s does not import desktop.role_window — the '
                            'startup role declaration is gone' % rel)

    def test_both_launchers_show_window_at_startup(self):
        for rel in ('desktop/launcher.py', 'desktop/agent_launcher.py'):
            src = _src(rel)
            self.assertIn('should_show_at_startup', src,
                          '%s lost the startup gate — the role window is '
                          'never offered' % rel)
            self.assertIn('show_role_window(', src,
                          '%s never calls show_role_window' % rel)

    def test_agent_window_wires_the_copy_diag_button(self):
        """2026-08-06: debugging a dead link on a machine with no shell
        access was blind — the agent window/tray carry「复制诊断信息」so
        the evidence pack (route / candidates / verdict / log tail) is one
        click away. NEUTER: drop the action or the button and this goes red.
        """
        launcher = _src('desktop/agent_launcher.py')
        self.assertIn("'copy_diag'", launcher,
                      'the agent window lost its diagnostics action')
        self.assertIn('_diag_report', launcher,
                      'the diagnostics report builder is gone')
        window = _src('desktop/role_window.py')
        self.assertIn('desktop.role.copyDiag', window,
                      'the agent role window lost the copy-diag button')
        import desktop._tk_theme as theme
        for key in ('desktop.role.copyDiag', 'desktop.role.copyDiagDone',
                    'desktop.tray.copyDiag'):
            pair = theme.STRINGS.get(key)
            self.assertIsNotNone(pair, '%s missing from STRINGS' % key)
            self.assertIn('en', pair, '%s missing en' % key)
            self.assertIn('zh', pair, '%s missing zh' % key)

    def test_tray_gains_control_panel_reentry(self):
        for rel in ('desktop/launcher.py', 'desktop/agent_launcher.py'):
            self.assertIn('desktop.tray.controlPanel', _src(rel),
                          '%s tray lost the Control-panel re-entry — the '
                          'panel is tray-hidden again' % rel)


if __name__ == '__main__':
    unittest.main()
