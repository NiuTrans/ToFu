"""Native-command discovery is automatic, bounded and account preserving."""
import base64
import json
import os
from pathlib import Path
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.oauth import codex_command_login as command
from lib.oauth.codex_local_login import read_local_login

pytestmark = [pytest.mark.unit, pytest.mark.skipif(os.name == 'nt', reason='POSIX native-shell adapter')]


def _token(account='account-a', expired=False):
    claims = {'exp': time.time() + (-10 if expired else 3600),
              'https://api.openai.com/auth': {'chatgpt_account_id': account}}
    jwt = 'header.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.signature'
    return {'auth_mode': 'chatgpt', 'tokens': {'access_token': jwt,
            'refresh_token': 'never-copy-refresh', 'account_id': account}}


@pytest.fixture
def native(tmp_path, monkeypatch):
    """A synthetic installed Codex, including its OS shell configuration."""
    import pwd
    from types import SimpleNamespace
    monkeypatch.setattr(pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_shell='/bin/bash'))
    home = tmp_path / 'home'; home.mkdir()
    root = tmp_path / 'actual-login'; root.mkdir()
    auth = root / 'auth.json'; auth.write_text(json.dumps(_token())); auth.chmod(0o600)
    config = root / 'config.toml'; config.write_text(''); config.chmod(0o600)
    linked = home / 'native-login'; linked.mkdir()
    (linked / 'auth.json').symlink_to(auth)
    (linked / 'config.toml').symlink_to(config)
    wrong = home / '.codex'; wrong.mkdir()
    (wrong / 'auth.json').write_text(json.dumps(_token('wrong-default', expired=True)))
    bin_dir = tmp_path / 'bin'; bin_dir.mkdir()
    (home / '.bashrc').write_text(f'export CODEX_HOME="{linked}"\nexport PATH="{bin_dir}:$PATH"\n')
    program = bin_dir / 'codex'
    program.write_text('#!' + sys.executable + ' -S\n' + r'''
import json, os, pathlib, time
root = pathlib.Path(os.environ['CODEX_HOME'])
trace = root.parent / 'trace.jsonl'
trace.open('a').write(json.dumps({'pid': os.getpid(), 'event': 'start'})+'\n')
for line in __import__('sys').stdin:
    msg = json.loads(line); method = msg.get('method')
    trace.open('a').write(json.dumps({'pid': os.getpid(), 'event': method})+'\n')
    if not msg.get('id'): continue
    mode = (root.parent/'mode').read_text() if (root.parent/'mode').exists() else ''
    if mode == 'hang': time.sleep(60)

    if mode == 'hang-' + str(method): time.sleep(60)
    if method == 'initialize' and mode == 'cold': time.sleep(0.25)
    if mode == 'flood': print('x'*70000, flush=True); continue
    if mode == 'exit': __import__('sys').exit(3)
    if method == 'initialize': result = {'codexHome': str(root)}
    elif method == 'config/read': result = {'config': {}}
    elif method == 'configRequirements/read':

        if mode == 'mutate-policy': (root/'config.toml').write_text('# changed during policy read')
        result = {'requirements': {'cliAuthCredentialsStore': 'keyring'} if mode == 'keyring' else
            {'allowedLoginMethods': []} if mode == 'restricted' else None}
    elif method == 'account/read':
        if mode == 'renew-fail':
            print(json.dumps({'id':msg['id'],'error':{'code':-1,'message':'never-copy-refresh'}}),flush=True);continue
        path = root/'auth.json'; data=json.loads(path.read_text())
        import base64
        claims={'exp':time.time()+7200,'https://api.openai.com/auth':{'chatgpt_account_id':data['tokens']['account_id']}}
        data['tokens']['access_token']='header.'+base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')+'.signature'
        target=path.resolve(); following=target.with_suffix('.next')
        following.write_text(json.dumps(data));following.chmod(0o600);os.replace(following,target)
        result={'account':{'type':'chatgpt'}}
    else: result={}
    print(json.dumps({'id':msg['id'],'result':result}),flush=True)
''')
    program.chmod(0o700)
    env = {'HOME': str(home), 'SHELL': '/bin/bash', 'PATH': '/usr/bin:/bin'}
    command.clear_command_context()
    monkeypatch.setattr(command, '_DEADLINE_SECONDS', 5.0)
    from types import SimpleNamespace
    original = command.subprocess
    processes = []
    def launch(*args, **kwargs):
        child = original.Popen(*args, **kwargs); processes.append(child); return child
    monkeypatch.setattr(command, 'subprocess', SimpleNamespace(
        Popen=launch, PIPE=original.PIPE, DEVNULL=original.DEVNULL, TimeoutExpired=original.TimeoutExpired))
    yield {'home': home, 'root': root, 'linked': linked, 'auth': auth, 'env': env,
           'trace': home / 'trace.jsonl', 'mode': home / 'mode', 'processes': processes}
    command.clear_command_context()


