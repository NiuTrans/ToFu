"""Rollback of the restart acceptance stamp when no bridge can execute.

Pin for the second half of the stuck-retry fix: when ``perform_server_reexec``
fails BEFORE the shutdown bridge fires (manager refused AND in-place execv
unavailable), the acceptance stamp recorded by ``consume_restart`` must be
rolled back. Otherwise the still-serving old process keeps reporting
``restartPending``/cooldown and refuses every later retry — the exact dead
end that forced operators to the terminal.
"""

from __future__ import annotations

import lib.server.server_reexec as reexec

import pytest

from routes.api_v1 import update as upd


@pytest.mark.unit
def test_total_failure_rolls_back_restart_stamp(monkeypatch):
    """Manager refuses + execv bridge absent → False AND the stamp is cleared,
    so the next human-approved retry is a real retry."""
    cleared = []
    monkeypatch.delenv('TOFU_MANAGED_BY', raising=False)
    monkeypatch.setenv('TOFU_PROJECT_PATH', '/project')
    monkeypatch.setattr(reexec, 'prepare_server_reexec_frontend', lambda: '')

    def refuse(project, **kwargs):
        raise RuntimeError('manager unreachable')

    monkeypatch.setattr(
        'serverctl_pkg.application_restart.run_application_restart_control', refuse)
    monkeypatch.setattr(
        'lib.server.server_reexec.begin_server_reexec', lambda reason: False)
    monkeypatch.setattr(
        'lib.lifecycle_approval.rollback_restart_stamp',
        lambda *, pid: cleared.append(pid) or True)
    monkeypatch.setattr(reexec, 'audit_log', lambda *a, **k: None)

    assert reexec.perform_server_reexec('update') is False
    assert cleared == [reexec.os.getpid()]


@pytest.mark.unit
def test_preflight_failure_does_not_touch_stamp(monkeypatch):
    """A preflight refusal happens before acceptance semantics changed hands;
    it must not roll back a stamp another path may still rely on."""
    cleared = []
    monkeypatch.setattr(
        reexec, 'prepare_server_reexec_frontend', lambda: 'bundle broken')
    monkeypatch.setattr(
        'lib.lifecycle_approval.rollback_restart_stamp',
        lambda *, pid: cleared.append(pid) or True)
    monkeypatch.setattr(reexec, 'audit_log', lambda *a, **k: None)

    assert reexec.perform_server_reexec('update') is False
    assert cleared == []
