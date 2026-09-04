"""Unit contracts for the human-facing lifecycle CLI."""

from __future__ import annotations

import io
import json
import os
import shlex
import sys
from types import ModuleType, SimpleNamespace

import pytest

import serverctl


pytestmark = pytest.mark.unit


class _TTYBuffer(io.StringIO):
    def isatty(self):
        return True


def test_startup_progress_streams_worker_boot_stage_and_failure(tmp_path):
    worker_log = tmp_path / 'server-console.log'
    worker_log.write_text('', encoding='utf-8')
    output = _TTYBuffer()
    progress = serverctl._StartupProgress(worker_log, stream=output)

    progress.start()
    progress.tick({'pid': 41, 'running': False})
    assert progress.last_stage == 'Contacting lifecycle manager…'
    with worker_log.open('a', encoding='utf-8') as stream:
        stream.write(
            '\x1b[36m[boot +  7.5s]\x1b[0m Starting storage sidecar…\n'
            'RuntimeError: storage sidecar startup refused '
            '(database_unavailable): lease held\n')
    progress.tick({'pid': 42})
    progress.finish(ready=False)

    rendered = output.getvalue()
    assert 'Tofu startup [' in rendered
    assert 'Starting storage sidecar…' in rendered
    assert 'Tofu startup failed after' in rendered
    assert 'Startup error: RuntimeError: storage sidecar startup refused' in rendered


def test_startup_progress_uses_completed_phases_and_prints_durations(tmp_path):
    worker_log = tmp_path / 'server-console.log'
    worker_log.write_text('', encoding='utf-8')
    output = _TTYBuffer()
    progress = serverctl._StartupProgress(worker_log, stream=output)

    progress.start()
    with worker_log.open('a', encoding='utf-8') as stream:
        stream.write(
            '[boot +  0.1s] [startup phase 1/2] start | Frontend assets\n'
            '[boot +  1.3s] [startup phase 1/2] done | Frontend assets | 1.2s\n'
            '[boot +  1.3s] [startup phase 2/2] start | Database recovery\n')
    progress.tick({'running': True, 'pid': 41})
    progress.finish(ready=True)

    rendered = output.getvalue()
    assert 'Tofu startup [██' in rendered
    assert '1/2' in rendered
    assert 'Frontend assets' in rendered
    assert '1.2s' in rendered
    assert 'Database recovery' in rendered
    assert 'Startup stages:' in rendered
    assert 'Tofu startup ready after' in rendered


def test_cron_migration_is_idempotent_and_preserves_unrelated_lines(monkeypatch):
    old = (
        '15 2 * * * /usr/local/bin/backup\n'
        '@reboot /old/tofu_guard.sh --ensure # tofu_guard\n'
        '* * * * * old manager # tofu_manager\n'
    )
    applied = []

    def fake_run(cmd, **kwargs):
        if cmd == ['crontab', '-l']:
            return SimpleNamespace(stdout=old, stderr='', returncode=0)
        assert cmd == ['crontab', '-']
        applied.append(kwargs['input'])
        return SimpleNamespace(stdout='', stderr='', returncode=0)

    monkeypatch.setattr(serverctl.subprocess, 'run', fake_run)
    ok, error = serverctl._replace_guard_cron()
    assert ok is True and error == ''
    text = applied[0]
    assert '15 2 * * * /usr/local/bin/backup' in text
    assert 'tofu_guard' not in text
    assert text.count('# tofu_manager') == 2
    assert text.count('serverctl.py ensure') == 2


def test_cron_migration_surfaces_atomic_apply_failure(monkeypatch):
    def fake_run(cmd, **_kwargs):
        if cmd == ['crontab', '-l']:
            return SimpleNamespace(stdout='0 1 * * * keep-me\n', stderr='', returncode=0)
        return SimpleNamespace(stdout='', stderr='permission denied', returncode=1)

    monkeypatch.setattr(serverctl.subprocess, 'run', fake_run)
    ok, error = serverctl._replace_guard_cron()
    assert ok is False
    assert error == 'permission denied'


def test_install_leaves_legacy_recovery_untouched_when_cron_commit_fails(
        monkeypatch, capsys):
    monkeypatch.setattr(serverctl, 'ensure_manager', lambda: {'managerPid': 42})
    monkeypatch.setattr(
        serverctl, '_remote_status',
        lambda probe=False: {'observed': 'running', 'health': True})
    monkeypatch.setattr(
        serverctl, '_replace_guard_cron',
        lambda: (False, 'permission denied'))
    monkeypatch.setattr(
        serverctl.subprocess, 'run',
        lambda *_args, **_kwargs: pytest.fail(
            'legacy guard must not stop before cron replacement commits'))

    assert serverctl.cmd_install(SimpleNamespace()) == 1
    error = capsys.readouterr().err
    assert 'cron migration failed: permission denied' in error
    assert 'Legacy recovery was left unchanged' in error


def test_install_does_not_call_retired_guard_stop_after_cron_commit(
        monkeypatch, capsys):
    monkeypatch.setattr(serverctl, 'ensure_manager', lambda: {'managerPid': 42})
    monkeypatch.setattr(
        serverctl, '_remote_status',
        lambda probe=False: {'observed': 'running', 'health': True})
    monkeypatch.setattr(serverctl, '_replace_guard_cron', lambda: (True, ''))
    monkeypatch.setattr(serverctl, '_proc_lines', lambda _needle: [])
    monkeypatch.setattr(
        serverctl.subprocess, 'run',
        lambda *_args, **_kwargs: pytest.fail(
            'the retired guard --stop surface must not run after migration'))

    assert serverctl.cmd_install(SimpleNamespace()) == 0
    output = capsys.readouterr().out
    assert 'Tofu manager installed (PID 42)' in output
    assert 'current server was not restarted' in output


def test_forwarded_server_env_is_narrow_and_uses_project_dotenv(
        tmp_path, monkeypatch):
    (tmp_path / '.env').write_text(
        'PORT="15000"\nBIND_HOST=0.0.0.0\n'
        'TOFU_AGENT_WORKERS=12\nTOFU_AGENT_QUEUE_CAPACITY=96\n'
        'DATABASE_URL=file-secret\n',
        encoding='utf-8')
    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    for key in serverctl.SERVER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('PORT', '16000')
    monkeypatch.setenv('TOFU_PROCESS_RSS_RECYCLE_MB', '6144')
    monkeypatch.setenv('TOFU_AGENT_QUEUE_CAPACITY', '128')
    monkeypatch.setenv('DATABASE_URL', 'secret')
    assert serverctl._forwarded_server_env() == {
        'PORT': '16000', 'BIND_HOST': '0.0.0.0',
        'TOFU_PROCESS_RSS_RECYCLE_MB': '6144',
        'TOFU_AGENT_WORKERS': '12',
        'TOFU_AGENT_QUEUE_CAPACITY': '128'}


@pytest.mark.parametrize(
    ('server_args', 'server_env', 'expected'),
    [
        (['--port'], {}, '--port requires a value'),
        (['--port=70000'], {}, '--port must be an integer from 1 to 65535'),
        (['--port', '15000', '--port=16000'], {},
         '--port may be supplied only once'),
        ([], {'PORT': 'not-a-port'},
         'PORT must be an integer from 1 to 65535'),
        (['--workers', '2'], {}, '--workers must be 1'),
        (['--host'], {}, '--host requires a value'),
        (['--unknown'], {}, 'unsupported server option'),
        (['positional'], {}, 'positional server arguments are not supported'),
        (['--no-tls', '--no-tls'], {}, '--no-tls may be supplied only once'),
        (['--certfile', 'server.pem'], {},
         '--certfile and --keyfile must be configured together'),
        ([], {'TOFU_TLS': 'sometimes'}, 'unsupported TOFU_TLS'),
    ],
)
def test_forwarded_lifecycle_options_are_validated(
        server_args, server_env, expected):
    assert expected in serverctl._forwarded_server_options_error(
        server_args, server_env)

    assert serverctl._forwarded_server_options_error(
        ['--port', '16000', '--workers=1', '--no-tls'], {}) == ''
    assert serverctl._forwarded_server_options_error(
        ['--no-tls'], {'TOFU_TLS': 'sometimes'}) == ''


