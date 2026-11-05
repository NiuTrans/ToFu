"""Structural dangerous-command guard: command words a shell would run, never data.

Incidents (2026-10-27 .. 2026-11-02): the raw-regex guard matched words
anywhere outside quotes. It refused sub-agent scratch scripts written through
a heredoc (``cat > /tmp/x.py <<'EOF'`` holding ``executor.shutdown(wait=True)``),
log greps (``grep -c shutdown logs/app.log``), flags (``--graceful-shutdown``)
and branch names, while ``sh -c 'shutdown now'`` passed because masking the
quoted literals also blanked the string the shell executes. The safety
pre-hook kept a second substring list that refused heredoc data and
``chmod -R 777 /tmp/x``, and neither layer refused the spaced fork bomb.

The guard now judges the command words of every simple command the shared
shell-structure parser says a shell may run (``-c`` strings, shell stdin
heredocs, substitution bodies, groups and subshells included), after
transparent wrappers. Argument-shaped rules are evaluated on the parsed
command. Text the parser cannot delimit falls back to the raw patterns.
run_command, the desktop agent and the pre-hook share this one judge and keep
their own refusal text.
"""

from __future__ import annotations

import json

import pytest

from lib.project_mod.command_analysis import _is_dangerous_command

pytestmark = pytest.mark.unit


BLOCKED = [
    # The shapes the old guard already refused.
    'shutdown -h now',
    'sudo reboot',
    "bash <<'EOF'\nshutdown -h now\nEOF",
    'init 0',
    'diskpart',
    'format C:',
    r'rd /s /q C:\tmp',
    'del /s /q build',
    'dd if=/dev/zero of=/dev/sda',
    'echo x > /dev/sda',
    'mkfs.ext4 /dev/sdb1',
    'chmod -R 777 /',
    # A shell's -c string is a script, not a quoted literal.
    "sh -c 'shutdown now'",
    'bash -c "reboot"',
    "bash -lc 'echo ok && reboot'",
    "sudo sh -c 'mkfs.ext4 /dev/sdb1'",
    "zsh -c 'shutdown -h now'",
    # Transparent wrappers and assignment prefixes.
    'env FOO=1 shutdown -r now',
    'FOO=1 shutdown -h now',
    'timeout 5 reboot',
    'sudo -u root /sbin/shutdown -h now',
    'nohup reboot &',
    'nice -n 5 reboot',
    'time -p reboot',
    'exec reboot',
    'command reboot',
    'xargs shutdown',
    'xargs -0 -n1 sh -c "reboot"',
    'find . -name x | xargs -I{} sudo reboot',
    r'\reboot',
    '"shutdown" -h now',
    'eval "shutdown -h now"',
    'systemctl reboot',
    # A substitution computes the program name.
    '$(which reboot)',
    'sudo "$(command -v shutdown)" -h now',
    '`which shutdown` -h now',
    # Substitution bodies, groups, subshells and shell stdin.
    'echo $(shutdown now)',
    'echo "$(reboot)"',
    'echo `reboot`',
    '( reboot )',
    '{ reboot; }',
    'if true; then shutdown -h now; fi',
    'for i in 1; do reboot; done',
    'function f { reboot; }',
    "cat <<'EOF' | sh\nreboot\nEOF",
    "bash <<< 'reboot'",
    # Argument-shaped rules on the parsed simple command.
    'echo x 2>>/dev/sdb1',
    '(echo x) > /dev/sda',
    '{ echo x; } > /dev/sda',
    'sudo dd if=/dev/urandom of=/dev/sdb bs=1M',
    'chmod -Rv 777 /*',
    ':(){ :|:& };:',
    'bomb(){ bomb|bomb& };bomb',
    'function f { f | f & }; f',
    "bash -c ':(){ :|:& };:'",
]

