"""Executable specification of ``lib/project_mod/glob_match.py``: the one glob
dialect behind ``search_files`` include filters and ``find_files`` patterns.

* An accepted glob selects exactly what ripgrep (``--iglob``) and fd
  (``--glob --ignore-case``) select, checked against the real binaries when
  they are installed.
* Malformed globs, and brace groups whose meaning differs between ripgrep and
  shells, raise ``GlobPatternError`` naming the repair.
* GNU grep ``--include``, which has no alternation, receives an exact union
  of brace-free globs, or none when no such union exists.
"""

import shutil
import subprocess

import pytest

from lib.project_mod.glob_match import GlobPatternError, compile_glob

pytestmark = pytest.mark.unit

_NAMES = (
    'a.ts', 'b.tsx', 'c.js', 'd.css', 'E.TS', '{x}.txt', 'x.txt', 'p,q', 'ab',
    'aXb', 'a]', ']x', '^z', 'foo', 'foo.js', 'abcx', 'sub/e.ts',
    'sub/deep/f.js', 'sub/deep/g.tsx', 'q/c', 'axx/yc',
)
_BASENAME_GLOBS = (
    '*.{ts,js}', '*.{t{s,sx},js}', '{a,b}.{ts,tsx}', '{a,c}.*', '{{a,b},c}.*',
    '[{]x[}].txt', r'\{x\}.txt', r'{p\,q,ab}', 'p,q', '[!a]*', '[^a]*',
    '[]]x', '[a-c].*', 'a?b', '*', '**', 'a**b', '*.TS', '*{s,x}',
    # The shortest alternative must stay available to the following star.
    '*{abc,a}*bcx', '*{*a,b}*x',
)
_PATH_GLOBS = (
    '**/*.ts', 'sub/**', 'sub/**/*.js', '{sub,other}/**/*.{ts,js}',
    '**/deep/*.tsx',
    # A negated class also matches '/', joining a directory and a file name.
    '*[!x]*c',
    # An alternative holding '/' starts after the earliest match of another.
    'a*{xx,/y}*c',
)


def _selected(glob):
    compiled = compile_glob(glob)
    return {name for name in _NAMES if compiled.matches(name)}


def _listing(tmp_path, argv):
    for name in _NAMES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('line\n')
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode in (0, 1), result.stderr
    return {line.removeprefix('./') for line in result.stdout.splitlines()}


@pytest.mark.skipif(shutil.which('rg') is None, reason='ripgrep is not installed')
@pytest.mark.parametrize('glob', _BASENAME_GLOBS + _PATH_GLOBS)
def test_accepted_glob_selects_what_ripgrep_selects(tmp_path, glob):
    argv = ['rg', '--no-config', '--files', '--hidden', '--no-ignore',
            '--iglob', glob]
    assert _selected(glob) == _listing(tmp_path, argv)


@pytest.mark.skipif(not (shutil.which('fd') or shutil.which('fdfind')),
                    reason='fd is not installed')
@pytest.mark.parametrize('glob', _BASENAME_GLOBS)
def test_accepted_basename_glob_selects_what_fd_selects(tmp_path, glob):
    fd = shutil.which('fd') or shutil.which('fdfind')
    argv = [fd, '--hidden', '--no-ignore', '--type', 'f', '--ignore-case',
            '--glob', glob]
    assert _selected(glob) == _listing(tmp_path, argv)


def test_slash_free_glob_matches_names_and_slashed_glob_the_relative_path():
    assert compile_glob('*.{ts,js}').matches('web/ui/a.ts')
    braced_path = compile_glob('{web/*,x}.ts')
    assert braced_path.matches('web/a.ts') and braced_path.matches('x.ts')
    assert not braced_path.matches('lib/web/a.ts')
    assert compile_glob('{src,lib}/**/*.py').matches('src/a.py')


def test_case_sensitivity_is_explicit():
    assert compile_glob('*.{TS,js}').matches('a.ts')
    assert not compile_glob('*.{TS,js}', case_sensitive=True).matches('a.ts')
    assert compile_glob('*.{TS,js}', case_sensitive=True).matches('a.TS')