def test_explicit_tls_files_are_checked_before_start(tmp_path, monkeypatch):
    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    cert = tmp_path / 'cert.pem'
    key = tmp_path / 'key.pem'
    cert.write_text('certificate', encoding='utf-8')
    key.write_text('key', encoding='utf-8')

    assert serverctl._forwarded_server_options_error(
        ['--certfile', 'cert.pem', '--keyfile', 'key.pem'],
        {'TOFU_TLS': 'sometimes'}) == ''
    key.unlink()
    assert 'file does not exist' in serverctl._forwarded_server_options_error(
        ['--certfile', 'cert.pem', '--keyfile', 'key.pem'], {})


def test_worker_port_drift_probes_explicit_locked_worker_endpoint(monkeypatch):
    observed = {}

    def fake_probe(port, pid, *, preferred_scheme=''):
        observed.update(port=port, pid=pid, scheme=preferred_scheme)
        return {
            'health': True, 'liveness': True, 'ready': True,
            'scheme': 'http',
            'url': f'http://localhost:{port}', 'payloadPid': pid,
            'pidMatches': True,
        }

    monkeypatch.setattr(serverctl, '_probe_local_worker', fake_probe)
    monkeypatch.setattr(serverctl, 'listener_pids', lambda port: [42])
    drift = serverctl._worker_port_drift(
        {'port': 15599, 'scheme': 'http'},
        {'running': True, 'pid': 42,
         'cmdline': 'python server.py --no-tls --port 15000'})

    assert observed == {'port': 15000, 'pid': 42, 'scheme': 'http'}
    assert drift == {
        'managerPort': 15599,
        'workerDeclaredPort': 15000,
        'workerPid': 42,
        'listenerPids': [42],
        'health': True,
        'liveness': True,
        'ready': True,
        'scheme': 'http',
        'url': 'http://localhost:15000',
        'payloadPid': 42,
        'pidMatches': True,
    }
    assert serverctl._worker_port_drift(
        {'port': 15000},
        {'running': True, 'pid': 42,
         'cmdline': 'python server.py --port 15000'}) is None


