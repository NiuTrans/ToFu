"""tests/test_desktop_tk_theme.py — desktop/_tk_theme.py + themed dialog wiring.

The desktop build's FIRST screen after installation is a tkinter dialog
(first-launch component picker, remote-connect prompt). Both were stock
gray ttk with hardcoded English strings and, worst of all, the component
installer ran invisibly: clicking "Install Selected" closed the dialog and
~165 MB downloaded with no progress anywhere (progress_callback existed but
was never wired to UI).

These suites pin:

* **detect_dark()** — Windows registry / macOS defaults / Linux gsettings,
  plus the TOFU_THEME override which must WIN over the OS probe, and a
  light fallback for every failure shape (headless CI, no winreg, no
  gsettings, unknown platform). Each branch has a dedicated NEUTER target:
  deleting the branch must turn exactly its tests red.
* **detect_lang() / t()** — TOFU_LANG override, zh locale detection, en
  fallback; every string key carries both languages.
* **Palettes** — LIGHT and DARK carry identical keys, all #rrggbb, so a
  dialog can never reference a token missing in one theme.
* **Source ratchets** — post_install.py and launcher.py consume the theme
  module (no ad-hoc colors/fonts) and the installer progress is WIRED
  (queue + after-poll + progress_callback), because an unwired progress
  pipe is exactly the bug that shipped.

Headless-safe: _tk_theme imports tkinter lazily inside functions, so this
suite never needs a display.
"""

import ast
import os
import sys
import types
import unittest
from unittest import mock

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

pytestmark = pytest.mark.unit


def _theme():
    import desktop._tk_theme as theme
    return theme


class DarkDetectEnvOverrideTest(unittest.TestCase):
    """TOFU_THEME must WIN over the OS probe. NEUTER target: delete the env
    branch and these two go red while every platform test stays green."""

    def test_env_dark_wins_without_probing_os(self):
        theme = _theme()
        with mock.patch.dict(os.environ, {'TOFU_THEME': 'dark'}):
            with mock.patch.object(theme.subprocess, 'run') as run:
                self.assertTrue(theme.detect_dark())
        run.assert_not_called()

    def test_env_light_wins_on_dark_os(self):
        theme = _theme()
        with mock.patch.dict(os.environ, {'TOFU_THEME': 'light'}):
            with mock.patch.object(theme.sys, 'platform', 'darwin'):
                with mock.patch.object(theme.subprocess, 'run') as run:
                    self.assertFalse(theme.detect_dark())
        run.assert_not_called()

    def test_env_garbage_falls_through_to_probe(self):
        theme = _theme()
        with mock.patch.dict(os.environ, {'TOFU_THEME': 'banana'}):
            with mock.patch.object(theme.sys, 'platform', 'linux'):
                with mock.patch.object(theme.subprocess, 'run') as run:
                    run.side_effect = FileNotFoundError
                    self.assertFalse(theme.detect_dark())
        run.assert_called()


class DarkDetectWindowsTest(unittest.TestCase):
    """Windows branch: HKCU AppsUseLightTheme. NEUTER target: delete the
    win32 branch → the dark case goes red (it would fall to light)."""

    def _fake_winreg(self, value):
        mod = types.ModuleType('winreg')
        mod.HKEY_CURRENT_USER = object()
        mod.OpenKey = lambda *a, **k: object()
        if value is None:
            def _raise(*a, **k):
                raise OSError('no such value')
            mod.QueryValueEx = _raise
        else:
            mod.QueryValueEx = lambda *a, **k: (value, 4)
        mod.CloseKey = lambda *a, **k: None
        return mod

    def test_windows_dark(self):
        theme = _theme()
        fake = self._fake_winreg(0)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TOFU_THEME', None)
            with mock.patch.object(theme.sys, 'platform', 'win32'):
                with mock.patch.dict(sys.modules, {'winreg': fake}):
                    self.assertTrue(theme.detect_dark())

    def test_windows_light(self):
        theme = _theme()
        fake = self._fake_winreg(1)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TOFU_THEME', None)
            with mock.patch.object(theme.sys, 'platform', 'win32'):
                with mock.patch.dict(sys.modules, {'winreg': fake}):
                    self.assertFalse(theme.detect_dark())

    def test_windows_probe_failure_falls_back_light(self):
        theme = _theme()
        fake = self._fake_winreg(None)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TOFU_THEME', None)
            with mock.patch.object(theme.sys, 'platform', 'win32'):
                with mock.patch.dict(sys.modules, {'winreg': fake}):
                    self.assertFalse(theme.detect_dark())


