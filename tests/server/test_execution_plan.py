"""Resident execution generations follow executable source and launch policy."""
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _source(tmp_path):
    (tmp_path / 'lib').mkdir()
    (tmp_path / 'lib/__init__.py').write_text('')
    (tmp_path / 'lib/task.py').write_text('VALUE = 1\n')
    (tmp_path / 'server.py').write_text('APPLICATION = 1\n')
    (tmp_path / 'frontend').mkdir()
    (tmp_path / 'frontend/view.ts').write_text('view')
    return tmp_path


def test_generation_covers_source_configuration_and_interpreter_without_secrets(tmp_path):
    from serverctl_pkg.execution_plan import prepare_plan
    project = _source(tmp_path)
    environment = {'TOFU_PROVIDER_TOKEN': 'private-provider-token'}
    plan = prepare_plan(project, environment, python='/env/python')
    assert plan == prepare_plan(project, dict(environment), python='/env/python')
    assert 'private-provider-token' not in json.dumps(plan)
    assert plan != prepare_plan(project, {**environment, 'TOFU_PROVIDER_TOKEN': 'changed'}, python='/env/python')
    assert plan != prepare_plan(project, environment, python='/env/other-python')
    (project / 'frontend/view.ts').write_text('changed UI')
    assert plan == prepare_plan(project, environment, python='/env/python')
    (project / 'lib/task.py').write_text('VALUE = 2\n')
    assert plan != prepare_plan(project, environment, python='/env/python')


def test_new_and_deleted_lazy_modules_change_generation_but_runtime_data_does_not(tmp_path):
    from serverctl_pkg.execution_plan import prepare_plan
    project = _source(tmp_path)
    plan = prepare_plan(project, {}, python='/env/python')
    (project / 'lib/runtime_data').mkdir()
    (project / 'lib/runtime_data/task.py').write_text('user data')
    assert plan == prepare_plan(project, {}, python='/env/python')
    (project / 'lib/lazy.py').write_text('LAZY = True\n')
    assert plan != prepare_plan(project, {}, python='/env/python')
    (project / 'lib/lazy.py').unlink()
    assert plan == prepare_plan(project, {}, python='/env/python')


def test_source_change_while_reading_refuses_the_plan(tmp_path, monkeypatch):
    from serverctl_pkg.execution_plan import prepare_plan
    project = _source(tmp_path)
    original = Path.read_bytes

    def read_and_change(path):
        content = original(path)
        if path.name == 'task.py':
            path.write_text('VALUE = 999999\n')
        return content

    monkeypatch.setattr(Path, 'read_bytes', read_and_change)
    with pytest.raises(RuntimeError, match='changed during'):
        prepare_plan(project, {}, python='/env/python')


def test_preparation_checks_storage_ownership_before_a_service_can_be_stopped(tmp_path):
    from serverctl_pkg.execution_plan import prepare_plan
    project = _source(tmp_path)
    (project / 'lib/task.py').write_text("import sqlite3\nconn = sqlite3.connect('application.db')\n")
    with pytest.raises(RuntimeError, match='storage cutover refused'):
        prepare_plan(project, {}, python='/env/python')
    assert not (project / 'application.db').exists()


@pytest.mark.parametrize('field, source_only', [
    ('sourceSha256', True), ('sourceFiles', True),
    ('python', False), ('configurationSha256', False),
    ('environmentFingerprint', False), ('format', False),
])
def test_plan_admission_distinguishes_code_from_launch_policy(field, source_only):
    from serverctl_pkg.execution_plan import ExecutionSourceChanged, require_plan_match
    expected = {'format': 'tofu.execution-plan/v1', 'python': '/env/python',
        'sourceSha256': 'first', 'sourceFiles': 3, 'environmentFingerprint': 'installed',
        'configurationSha256': 'configuration'}
    require_plan_match(expected, dict(expected), stage='before boot')
    actual = {**expected, field: 4 if field == 'sourceFiles' else 'changed'}
    with pytest.raises(RuntimeError) as failure:
        require_plan_match(expected, actual, stage='before boot')
    assert isinstance(failure.value, ExecutionSourceChanged) is source_only
    assert field in str(failure.value)
    assert 'configuration' not in str(failure.value) or field == 'configurationSha256'


def test_unreadable_package_cannot_disappear_from_prepared_identity(tmp_path, monkeypatch):
    import os
    from serverctl_pkg.execution_plan import prepare_plan
    project = _source(tmp_path)
    original = os.scandir

    def unreadable(path):
        if Path(path) == project / 'lib':
            raise PermissionError('package temporarily inaccessible')
        return original(path)

    monkeypatch.setattr(os, 'scandir', unreadable)
    with pytest.raises(PermissionError, match='inaccessible'):
        prepare_plan(project, {}, python='/env/python')


def test_installs_into_the_interpreter_environment_change_generation_but_bytecode_does_not(tmp_path):
    import subprocess
    import sys
    from serverctl_pkg.execution_plan import prepare_plan
    project = tmp_path / 'project'
    project.mkdir()
    _source(project)
    environment = tmp_path / 'environment'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(environment)],
                   check=True, capture_output=True, timeout=300)
    python = str(environment / 'bin' / 'python')
    purelib = Path(subprocess.run(
        [python, '-I', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'],
        check=True, capture_output=True, text=True, timeout=120).stdout.strip())
    plan = prepare_plan(project, {}, python=python)
    (purelib / '__pycache__').mkdir()
    (purelib / '__pycache__' / 'provider.cpython-312.pyc').write_bytes(b'bytecode')
    assert plan == prepare_plan(project, {}, python=python)
    (purelib / 'provider-0.1.1.dist-info').mkdir()
    assert plan != prepare_plan(project, {}, python=python)


@pytest.mark.parametrize('package', ['tofu_db', 'tofu_workers', 'scripts', 'new_runtime_owner'])
def test_every_importable_runtime_package_changes_generation(tmp_path, package):
    from serverctl_pkg.execution_plan import prepare_plan
    project = _source(tmp_path)
    owner = project / package
    owner.mkdir()
    (owner / '__init__.py').write_text('')
    module = owner / 'lazy.py'
    module.write_text('VALUE = 1\n')
    plan = prepare_plan(project, {}, python='/env/python')
    module.write_text('VALUE = 2\n')
    assert plan != prepare_plan(project, {}, python='/env/python')