def test_local_worker_probe_requires_health_pid_to_match_lock(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read(_limit=None):
            return b'{"ok":true,"pid":99}'

    monkeypatch.setattr(serverctl.urllib.request, 'urlopen', lambda *_a, **_k: Response())
    probe = serverctl._probe_local_worker(15000, 42, preferred_scheme='http')
    assert probe['health'] is False
    assert probe['pidMatches'] is False
    assert probe['url'] is None
    assert 'does not match' in probe['error']


def test_remote_status_safely_enriches_a_live_legacy_manager(monkeypatch):
    legacy = {
        'running': True,
        'observed': 'running',
        'health': True,
        'pid': 42,
        'port': 15000,
        'scheme': 'http',
    }
    direct = {
        'health': True,
        'liveness': True,
        'ready': False,
        'scheme': 'http',
        'livenessError': '',
        'readinessError': (
            'application readiness failed: lifecycle=starting, '
            'storage=restarting, HTTP 503'),
        'readinessState': 'starting',
        'storageState': 'restarting',
    }
    monkeypatch.setattr(serverctl, '_request', lambda *_a, **_k: dict(legacy))
    monkeypatch.setattr(
        serverctl, 'probe_application_readiness',
        lambda *_a, **_k: dict(direct))

    status = serverctl._remote_status(probe=True)

    assert status['legacyManagerProbe'] is True
    assert status['health'] is True
    assert status['liveness'] is True
    assert status['ready'] is False
    assert status['observed'] == 'degraded'
    assert status['storageState'] == 'restarting'
    assert 'storage=restarting' in status['lastError']
    assert serverctl._status_ready(status) is False


def test_invalid_start_option_never_contacts_manager(monkeypatch, capsys):
    monkeypatch.setattr(
        serverctl, '_post',
        lambda *_args, **_kwargs: pytest.fail('invalid options reached manager'))
    monkeypatch.delenv('PORT', raising=False)

    assert serverctl.managed_start(['--port=99999']) == 2
    error = capsys.readouterr().err
    assert 'Cannot start Tofu' in error
    assert 'server.py --help' in error


def test_invalid_project_dotenv_never_contacts_manager(tmp_path, monkeypatch, capsys):
    (tmp_path / '.env').write_text('PORT=not-a-port\n', encoding='utf-8')
    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    monkeypatch.setattr(
        serverctl, '_post',
        lambda *_args, **_kwargs: pytest.fail('invalid .env reached manager'))
    monkeypatch.delenv('PORT', raising=False)

    assert serverctl.managed_start([]) == 2
    error = capsys.readouterr().err
    assert "PORT must be an integer from 1 to 65535" in error
    assert 'not-a-port' in error


def test_managed_start_surfaces_healthy_port_drift_instead_of_false_outage(
        monkeypatch, capsys):
    status = {
        'running': True, 'observed': 'starting', 'health': False,
        'port': 15599, 'pid': 42, 'processStartedAt': 100.0,
    }
    monkeypatch.setenv('TOFU_STARTUP_PROGRESS', '0')
    monkeypatch.setattr(serverctl, '_post', lambda *_a, **_k: {'ok': True})
    monkeypatch.setattr(serverctl, '_wait_ready', lambda *_a, **_k: status)
    monkeypatch.setattr(
        serverctl, 'read_lock_status',
        lambda _project: {'running': True, 'pid': 42,
                          'cmdline': 'python server.py --port 15000'})
    monkeypatch.setattr(
        serverctl, '_worker_port_drift',
        lambda *_a: {
            'health': True, 'liveness': True, 'ready': True,
            'url': 'http://localhost:15000',
            'managerPort': 15599, 'workerDeclaredPort': 15000})

    assert serverctl.managed_start(wait=0) == 1
    error = capsys.readouterr().err
    assert 'ready at http://localhost:15000' in error
    assert 'probing port 15599' in error
    assert 'startup is stuck' not in error


def test_managed_start_refuses_false_success_when_live_port_ignores_request(
        monkeypatch, capsys):
    monkeypatch.setenv('TOFU_STARTUP_PROGRESS', '0')
    monkeypatch.setattr(
        serverctl, '_forwarded_server_env', lambda: {'PORT': '16000'})
    monkeypatch.setattr(
        serverctl, '_post',
        lambda *_a, **_k: {
            'ok': True, 'alreadyRunning': True,
            'message': 'already running; supplied server options were ignored',
        })
    monkeypatch.setattr(
        serverctl, '_wait_ready',
        lambda *_a, **_k: {
            'running': True, 'observed': 'running', 'health': True,
            'liveness': True, 'ready': True,
            'port': 15000, 'pid': 42, 'scheme': 'http',
        })

    assert serverctl.managed_start(wait=0) == 1
    error = capsys.readouterr().err
    assert 'already ready at http://localhost:15000' in error
    assert 'requested port 16000 was not applied' in error
    assert 'human-approved restart' in error
    assert 'serverctl.py restart' in error


def test_managed_start_reports_queued_storage_maintenance_without_waiting(
        monkeypatch, capsys):
    monkeypatch.setenv('TOFU_STARTUP_PROGRESS', '0')
    monkeypatch.setattr(serverctl, '_forwarded_server_env', lambda: {})
    monkeypatch.setattr(serverctl, '_post', lambda *_args, **_kwargs: {
        'ok': True,
        'waitingForMaintenance': True,
        'desired': 'running',
        'observed': 'maintenance',
        'storageLease': {
            'held': True,
            'kind': 'offline_maintenance',
            'label': 'SQLite deep clean',
            'pid': 4321,
            'ageSeconds': 120.0,
        },
        'lastError': (
            'SQLite deep clean (PID 4321) holds the storage lease; '
            'start is queued'),
    })
    monkeypatch.setattr(
        serverctl, '_wait_ready',
        lambda *_args, **_kwargs: pytest.fail(
            'queued maintenance must not consume the readiness timeout'))

    assert serverctl.managed_start(wait=180) == 0
    output = capsys.readouterr().out
    assert 'start is queued behind offline storage maintenance' in output
    assert 'start it automatically' in output
    assert 'SQLite deep clean (PID 4321); active 2.0m' in output


def test_manager_launcher_timeout_outlives_shell_watchdog_budget(monkeypatch):
    health = iter([None, {'ok': True}])
    monkeypatch.setattr(serverctl, '_manager_health', lambda: next(health))
    monkeypatch.setattr(
        serverctl, 'supervisor_generation_matches', lambda *_args: True)
    observed = {}

    def fake_run(_cmd, **kwargs):
        observed['timeout'] = kwargs['timeout']
        return SimpleNamespace(stdout='', stderr='', returncode=0)

    monkeypatch.setattr(serverctl.subprocess, 'run', fake_run)

    assert serverctl.ensure_manager(timeout=8.0) == {'ok': True}
    assert observed['timeout'] >= 25.0


def test_live_stale_manager_is_refreshed_before_lifecycle_use(monkeypatch):
    stale = {'ok': True, 'managerPid': 11}
    current = {'ok': True, 'managerPid': 12}
    health = iter([stale, current])
    refreshed = []
    monkeypatch.setattr(serverctl, '_manager_health', lambda: next(health))
    monkeypatch.setattr(
        serverctl,
        'supervisor_generation_matches',
        lambda value, _project: value is current,
    )
    monkeypatch.setattr(
        serverctl,
        'refresh_supervisor',
        lambda project, **kwargs: refreshed.append((project, kwargs)) or {
            'ok': True,
        },
    )

    assert serverctl.ensure_manager(timeout=8.0) is current
    assert refreshed and refreshed[0][0] == serverctl.PROJECT


def test_doctor_reports_manager_and_live_worker_budget_drift():
    findings = serverctl._doctor_findings(
        {
            'observed': 'running',
            'desired': 'running',
            'health': True,
            'ready': True,
            'workerRssGuardEnabled': True,
        },
        guard_loops=[],
        legacy_cron=[],
        manager_cron=['installed'],
        memory={'usagePct': 10.0},
        snapshot={'required': False},
        manager_generation={
            'current': False,
            'loadedFingerprint': 'old-generation',
        },
        worker_budget_drift={
            'stale': True,
            'mismatches': {
                'TOFU_AGENT_WORKERS': {'observed': '4', 'expected': '48'},
            },
        },
    )
    by_code = {finding['code']: finding for finding in findings}
    assert by_code['supervisor_source_stale']['severity'] == 'warning'
    assert by_code['worker_resource_budget_stale']['severity'] == 'warning'
    assert 'TOFU_AGENT_WORKERS=4 (next 48)' in \
        by_code['worker_resource_budget_stale']['message']


def test_stale_source_frontend_is_rebuilt_once_before_lifecycle_action(
        tmp_path, monkeypatch):
    validations = iter([
        RuntimeError('stale i18n'), RuntimeError('stale i18n'), None])
    builds = []

    def validate():
        error = next(validations)
        if error:
            raise error

    monkeypatch.setattr(serverctl, '_lifecycle_owns_frontend', lambda: True)
    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    monkeypatch.setattr(serverctl, '_validate_frontend_artifact', validate)
    monkeypatch.setattr(
        serverctl, '_source_frontend_build_command',
        lambda: ['/env/bin/node', '/project/scripts/build_frontend.mjs'])
    monkeypatch.setattr(
        serverctl.subprocess, 'run',
        lambda command, **kwargs: builds.append((command, kwargs))
        or SimpleNamespace(returncode=0))

    assert serverctl._repair_source_frontend_artifact('test startup') == ''
    assert len(builds) == 1
    assert builds[0][0][0] == '/env/bin/node'
    assert builds[0][1]['timeout'] == 600.0


def test_other_checkout_frontend_validation_includes_authoring_freshness(
        tmp_path, monkeypatch):
    verifier = tmp_path / 'scripts' / 'verify_frontend_dist.py'
    verifier.parent.mkdir(parents=True)
    verifier.write_text('# verifier\n', encoding='utf-8')
    observed = {}

    def fake_run(command, **kwargs):
        observed['command'] = command
        observed['kwargs'] = kwargs
        return SimpleNamespace(returncode=0, stdout='validated')

    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    monkeypatch.setattr(serverctl.subprocess, 'run', fake_run)

    serverctl._validate_frontend_artifact()

    assert observed['command'][-1] == '--authoring-freshness'
    assert observed['kwargs']['cwd'] == tmp_path.resolve()


def test_frontend_repair_revalidates_after_cross_process_lock(
        tmp_path, monkeypatch):
    validations = iter([RuntimeError('stale i18n'), None])

    def validate():
        error = next(validations)
        if error:
            raise error

    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    monkeypatch.setattr(serverctl, '_lifecycle_owns_frontend', lambda: True)
    monkeypatch.setattr(serverctl, '_validate_frontend_artifact', validate)
    monkeypatch.setattr(
        serverctl, '_source_frontend_build_command',
        lambda: ['/env/bin/node', '/project/scripts/build_frontend.mjs'])
    monkeypatch.setattr(
        serverctl.subprocess, 'run',
        lambda *_args, **_kwargs: pytest.fail(
            'a waiting lifecycle process must reuse the published graph'))

    assert serverctl._repair_source_frontend_artifact('test startup') == ''


def test_invalid_release_without_node_is_refused_before_lifecycle_change(
        monkeypatch):
    monkeypatch.setattr(serverctl, '_lifecycle_owns_frontend', lambda: True)
    monkeypatch.setattr(
        serverctl, '_validate_frontend_artifact',
        lambda: (_ for _ in ()).throw(RuntimeError('manifest missing')))
    monkeypatch.setattr(
        serverctl, '_source_frontend_build_command', lambda: None)
    monkeypatch.setattr(
        serverctl.subprocess, 'run',
        lambda *_args, **_kwargs: pytest.fail(
            'release lifecycle must not acquire a Node build dependency'))

    error = serverctl._repair_source_frontend_artifact('release startup')

    assert 'no local Vite builder is available' in error
    assert 'manifest missing' in error


def test_prepare_frontend_command_reports_preflight_failure(
        monkeypatch, capsys):
    monkeypatch.setattr(
        serverctl, 'prepare_source_frontend_artifact',
        lambda _operation: 'authoring digest mismatch',
    )

    exit_code = serverctl.main([
        'prepare-frontend', '--operation', 'manager recovery'])

    assert exit_code == 1
    assert 'authoring digest mismatch' in capsys.readouterr().err


def test_non_frontend_role_skips_source_artifact_repair(monkeypatch):
    monkeypatch.setattr(serverctl, '_lifecycle_owns_frontend', lambda: False)
    monkeypatch.setattr(
        serverctl, '_validate_frontend_artifact',
        lambda: pytest.fail('worker-only lifecycle validated frontend assets'))

    assert serverctl._repair_source_frontend_artifact('worker startup') == ''


def test_cold_manager_launch_stops_when_frontend_repair_fails(monkeypatch):
    monkeypatch.setattr(serverctl, '_manager_health', lambda: None)
    monkeypatch.setattr(
        serverctl, '_repair_source_frontend_artifact',
        lambda _operation: 'frontend build failed')
    monkeypatch.setattr(
        serverctl.subprocess, 'run',
        lambda *_args, **_kwargs: pytest.fail(
            'manager must not launch with a known failed source repair'))

    with pytest.raises(serverctl.ManagerUnavailable, match='frontend build failed'):
        serverctl.ensure_manager()


def test_restart_aborts_before_manager_post_when_frontend_repair_fails(
        monkeypatch, capsys):
    monkeypatch.setattr(serverctl, '_remote_status', lambda: {'running': False})
    monkeypatch.setattr(
        serverctl, 'read_lock_status', lambda _project: {'running': False})
    monkeypatch.setattr(
        serverctl, '_repair_source_frontend_artifact',
        lambda _operation: 'frontend build failed')
    monkeypatch.setattr(
        serverctl, '_post',
        lambda *_args, **_kwargs: pytest.fail(
            'failed source repair must not restart the worker'))

    args = serverctl.build_parser().parse_args(['restart'])
    assert serverctl.cmd_restart(args) == 1
    assert 'Cannot restart Tofu: frontend build failed' in capsys.readouterr().err


def test_restart_checks_raw_lock_when_manager_is_offline(monkeypatch):
    monkeypatch.setattr(serverctl, '_remote_status', lambda: None)
    monkeypatch.setattr(serverctl, 'read_lock_status', lambda _p: {'running': True})
    approved = []
    monkeypatch.setattr(serverctl, '_approve_restart_if_needed',
                        lambda _args: approved.append(True) or False)
    called = []
    monkeypatch.setattr(serverctl, '_post', lambda *_a, **_k: called.append(True))
    args = serverctl.build_parser().parse_args(['restart'])
    assert serverctl.cmd_restart(args) == 3
    assert approved == [True]
    assert called == []


def test_stop_checks_raw_lock_and_requires_shutdown_approval(monkeypatch):
    monkeypatch.setattr(serverctl, '_remote_status', lambda: None)
    monkeypatch.setattr(
        serverctl, 'read_lock_status', lambda _p: {'running': True})
    approved = []
    monkeypatch.setattr(
        serverctl, '_approve_stop_if_needed',
        lambda _args: approved.append(True) or False)
    called = []
    monkeypatch.setattr(
        serverctl, '_post', lambda *_a, **_k: called.append(True))
    args = serverctl.build_parser().parse_args(['stop'])

    assert serverctl.cmd_stop(args) == 3
    assert approved == [True]
    assert called == []


def test_stop_is_idempotent_without_approval_when_no_worker_is_live(monkeypatch):
    monkeypatch.setattr(
        serverctl, '_remote_status', lambda: {'running': False})
    monkeypatch.setattr(
        serverctl, 'read_lock_status', lambda _p: {'running': False})
    monkeypatch.setattr(
        serverctl, '_approve_stop_if_needed',
        lambda _args: pytest.fail('stopped service must not ask for approval'))
    monkeypatch.setattr(
        serverctl, '_post', lambda *_a, **_k: {'ok': True, 'message': 'stopped'})
    args = serverctl.build_parser().parse_args(['stop'])

    assert serverctl.cmd_stop(args) == 0


def test_noninteractive_lifecycle_gate_creates_one_reusable_pending_request(
        monkeypatch, capsys):
    fake = ModuleType('lib.lifecycle_approval')
    created = []
    fake.consume_any = lambda action: (False, 'no-approved-token', None)
    fake.list_records = lambda **_kwargs: []
    fake.create_request = lambda action, origin: (
        created.append((action, origin)) or {'id': 'request-12345678'})
    monkeypatch.setitem(sys.modules, 'lib.lifecycle_approval', fake)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)

    args = SimpleNamespace(yes=True)
    assert serverctl._approve_lifecycle_if_needed(
        args, action='shutdown', prompt='unused') is False

    assert created[0][0] == 'shutdown'
    assert created[0][1]['source'] == 'serverctl'
    error = capsys.readouterr().err
    assert 'requires human approval' in error
    assert 'request request-' in error
    assert 'then re-run this command' in error


