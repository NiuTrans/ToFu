"""tests/test_run_command_grep_redirect.py — filesystem-grep redirect guard.

Incident (2026-08-04, owner screenshot): the model ran
``grep -n 'cream\\|#f5f0\\|--gold\\|--ink' static/styles.css | head -30;
echo ---; grep -rn 'cream' static/*.css 2>/dev/null | head -20`` via
run_command and it sat RUNNING for 17m04s in a FUSE bad window with zero
output and no timeout to end it. The run_command tool description had
*advised* grep_search-over-grep for months — advice is not enforcement.

The guard (``_grep_filesystem_segment`` + its call site in
``tool_run_command``): refuse a grep-family segment (grep / egrep / fgrep,
sudo seen through) that reads the FILESYSTEM — explicit file/dir operands
or a recursive flag (implicit cwd walk) — with a message translating the
call to grep_search. Stream filters stay legal: grep with no operands
whose stdin is fed by a pipe (``pytest 2>&1 | grep PASS``,
``ps aux | grep python``) cannot be replaced by grep_search. ``rg``,
``git grep`` and coreutils-``timeout``-wrapped greps also stay legal.

Pinned here:
  1. classifier matrix — filesystem greps refused; stream filters / rg /
     git grep / timeout-wrapped / heredoc shapes allowed;
  2. redirection stripping — ``2>/dev/null`` must not fake a file operand;
  3. end-to-end through ``tool_run_command`` — refused BEFORE any
     subprocess (Popen tripwire); allowed shapes actually execute;
  4. the kill switch TOFU_RUN_GREP_GUARD=0.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_run_command_grep_redirect.py -v
"""

from __future__ import annotations

import pytest

from lib.project_mod.command_analysis import _grep_filesystem_segment
from lib.project_mod.run_command import tool_run_command

pytestmark = pytest.mark.unit


@pytest.fixture()
def ws(tmp_path):
    base = tmp_path / 'chatui'
    (base / 'lib').mkdir(parents=True)
    (base / 'lib' / 'a.py').write_text('x = 1\n', encoding='utf-8')
    return str(base)


# ═════════════════════════════════════════════════════════════════════
#  1. Classifier matrix
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestGrepRedirectBlockedShapes:
    @pytest.mark.parametrize('command', [
        # The incident itself (both grep segments carry operands).
        "grep -n 'cream\\|#f5f0\\|--gold\\|--ink' static/styles.css | head -30; "
        "echo ---; grep -rn 'cream' static/*.css 2>/dev/null | head -20",
        'grep -rn x lib/',                      # the worst-case shape
        'grep -rn x',                           # recursive, implicit cwd walk
        'grep -r x .',
        'grep --recursive x lib',
        'grep -irn x lib/',                     # clustered flags
        'grep -n x lib/a.py',                   # single file, non-recursive
        'grep x /tmp/out.log',
        'grep -c x lib/a.py',                   # count mode → count_only=True
        'egrep -n foo bar.py',
        'fgrep -rn x .',
        '/usr/bin/grep -rn x lib/',             # path-prefixed binary
        'sudo grep -rn x lib/',                 # privilege wrapper seen through
        'LC_ALL=C grep -rn x lib/',             # env prefix stripped
        'cd lib && grep -rn x .',               # chain segment
        'grep -rn -e x lib/',                   # -e pattern, operand after
        'grep -rn "foo|bar" lib/',              # quoted pipe stays unsplit
        'grep -rn x lib/ > /tmp/out.txt',       # redirect stripped, operand caught
        'grep -q x config.yaml && echo found',  # control-flow grep → adapt
        'grep -m 5 x lib/a.py',                 # arg-flag consumed, operand caught
        'grep --include=*.py -rn x lib/',
    ])
    def test_filesystem_greps_blocked(self, command):
        assert _grep_filesystem_segment(command) is not None, (
            f'should intercept: {command}')


@pytest.mark.unit
class TestGrepRedirectAllowedShapes:
    @pytest.mark.parametrize('command', [
        'ps aux | grep python',                 # stream filter — no replacement
        'ps aux | grep -v grep 2>/dev/null',    # redirect must not fake an operand
        'pytest -q 2>&1 | grep PASS',
        'pytest -q 2>&1 | grep -v slow | head -20',
        'git log --oneline | grep fix',
        'make 2>&1 | tail -50',
        'docker logs c 2>&1 | grep -i err | head',
        'rg -n x lib/',                         # already the fast path
        'git grep -n x',                        # git-index backed
        'printf "a\\nb\\n" | grep a',
        'echo "$V" | grep -q x',
        'grep -e foo',                          # stdin grep, no operands
        'grep x -',                             # explicit stdin marker
        'grep x <<EOF',                         # heredoc → fail open
        'timeout 30 grep -rn x lib/',           # bounded wrapper — see scan guard
        'cat f.log | grep x',                   # grep filters cat's stream
        'find . -name "*.py" | xargs grep x',   # xargs not unwrapped (fail open)
    ])
    def test_stream_filters_and_fast_paths_allowed(self, command):
        assert _grep_filesystem_segment(command) is None, (
            f'should allow: {command}')


# ═════════════════════════════════════════════════════════════════════
#  2. End-to-end through tool_run_command
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestGrepRedirectEndToEnd:
    def test_incident_shape_refused_before_any_spawn(self, ws, monkeypatch):
        """The exact incident command must be refused BEFORE a subprocess
        exists — the Popen tripwire proves it."""
        def _tripwire(*_a, **_kw):
            raise RuntimeError('subprocess spawned for a refused command')
        monkeypatch.setattr('subprocess.Popen', _tripwire)
        result = tool_run_command(
            ws, "grep -n 'cream' static/styles.css | head -30; "
                "echo ---; grep -rn 'cream' static/*.css 2>/dev/null | head -20")
        assert 'Command intercepted' in result
        # The error is ACTIONABLE: it must teach the grep_search translation.
        assert 'grep_search' in result
        assert 'max_results' in result

    def test_recursive_grep_refused(self, ws, monkeypatch):
        def _tripwire(*_a, **_kw):
            raise RuntimeError('subprocess spawned for a refused command')
        monkeypatch.setattr('subprocess.Popen', _tripwire)
        result = tool_run_command(ws, 'grep -rn "x = 1" lib/')
        assert 'Command intercepted' in result

    def test_stream_filter_actually_runs(self, ws):
        result = tool_run_command(ws, "printf 'a\\nb\\n' | grep a")
        assert 'Command intercepted' not in result
        assert 'a' in result

    def test_kill_switch_disables_guard(self, ws, monkeypatch):
        monkeypatch.setenv('TOFU_RUN_GREP_GUARD', '0')
        result = tool_run_command(ws, 'grep -rn "x = 1" lib/')
        assert 'Command intercepted' not in result
        assert 'a.py' in result


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