ALLOWED = [
    # Reported false positives.
    "cat > /tmp/x.py <<'EOF'\nexecutor.shutdown(wait=True)\nEOF",
    'grep -c shutdown logs/app.log',
    'python3 x.py --graceful-shutdown',
    'grep -E "graceful shutdown" app.log',
    "python3 -c 'pool.shutdown()'",
    "tee /tmp/notes.sh <<'EOF'\nshutdown -h now\nEOF",
    'git branch fix/shutdown-peer-disconnect',
    # Plain arguments, flags, paths and heredocs fed to non-shells.
    'echo reboot',
    'man shutdown',
    'command -v shutdown',
    'ls -l /dev/sda',
    'head -c 512 /dev/sda | xxd',
    'grep -rn "dd if=" docs',
    "python3 - <<'PY'\nimport os\nos.system('shutdown -h now')\nPY",
    "cat > /tmp/x.sh <<'EOF'\n:(){ :|:& };:\nEOF",
    "sh -c 'grep -c shutdown logs/app.log'",
    'dd if=in.img of=out.img',
    'chmod -R 777 /tmp/build',
    'systemctl status reboot.target',
    "echo ':(){ :|:& };:'",
    'printf "%s\\n" "mkfs.ext4 /dev/sdb1"',
    './scripts/graceful-shutdown.sh --dry-run',
    '$(git rev-parse --show-toplevel)/scripts/graceful-shutdown.sh --dry-run',
    'yes | yes | head -1',
]


# Runners execute their operands as a command (after their own options) or a
# string operand as shell text. The raw-regex guard refused these because it
# matched every word; the structural guard judges what the runner runs.
RUNNERS_BLOCKED = [
    'setsid reboot',
    'setsid -f -w reboot',
    'su -c reboot',
    "su -c 'shutdown now'",
    "su - root -c 'shutdown -h now'",
    'su --command=reboot root',
    'runuser -u x -- reboot',
    'runuser -u x reboot',
    "runuser -l x -c 'reboot'",
    'watch reboot',
    'watch -n 5 reboot',
    "watch 'echo hi; reboot'",
    'watch -x sudo reboot',
    r'find / -exec reboot \;',
    r'find . -execdir reboot \;',
    "find . -ok reboot ';'",
    'find . -name x -exec reboot {} +',
    r"find . -exec sh -c 'reboot' \;",
    'ssh host reboot',
    "ssh host 'sudo reboot'",
    'ssh -p 2222 -i key user@host shutdown -h now',
    'ssh host -t reboot',
    "ssh host <<'EOF'\nreboot\nEOF",
    "ssh host bash <<'EOF'\nreboot\nEOF",
    'docker exec c reboot',
    'docker exec -it -u root c reboot',
    'docker container exec c reboot',
    'docker --context prod exec c reboot',
    'podman exec c reboot',
    'podman exec -l reboot',
    'kubectl exec pod -- reboot',
    'kubectl -n ns exec -it pod -c app -- reboot',
    'kubectl exec pod reboot',
    'flock /tmp/l reboot',
    "flock -w 5 /tmp/l -c 'reboot'",
    'chroot / reboot',
    'chroot --userspec=a:b / reboot',
    'unshare reboot',
    'unshare -m -U --propagation private reboot',
    'nsenter -t 1 -m reboot',
    'nsenter --target=1 --mount reboot',
    'ionice -c 3 reboot',
    'chrt 10 reboot',
    'chrt -f 10 reboot',
    'taskset 0x1 reboot',
    'taskset -c 0-3 reboot',
    "script -c 'reboot'",
    "script -q -c 'reboot' /dev/null",
    "script /dev/null -c 'shutdown now'",
    'sudo setsid nohup timeout 5 ssh host reboot',
    # ANSI-C quoted shell strings.
    "bash -c $'reboot'",
    "sh -c $'echo ok\\nreboot'",
    "su -c $'\\x72eboot'",
]