def test_cgroup_memory_snapshot_reports_v1_oom_and_zero_swap(monkeypatch):
    values = {
        serverctl._CGROUP_V1_USAGE: str(180 * (1 << 30)),
        serverctl._CGROUP_V1_LIMIT: str(220 * (1 << 30)),
        serverctl._CGROUP_V1_MEMSW_LIMIT: str(220 * (1 << 30)),
        serverctl._CGROUP_V1_OOM: 'oom_kill_disable 0\noom_kill 2\n',
        serverctl._CGROUP_V1_FAILCNT: '7',
    }
    monkeypatch.setattr(
        serverctl, '_read_first_text',
        lambda paths: next((values[path] for path in paths if path in values), None))

    snapshot = serverctl.cgroup_memory_snapshot()

    assert snapshot['usagePct'] == 81.8
    assert snapshot['swapLimitBytes'] == 0
    assert snapshot['oomKills'] == 2
    assert snapshot['failCount'] == 7


def test_cgroup_memory_snapshot_preserves_v2_zero_swap_and_counters(monkeypatch):
    values = {
        serverctl._CGROUP_V2_USAGE: str(3 * (1 << 30)),
        serverctl._CGROUP_V2_LIMIT: str(8 * (1 << 30)),
        serverctl._CGROUP_V2_SWAP_LIMIT: '0',
        serverctl._CGROUP_V2_EVENTS: 'oom 0\noom_kill 0\n',
        serverctl._CGROUP_V1_FAILCNT: '0',
    }
    monkeypatch.setattr(
        serverctl, '_read_first_text',
        lambda paths: next((values[path] for path in paths if path in values), None))

    snapshot = serverctl.cgroup_memory_snapshot()

    assert snapshot['swapLimitBytes'] == 0
    assert snapshot['oomKills'] == 0
    assert snapshot['failCount'] == 0


def test_recovery_cron_classification_separates_manager_from_legacy_guard():
    legacy, manager = serverctl._classify_recovery_cron(
        '# disabled tofu_guard\n'
        '@reboot cd /srv/tofu && python serverctl.py ensure # tofu_manager\n'
        '* * * * * /srv/tofu/deploy/tofu_guard.sh --ensure # tofu_guard\n'
        '0 2 * * * /usr/local/bin/backup\n')

    assert len(legacy) == 1
    assert len(manager) == 1


def test_status_exit_code_is_monitoring_safe(monkeypatch, capsys):
    args = serverctl.build_parser().parse_args(['status'])
    monkeypatch.setattr(
        serverctl, '_remote_status',
        lambda probe=False: {'observed': 'degraded', 'restartCount': 2})
    assert serverctl.cmd_status(args) == 1
    assert 'Server  : degraded' in capsys.readouterr().out

    monkeypatch.setattr(
        serverctl, '_remote_status',
        lambda probe=False: {'observed': 'running', 'restartCount': 2,
                             'workerFailureCount': 1,
                             'plannedExitCount': 3,
                             'recentFailureCount': 1,
                             'failureWindowSeconds': 120,
                             'maxFailures': 5,
                             'lastRecoverySeconds': 2.5,
                             'workerRssGuardEnabled': True,
                             'workerRssBytes': 2 * (1 << 30),
                             'workerRssRecycleBytes': 8 * (1 << 30)})
    assert serverctl.cmd_status(args) == 0
    output = capsys.readouterr().out
    assert 'recent=1/5 in 120s' in output
    assert 'failures=1; planned=3' in output
    assert 'Last RTO: 2.500s' in output
    assert 'RSS guard: 2.00 GiB / 8.00 GiB hard ceiling' in output