def _read(native, **kwargs):
    return read_local_login(environment=native['env'], **kwargs)


def _events(native):
    return [json.loads(line) for line in native['trace'].read_text().splitlines()]


def test_automatically_uses_native_command_home_and_safe_symlink(native):
    snapshot = _read(native)
    assert snapshot['account_id'] == 'account-a'
    assert snapshot['access_token']
    assert 'refresh_token' not in snapshot
    assert 'CODEX_HOME' not in native['env']
    assert 'account/read' not in [e['event'] for e in _events(native)]


@pytest.mark.parametrize('service_shell', ['/bin/sh', '/bin/dash', ''])
def test_service_environment_discovers_the_os_account_login(native, service_shell):
    broken_bin = native['home'] / 'service-bin'
    broken_bin.mkdir()
    broken = broken_bin / 'codex'
    broken.write_text('#!/bin/sh\nexit 1\n')
    broken.chmod(0o700)
    environment = {**native['env'], 'SHELL': service_shell,
                   'PATH': str(broken_bin) + ':/usr/bin:/bin'}

    snapshot = read_local_login(environment=environment)

    assert snapshot.get('account_id') == 'account-a', snapshot
    assert snapshot.get('access_token')
    assert 'refresh_token' not in snapshot
    assert 'account/read' not in [event['event'] for event in _events(native)]


def test_account_shell_lookup_failure_uses_environment_shell(native, monkeypatch):
    import pwd
    def unavailable(uid):
        raise KeyError(uid)
    monkeypatch.setattr(pwd, 'getpwuid', unavailable)
    assert _read(native)['account_id'] == 'account-a'


def test_account_shell_change_invalidates_cached_native_metadata(native, monkeypatch):
    import pwd
    from types import SimpleNamespace
    assert _read(native)['account_id'] == 'account-a'
    monkeypatch.setattr(pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_shell='/bin/sh'))
    assert command.command_context(environment=native['env']) == {
        'shared_login_issue': 'codex_command_missing'}


def test_concurrent_reads_share_one_probe_but_follow_file_replacement(native):
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: _read(native), range(16)))
    assert all(v.get('access_token') for v in values)
    assert len([e for e in _events(native) if e['event'] == 'start']) == 1
    native['auth'].write_text(json.dumps(_token('account-b')))
    assert _read(native, expected_account_id='account-a')['shared_login_issue'] == 'account_changed'
    native['auth'].unlink()
    assert _read(native)['shared_login_issue'] == 'codex_not_signed_in'


def test_only_native_codex_renews_expired_access(native):
    native['auth'].write_text(json.dumps(_token(expired=True)))
    assert _read(native)['shared_login_issue'] == 'codex_access_expired'
    assert _read(native, refresh=True)['access_token']
    assert [e['event'] for e in _events(native)].count('account/read') == 1
    _read(native, refresh=True)
    assert [e['event'] for e in _events(native)].count('account/read') == 1
    assert json.loads(native['auth'].read_text())['tokens']['refresh_token'] == 'never-copy-refresh'


@pytest.mark.parametrize('mode,issue', [('hang','codex_command_timeout'),
    ('flood','codex_command_invalid_response'),('exit','codex_command_unavailable'),
    ('keyring','credential_store_unsupported'),('restricted','login_method_restricted')])
