#!/usr/bin/env python3
"""tests/test_desktop_native_i18n.py — no raw English may reach a native surface.

Owner review 2026-08-04 (rejecting「i18n 全量落实」): spot-checks found five
English leaks in the SAME dialogs that had just been redesigned —

1. parse_connect_line's ValueError sentences rendered raw in the connect
   dialog (``err.config(text=str(ve))``);
2. probe/pair machine tokens ('unreachable', 'http_404', 'not_tofu'…)
   interpolated raw into otherwise-localized messages;
3. Component size hints ('~115 MB download') hardcoded as class attributes;
4. install()/progress messages ('Chromium browser installed successfully.',
   'Installation failed: …') rendered raw in the progress view;
5. the tray link line's ``str(detail or code)`` fallback.

The ROOT discipline this suite pins: lib modules return MACHINE TOKENS,
never prose; the UI boundary (connect_ui / role_window / post_install /
launchers) maps them through the THREE theme helpers (reason_text /
connect_error_text / component_msg); unknown tokens pass through verbatim
in dev but the census ratchet below demands a key for every token the lib
can emit, so a new token without a translation fails the build here, not
on a Chinese user's screen.

Headless-safe: pure functions + STRINGS + source/AST ratchets only.
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


def _theme():
    import desktop._tk_theme as theme
    return theme


def _src(rel):
    with open(os.path.join(_REPO, rel), encoding='utf-8') as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════
#  reason_text — probe/pair tokens
# ═══════════════════════════════════════════════════════════════

class ReasonTextTest(unittest.TestCase):

    def test_every_known_token_maps_both_languages(self):
        theme = _theme()
        for token in ('unreachable', 'timeout', 'error', 'not_tofu',
                      'bad_response'):
            en = theme.reason_text(token, lang='en')
            zh = theme.reason_text(token, lang='zh')
            self.assertTrue(en.strip() and zh.strip(), token)
            self.assertNotEqual(en, zh,
                                '%s ships English as its Chinese' % token)
            self.assertNotEqual(zh, token,
                                '%s renders the raw token as Chinese' % token)

    def test_http_token_fills_the_code(self):
        theme = _theme()
        self.assertIn('404', theme.reason_text('http_404', lang='en'))
        self.assertIn('404', theme.reason_text('http_404', lang='zh'))
        self.assertIn('HTTP', theme.reason_text('http_404', lang='zh'))

    def test_unknown_token_passes_through_verbatim(self):
        theme = _theme()
        self.assertEqual(theme.reason_text('some_future_token', lang='zh'),
                         'some_future_token')

    def test_empty_token_is_empty(self):
        theme = _theme()
        self.assertEqual(theme.reason_text('', lang='zh'), '')


class LibTokenCensusRatchetTest(unittest.TestCase):
    """Every machine token _probe.py / _pair.py can return must be mapped
    (or deliberately dialog-handled). NEUTER target: add
    ``return False, 'dns_poisoned'`` to _probe.py without a key — this goes
    red naming the orphan, instead of a Chinese user reading
   「dns_poisoned」in their dialog."""

    # The pair dialog's handled tokens (invalid_code / rate_limited)
    # left with exchange_pair_code in 2026-08-05's pairing retirement.
    _DIALOG_HANDLED: set = set()
    _TOKEN_RE = re.compile(r"return\s+False,\s*'([a-z_0-9%]+)'")

    def _lib_tokens(self, rel):
        tokens = set()
        for match in self._TOKEN_RE.finditer(_src(rel)):
            tok = match.group(1)
            if '%' in tok:  # the 'http_%d' template — family-covered
                tokens.add('http_*')
            else:
                tokens.add(tok)
        return tokens

    def test_probe_and_pair_tokens_are_all_mapped(self):
        theme = _theme()
        mapped = set(theme._REASON_KEYS) | self._DIALOG_HANDLED
        for rel in ('lib/desktop_agent/_probe.py',
                    'lib/desktop_agent/_pair.py'):
            for tok in sorted(self._lib_tokens(rel)):
                if tok == 'http_*':
                    continue  # reason_text handles the http_ family
                self.assertIn(
                    tok, mapped,
                    '%s returns token %r with no localized mapping — it '
                    'renders raw in a zh dialog' % (rel, tok))


# ═══════════════════════════════════════════════════════════════
#  connect_error_text — parse_connect_line coded refusals
# ═══════════════════════════════════════════════════════════════

class ConnectErrorTextTest(unittest.TestCase):

    def test_parse_raises_coded_errors(self):
        from lib.desktop_agent.config import (ConnectLineError,
                                              parse_connect_line)
        cases = [('tok_ONLY', 'missing_parts'),
                 ('', 'missing_parts'),
                 ('https://tofu.example.com', 'missing_parts'),
                 ('a b c', 'too_many_parts'),
                 ('ftp://x tok_1', 'bad_url')]
        for line, code in cases:
            with self.assertRaises(ConnectLineError) as ei:
                parse_connect_line(line)
            self.assertEqual(ei.exception.code, code, line)
            self.assertTrue(str(ei.exception).strip(),
                            'a coded refusal must still str() non-empty '
                            '(logs + the contract suite pin this)')

    def test_refusal_never_echoes_the_secret(self):
        from lib.desktop_agent.config import parse_connect_line
        with self.assertRaises(ValueError) as ei:
            parse_connect_line('ftp://tofu.example.com tok_SUPERSECRET')
        self.assertNotIn('tok_SUPERSECRET', str(ei.exception))
        self.assertNotIn('tok_SUPERSECRET',
                         getattr(ei.exception, 'detail', ''))

    def test_every_code_maps_both_languages(self):
        theme = _theme()
        from lib.desktop_agent.config import ConnectLineError
        for code in ('missing_parts', 'too_many_parts', 'bad_url'):
            err = ConnectLineError(code, detail='ftp://x' if code == 'bad_url'
                                   else '')
            en = theme.connect_error_text(err, lang='en')
            zh = theme.connect_error_text(err, lang='zh')
            self.assertNotEqual(en, zh, code)
            self.assertNotIn('connect_line:', zh,
                             '%s leaks the machine token into the dialog'
                             % code)
        self.assertIn('ftp://x', theme.connect_error_text(
            ConnectLineError('bad_url', detail='ftp://x'), lang='zh'))

    def test_unknown_valueerror_passes_through(self):
        theme = _theme()
        self.assertEqual(theme.connect_error_text(ValueError('legacy'),
                                                  lang='zh'), 'legacy')


# ═══════════════════════════════════════════════════════════════
#  component_msg — install worker tokens + detail: prefix
# ═══════════════════════════════════════════════════════════════

class ComponentMsgTest(unittest.TestCase):

    def test_every_token_maps_both_languages(self):
        theme = _theme()
        for token in theme._COMP_MSG_KEYS:
            en = theme.component_msg(token, lang='en')
            zh = theme.component_msg(token, lang='zh')
            self.assertTrue(en.strip() and zh.strip(), token)
            self.assertNotEqual(en, zh, token)
            self.assertNotEqual(zh, token,
                                '%s renders the raw token as Chinese' % token)

    def test_detail_prefix_is_localized_tail_stays_raw(self):
        theme = _theme()
        out = theme.component_msg('detail:make: some stderr blob', lang='zh')
        self.assertTrue(out.startswith('安装失败'), out)
        self.assertIn('make: some stderr blob', out)
        out_en = theme.component_msg('detail:boom', lang='en')
        self.assertTrue(out_en.startswith('Installation failed'), out_en)

    def test_unknown_message_passes_through(self):
        theme = _theme()
        self.assertEqual(theme.component_msg('totally novel', lang='zh'),
                         'totally novel')


# ═══════════════════════════════════════════════════════════════
#  Composed copy — the shell + reason seam must read clean
# ═══════════════════════════════════════════════════════════════

class ComposedCopyTest(unittest.TestCase):
    """Owner final polish 2026-08-04: the most-read failure copy in the
    product doubled its verb at the shell/reason seam —
    'Cannot reach Tofu there: the address cannot be reached.' and
    「连不上服务器：无法连接到该地址。」 — in BOTH languages and BOTH
    dialogs. unreachable is the dominant probe/pair failure (server off /
    wrong port / dead tunnel), so this exact composed string is what a
    user reads most often. NEUTER target: restore a reach-verb phrase in
    reason.unreachable and both pins go red."""

    def test_unreachable_phrase_is_shell_compatible(self):
        theme = _theme()
        self.assertEqual(theme.reason_text('unreachable', 'en'),
                         'no answer from the server')
        self.assertEqual(theme.reason_text('unreachable', 'zh'),
                         '服务器无响应')

    def test_unreachable_composes_without_doubled_wording(self):
        theme = _theme()
        # The pair dialog's shell (desktop.pair.failed) left with the
        # dialog itself (2026-08-05 retirement); the connect-line shell
        # remains the live seam.
        for shell_key in ('desktop.connect.verifyFailed',):
            en = theme.t(shell_key, 'en').replace(
                '{reason}', theme.reason_text('unreachable', 'en'))
            zh = theme.t(shell_key, 'zh').replace(
                '{reason}', theme.reason_text('unreachable', 'zh'))
            self.assertNotIn('cannot be reached', en,
                             '%s en doubles the reach verb again' % shell_key)
            self.assertNotIn('无法连接', zh,
                             '%s zh doubles 连不上/无法连接 again' % shell_key)
            # The reason still carries information, not a euphemism void:
            self.assertIn('server', en.lower())
            self.assertIn('服务器', zh)

    def test_other_reason_tokens_compose_without_the_same_disease(self):
        """The sibling tokens, checked for the same shell-verb collision so
        this stays fixed as a CLASS, not a one-word patch: none of them may
        contain the shells' reach/连 verb or the pair shell's pair verb."""
        theme = _theme()
        for token in ('timeout', 'error', 'not_tofu', 'bad_response'):
            en_reason = theme.reason_text(token, 'en').lower()
            zh_reason = theme.reason_text(token, 'zh')
            self.assertNotIn('reach', en_reason,
                             '%s en reason reuses the shell verb' % token)
            self.assertNotIn('pair', en_reason,
                             '%s en reason reuses the pair verb' % token)
            self.assertNotIn('连不上', zh_reason,
                             '%s zh reason reuses 连不上' % token)


# ═══════════════════════════════════════════════════════════════
#  Surface wiring ratchets — the boundaries must USE the mapping
# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
#  Surface wiring ratchets — the boundaries must USE the mapping
# ═══════════════════════════════════════════════════════════════

class SurfaceWiringRatchetTest(unittest.TestCase):

    def test_connect_dialog_maps_parse_errors(self):
        src = _src('desktop/connect_ui.py')
        self.assertIn('theme.connect_error_text(ve, lang)', src,
                      'the connect dialog renders str(ve) raw again — the '
                      'English parse sentences are back in the zh dialog')

    def test_dialogs_map_reason_tokens(self):
        src = _src('desktop/connect_ui.py')
        self.assertIn('theme.reason_text(reason, lang)', src,
                      'verifyFailed fills {reason} with a raw token')

    def test_post_install_has_no_install_prose_left(self):
        """NEUTER target: restore any of these literals — the progress view
        speaks English again."""
        src = _src('desktop/post_install.py')
        for legacy in ('installed successfully', 'Installation failed',
                       'timed out', 'not found in bundle',
                       'bootstrap failed', 'size_hint',
                       'Downloading Chromium...', 'Setting up PostgreSQL...'):
            self.assertNotIn(legacy, src,
                             'post_install.py carries the English literal '
                             '%r again — worker messages are TOKENS mapped '
                             'by theme.component_msg' % legacy)

    def test_post_install_renders_through_component_msg(self):
        src = _src('desktop/post_install.py')
        self.assertIn('theme.component_msg(msg, lang)', src,
                      'the failure row renders the worker message raw')
        self.assertIn('theme.component_msg(payload, lang)', src,
                      'the progress row renders the worker text raw')

    def test_component_size_is_keyed(self):
        src = _src('desktop/post_install.py')
        self.assertIn("desktop.comp.%s.size", src,
                      'the component card size hint is not keyed')
        theme = _theme()
        for comp in ('chromium', 'postgresql'):
            key = 'desktop.comp.%s.size' % comp
            pair = theme.STRINGS.get(key)
            self.assertIsNotNone(pair, '%s missing' % key)
            self.assertIn('en', pair)
            self.assertIn('zh', pair)

    def test_tray_link_error_branch_is_keyed(self):
        src = _src('desktop/agent_launcher.py')
        self.assertIn('desktop.tray.stError', src,
                      'the link line error branch renders a raw exception '
                      'with no localized prefix')
        theme = _theme()
        pair = theme.STRINGS.get('desktop.tray.stError')
        self.assertIsNotNone(pair)
        self.assertIn('{detail}', pair['en'])
        self.assertIn('{detail}', pair['zh'])

    def test_terminal_fallback_is_bilingual(self):
        src = _src('desktop/post_install.py')
        self.assertIn('desktop.terminal.installPrompt', src)
        self.assertIn('desktop.terminal.alreadyInstalled', src)

    def test_new_key_families_zh_is_real_translation(self):
        theme = _theme()
        for key, pair in theme.STRINGS.items():
            if key.startswith(('desktop.reason.', 'desktop.connect.err',
                               'desktop.compmsg.', 'desktop.terminal.')):
                self.assertNotEqual(
                    pair['en'], pair['zh'],
                    '%s ships English as its Chinese — fake i18n' % key)

    def test_new_key_families_placeholder_parity(self):
        theme = _theme()
        ph = re.compile(r'\{[a-z]+\}')
        for key, pair in theme.STRINGS.items():
            if key.startswith(('desktop.reason.', 'desktop.connect.err',
                               'desktop.compmsg.', 'desktop.terminal.')):
                self.assertEqual(sorted(ph.findall(pair['en'])),
                                 sorted(ph.findall(pair['zh'])),
                                 '%s placeholder mismatch — the .replace() '
                                 'call site fills only one language' % key)


if __name__ == '__main__':
    unittest.main()
