"""Offline package bootstrap routing: known inputs, preserved authority.

No installer is invoked. Probes, route availability and child execution are
controlled seams; the named distributions are data, never test dependencies.
"""
from __future__ import annotations

import os
import pytest

from lib.project_mod import network_command as network
from lib.network_routes import Route

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_network(monkeypatch):
    monkeypatch.setenv('TOFU_RUN_NETWORK_ROUTE', 'auto')
    monkeypatch.delenv('TOFU_RUN_NETWORK_RESPECT_NO_PROXY', raising=False)
    network.reset_for_test()
    yield
    network.reset_for_test()


def environment(tmp_path, family='uv'):
    result = {'HOME': str(tmp_path / 'home'), 'XDG_CONFIG_HOME': str(tmp_path / 'user'),
              'XDG_CONFIG_DIRS': str(tmp_path / 'system'), 'PATH': os.defpath}
    if family == 'uv':
        result['UV_NO_CONFIG'] = '1'
    else:
        result['PIP_CONFIG_FILE'] = os.devnull
    return result


class Manager:
    def __init__(self, available=True):
        self.available = available
        self.calls = []
        self.reports = []

    def cached_candidates(self, url, routes):
        return []

    def candidates(self, url, routes, **kwargs):
        self.calls.append((url, tuple(routes), kwargs))
        return [next(r for r in routes if r.mode == 'proxy')] if self.available else []

    def report(self, url, route, ok, **kwargs):
        self.reports.append((url, ok, kwargs))

    def reset(self):
        pass


def install_routes(monkeypatch, *, available=True):
    direct = Route('direct', 'direct', 'direct')
    proxy = Route('proxy:test', 'configured test route', 'proxy', proxy_url='http://proxy.test:8080')
    manager = Manager(available)
    declarations = []

    def specs(url, **kwargs):
        declarations.append((url, kwargs))
        return [direct, proxy]

    monkeypatch.setattr(network, '_route_manager', manager)
    monkeypatch.setattr('lib.proxy.global_egress_route_specs', specs)
    return manager, declarations


@pytest.mark.parametrize('command', [
    'uv tool install inspect-ai --with openai --with inspect-evals',
    'uv pip install requests',
    'python3 -m pip install pytest',
])
def test_named_package_installs_reach_the_shared_route_owner(monkeypatch, tmp_path, command):
    manager, declarations = install_routes(monkeypatch)
    child = environment(tmp_path, 'pip' if '-m pip' in command else 'uv')
    original = dict(child)
    prepared = network.prepare_network_command(command, child)
    assert prepared is not None, 'A known registry install must not skip route preparation'
    assert prepared.selected
    assert prepared.target.display_origin == 'https://pypi.org:443'
    assert declarations[0][0] == 'https://pypi.org/'
    assert declarations[0][1]['include_bypassed'] is False
    assert not declarations[0][1].get('include_credentialed', False)
    assert len(manager.calls) == 1
    assert child == original, 'Preparation cannot mutate the child mapping'


def test_selected_package_route_preserves_internal_bypass(monkeypatch, tmp_path):
    install_routes(monkeypatch)
    child = environment(tmp_path)
    child['NO_PROXY'] = '.internal.example.com,127.0.0.1'
    prepared = network.prepare_network_command('uv tool install inspect-ai', child)
    assert prepared is not None and prepared.selected
    network.apply_network_environment(child, prepared)
    assert child['HTTPS_PROXY'] == 'http://proxy.test:8080'
    assert child['NO_PROXY'] == '.internal.example.com,127.0.0.1'
    assert 'no_proxy' not in child


def test_package_cdn_failure_does_not_poison_index_health(monkeypatch, tmp_path):
    manager, _ = install_routes(monkeypatch)
    prepared = network.prepare_network_command('uv tool install inspect-ai', environment(tmp_path))
    assert prepared is not None and prepared.selected
    output = network.finalize_network_command(
        prepared, '$ uv tool install inspect-ai\nerror: failed to download from files.pythonhosted.org: '
        'temporary failure in name resolution\n\n[exit code: 1]')
    assert 'network outcome: dns_failure' in output
    assert 'automatic_replay=false' in output
    assert manager.reports == [], 'An artifact-host failure is not an index-origin health fact'