def test_sqlite_snapshot_status_reports_fresh_external_snapshot(
        tmp_path, monkeypatch):
    project = tmp_path / 'project'
    data = project / 'data'
    external = tmp_path / 'backup-mount'
    data.mkdir(parents=True)
    external.mkdir()
    (data / 'tofu.db').write_bytes(b'db')
    snapshot = external / 'storage-sqlite-20260813T020000Z-test.sqlite3'
    snapshot.write_bytes(b'snapshot')
    snapshot.with_name(snapshot.name + '.manifest.json').write_text(json.dumps({
        'format': 'tofu.storage-backup.v1',
        'backend': 'sqlite',
        'artifact': snapshot.name,
        'bytes': len(b'snapshot'),
        'sha256': 'a' * 64,
        'integrity': 'ok',
    }), encoding='utf-8')
    os.utime(snapshot, (1000.0, 1000.0))
    (project / '.env').write_text(
        f'TOFU_SQLITE_SNAPSHOT_DIR={external}\n'
        'TOFU_SQLITE_SNAPSHOT_MAX_AGE_HOURS=26\n')
    monkeypatch.delenv('TOFU_SQLITE_SNAPSHOT_DIR', raising=False)
    monkeypatch.delenv('TOFU_SQLITE_SNAPSHOT_MAX_AGE_HOURS', raising=False)

    status = serverctl.sqlite_snapshot_status(str(project), now=1000.0 + 3600)

    assert status['required'] is True
    assert status['destinationConfigured'] is True
    assert status['fresh'] is True
    assert status['latestAgeHours'] == 1.0
    assert status['latestPath'] == str(snapshot)


def test_sqlite_snapshot_status_uses_sidecar_authority_and_inventories_legacy(
        tmp_path):
    project = tmp_path / 'project'
    data = project / 'data'
    backups = data / 'backups'
    legacy = data / 'db_snapshots'
    backups.mkdir(parents=True)
    legacy.mkdir()
    (data / 'tofu.db').write_bytes(b'db')
    canonical = backups / 'storage-sqlite-20260813T020000Z-test.sqlite3'
    canonical.write_bytes(b'canonical')
    canonical.with_name(canonical.name + '.manifest.json').write_text(
        json.dumps({
            'format': 'tofu.storage-backup.v1',
            'backend': 'sqlite',
            'artifact': canonical.name,
            'bytes': len(b'canonical'),
            'sha256': 'b' * 64,
            'integrity': 'ok',
        }),
        encoding='utf-8',
    )
    published = legacy / 'tofu-20260812_020000-old.sqlite3'
    temporary = legacy / '.tofu-20260813_020000.sqlite3.tmp-dead'
    published.write_bytes(b'old-copy')
    temporary.write_bytes(b'partial')
    os.utime(canonical, (1000.0, 1000.0))

    status = serverctl.sqlite_snapshot_status(
        str(project), now=1000.0 + 3600)

    assert status['snapshotDir'] == str(backups)
    assert status['snapshotCount'] == 1
    assert status['latestPath'] == str(canonical)
    assert status['fresh'] is True
    assert status['legacy'] == {
        'publishedCount': 1,
        'publishedBytes': len(b'old-copy'),
        'publishedAllocatedBytes': published.stat().st_blocks * 512,
        'temporaryCount': 1,
        'temporaryBytes': len(b'partial'),
        'temporaryAllocatedBytes': temporary.stat().st_blocks * 512,
        'totalBytes': len(b'old-copy') + len(b'partial'),
        'totalAllocatedBytes': (
            published.stat().st_blocks + temporary.stat().st_blocks) * 512,
        'allocatedBytesAvailable': True,
        'scanTruncated': False,
    }


def test_sqlite_snapshot_status_marks_missing_required_backup(tmp_path):
    project = tmp_path / 'project'
    data = project / 'data'
    data.mkdir(parents=True)
    (data / 'tofu.db').write_bytes(b'db')

    status = serverctl.sqlite_snapshot_status(str(project), now=1000.0)

    assert status['required'] is True
    assert status['destinationConfigured'] is False
    assert status['fresh'] is False
    assert status['latestPath'] is None


def test_sqlite_snapshot_freshness_uses_manifest_recovery_point(tmp_path):
    project = tmp_path / 'project'
    backups = project / 'data' / 'backups'
    backups.mkdir(parents=True)
    (project / 'data' / 'tofu.db').write_bytes(b'db')
    snapshot = backups / 'storage-sqlite-recovered-late.sqlite3'
    snapshot.write_bytes(b'snapshot')
    snapshot.with_name(snapshot.name + '.manifest.json').write_text(
        json.dumps({
            'format': 'tofu.storage-backup.v1',
            'backend': 'sqlite',
            'artifact': snapshot.name,
            'bytes': len(b'snapshot'),
            'sha256': 'c' * 64,
            'integrity': 'ok',
            'recovery_point_at': '1970-01-01T00:16:40+00:00',
        }),
        encoding='utf-8',
    )
    os.utime(snapshot, (1000.0 + 10 * 3600, 1000.0 + 10 * 3600))

    status = serverctl.sqlite_snapshot_status(
        str(project), now=1000.0 + 30 * 3600)

    assert status['fresh'] is False
    assert status['latestAgeHours'] == 30.0
    assert status['latestMtime'] == 1000.0 + 10 * 3600
    assert status['latestRecoveryPoint'] == 1000.0
    assert status['latestRecoveryPointSource'] == 'manifest'


def test_legacy_snapshot_inventory_distinguishes_sparse_allocation(tmp_path):
    data = tmp_path / 'data'
    legacy = data / 'db_snapshots'
    legacy.mkdir(parents=True)
    temporary = legacy / '.tofu-20260820_020000.sqlite3.tmp-dead'
    with temporary.open('wb') as stream:
        stream.truncate(64 * 1024 * 1024)

    inventory = serverctl._legacy_snapshot_inventory(data)

    assert inventory['temporaryBytes'] == 64 * 1024 * 1024
    assert inventory['temporaryAllocatedBytes'] == (
        temporary.stat().st_blocks * 512)
    assert inventory['totalAllocatedBytes'] <= inventory['totalBytes']
    assert inventory['allocatedBytesAvailable'] is True


def test_sqlite_snapshot_status_is_not_required_in_distributed_mode(
        tmp_path, monkeypatch):
    project = tmp_path / 'project'
    data = project / 'data'
    data.mkdir(parents=True)
    (data / 'tofu.db').write_bytes(b'leftover personal database')
    (project / '.env').write_text(
        'TOFU_DEPLOYMENT_MODE=distributed\n', encoding='utf-8')
    monkeypatch.delenv('TOFU_DEPLOYMENT_MODE', raising=False)

    status = serverctl.sqlite_snapshot_status(str(project), now=1000.0)

    assert status['required'] is False


