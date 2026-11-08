"""The guest entry bin/tofu-local-kernel (README §4) with a stand-in python."""
import json
import os
import shutil
import signal
import subprocess

from runtime_builder.payload import LAUNCHER

# Records what the launcher handed over, then behaves like the kernel: one
# stdout line, exit 0 on SIGTERM, otherwise the status the test asks for.
FAKE_PYTHON = """#!/bin/sh
secret=$(head -n 1)
printf '%s\\n' "$(pwd)" "$PATH" "$HOME" "$secret" "$@" > "$RECORD"
echo '{"event": "listening"}'
trap 'exit 0' TERM
if [ "$1" = local_kernel.py ] && [ "$4" = --wait ]; then
  while :; do sleep 0.1; done
fi
exit "${STATUS:-0}"
"""


def _payload(tmp_path):
    payload = tmp_path / 'payload'
    (payload / 'bin').mkdir(parents=True)
    (payload / 'python' / 'bin').mkdir(parents=True)
    (payload / 'tofu').mkdir()
    shutil.copyfile(LAUNCHER, payload / 'bin' / 'tofu-local-kernel')
    (payload / 'bin' / 'tofu-local-kernel').chmod(0o755)
    python = payload / 'python' / 'bin' / 'python3'
    python.write_text(FAKE_PYTHON)
    python.chmod(0o755)
    return payload


def _env(tmp_path, **extra):
    return {'RECORD': str(tmp_path / 'record'), **extra}


def test_hands_stdin_argv_and_environment_to_local_kernel(tmp_path):
    payload = _payload(tmp_path)
    result = subprocess.run([str(payload / 'bin' / 'tofu-local-kernel'), '--port', '0'],
                            input=b'tofu_admin_' + b'a' * 64 + b'\n', capture_output=True,
                            env=_env(tmp_path), cwd='/')
    assert result.returncode == 0
    assert result.stdout == b'{"event": "listening"}\n', 'the launcher itself prints nothing'
    cwd, path, home, secret, *argv = (tmp_path / 'record').read_text().splitlines()
    resolved = payload.resolve()
    assert cwd == str(resolved / 'tofu')
    assert path.split(':')[:2] == [str(resolved / 'bin'), str(resolved / 'python' / 'bin')]
    assert path.endswith(':/usr/sbin:/usr/bin:/sbin:/bin')
    assert home == '/root'
    assert secret == 'tofu_admin_' + 'a' * 64
    assert argv == ['local_kernel.py', '--data-dir', '/var/lib/tofu', '--port', '0']


def test_keeps_a_given_home_and_lets_a_later_data_dir_override(tmp_path):
    payload = _payload(tmp_path)
    subprocess.run([str(payload / 'bin' / 'tofu-local-kernel'), '--data-dir', '/tmp/data'],
                   input=b'x\n', capture_output=True, env=_env(tmp_path, HOME='/home/test'),
                   check=True)
    lines = (tmp_path / 'record').read_text().splitlines()
    assert lines[2] == '/home/test'
    assert lines[4:] == ['local_kernel.py', '--data-dir', '/var/lib/tofu', '--data-dir', '/tmp/data']


def test_needs_no_inherited_path(tmp_path):
    payload = _payload(tmp_path)
    result = subprocess.run([str(payload / 'bin' / 'tofu-local-kernel')], input=b'x\n',
                            capture_output=True,
                            env=_env(tmp_path, PATH='/system/bin:/apex/com.android.runtime/bin'))
    assert result.returncode == 0, result.stderr


def test_exit_status_passes_through(tmp_path):
    payload = _payload(tmp_path)
    result = subprocess.run([str(payload / 'bin' / 'tofu-local-kernel')], input=b'x\n',
                            capture_output=True, env=_env(tmp_path, STATUS='2'))
    assert result.returncode == 2


def test_sigterm_reaches_the_kernel_process(tmp_path):
    payload = _payload(tmp_path)
    process = subprocess.Popen([str(payload / 'bin' / 'tofu-local-kernel'), '--wait'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               env=_env(tmp_path))
    process.stdin.write(b'x\n')
    process.stdin.close()
    assert json.loads(process.stdout.readline()) == {'event': 'listening'}
    os.kill(process.pid, signal.SIGTERM)
    # Without exec the shell would die of SIGTERM (status -15) instead.
    assert process.wait() == 0
