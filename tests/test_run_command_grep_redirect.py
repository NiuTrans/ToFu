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
  1. classifier matrix — filesystem greps detected; stream filters / rg /
     git grep / timeout-wrapped / heredoc shapes pass through;
  2. redirection stripping — ``2>/dev/null`` must not fake a file operand;
  3. shell structure (2026-08-06 incident): subshell parens, command
     substitution and newlines are command BOUNDARIES, not word chars —
     the closer in ``( producer | grep -E 'pat' )`` must not become a
     phantom file operand (stream filter misrefused), and the opener in
     ``( grep -rn x lib/ )`` must not mask the command word (evasion);
  4. end-to-end through ``tool_run_command`` (owner ruling 2026-08-06):
     filesystem greps are EXECUTED by the in-process GNU-faithful engine
     (lib/project_mod/grep_redirect.py) and the pipeline spliced around
     temp files — refuse+teach only when translation is impossible;
  5. quoted multi-word argv values use one shared shell parser in both the
     transparent planner and refusal fallback;
  6. the kill switches TOFU_RUN_GREP_GUARD=0 / TOFU_GREP_REDIRECT=0.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_run_command_grep_redirect.py -v
"""

from __future__ import annotations

import os

import pytest

from lib.project_mod.command_analysis import _grep_filesystem_segment
from lib.project_mod.run_command import tool_run_command

pytestmark = pytest.mark.unit


@pytest.fixture()
def ws(tmp_path):
    base = tmp_path / 'chatui'
    (base / 'lib').mkdir(parents=True)
    (base / 'lib' / 'a.py').write_text('x = 1\n', encoding='utf-8')
    (base / 'static').mkdir(parents=True)
    (base / 'static' / 'styles.css').write_text(
        ':root { --cream: #f5f0e6; --gold: #c9a227; }\n'
        'body { color: var(--cream); }\n', encoding='utf-8')
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
        'grep "not found" app.log',
        'grep -i "not found" app.log',
        'grep -- "a b" app.log',
        'grep -e "a b" app.log',
        'grep --include=*.py -rn x lib/',
        '( grep -rn x lib/ )',                  # subshell framing is no evasion
        'echo $(grep -rn x lib/)',              # command substitution seen through
        'echo start\ngrep -rn x lib/',          # newline is a command separator
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
        'ldd /bin/sh 2>/dev/null | grep "not found" | sort -u',
        "printf 'two words\\n' | grep -i 'two words'",
        "printf 'a b\\n' | grep -- 'a b'",
        'printf "%s\\n" "$V" | grep "$PATTERN"',
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
        'grep "a b" -',                        # quoted pattern, explicit stdin
        'grep x <<EOF',                         # heredoc → fail open
        'timeout 30 grep -rn x lib/',           # bounded wrapper — see scan guard
        'cat f.log | grep x',                   # grep filters cat's stream
        'find . -name "*.py" | xargs grep x',   # xargs not unwrapped (fail open)
        # 2026-08-06 false positive: subshell framing must not glue the
        # closer ``)`` onto the grep segment as a phantom file operand.
        "( tls_env='TOFU_TLS=0'; PORT=15000 env x | grep -E '^(TOFU_TLS|PORT)=' )",
        '( ps aux | grep python )',
        'out=$(ps aux | grep python)',          # command substitution, stream
        'ps aux | grep python\necho done',      # newline-separated commands
    ])
    def test_stream_filters_and_fast_paths_allowed(self, command):
        assert _grep_filesystem_segment(command) is None, (
            f'should allow: {command}')


# ═════════════════════════════════════════════════════════════════════
#  2. End-to-end through tool_run_command
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestGrepRedirectEndToEnd:
    def test_incident_shape_executes_via_internal_engine(self, ws):
        """Owner ruling 2026-08-06: the exact 17-minute incident command is
        now EXECUTED by the in-process engine — no refusal, pipeline
        continues, matches come back."""
        result = tool_run_command(
            ws, "grep -n 'cream' static/styles.css | head -30; "
                "echo ---; grep -rn 'cream' static/*.css 2>/dev/null | head -20")
        assert 'Command intercepted' not in result
        assert '--cream: #f5f0e6' in result   # the match line, with line no
        assert '1::root { --cream' in result
        assert '---' in result

    def test_display_callback_is_out_of_band(self, ws):
        seen = []
        result = tool_run_command(
            ws, 'grep -n "x = 1" lib/a.py',
            on_grep_intercept=seen.append)
        assert seen == [1]
        assert 'grep_search' not in result
        assert 'x = 1' in result

    def test_recursive_grep_executes(self, ws):
        result = tool_run_command(ws, 'grep -rn "x = 1" lib/')
        assert 'Command intercepted' not in result
        assert 'lib/a.py:1:x = 1' in result

    def test_pipeline_continuation_wc(self, ws):
        result = tool_run_command(ws, 'grep -n "x = 1" lib/a.py | wc -l')
        assert 'Command intercepted' not in result
        assert '1' in result

    def test_grep_then_cat_into_file(self, ws):
        """The owner's canonical shape: grep X, then cat into a file."""
        result = tool_run_command(
            ws, 'grep "x = 1" lib/a.py | cat > found.txt; cat found.txt')
        assert 'Command intercepted' not in result
        assert 'x = 1' in result
        with open(os.path.join(ws, 'found.txt')) as f:
            assert f.read() == 'x = 1\n'

    def test_quiet_grep_exit_code_chain(self, ws):
        result = tool_run_command(
            ws, 'grep -q "x = 1" lib/a.py && echo FOUND; '
                'grep -q zzz lib/a.py || echo MISSING')
        assert 'FOUND' in result and 'MISSING' in result

    def test_full_gnu_option_fallback(self, ws):
        """Options not translated to rg execute through native GNU grep."""
        result = tool_run_command(ws, "grep -P 'x+' lib/a.py")
        assert 'Command intercepted' not in result
        assert 'x = 1' in result
        assert '[exit code: 0]' in result

    def test_earlier_segment_write_is_visible(self, ws):
        """Runtime delegation observes bytes written by an earlier segment."""
        result = tool_run_command(
            ws, "printf 'y = 2\\n' > lib/new.py; grep -rn 'y = 2' lib/")
        assert 'Command intercepted' not in result
        assert 'lib/new.py:1:y = 2' in result

    def test_redirect_kill_switch_restores_refusal(self, ws, monkeypatch):
        monkeypatch.setenv('TOFU_GREP_REDIRECT', '0')
        result = tool_run_command(ws, 'grep -rn "x = 1" lib/')
        assert 'Command intercepted' in result

    def test_stream_filter_actually_runs(self, ws):
        result = tool_run_command(ws, "printf 'a\\nb\\n' | grep a")
        assert 'Command intercepted' not in result
        assert 'a' in result

    def test_quoted_multiword_stream_filter_actually_runs(self, ws):
        result = tool_run_command(
            ws, 'printf "libx.so => not found\\nliby.so => ok\\n" '
                '| grep "not found" | sort -u')
        assert 'Command intercepted' not in result
        output = result.split('\n', 1)[1]
        assert 'libx.so => not found' in output
        assert 'liby.so => ok' not in output

    def test_subshell_stream_filter_actually_runs(self, ws):
        """The 2026-08-06 incident shape: a stream filter inside a subshell
        was refused because the closer ``)`` tokenized as a file operand."""
        result = tool_run_command(ws, "( printf 'a\\nb\\n' | grep a )")
        assert 'Command intercepted' not in result
        assert 'a' in result

    def test_kill_switch_disables_guard(self, ws, monkeypatch):
        monkeypatch.setenv('TOFU_RUN_GREP_GUARD', '0')
        result = tool_run_command(ws, 'grep -rn "x = 1" lib/')
        assert 'Command intercepted' not in result
        assert 'a.py' in result


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