def test_offline_port_diagnostics_use_project_env_and_preserve_invalid_value(
        tmp_path, monkeypatch):
    (tmp_path / '.env').write_text(
        'PORT="16400"\n', encoding='utf-8')
    monkeypatch.delenv('PORT', raising=False)
    assert serverctl._configured_port_snapshot(str(tmp_path)) == {
        'source': 'project .env',
        'raw': '16400',
        'valid': True,
        'port': 16400,
    }

    monkeypatch.setenv('PORT', 'invalid')
    invalid = serverctl._configured_port_snapshot(str(tmp_path))
    assert invalid['valid'] is False
    assert invalid['raw'] == 'invalid'
    assert invalid['port'] == 15000

    findings = serverctl._doctor_findings(
        None, guard_loops=[], legacy_cron=[], manager_cron=[],
        memory={'usagePct': 10.0},
        snapshot={'required': False, 'fresh': False,
                  'destinationConfigured': False},
        port_config=invalid)
    assert findings[0]['code'] == 'port_config_invalid'
    assert findings[0]['severity'] == 'error'


def test_doctor_reports_allocated_and_logical_legacy_footprints():
    findings = serverctl._doctor_findings(
        None,
        guard_loops=[],
        legacy_cron=[],
        manager_cron=[],
        memory={'usagePct': 10.0},
        snapshot={
            'required': False,
            'fresh': False,
            'destinationConfigured': False,
            'legacy': {
                'publishedCount': 2,
                'temporaryCount': 1,
                'totalBytes': 12 << 30,
                'totalAllocatedBytes': 8 << 30,
                'allocatedBytesAvailable': True,
            },
        },
    )

    legacy = next(
        item for item in findings
        if item['code'] == 'legacy_sqlite_snapshot_artifacts')
    assert '8.0 GiB allocated / 12.0 GiB logical' in legacy['message']
    assert '(2 published, 1 interrupted)' in legacy['message']


def test_doctor_preserves_unreadable_dotenv_as_actionable_finding(
        tmp_path, monkeypatch):
    (tmp_path / '.env').write_bytes(b'PORT=15000\xff\n')
    monkeypatch.delenv('PORT', raising=False)

    config = serverctl._configured_port_snapshot(str(tmp_path))
    assert config['valid'] is False
    assert config['port'] == 15000
    assert 'valid UTF-8' in config['error']

    stopped = serverctl._doctor_findings(
        None, guard_loops=[], legacy_cron=[], manager_cron=[],
        memory={'usagePct': 10.0},
        snapshot={'required': False, 'fresh': False,
                  'destinationConfigured': False},
        port_config=config)
    assert stopped[0]['code'] == 'dotenv_unreadable'
    assert stopped[0]['severity'] == 'error'

    running = serverctl._doctor_findings(
        {'running': True, 'observed': 'running', 'desired': 'running',
         'health': True, 'ready': True},
        guard_loops=[], legacy_cron=[], manager_cron=['installed'],
        memory={'usagePct': 10.0},
        snapshot={'required': False, 'fresh': False,
                  'destinationConfigured': False},
        port_config=config)
    assert running[0]['code'] == 'dotenv_unreadable'
    assert running[0]['severity'] == 'warning'


def test_doctor_surfaces_invalid_next_start_listener_configuration():
    base = dict(
        guard_loops=[], legacy_cron=[], manager_cron=['installed'],
        memory={'usagePct': 10.0},
        snapshot={'required': False, 'fresh': False,
                  'destinationConfigured': False},
        startup_config={
            'valid': False,
            'error': "unsupported TOFU_TLS='sometimes'",
        },
    )
    stopped = serverctl._doctor_findings(None, **base)
    finding = next(
        item for item in stopped if item['code'] == 'startup_config_invalid')
    assert finding['severity'] == 'error'
    assert "TOFU_TLS='sometimes'" in finding['message']

    running = serverctl._doctor_findings(
        {'running': True, 'observed': 'running', 'desired': 'running',
         'health': True, 'ready': True}, **base)
    finding = next(
        item for item in running if item['code'] == 'startup_config_invalid')
    assert finding['severity'] == 'warning'


def test_doctor_explains_project_port_waiting_for_restart():
    base = dict(
        guard_loops=[], legacy_cron=[], manager_cron=['installed'],
        memory={'usagePct': 10.0},
        snapshot={'required': False, 'fresh': False,
                  'destinationConfigured': False},
        port_config={'source': 'project .env', 'raw': '16000',
                     'valid': True, 'port': 16000},
    )
    status = {
        'running': True, 'observed': 'running', 'desired': 'running',
        'health': True, 'ready': True,
        'port': 15000, 'serverArgs': [],
    }

    findings = serverctl._doctor_findings(status, **base)
    pending = next(
        item for item in findings
        if item['code'] == 'configured_port_not_applied')
    assert pending['severity'] == 'warning'
    assert 'configured port 16000' in pending['message']
    assert 'still use 15000' in pending['message']
    assert 'serverctl.py restart' in pending['command']

    status['serverArgs'] = ['--port', '15000']
    assert not any(
        item['code'] == 'configured_port_not_applied'
        for item in serverctl._doctor_findings(status, **base))


def test_doctor_classifies_runtime_failures_separately_from_hardening_gaps():
    memory = {'usagePct': 20.0}
    snapshot = {
        'required': True,
        'fresh': True,
        'destinationConfigured': False,
    }
    running = serverctl._doctor_findings(
        {'running': True, 'observed': 'running', 'desired': 'running',
         'health': True, 'ready': True, 'workerRssGuardEnabled': True},
        guard_loops=[], legacy_cron=[], manager_cron=['installed'],
        memory=memory, snapshot=snapshot)
    assert [(item['code'], item['severity']) for item in running] == [
        ('sqlite_backup_same_failure_domain', 'warning')]

    degraded = serverctl._doctor_findings(
        {'observed': 'degraded', 'desired': 'running', 'health': False,
         'lastError': 'HTTP health failed', 'workerRssGuardEnabled': True},
        guard_loops=[], legacy_cron=[], manager_cron=['installed'],
        memory=memory, snapshot={**snapshot, 'destinationConfigured': True})
    assert degraded[0]['code'] == 'worker_degraded'
    assert degraded[0]['severity'] == 'error'
    assert 'logs -n 200' in degraded[0]['command']

    stuck = serverctl._doctor_findings(
        {'observed': 'starting', 'desired': 'running', 'health': False,
         'processStartedAt': 100.0, 'lastError': 'HTTP health failed',
         'workerRssGuardEnabled': True},
        guard_loops=[], legacy_cron=[], manager_cron=['installed'],
        memory=memory, snapshot={**snapshot, 'destinationConfigured': True},
        now=500.0)
    assert stuck[0]['code'] == 'worker_startup_stuck'
    assert stuck[0]['severity'] == 'error'
    assert '6.7 minutes' in stuck[0]['message']

    drift = serverctl._doctor_findings(
        {'observed': 'starting', 'desired': 'running', 'health': False,
         'processStartedAt': 100.0, 'lastError': 'HTTP health failed',
         'workerRssGuardEnabled': True},
        guard_loops=[], legacy_cron=[], manager_cron=['installed'],
        memory=memory, snapshot={**snapshot, 'destinationConfigured': True},
        port_drift={
            'managerPort': 15599, 'workerDeclaredPort': 15000,
            'workerPid': 42, 'health': True, 'liveness': True, 'ready': True,
            'url': 'http://localhost:15000'},
        now=500.0)
    assert drift[0]['code'] == 'worker_port_drift'
    assert drift[0]['severity'] == 'error'
    assert 'ready at http://localhost:15000' in drift[0]['message']
    assert '--port 15000' in drift[0]['command']
    assert not any(item['code'] == 'worker_startup_stuck' for item in drift)


