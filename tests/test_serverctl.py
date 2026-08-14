"""Unit contracts for the human-facing lifecycle CLI."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

import serverctl


pytestmark = pytest.mark.unit


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


def test_forwarded_server_env_is_narrow(monkeypatch):
    monkeypatch.setenv('PORT', '16000')
    monkeypatch.setenv('BIND_HOST', '127.0.0.1')
    monkeypatch.setenv('TOFU_PROCESS_RSS_RECYCLE_MB', '6144')
    monkeypatch.setenv('DATABASE_URL', 'secret')
    assert serverctl._forwarded_server_env() == {
        'PORT': '16000', 'BIND_HOST': '127.0.0.1',
        'TOFU_PROCESS_RSS_RECYCLE_MB': '6144'}


def test_manager_launcher_timeout_outlives_shell_watchdog_budget(monkeypatch):
    health = iter([None, {'ok': True}])
    monkeypatch.setattr(serverctl, '_manager_health', lambda: next(health))
    observed = {}

    def fake_run(_cmd, **kwargs):
        observed['timeout'] = kwargs['timeout']
        return SimpleNamespace(stdout='', stderr='', returncode=0)

    monkeypatch.setattr(serverctl.subprocess, 'run', fake_run)

    assert serverctl.ensure_manager(timeout=8.0) == {'ok': True}
    assert observed['timeout'] >= 25.0


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
    snapshot = external / 'tofu-20260813_020000-test.sqlite3'
    snapshot.write_bytes(b'snapshot')
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
