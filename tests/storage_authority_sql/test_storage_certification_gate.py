"""Adversarial tests for release evidence, separate from storage correctness."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.certify_storage_backends import validate_evidence

# Built-graph asset paths exist only after `npm run build:frontend`;
# the audit dead-path scan must not treat them as missing source.
_AUDIT_SYNTHETIC_REPO_PATHS = {'static/vite/assets/app.js'}

pytestmark = pytest.mark.unit


def good_evidence():
    return {'collected': ['test.py::test_example'], 'deselected': [],
            'pytest_exitstatus': 0,
            'reports': [{'nodeid': 'test.py::test_example', 'when': phase,
                         'outcome': 'passed', 'xfail': False}
                        for phase in ('setup', 'call', 'teardown')]}


def test_complete_evidence_accepts_only_full_execution():
    assert validate_evidence(good_evidence()) == 1


@pytest.mark.parametrize('attack', [
    'empty', 'duplicate', 'deselect', 'skip', 'xfail', 'failure',
    'no_call', 'no_teardown', 'repeat_call', 'foreign_case', 'nonzero_exit'])
def test_weakened_evidence_cannot_certify(attack):
    data = copy.deepcopy(good_evidence())
    if attack == 'empty': data['collected'] = []
    elif attack == 'duplicate': data['collected'] *= 2
    elif attack == 'deselect': data['deselected'] = ['test.py::test_required']
    elif attack == 'skip': data['reports'][1]['outcome'] = 'skipped'
    elif attack == 'xfail': data['reports'][1]['xfail'] = True
    elif attack == 'failure': data['reports'][2]['outcome'] = 'failed'
    elif attack == 'no_call': data['reports'].pop(1)
    elif attack == 'no_teardown': data['reports'].pop()
    elif attack == 'repeat_call': data['reports'].append(data['reports'][1])
    elif attack == 'foreign_case': data['reports'][1]['nodeid'] = 'other.py::test_other'
    elif attack == 'nonzero_exit': data['pytest_exitstatus'] = 1
    with pytest.raises(ValueError):
        validate_evidence(data)


@pytest.mark.parametrize('attack', ['none', 'skip', 'xfail', 'xpass', 'deselect'])
def test_real_pytest_plugin_refuses_incomplete_gate(tmp_path, attack):
    root = Path(__file__).resolve().parents[2]
    decorators = {'skip': '@pytest.mark.skip(reason="injected")',
                  'xfail': '@pytest.mark.xfail(reason="injected")',
                  'xpass': '@pytest.mark.xfail(reason="injected")'}
    assertion = 'False' if attack == 'xfail' else 'True'
    (tmp_path / 'test_probe.py').write_text(
        f'import pytest\n{decorators.get(attack, "")}\n'
        f'def test_required():\n    assert {assertion}\n'
        'def test_other():\n    assert True\n')
    (tmp_path / 'pytest.ini').write_text('[pytest]\n')
    evidence = tmp_path / 'evidence.json'
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
        PYTHONPATH=str(root), TOFU_STORAGE_CERTIFICATION_EVIDENCE=str(evidence))
    environment.pop('PYTEST_ADDOPTS', None)
    command = [sys.executable, '-m', 'pytest', '-p', 'tests.storage_certification_plugin',
               '-c', 'pytest.ini', 'test_probe.py', '-q']
    if attack == 'deselect': command += ['-k', 'other']
    result = subprocess.run(command, cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=30)
    assert (result.returncode == 0) == (attack == 'none'), result.stdout + result.stderr
    data = json.loads(evidence.read_text())
    if attack == 'none': assert validate_evidence(data) == 2
    else:
        with pytest.raises(ValueError): validate_evidence(data)


def test_differential_cannot_hide_missing_clock_or_two_failures():
    from tests.tofudb.test_tofudb_differential import Outcome, _normalize, _same_success_shape, _volatile_signature
    assert _normalize({'created_at_ms': 123}) == {'created_at_ms': 123}
    assert _volatile_signature({'created_at_ms': 123}) != _volatile_signature({})
    assert _volatile_signature({'created_at_ms': 123}) != _volatile_signature({'created_at_ms': '123'})
    assert _volatile_signature({'created_at_ms': 123}) == _volatile_signature({'created_at_ms': 456})
    assert not _same_success_shape(Outcome(ok=False, error_code='database_integrity'),
                                   Outcome(ok=False, error_code='database_integrity'))


@pytest.mark.parametrize('attack', ['none', 'original', 'candidate'])
def test_runner_rejects_binary_replacement(monkeypatch, tmp_path, attack):
    import scripts.certify_storage_backends as gate
    from types import SimpleNamespace

    (tmp_path / 'contracts').mkdir()
    (tmp_path / 'contracts/storage_harness_certification_v1.json').write_text(json.dumps({
        'scope': 'test', 'uncovered_claims': [], 'stages': [
            {'name': 'probe', 'backend': 'sqlite', 'tests': ['test.py'], 'marker': 'unit',
             'required_cases': ['test.py::test_example']}]}))
    binary = tmp_path / 'engine'
    binary.write_bytes(b'original')
    binary.chmod(0o700)
    report = tmp_path / 'report'
    monkeypatch.setattr(gate, 'ROOT', tmp_path)
    monkeypatch.setattr(gate, 'verification_sources', lambda: {'test.py': 'source-digest'})
    monkeypatch.setattr(sys, 'argv', ['gate', '--binary', str(binary), '--report-dir', str(report)])

    def run(*args, **kwargs):
        environment = kwargs['env']
        Path(environment['TOFU_STORAGE_CERTIFICATION_EVIDENCE']).write_text(json.dumps(good_evidence()))
        if attack != 'none':
            target = binary if attack == 'original' else Path(environment['TOFUDB_BIN'])
            target.chmod(0o700)
            target.write_bytes(b'replaced')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gate.subprocess, 'run', run)
    assert gate.main() == (0 if attack == 'none' else 1)
    assert json.loads((report / 'summary.json').read_text())['passed'] == (attack == 'none')


def test_harness_manifest_requires_both_authorities_and_discloses_scope():
    manifest = json.loads((Path(__file__).resolve().parents[2] /
        'contracts/storage_harness_certification_v1.json').read_text())
    assert manifest['contract'] == 'tofu.storage-harness-certification/v1'
    # Disclosure discipline: the manifest must always carry the claims
    # ledger (an empty list is the valid "all claims covered" state reached
    # by the completed soak) plus a human-readable scope statement.
    assert 'uncovered_claims' in manifest
    assert manifest['scope']
    assert {stage['backend'] for stage in manifest['stages'] if stage['marker'] == 'visual'} == {'sqlite', 'tofudb'}
    for stage in manifest['stages']:
        assert stage['tests'] and stage['required_cases']
        assert all(any(case == selector or case.startswith(selector + '::')
                       or case.startswith(selector + '[') for selector in stage['tests'])
                   for case in stage['required_cases'])


def test_differential_does_not_equate_boolean_with_number_or_arbitrary_uuid_text():
    from tests.tofudb.test_tofudb_differential import _json_equal, _tokenize_server_ids
    assert not _json_equal({'enabled': True}, {'enabled': 1})
    assert not _json_equal([False], [0])
    a, b = '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222'
    assert _tokenize_server_ids({'content': a}, {}) != _tokenize_server_ids({'content': b}, {})
    assert _tokenize_server_ids({'turnId': a}, {}) == _tokenize_server_ids({'turnId': b}, {})
    # A token learned from a real identity must not rewrite later content.
    tokens = {}
    _tokenize_server_ids({'turnId': a}, tokens)
    assert _tokenize_server_ids({'content': a}, tokens) == {'content': a}
    assert _tokenize_server_ids([a], tokens) == [a]


def test_verification_fingerprint_detects_served_asset_and_route_drift(monkeypatch, tmp_path):
    import scripts.certify_storage_backends as gate
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / '.gitignore').write_text('static/vite/\nscripts/generated_storage_operations.py\n')
    files = ['routes/conversations.py', 'static/vite/assets/app.js',
             'static/vite/manifest.json', 'templates/index.html',
             'scripts/generated_storage_operations.py']
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('original')
    monkeypatch.setattr(gate, 'ROOT', tmp_path)
    before = gate.verification_sources()
    assert set(files) <= before.keys()
    for name in files:
        path = tmp_path / name
        path.write_text('changed')
        assert gate.verification_sources()[name] != before[name]
        path.write_text('original')
    (tmp_path / files[1]).unlink()
    assert files[1] not in gate.verification_sources()


def test_history_proof_normalization_requires_independent_validation():
    import hashlib
    from tests.tofudb.test_tofudb_differential import Outcome, _verify_context_history, _comparison_value
    class Driver:
        def execute(self, operation, payload, command_id):
            assert operation == 'turn.list' and payload['user_id'] == 7
            return Outcome(True, [{'turnId': 'target'}])
    payload = {'conversation_id': 'test', 'user_id': 7, 'turn_id': 'target'}
    good = {'version': 2, 'count': 0, 'digest': hashlib.sha256(b'').hexdigest()}
    outcome = Outcome(True, {'historyProof': good.copy(), 'other': {'digest': 'unchanged'}})
    _verify_context_history(Driver(), payload, outcome)
    compared = _comparison_value(outcome)
    assert compared['historyProof']['digest'] != good['digest']
    assert outcome.value['historyProof'] == good
    assert compared['other']['digest'] == 'unchanged'
    for change in ({'digest': '0' * 64}, {'count': 1}, {'count': False}, {'version': True}):
        forged = Outcome(True, {'historyProof': good | change})
        with pytest.raises(AssertionError):
            _verify_context_history(Driver(), payload, forged)
        assert not forged.verified_history_proofs
        assert _comparison_value(forged) == forged.value