def test_doctor_json_warning_is_healthy_but_not_ready(monkeypatch, capsys):
    status = {
        'observed': 'starting', 'desired': 'running', 'health': False,
        'lastError': 'booting storage', 'workerRssGuardEnabled': True,
        'managerPid': 77,
    }
    monkeypatch.setattr(serverctl, '_remote_status', lambda probe=False: status)
    monkeypatch.setattr(
        serverctl, '_forwarded_server_env',
        lambda: {'TOFU_MAX_INFLIGHT_TASKS': '7'})
    monkeypatch.setattr(serverctl, 'read_lock_status', lambda _project: {})
    monkeypatch.setattr(serverctl, 'listener_pids', lambda _port: [])
    monkeypatch.setattr(
        serverctl, '_proc_lines',
        lambda pattern: [
            '77 1 1 now S python /unrelated-looking/supervisor.py',
            '88 1 1 now S python /another/project/supervisor.py',
        ] if pattern == 'supervisor.py' else [])
    monkeypatch.setattr(
        serverctl, 'cgroup_memory_snapshot',
        lambda _pid=None: {'usagePct': 20.0, 'usageBytes': 1,
                           'limitBytes': 10, 'swapLimitBytes': 0,
                           'oomKills': 0, 'workerRssBytes': 1})
    monkeypatch.setattr(
        serverctl, 'sqlite_snapshot_status',
        lambda _project: {'required': False, 'fresh': False,
                          'destinationConfigured': False, 'latestPath': None})
    resource_budget = {
        'deployment_mode': 'personal', 'adaptive': True,
        'probe': {'effective_cpu_count': 4},
        'defaults': {'TOFU_MAX_INFLIGHT_TASKS': 2},
        'overrides': {'TOFU_MAX_INFLIGHT_TASKS': '7'},
    }
    observed_resource_environment = {}

    def fake_resource_budget_manifest(environment):
        observed_resource_environment.update(environment)
        return resource_budget

    monkeypatch.setattr(
        serverctl, 'resource_budget_manifest', fake_resource_budget_manifest)
    monkeypatch.setattr(
        serverctl.subprocess, 'run',
        lambda *args, **kwargs: SimpleNamespace(
            stdout='@reboot python serverctl.py ensure # tofu_manager\n'))

    args = serverctl.build_parser().parse_args(['doctor', '--json'])
    assert serverctl.cmd_doctor(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['healthy'] is True
    assert report['ready'] is False
    assert report['errors'] == []
    assert report['resourceBudget'] == resource_budget
    assert observed_resource_environment['TOFU_MAX_INFLIGHT_TASKS'] == '7'
    assert report['supervisorProcesses'] == [
        '77 1 1 now S python /unrelated-looking/supervisor.py']
    assert report['findings'][0]['code'] == 'worker_starting'
    assert report['findings'][0]['severity'] == 'warning'


def test_status_json_names_command_success_and_runtime_readiness(monkeypatch, capsys):
    monkeypatch.setattr(
        serverctl, '_remote_status',
        lambda probe=False: {'ok': True, 'observed': 'starting',
                             'desired': 'running', 'health': False})
    args = serverctl.build_parser().parse_args(['status', '--json'])
    assert serverctl.cmd_status(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['commandOk'] is True
    assert payload['managerOnline'] is True
    assert payload['ready'] is False
    assert payload['applicationReachable'] is False
    assert payload['applicationUrl'] is None
    assert payload['startupStuck'] is False


def test_status_marks_old_starting_worker_stuck_and_returns_nonzero(
        monkeypatch, capsys):
    monkeypatch.setattr(
        serverctl, '_remote_status',
        lambda probe=False: {
            'running': True, 'observed': 'starting', 'health': False,
            'processStartedAt': 100.0,
        })
    monkeypatch.setattr(serverctl.time, 'time', lambda: 500.0)

    args = serverctl.build_parser().parse_args(['status', '--json'])
    assert serverctl.cmd_status(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload['ready'] is False
    assert payload['startupStuck'] is True
    assert payload['startupAgeSeconds'] == 400.0


def test_status_json_exposes_verified_worker_port_drift(monkeypatch, capsys):
    status = {
        'observed': 'starting', 'desired': 'running', 'health': False,
        'port': 15599, 'processStartedAt': 0,
    }
    lock = {'running': True, 'pid': 42,
            'cmdline': 'python server.py --port 15000'}
    drift = {
        'managerPort': 15599, 'workerDeclaredPort': 15000,
        'workerPid': 42, 'health': True, 'liveness': True, 'ready': True,
        'url': 'http://localhost:15000', 'listenerPids': [42],
    }
    monkeypatch.setattr(serverctl, '_remote_status', lambda probe=False: status)
    monkeypatch.setattr(serverctl, 'read_lock_status', lambda _project: lock)
    monkeypatch.setattr(
        serverctl, '_worker_port_drift',
        lambda manager_status, lock_status: drift
        if manager_status is status and lock_status is lock else None)

    args = serverctl.build_parser().parse_args(['status', '--json'])
    assert serverctl.cmd_status(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload['ready'] is False
    assert payload['portDrift'] == drift
    assert payload['applicationReachable'] is True
    assert payload['applicationUrl'] == 'http://localhost:15000'


def test_readiness_requires_live_http_probe_not_only_an_open_port(monkeypatch):
    statuses = iter([
        {'running': True, 'observed': 'running', 'health': False,
         'ready': False, 'port': 15000},
        {'running': True, 'observed': 'running', 'health': True,
         'ready': True, 'port': 15000},
    ])
    probes = []

    def fake_status(*, probe=False):
        probes.append(probe)
        return next(statuses)

    monkeypatch.setattr(serverctl, '_remote_status', fake_status)
    monkeypatch.setattr(serverctl.time, 'sleep', lambda _seconds: None)

    result = serverctl._wait_ready(5)

    assert result['health'] is True
    assert probes == [True, True]
    assert serverctl._status_ready(result) is True
    assert serverctl._status_ready({
        'running': True, 'observed': 'running', 'health': None}) is False


def test_zero_wait_still_performs_one_readiness_probe(monkeypatch):
    calls = []
    ready = {'running': True, 'observed': 'running', 'health': True,
             'ready': True}
    monkeypatch.setattr(
        serverctl, '_remote_status',
        lambda *, probe=False: calls.append(probe) or ready)

    assert serverctl._wait_ready(0) is ready
    assert calls == [True]


def test_wait_ready_returns_immediately_for_an_old_stuck_worker(monkeypatch):
    stuck = {
        'running': True, 'observed': 'starting', 'health': False,
        'processStartedAt': 100.0,
    }
    monkeypatch.setattr(serverctl, '_remote_status', lambda *, probe=False: stuck)
    monkeypatch.setattr(serverctl.time, 'time', lambda: 500.0)
    monkeypatch.setattr(
        serverctl.time, 'sleep',
        lambda _seconds: pytest.fail('old stuck startup must fail without waiting'))

    assert serverctl._wait_ready(180) is stuck
    assert serverctl._startup_stuck(stuck) is True


def test_wait_ready_returns_immediately_for_storage_maintenance(monkeypatch):
    maintenance = {
        'running': False,
        'desired': 'running',
        'observed': 'maintenance',
        'storageLease': {'held': True, 'kind': 'offline_maintenance'},
    }
    monkeypatch.setattr(
        serverctl, '_remote_status',
        lambda *, probe=False: maintenance)
    monkeypatch.setattr(
        serverctl.time, 'sleep',
        lambda _seconds: pytest.fail(
            'maintenance state must return without readiness polling'))

    assert serverctl._wait_ready(180) is maintenance


def test_service_url_normalizes_wildcard_host_and_preserves_tls(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    (data / '.last_serve_mode').write_text('https\n', encoding='utf-8')
    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    monkeypatch.setenv('BIND_HOST', '0.0.0.0')

    assert serverctl._service_url({'port': 16443}) == 'https://localhost:16443'
    assert serverctl._service_url({
        'port': 16443, 'bindHost': '2001:db8::1', 'scheme': 'http',
    }) == 'http://[2001:db8::1]:16443'


def test_logs_missing_file_explains_next_diagnostic_steps(tmp_path, monkeypatch, capsys):
    missing = tmp_path / 'server-console.log'
    monkeypatch.setattr(
        serverctl, '_remote_status', lambda: {'workerLog': str(missing)})
    monkeypatch.setattr(
        serverctl.subprocess, 'call',
        lambda *_args, **_kwargs: pytest.fail('tail must not run for a missing log'))

    args = serverctl.build_parser().parse_args(['logs'])
    assert serverctl.cmd_logs(args) == 1
    error = capsys.readouterr().err
    assert f'No worker log exists yet at {missing}' in error
    assert 'serverctl.py status' in error
    assert 'serverctl.py doctor' in error


def test_follow_logs_treats_ctrl_c_as_normal_shell_interrupt(
        tmp_path, monkeypatch, capsys):
    worker = tmp_path / 'server-console.log'
    worker.write_text('ready\n', encoding='utf-8')
    monkeypatch.setattr(
        serverctl, '_remote_status', lambda: {'workerLog': str(worker)})
    monkeypatch.setattr(
        serverctl.subprocess, 'call',
        lambda *_a, **_k: (_ for _ in ()).throw(KeyboardInterrupt()))

    args = serverctl.build_parser().parse_args(['logs', '-f'])
    assert serverctl.cmd_logs(args) == 130
    assert capsys.readouterr().err == ''


def test_serverctl_help_hides_internal_recovery_verb_and_no_args_is_help(capsys):
    help_text = serverctl.build_parser().format_help()
    assert 'ensure' not in help_text
    assert 'install-recovery' in help_text
    assert 'support-bundle' in help_text
    assert 'inspect-conversation' in help_text
    assert 'login-url' in help_text
    assert serverctl.main([]) == 0
    assert 'Manage the project-local Tofu server' in capsys.readouterr().out


def test_legacy_install_command_warns_and_delegates(monkeypatch, capsys):
    monkeypatch.setattr(serverctl, 'cmd_install', lambda _args: 17)

    assert serverctl.main(['install']) == 17

    captured = capsys.readouterr()
    assert captured.out == ''
    assert "renamed to 'install-recovery'" in captured.err


def test_control_commands_are_absolute_and_shell_copyable_with_spaces(
        tmp_path, monkeypatch):
    project = tmp_path / 'Tofu Project'
    monkeypatch.setattr(serverctl, 'PROJECT', str(project))

    command = serverctl._control_command('logs', '-n', 200)

    assert shlex.split(command) == [
        sys.executable, str(project / 'serverctl.py'), 'logs', '-n', '200']


def test_support_bundle_line_limit_is_validated_by_the_cli():
    parser = serverctl.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(['support-bundle', '--lines', '0'])
    assert exc.value.code == 2

    args = parser.parse_args(['support-bundle', '--no-logs'])
    assert args.no_logs is True


def test_inspect_conversation_delegates_to_read_only_inspector(
        tmp_path, monkeypatch):
    project = tmp_path / 'Tofu Project'
    script = project / 'debug' / 'inspect_conversation.py'
    script.parent.mkdir(parents=True)
    script.write_text('# inspector\n', encoding='utf-8')
    db_path = project / 'data' / 'other.db'
    captured = {}

    monkeypatch.setattr(serverctl, 'PROJECT', str(project))

    def fake_call(command, *, cwd):
        captured['command'] = command
        captured['cwd'] = cwd
        return 2

    monkeypatch.setattr(serverctl.subprocess, 'call', fake_call)
    args = serverctl.build_parser().parse_args([
        'inspect-conversation', 'conv-123', '--db', str(db_path),
        '--user-id', '7', '--full', '--raw', '--no-logs',
    ])

    assert serverctl.cmd_inspect_conversation(args) == 2
    assert captured == {
        'command': [
            sys.executable, str(script), 'conv-123', '--db', str(db_path),
            '--user-id', '7', '--full', '--raw', '--no-logs',
        ],
        'cwd': str(project),
    }


def test_inspect_conversation_line_limit_is_validated_by_cli():
    with pytest.raises(SystemExit) as exc:
        serverctl.build_parser().parse_args([
            'inspect-conversation', 'conv-123', '--lines', '1001'])
    assert exc.value.code == 2


def test_login_base_url_normalizes_wildcard_and_docker_port(
        tmp_path, monkeypatch):
    monkeypatch.setattr(serverctl, 'PROJECT', str(tmp_path))
    monkeypatch.setenv('BIND_HOST', '0.0.0.0')
    monkeypatch.setenv('TOFU_PUBLISHED_PORT', '18080')
    monkeypatch.delenv('TOFU_PUBLIC_URL', raising=False)
    monkeypatch.delenv('TOFU_TLS', raising=False)

    assert serverctl._login_base_url() == 'http://localhost:18080'
    assert serverctl._login_base_url(
        'https://[2001:db8::1]:8443/') == 'https://[2001:db8::1]:8443'
    with pytest.raises(ValueError, match='must not contain'):
        serverctl._login_base_url('https://user:secret@example.test/?x=1')
    monkeypatch.setenv('TOFU_TLS', 'sometimes')
    with pytest.raises(ValueError, match='unsupported TOFU_TLS'):
        serverctl._login_base_url()


def test_login_url_open_mode_never_reads_token(monkeypatch, capsys):
    monkeypatch.setattr(serverctl, '_resolved_auth_mode', lambda: 'open')
    monkeypatch.setattr(
        serverctl, '_read_first_run_token',
        lambda: (_ for _ in ()).throw(AssertionError('token must stay unread')))
    args = SimpleNamespace(base_url='http://localhost:15000')

    assert serverctl.cmd_login_url(args) == 0
    captured = capsys.readouterr()
    assert 'no login token is required' in captured.out
    assert 'Open: http://localhost:15000' in captured.out
    assert captured.err == ''


def test_login_url_private_mode_is_explicit_and_copyable(
        tmp_path, monkeypatch, capsys):
    token_path = tmp_path / '.first_run_token'
    monkeypatch.setattr(serverctl, '_resolved_auth_mode', lambda: 'private')
    monkeypatch.setattr(
        serverctl, '_read_first_run_token',
        lambda: ('tofu_admin_a/b+c', token_path))
    args = SimpleNamespace(base_url='http://localhost:15000')

    assert serverctl.cmd_login_url(args) == 0
    captured = capsys.readouterr()
    assert 'Open once: http://localhost:15000/?token=tofu_admin_a%2Fb%2Bc' \
        in captured.out
    assert f'Token source: {token_path}' in captured.out
    assert 'Do not share' in captured.err


def test_first_run_token_reader_refuses_broad_permissions(
        tmp_path, monkeypatch):
    from lib import api_keys

    token_file = tmp_path / '.first_run_token'
    token_file.write_text('tofu_admin_safevalue123\n', encoding='utf-8')
    token_file.chmod(0o644)
    monkeypatch.setattr(api_keys, '_FIRST_RUN_TOKEN_FILE', str(token_file))
    monkeypatch.setattr(api_keys, 'validate_token', lambda _token: object())

    with pytest.raises(PermissionError, match='permissions are too broad'):
        serverctl._read_first_run_token()

    token_file.chmod(0o600)
    token, path = serverctl._read_first_run_token()
    assert token == 'tofu_admin_safevalue123'
    assert path == token_file