@pytest.mark.parametrize('command,family', [
    ('/opt/test/bin/uv tool install inspect-ai', 'uv'),
    ('env UV_TOOL_DIR=.tools uv tool install inspect-ai', 'uv'),
    ("uv tool install 'inspect-ai==1.2.3' --with 'openai>=1'", 'uv'),
    ('uv tool install inspect-ai | head -5', 'uv'),
    ('uv --no-config tool install inspect-ai', 'uv'),
    ('pip install inspect-ai', 'pip'),
    ('python3.12 -m pip install inspect-ai', 'pip'),
    ('uv pip install inspect-ai', 'uv'),
])
def test_supported_literal_forms_share_routing(monkeypatch, tmp_path, command, family):
    install_routes(monkeypatch)
    prepared = network.prepare_network_command(command, environment(tmp_path, family), cwd=str(tmp_path))
    assert prepared is not None and prepared.selected
    assert prepared.target.host == 'pypi.org'


@pytest.mark.parametrize('command', [
    'uv tool install --offline inspect-ai',
    'uv tool install --no-index --find-links ./wheels inspect-ai',
    'uv tool install --with-requirements dependencies.txt inspect-ai',
    "uv tool install 'inspect-ai @ git+https://dev.example.test/repo.git'",
    'uv tool install ./checkout',
    'uv tool install inspect-ai --extra-index-url https://mirror.example.test/simple',
    'uv tool install inspect-ai --unknown-option value',
    'uv tool install inspect-ai && curl https://dev.example.test/',
    'curl https://dev.example.test/; uv tool install inspect-ai',
    'python3 -I -m pip install inspect-ai',
    'env -i uv tool install inspect-ai',
    'uv --directory elsewhere tool install inspect-ai',
    'uv tool install $PACKAGE',
    'uv tool install inspect-*',
    'uv tool install inspect-ai > output.txt',
    'npm install --ignore-scripts lodash',
])
def test_opaque_profiles_probe_nothing(monkeypatch, tmp_path, command):
    manager, declarations = install_routes(monkeypatch)
    child = environment(tmp_path, 'pip' if ' -m pip ' in command else 'uv')
    original = dict(child)
    assert network.prepare_network_command(command, child, cwd=str(tmp_path)) is None
    assert manager.calls == [] and declarations == []
    assert child == original


@pytest.mark.parametrize('addition', [
    {'HTTPS_PROXY': 'http://username:synthetic-token@manual.test:8080'},
    {'https_proxy': ''},
    {'PIP_PROXY': 'http://manual.test:8080'},
    {'UV_INDEX': 'https://extra.test/simple'},
    {'UV_EXTRA_INDEX_URL': 'https://extra.test/simple'},
    {'UV_FIND_LINKS': '/wheels'},
    {'UV_OFFLINE': '1'},
    {'UV_CONFIG_FILE': '/custom/uv.toml'},
    {'UV_TORCH_BACKEND': 'auto'},
    {'NO_PROXY': '.pypi.org'},
    {'no_proxy': '*'},
    {'NO_PROXY': 'internal.test', 'no_proxy': 'other.test'},
])
def test_final_child_configuration_wins_before_probe(monkeypatch, tmp_path, addition):
    manager, declarations = install_routes(monkeypatch)
    child = {**environment(tmp_path), **addition}
    assert network.prepare_network_command('uv tool install inspect-ai', child) is None
    assert manager.calls == [] and declarations == []
    assert all(child[name] == value for name, value in addition.items())


def test_explicit_internal_index_is_anonymous_and_not_replaced(monkeypatch, tmp_path):
    manager, declarations = install_routes(monkeypatch)
    child = environment(tmp_path)
    index = 'https://username:synthetic-token@packages.example.test/private/simple?signature=opaque'
    child['UV_DEFAULT_INDEX'] = index
    prepared = network.prepare_network_command('uv tool install inspect-ai', child)
    assert prepared is not None and prepared.selected
    assert prepared.target.probe_url == 'https://packages.example.test/'
    assert declarations[0][0] == 'https://packages.example.test/'
    assert child['UV_DEFAULT_INDEX'] == index
    assert 'synthetic-token' not in repr(prepared.target)
    assert 'signature' not in repr(prepared.target)
    assert len(manager.calls) == 1


def test_literal_cli_index_precedes_environment(monkeypatch, tmp_path):
    install_routes(monkeypatch)
    child = environment(tmp_path, 'pip')
    child['PIP_INDEX_URL'] = 'https://environment.test/simple'
    prepared = network.prepare_network_command(
        'pip install --index-url=https://selected.test/simple inspect-ai', child)
    assert prepared is not None and prepared.target.host == 'selected.test'
    assert child['PIP_INDEX_URL'] == 'https://environment.test/simple'


