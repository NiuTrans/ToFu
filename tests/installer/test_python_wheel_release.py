"""Release certification installs only the admitted lock in an empty interpreter."""
from pathlib import Path
import shutil
import subprocess

import pytest

from bootstrap_pkg import python_wheels
from scripts import build_python_wheels
from tests.installer.test_python_wheels import RUNTIME, wheel

pytestmark = pytest.mark.unit


@pytest.mark.skipif(shutil.which('uv') is None, reason='real uv offline installation')
def test_release_certification_ignores_unpinned_project_resolver_constraints(tmp_path, monkeypatch):
    root = tmp_path / 'release source'
    root.mkdir()
    (root / 'VERSION').write_text('1.0.0\n')
    (root / 'install.sh').write_text('# fixture\n')
    (root / 'requirements.txt').write_text('demo>=1\n')
    (root / 'pyproject.toml').write_text('[tool.uv]\nconstraint-dependencies = ["demo>=1"]\n')
    source_wheel = wheel(root / 'local')
    monkeypatch.chdir(root)
    monkeypatch.setenv('UV_OFFLINE', '1')
    monkeypatch.setenv('PIP_NO_INDEX', '1')
    monkeypatch.setattr(build_python_wheels, 'prepare_search_wheel', lambda *args: source_wheel)
    monkeypatch.setattr(python_wheels, 'identity', lambda python: RUNTIME)

    def prepare(python, requirements, house):
        stage = house.parent / 'admitted'
        artifact = wheel(stage)
        (stage / 'install.lock').write_text(f'demo==1.0 --hash=sha256:{python_wheels.digest(artifact)}\n')
        return python_wheels.publish(stage, house, requirements, RUNTIME)

    monkeypatch.setattr(build_python_wheels, 'prepare_python_wheels', prepare)
    run = subprocess.run

    def controlled_boundaries(command, **kwargs):
        if command[:2] == ['uv', 'export']:
            Path(command[command.index('--output-file') + 1]).write_text('demo==1.0\n')
            return subprocess.CompletedProcess(command, 0)
        if '-c' in command:
            # Replace the unrelated application canary with the real synthetic
            # package's metadata read in the newly created, installed env.
            command = [command[0], '-I', '-c',
                       'from importlib.metadata import version; assert version("demo") == "1.0"']
        elif command[-1].endswith(('verify_pdf_stack.py', 'bootstrap_pkg.runtime_checks')):
            return subprocess.CompletedProcess(command, 0)
        return run(command, **kwargs)

    monkeypatch.setattr(build_python_wheels.subprocess, 'run', controlled_boundaries)
    archive = build_python_wheels.build(root, tmp_path / 'release')
    assert archive.is_file() and archive.with_name(archive.name + '.sha256').is_file()
