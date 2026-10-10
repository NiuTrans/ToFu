"""Focused tests for the first-attempt tool-call outcome audit owner."""

from __future__ import annotations

import pytest

from lib.tasks_pkg import tool_call_outcome as outcome_mod


pytestmark = pytest.mark.unit


@pytest.fixture
def captured(monkeypatch):
    rows = []

    def fake_audit(event, **details):
        rows.append({'event': event, **details})

    monkeypatch.setattr(outcome_mod, 'audit_log', fake_audit)
    return rows


def _task():
    return {'id': 'task-1', 'convId': 'conv-1', 'model': 'kimi-k3'}


def test_clean_done_round_audits_ok_once(captured):
    task = _task()
    round_entry = {'toolName': 'read_files', 'status': 'done',
                   'source': 'native_direct'}

    outcome_mod.audit_round_outcome(task, round_entry, status='done')
    outcome_mod.audit_round_outcome(task, round_entry, status='done')

    assert len(captured) == 1
    row = captured[0]
    assert row['event'] == 'tool_call_outcome'
    assert row['outcome'] == 'ok'
    assert row['tool'] == 'read_files'
    assert row['model'] == 'kimi-k3'
    assert row['source'] == 'native_direct'


def test_done_round_with_repairs_audits_repaired_patterns(captured):
    round_entry = {
        'toolName': 'read_files', 'status': 'done',
        '_repaired': {'label': 'auto-fixed', 'detail': 'renamed',
                      'patterns': ['tool_name_alias', 'stringified_json']},
    }
    outcome_mod.audit_round_outcome(_task(), round_entry, status='done')

    assert captured[0]['outcome'] == 'repaired'
    assert captured[0]['repairs'] == ['tool_name_alias',
                                      'stringified_json']


def test_rejected_round_carries_descriptor_kind_and_attempted(captured):
    round_entry = {
        'toolName': 'serch', 'status': 'rejected',
        'rejection': {'kind': 'hallucinated', 'attempted': 'serch',
                      'suggestions': ['web_search']},
    }
    outcome_mod.audit_round_outcome(_task(), round_entry, status='rejected')

    row = captured[0]
    assert row['outcome'] == 'rejected'
    assert row['rejection_kind'] == 'hallucinated'
    assert row['attempted'] == 'serch'


def test_error_verdict_maps_to_raw_status(captured):
    round_entry = {'toolName': 'run_command', 'status': 'error'}
    outcome_mod.audit_round_outcome(_task(), round_entry, status='error')

    assert captured[0]['outcome'] == 'error'


def test_gateway_rejection_uses_code_and_attempted(captured):
    error = {'code': 'unknown_tool', 'attempted': 'bashh', 'index': 0}
    outcome_mod.audit_gateway_rejected_call(
        _task(), error, source='execute_calls')

    row = captured[0]
    assert row['event'] == 'tool_call_outcome'
    assert row['outcome'] == 'rejected'
    assert row['rejection_kind'] == 'gateway_unknown_tool'
    assert row['attempted'] == 'bashh'
    assert row['source'] == 'execute_calls'


def test_program_outcome_audits_once_with_clean_evidence(captured):
    run = {
        'callId': 'prog-1', 'status': 'completed', 'tStart': 100,
        'tEnd': 4600, 'childCalls': [
            {'id': 'c1', 'name': 'read_files', 'status': 'completed'},
            {'id': 'c2', 'name': 'web_search', 'status': 'rejected'},
        ],
        'syntaxRepairs': [{'kind': 'unterminated_string', 'offset': 42}],
    }
    outcome_mod.audit_program_outcome(
        _task(), run, backend='local_toolscript', status='completed')
    outcome_mod.audit_program_outcome(
        _task(), run, backend='local_toolscript', status='completed')

    assert len(captured) == 1
    row = captured[0]
    assert row['event'] == 'ptc_program_outcome'
    assert row['backend'] == 'local_toolscript'
    assert row['status'] == 'completed'
    assert row['children'] == 2
    assert row['rejected_children'] == 1
    assert row['syntax_repairs'] == 1
    assert row['duration_ms'] == 4500


def test_program_outcome_error_code_prefers_code_then_kind(captured):
    run = {'callId': 'prog-2', 'status': 'error',
           'error': {'kind': 'toolscript_error', 'code': 'syntax_error'}}
    outcome_mod.audit_program_outcome(
        _task(), run, backend='local_toolscript', status='error')

    assert captured[0]['error_code'] == 'syntax_error'


def test_audit_failures_never_propagate(monkeypatch):
    def boom(event, **details):
        raise RuntimeError('audit backend down')

    monkeypatch.setattr(outcome_mod, 'audit_log', boom)
    # Must not raise even when the audit backend explodes.
    outcome_mod.audit_round_outcome(
        _task(), {'toolName': 'read_files'}, status='done')
    outcome_mod.audit_gateway_rejected_call(
        _task(), {'code': 'x'}, source='execute_calls')
    outcome_mod.audit_program_outcome(
        _task(), {'callId': 'p'}, backend='native_openai', status='error')