RUNNERS_ALLOWED = [
    'setsid python3 server.py --graceful-shutdown',
    "su -c 'grep -c shutdown /var/log/syslog'",
    'runuser -u x -- grep shutdown log',
    'watch -n 5 grep -c shutdown logs/app.log',
    "watch 'grep \"graceful shutdown\" app.log'",
    r'find . -name "*.log" -exec grep -c shutdown {} +',
    r'find . -name reboot -print',
    'find . -path ./reboot -prune',
    "ssh host 'grep -c shutdown /var/log/syslog'",
    'ssh host',
    "ssh host python3 - <<'PY'\nimport os\nos.system('reboot')\nPY",
    'docker exec c grep shutdown /var/log/app.log',
    'docker logs reboot',
    'docker ps --filter name=reboot',
    'kubectl get pods -l app=reboot',
    'kubectl exec pod -- grep shutdown /var/log/app.log',
    'flock /tmp/l python3 x.py --graceful-shutdown',
    "flock /tmp/l -c 'grep shutdown log'",
    'chroot /srv/root grep shutdown /etc/x',
    'nsenter -t 1 -m grep shutdown /proc/1/status',
    'ionice -c 3 -p 1234',
    'chrt -p 10 1234',
    'taskset -p 0x1 1234',
    "script -q -c 'grep shutdown log' /dev/null",
    "bash -c $'grep -c shutdown log'",
    "echo $'reboot'",
]


@pytest.mark.parametrize('command', RUNNERS_BLOCKED)
def test_runner_commands_are_blocked(command):
    assert _is_dangerous_command(command) is True


@pytest.mark.parametrize('command', RUNNERS_ALLOWED)
def test_runner_data_is_admitted(command):
    assert _is_dangerous_command(command) is False


@pytest.mark.parametrize('command', [
    # A known runner whose options are not understood hides its program.
    'setsid --bogus reboot',
    'ssh -Z host reboot',
    'docker exec --bogus c reboot',
    'nsenter --bogus reboot',
    'flock --bogus /tmp/l reboot',
])
def test_unknown_runner_options_fall_back_to_the_raw_patterns(command):
    assert _is_dangerous_command(command) is True


def test_unknown_runner_options_without_a_pattern_are_admitted():
    assert _is_dangerous_command('setsid --bogus python3 x.py') is False


# sudo(8) and doas(1) options that take a value, in every spelling getopt
# accepts: separate, attached, `--long value`, `--long=value`, clusters,
# VAR=value words before or between options, `--`, and options whose value is
# optional (`-E`, `--preserve-env`). Before 2026-11-02 the unwrap knew eight
# sudo value letters and no long option, cluster or VAR=value word, so the
# value was judged as the program: `sudo -D /tmp reboot` was judged as `/tmp`.
PRIVILEGE_OPTION_FORMS = [
    'sudo -u x', 'sudo -ux', 'sudo --user x', 'sudo --user=x',
    'sudo -g w', 'sudo -gw', 'sudo --group w', 'sudo --group=w',
    'sudo -D /tmp', 'sudo -D/tmp', 'sudo --chdir /tmp', 'sudo --chdir=/tmp',
    'sudo -R /', 'sudo -R/', 'sudo --chroot /', 'sudo --chroot=/',
    'sudo -T 5', 'sudo -T5', 'sudo --command-timeout 5', 'sudo --command-timeout=5',
    'sudo -C 3', 'sudo -C3', 'sudo --close-from 3', 'sudo --close-from=3',
    'sudo -h host', 'sudo -hhost', 'sudo --host host', 'sudo --host=host',
    'sudo -p pw', 'sudo -ppw', 'sudo --prompt pw', 'sudo --prompt=pw',
    'sudo -r role', 'sudo -rrole', 'sudo --role role', 'sudo --role=role',
    'sudo -t type', 'sudo -ttype', 'sudo --type type', 'sudo --type=type',
    'sudo -U y', 'sudo -Uy', 'sudo --other-user y', 'sudo --other-user=y',
    # BSD login class and auth type; sudo without them exits with a usage error.
    'sudo -c staff', 'sudo -cstaff', 'sudo --login-class staff',
    'sudo --login-class=staff', 'sudo -a passwd', 'sudo -apasswd',
    'sudo --auth-type passwd', 'sudo --auth-type=passwd',
    'sudo -nu x', 'sudo -Eu x', 'sudo -HnD /tmp', 'sudo -iu x', 'sudo -E',
    'sudo --preserve-env', 'sudo --preserve-env=PATH -u x', 'sudo FOO=1 -u x',
    'sudo -u x FOO=1', 'sudo FOO=1 -D /tmp BAR=2', 'sudo ./x=y -u x',
    'sudo -u x --', 'sudo -p -u -D /tmp',
    'doas -u x', 'doas -ux', 'doas -C /etc/doas.conf', 'doas -C/etc/doas.conf',
    'doas -a passwd', 'doas -apasswd', 'doas -nu x', 'doas -n -u x', 'doas -u x --',
]


