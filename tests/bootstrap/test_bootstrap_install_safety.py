"""Installer boundaries: verified targets, atomic validation and bounded runner."""
import json
from types import SimpleNamespace

import pytest

from bootstrap_pkg import install

pytestmark = pytest.mark.unit


@pytest.fixture
def harness(monkeypatch, tmp_path):
    events, calls = [], []
    (tmp_path / 'requirements.txt').write_text(
        'requests>=2.31\nlxml>=5.3\nlxml_html_clean>=0.4\nmcp>=2,<3\n'
        'pip>=1\nsetuptools>=1\nwheel>=1\n', encoding='utf-8')
    monkeypatch.setattr(install, 'BASE_DIR', str(tmp_path))
    monkeypatch.setattr(install.sys, 'prefix', str(tmp_path))
    monkeypatch.setattr(install.runtime, '_bus', SimpleNamespace(
        emit=lambda kind, data: events.append((kind, data))))
    monkeypatch.setattr('bootstrap_pkg.runtime_checks.check_javascript_runtime',
                        lambda **kwargs: (True, 'verified'))
    monkeypatch.setattr('bootstrap_pkg.runtime_repair.repair_javascript_runtime',
                        lambda detail, **kwargs: (False, detail))
    state = SimpleNamespace(rc=0, tail='bounded redacted tail', calls=calls,
                            events=events, prefix=tmp_path)

    def run(command, **kwargs):
        calls.append((command, kwargs))
        kwargs['emit'](state.tail)
        return state.rc, state.tail

    monkeypatch.setattr(install, 'run_command', run)
    monkeypatch.setattr(install, '_find_conda_exe', lambda: '/fake/conda')
    return state


@pytest.mark.parametrize('packages', [
    [], 'requests', None, [None], [1], ['requests'] * 33,
    ['requests', '--target=/tmp/elsewhere'], ['requests', '-e'],
    ['requests', 'https://example.invalid/pkg.whl'], ['requests', './pkg'],
    ['requests', '/tmp/pkg'], ['requests', r'C:\pkg'],
    ['requests', 'requests @ https://example.invalid/pkg'],
    ['requests', 'conda:postgresql'], ['requests', 'pip>=1'],
    ['requests', 'SETUPTOOLS[extra]==1'], ['requests', 'Wheel!=2'],
    ['requests', 'undeclared-package'], ['requests', 'a' * 257],
    ['requests\n--upgrade'], ['requests;python_version>="3"'],
    ['requests===anything'], ['requests>=garbage'], ['requests[--bad]'],
])
def test_rejects_entire_suggestion_without_spawning(harness, packages):
    success, message = install._pip_install(packages)
    assert not success and message
    assert harness.calls == []
    assert harness.events == []


@pytest.mark.parametrize('package', [
    'requests', 'requests>=2.31,<3', 'lxml[html_clean]>=5.3',
    'LXML.HTML_CLEAN==0.4', 'requests!=2.0.*', 'requests~=2.31',
    'charset-normalizer', 'black', 'mypy',
])
def test_valid_declared_and_known_packages_use_runner(harness, package):
    assert install._pip_install([package]) == (True, harness.tail)
    command, options = harness.calls[0]
    assert command == [install.sys.executable, '-m', 'pip', 'install',
                       '--no-input', package]
    assert options['cwd'] == str(harness.prefix)
    assert options['timeout'] == install.PIP_TIMEOUT
    assert ('pip_output', harness.tail) in harness.events


def test_inherited_conda_activation_does_not_own_current_interpreter(harness, monkeypatch, tmp_path):
    foreign = tmp_path / 'foreign'
    (foreign / 'conda-meta').mkdir(parents=True)
    monkeypatch.setenv('CONDA_PREFIX', str(foreign))
    monkeypatch.setenv('CONDA_DEFAULT_ENV', 'base')
    monkeypatch.setattr(install.sys, 'executable', '/miniconda/bin/python')
    assert not install._running_in_conda_env()
    assert not install._try_conda_install_deps()
    assert harness.calls == []


@pytest.mark.parametrize('rc', [0, 1, 124, 127])
def test_conda_only_modifies_verified_current_prefix(harness, monkeypatch, rc):
    (harness.prefix / 'conda-meta').mkdir()
    monkeypatch.setenv('CONDA_PREFIX', '/unrelated/base')
    monkeypatch.setenv('CONDA_DEFAULT_ENV', 'base')
    harness.rc = rc
    assert install._running_in_conda_env()
    assert install._try_conda_install_deps() is (rc == 0)
    assert len(harness.calls) == 1
    command, options = harness.calls[0]
    assert command[:4] == ['/fake/conda', 'install', '-p', str(harness.prefix)]
    assert not {'uninstall', 'update', 'config', 'base', '-n'} & set(command)
    assert options['timeout'] == 600
    phases = [json.loads(data) for kind, data in harness.events if kind == 'phase']
    assert phases[-1]['status'] == ('done' if rc == 0 else 'error')


@pytest.mark.parametrize('flow', ['requirements', 'pip', 'mypyc'])
@pytest.mark.parametrize('rc', [0, 124, 127])
def test_all_pip_flows_delegate_and_report_failure(harness, flow, rc):
    harness.rc = rc
    if flow == 'requirements':
        result = install._try_requirements_txt()
        assert '-r' in harness.calls[0][0]
    elif flow == 'pip':
        result, output = install._pip_install(['requests'])
        assert output == harness.tail
    else:
        result = install._try_fix_mypyc("No module named 'abc123__mypyc'")
        assert '--force-reinstall' in harness.calls[0][0]
        assert 'charset-normalizer' in harness.calls[0][0]
    assert result is (rc == 0)
    assert len(harness.calls) == 1
    assert harness.calls[0][1]['timeout'] == install.PIP_TIMEOUT
    if flow != 'pip':
        phases = [json.loads(data) for kind, data in harness.events if kind == 'phase']
        assert phases[-1]['status'] == ('done' if rc == 0 else 'error')


def test_conda_failure_falls_back_without_preemptive_uninstall(harness):
    (harness.prefix / 'conda-meta').mkdir()
    harness.rc = 124
    assert not install._try_requirements_txt()
    assert len(harness.calls) == 2
    assert harness.calls[1][0][1:4] == ['-m', 'pip', 'install']
    assert all('uninstall' not in command for command, _ in harness.calls)


def test_requirements_success_does_not_hide_unusable_native_runtime(harness, monkeypatch):
    monkeypatch.setattr('bootstrap_pkg.runtime_checks.check_javascript_runtime',
                        lambda **kwargs: (False, 'GLIBC_2.18 not found'))
    assert not install._try_requirements_txt()
    phases = [json.loads(data) for kind, data in harness.events if kind == 'phase']
    assert phases[-1]['status'] == 'error'
    assert 'GLIBC_2.18' in phases[-1]['detail']


def test_conda_requirements_success_still_requires_runtime_verification(harness, monkeypatch):
    (harness.prefix / 'conda-meta').mkdir()
    monkeypatch.setattr('bootstrap_pkg.runtime_checks.check_javascript_runtime',
                        lambda **kwargs: (False, 'GLIBC_2.28 not found'))
    assert not install._try_requirements_txt()
    phases = [json.loads(data) for kind, data in harness.events if kind == 'phase']
    assert phases[-1]['status'] == 'error'
    assert len(harness.calls) == 1
