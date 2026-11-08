"""Boot the x86_64 build of the payload on this host, without proot (README §8).

The payload's own python runs ``bin/tofu-local-kernel`` with a temporary data
directory and a clean environment, exactly as the device would, minus proot
and the mounts. One boot serves every test here. Run with
``--runtime-host-payload <out>/x86_64``.
"""
import http.client
import json
from pathlib import Path
import secrets
import signal
import subprocess
import tarfile
import time

import pytest

# Same bounds as the local kernel's own CLI boot test
# (tests/server/test_local_kernel_entry.py): a cold boot on a busy host.
pytestmark = [pytest.mark.slow, pytest.mark.timeout(900)]
READY_SECONDS = 600


def _request(port, path, headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
    try:
        connection.request('GET', path, headers={'Host': f'127.0.0.1:{port}', **(headers or {})})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _snapshot(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob('*')}


@pytest.fixture(scope='module')
def boot(host_payload, tmp_path_factory):
    """Extract, boot, probe and stop the kernel once; return what happened."""
    tmp_path = tmp_path_factory.mktemp('host-boot')
    (archive,) = (host_payload / 'assets' / 'kernel').glob('payload-*.tgz')
    payload = tmp_path / 'payload'
    with tarfile.open(archive) as bundle:
        bundle.extractall(payload, filter='tar')
    before = _snapshot(payload)
    # Like /var/lib/tofu: a TOFU_DATA_DIR not named "data" is itself the
    # runtime base, so logs and uploads stay inside it (runtime_path_policy.py).
    home, data = tmp_path / 'home', tmp_path / 'var-lib-tofu'
    home.mkdir()
    secret = 'tofu_admin_' + secrets.token_hex(32)
    stderr_path = tmp_path / 'kernel.stderr'
    environment = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'LANG': 'C.UTF-8'}
    result = {'secret': secret, 'data': data, 'tmp': tmp_path, 'stderr': stderr_path}
    with open(stderr_path, 'wb') as stderr:
        process = subprocess.Popen(
            [str(payload / 'bin' / 'tofu-local-kernel'), '--data-dir', str(data)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, env=environment,
            cwd=tmp_path)
    try:
        process.stdin.write((secret + '\n').encode())
        process.stdin.close()
        result['record'] = json.loads(process.stdout.readline())
        result['pid'] = process.pid
        port = result['record']['port']
        bearer = {'Authorization': f'Bearer {secret}'}
        deadline = time.monotonic() + READY_SECONDS
        status = None
        while time.monotonic() < deadline:
            status, body = _request(port, '/api/v4/meta', bearer)
            if status == 200:
                result['meta'] = json.loads(body)
                break
            time.sleep(0.5)
        result['meta_status'] = status
        result['wrong_secret'] = _request(port, '/api/v4/meta', {
            'Authorization': 'Bearer tofu_admin_' + 'e' * 64})[0]
        result['keys_without'] = _request(port, '/api/v4/keys')[0]
        result['keys_with'] = _request(port, '/api/v4/keys', bearer)[0]
        result['foreign_host'] = _request(port, '/api/v4/meta', {
            **bearer, 'Host': 'rebound.example'})[0]
    finally:
        process.send_signal(signal.SIGTERM)
        result['exit_code'] = process.wait(timeout=180)
    result['written'] = sorted(path for path in _snapshot(payload) - before
                               if '__pycache__' not in path)
    return result


def test_kernel_reports_its_port_and_runs_as_the_launched_pid(boot):
    assert boot['record']['event'] == 'listening' and boot['record']['host'] == '127.0.0.1'
    assert boot['record']['pid'] == boot['pid'], 'the launcher execs into the kernel'


def test_meta_answers_200_with_the_secret(boot):
    assert boot['meta_status'] == 200
    assert isinstance(boot['meta'], dict)


def test_private_mode_and_host_fence(boot):
    assert boot['wrong_secret'] == 401
    assert boot['keys_without'] == 401
    assert boot['keys_with'] == 200
    assert boot['foreign_host'] == 403


def test_sigterm_exits_zero(boot):
    assert boot['exit_code'] == 0


def test_state_stays_in_the_data_directory_and_the_secret_in_no_log(boot):
    data = boot['data']
    assert (data / 'logs').is_dir(), 'logs follow the data directory'
    assert sorted(path.name for path in boot['tmp'].iterdir()) == [
        'home', 'kernel.stderr', 'payload', 'var-lib-tofu'], 'nothing lands beside the data directory'
    assert boot['secret'] not in boot['stderr'].read_text(errors='replace')
    assert not any(boot['secret'] in path.read_text(errors='ignore')
                   for path in (data / 'logs').rglob('*') if path.is_file())


def test_the_payload_stays_unwritten(boot):
    assert boot['written'] == [], f'the kernel wrote into its payload: {boot["written"][:20]}'
