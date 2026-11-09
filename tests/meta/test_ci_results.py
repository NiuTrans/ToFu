"""The merge gate must reject absent, failed, cancelled and unexpected skips."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / '.github/scripts/check_ci_results.py'
spec = importlib.util.spec_from_file_location('check_ci_results', SCRIPT)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


@pytest.mark.parametrize('evidence', [None, [], {}, {'unit': {}}, {'unit': None}])
def test_missing_results_fail(evidence):
    assert ci.dependency_failures(evidence, set())


@pytest.mark.parametrize('result', ['failure', 'cancelled', 'skipped', 'pending', 'unknown'])
def test_only_explicit_success_passes(result):
    assert ci.dependency_failures({'unit': {'result': result}}, set())


def test_nightly_skip_is_event_specific_and_never_hides_failure():
    passed = {'unit': {'result': 'success'}, 'nightly': {'result': 'skipped'}}
    assert not ci.dependency_failures(passed, {'nightly'})
    assert ci.dependency_failures(passed, set())
    assert ci.dependency_failures({'nightly': {'result': 'failure'}}, {'nightly'})
    assert ci.dependency_failures({'unit': {'result': 'success'}}, {'nightly'})


@pytest.mark.parametrize('payload,code', [('{', 1), ('{}', 1),
    (json.dumps({'unit': {'result': 'success'}}), 0),
    (json.dumps({'unit': {'result': 'cancelled'}}), 1)])
def test_cli_exit_status(payload, code):
    result = subprocess.run([sys.executable, str(SCRIPT)], input=payload,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == code, result.stderr


def test_workflow_aggregates_every_lane_and_keeps_merge_candidate_checkout():
    jobs = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']
    required = jobs['required']
    assert set(required['needs']) == set(jobs) - {'required'}
    assert required['if'] == '${{ always() }}'
    for job in jobs.values():
        assert not job.get('continue-on-error', False)
        for step in job.get('steps', []):
            if str(step.get('uses', '')).startswith('actions/checkout@'):
                assert 'ref' not in step.get('with', {})  # GitHub's merge candidate
    gate = next(step for step in required['steps'] if 'run' in step)
    assert gate['env']['NEEDS'] == '${{ toJSON(needs) }}'
    assert gate['env']['EVENT_NAME'] == '${{ github.event_name }}'
    assert gate['env']['VERIFICATION_PROFILE'] == '${{ needs.verification-scope.outputs.profile }}'
    # Execute the actual workflow shell with realistic dependency evidence.
    for event, profile in (('pull_request', 'incremental'), ('push', 'incremental'),
                           ('schedule', 'full'), ('workflow_dispatch', 'full'), ('push', 'full')):
        outcomes = {name: {'result': 'success'} for name in required['needs']}
        inactive = ({'test-affected'} if profile == 'full' else
                    {'test-unit', 'test-api', 'coverage', 'test-slow', 'visual-nightly', 'perf-nightly'})
        for name in inactive:
            outcomes[name]['result'] = 'skipped'
        assert run_workflow_gate(gate['run'], event, outcomes, profile).returncode == 0
        assert run_workflow_gate(gate['run'], event, outcomes, '').returncode != 0
        active = 'test-unit' if profile == 'full' else 'test-affected'
        outcomes[active]['result'] = 'skipped'
        assert run_workflow_gate(gate['run'], event, outcomes, profile).returncode != 0
        outcomes[active]['result'] = 'success'
        outcomes['frontend']['result'] = 'failure'
        assert run_workflow_gate(gate['run'], event, outcomes, profile).returncode != 0
        outcomes['frontend']['result'] = 'skipped'
        assert run_workflow_gate(gate['run'], event, outcomes, profile).returncode != 0


def run_workflow_gate(command, event, outcomes, profile):
    import os
    return subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', command], cwd=ROOT,
        env={**os.environ, 'NEEDS': json.dumps(outcomes), 'EVENT_NAME': event,
             'PATH': os.path.dirname(sys.executable) + os.pathsep + os.environ.get('PATH', ''),
             'VERIFICATION_PROFILE': profile},
        text=True, capture_output=True, timeout=10)


def test_browser_lane_uses_the_same_journeys_as_local_development():
    jobs = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']
    commands = [step.get('run', '') for step in jobs['e2e']['steps']]
    assert any('make test-e2e' in command for command in commands)


def test_incremental_and_qualification_jobs_have_explicit_scope_and_receipts():
    jobs = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']
    scope = jobs['verification-scope']
    assert any(step.get('with', {}).get('fetch-depth') == 0 for step in scope['steps'])
    affected = jobs['test-affected']
    assert "'incremental'" in affected['if']
    run = next(step for step in affected['steps'] if 'landing_verification check' in step.get('run', ''))
    assert '--json' in run['run'] and '--base' in run['run'] and '--expected-revision' in run['run']
    assert run['env']['BASE_REVISION'] == '${{ needs.verification-scope.outputs.base }}'
    assert run['env']['EXPECTED_REVISION'] == '${{ needs.verification-scope.outputs.head }}'
    artifacts = [step for step in affected['steps'] if str(step.get('uses', '')).startswith('actions/upload-artifact@')]
    assert any('${{ github.sha }}' in step['with']['name'] and
               step['with'].get('if-no-files-found') == 'error' for step in artifacts)
    for name in ('test-unit', 'test-api', 'test-slow', 'coverage', 'visual-nightly', 'perf-nightly'):
        assert "'full'" in jobs[name]['if']
