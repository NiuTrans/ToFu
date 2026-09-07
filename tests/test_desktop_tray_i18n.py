"""tests/test_desktop_tray_i18n.py — the system tray is no longer English-only.

Both packaged apps (desktop/launcher.py full app, desktop/agent_launcher.py
agent) rendered EVERY pystray menu item as a hardcoded English literal —
the last English-only surface after the tk dialogs went bilingual through
desktop/_tk_theme.py. Owner directive 2026-08-03: the tray has no i18n.

These suites pin:

* **No hardcoded MenuItem literals** — an AST ratchet over both launchers:
  the first positional argument of every ``MenuItem(...)`` call must not be
  a string constant (a callable / t() lookup is fine). Deleting the t()
  wiring on ANY item turns exactly this red — the NEUTER target.
* **Key coverage** — every ``desktop.tray.*`` key the launchers reference
  exists in _tk_theme.STRINGS with BOTH languages; a key referenced but
  never added crashes the menu at runtime, so the build fails here first.
* **Real translations** — zh values are not the English text re-shipped
  (the classic fake-i18n), and placeholder tokens ({tag}/{url}/{port})
  survive in both languages so .replace() keeps working.

Headless-safe: source-level + STRINGS only, no tkinter, no pystray.
"""

import ast
import os
import re
import sys
import unittest

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

pytestmark = pytest.mark.unit

_LAUNCHERS = ('desktop/launcher.py', 'desktop/agent_launcher.py')
_KEY_RE = re.compile(r"desktop\.tray\.[A-Za-z]+")
_PLACEHOLDER_RE = re.compile(r"\{[a-z]+\}")


def _src(rel):
    with open(os.path.join(_REPO, rel), encoding='utf-8') as f:
        return f.read()


def _referenced_tray_keys(rel):
    return set(_KEY_RE.findall(_src(rel)))


class NoHardcodedMenuItemsTest(unittest.TestCase):
    """AST ratchet — NEUTER target: restore ANY literal MenuItem text and
    exactly these tests go red."""

    def _literal_items(self, rel):
        tree = ast.parse(_src(rel), filename=rel)
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else getattr(func, 'id', ''))
            if name != 'MenuItem':
                continue
            if (node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                bad.append((node.lineno, node.args[0].value))
        return bad

    def test_full_app_tray_has_no_literal_menu_text(self):
        bad = self._literal_items('desktop/launcher.py')
        self.assertEqual(bad, [],
                         'launcher.py hardcodes tray text %s — tray strings '
                         'belong in _tk_theme.STRINGS (desktop.tray.*)' % bad)

    def test_agent_tray_has_no_literal_menu_text(self):
        bad = self._literal_items('desktop/agent_launcher.py')
        self.assertEqual(bad, [],
                         'agent_launcher.py hardcodes tray text %s — tray '
                         'strings belong in _tk_theme.STRINGS '
                         '(desktop.tray.*)' % bad)


class TrayKeyCoverageTest(unittest.TestCase):
    """Every desktop.tray.* key a launcher references must exist, in both
    languages, with placeholders intact."""

    def _strings(self):
        import desktop._tk_theme as theme
        return theme.STRINGS

    def test_both_launchers_actually_use_tray_keys(self):
        """Wiring NEUTER target: a launcher that references ZERO tray keys
        has been unwired — fail loudly instead of green-by-absence."""
        for rel in _LAUNCHERS:
            keys = _referenced_tray_keys(rel)
            self.assertGreaterEqual(
                len(keys), 6,
                '%s references only %d desktop.tray.* keys — the t() '
                'wiring was stripped' % (rel, len(keys)))

    def test_referenced_keys_exist_with_both_languages(self):
        strings = self._strings()
        for rel in _LAUNCHERS:
            for key in sorted(_referenced_tray_keys(rel)):
                pair = strings.get(key)
                self.assertIsNotNone(
                    pair, '%s references %s which is missing from '
                    '_tk_theme.STRINGS' % (rel, key))
                self.assertIn('en', pair, '%s missing en' % key)
                self.assertIn('zh', pair, '%s missing zh' % key)

    def test_zh_is_a_real_translation(self):
        strings = self._strings()
        for key, pair in strings.items():
            if not key.startswith('desktop.tray.'):
                continue
            self.assertNotEqual(pair['en'], pair['zh'],
                                '%s ships English as its Chinese — fake '
                                'i18n' % key)

    def test_placeholders_survive_both_languages(self):
        strings = self._strings()
        for key, pair in strings.items():
            if not key.startswith('desktop.tray.'):
                continue
            en_ph = sorted(_PLACEHOLDER_RE.findall(pair['en']))
            zh_ph = sorted(_PLACEHOLDER_RE.findall(pair['zh']))
            self.assertEqual(en_ph, zh_ph,
                             '%s placeholder mismatch en=%s zh=%s — the '
                             '.replace() call site fills only one language'
                             % (key, en_ph, zh_ph))


if __name__ == '__main__':
    unittest.main()
