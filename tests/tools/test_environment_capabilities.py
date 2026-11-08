"""Environment-capability snapshot: tool exposure follows what this kernel has.

Executable specification for docs/modules/tools_execution.md "Environment
capabilities": one boot probe, explicit refresh events, declared tool needs,
withheld tools with actionable refusals, and the capabilities ``environment``
section the Android shell reads (docs/ANDROID_KERNELS.md).

tests/conftest.py pins every test to a snapshot with every capability present
and stubs the module's probe; these specs reset that state and probe through
``host_probe``, the real function captured at import.
"""

from __future__ import annotations

import asyncio
import os
import stat
import threading
from types import SimpleNamespace

import jsonschema
import pytest

from lib.tools.registry import ToolContext, ToolSpec, assemble_tool_list, register_tool_spec
from lib.tools import environment_capabilities as environment
from lib.tools.environment_capabilities import (
    ENVIRONMENT_SECTION_SCHEMA,
    EVERY_CAPABILITY_AVAILABLE,
    EnvironmentSnapshot,
)
from lib.tools.environment_capabilities import probe_environment as host_probe

pytestmark = pytest.mark.unit

GIT_TOOLS = {'integration_checkpoint', 'integration_submit'}
NODE_TOOLS = {'motion_video_check', 'motion_video_render'}


def _names(tools):
    return {tool['function']['name'] for tool in tools or ()}


def _ctx(**overrides):
    base = dict(
        cfg={}, task_id='env-test', project_path='', project_enabled=False,
        search_mode='multi', search_enabled=True, fetch_enabled=True,
        code_exec_enabled=False, browser_enabled=False, desktop_enabled=False,
        image_gen_enabled=False, human_guidance_enabled=False,
        scheduler_enabled=False, messages=[],
    )
    base.update(overrides)
    return ToolContext(**base)


def _project_ctx(tmp_path, **overrides):
    # Every plugin visible, so the bundled node-dependent family is assembled.
    return _ctx(project_path=str(tmp_path), project_enabled=True,
                enabled_plugins=None, **overrides)


def _stub(directory, name):
    path = directory / name
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def fake_host(tmp_path, monkeypatch):
    """A host whose PATH has python3/uv/uvx but neither git nor node/npx."""
    bin_dir = tmp_path / 'bin'
    home = tmp_path / 'home'
    bin_dir.mkdir()
    home.mkdir()
    for name in ('python3', 'uv', 'uvx'):
        _stub(bin_dir, name)
    monkeypatch.setattr('lib.tools.process_environment.bundled_toolchain_bins',
                        lambda: [])
    monkeypatch.setattr(environment, '_playwright_browser_present', lambda: True)
    return {'PATH': str(bin_dir), 'HOME': str(home)}


@pytest.fixture
def process_state(monkeypatch):
    """Isolate the process snapshot; restored after the test."""
    monkeypatch.setattr(environment, '_snapshot', None)
    monkeypatch.setattr(environment, '_stale', False)
    monkeypatch.setattr(environment, '_stale_reason', '')
    monkeypatch.setattr(environment, '_local_kernel', False)
    return environment


def _snapshot_for(environ, *, local_kernel=False):
    unavailable = host_probe(environ, local_kernel=local_kernel)
    return EnvironmentSnapshot(revision=1, local_kernel=local_kernel,
                               unavailable=unavailable)


# ── Probe ────────────────────────────────────────────────────────────

def test_probe_reports_git_and_node_missing_from_a_fake_path(fake_host):
    assert host_probe(fake_host, local_kernel=False) == {
        'git': 'not_installed', 'node': 'not_installed'}


def test_probe_finds_executables_in_persistent_tool_directories(fake_host, tmp_path):
    # ~/.local/bin is on every tool child's PATH (apply_tool_paths) even when
    # the server's own PATH lacks it, so a `uv tool install`ed CLI counts.
    local_bin = tmp_path / 'home' / '.local' / 'bin'
    local_bin.mkdir(parents=True)
    for name in ('git', 'node', 'npx'):
        _stub(local_bin, name)
    assert host_probe(fake_host, local_kernel=False) == {}


