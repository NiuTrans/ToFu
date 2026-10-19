"""Coding evidence must come from delivered behavior, with bounded failure costs."""
from __future__ import annotations

import json
import os

import pytest

from evaluations.evolution import coding, coding_task, soak
from tests.evolution.test_evolution_soak import _run

pytestmark = pytest.mark.unit

REFERENCE = '''
def dependency_layers(dependencies):
    pending = {name: set(prerequisites) for name, prerequisites in dependencies.items()}
    for prerequisites in dependencies.values():
        for name in prerequisites:
            pending.setdefault(name, set())
    layers = []
    while pending:
        ready = sorted(name for name, prerequisites in pending.items() if not prerequisites)
        if not ready:
            raise ValueError('cycle')
        layers.append(ready)
        for name in ready:
            del pending[name]
        for prerequisites in pending.values():
            prerequisites.difference_update(ready)
    return layers
'''


@pytest.mark.parametrize('source,passed', [(REFERENCE, True), (coding_task.STARTER, False),
    ('def dependency_layers(x):\n    return []\n', False), ('broken python !', False)])
def test_independent_checker_grades_behavior(tmp_path, source, passed):
    (tmp_path / 'planner.py').write_text(source)
    # Candidate-written tests or victory files never determine the score.
    (tmp_path / 'test_planner.py').write_text('raise SystemExit(0)')
    result = coding_task.check_delivery(tmp_path, scratch=tmp_path)
    assert result['passed'] is passed
    assert result['source_sha256']
    assert not result['timed_out']


def test_checker_times_out_and_keeps_exit_separate(tmp_path):
    (tmp_path / 'planner.py').write_text('while True: pass')
    result = coding_task.check_delivery(tmp_path, scratch=tmp_path, timeout_seconds=.15)
    assert result['timed_out'] and result['exit_code'] != 0 and not result['passed']
    assert list(tmp_path.iterdir()) == [tmp_path / 'planner.py']