@pytest.mark.parametrize('wrapper', PRIVILEGE_OPTION_FORMS)
def test_privilege_wrapper_option_values_never_become_the_program(wrapper):
    assert _is_dangerous_command(f'{wrapper} reboot') is True


@pytest.mark.parametrize('command', [
    "sudo -s <<'EOF'\nreboot\nEOF",
    "sudo -i <<< 'shutdown -h now'",
    "cat <<'EOF' | sudo -u x -s\nreboot\nEOF",
    "doas -s <<'EOF'\nreboot\nEOF",
])
def test_a_privilege_wrapper_without_a_command_runs_a_shell_on_stdin(command):
    assert _is_dangerous_command(command) is True


@pytest.mark.parametrize('command', [
    'sudo -u x ls /',
    'sudo --user x grep -c shutdown logs/app.log',
    'sudo -D /tmp systemctl status reboot.target',
    'sudo --chdir=/tmp python3 x.py --graceful-shutdown',
    'sudo FOO=1 -u x ls /',
    'doas -u x ls /',
    'doas -C /etc/doas.conf ls /',
    "sudo -s <<'EOF'\necho reboot\nEOF",
])
def test_benign_privileged_commands_are_admitted(command):
    assert _is_dangerous_command(command) is False


@pytest.mark.parametrize('command', [
    'sudo --bogus x reboot',
    'sudo -Z x reboot',
    'doas -Z x reboot',
])
def test_unknown_privilege_wrapper_options_fall_back_to_the_raw_patterns(command):
    assert _is_dangerous_command(command) is True


def test_unknown_privilege_wrapper_options_without_a_pattern_are_admitted():
    assert _is_dangerous_command('sudo --bogus python3 x.py') is False


@pytest.mark.parametrize('command', BLOCKED)
def test_dangerous_command_words_are_blocked(command):
    assert _is_dangerous_command(command) is True


@pytest.mark.parametrize('command', ALLOWED)
def test_data_is_never_judged_as_a_command(command):
    assert _is_dangerous_command(command) is False


@pytest.mark.parametrize('command', [
    # The parser cannot delimit these; their raw text names a dangerous command.
    'shutdown -h now; echo "unterminated',
    "sh -c 'reboot; echo \"x'",
    'if exist build (del /s /q build)',
    # A wrapper whose options are not understood hides the program word.
    "env -S 'shutdown -h now'",
    'timeout --bogus 5 reboot',
])
def test_unknown_structure_fails_closed_on_a_dangerous_pattern(command):
    assert _is_dangerous_command(command) is True


@pytest.mark.parametrize('command', [
    'echo "unterminated',
    "sh -c 'echo \"x'",
    "env -S 'echo hi'",
])
def test_unknown_structure_without_a_dangerous_pattern_is_allowed(command):
    assert _is_dangerous_command(command) is False


@pytest.mark.parametrize('command, dangerous', [
    ("sh -c 'shutdown now'", True),
    ("cat > /tmp/x.py <<'EOF'\nexecutor.shutdown(wait=True)\nEOF", False),
])
def test_background_wrapper_keeps_the_verdict(command, dangerous):
    # The exact shape the background runner hands the executor-side guard.
    from lib.tasks_pkg.background_commands import _wrap_for_record
    paths = {'stdout': '/tmp/tofu-dc-bg/stdout.log',
             'stderr': '/tmp/tofu-dc-bg/stderr.log',
             'exit': '/tmp/tofu-dc-bg/exit'}
    assert _is_dangerous_command(_wrap_for_record(command, paths)) is dangerous