@pytest.mark.parametrize('glob,repair', [
    ('*.{ts,js', "'[{]'"),
    ('*.ts}', "'[}]'"),
    ('*.{ts}', 'two or more'),
    ('*.{,ts}', 'empty alternative'),
    ('*.{ts,}', 'empty alternative'),
    ('{}', 'empty alternative'),
    ('{,}', 'empty alternative'),
    ('[ab', "'[[]'"),
    ('a\\', "'[\\]'"),
    ('[z-a]', "'z-a'"),
    ('', "'*'"),
])
def test_rejected_glob_names_its_repair(glob, repair):
    with pytest.raises(GlobPatternError) as caught:
        compile_glob(glob)
    assert repair in str(caught.value)


@pytest.mark.skipif(shutil.which('rg') is None, reason='ripgrep is not installed')
@pytest.mark.parametrize('glob', ['*.{ts,js', '*.ts}', '[ab', 'a\\', '[z-a]'])
def test_malformed_glob_is_also_refused_by_ripgrep(tmp_path, glob):
    result = subprocess.run(
        ['rg', '--no-config', '--files', '--iglob', glob], cwd=tmp_path,
        capture_output=True, text=True)
    assert result.returncode == 2 and 'error parsing glob' in result.stderr


@pytest.mark.parametrize('glob,expected', [
    ('*.py', ['*.py']),
    ('*.{t{s,sx},js}', ['*.ts', '*.tsx', '*.js']),
    ('{a,b}.{x,y}', ['a.x', 'a.y', 'b.x', 'b.y']),
    (r'\{x\}.{a,b}', ['{x}.a', '{x}.b']),
    ('[{]{a,b}', [r'[\{]a', r'[\{]b']),
    (r'{p\,q,ab}', ['p,q', 'ab']),
    ('[a-c-]', [r'[\-abc]']),
    ('a?b', None),        # '?' is one byte in ripgrep, one character in grep
    ('[!a]*', None),      # a negated class also matches '/' in ripgrep
    ('src/*.py', None),   # --include sees base names only
    ('{web/*,x}.ts', None),
])
def test_grep_includes_spell_each_alternative(glob, expected):
    compiled = compile_glob(glob, case_sensitive=True)
    assert compiled.grep_includes(argv_budget=1 << 20) == expected


def test_grep_includes_fold_case_into_classes():
    assert compile_glob('*.{ts,JS}').grep_includes(argv_budget=1 << 20) == [
        '*.[tT][sS]', '*.[jJ][sS]']
    assert compile_glob('[a-b]1').grep_includes(argv_budget=1 << 20) == [
        '[ABab]1']


def test_grep_includes_fit_the_argv_budget_without_listing_first():
    compiled = compile_glob('*.{ts,js}', case_sensitive=True)
    exact = len('--include=*.ts\0--include=*.js\0')
    assert compiled.grep_includes(argv_budget=exact) == ['*.ts', '*.js']
    assert compiled.grep_includes(argv_budget=exact - 1) is None
    # 2**64 spellings: refused from their count, never materialized.
    assert compile_glob('{a,b}' * 64).grep_includes(argv_budget=1 << 20) is None


@pytest.mark.parametrize('glob', _BASENAME_GLOBS)
def test_grep_includes_select_what_the_glob_selects(tmp_path, glob):
    gnu = shutil.which('grep')
    if gnu is None or 'GNU' not in subprocess.run(
            [gnu, '--version'], capture_output=True, text=True).stdout:
        pytest.skip('GNU grep is not installed')
    includes = compile_glob(glob).grep_includes(argv_budget=1 << 20)
    if glob in ('[!a]*', '[^a]*', 'a?b'):
        assert includes is None
        return
    argv = [gnu, '-rl', *(f'--include={spelled}' for spelled in includes),
            '-e', '', '.']
    assert _selected(glob) == _listing(tmp_path, argv)


def test_matching_never_backtracks():
    # Each of these takes a backtracking matcher longer than any test timeout.
    assert not compile_glob('*a' * 24 + 'b').matches('a' * 4096)
    assert not compile_glob('*{*a,b}' * 8 + 'c').matches('a' * 4096)
    assert not compile_glob('*{a,aa}' * 8 + 'b').matches('a' * 4096)
    assert not compile_glob('{a,aa}' * 40 + 'b').matches('a' * 80)
    assert not compile_glob('**/a/' * 16 + 'b').matches('a/' * 2000 + 'c')
    assert compile_glob('**/a/' * 16 + 'b').matches('a/' * 2000 + 'b')
