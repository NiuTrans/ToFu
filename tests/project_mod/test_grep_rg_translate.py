"""tests/project_mod/test_grep_rg_translate.py — GNU grep → ripgrep argv translation.

The translator is the run_command grep fast path's correctness boundary:
every accepted construct must be observably equivalent to GNU (stdout,
exit code; traversal order and rg's ignore-aware traversal are the two
documented deviations). Anything else returns None and the caller keeps
native GNU grep with only --exclude-dir injected.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/project_mod/test_grep_rg_translate.py -v
"""

from __future__ import annotations

import os

import pytest

from lib.project_mod import grep_rg_translate as tr

pytestmark = pytest.mark.unit


# ═════════════════════════════════════════════════════════════════════
#  1. BRE pattern translation
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('pattern,expected', [
    ('cream', 'cream'),
    ('x = 1', 'x = 1'),
    ('^def', '^def'),                    # leading anchor stays an anchor
    ('a^b', 'a\\^b'),                    # mid-pattern caret is BRE-literal
    ('def$', 'def$'),
    ('a$b', 'a\\$b'),                    # mid-pattern dollar is BRE-literal
    ('a+b', 'a\\+b'),                    # unescaped ERE metas are BRE literals
    ('foo(bar)', 'foo\\(bar\\)'),
    ('a|b', 'a\\|b'),
    ('x{2}', 'x\\{2\\}'),
    ('a*', 'a*'),
    ('*star', '\\*star'),                # leading star is BRE-literal
    ('\\(def\\)', '(def)'),              # BRE group form → rg group
    ('word\\|other', 'word|other'),      # GNU alternation extension
    ('x\\{2\\}', 'x{2}'),                # BRE interval form
    ('a\\+', 'a+'),                      # GNU quantifier extensions
    ('b\\?', 'b?'),
    ('[a-z]x', '[a-z]x'),
    ('[^abc]*', '[^abc]*'),
    ('a\\.b', 'a\\.b'),
    ('\\w+', '\\w\\+'),                  # \w passes; + is BRE-literal
    ('\\bx\\b', '\\bx\\b'),
    ('foo\\`', 'foo\\A'),                # GNU buffer anchors
    ("bar\\'", 'bar\\z'),
    ('a$b\\(c\\|^d\\)', 'a\\$b(c|^d)'),  # anchors after \( and \| forms
])
def test_bre_translation(pattern, expected):
    assert tr.translate_bre_pattern(pattern) == expected


@pytest.mark.parametrize('pattern', [
    'a\\1',          # backreference — no Rust-regex equivalent
    '\\<word',       # word anchors
    'word\\>',
    'a\\nb',         # GNU letter escapes are bare letters, rg controls
    'a\\tb',
    'foo\\',         # lone trailing backslash
    '[a-z',          # unterminated bracket
    '[[:alpha:]]',   # POSIX class is fine — must NOT fall back
])
def test_bre_fallback_cases(pattern):
    if pattern == '[[:alpha:]]':
        assert tr.translate_bre_pattern(pattern) is not None
    else:
        assert tr.translate_bre_pattern(pattern) is None


@pytest.mark.parametrize('pattern,expected', [
    ('a+', 'a+'),                        # ERE metas pass through
    ('(a|b)+', '(a|b)+'),
    ('x{2,3}', 'x{2,3}'),
    ('a\\+b', 'a\\+b'),                  # escaped = literal in both
    ('colou?r', 'colou?r'),
])
def test_ere_translation(pattern, expected):
    assert tr.translate_ere_pattern(pattern) == expected


@pytest.mark.parametrize('pattern', ['x\\1y', 'a\\<b', 'a\\nb'])
def test_ere_fallback_cases(pattern):
    assert tr.translate_ere_pattern(pattern) is None


# ═════════════════════════════════════════════════════════════════════
#  2. argv translation with a stub operand probe
# ═════════════════════════════════════════════════════════════════════

def _probe(kinds):
    def probe(path):
        base = os.path.basename(path)
        return kinds.get(base, 'text')
    return probe


def _argv(program, args, kinds=None, excludes=()):
    return tr.translate_grep_to_rg(
        program, args, '/cwd', excludes, probe=_probe(kinds or {}))


