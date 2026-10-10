"""Approval delegation: policy registration, verdict parsing, gate wiring.

The adjudicator is always substituted — these tests pin the contract and
the fail-closed gate semantics without any LLM call.
"""
import json

import pytest

import lib.tasks_pkg.approval_delegate as delegate
from lib.tasks_pkg.approval_delegate import (
    approval_delegate_for,
    parse_adjudicator_verdict,
    register_approval_delegate,
)

pytestmark = pytest.mark.unit


def _task():
    return {'id': 'task-12345678', 'config': {}, '_userId': 7,
            'aborted': False, 'status': 'running'}


# ─────────────────────────────────────────────────────────
#  register_approval_delegate
# ─────────────────────────────────────────────────────────

def test_register_enable_then_clear():
    task = _task()
    out = json.loads(register_approval_delegate(task, {
        'policy': 'Allow read-only commands; deny package installs.'}))
    assert out['status'] == 'ok' and out['delegate'] == 'active'
    assert out['tools'] == ['run_command']  # default scope
    assert out['timeout_seconds'] == 30
    stored = task['config']['_approvalDelegate']
    assert stored['policy'].startswith('Allow read-only')
    assert stored['model_tier'] == 'light'

    out = json.loads(register_approval_delegate(task, {'enabled': False}))
    assert out['delegate'] == 'cleared'
    assert '_approvalDelegate' not in task['config']
    out = json.loads(register_approval_delegate(task, {'enabled': False}))
    assert out['delegate'] == 'already_inactive'


def test_register_requires_policy_and_rejects_wildcards():
    task = _task()
    out = json.loads(register_approval_delegate(task, {}))
    assert out['status'] == 'error' and out['error'] == 'policy_required'
    out = json.loads(register_approval_delegate(task, {
        'policy': 'allow everything', 'tools': ['mcp__*']}))
    assert out['status'] == 'error' and out['error'] == 'wildcards_rejected'
    assert '_approvalDelegate' not in task['config']


def test_register_clamps_and_validates():
    task = _task()
    out = json.loads(register_approval_delegate(task, {
        'policy': 'x', 'timeout_seconds': 99999, 'model_tier': 'heavy',
        'tools': ['run_command', 'run_command', '']}))
    assert out['timeout_seconds'] == 120
    stored = task['config']['_approvalDelegate']
    assert stored['model_tier'] == 'light'  # invalid tier → light
    assert stored['tools'] == ['run_command']  # deduped, empties dropped

    out = json.loads(register_approval_delegate(task, {
        'policy': 'x' * 5000}))
    assert out['status'] == 'error' and out['error'] == 'policy_too_long'


def test_approval_delegate_for_matches_scope():
    task = _task()
    assert approval_delegate_for(task, 'run_command') is None
    register_approval_delegate(task, {
        'policy': 'judge', 'tools': ['run_command', 'write_file']})
    assert approval_delegate_for(task, 'run_command') is not None
    assert approval_delegate_for(task, 'write_file') is not None
    assert approval_delegate_for(task, 'edit_file') is None


# ─────────────────────────────────────────────────────────
#  parse_adjudicator_verdict — conservative extraction
# ─────────────────────────────────────────────────────────

def test_parse_verdict_accepts_clean_approve_and_deny():
    assert parse_adjudicator_verdict(
        'Some reasoning.\n{"approve": true, "reason": "read-only ls"}') == {
        'approve': True, 'reason': 'read-only ls'}
    assert parse_adjudicator_verdict(
        '{"approve": false, "reason": "touches prod"}') == {
        'approve': False, 'reason': 'touches prod'}


def test_parse_verdict_last_object_wins():
    content = ('{"approve": false, "reason": "draft"}\nmore thought\n'
               '{"approve": true, "reason": "final"}')
    assert parse_adjudicator_verdict(content)['approve'] is True


def test_parse_verdict_rejects_malformed_and_ambiguous():
    assert parse_adjudicator_verdict('') is None
    assert parse_adjudicator_verdict('no json here') is None
    assert parse_adjudicator_verdict('{"approve": "yes"}') is None
    assert parse_adjudicator_verdict('{"reason": "no verdict"}') is None
    assert parse_adjudicator_verdict('{not json}') is None


def test_parse_verdict_caps_reason_and_defaults():
    verdict = parse_adjudicator_verdict(
        '{"approve": true, "reason": "' + 'x' * 900 + '"}')
    assert len(verdict['reason']) == 500
    verdict = parse_adjudicator_verdict('{"approve": true}')
    assert verdict['reason'] == 'approved by policy'