def test_native_failure_is_bounded_and_does_not_borrow_stale_default(native, monkeypatch, mode, issue):
    native['mode'].write_text(mode)
    if mode == 'hang': monkeypatch.setattr(command, '_DEADLINE_SECONDS', 1.0)
    before=time.monotonic();snapshot = _read(native)
    assert snapshot == {'shared_login_issue': issue}
    assert time.monotonic()-before < 8
    assert 'never-copy-refresh' not in repr(snapshot)
    assert native['processes']
    for child in native['processes']:
        assert child.poll() is not None
        assert child.stdin.closed and child.stdout.closed


def test_renewal_failure_is_redacted_and_keeps_original_identity(native):
    native['auth'].write_text(json.dumps(_token(expired=True)))
    native['mode'].write_text('renew-fail')
    snapshot = _read(native, refresh=True)
    assert snapshot == {'shared_login_issue': 'codex_command_unavailable', 'account_id': 'account-a'}


def test_account_change_cannot_trigger_native_renewal(native):
    native['auth'].write_text(json.dumps(_token('account-b', expired=True)))
    assert _read(native, expected_account_id='account-a', refresh=True)['shared_login_issue'] == 'account_changed'
    assert 'account/read' not in [e['event'] for e in _events(native)]


def test_command_context_expiry_reuses_metadata_transport(native, monkeypatch):
    _read(native)
    monkeypatch.setattr(command, '_cached', (command._cached[0], 0, command._cached[2]))
    _read(native, refresh=True)
    assert [e['event'] for e in _events(native)].count('start') == 1
    assert [e['event'] for e in _events(native)].count('config/read') == 2
    assert 'access_token' not in repr(command._cached)


def test_explicit_existing_override_remains_authoritative(native):
    root = native['home'] / 'operator-home'; root.mkdir()
    auth = root / 'auth.json'; auth.write_text(json.dumps(_token('operator-account'))); auth.chmod(0o600)
    environment = {**native['env'], 'CODEX_HOME': str(root)}
    assert read_local_login(environment=environment)['account_id'] == 'operator-account'


