"""serverctl's operator paths leave distinct, audited approval records (owner decision 2026-11-06).

The owner's interactive terminal confirmation and the handoff variable of a
caller that already passed the gate (restart_15000.sh, the TofuDB cutover)
stay available, but a restart through them is never again invisible in the
approval history.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def approvals(tmp_path, monkeypatch):
    from lib import lifecycle_approval as gate
    monkeypatch.setattr(gate, '_APPROVALS_FILE', str(tmp_path / 'lifecycle_approvals.json'))
    monkeypatch.setattr(gate, '_STATE_FILE', str(tmp_path / 'lifecycle_state.json'))
    for name in ('TOFU_RESTART_GATE_PASSED', 'TOFU_LIFECYCLE_GATE_PASSED'):
        monkeypatch.delenv(name, raising=False)
    return gate


class _Terminal:
    def isatty(self):
        return True

    def fileno(self):
        return 0


@pytest.mark.parametrize('variable, value, action', [
    ('TOFU_RESTART_GATE_PASSED', '1', 'restart'),
    ('TOFU_LIFECYCLE_GATE_PASSED', 'shutdown', 'shutdown'),
])
def test_the_environment_handoff_is_recorded_as_its_own_provenance(approvals, monkeypatch, variable, value,
                                                                     action):
    import serverctl
    monkeypatch.setenv(variable, value)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    assert serverctl._approve_lifecycle_if_needed(
        SimpleNamespace(yes=False, source='restart_15000.sh'), action=action, prompt='unused') is True
    [record] = approvals.list_records()
    assert record['action'] == action and record['status'] == 'consumed'
    assert record['decided_by'] == 'environment-override'
    assert record['origin']['variable'] == variable and record['origin']['pid'] > 0
    assert record['origin']['source'] == 'restart_15000.sh'


def test_the_owners_terminal_confirmation_still_works_and_is_recorded(approvals, monkeypatch):
    import serverctl
    monkeypatch.setattr(sys, 'stdin', _Terminal())
    monkeypatch.setattr('os.ttyname', lambda fd: '/dev/pts/3')
    answers = iter(['y', 'n'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(answers))
    args = SimpleNamespace(yes=False, source='serverctl')
    assert serverctl._approve_lifecycle_if_needed(args, action='restart', prompt='Restart? ') is True
    [record] = approvals.list_records()
    assert record['decided_by'] == 'interactive-terminal' and record['status'] == 'consumed'
    assert record['origin']['tty'] == '/dev/pts/3' and record['origin']['confirmation'] == 'typed'
    # A declined prompt runs nothing and records nothing.
    assert serverctl._approve_lifecycle_if_needed(args, action='restart', prompt='Restart? ') is False
    assert len(approvals.list_records()) == 1
    yes = SimpleNamespace(yes=True, source='serverctl')
    assert serverctl._approve_lifecycle_if_needed(yes, action='restart', prompt='unused') is True
    assert approvals.list_records()[0]['origin']['confirmation'] == 'yes-flag'


def test_an_unwritable_approval_store_never_blocks_the_owners_terminal(approvals, monkeypatch, capsys):
    import serverctl

    def unwritable(action, provenance):
        raise OSError('read-only data volume')
    monkeypatch.setattr(approvals, 'record_operator_pass', unwritable)
    monkeypatch.setattr(sys, 'stdin', _Terminal())
    monkeypatch.setattr('os.ttyname', lambda fd: '/dev/pts/3')
    assert serverctl._approve_lifecycle_if_needed(
        SimpleNamespace(yes=True, source='serverctl'), action='restart', prompt='unused') is True
    assert 'approval record could not be written' in capsys.readouterr().err


def test_operator_records_accept_only_operator_provenance(approvals):
    with pytest.raises(ValueError):
        approvals.record_operator_pass('restart', {'kind': 'human-ui'})
    with pytest.raises(ValueError):
        approvals.record_operator_pass('reboot', {'kind': 'interactive-terminal'})
    assert approvals.list_records() == []


@pytest.mark.parametrize('action', ['restart', 'shutdown'])
def test_noninteractive_gate_reuses_request_and_directs_to_ui_execution(
        approvals, monkeypatch, capsys, action):
    """The UI executes its approval; the CLI must direct follow-up to status."""
    import serverctl
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    args = SimpleNamespace(yes=True, source='serverctl')
    for _ in range(2):
        assert serverctl._approve_lifecycle_if_needed(
            args, action=action, prompt='unused') is False
    [request] = approvals.list_records()
    assert request['action'] == action and request['status'] == 'pending'
    assert request['origin']['source'] == 'serverctl'
    error = capsys.readouterr().err
    assert 'requires human approval' in error
    assert request['id'][:8] in error
    assert 'Approve and execute' in error
    assert 'serverctl.py status' in error
    assert 'then re-run this command' not in error