def test_uv_tool_ignores_project_config_but_not_user_config(monkeypatch, tmp_path):
    manager, declarations = install_routes(monkeypatch)
    child = environment(tmp_path)
    child.pop('UV_NO_CONFIG')
    # Avoid depending on the host's global configuration. This is metadata,
    # not a config-parser replacement or permission expansion.
    import lib.project_mod.package_network as packages
    original_absent = packages._absent
    monkeypatch.setattr(packages, '_absent',
                        lambda path: True if str(path).startswith('/etc/') else original_absent(path))
    (tmp_path / 'pyproject.toml').write_text('[tool.uv]\nindex-url="https://project.test/simple"\n')
    prepared = network.prepare_network_command('uv tool install inspect-ai', child, cwd=str(tmp_path))
    assert prepared is not None and prepared.target.host == 'pypi.org'
    config = tmp_path / 'user/uv/uv.toml'
    config.parent.mkdir(parents=True)
    config.write_text('index-url="https://private.test/simple"\n')
    manager.calls.clear(); declarations.clear()
    assert network.prepare_network_command('uv tool install inspect-ai', child, cwd=str(tmp_path)) is None
    assert manager.calls == [] and declarations == []


def test_uv_pip_unknown_project_config_keeps_original_semantics(monkeypatch, tmp_path):
    manager, declarations = install_routes(monkeypatch)
    child = environment(tmp_path)
    child.pop('UV_NO_CONFIG')
    (tmp_path / 'pyproject.toml').write_text('[tool.uv]\nindex-url="https://private.test/simple"\n')
    assert network.prepare_network_command('uv pip install inspect-ai', child, cwd=str(tmp_path)) is None
    assert manager.calls == [] and declarations == []


def test_pip_disable_config_is_explicit_not_injected(monkeypatch, tmp_path):
    manager, declarations = install_routes(monkeypatch)
    child = environment(tmp_path, 'pip')
    config = tmp_path / 'user/pip/pip.conf'
    config.parent.mkdir(parents=True)
    config.write_text('[global]\nindex-url=https://private.test/simple\n')
    prepared = network.prepare_network_command('pip install inspect-ai', child)
    assert prepared is not None and prepared.target.host == 'pypi.org'
    child.pop('PIP_CONFIG_FILE')
    child['VIRTUAL_ENV'] = str(tmp_path / 'venv')
    manager.calls.clear(); declarations.clear()
    assert network.prepare_network_command('pip install inspect-ai', child) is None
    assert manager.calls == [] and declarations == []
    assert 'PIP_CONFIG_FILE' not in child


@pytest.mark.parametrize('route_mode', ['direct', 'proxy'])
def test_package_projection_does_not_change_unrelated_environment(route_mode):
    route = Route('test', 'test', route_mode, proxy_url='http://proxy.test:8080' if route_mode == 'proxy' else '')
    target = network.NetworkTarget('package:uv:tool', 'https', 'pypi.org', 443, 'https://pypi.org/')
    prepared = network.PreparedNetworkCommand(target, route, 'forced', 0, ('test',))
    child = {'NO_PROXY': '.internal.test,localhost', 'PIP_REQUIRE_VIRTUALENV': '1', 'UV_TOOL_DIR': '.tools'}
    original = dict(child)
    network.apply_network_environment(child, prepared)
    assert all(child[key] == value for key, value in original.items())
    assert 'no_proxy' not in child


def test_package_command_executes_once_under_original_lifecycle(monkeypatch, tmp_path):
    from lib.project_mod import run_command as runner
    manager, _ = install_routes(monkeypatch)
    child = environment(tmp_path)
    child['NO_PROXY'] = '.internal.test,localhost'
    monkeypatch.setattr(runner, '_get_cmd_env', lambda *_args, **_kwargs: dict(child))
    executions = []

    def execute(command, full_command, timeout, base, **kwargs):
        executions.append((command, dict(kwargs['child_env'])))
        return '$ ' + command + '\nupstream installer fixture\n\n[exit code: 0]'

    monkeypatch.setattr(runner, '_run_command_simple', execute)
    original = 'uv tool install inspect-ai --with openai --with inspect-evals'
    output = runner.tool_run_command(str(tmp_path), original)
    assert len(executions) == 1 and executions[0][0] == original
    assert executions[0][1]['HTTPS_PROXY'] == 'http://proxy.test:8080'
    assert executions[0][1]['NO_PROXY'] == '.internal.test,localhost'
    assert '[exit code: 0]' in output and 'network route:' in output
    assert manager.reports == []


