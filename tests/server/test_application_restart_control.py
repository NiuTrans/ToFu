"""Settings restart must load disk code rather than serving-process imports."""
import json
import os
from pathlib import Path
import shutil
import sys
import types

import pytest

from serverctl_pkg import application_restart as control
from serverctl_pkg.native_engine import observe_preparation

pytestmark = pytest.mark.unit


@pytest.fixture
def target(tmp_path, monkeypatch):
    # The preparation stub mutates the parent process environment just like the
    # real restart port; retain that assertion without leaking its synthetic pin.
    monkeypatch.setenv('TOFUDB_EXPECTED_SHA256', os.environ.get('TOFUDB_EXPECTED_SHA256', '0' * 64))
    package = tmp_path / 'serverctl_pkg'
    package.mkdir()
    (package / '__init__.py').write_text('')
    for name in ('application_restart.py', 'native_engine.py'):
        shutil.copyfile(Path(control.__file__).parent / name, package / name)
    roles = tmp_path / 'lib'
    roles.mkdir()
    (roles / '__init__.py').write_text('')
    shutil.copyfile(Path(control.__file__).parents[1] / 'lib/process_roles.py',
                    roles / 'process_roles.py')
    (tmp_path / 'serverctl.py').write_text('''
import os
from serverctl_pkg.native_engine import _report_stage
def prepare_native_engine_artifact(reason, *, project):
    _report_stage('compiling', 'target generation engine preparation')
    os.environ['TOFUDB_EXPECTED_SHA256'] = 'new-pin'
    return ''
def prepare_source_frontend_artifact(reason, *, capture_output):
    assert capture_output is True
    return ''
''')
    (tmp_path / 'supervisor_protocol.py').write_text('''
def request_deferred_worker_restart(project, **kwargs):
    return {'ok': True, 'generation': 'first', 'project': project, 'request': {
        key: value for key, value in kwargs.items() if key != 'environment'}}
''')
    return tmp_path


def test_preparation_ignores_cached_modules_and_relays_progress(target, monkeypatch):
    monkeypatch.setitem(sys.modules, 'serverctl', types.SimpleNamespace())
    monkeypatch.setenv('TOFUDB_EXPECTED_SHA256', 'old-pin')
    phases = []
    with observe_preparation(phases.append):
        result = control.run_application_restart_control(str(target), stage='prepare')
    assert result['ok'] is True
    assert os.environ['TOFUDB_EXPECTED_SHA256'] == 'new-pin'
    assert 'compiling' in [frame['phase'] for frame in phases]
    assert 'frontend' in [frame['phase'] for frame in phases]


def test_delegation_uses_latest_disk_generation(target, monkeypatch):
    monkeypatch.setitem(sys.modules, 'supervisor_protocol', types.SimpleNamespace())
    first = control.run_application_restart_control(str(target), stage='delegate')
    module = target / 'supervisor_protocol.py'
    module.write_text(module.read_text().replace('first', 'updated-generation'))
    second = control.run_application_restart_control(
        str(target), stage='delegate', reason='manual', requested_at=123.5)
    assert first['generation'] == 'first'
    assert second['generation'] == 'updated-generation'
    assert second['project'] == str(target)
    assert second['request'] == {'source': 'application-manual', 'requested_at': 123.5}


def test_preparation_failure_keeps_build_diagnostics(target):
    (target / 'serverctl.py').write_text('''
def prepare_native_engine_artifact(*args, **kwargs):
    return 'candidate engine rejected: synthetic compile diagnostic'
def prepare_source_frontend_artifact(*args, **kwargs):
    raise AssertionError('frontend must not run after an engine refusal')
''')
    with pytest.raises(RuntimeError, match='synthetic compile diagnostic'):
        control.run_application_restart_control(str(target), stage='prepare')


@pytest.mark.parametrize('receipt', ['', control.RESULT_PREFIX + 'null',
                                   control.RESULT_PREFIX + '{bad-json',
                                   control.RESULT_PREFIX + '{"ok": false}'])
def test_missing_or_invalid_receipt_is_not_success(target, monkeypatch, receipt):
    monkeypatch.setattr(control, '_run', lambda *args, **kwargs: receipt)
    with pytest.raises(RuntimeError):
        control.run_application_restart_control(str(target), stage='delegate')


def test_child_cannot_overwrite_concurrent_override_or_unrelated_environment(target, monkeypatch):
    monkeypatch.setenv('TOFUDB_BIN', 'before')
    def completed(*args, **kwargs):
        os.environ['TOFUDB_BIN'] = 'explicit-concurrent-override'
        return control.RESULT_PREFIX + json.dumps({'ok': True, 'environmentChanges': {
            'TOFUDB_BIN': {'before': 'before', 'after': 'child-pin'},
            'UNRELATED_RESTART_SETTING': {'before': None, 'after': 'ignored'},
        }})
    monkeypatch.setattr(control, '_run', completed)
    monkeypatch.delenv('UNRELATED_RESTART_SETTING', raising=False)
    control.run_application_restart_control(str(target), stage='prepare')
    assert os.environ['TOFUDB_BIN'] == 'explicit-concurrent-override'
    assert 'UNRELATED_RESTART_SETTING' not in os.environ


def test_timeout_reaps_control_child_with_diagnostics(target, monkeypatch):
    (target / 'supervisor_protocol.py').write_text('''
import time
def request_deferred_worker_restart(*args, **kwargs):
    print('synthetic stuck manager', flush=True)
    time.sleep(60)
''')
    monkeypatch.setattr(control, 'DELEGATION_TIMEOUT_SECONDS', 5)
    with pytest.raises(RuntimeError, match='Application restart delegate timed out') as error:
        control.run_application_restart_control(str(target), stage='delegate')
    assert 'synthetic stuck manager' in str(error.value)


def test_invalid_role_refuses_preparation_before_shutdown(target, monkeypatch):
    monkeypatch.setenv('TOFU_PROCESS_ROLE', 'invalid-role')
    with pytest.raises(RuntimeError, match='process role must be'):
        control.run_application_restart_control(str(target), stage='prepare')


def test_worker_role_does_not_build_frontend(target, monkeypatch):
    monkeypatch.setenv('TOFU_PROCESS_ROLE', 'worker')
    module = target / 'serverctl.py'
    module.write_text(module.read_text().replace("assert capture_output is True", "raise AssertionError('worker must not build frontend')"))
    assert control.run_application_restart_control(str(target), stage='prepare')['ok'] is True