# ─────────────────────────────────────────────────────────
#  Gate wiring — _approval_stops_tool_call with a delegate
# ─────────────────────────────────────────────────────────

def _gate_call(task, *, fn_name='run_command',
               fn_args=None, verdict_content=''):
    """Invoke the real gate with the adjudicator/human paths substituted."""
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline

    fn_args = fn_args or {'command': 'rm -rf /'}
    round_entry = {'toolName': fn_name, 'toolCallId': 'tc-1'}
    tool_results = {}
    monkeypatch = _gate_call._monkeypatch
    monkeypatch.setattr(
        delegate, '_run_adjudicator', lambda *a, **kw: verdict_content)
    settle = []
    monkeypatch.setattr(
        pipeline, '_settle_tool_result',
        lambda *a, **kw: settle.append(kw.get('terminal_status')))
    human = []
    monkeypatch.setattr(
        pipeline, '_handle_approval',
        lambda *a, **kw: (human.append(a) or (False, 'human says no')))
    stopped = pipeline._approval_stops_tool_call(
        task=task, fn_name=fn_name, fn_args=fn_args, tc_id='tc-1', rn=1,
        round_entry=round_entry,
        confirmation_tools={'run_command'}, write_tools=set(),
        attended=True, auto_apply=False, cfg=task['config'],
        project_path='', round_num=1, model='test-model',
        tool_results=tool_results, idempotent_tools=set(), cache={},
        tid='task-123')
    return stopped, round_entry, tool_results, settle, human


@pytest.fixture
def _patch(monkeypatch):
    _gate_call._monkeypatch = monkeypatch
    return monkeypatch


def test_gate_delegate_approve_binds_receipt_and_records(_patch):
    task = _task()
    register_approval_delegate(task, {'policy': 'allow everything'})
    stopped, round_entry, _results, settle, human = _gate_call(
        task, verdict_content='{"approve": true, "reason": "fits policy"}')
    assert stopped is False
    assert human == []  # human gate never reached
    assert settle == []  # nothing rejected
    assert round_entry['approvalDelegate'] == {
        'decision': 'approved', 'reason': 'fits policy',
        'authority': 'agent_adjudicator'}
    receipt = task['_tool_approval_receipts']['tc-1']
    assert receipt['authority'] == 'agent_adjudicator'


def test_gate_delegate_deny_settles_rejected_with_reason(_patch):
    task = _task()
    register_approval_delegate(task, {'policy': 'deny destructive'})
    stopped, _entry, results, settle, human = _gate_call(
        task, verdict_content='{"approve": false, "reason": "rm -rf is out"}')
    assert stopped is True
    assert human == []
    assert settle == ['rejected']
    content, _ = results['tc-1']
    assert 'rm -rf is out' in content
    assert '_tool_approval_receipts' not in task


def test_gate_undecided_falls_through_to_human(_patch):
    task = _task()
    register_approval_delegate(task, {'policy': 'unclear'})
    stopped, _entry, results, _settle, human = _gate_call(
        task, verdict_content='I cannot decide this one.')
    assert human, 'undecided adjudication must reach the human gate'
    assert stopped is True  # (substituted human said no)
    assert results['tc-1'][0] == 'human says no'


def test_gate_never_adjudicates_child_invocations(_patch):
    from lib.tasks_pkg.invocation import InvocationPolicy
    task = _task()
    register_approval_delegate(task, {'policy': 'allow everything'})
    task['_invocationPolicy'] = InvocationPolicy.child('parent-task')
    called = []
    _patch.setattr(delegate, '_run_adjudicator',
                   lambda *a, **kw: called.append(a) or '{"approve": true}')
    _gate_call(task)  # substituted human gate handles it
    assert called == [], 'child invocations must not spawn adjudicators'


def test_gate_skips_uncovered_tools(_patch):
    task = _task()
    register_approval_delegate(task, {
        'policy': 'judge', 'tools': ['write_file']})
    called = []
    _patch.setattr(delegate, '_run_adjudicator',
                   lambda *a, **kw: called.append(a) or '{"approve": true}')
    _gate_call(task, fn_name='run_command')
    assert called == []


def test_gate_without_delegate_unchanged(_patch):
    task = _task()
    called = []
    _patch.setattr(delegate, '_run_adjudicator',
                   lambda *a, **kw: called.append(a) or '')
    _gate_call(task)
    assert called == []


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
