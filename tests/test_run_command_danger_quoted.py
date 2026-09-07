"""Regression: the run_command dangerous-pattern guard must not false-positive
on words that appear INSIDE quoted arguments.

Background: ``DANGEROUS_PATTERNS`` (lib/project_mod/config.py) contains bare
words like ``\\bshutdown\\b`` / ``\\breboot\\b`` / ``\\bmkfs\\b`` / ``\\bdiskpart\\b``.
The guard scanned the whole raw command string, so a benign log-debugging
grep such as::

    grep -ihE "graceful shutdown|shutting down" logs/app.log | tail -30

matched ``\\bshutdown\\b`` inside the quoted search pattern and was blocked
("Command blocked for safety: matches dangerous pattern"), even though nothing
was actually shutting down. The fix masks quoted-literal CONTENTS before the
scan so only unquoted command structure is inspected.
"""

import pytest

from lib.project_mod.run_command import _is_dangerous_command, _mask_quoted_literals

pytestmark = pytest.mark.unit


# ── The reported false positives must now be ALLOWED ────────────────────────

@pytest.mark.parametrize('command', [
    # The exact user command that was being blocked.
    'grep -ihE "killed|out of memory|oom|signal 9|sigkill|graceful shutdown|'
    'shutting down|startup complete|recover_stale" logs/app.log | tail -30',
    # Single-quoted variants of each bare-word pattern.
    "grep 'shutdown' logs/app.log",
    "grep 'reboot sequence' logs/app.log",
    "rg 'mkfs failed' logs/app.log",
    "echo 'running diskpart step'",
    'grep -E "init 0|shutdown|reboot" logs/app.log',
])
def test_quoted_dangerous_words_are_not_blocked(command):
    assert _is_dangerous_command(command) is False


# ── Real dangerous command STRUCTURE is still blocked ───────────────────────
# NOTE: no ``rm -rf`` shape belongs in this list anymore — delete commands
# left the regex layer entirely (the blunt ``\brm\s+-rf\s+/`` false-positived
# every absolute-path delete, e.g. ``rm -rf /tmp/wt_fill``). They are guarded
# by the argument parser ``_is_catastrophic_delete``; the result-level pin
# for BOTH layers lives in tests/test_run_command_rm_rf_scoped.py.

@pytest.mark.parametrize('command', [
    'sudo shutdown -h now',
    'reboot',
    'mkfs.ext4 /dev/sda1',
    'dd if=/dev/zero of=/dev/sda',
    'echo hi > /dev/sda',
    'diskpart',
    'init 0',
    # Dangerous word before a pipe that pipes into a benign grep — the
    # unquoted `shutdown` structure must still trip the guard.
    'shutdown -h now && echo done',
])
def test_real_dangerous_commands_still_blocked(command):
    assert _is_dangerous_command(command) is True


# ── The masking helper preserves structure, blanks only quoted contents ─────

def test_mask_blanks_quoted_contents_only():
    masked = _mask_quoted_literals('grep "shutdown" file && shutdown')
    # The quoted occurrence is gone…
    assert 'shutdown' in masked  # (the trailing unquoted one remains)
    # …but exactly ONE 'shutdown' survives (the unquoted trailing command).
    assert masked.count('shutdown') == 1
    # Structure outside quotes is preserved verbatim.
    assert 'grep' in masked and '&&' in masked
    # Length is preserved (contents replaced 1:1 with spaces).
    assert len(masked) == len('grep "shutdown" file && shutdown')


def test_mask_handles_single_and_double_quotes():
    masked = _mask_quoted_literals("""a 'reboot' b "mkfs" c""")
    assert 'reboot' not in masked
    assert 'mkfs' not in masked
    assert 'a ' in masked and ' b ' in masked and ' c' in masked
