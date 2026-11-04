"""tests/tasks_pkg/test_run_command_rewrite_fidelity.py — span rewriters keep
shell data and runtime expansions intact.

run_command bounds direct ``find`` segments (``_bound_scan_segments``) and
splices filesystem ``grep`` segments (``plan_grep_redirect``) in place.
Incidents (2026-10-23):

  * both rewriters split the WHOLE command on newlines and parentheses, so the
    ``python3 - <<'EOF'`` body line ``print(find(1))`` became
    ``print(<timeout> find(1))`` (SyntaxError) and ``cat > f <<'EOF'`` wrote
    the timeout prefix or an ``rg`` argv into the file;
  * the rg translation re-quotes plan-time word text, so
    ``for t in a b; do grep -n "$t" log; done`` searched the literal ``$t``
    and silently returned nothing.

Pinned here:
  1. complete quoted cat/python/node heredoc bodies stay byte-identical;
     shell-executed bodies and every segment outside a body keep the rewrite;
  2. the recursive-scan resource guard keeps its verdicts for heredoc shapes;
  3. grep argv words that expand at runtime keep the GNU program-word splice
     (original shell text); literal words keep the rg translation;
  4. the same shapes end to end through ``tool_run_command``.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/tasks_pkg/test_run_command_rewrite_fidelity.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from lib.project_mod.command_analysis import _bound_scan_segments
from lib.project_mod.grep_redirect import plan_grep_redirect
from lib.project_mod.grep_rg_translate import rg_executable
from lib.project_mod.run_command import tool_run_command

pytestmark = pytest.mark.unit

_TIMEOUT = shutil.which('timeout')
_PREFIX = '/usr/bin/timeout --signal=TERM --kill-after=2s 40s '
needs_timeout = pytest.mark.skipif(
    _TIMEOUT is None, reason='coreutils timeout required')
needs_rg = pytest.mark.skipif(
    rg_executable() is None, reason='ripgrep required')


@pytest.fixture()
def ws(tmp_path):
    """A workspace two levels deep so ``..`` is an ancestor scan target."""
    base = tmp_path / 'parent' / 'chatui'
    (base / 'logs').mkdir(parents=True)
    (base / 'logs' / 'app.log').write_text(
        'start\nalpha ready\nbeta ready\n', encoding='utf-8')
    (base / 'a.py').write_text('needle = 1\n', encoding='utf-8')
    (base / 'b.py').write_text('other = 2\n', encoding='utf-8')
    return str(base)


def _sh(command, cwd):
    return subprocess.run(['sh', '-c', command], cwd=cwd,
                          capture_output=True, text=True, timeout=30)


def _read(ws, name):
    with open(os.path.join(ws, name), 'rb') as handle:
        return handle.read().decode('utf-8')


# ═════════════════════════════════════════════════════════════════════
#  1. Direct-find budget and heredoc bodies
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('command', [
    "python3 - <<'EOF'\ndef find(x):\n    return x\nprint(find(1))\nEOF",
    "cat > out.sh <<'EOF'\nfind . -maxdepth 0\n(find . -maxdepth 0)\nEOF",
    "cat <<'EOF' > out.sh\nfind . -name x | sort\nEOF\n",
    'node <<"JS"\nconst find = (x) => x;\nfind(1);\nJS\n',
])
def test_find_budget_leaves_literal_heredoc_bodies_unchanged(command):
    assert _bound_scan_segments(command, '/usr/bin/timeout', 40) == (command, 0)


def test_find_budget_keeps_wrapping_segments_around_a_heredoc():
    command = ("find . -maxdepth 0; cat > out.sh <<'EOF'\n"
               "find . -maxdepth 0\nEOF\nfind . -maxdepth 0 | head -1")
    assert _bound_scan_segments(command, '/usr/bin/timeout', 40) == (
        f"{_PREFIX}find . -maxdepth 0; cat > out.sh <<'EOF'\n"
        f"find . -maxdepth 0\nEOF\n{_PREFIX}find . -maxdepth 0 | head -1", 2)


@pytest.mark.parametrize('header', ["bash <<'EOF'", "cat <<'EOF' | sh"])
def test_find_budget_still_bounds_shell_executed_heredoc_bodies(header):
    """A body a shell runs is shell code: the rewrite fails closed."""
    command = f'{header}\nfind . -maxdepth 0\nEOF'
    assert _bound_scan_segments(command, '/usr/bin/timeout', 40) == (
        f'{header}\n{_PREFIX}find . -maxdepth 0\nEOF', 1)


@needs_timeout
def test_bounded_heredoc_command_writes_the_exact_body(ws):
    body = 'find . -maxdepth 0\n(find . -maxdepth 0)\n'
    command = f"cat > out.sh <<'EOF'\n{body}EOF\nfind . -maxdepth 0"
    rewritten, count = _bound_scan_segments(command, _TIMEOUT, 40)
    assert count == 1
    result = _sh(rewritten, ws)
    assert (result.returncode, result.stdout) == (0, '.\n'), result.stderr
    assert _read(ws, 'out.sh') == body


@needs_timeout
def test_bounded_python_heredoc_still_runs(ws):
    command = "python3 - <<'EOF'\ndef find(x):\n    return x\nprint(find(1))\nEOF"
    rewritten, _count = _bound_scan_segments(command, _TIMEOUT, 40)
    result = _sh(rewritten, ws)
    assert (result.returncode, result.stdout) == (0, '1\n'), result.stderr


# ═════════════════════════════════════════════════════════════════════
#  2. Filesystem-grep splice and heredoc bodies
# ═════════════════════════════════════════════════════════════════════

def test_grep_redirect_ignores_literal_heredoc_bodies(ws):
    assert plan_grep_redirect(
        "cat > out.sh <<'EOF'\ngrep -rn needle .\nEOF", ws) is None


def test_grep_redirect_still_splices_shell_executed_heredoc_bodies(ws):
    plan = plan_grep_redirect("cat <<'EOF' | sh\ngrep -rn needle .\nEOF", ws)
    assert plan is not None and plan.n_redirected == 1
    result = _sh(plan.rewritten, ws)
    assert result.stdout == './a.py:1:needle = 1\n', result.stderr


def test_grep_redirect_splices_around_a_heredoc_and_keeps_its_body(ws):
    body = 'grep -rn needle .\n(grep -n needle a.py)\n'
    command = (f"grep -n needle a.py; cat > out.sh <<'EOF'\n{body}EOF\n"
               'grep -c needle out.sh a.py')
    plan = plan_grep_redirect(command, ws)
    assert plan is not None and plan.n_redirected == 2
    assert f"<<'EOF'\n{body}EOF\n" in plan.rewritten
    result = _sh(plan.rewritten, ws)
    assert result.stdout == '1:needle = 1\nout.sh:2\na.py:1\n', result.stderr
    assert _read(ws, 'out.sh') == body


# ═════════════════════════════════════════════════════════════════════
#  3. Runtime expansions in grep argv
# ═════════════════════════════════════════════════════════════════════

def test_rg_translation_keeps_double_quoted_parameter(ws):
    plan = plan_grep_redirect(
        't=alpha; echo x; grep -n "$t" logs/app.log | head -3', ws)
    assert plan is not None and plan.rewritten is not None
    assert '"$t"' in plan.rewritten and "'$t'" not in plan.rewritten
    result = _sh(plan.rewritten, ws)
    assert result.stdout == 'x\n2:alpha ready\n', result.stderr


@pytest.mark.parametrize('command, expected', [
    ('for t in alpha beta; do echo hi; grep -n "$t" logs/app.log | head; done',
     'hi\n2:alpha ready\nhi\n3:beta ready\n'),
    ('t=alpha; grep -n $t logs/app.log', '2:alpha ready\n'),
    ('grep -n ${TOFU_TEST_UNSET_PATTERN:-beta} logs/app.log', '3:beta ready\n'),
    ('grep -n "$(echo beta)" logs/app.log', '3:beta ready\n'),
    ('grep -n "`echo beta`" logs/app.log', '3:beta ready\n'),
    # An unquoted glob pattern expands against the runtime cwd (file alpha).
    ('grep -n alph? logs/app.log', '2:alpha ready\n'),
    ('grep -n needle *.py', 'a.py:1:needle = 1\n'),
    ('grep -rn needle --include=*.py .', './a.py:1:needle = 1\n'),
])
def test_runtime_expanded_grep_words_keep_original_shell_text(
        ws, command, expected):
    with open(os.path.join(ws, 'alpha'), 'w', encoding='utf-8') as handle:
        handle.write('glob target\n')
    plan = plan_grep_redirect(command, ws)
    assert plan is not None and plan.backend == 'gnu'
    result = _sh(plan.rewritten, ws)
    assert result.stdout == expected, result.stderr


@needs_rg
@pytest.mark.parametrize('command', [
    "grep -n 'alpha' logs/app.log",
    'grep -n "alpha ready" logs/app.log',
    "grep -n '$t' logs/app.log",
    'grep -n "\\$t" logs/app.log',
    "grep -rn needle --include='*.py' .",
    'grep -rn alpha logs',
])
def test_literal_grep_words_keep_rg_translation(ws, command):
    plan = plan_grep_redirect(command, ws)
    assert plan is not None and plan.backend == 'rg'


# ═════════════════════════════════════════════════════════════════════
#  4. End to end through tool_run_command
# ═════════════════════════════════════════════════════════════════════

def test_heredoc_scan_lines_stay_data_through_run_command(ws):
    """Body lines were always judged bounded by the resource guard; keeping
    them byte-identical must not turn them into new refusals."""
    body = ('find .. -maxdepth 1 -name chatui\n'
            'grep -rn needle .\n'
            '(find . -maxdepth 0)\n')
    result = tool_run_command(ws, f"cat > notes.sh <<'EOF'\n{body}EOF")
    assert 'blocked by resource guard' not in result
    assert 'Command intercepted' not in result
    assert '[exit code: 0]' in result
    assert _read(ws, 'notes.sh') == body


def test_rewrites_around_a_heredoc_keep_its_body_through_run_command(ws):
    """The find budget runs on the grep-spliced source; both keep the body."""
    body = 'find . -maxdepth 0\ngrep -rn needle .\n'
    result = tool_run_command(
        ws, f"grep -n needle a.py && cat > out.sh <<'EOF'\n{body}EOF\n"
            'find . -maxdepth 0 | head -1')
    assert '1:needle = 1\n.\n' in result
    assert _read(ws, 'out.sh') == body


def test_python_heredoc_runs_unchanged_through_run_command(ws):
    result = tool_run_command(
        ws, "python3 - <<'EOF'\ndef find(x):\n    return x\nprint(find(1))\nEOF")
    assert '[exit code: 0]' in result
    assert '\n1\n' in result


def test_shell_executed_heredoc_find_keeps_its_budget(ws):
    chunks = []
    result = tool_run_command(
        ws, "bash <<'EOF'\nfind .. -maxdepth 1 -name chatui\nEOF",
        on_chunk=lambda *args: chunks.append(args))
    assert 'blocked by resource guard' not in result
    assert '../chatui' in result
    assert any('find scan capped' in str(chunk) for chunk in chunks)


def test_shell_executed_heredoc_scan_is_still_refused(ws):
    result = tool_run_command(ws, "bash <<'EOF'\ngrep -rn needle ..\nEOF")
    assert 'blocked by resource guard' in result


def test_grep_loop_over_shell_variable_through_run_command(ws):
    result = tool_run_command(
        ws, 'for t in alpha beta; do echo hi; grep -n "$t" logs/app.log | head -1; done')
    assert '2:alpha ready' in result
    assert '3:beta ready' in result


# ═════════════════════════════════════════════════════════════════════
#  5. Git read hardening and the cp fast path share the same boundary
# ═════════════════════════════════════════════════════════════════════

_DATA_BODY = 'git log --oneline -1\ncp -r logs copy\n'


def test_git_hardening_leaves_literal_heredoc_bodies_unchanged():
    from lib.project_mod.command_analysis import harden_git_inspections
    command = f"cat > notes.sh <<'EOF'\n{_DATA_BODY}EOF"
    assert harden_git_inspections(command) == command


def test_git_hardening_still_applies_around_a_heredoc():
    from lib.project_mod.command_analysis import harden_git_inspections
    tail = f"cat > notes.sh <<'EOF'\n{_DATA_BODY}EOF"
    command = f'git status --short && {tail}'
    hardened = harden_git_inspections(command)
    assert hardened != command
    assert hardened.endswith(tail)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX shell grammar')
@pytest.mark.parametrize('command', [
    # `$( (` opens a subshell; `$((` would open arithmetic expansion.
    'x=$( (git status --short; echo listed) | sort); echo "$x"',
    'x=` (git status --short; echo listed) | sort`; echo "$x"',
    # The escaped trailing blank belongs to the last word.
    'git status --short; echo listed\\ ',
])
def test_git_hardening_keeps_edge_blanks_that_are_shell_syntax(tmp_path, command):
    """Hardening rewrites Git words only; the blanks at the edges of a command
    or substitution body stay, because there they can be syntax."""
    from lib.project_mod.command_analysis import harden_git_inspections
    hardened = harden_git_inspections(command)
    assert '--no-optional-locks' in hardened
    environment = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True, env=environment)

    def run(source):
        return subprocess.run(['sh', '-c', source], cwd=tmp_path, env=environment,
                              capture_output=True, text=True)

    original, rewritten = run(command), run(hardened)
    assert original.stdout.startswith('listed'), original
    assert (rewritten.returncode, rewritten.stdout, rewritten.stderr) == (
        original.returncode, original.stdout, original.stderr)


@pytest.mark.skipif(os.name != 'posix', reason='cp fast path is posix-only')
def test_cp_fast_path_leaves_literal_heredoc_bodies_unchanged():
    from lib.project_mod.copy_engine import plan_cp_splice
    assert plan_cp_splice(f"cat > notes.sh <<'EOF'\n{_DATA_BODY}EOF") is None


@pytest.mark.skipif(os.name != 'posix', reason='cp fast path is posix-only')
def test_cp_fast_path_still_splices_around_a_heredoc():
    from lib.project_mod.copy_engine import plan_cp_splice
    tail = f"cat > notes.sh <<'EOF'\n{_DATA_BODY}EOF"
    spliced = plan_cp_splice(f'cp -r logs saved && {tail}')
    assert spliced is not None and not spliced.startswith('cp ')
    assert spliced.endswith(tail)


def test_git_and_cp_heredoc_lines_stay_data_through_run_command(ws):
    result = tool_run_command(ws, f"cat > notes.sh <<'EOF'\n{_DATA_BODY}EOF")
    assert '[exit code: 0]' in result
    assert _read(ws, 'notes.sh') == _DATA_BODY


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


@pytest.mark.parametrize('header', [
    "python3 - <<-'EOF'",        # tab-stripping delimiter: shape not classified
    'python3 - <<EOF',           # an expanded runtime body is Python, not shell
    "python3 -<<'EOF'",          # attached operator
    "uv run python - <<'EOF'",   # unknown stdin program
])
def test_unclassified_heredoc_bodies_are_never_rewritten(header):
    """A rewriter that cannot prove a body is shell source leaves it as written.

    Reported 2026-10-23: the body line ``cp = 1`` of a Python heredoc became a
    copy-engine invocation (SyntaxError). Rewriting is an optimisation; the
    executed bytes must not change when a body is not provably shell.
    """
    from lib.project_mod.command_analysis import harden_git_inspections
    from lib.project_mod.copy_engine import plan_cp_splice
    command = f'{header}\ncp = 1\nfind = 2\ngit = 3\nEOF'
    if os.name == 'posix':
        assert plan_cp_splice(command) is None
    assert _bound_scan_segments(command, _TIMEOUT or 'timeout', 40) == (command, 0)
    assert harden_git_inspections(command) == command


@pytest.mark.skipif(os.name != 'posix', reason='cp fast path is posix-only')
def test_shell_executed_unquoted_heredoc_keeps_its_cp_splice():
    from lib.project_mod.copy_engine import plan_cp_splice
    assert plan_cp_splice('bash <<EOF\ncp -r src dst\nEOF') is not None


@pytest.mark.skipif(os.name != 'posix', reason='cp fast path is posix-only')
@pytest.mark.parametrize('command', [
    'W=src; cp $W/a.py dst.py',          # parameter expansion
    'cp "$SRC" dst.py',
    'cp ${SRC:-a.py} dst.py',
    'cp ~/a.py dst.py',                  # tilde expansion
    'cp *.py dst/',                      # glob expansion
    'cp {a,b}.py dst/',                  # brace expansion
])
def test_cp_fast_path_never_freezes_shell_expansions(command):
    """The engine receives argv verbatim, so an expanding word must stay with the shell.

    Reported 2026-10-23: `W=/abs/dir; cp $W/a /tmp/x` failed with
    `cp: cannot stat '$W/a'` because the splice quoted the word literally.
    """
    from lib.project_mod.copy_engine import plan_cp_splice
    assert plan_cp_splice(command) is None


@pytest.mark.skipif(os.name != 'posix', reason='cp fast path is posix-only')
def test_cp_fast_path_keeps_literal_and_quoted_operands():
    from lib.project_mod.copy_engine import plan_cp_splice
    assert plan_cp_splice("echo start && cp -r logs 'saved *dir'") is not None
    assert plan_cp_splice('echo start && cp -r logs saved') is not None


def test_cp_with_parameter_expansion_copies_the_expanded_path(ws):
    result = tool_run_command(ws, 'W=logs; cp $W/app.log copied.log && cat copied.log')
    assert 'alpha ready' in result, result
    assert _read(ws, 'copied.log') == _read(ws, 'logs/app.log')
