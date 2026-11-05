"""Pi's installed Harbor adapter receives pinned, reproducible launch intent.

Exercises the existing job builder and dry-run artifact writer without invoking
Harbor, a model, or a sandbox. Runtime parity still needs live trial evidence.
"""
from __future__ import annotations

from dataclasses import replace
import json

import pytest

from evaluations.swebench.harbor_runner import (
    HarborRunSpec, build_job_config, start_harbor_run,
)
from evaluations.swebench.rootless_qemu import RootlessQemuSettings

pytestmark = pytest.mark.unit


def _spec(root, **overrides):
    values = dict(
        agent='pi', models=('anthropic/model-for-contract-test',),
        backend='docker', output_root=root / 'runs',
        benchmark='terminal-bench-2.1', limit=1, attempts=1,
        reasoning_effort='high', harbor_bin='missing-harbor-test',
    )
    values.update(overrides)
    return HarborRunSpec(**values)


@pytest.mark.parametrize('effort', ['off', 'minimal', 'low', 'medium', 'high', 'xhigh'])
def test_pi_maps_reasoning_to_the_upstream_thinking_flag(host_tmp_path, effort):
    spec = _spec(host_tmp_path, reasoning_effort=effort)
    config = build_job_config(spec, run_dir=host_tmp_path / 'run', run_id='pi')
    assert config['agents'][0]['kwargs'] == {'version': '1.4.0', 'thinking': effort}


@pytest.mark.parametrize('version', ['latest', '^1.4.0', '0.65.0'])
def test_pi_refuses_an_unreviewed_or_unpinned_version(host_tmp_path, version):
    with pytest.raises(ValueError, match='Pi.*version'):
        _spec(host_tmp_path, agent_version=version).validate()


@pytest.mark.parametrize('effort', [None, 'max', 'ultra'])
def test_pi_requires_an_explicit_supported_thinking_level(host_tmp_path, effort):
    with pytest.raises(ValueError, match='Pi.*thinking'):
        _spec(host_tmp_path, reasoning_effort=effort).validate()


@pytest.mark.parametrize('model', ['bare-model', '/model', 'provider/', 'provider/model;id'])
def test_pi_model_is_an_explicit_safe_provider_model_pair(host_tmp_path, model):
    with pytest.raises(ValueError, match='Pi.*provider/model'):
        _spec(host_tmp_path, models=(model,)).validate()


def test_pi_dry_run_records_the_effective_version_without_claiming_a_score(
        host_tmp_path, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'synthetic-test-key-never-serialize')
    spec = _spec(host_tmp_path, secret_env=('ANTHROPIC_API_KEY',))
    code, run_dir = start_harbor_run(spec, dry_run=True)
    assert code == 0
    manifest = json.loads((run_dir / 'manifest.json').read_text())
    config = json.loads((run_dir / 'job-config.json').read_text())
    assert manifest['agent_version'] == '1.4.0'
    assert manifest['reasoning_effort'] == 'high'
    assert manifest['status'] == 'prepared'
    assert config['agents'][0]['env'] == {'ANTHROPIC_API_KEY': '${ANTHROPIC_API_KEY}'}
    assert config['agents'][0]['kwargs']['thinking'] == 'high'
    assert 'synthetic-test-key-never-serialize' not in json.dumps([manifest, config])
    assert not (run_dir / 'result.json').exists()


def test_pi_does_not_bypass_the_rootless_credential_boundary(host_tmp_path):
    # Validation rejects the credential boundary before inspecting VM artifacts.
    from tests.evaluations.test_opencode_formal_runtime import _runtime_fixture

    settings, _ = _runtime_fixture(host_tmp_path)
    assert isinstance(settings, RootlessQemuSettings)
    spec = replace(_spec(host_tmp_path), backend='rootless-qemu', rootless_qemu=settings)
    with pytest.raises(ValueError, match='credential-safe'):
        spec.validate()