def test_concurrent_expired_reads_join_one_native_renewal(native):
    native['auth'].write_text(json.dumps(_token(expired=True)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = list(pool.map(lambda _: _read(native, refresh=True), range(16)))
    assert all(s.get('access_token') for s in snapshots)
    assert [e['event'] for e in _events(native)].count('account/read') == 1


def test_changed_native_launch_source_never_renews_another_source(native):
    native['auth'].write_text(json.dumps(_token(expired=True)))
    _read(native)
    other = native['home'] / 'other-login'; other.mkdir()
    (other / 'auth.json').write_text(json.dumps(_token('account-b', expired=True)))
    (native['home'] / '.bashrc').write_text(
        f'export CODEX_HOME="{other}"\nexport PATH="{native["linked"].parent.parent / "bin"}:$PATH"\n')
    snapshot = _read(native, expected_account_id='account-a', refresh=True)
    assert snapshot['shared_login_issue'] == 'codex_source_changed'
    assert 'account/read' not in [e['event'] for e in _events(native)]


def test_native_selected_symlink_cannot_bypass_target_permissions(native):
    native['auth'].chmod(0o666)
    assert _read(native) == {'shared_login_issue': 'codex_credentials_unreadable'}


def test_reader_start_failure_rolls_back_the_owned_process(native, monkeypatch):
    from types import SimpleNamespace
    processes = []
    original = command.subprocess.Popen
    def launch(*args, **kwargs):
        process = original(*args, **kwargs); processes.append(process)
        return process
    class FailedReader:
        def __init__(self, **kwargs): pass
        def start(self): raise RuntimeError('synthetic thread admission failure')
    monkeypatch.setattr(command, 'threading', SimpleNamespace(
        Thread=FailedReader, Event=command.threading.Event))
    monkeypatch.setattr(command, 'subprocess', SimpleNamespace(Popen=launch,
        PIPE=command.subprocess.PIPE, DEVNULL=command.subprocess.DEVNULL,
        TimeoutExpired=command.subprocess.TimeoutExpired))
    assert _read(native) == {'shared_login_issue': 'codex_command_unavailable'}
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed and processes[0].stdout.closed


def test_request_recovery_bypasses_cached_transient_probe_failure(native):
    native['mode'].write_text('exit')
    assert _read(native)['shared_login_issue'] == 'codex_command_unavailable'
    native['mode'].write_text('')
    # Passive status retains its short failure cache; explicit recovery re-probes.
    assert _read(native)['shared_login_issue'] == 'codex_command_unavailable'
    assert _read(native, refresh=True)['access_token']
    assert [e['event'] for e in _events(native)].count('start') == 2


def test_recovery_does_not_bypass_cached_policy_failure(native):
    native['mode'].write_text('restricted')
    assert _read(native)['shared_login_issue'] == 'login_method_restricted'
    native['mode'].write_text('')
    assert _read(native, refresh=True)['shared_login_issue'] == 'login_method_restricted'
    assert [e['event'] for e in _events(native)].count('start') == 1


def test_healthy_file_read_does_not_wait_for_native_renewal_lock(native):
    assert _read(native)['access_token']
    assert command._lock.acquire(timeout=1)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            # A held renewal lock used to make this healthy read time out.
            assert pool.submit(_read, native).result(timeout=1)['account_id'] == 'account-a'
            native['auth'].unlink()
            assert pool.submit(_read, native).result(timeout=1) == {
                'shared_login_issue': 'codex_not_signed_in'}
    finally:
        command._lock.release()
    assert [e['event'] for e in _events(native)].count('start') == 1


def test_concurrent_recovery_joins_one_failed_probe(native, monkeypatch):
    import threading
    native['mode'].write_text('exit')
    assert _read(native)['shared_login_issue'] == 'codex_command_unavailable'
    # Admit both readers before allowing the first recovery to publish its
    # result. The second waits on the real flight lock, then joins that result.
    original_lock = command._lock
    arrived = threading.Barrier(2)
    class JoinedLock:
        def acquire(self, **kwargs):
            arrived.wait(timeout=2)
            return original_lock.acquire(**kwargs)
        def release(self):
            original_lock.release()
    with monkeypatch.context() as patch:
        patch.setattr(command, '_lock', JoinedLock())
        with ThreadPoolExecutor(max_workers=2) as pool:
            snapshots = list(pool.map(lambda _: _read(native, refresh=True), range(2)))
    assert all(value == {'shared_login_issue': 'codex_command_unavailable'} for value in snapshots)
    assert len(native['processes']) == 2  # Initial failure plus one shared recovery.
    assert all(child.poll() is not None for child in native['processes'])


def _expire_metadata(monkeypatch):
    monkeypatch.setattr(command, '_cached', (command._cached[0], 0, command._cached[2]))


def test_transient_reprobe_keeps_verified_source_and_reads_current_token(native, monkeypatch):
    assert _read(native)['account_id'] == 'account-a'
    _expire_metadata(monkeypatch)
    native['mode'].write_text('exit')
    assert _read(native, refresh=True)['access_token']
    native['auth'].write_text(json.dumps(_token('account-b')))
    assert _read(native, expected_account_id='account-a') == {'shared_login_issue': 'account_changed'}
    native['auth'].unlink()
    assert _read(native) == {'shared_login_issue': 'codex_not_signed_in'}
    assert len(native['processes']) == 1
    assert all(child.poll() is not None for child in native['processes'])
    assert 'access_token' not in repr(command._verified)
    assert 'never-copy-refresh' not in repr(command._verified)


def test_failed_probes_never_extend_absolute_metadata_age(native, monkeypatch):
    assert _read(native, refresh=True)['access_token']
    original_deadline = command._verified[1]
    _expire_metadata(monkeypatch)
    native['mode'].write_text('exit')
    assert _read(native, refresh=True)['access_token']
    assert command._verified[1] == original_deadline
    verified = command._verified
    monkeypatch.setattr(command, '_verified', (verified[0], 0, verified[2], verified[3]))
    assert _read(native) == {'shared_login_issue': 'codex_command_unavailable'}


@pytest.mark.parametrize('changed_file', ['shell', 'config', 'target_permissions'])
def test_local_source_changes_prevent_verified_metadata_reuse(native, monkeypatch, changed_file):
    assert _read(native, refresh=True)['access_token']
    if changed_file == 'shell':
        with (native['home'] / '.bashrc').open('a') as stream:
            stream.write('# changed source\n')
    elif changed_file == 'config':
        (native['root'] / 'config.toml').write_text('forced_login_method = "api"')
    else:
        (native['root'] / 'config.toml').chmod(0o666)
    native['mode'].write_text('exit')
    # Even still-fresh cache metadata is revoked by an observable source change.
    assert _read(native) == {'shared_login_issue': 'codex_command_unavailable'}


def test_native_policy_rejection_revokes_previous_success(native, monkeypatch):
    assert _read(native, refresh=True)['access_token']
    _expire_metadata(monkeypatch)
    native['mode'].write_text('restricted')
    assert _read(native, refresh=True) == {'shared_login_issue': 'login_method_restricted'}
    assert command._verified is None
    _expire_metadata(monkeypatch)
    native['mode'].write_text('exit')
    assert _read(native) == {'shared_login_issue': 'codex_command_unavailable'}


def test_expired_token_is_not_extended_by_metadata_grace(native, monkeypatch):
    assert _read(native, refresh=True)['access_token']
    _expire_metadata(monkeypatch)
    native['mode'].write_text('exit')
    native['auth'].write_text(json.dumps(_token(expired=True)))
    assert _read(native) == {
        'shared_login_issue': 'codex_access_expired', 'account_id': 'account-a'}
    assert _read(native, refresh=True) == {
        'shared_login_issue': 'codex_command_unavailable', 'account_id': 'account-a'}
    assert _read(native, refresh=True) == {
        'shared_login_issue': 'codex_command_unavailable', 'account_id': 'account-a'}


def test_timeout_diagnostics_name_native_phase_without_secret(native, monkeypatch, caplog):
    assert _read(native, refresh=True)['access_token']
    _expire_metadata(monkeypatch)
    native['mode'].write_text('hang')
    monkeypatch.setattr(command, '_DEADLINE_SECONDS', 0.2)
    assert _read(native, refresh=True)['access_token']
    assert 'phase=config' in caplog.text
    assert 'never-copy-refresh' not in caplog.text
    assert all(child.poll() is not None for child in native['processes'])


def test_expired_metadata_can_survive_lock_contention_within_absolute_age(native, monkeypatch):
    assert _read(native, refresh=True)['access_token']
    _expire_metadata(monkeypatch)
    monkeypatch.setattr(command, '_DEADLINE_SECONDS', 0.02)
    assert command._lock.acquire(timeout=1)
    try:
        assert _read(native)['access_token']
        native['auth'].unlink()
        assert _read(native) == {'shared_login_issue': 'codex_not_signed_in'}
    finally:
        command._lock.release()


def test_clear_context_discards_verified_grace(native):
    assert _read(native, refresh=True)['access_token']
    command.clear_command_context()
    native['mode'].write_text('exit')
    assert _read(native) == {'shared_login_issue': 'codex_command_unavailable'}


def test_periodic_revalidation_does_not_block_healthy_requests_and_is_singleflight(native, monkeypatch):
    import threading
    assert _read(native)['access_token']
    _expire_metadata(monkeypatch)
    started = threading.Event()
    release = threading.Event()
    original = command._exchange
    def blocked(*args, **kwargs):
        started.set()
        assert release.wait(timeout=3)
        return original(*args, **kwargs)
    monkeypatch.setattr(command, '_exchange', blocked)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            snapshots = list(pool.map(lambda _: _read(native), range(16)))
        assert all(value.get('access_token') for value in snapshots)
        assert started.wait(timeout=1)
        worker = command._metadata_probe
        assert worker is not None and worker.is_alive()
        native['auth'].unlink()
        assert _read(native) == {'shared_login_issue': 'codex_not_signed_in'}
    finally:
        release.set()
        command.clear_command_context()
    assert not worker.is_alive()
    assert command._verified is None and command._cached is None
    assert len(native['processes']) == 1
    assert all(child.poll() is not None for child in native['processes'])


def test_optional_metadata_worker_admission_failure_does_not_fail_healthy_read(native, monkeypatch):
    from types import SimpleNamespace
    assert _read(native)['access_token']
    _expire_metadata(monkeypatch)
    class FailedWorker:
        def __init__(self, **kwargs):
            raise RuntimeError('synthetic thread admission failure')
    monkeypatch.setattr(command, 'threading', SimpleNamespace(
        Thread=FailedWorker, Event=command.threading.Event))
    assert _read(native)['access_token']
    assert command._metadata_probe is None
    assert not command._lock.locked()
    assert len(native['processes']) == 1