class DarkDetectMacLinuxTest(unittest.TestCase):
    """macOS/Linux branches. NEUTER targets: delete the darwin branch →
    test_mac_dark red; delete the linux gsettings probe → test_linux_dark
    red."""

    def _cp(self, rc, out):
        return types.SimpleNamespace(returncode=rc, stdout=out)

    def test_mac_dark(self):
        theme = _theme()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TOFU_THEME', None)
            with mock.patch.object(theme.sys, 'platform', 'darwin'):
                with mock.patch.object(theme.subprocess, 'run',
                                       return_value=self._cp(0, 'Dark\n')):
                    self.assertTrue(theme.detect_dark())

    def test_mac_light_when_key_absent(self):
        theme = _theme()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TOFU_THEME', None)
            with mock.patch.object(theme.sys, 'platform', 'darwin'):
                with mock.patch.object(theme.subprocess, 'run',
                                       return_value=self._cp(1, '')):
                    self.assertFalse(theme.detect_dark())

    def test_mac_defaults_missing_falls_back_light(self):
        theme = _theme()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TOFU_THEME', None)
            with mock.patch.object(theme.sys, 'platform', 'darwin'):
                with mock.patch.object(theme.subprocess, 'run',
                                       side_effect=FileNotFoundError):
                    self.assertFalse(theme.detect_dark())

    def test_linux_dark(self):
        theme = _theme()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TOFU_THEME', None)
            with mock.patch.object(theme.sys, 'platform', 'linux'):
                with mock.patch.object(
                        theme.subprocess, 'run',
                        return_value=self._cp(0, "'prefer-dark'\n")):
                    self.assertTrue(theme.detect_dark())

    def test_linux_light(self):
        theme = _theme()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TOFU_THEME', None)
            with mock.patch.object(theme.sys, 'platform', 'linux'):
                with mock.patch.object(
                        theme.subprocess, 'run',
                        return_value=self._cp(0, "'default'\n")):
                    self.assertFalse(theme.detect_dark())

    def test_linux_no_gsettings_falls_back_light(self):
        theme = _theme()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TOFU_THEME', None)
            with mock.patch.object(theme.sys, 'platform', 'linux'):
                with mock.patch.object(theme.subprocess, 'run',
                                       side_effect=FileNotFoundError):
                    self.assertFalse(theme.detect_dark())

    def test_unknown_platform_falls_back_light(self):
        theme = _theme()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TOFU_THEME', None)
            with mock.patch.object(theme.sys, 'platform', 'sunos5'):
                self.assertFalse(theme.detect_dark())


class LangDetectTest(unittest.TestCase):
    """TOFU_LANG override wins; zh locale → zh; anything else → en.
    NEUTER target: delete the env branch → override tests red."""

    def test_env_override_zh(self):
        theme = _theme()
        with mock.patch.dict(os.environ, {'TOFU_LANG': 'zh'}):
            self.assertEqual(theme.detect_lang(), 'zh')

    def test_env_override_en_beats_zh_locale(self):
        theme = _theme()
        with mock.patch.dict(os.environ, {'TOFU_LANG': 'en'}):
            with mock.patch.object(theme.locale, 'getlocale',
                                   return_value=('zh_CN', 'UTF-8')):
                self.assertEqual(theme.detect_lang(), 'en')

    def test_locale_zh(self):
        theme = _theme()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TOFU_LANG', None)
            with mock.patch.object(theme.locale, 'getlocale',
                                   return_value=('zh_CN', 'UTF-8')):
                self.assertEqual(theme.detect_lang(), 'zh')

    def test_locale_en(self):
        theme = _theme()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TOFU_LANG', None)
            with mock.patch.object(theme.locale, 'getlocale',
                                   return_value=('en_US', 'UTF-8')):
                self.assertEqual(theme.detect_lang(), 'en')

    def test_locale_none_falls_back_to_env_lang(self):
        theme = _theme()
        env = {k: v for k, v in os.environ.items()
               if k not in ('TOFU_LANG', 'LC_ALL', 'LC_MESSAGES')}
        env['LANG'] = 'zh_CN.UTF-8'
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(theme.locale, 'getlocale',
                                   return_value=(None, None)):
                self.assertEqual(theme.detect_lang(), 'zh')

    def test_nothing_known_is_en(self):
        theme = _theme()
        env = {k: v for k, v in os.environ.items()
               if k not in ('TOFU_LANG', 'LANG', 'LC_ALL', 'LC_MESSAGES')}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(theme.locale, 'getlocale',
                                   return_value=(None, None)):
                self.assertEqual(theme.detect_lang(), 'en')


