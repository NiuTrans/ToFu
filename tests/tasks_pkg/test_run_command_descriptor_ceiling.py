"""run_command waits on its command's pipes without spinning.

Long-lived servers reach descriptor numbers above select()'s 1024 ceiling
(FD_SETSIZE). Live 2026-11-07: the execution host's command pipes were fds
1817 and 4009, select() raised, the pipe wait returned at once, and both run
loops spun on ``proc.poll()`` without ever draining output, so a command that
filled a pipe blocked until its deadline. A running command that closed its
own stdio made the same loops spin on end-of-file.
"""

import os
import shlex
import sys
import time

import pytest

from lib.compat import IS_WINDOWS

pytestmark = [pytest.mark.unit, pytest.mark.skipif(IS_WINDOWS, reason='POSIX descriptors')]

SELECT_CEILING = 1024  # FD_SETSIZE of select(2)
LOOP_MODES = pytest.mark.parametrize('interactive', [False, True], ids=['simple', 'interactive'])


@pytest.fixture
def low_descriptors_taken():
    """Occupy every descriptor below the select ceiling, as a long-lived
    server does, so the command's pipes are numbered above it."""
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 4096
    if hard != resource.RLIM_INFINITY and hard < wanted:
        pytest.skip(f'descriptor hard limit {hard} is below {wanted}')
    raised = soft != resource.RLIM_INFINITY and soft < wanted
    if raised:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    held = []
    try:
        while True:
            fd = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
            if fd >= SELECT_CEILING:
                os.close(fd)
                break
            held.append(fd)
        yield
    finally:
        for fd in held:
            os.close(fd)
        if raised:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def _run(tmp_path, command, interactive):
    from lib.project_mod.run_command import tool_run_command
    options = {'stdin_callback': lambda hint: None} if interactive else {}
    cpu, wall = time.process_time(), time.monotonic()
    output = tool_run_command(str(tmp_path), command, timeout=60,
                              task={'aborted': False}, **options)
    return output, time.process_time() - cpu, time.monotonic() - wall


@LOOP_MODES
def test_output_larger_than_a_pipe_drains_while_pipes_are_above_the_ceiling(
        tmp_path, low_descriptors_taken, interactive):
    script = "import sys; sys.stdout.write('o' * 300000); sys.stderr.write('e' * 300000)"
    command = f'{shlex.quote(sys.executable)} -c {shlex.quote(script)}'
    output, _, wall = _run(tmp_path, command, interactive)
    assert '[exit code: 0]' in output, output[-400:]
    assert wall < 30, f'undrained pipes blocked the command for {wall:.1f}s'


@LOOP_MODES
def test_quiet_command_with_pipes_above_the_ceiling_does_not_spin(
        tmp_path, low_descriptors_taken, interactive):
    output, cpu, wall = _run(tmp_path, 'sleep 2', interactive)
    assert '[exit code: 0]' in output, output[-400:]
    assert cpu < 0.5, f'the pipe wait spun: {cpu:.2f}s CPU over {wall:.1f}s'


@LOOP_MODES
def test_running_command_that_closed_its_output_does_not_spin(tmp_path, interactive):
    output, cpu, wall = _run(tmp_path, 'exec >/dev/null 2>&1; sleep 2', interactive)
    assert '[exit code: 0]' in output, output[-400:]
    assert cpu < 0.5, f'the wait spun on end-of-file: {cpu:.2f}s CPU over {wall:.1f}s'