def test_no_package_route_never_spawns_installer(monkeypatch, tmp_path):
    from lib.project_mod import run_command as runner
    install_routes(monkeypatch, available=False)
    monkeypatch.setattr(runner, '_get_cmd_env', lambda *_args, **_kwargs: environment(tmp_path))
    called = []
    monkeypatch.setattr(runner, '_run_command_simple', lambda *_args, **_kwargs: called.append(True))
    output = runner.tool_run_command(str(tmp_path), 'uv tool install inspect-ai')
    assert called == []
    assert 'The command was not started' in output
    assert '[exit code:' not in output


def test_pip_virtual_env_is_not_interpreter_config_proof(monkeypatch, tmp_path):
    manager, declarations = install_routes(monkeypatch)
    child = environment(tmp_path, 'pip')
    child.pop('PIP_CONFIG_FILE')
    child['VIRTUAL_ENV'] = str(tmp_path / 'different-venv')
    import lib.project_mod.package_network as packages
    monkeypatch.setattr(packages, '_absent', lambda _path: True)
    assert network.prepare_network_command('pip install inspect-ai', child) is None
    assert manager.calls == [] and declarations == []
    assert 'PIP_CONFIG_FILE' not in child


def test_cidr_bypass_keeps_client_route_authority(monkeypatch, tmp_path):
    manager, declarations = install_routes(monkeypatch)
    child = environment(tmp_path)
    child.update(UV_DEFAULT_INDEX='https://10.1.2.3/simple', NO_PROXY='10.0.0.0/8,localhost')
    assert network.prepare_network_command('uv tool install inspect-ai', child) is None
    assert manager.calls == [] and declarations == []
    assert child['NO_PROXY'] == '10.0.0.0/8,localhost'


def test_package_route_reaches_one_real_child(monkeypatch, tmp_path):
    from lib.project_mod import run_command as runner
    import shlex

    manager, _ = install_routes(monkeypatch)
    launcher = tmp_path / 'uv'
    receipt = tmp_path / 'invocations'
    launcher.write_text(
        '#!/bin/sh\n'
        'test "$HTTPS_PROXY" = "http://proxy.test:8080" || exit 41\n'
        'test "$NO_PROXY" = ".internal.test,localhost" || exit 42\n'
        'test "$UV_NO_CONFIG" = "1" || exit 43\n'
        'test "$1 $2 $3" = "tool install inspect-ai" || exit 44\n'
        'printf "started\\n" >> ' + shlex.quote(str(receipt)) + '\n'
        'printf "isolated child accepted\\n"\n')
    launcher.chmod(0o700)
    child = environment(tmp_path)
    child['NO_PROXY'] = '.internal.test,localhost'
    monkeypatch.setattr(runner, '_get_cmd_env', lambda *_args, **_kwargs: dict(child))
    original = shlex.quote(str(launcher)) + ' tool install inspect-ai'
    output = runner.tool_run_command(str(tmp_path), original)
    assert '[exit code: 0]' in output and 'isolated child accepted' in output
    assert 'network scope: package-index probe' in output
    assert receipt.read_text() == 'started\n'
    assert manager.reports == []


def test_uv_pip_symlink_cwd_cannot_hide_parent_config(monkeypatch, tmp_path):
    manager, declarations = install_routes(monkeypatch)
    child = environment(tmp_path)
    child.pop('UV_NO_CONFIG')
    import lib.project_mod.package_network as packages
    original_absent = packages._absent
    monkeypatch.setattr(packages, '_absent',
                        lambda path: True if str(path).startswith('/etc/') else original_absent(path))
    physical = tmp_path / 'physical'
    directory = physical / 'child'
    directory.mkdir(parents=True)
    (physical / 'uv.toml').write_text('index-url="https://private.test/simple"\n')
    alias = tmp_path / 'aliases/work'
    alias.parent.mkdir()
    alias.symlink_to(directory, target_is_directory=True)
    assert network.prepare_network_command('uv pip install inspect-ai', child, cwd=str(alias)) is None
    assert manager.calls == [] and declarations == []


def test_uv_pip_unresolved_cwd_is_not_neutral(monkeypatch, tmp_path):
    manager, declarations = install_routes(monkeypatch)
    child = environment(tmp_path)
    child.pop('UV_NO_CONFIG')
    import lib.project_mod.package_network as packages
    monkeypatch.setattr(packages, '_absent', lambda _path: True)
    assert network.prepare_network_command(
        'uv pip install inspect-ai', child, cwd=str(tmp_path / 'missing')) is None
    assert manager.calls == [] and declarations == []