class LangDetectWindowsNamesTest(unittest.TestCase):
    """Owner report 2026-08-04: a zh-CN Windows machine rendered the whole
    native surface in ENGLISH. locale.getlocale() returns DISPLAY names on
    Windows ('Chinese (Simplified)_China'), which the old startswith('zh')
    check missed, and the `if loc: return 'en'` branch then hard-locked
    English. NEUTER target: drop the 'chinese' prefix branch — both zh
    cases here go red while every ISO-code test stays green."""

    def _lang_with(self, loc):
        theme = _theme()
        env = {k: v for k, v in os.environ.items()
               if k not in ('TOFU_LANG', 'LANG', 'LC_ALL', 'LC_MESSAGES')}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(theme.locale, 'getlocale',
                                   return_value=loc):
                return theme.detect_lang()

    def test_simplified_chinese_display_name(self):
        self.assertEqual(
            self._lang_with(('Chinese (Simplified)_China', '936')), 'zh')

    def test_traditional_chinese_display_name(self):
        self.assertEqual(
            self._lang_with(('Chinese (Traditional)_Taiwan', '950')), 'zh')

    def test_dashed_iso_code_normalises(self):
        self.assertEqual(self._lang_with(('zh-CN', 'UTF-8')), 'zh')

    def test_english_display_name_is_en(self):
        self.assertEqual(
            self._lang_with(('English_United States', '1252')), 'en')


class FontStackTest(unittest.TestCase):
    """The serif-UI root fix: the family is RESOLVED from an explicit
    per-platform stack, CJK-first for zh — never left to tk's default
    (SimSun, a serif face, on a Chinese-locale Windows). NEUTER target:
    make pick_font_family return '' unconditionally — every case red."""

    def test_windows_en_prefers_segoe(self):
        theme = _theme()
        fam = theme.pick_font_family({'Segoe UI', 'Microsoft YaHei UI'},
                                     platform='win32', lang='en')
        self.assertEqual(fam, 'Segoe UI')

    def test_windows_zh_prefers_yahei(self):
        theme = _theme()
        fam = theme.pick_font_family({'Segoe UI', 'Microsoft YaHei UI'},
                                     platform='win32', lang='zh')
        self.assertEqual(fam, 'Microsoft YaHei UI')

    def test_first_missing_falls_to_next(self):
        theme = _theme()
        fam = theme.pick_font_family({'Tahoma'}, platform='win32', lang='en')
        self.assertEqual(fam, 'Tahoma')

    def test_match_is_case_insensitive(self):
        theme = _theme()
        fam = theme.pick_font_family({'segoe ui'}, platform='win32',
                                     lang='en')
        self.assertEqual(fam, 'Segoe UI')

    def test_none_available_returns_empty_for_tk_default(self):
        theme = _theme()
        fam = theme.pick_font_family({'Courier New'}, platform='win32',
                                     lang='en')
        self.assertEqual(fam, '')

    def test_linux_zh_prefers_cjk(self):
        theme = _theme()
        fam = theme.pick_font_family({'Noto Sans', 'Noto Sans CJK SC'},
                                     platform='linux', lang='zh')
        self.assertEqual(fam, 'Noto Sans CJK SC')

    def test_mac_zh_prefers_pingfang(self):
        theme = _theme()
        fam = theme.pick_font_family({'Helvetica Neue', 'PingFang SC'},
                                     platform='darwin', lang='zh')
        self.assertEqual(fam, 'PingFang SC')


class CenterGeometryTest(unittest.TestCase):
    """Pure math behind center_on_screen — the window must open centred,
    and a window larger than the screen must clamp, never open with its
    title bar off-screen."""

    def test_centers_optically(self):
        theme = _theme()
        # x = (1920-500)/2 = 710; y = (1080-600)*0.38 = 182.
        self.assertEqual(theme._center_geometry(1920, 1080, 500, 600),
                         '+710+182')

    def test_oversized_window_clamps_to_origin(self):
        theme = _theme()
        self.assertEqual(theme._center_geometry(800, 600, 2000, 1200),
                         '+0+0')


class StringsTest(unittest.TestCase):

    def test_t_returns_zh_and_en(self):
        theme = _theme()
        self.assertEqual(theme.t('desktop.components.title', lang='en'),
                         'Optional Components')
        self.assertEqual(theme.t('desktop.components.title', lang='zh'),
                         '可选组件')

    def test_t_unknown_key_returns_key(self):
        theme = _theme()
        self.assertEqual(theme.t('desktop.nope', lang='en'), 'desktop.nope')

    def test_every_key_has_both_languages(self):
        theme = _theme()
        for key, pair in theme.STRINGS.items():
            self.assertIn('en', pair, '%s missing en' % key)
            self.assertIn('zh', pair, '%s missing zh' % key)
            self.assertTrue(pair['en'].strip(), '%s empty en' % key)
            self.assertTrue(pair['zh'].strip(), '%s empty zh' % key)