# ═══════════════════════════════════════════════════════════════════════════
#  Every enforcement point shares the judge and keeps its refusal text
# ═══════════════════════════════════════════════════════════════════════════

# Dangerous by rule (`dd` with an `of=/…` operand inside a `-c` string) and
# harmless if a regression ever let it run.
_HARMLESS_DANGEROUS = "sh -c 'dd if=/dev/null of=/dev/null count=0'"
_HEREDOC_DATA = "cat > {target} <<'EOF'\nexecutor.shutdown(wait=True)\nEOF"


def _forbid_spawn(monkeypatch):
    def _tripwire(*_args, **_kwargs):
        raise RuntimeError('subprocess spawned for a refused command')
    monkeypatch.setattr('subprocess.Popen', _tripwire)


@pytest.fixture()
def desktop_root(tmp_path, monkeypatch):
    import lib.desktop_agent._project as project
    root = tmp_path / 'app'
    root.mkdir()
    config = tmp_path / 'cfg.json'
    config.write_text(json.dumps(
        {'share_roots': [{'name': 'app', 'path': str(root)}]}), encoding='utf-8')
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(config))
    project._freshness.clear()
    yield root
    project._freshness.clear()


class TestRunCommand:
    def test_refuses_a_shell_string_with_unchanged_text(self, monkeypatch, tmp_path):
        from lib.project_mod.run_command import tool_run_command
        _forbid_spawn(monkeypatch)
        assert tool_run_command(str(tmp_path), _HARMLESS_DANGEROUS) == (
            'Error: Command blocked for safety: matches dangerous pattern.')

    def test_runs_a_heredoc_that_writes_data(self, tmp_path):
        from lib.project_mod.run_command import tool_run_command
        target = tmp_path / 'x.py'
        result = tool_run_command(str(tmp_path), _HEREDOC_DATA.format(target=target))
        assert '[exit code: 0]' in result, result
        assert target.read_text(encoding='utf-8') == 'executor.shutdown(wait=True)\n'


class TestDesktopAgent:
    def test_refuses_a_shell_string_with_unchanged_text(self, desktop_root, monkeypatch):
        import lib.desktop_agent._project as project
        _forbid_spawn(monkeypatch)
        out = project.cmd_project_run_command(
            {'root': 'app', 'command': _HARMLESS_DANGEROUS})
        assert out == {'error': 'command blocked by dangerous-pattern guard'}

    def test_runs_a_heredoc_that_writes_data(self, desktop_root):
        import lib.desktop_agent._project as project
        out = project.cmd_project_run_command(
            {'root': 'app', 'command': _HEREDOC_DATA.format(target='x.py')})
        assert out.get('exit_code') == 0, out
        assert (desktop_root / 'x.py').read_text(encoding='utf-8') == (
            'executor.shutdown(wait=True)\n')


class TestSafetyPreHook:
    @staticmethod
    def _hook(command):
        from lib.tasks_pkg.tool_hooks import _run_command_safety_hook
        return _run_command_safety_hook('run_command', {'command': command}, {})

    @pytest.mark.parametrize('command', [
        _HARMLESS_DANGEROUS, 'mkfs.ext4 /dev/sda1', 'chmod -R 777 /',
        ':(){ :|:& };:', 'dd if=/dev/zero of=/dev/sdb'])
    def test_blocks_with_unchanged_text_and_recovery_guidance(self, command):
        result = self._hook(command)
        assert result is not None and result.action == 'block'
        assert result.message.startswith('Blocked dangerous command pattern: ')
        assert 'do not retry the identical command' in result.additional_context

    @pytest.mark.parametrize('command', [
        _HEREDOC_DATA.format(target='/tmp/x.py'),
        # Substrings of the retired hook list inside data and scoped paths.
        "cat > /tmp/x.sh <<'EOF'\n" + ':(){:|:&};:' + "\nEOF",
        'chmod -R 777 /tmp/build',
        "grep -n 'mkfs.ext4' notes.md",
    ])
    def test_admits_data(self, command):
        assert self._hook(command) is None