def test_lan_discovery_follows_the_responder_policy(fake_host):
    disabled = {**fake_host, 'TOFU_DESKTOP_LAN_DISCOVERY': '0'}
    loopback = {**fake_host, '_TOFU_RUNTIME_HOST': '127.0.0.1'}
    assert host_probe(disabled, local_kernel=False)[
        'lan_discovery'] == 'disabled'
    assert host_probe(loopback, local_kernel=False)[
        'lan_discovery'] == 'loopback_bind'
    assert 'lan_discovery' not in host_probe(
        {**fake_host, '_TOFU_RUNTIME_HOST': '0.0.0.0'}, local_kernel=False)


# ── Exposure ─────────────────────────────────────────────────────────

def test_git_and_node_tools_are_withheld_from_wire_and_catalog(fake_host, tmp_path):
    ctx = _project_ctx(tmp_path, environment=_snapshot_for(fake_host))
    wire, _ = assemble_tool_list(ctx)
    exposed = _names(wire) | _names(ctx.executable_tool_catalog)
    assert not (GIT_TOOLS | NODE_TOOLS) & exposed
    # Tools whose requirements are present keep their exposure.
    assert {'run_command', 'browser_preview_page', 'motion_video_probe',
            'send_project_message'} <= exposed


def test_everything_present_withholds_nothing(tmp_path):
    full = _project_ctx(tmp_path, environment=EVERY_CAPABILITY_AVAILABLE)
    wire, _ = assemble_tool_list(full)
    assert (GIT_TOOLS | NODE_TOOLS | {'run_command', 'browser_preview_page'}) <= (
        _names(wire) | _names(full.executable_tool_catalog))


def test_host_exposure_parity(tmp_path):
    """Before/after on this host: the gate removes only declared-unmet tools.

    "Before" is assembly with every capability available — the gate is then
    the identity — and "after" gates against this host's real probe. On a host
    with every capability the two are identical.
    """
    host = EnvironmentSnapshot(
        revision=1, local_kernel=False,
        unavailable=host_probe(os.environ, local_kernel=False))
    from lib.tools.registry import all_specs
    declared = {name: requirements for spec in all_specs()
                for name, requirements in spec.environment_requirements_by_name.items()}
    matrix = {
        'chat': lambda **kw: _ctx(**kw),
        'code_exec': lambda **kw: _ctx(code_exec_enabled=True, **kw),
        'project': lambda **kw: _ctx(project_path=str(tmp_path), project_enabled=True, **kw),
        'project_plugins': lambda **kw: _project_ctx(tmp_path, **kw),
    }
    changed = {}
    for label, make in matrix.items():
        before = make(environment=EVERY_CAPABILITY_AVAILABLE)
        after = make(environment=host)
        before_wire, _ = assemble_tool_list(before)
        after_wire, _ = assemble_tool_list(after)
        removed = ((_names(before_wire) | _names(before.executable_tool_catalog))
                   - (_names(after_wire) | _names(after.executable_tool_catalog)))
        assert all(host.missing(declared.get(name, ())) for name in removed), removed
        assert [n for n in (t['function']['name'] for t in before_wire)
                if n not in removed] == [t['function']['name'] for t in after_wire]
        if removed:
            changed[label] = sorted(removed)
    if not host.unavailable:
        assert changed == {}


