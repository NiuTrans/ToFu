"""The run_command log line identifies the command that holds a lease.

Lease forensics join `[WorkspaceLease] wait … holder:` lines to the holder's
`run_command: $ …` line. A 120-char cut usually ended inside the redundant
`cd <cwd> && ` prefix (measured 2026-10-23: 862 of 993 logged commands were
cut), so a multi-hour holder could not be identified from logs.
"""
import pytest

from lib.project_mod.run_command import _LOGGED_COMMAND_CHARS, _logged_command

pytestmark = pytest.mark.unit


def test_redundant_cd_to_the_logged_cwd_is_elided():
    assert _logged_command('cd /repo && sh scripts/check.sh', '/repo') == 'sh scripts/check.sh'
    assert _logged_command('cd /other && sh x.sh', '/repo') == 'cd /other && sh x.sh'


def test_logged_command_stays_one_line():
    shown = _logged_command("python3 - <<'EOF'\nprint(1)\nEOF", '/repo')
    assert '\n' not in shown and '\\n' in shown


def test_long_commands_are_bounded_with_an_explicit_remainder():
    command = 'echo ' + 'x' * (_LOGGED_COMMAND_CHARS * 2)
    shown = _logged_command(command, '/repo')
    assert shown.startswith('echo xxx')
    assert shown.endswith(f'[+{len(command) - _LOGGED_COMMAND_CHARS} chars]')
