"""tests/test_supervisor_conf.py — guard the supervisord program definition.

deploy/supervisor/tofu.conf hands the :15000 server's lifecycle to the host
supervisord (autostart + autorestart), which is the durable fix for the
"bare process, nobody relaunches it, restarts lose the port race" class the
manual restart kept hitting. This suite parses the conf and pins the
lifecycle-critical fields so a future edit can't silently ship a program that
(a) re-execs into the env (defeating supervisord's PID tracking), (b) doesn't
auto-restart on crash/OOM, or (c) hard-kills without a graceful drain.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_supervisor_conf.py \
     -p no:cacheprovider
"""

import configparser
import os

import pytest

pytestmark = pytest.mark.unit

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONF = os.path.join(_PROJ, 'deploy', 'supervisor', 'tofu.conf')


def _load():
    # supervisord conf is INI-shaped; configparser reads it (comments are ';').
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    with open(_CONF, 'r', encoding='utf-8') as f:
        cp.read_file(f)
    assert cp.has_section('program:tofu'), 'missing [program:tofu] section'
    return cp['program:tofu']


def test_conf_exists():
    assert os.path.isfile(_CONF), f'{_CONF} must exist'


def test_command_uses_env_interpreter_directly():
    """command= must invoke the conda env's python directly on server.py, so
    server.py's _tofu_maybe_reexec_into_env sees same-interpreter and does NOT
    os.execv — otherwise supervisord would track a PID that immediately
    re-execs, muddying stop/restart + the boot-identity PID."""
    prog = _load()
    cmd = prog.get('command', '')
    assert cmd.endswith('server.py'), f'command must run server.py: {cmd!r}'
    assert '/envs/tofu/bin/python' in cmd, (
        f'command must use the tofu conda-env interpreter directly '
        f'(avoids the env re-exec hop): {cmd!r}')


def test_directory_is_project_root():
    """``directory`` must be an ABSOLUTE path to a real Tofu checkout.

    Deliberately NOT ``== _PROJ``. The conf is a DEPLOYMENT artefact: it pins
    the absolute path of the box's live checkout, which is not the tree a test
    happens to run in. Equality made this guard fail in every ``git worktree``
    and in CI — i.e. exactly where it is supposed to be trustworthy — for a
    conf that was entirely correct. What actually matters is that the value is
    absolute and names a directory holding ``server.py``, so supervisord's cwd
    can never be a stale/relative path.
    """
    prog = _load()
    d = prog.get('directory', '')
    assert d, 'directory= must be set'
    assert os.path.isabs(d), f'directory must be absolute: {d!r}'
    assert os.path.isfile(os.path.join(d, 'server.py')), (
        f'directory must be a Tofu checkout (no server.py under it): {d!r}')


def test_lifecycle_autostart_autorestart():
    """The whole point: start on boot AND relaunch after ANY death."""
    prog = _load()
    assert prog.get('autostart', '').lower() == 'true'
    assert prog.get('autorestart', '').lower() == 'true'
    # startsecs guards a crash-loop from being reported "running".
    assert int(prog.get('startsecs', '0')) >= 10
    # bounded retries so a broken build doesn't hammer the box forever.
    assert int(prog.get('startretries', '0')) >= 1


def test_graceful_stop_signal_and_group():
    """Stop must be a graceful SIGTERM (the server traps it to drain in-flight
    tasks) with room before SIGKILL, and must target the whole process group so
    MCP subprocesses / thread pool don't orphan on stop/restart."""
    prog = _load()
    assert prog.get('stopsignal', '').upper() == 'TERM'
    assert int(prog.get('stopwaitsecs', '0')) >= 15
    assert prog.get('stopasgroup', '').lower() == 'true'
    assert prog.get('killasgroup', '').lower() == 'true'


def test_env_pins_port_deterministically():
    """PORT must be pinned in environment= so the bind is deterministic even if
    .env drifts (the port-shift class); HOME must be set for ~/.config/tokens."""
    prog = _load()
    env = prog.get('environment', '')
    assert 'PORT="15000"' in env, f'PORT must be pinned to 15000: {env!r}'
    assert 'HOME=' in env, f'HOME must be set: {env!r}'


def test_supervisor_log_path_under_project_logs():
    """The log must live under the SAME checkout's ``logs/``.

    Asserted as a RELATIONSHIP to ``directory`` rather than to the running
    tree, for the same reason as test_directory_is_project_root: a deployment
    conf legitimately carries the box's absolute path. This still catches the
    thing that matters — a log written outside the deployment (or into another
    checkout's logs/), where nothing collects or rotates it.
    """
    prog = _load()
    log = prog.get('stdout_logfile', '')
    d = prog.get('directory', '')
    assert log == os.path.join(d, 'logs', 'supervisor_tofu.log'), (
        f'stdout_logfile must be <directory>/logs/supervisor_tofu.log '
        f'(directory={d!r}): {log!r}')