class TestArgvTranslation:
    def test_recursive_literal(self):
        out = _argv('grep', ['-rn', 'beta', 'sub'],
                    {'sub': 'dir'}, ['node_modules'])
        assert out is not None and out[0] == tr.rg_executable()
        assert '--no-heading' in out and '--color=never' in out
        assert '-n' in out
        assert '--regexp' in out and 'beta' in out
        assert '--glob' in out and '!node_modules/' in out
        assert out[-2:] == ('--', ['sub']) or out[-1] == 'sub'

    def test_single_text_file(self):
        out = _argv('grep', ['-n', 'x = 1', 'a.py'])
        assert out is not None and 'a.py' in out

    def test_multi_file_operands_pin_serial_order(self):
        out = _argv('grep', ['-n', 'beta', 'a.txt', 'b.txt'])
        assert out is not None and '--threads=1' in out
        out = _argv('grep', ['-rn', 'beta', 'a.txt', 'sub'],
                    {'sub': 'dir'})
        assert out is not None and '--threads=1' not in out

    def test_regex_flavors(self):
        assert '--fixed-strings' in _argv('fgrep', ['a+b', 'f.txt'])
        assert '--fixed-strings' not in _argv('egrep', ['a+b', 'f.txt'])
        # BRE literal + would need escaping → translated pattern is a\+b
        out = _argv('grep', ['a+b', 'f.txt'])
        idx = out.index('--regexp')
        assert out[idx + 1] == 'a\\+b'

    def test_backref_pattern_falls_back(self):
        assert _argv('grep', ['-rn', '\\(a\\)\\1', 'sub'], {'sub': 'dir'}) \
            is None

    def test_count_falls_back(self):
        # rg ≥14 dropped --with-zero-count: zero-match counts would diverge.
        assert _argv('grep', ['-c', 'x', 'a.txt']) is None
        assert _argv('grep', ['-rc', 'x', 'sub'], {'sub': 'dir'}) is None

    def test_perl_falls_back(self):
        assert _argv('grep', ['-P', 'x+', 'a.txt']) is None

    def test_missing_operand_falls_back(self):
        assert _argv('grep', ['x', 'gone.txt'], {'gone.txt': 'missing'}) \
            is None

    def test_dir_operand_without_recursive_falls_back(self):
        assert _argv('grep', ['-n', 'x', 'sub'], {'sub': 'dir'}) is None

    def test_binary_operand_needs_text_policy(self):
        kinds = {'bin.dat': 'binary'}
        # Only -a/--text is spliceable (both print raw lines); silent -I
        # and GNU's own notice text have no rg equivalent for explicit
        # operands → native GNU.
        assert _argv('grep', ['x', 'bin.dat'], kinds) is None
        assert _argv('grep', ['-I', 'x', 'bin.dat'], kinds) is None
        assert _argv('grep', ['-a', 'x', 'bin.dat'], kinds) is not None
        assert _argv('grep', ['--binary-files=text', 'x', 'bin.dat'],
                     kinds) is not None

    def test_include_glob_slash_falls_back(self):
        assert _argv('grep', ['-r', '--include=src/*.py', 'x', 'd'],
                     {'d': 'dir'}) is None
        assert _argv('grep', ['-r', '--include=*.py', 'x', 'd'],
                     {'d': 'dir'}) is not None

    def test_follow_maps(self):
        out = _argv('grep', ['-R', 'x', 'd'], {'d': 'dir'})
        assert '--follow' in out

    def test_context_and_max_count(self):
        out = _argv('grep', ['-rn', '-A2', '-B3', '-m', '4', 'x', 'd'],
                    {'d': 'dir'})
        for flag, value in (('--after-context', '2'),
                            ('--before-context', '3'),
                            ('--max-count', '4')):
            idx = out.index(flag)
            assert out[idx + 1] == value

    def test_pattern_file_fixed_only(self):
        assert '--file' in (_argv('fgrep', ['-rf', 'pats.txt', 'd'],
                                  {'d': 'dir'}) or ())
        assert _argv('grep', ['-rf', 'pats.txt', 'd'], {'d': 'dir'}) is None

    def test_unsupported_flags_fall_back(self):
        for args in (['-T', 'x', 'f'], ['-z', 'x', 'f'],
                     ['--exclude-from=ex.txt', 'x', 'f'],
                     ['--color=always', 'x', 'f'],
                     ['-d', 'skip', 'x', 'f']):
            assert _argv('grep', args) is None, args

    def test_no_operands_requires_recursive(self):
        assert _argv('grep', ['-n', 'x']) is None
        assert _argv('grep', ['-rn', 'x']) is not None

    def test_force_gnu_env(self, monkeypatch):
        monkeypatch.setenv('TOFU_GREP_FORCE_GNU', '1')
        assert _argv('grep', ['-rn', 'x', 'd'], {'d': 'dir'}) is None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
