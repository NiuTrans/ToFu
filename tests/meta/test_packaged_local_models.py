"""Standalone manifests cannot drift or bypass the shared Model admission."""
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import gen_packaged_local_models as generator

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_packaged_local_models_are_current():
    result = subprocess.run(
        [sys.executable, str(ROOT / 'scripts/gen_packaged_local_models.py'), '--check'],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_generator_check_never_replaces_a_stale_output(tmp_path, monkeypatch):
    output = tmp_path / 'generated_local_models.py'
    output.write_text('stale catalog\n')
    monkeypatch.setattr(generator, 'OUTPUT', output)
    with pytest.raises(SystemExit, match='Regenerate admitted local models'):
        generator.main(['--check'])
    assert output.read_text() == 'stale catalog\n'
    generator.main([])
    assert generator.main(['--check']) == 0


def test_failed_admission_never_overwrites_a_published_manifest(tmp_path, monkeypatch):
    from lib.model_info import local_artifacts
    from lib.model_routing import ModelRoutingError

    output = tmp_path / 'generated_local_models.py'
    output.write_text('last admitted catalog\n')
    monkeypatch.setattr(generator, 'OUTPUT', output)

    def incomplete_catalog():
        raise ModelRoutingError('missing facts', kind='model_registration_incomplete')

    monkeypatch.setattr(local_artifacts, 'load_local_model_artifacts', incomplete_catalog)
    with pytest.raises(ModelRoutingError, match='missing facts'):
        generator.main([])
    assert output.read_text() == 'last admitted catalog\n'
