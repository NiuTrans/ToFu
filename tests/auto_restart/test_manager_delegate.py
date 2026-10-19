"""Unconditional manager delegation for the in-app restart primitive.

Pin for the root-cause fix of the stuck-retry incident: ``_perform_server_reexec``
must try the host-local Supervisor's deferred worker restart for EVERY launch
mode, not only when ``TOFU_MANAGED_BY=supervisor`` happens to be set. The
manager kills and respawns the worker from outside, so a restart never depends
on the sick process replacing itself. Self-``execv`` remains the last-resort
fallback when the manager cannot be reached or refuses.
"""

from __future__ import annotations

import pytest

from routes.api_v1 import update as upd


@pytest.mark.unit
def test_unmanaged_worker_delegates_to_manager(monkeypatch):
    """No TOFU_MANAGED_BY → still prefer the manager over self-execv."""
    calls = []
    monkeypatch.delenv('TOFU_MANAGED_BY', raising=False)
    monkeypatch.setenv('TOFU_PROJECT_PATH', '/project')
    monkeypatch.setattr(upd, '_prepare_server_reexec_frontend', lambda: '')
    monkeypatch.setattr(
        'serverctl_pkg.application_restart.run_application_restart_control',
        lambda project, **kwargs: calls.append((project, kwargs)) or {
            'ok': True,
            'supervisorRefresh': {'managerPid': 42},
        },
    )
    monkeypatch.setattr(
        'lib.server.server_reexec.begin_server_reexec',
        lambda reason: calls.append(('execv', reason)) or True)

    assert upd._perform_server_reexec('update') is True
    assert calls == [('/project', {
        'stage': 'delegate',
        'reason': 'update',
    })]


@pytest.mark.unit
def test_alien_managed_worker_delegates_to_manager(monkeypatch):
    """docker/bootstrap/desktop launch modes must also reach the manager."""
    calls = []
    monkeypatch.setenv('TOFU_MANAGED_BY', 'docker')
    monkeypatch.setenv('TOFU_PROJECT_PATH', '/project')
    monkeypatch.setattr(upd, '_prepare_server_reexec_frontend', lambda: '')
    monkeypatch.setattr(
        'serverctl_pkg.application_restart.run_application_restart_control',
        lambda project, **kwargs: calls.append((project, kwargs)) or {
            'ok': True,
            'supervisorRefresh': {'managerPid': 42},
        },
    )

    assert upd._perform_server_reexec('update') is True
    assert calls and calls[0][0] == '/project'


@pytest.mark.unit
def test_manager_refusal_falls_back_to_self_execv(monkeypatch):
    """When the manager refuses, the bounded in-place execv path still works
    and the failure is audited as a delegation miss, not silently swallowed."""
    calls = []
    monkeypatch.delenv('TOFU_MANAGED_BY', raising=False)
    monkeypatch.setenv('TOFU_PROJECT_PATH', '/project')
    monkeypatch.setattr(upd, '_prepare_server_reexec_frontend', lambda: '')

    def refuse(project, **kwargs):
        raise RuntimeError('manager unreachable')

    monkeypatch.setattr(
        'serverctl_pkg.application_restart.run_application_restart_control', refuse)
    monkeypatch.setattr(
        'lib.server.server_reexec.begin_server_reexec',
        lambda reason: calls.append(('begin', reason)) or True)
    monkeypatch.setattr(
        'lib.server.server_reexec.finish_server_reexec_preparation',
        lambda: calls.append(('finish',)) or True)
    monkeypatch.setattr(upd, 'audit_log', lambda *a, **k: None)

    assert upd._perform_server_reexec('update') is True
    assert ('begin', 'update') in calls
    assert ('finish',) in calls
