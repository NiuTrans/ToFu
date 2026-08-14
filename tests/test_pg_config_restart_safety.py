"""Regression contract: config discovery must never bounce a live PG."""

from __future__ import annotations

import logging

import pytest

pytestmark = pytest.mark.unit


def test_managed_config_change_is_staged_without_automatic_restart(
        monkeypatch, caplog):
    from lib.database._bootstrap import _orchestrate as orchestrate
    from lib.database._bootstrap import _config as config

    writes = []
    monkeypatch.setattr(
        orchestrate, '_ensure_managed_pg_config',
        lambda pgdata: writes.append(pgdata) or True)

    def forbidden_restart(*_args, **_kwargs):
        raise AssertionError('import-time discovery must not restart shared PG')

    monkeypatch.setattr(config, '_restart_local_pg', forbidden_restart)
    with caplog.at_level(logging.WARNING):
        assert orchestrate._stage_managed_pg_config('/owned/pgdata') is True

    assert writes == ['/owned/pgdata']
    assert 'automatic restart is disabled' in caplog.text


def test_unchanged_managed_config_is_a_quiet_noop(monkeypatch, caplog):
    from lib.database._bootstrap import _orchestrate as orchestrate

    monkeypatch.setattr(
        orchestrate, '_ensure_managed_pg_config', lambda _pgdata: False)
    with caplog.at_level(logging.WARNING):
        assert orchestrate._stage_managed_pg_config('/owned/pgdata') is False
    assert 'automatic restart is disabled' not in caplog.text


def test_low_level_restart_requires_explicit_maintenance_approval(
        monkeypatch, caplog):
    from lib.database._bootstrap import _config as config

    def forbidden_run(*_args, **_kwargs):
        raise AssertionError('pg_ctl must not run without maintenance approval')

    monkeypatch.setattr(config.subprocess, 'run', forbidden_run)
    with caplog.at_level(logging.ERROR):
        assert config._restart_local_pg('/owned/pgdata', '/project') is False
    assert 'without explicit maintenance approval' in caplog.text


def test_approved_low_level_restart_keeps_argument_vector_safe(monkeypatch):
    from types import SimpleNamespace

    from lib.database._bootstrap import _config as config

    calls = []
    monkeypatch.setattr(config, '_find_pg_binary', lambda _name: '/bin/pg_ctl')
    monkeypatch.setattr(
        config.subprocess, 'run',
        lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(
            returncode=0, stderr=''))

    assert config._restart_local_pg(
        '/owned/pgdata', '/project', maintenance_approved=True) is True
    assert calls[0][0] == [
        '/bin/pg_ctl', '-D', '/owned/pgdata', '-l',
        '/project/logs/postgresql.log', 'restart', '-m', 'fast', '-w', '-t', '30']
    assert calls[0][1]['timeout'] == 45