def test_local_kernel_withholds_device_tools(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.desktop.is_desktop_agent_connected', lambda: True)
    local = EnvironmentSnapshot(revision=1, local_kernel=True, unavailable={
        'devices': 'local_kernel', 'lan_discovery': 'local_kernel'})
    desktop = dict(project_path=str(tmp_path), project_enabled=True,
                   desktop_enabled=True)
    full_ctx = _ctx(environment=EVERY_CAPABILITY_AVAILABLE, **desktop)
    local_ctx = _ctx(environment=local, **desktop)
    assemble_tool_list(full_ctx)
    assemble_tool_list(local_ctx)
    assert 'desktop_run_command' in _names(full_ctx.executable_tool_catalog)
    assert not {name for name in _names(local_ctx.executable_tool_catalog)
                if name.startswith('desktop_')}


def test_requirements_must_name_declared_tools_and_known_capabilities():
    def build(_ctx):
        return []

    with pytest.raises(ValueError, match='environment_requirements_by_name'):
        register_tool_spec(ToolSpec('_env_bad_name', build, provides=frozenset({'a'}),
                                    environment_requirements_by_name={'b': frozenset({'git'})}))
    with pytest.raises(ValueError, match='environment_requirements_by_name'):
        register_tool_spec(ToolSpec('_env_bad_cap', build, provides=frozenset({'a'}),
                                    environment_requirements_by_name={'a': frozenset({'gpu'})}))


# ── Actionable refusals ──────────────────────────────────────────────

def test_direct_call_returns_actionable_error(fake_host, tmp_path, process_state,
                                              monkeypatch):
    from lib.tasks_pkg.tool_dispatch.api import parse_tool_calls

    snapshot = _snapshot_for(fake_host)
    monkeypatch.setattr(environment, '_snapshot', snapshot)
    ctx = _project_ctx(tmp_path, environment=snapshot)
    wire, _ = assemble_tool_list(ctx)
    task = {'id': 'env-direct', 'convId': 'env-direct', 'model': 'test',
            'events': [], 'events_lock': threading.Lock(), 'toolRounds': [],
            'aborted': False, 'config': {}, '_tool_schema': wire,
            '_executable_tool_catalog': ctx.executable_tool_catalog,
            '_toolContractDocumentsByName': ctx.tool_contract_documents_by_name}
    parsed = parse_tool_calls({'content': '', 'tool_calls': [{
        'id': 'call-env', 'type': 'function', 'function': {
            'name': 'integration_submit', 'arguments': '{"summary":"done"}'}}]},
        task, round_num=1, tool_round_num=0, project_enabled=True)[0][0]
    rejection = parsed[5]['_rejected']
    assert parsed[5]['status'] == 'rejected'
    assert rejection['kind'] == 'tool_unavailable'
    assert rejection['missing_capabilities'] == ['git']
    assert 'git is not installed' in str(parsed)
    assert 'apt install git' in str(parsed)
    assert not task['aborted']


def test_gateway_call_returns_actionable_error(fake_host, tmp_path, process_state,
                                               monkeypatch):
    from lib.tools.gateway import normalize_gateway_call

    snapshot = _snapshot_for(fake_host)
    monkeypatch.setattr(environment, '_snapshot', snapshot)
    ctx = _project_ctx(tmp_path, environment=snapshot)
    assemble_tool_list(ctx)
    call, error = normalize_gateway_call(
        {'name': 'motion_video_render', 'arguments': {}},
        catalog=ctx.executable_tool_catalog, namespace_by_name={},
        gateway_call_id='gw', index=0, source='execute_tools')
    assert call is None
    assert error['code'] == 'environment_capability_missing'
    assert 'Node.js (node and npx) is not installed' in error['message']
    assert 'apt install nodejs npm' in error['retry_hint']


def test_local_kernel_refusal_names_userland_and_other_kernels():
    local = EnvironmentSnapshot(revision=1, local_kernel=True,
                                unavailable={'git': 'not_installed'})
    detail = environment.environment_unavailable_tool('integration_checkpoint', local)
    assert detail['missing_capabilities'] == ['git']
    assert 'not installed in this kernel' in detail['message']
    assert "phone's userland run `apt install git`" in detail['recovery_hint']
    assert 'switch to an SSH or URL kernel' in detail['recovery_hint']
    assert environment.environment_unavailable_tool(
        'integration_checkpoint', EVERY_CAPABILITY_AVAILABLE) is None
    assert environment.environment_unavailable_tool('read_files', local) is None


def test_tool_search_explains_withheld_tools(fake_host, process_state, monkeypatch):
    monkeypatch.setattr(environment, '_snapshot', _snapshot_for(fake_host))
    rows = environment.environment_search_availability('commit with git', '', ())
    assert {row['tool_name'] for row in rows} == GIT_TOOLS
    assert all(row['available'] is False and 'apt install git' in row['recovery_hint']
               for row in rows)


# ── Snapshot lifecycle ───────────────────────────────────────────────

def test_snapshot_probes_once_per_boot_and_changes_only_on_refresh(process_state,
                                                                   monkeypatch):
    observed = {'unavailable': {}}
    calls = []

    def probe(environ, *, local_kernel):
        calls.append(local_kernel)
        return dict(observed['unavailable'])

    monkeypatch.setattr(environment, 'probe_environment', probe)
    environment.record_environment_launch_facts(SimpleNamespace(extensions={}))
    assert calls == [], 'recording launch facts before readiness probes nothing'
    boot = environment.environment_snapshot()
    assert boot.revision == 1 and len(calls) == 1
    observed['unavailable'] = {'git': 'not_installed'}
    for _ in range(3):
        assert environment.environment_snapshot() is boot
    assert len(calls) == 1, 'no probe without a refresh event'

    environment.mark_environment_stale('run_command')
    refreshed = environment.environment_snapshot()
    assert len(calls) == 2
    assert refreshed.revision == 2 and refreshed.missing({'git'}) == ('git',)

    environment.mark_environment_stale('run_command')
    assert environment.environment_snapshot() is refreshed, 'same result keeps revision'
    assert environment.refresh_environment_snapshot('user re-scan') is refreshed
    assert len(calls) == 4


def test_tool_schemas_stay_stable_between_refresh_events(fake_host, tmp_path,
                                                         process_state, monkeypatch):
    observed = {'environ': {**fake_host, 'PATH': os.pathsep.join(
        [fake_host['PATH'], os.environ.get('PATH', '')])}}
    monkeypatch.setattr(environment, 'probe_environment',
                        lambda environ, *, local_kernel: host_probe(
                            observed['environ'], local_kernel=local_kernel))

    def exposed():
        ctx = _project_ctx(tmp_path)
        wire, _ = assemble_tool_list(ctx)
        return [tool['function']['name'] for tool in wire]

    first = exposed()
    observed['environ'] = fake_host  # git and node disappear from the host
    assert exposed() == first, 'tool schemas change only at a refresh event'
    environment.mark_environment_stale('run_command')
    after = exposed()
    assert not (GIT_TOOLS | NODE_TOOLS) & set(after)
    assert [name for name in first if name in set(after)] == after


def test_finished_shell_command_is_the_refresh_event(tmp_path, monkeypatch):
    from lib.project_mod.run_command import tool_run_command

    events = []
    monkeypatch.setattr(environment, 'mark_environment_stale', events.append)
    tool_run_command(str(tmp_path), 'true')
    assert events == ['run_command']


def test_local_kernel_boot_reports_devices_unreachable_and_lan_off(fake_host,
                                                                    process_state,
                                                                    monkeypatch):
    monkeypatch.setattr(environment, 'probe_environment',
                        lambda environ, *, local_kernel: host_probe(
                            fake_host, local_kernel=local_kernel))
    assert environment.environment_snapshot().local_kernel is False
    app = SimpleNamespace(extensions={'tofu_local_kernel': {'host': '127.0.0.1:43567'}})
    environment.record_environment_launch_facts(app)
    snapshot = environment.environment_snapshot()
    section = snapshot.public_section()
    jsonschema.validate(section, ENVIRONMENT_SECTION_SCHEMA)
    assert section['local_kernel'] is True
    assert section['capabilities']['devices'] == {
        'available': False, 'reason': 'local_kernel'}
    assert section['capabilities']['lan_discovery'] == {
        'available': False, 'reason': 'local_kernel'}


# ── Capabilities endpoint ────────────────────────────────────────────

def test_capabilities_endpoint_returns_environment_section(process_state, monkeypatch):
    from tests.api.test_api_v4_contract import _bootstrap_app

    snapshot = EnvironmentSnapshot(revision=3, local_kernel=False,
                                   unavailable={'browser': 'not_installed'})
    monkeypatch.setattr(environment, '_snapshot', snapshot)
    app = _bootstrap_app()

    async def exercise():
        response = await app.test_client().get('/api/v4/capabilities')
        assert response.status_code == 200
        return (await response.get_json())['data']

    data = asyncio.run(exercise())
    jsonschema.validate(data['environment'], ENVIRONMENT_SECTION_SCHEMA)
    assert data['environment'] == snapshot.public_section()
    assert data['environment']['capabilities']['browser'] == {
        'available': False, 'reason': 'not_installed'}
    assert not any(os.sep in str(value) for value in
                   data['environment']['capabilities'].values()), 'no host paths'


def test_environment_rescan_route_requires_auth_and_reprobes(process_state, monkeypatch):
    import routes.api_v1.auth as auth
    from tests.api.test_api_v4_contract import _bootstrap_app

    probes = []

    def probe(environ, *, local_kernel):
        probes.append(local_kernel)
        return {'browser': 'not_installed'} if len(probes) > 1 else {}

    monkeypatch.setattr(environment, 'probe_environment', probe)
    assert environment.environment_snapshot().revision == 1
    app = _bootstrap_app()
    path = '/api/v4/capabilities/environment/refresh'

    async def exercise():
        client = app.test_client()
        denied = await client.post(path)
        assert denied.status_code == 401 and len(probes) == 1, 'denied: no probe'
        monkeypatch.setattr(auth, 'current_auth',
                            lambda: SimpleNamespace(is_authenticated=True))
        allowed = await client.post(path)
        return allowed.status_code, await allowed.get_json()

    status, body = asyncio.run(exercise())
    assert status == 200 and len(probes) == 2
    section = body['data']['environment']
    jsonschema.validate(section, ENVIRONMENT_SECTION_SCHEMA)
    assert section['revision'] == 2
    assert section['capabilities']['browser'] == {
        'available': False, 'reason': 'not_installed'}


def test_failed_probe_never_shrinks_the_tool_surface(process_state, monkeypatch, tmp_path):
    outcomes = iter([OSError('registry unreadable'), {'git': 'not_installed'},
                     OSError('transient')])

    def probe(environ, *, local_kernel):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(environment, 'probe_environment', probe)
    first = environment.environment_snapshot()
    assert first.unavailable == {}, 'an unobserved capability is not withheld'
    ctx = _project_ctx(tmp_path)
    assemble_tool_list(ctx)
    assert GIT_TOOLS <= _names(ctx.executable_tool_catalog)
    observed = environment.refresh_environment_snapshot('re-scan')
    assert observed.missing({'git'}) == ('git',)
    assert environment.refresh_environment_snapshot('re-scan') is observed


def test_device_refusal_points_to_reachable_kernels():
    local = EnvironmentSnapshot(revision=1, local_kernel=True, unavailable={
        'devices': 'local_kernel', 'lan_discovery': 'local_kernel'})
    detail = environment.environment_unavailable_tool('desktop_run_command', local)
    assert detail['missing_capabilities'] == ['devices']
    assert 'devices cannot reach this loopback-only phone kernel' in detail['message']
    assert detail['recovery_hint'] == 'Use a URL or SSH kernel that devices can reach.'


def test_snapshot_is_an_immutable_copyable_value():
    import copy
    import pickle

    source = {'git': 'not_installed'}
    snapshot = EnvironmentSnapshot(revision=2, local_kernel=False, unavailable=source)
    source['node'] = 'not_installed'
    assert dict(snapshot.unavailable) == {'git': 'not_installed'}
    with pytest.raises(TypeError):
        snapshot.unavailable['node'] = 'not_installed'
    assert copy.deepcopy(snapshot) == snapshot
    assert pickle.loads(pickle.dumps(snapshot)) == snapshot