def test_checker_does_not_inherit_secrets_or_python_path(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_API_KEY', 'test-secret')
    source = "import os\nassert 'TOFU_API_KEY' not in os.environ\n" + REFERENCE
    (tmp_path / 'planner.py').write_text(source)
    assert coding_task.check_delivery(tmp_path, scratch=tmp_path)['passed']


@pytest.mark.parametrize('kind', ['symlink', 'large', 'fifo', 'noisy'])
def test_checker_refuses_unbounded_or_redirected_delivery(tmp_path, kind):
    module = tmp_path / 'planner.py'
    if kind == 'symlink':
        target = tmp_path / 'original.py'
        target.write_text(REFERENCE)
        module.symlink_to(target)
    elif kind == 'fifo':
        os.mkfifo(module)
    elif kind == 'large':
        module.write_bytes(b' ' * (coding_task.MAX_SOURCE_BYTES + 1))
    else:
        module.write_text('print("x" * 1000000)')
    assert not coding_task.check_delivery(tmp_path, scratch=tmp_path)['passed']


def test_launch_policy_is_frozen_across_retry_and_retains_admission_cap():
    policy = {'project_path': '/project', 'executor_schema_budget_tokens': 4000,
              'budget': {'max_tool_calls': 20},
              'require_command_execution': True, 'max_attempts': 1}
    seen = []
    complete = _run('completed', tokens=9, receipts=[{'task_id': 'task'}])
    def request(method, path, body, key):
        if method == 'POST':
            seen.append(json.loads(json.dumps(body)))
            policy['budget']['max_tool_calls'] = 999
        return {'run': complete}
    result = soak.observe_run(request, objective='code', key='key', max_tokens=100,
        launch_policy=policy, poll_seconds=1, timeout_seconds=10)
    assert result['complete']
    assert seen[0] == seen[1]
    assert seen[0]['budget'] == {'max_tokens': 100, 'max_tool_calls': 20}
    assert seen[0]['require_command_execution']
    assert seen[0]['executor_schema_budget_tokens'] == 4000


@pytest.mark.parametrize('policy', [{'objective': 'changed'}, {'budget': {'max_tokens': 999}}, []])
def test_policy_cannot_override_goal_or_token_allowance(policy):
    def no_request(*args):
        pytest.fail('invalid policy dispatched')
    with pytest.raises(ValueError):
        soak.observe_run(no_request, objective='code', key='key', max_tokens=100,
            launch_policy=policy, poll_seconds=1, timeout_seconds=10)


def test_offline_preflight_does_not_contact_service(monkeypatch, capsys):
    def no_access(**kwargs):
        pytest.fail('offline preflight contacted service')
    monkeypatch.setattr(coding, 'ChatEvaluator', no_access)
    assert coding.main([]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest['task']['case_count'] == len(coding_task.CASES)
    assert manifest['mode'] == 'offline_preflight'


@pytest.mark.parametrize('delivery,usage_unknown', [(REFERENCE, False),
    (coding_task.STARTER, False), (REFERENCE, True)])
def test_cli_separates_review_code_and_unknown_cost(tmp_path, monkeypatch, capsys, delivery, usage_unknown):
    class Access:
        deployment_identity = {'boot_id': 'test'}
        def __init__(self, **kwargs):
            pass
        def _deployment_identity(self):
            return self.deployment_identity
    monkeypatch.setattr(coding, 'ChatEvaluator', Access)
    # Storage policy has its own tests; this hermetic CLI owns pytest's temp root.
    monkeypatch.setattr(coding, 'validate_output_path', lambda path: path.resolve())
    def observe(request, **kwargs):
        project = coding.Path(kwargs['launch_policy']['project_path'])
        (project / 'planner.py').write_text(delivery)
        assert kwargs['max_tokens'] == 60000
        return {'status': 'completed', 'complete': True, 'tokens': 123,
                'provider_usage_unknown': usage_unknown, 'elapsed_seconds': 2.5}
    monkeypatch.setattr(soak, 'observe_run', observe)
    output = tmp_path / 'evaluation'
    passed = delivery == REFERENCE
    assert coding.main(['--live', '--user-id', '7', '--model-ref',
        '{"creator_id":"test","model_id":"model"}', '--output-dir', str(output)]) == (0 if passed else 1)
    summary = json.loads(capsys.readouterr().out)
    assert summary['review_completed'] == 1
    assert summary['code_passed'] == int(passed)
    assert summary['tokens'] == 123
    assert summary['tokens_are_lower_bound'] == usage_unknown
    assert summary['p50_tokens_per_accepted'] == (123 if passed and not usage_unknown else None)
    assert (output / 'manifest.json').is_file()


def test_live_requires_owner_and_model_before_network():
    with pytest.raises(SystemExit):
        coding.main(['--live'])


def test_diagnostics_keep_cost_and_learning_metadata_without_prompts():
    def request(method, path, body, key):
        if path.endswith('/requests'):
            return {'coverage': 'full', 'requests': [{'roundNum': 1,
                'messages': ['private prompt'], 'attempts': [{'tokensIn': 24000,
                    'tokensOut': 100, 'streamElapsedMs': 9000,
                    'wireProjection': {'schemaTokens': 14600, 'toolCount': 39}}]}]}
        return {'run': {'run_id': 'run', 'learning': {'ok': True, 'complete': False},
                        'receipts': [{'task_id': 'task', 'stage': 'executing'}]}}
    result = coding.request_diagnostics(request, {'run_id': 'run'})
    assert result['complete'] and result['learning_ok'] and not result['learning_complete']
    assert result['requests'][0]['schemaTokens'] == 14600
    assert result['requests'][0]['tokensIn'] == 24000
    assert 'private prompt' not in json.dumps(result)


def test_missing_request_evidence_is_unknown():
    def request(method, path, body, key):
        if path.endswith('/requests'):
            return {'coverage': 'expired', 'requests': []}
        return {'run': {'run_id': 'run', 'receipts': [{'task_id': 'task'}]}}
    assert not coding.request_diagnostics(request, {'run_id': 'run'})['complete']


@pytest.mark.parametrize('budget', [0, 4000, 128000])
def test_offline_preflight_pins_schema_policy(budget, capsys):
    assert coding.main(['--executor-schema-budget-tokens', str(budget)]) == 0
    assert json.loads(capsys.readouterr().out)['limits']['executor_schema_budget_tokens'] == budget


@pytest.mark.parametrize('budget', ['-1', '128001'])
def test_invalid_schema_policy_refuses_before_access(budget):
    with pytest.raises(SystemExit):
        coding.main(['--executor-schema-budget-tokens', budget])
