"""Resident execution generations follow executable source and launch policy."""
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _source(project):
    (project / 'lib').mkdir()
    (project / 'lib/__init__.py').write_text('')
    (project / 'lib/task.py').write_text('VALUE = 1\n')
    (project / 'server.py').write_text('APPLICATION = 1\n')
    (project / 'frontend').mkdir()
    (project / 'frontend/view.ts').write_text('view')
    return project


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
    module = project / 'lib/lazy.py'
    module.write_text('LAZY = True\n')
    assert plan != prepare_plan(project, {}, python='/env/python')
    module.unlink()
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
