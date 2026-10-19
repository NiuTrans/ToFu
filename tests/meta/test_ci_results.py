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
    # Execute the actual workflow shell with realistic dependency evidence.
    for event in ('pull_request', 'push', 'schedule'):
        outcomes = {name: {'result': 'success'} for name in required['needs']}
        if event != 'schedule':
            for name, job in jobs.items():
                if job.get('if') == "github.event_name == 'schedule'":
                    outcomes[name]['result'] = 'skipped'
        assert run_workflow_gate(gate['run'], event, outcomes).returncode == 0
        outcomes['frontend']['result'] = 'failure'
        assert run_workflow_gate(gate['run'], event, outcomes).returncode != 0
        outcomes['frontend']['result'] = 'skipped'
        assert run_workflow_gate(gate['run'], event, outcomes).returncode != 0


def run_workflow_gate(command, event, outcomes):
    import os
    return subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', command], cwd=ROOT,
        env={**os.environ, 'NEEDS': json.dumps(outcomes), 'EVENT_NAME': event},
        text=True, capture_output=True, timeout=10)


def test_browser_lane_uses_the_same_journeys_as_local_development():
    jobs = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']
    commands = [step.get('run', '') for step in jobs['e2e']['steps']]
    assert any('make test-e2e' in command for command in commands)