class PaletteTest(unittest.TestCase):

    def test_palettes_have_identical_keys(self):
        theme = _theme()
        self.assertEqual(set(theme.LIGHT), set(theme.DARK),
                         'a token missing from one theme renders the other '
                         'theme with a KeyError at dialog-build time')

    def test_all_tokens_are_hex_colors(self):
        theme = _theme()
        import re
        for name, pal in (('LIGHT', theme.LIGHT), ('DARK', theme.DARK)):
            for token, val in pal.items():
                self.assertRegex(val, r'^#[0-9a-fA-F]{6}$',
                                 '%s[%s]=%r is not #rrggbb' % (name, token, val))

    def test_current_palette_picks_by_detect_dark(self):
        theme = _theme()
        with mock.patch.object(theme, 'detect_dark', return_value=True):
            self.assertIs(theme.current_palette(), theme.DARK)
        with mock.patch.object(theme, 'detect_dark', return_value=False):
            self.assertIs(theme.current_palette(), theme.LIGHT)


class ThemedWiringRatchetTest(unittest.TestCase):
    """Source-level pins: the two dialogs CONSUME the theme (no ad-hoc
    styling can silently come back) and the install progress is wired.

    These are ratchets, not behaviour tests — they exist because the bug
    they guard (stock-gray dialog, invisible 165 MB download) was invisible
    to every behavioural suite until a human looked at the screen.
    """

    def _src(self, rel):
        with open(os.path.join(_REPO, rel), encoding='utf-8') as f:
            return f.read()

    def test_both_dialogs_import_theme(self):
        for rel in ('desktop/post_install.py', 'desktop/launcher.py'):
            tree = ast.parse(self._src(rel), filename=rel)
            # Resolve the FULL dotted path of what gets imported:
            #   from desktop import _tk_theme   → desktop._tk_theme
            #   import desktop._tk_theme        → desktop._tk_theme
            #   from desktop._tk_theme import t → desktop._tk_theme
            full = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    full.add(node.module)
                    full.update('%s.%s' % (node.module, a.name)
                                for a in node.names)
                elif isinstance(node, ast.Import):
                    full.update(a.name for a in node.names)
            self.assertTrue(
                any(m.endswith('_tk_theme') or '._tk_theme.' in m
                    for m in full),
                '%s does not import desktop._tk_theme — an ad-hoc-styled '
                'dialog is back' % rel)

    def test_no_legacy_hardcoded_colors(self):
        for rel in ('desktop/post_install.py', 'desktop/launcher.py'):
            src = self._src(rel)
            for legacy in ("foreground='gray'", "foreground='#666'",
                           "foreground='#b00'"):
                self.assertNotIn(legacy, src,
                                 '%s reintroduced the stock-gray hardcoded '
                                 'color %s — colors belong in _tk_theme' %
                                 (rel, legacy))

    def test_theme_resolves_a_real_font_family(self):
        """NEUTER target: restore base_font = ('', 10) in apply_theme and
        this goes red — the empty family falls through to SimSun (serif)
        on a Chinese-locale Windows, the 2026-08-04「界面丑」report."""
        src = self._src('desktop/_tk_theme.py')
        self.assertIn('pick_font_family', src,
                      'apply_theme no longer resolves a font family — the '
                      'tk default (serif on zh Windows) is back')
        self.assertIn('base_font = _f(10)', src,
                      'base_font is built from the resolved family')
        self.assertNotIn("base_font = ('', 10)", src,
                         'the bare empty-family font is back — serif UI '
                         'on zh Windows')

    def test_progress_is_wired_not_silent(self):
        """The invisible-install bug: _install_components was called with NO
        progress_callback and its results only hit a log file. The dialog
        must (a) pass a progress_callback into install(), (b) marshal worker
        events onto the tk thread via a Queue + after(), and (c) keep the
        dialog OPEN during install (the old flow closed it immediately)."""
        src = self._src('desktop/post_install.py')
        self.assertIn('queue.Queue', src,
                      'no worker→UI event queue — progress cannot reach tk '
                      'thread-safely')
        self.assertIn('.after(', src,
                      'no after()-poll draining the queue — the dialog does '
                      'not repaint progress')
        # Pin the CALL SITE, not the substring: 'progress_callback' alone
        # also matches the install() signatures, so a neutered worker (which
        # drops the kwarg) still contained it — measured NEUTER-miss
        # 2026-08-01. 'progress_callback=lambda' exists ONLY at the real
        # worker call site.
        self.assertIn('progress_callback=lambda', src,
                      'install() called without progress_callback again — '
                      'the 165 MB silent download is back')


if __name__ == '__main__':
    unittest.main()
