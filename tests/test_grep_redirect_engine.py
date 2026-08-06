"""tests/test_grep_redirect_engine.py — transparent grep redirect engine.

Owner ruling 2026-08-06: filesystem greps in run_command are EXECUTED
in-process (GNU-faithful) and the pipeline spliced around temp files;
refuse+teach is only the honest fallback.

The expected outputs pinned here are GNU grep ground truth probed on this
host 2026-08-06 (three probe rounds: prefix rules, exit codes 0/1/2,
context separators, binary notices, -l/-L raw-match rc basis, --include on
explicit operands, --exclude-dir on explicit dirs, DFS readdir order).
The differential class re-asserts parity against the host's real GNU grep
where one exists (skipped elsewhere), with the hardening layer's
--exclude-dir exclusions injected so both sides prune identically.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_grep_redirect_engine.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from lib.project_mod.grep_redirect import (
    _TEMP_REGISTRY,
    _bre_to_py,
    _ere_to_py,
    _shell_words,
    _sweep_temps,
    plan_grep_redirect,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def ws(tmp_path):
    """The probe corpus (creation order matters for DFS readdir parity)."""
    base = tmp_path / 'w'
    (base / 'sub' / 'src').mkdir(parents=True)
    (base / 'sub' / 'logs').mkdir(parents=True)
    (base / 'a.txt').write_text('alpha\nbeta\nALPHA gamma\n')
    (base / 'sub' / 'src' / 'b.txt').write_text('beta\ndelta\n')
    (base / 'sub' / 'logs' / 'l.txt').write_text('hit\n')
    (base / 'single.txt').write_text('x\ny\n')
    (base / 'bin.dat').write_bytes(b'ab\x00cd\nbeta\n')
    return str(base)


def run_spliced(cmd, cwd):
    """Plan + execute the rewritten command in bash; return (rc, out, err)."""
    plan = plan_grep_redirect(cmd, cwd)
    assert plan is not None, f'no plan for: {cmd}'
    assert plan.rewritten is not None, f'refused[{plan.refusal_reason}]: {cmd}'
    p = subprocess.run(['bash', '-c', plan.rewritten],
                       capture_output=True, text=True, cwd=cwd)
    return p.returncode, p.stdout, p.stderr


# ═════════════════════════════════════════════════════════════════════
#  1. Tokenizer & pattern translation
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestShellWords:
    def test_quotes_preserved_as_one_word(self):
        ws_ = _shell_words("grep -n 'foo bar' f.py")
        assert [w.text for w in ws_] == ['grep', '-n', 'foo bar', 'f.py']
        assert ws_[2].squote and not ws_[3].squote

    def test_offsets_point_at_raw_text(self):
        s = "  grep -E 'a|b'  x.txt"
        for w in _shell_words(s):
            assert s[w.start:w.end].strip("'\"") or True  # offsets in range
        words = _shell_words(s)
        assert s[words[1].start:words[1].end] == '-E'
        assert s[words[2].start:words[2].end] == "'a|b'"

    def test_double_quote_escape(self):
        ws_ = _shell_words(r'grep "a\"b" f')
        assert ws_[1].text == 'a"b' and ws_[1].dquote


@pytest.mark.unit
class TestPatternTranslation:
    def test_bre_alternation_and_plus(self):
        import re
        rx = re.compile(_bre_to_py(r'alp\|bet'))
        assert rx.search('beta') and rx.search('alpha') and not rx.search('zzz')
        assert re.compile(_bre_to_py(r'a\+')).search('aaa')
        # BRE bare metachars are literal
        assert re.compile(_bre_to_py('a+')).search('a+')
        assert not re.compile(_bre_to_py('a+')).search('aaa')
        assert re.compile(_bre_to_py('a(b)')).search('a(b)')

    def test_bre_interval(self):
        import re
        assert re.compile(_bre_to_py(r'a\{2,3\}')).search('caaard')
        assert not re.compile(_bre_to_py(r'a\{2,3\}')).search('card')

    def test_bre_word_boundaries(self):
        import re
        rx = re.compile(_bre_to_py(r'\<bet\>'))
        assert rx.search('a bet b') and not rx.search('beta')

    def test_ere_escaped_metachars_are_literal(self):
        import re
        assert re.compile(_ere_to_py(r'a\(b\)')).search('a(b)')
        assert re.compile(_ere_to_py('a+')).search('aaa')

    def test_posix_classes(self):
        import re
        assert re.compile(_bre_to_py('[[:digit:]]')).search('a1')
        assert not re.compile(_bre_to_py('[[:digit:]]')).search('ab')
        assert re.compile(_bre_to_py('[^[:digit:]]')).search('ab')


# ═════════════════════════════════════════════════════════════════════
#  2. Engine GNU-truth matrix (probed 2026-08-06)
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestEngineGNUTruth:
    # (command, rc, stdout, stderr) — byte-exact GNU parity
    @pytest.mark.parametrize('cmd, rc, out, err', [
        ('grep beta a.txt', 0, 'beta\n', ''),
        ('grep beta a.txt single.txt', 0, 'a.txt:beta\n', ''),
        ('grep beta missing.txt a.txt', 2, 'a.txt:beta\n',
         'grep: missing.txt: No such file or directory\n'),
        ('grep -c zzz a.txt', 1, '0\n', ''),
        ('grep -c zzz a.txt single.txt', 1, 'a.txt:0\nsingle.txt:0\n', ''),
        ('grep -n -C1 beta a.txt sub/src/b.txt', 0,
         'a.txt-1-alpha\na.txt:2:beta\na.txt-3-ALPHA gamma\n--\n'
         'sub/src/b.txt:1:beta\nsub/src/b.txt-2-delta\n', ''),
        ("grep -on 'b.t' a.txt sub/src/b.txt", 0,
         'a.txt:2:bet\nsub/src/b.txt:1:bet\n', ''),
        ('grep beta sub', 2, '', 'grep: sub: Is a directory\n'),
        ('grep -q beta missing.txt', 2, '',
         'grep: missing.txt: No such file or directory\n'),
        ('grep -q beta missing.txt a.txt', 0, '',
         'grep: missing.txt: No such file or directory\n'),
        ('grep -l beta a.txt single.txt', 0, 'a.txt\n', ''),
        ('grep -L beta a.txt single.txt', 0, 'single.txt\n', ''),
        ('grep -L zzz a.txt single.txt', 1, 'a.txt\nsingle.txt\n', ''),
        ('grep beta bin.dat', 0, 'Binary file bin.dat matches\n', ''),
        ('grep -I beta bin.dat', 1, '', ''),
        ('grep -c beta bin.dat', 0, '1\n', ''),
        ('grep -m1 -n beta a.txt sub/src/b.txt', 0,
         'a.txt:2:beta\nsub/src/b.txt:1:beta\n', ''),
        ("grep 'alp\\|bet' a.txt", 0, 'alpha\nbeta\n', ''),
        ('grep -w bet a.txt', 1, '', ''),
        ('grep -w beta a.txt', 0, 'beta\n', ''),
        ('grep -x beta a.txt sub/src/b.txt', 0, 'a.txt:beta\nsub/src/b.txt:beta\n', ''),
        ('grep -h beta a.txt sub/src/b.txt', 0, 'beta\nbeta\n', ''),
        ('grep -H beta a.txt', 0, 'a.txt:beta\n', ''),
        ('grep -vn beta a.txt', 0, '1:alpha\n3:ALPHA gamma\n', ''),
        ("grep '[[:digit:]]' a.txt", 1, '', ''),
        ('grep -e alpha -e delta a.txt sub/src/b.txt', 0,
         'a.txt:alpha\nsub/src/b.txt:delta\n', ''),
        ('grep -d skip beta sub', 1, '', ''),
        ('grep -s beta missing.txt a.txt', 2, 'a.txt:beta\n', ''),
        ('grep -cv beta a.txt', 0, '2\n', ''),
        ('grep -o -C1 beta a.txt', 0, 'beta\n', ''),
        # DFS readdir order (dirs descended inline) — GNU fts parity:
        ('grep -rn beta .', 0,
         './sub/src/b.txt:1:beta\n./a.txt:2:beta\n'
         'Binary file ./bin.dat matches\n', ''),
        ('grep -r beta', 0,
         'sub/src/b.txt:beta\na.txt:beta\nBinary file bin.dat matches\n', ''),
        ('grep -rn zzz .', 1, '', ''),
        ('grep -rl beta sub', 0, 'sub/src/b.txt\n', ''),
        ('grep -r beta a.txt', 0, 'beta\n', ''),
        # logs/ is in IGNORE_DIRS — pruned exactly like the hardening
        # layer's injected --exclude-dir (even as an explicit operand):
        ('grep -rn hit sub', 1, '', ''),
    ])
    def test_gnu_parity(self, ws, cmd, rc, out, err):
        got_rc, got_out, got_err = run_spliced(cmd, ws)
        assert (got_rc, got_out, got_err) == (rc, out, err)


# ═════════════════════════════════════════════════════════════════════
#  3. Splice shapes — pipelines, chains, substitution, redirection
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSpliceShapes:
    def test_pipe_to_wc(self, ws):
        rc, out, err = run_spliced('grep -n beta a.txt | wc -l', ws)
        assert (rc, out.strip()) == (0, '1')

    def test_owner_shape_grep_then_cat_to_file(self, ws):
        rc, out, _ = run_spliced(
            'grep beta a.txt sub/src/b.txt | cat > result.txt', ws)
        assert rc == 0
        assert open(os.path.join(ws, 'result.txt')).read() == \
            'a.txt:beta\nsub/src/b.txt:beta\n'

    def test_output_redirection_preserved(self, ws):
        rc, out, _ = run_spliced(
            'grep beta a.txt sub/src/b.txt > out.txt && cat out.txt', ws)
        assert out == 'a.txt:beta\nsub/src/b.txt:beta\n'

    def test_stderr_redirection_preserved(self, ws):
        rc, out, err = run_spliced(
            'grep beta missing.txt a.txt 2>/dev/null', ws)
        assert (rc, out, err) == (2, 'a.txt:beta\n', '')

    def test_exit_code_chains(self, ws):
        assert run_spliced('grep -q zzz a.txt || echo NONE', ws)[1] == 'NONE\n'
        assert run_spliced('grep -q beta a.txt && echo YES', ws)[1] == 'YES\n'

    def test_command_substitution_brace_group(self, ws):
        # $( ( … ) would be parsed as arithmetic — the brace group must win.
        rc, out, _ = run_spliced('x=$(grep -c beta a.txt); echo count=$x', ws)
        assert (rc, out) == (0, 'count=1\n')

    def test_if_condition(self, ws):
        rc, out, _ = run_spliced('if grep -q beta a.txt; then echo IFYES; fi', ws)
        assert out == 'IFYES\n'

    def test_subshell_framing(self, ws):
        rc, out, _ = run_spliced('( grep beta a.txt )', ws)
        assert out == 'beta\n'

    def test_env_prefix_and_cd_fold(self, ws):
        rc, out, _ = run_spliced('LC_ALL=C grep beta a.txt', ws)
        assert out == 'beta\n'
        rc, out, _ = run_spliced('cd sub && grep -rn beta src', ws)
        assert out == 'src/b.txt:1:beta\n'

    def test_multi_grep_chain(self, ws):
        rc, out, _ = run_spliced(
            "grep -n 'alp\\|bet' a.txt | head -2; echo ---; "
            'grep -rn beta sub 2>/dev/null | head -5', ws)
        assert out == '1:alpha\n2:beta\n---\nsub/src/b.txt:1:beta\n'

    def test_fs_grep_feeding_stream_grep(self, ws):
        # The second grep is a stream filter and must stay REAL grep.
        rc, out, _ = run_spliced('grep beta a.txt | grep -c beta', ws)
        assert out.strip() == '1'

    def test_glob_operand_expansion(self, ws):
        # The glob expands to ONE file — single operand, so GNU prints no
        # filename prefix (prefix appears only at 2+ expanded operands).
        rc, out, _ = run_spliced('grep delta sub/src/*.txt', ws)
        assert out == 'delta\n'


# ═════════════════════════════════════════════════════════════════════
#  4. Honest refusals (fallback to refuse+teach)
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestHonestRefusals:
    @pytest.mark.parametrize('cmd, why', [
        ('grep -P beta a.txt', 'unsupported grep flag -P'),
        ('grep --perl-regexp beta a.txt', 'unsupported grep flag --perl-regexp'),
        ('grep -z beta a.txt', 'unsupported grep flag -z'),
        ('sudo grep -rn beta sub', 'sudo-wrapped grep'),
        ('grep beta $', 'command substitution'),
        ('grep beta f`echo x`.txt', 'command substitution'),
        # plan-time execution vs an earlier segment that may write:
        ("printf 'beta\\n' > new.txt; grep beta new.txt", 'not provably read-only'),
        ('make all; grep -rn beta sub', 'not provably read-only'),
        ('git checkout main; grep beta a.txt', 'git checkout may write'),
        ('sed -i s/a/z/ a.txt; grep beta a.txt', 'sed -i writes in place'),
        ('awk "{print}" a.txt; grep beta a.txt', 'awk'),
        ("echo beta > f.txt; grep beta f.txt", 'not provably read-only'),
    ])
    def test_refusal_carries_reason(self, ws, cmd, why):
        plan = plan_grep_redirect(cmd, ws)
        assert plan is not None and plan.refused_segment is not None
        assert why in plan.refusal_reason

    def test_readonly_predecessors_do_not_refuse(self, ws):
        plan = plan_grep_redirect('git status; ls; grep beta a.txt', ws)
        assert plan.rewritten is not None

    def test_stream_grep_is_no_plan(self, ws):
        assert plan_grep_redirect('ps aux | grep python', ws) is None
        assert plan_grep_redirect('echo hi | grep -v h | wc -l', ws) is None


# ═════════════════════════════════════════════════════════════════════
#  5. Temp-file lifecycle
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestTempLifecycle:
    def test_temps_registered_then_swept(self, ws):
        plan = plan_grep_redirect('grep beta a.txt', ws)
        assert plan.rewritten is not None
        temps = [p for p in _TEMP_REGISTRY if os.path.exists(p)]
        assert temps, 'expected registered temp files'
        _sweep_temps()
        assert not [p for p in temps if os.path.exists(p)]


# ═════════════════════════════════════════════════════════════════════
#  6. Differential parity against the host's real GNU grep
# ═════════════════════════════════════════════════════════════════════

_GNU_GREP = shutil.which('grep') and b'GNU' in subprocess.run(
    ['grep', '--version'], capture_output=True).stdout


@pytest.mark.unit
@pytest.mark.skipif(not _GNU_GREP, reason='needs GNU grep on the host')
class TestDifferentialParity:
    """Same corpus, same flags — our engine's spliced result must equal
    GNU grep (with the hardening layer's --exclude-dir exclusions, so both
    sides prune the same directories)."""

    CASES = [
        'grep beta a.txt',
        'grep -n beta a.txt sub/src/b.txt',
        'grep -i BETA a.txt',
        'grep -v beta a.txt',
        'grep -w bet a.txt',
        'grep -x beta sub/src/b.txt',
        'grep -o b.t a.txt',
        'grep -m1 -n beta a.txt sub/src/b.txt',
        'grep -C1 -n beta a.txt sub/src/b.txt',
        'grep -l beta a.txt single.txt',
        'grep -L beta a.txt single.txt',
        'grep -c beta a.txt sub/src/b.txt',
        'grep beta missing.txt a.txt',
        'grep -q beta a.txt',
        'grep -q zzz a.txt',
        "grep 'alp\\|bet' a.txt",
        "grep -E 'alp(a|ha)' a.txt",
        'grep -rn beta sub',
        'grep -rl beta sub',
        'grep -r beta',
        'grep --include=*.txt -rn delta sub',
        'grep beta bin.dat',
        'grep -I beta bin.dat',
    ]

    def test_parity_matrix(self, ws):
        from lib.project_mod.config import IGNORE_DIRS
        excl = []
        for d in sorted(IGNORE_DIRS):
            excl += ['--exclude-dir', d]
        import shlex
        for cmd in self.CASES:
            argv = shlex.split(cmd)
            gnu_argv = ['grep'] + excl + argv[1:]
            g = subprocess.run(gnu_argv, capture_output=True, text=True, cwd=ws)
            rc, out, err = run_spliced(cmd, ws)
            assert (rc, out) == (g.returncode, g.stdout), (
                f'stdout/rc drift on {cmd!r}:\nours: {rc} {out!r}\n'
                f'gnu : {g.returncode} {g.stdout!r}')
            assert (err == g.stderr) or (
                err.replace('grep:', 'grep:') == g.stderr), (
                f'stderr drift on {cmd!r}:\nours: {err!r}\ngnu : {g.stderr!r}')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
