"""Drained storage-host replacement covers every input the host acts on at launch.

Authority: ``serverctl_pkg.native_storage_host.prepare_plan``. The manager
replaces the resident host only when this plan changes, so any module or
setting the host uses while launching the native daemon must change it.
Source bytes are varied through a read wrapper; no file is modified.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from serverctl_pkg import native_storage_host as host

pytestmark = pytest.mark.unit


def _supervisor():
    return SimpleNamespace(
        _configured_backend=lambda: 'tofudb',
        _resolve_transport=lambda: 'daemon',
        _resolve_native_daemon_binary=lambda: None,
        _admitted_native_artifact=(Path('/opt/engine/tofu-db'), 'a' * 64),
    )


@pytest.fixture
def plan_identity(monkeypatch, tmp_path):
    import lib.storage_authority_sql.projection_paths as projection_paths
    import runtime_guards

    monkeypatch.setattr(runtime_guards, '_persistent_data_path', lambda environ: str(tmp_path))
    monkeypatch.setattr(projection_paths, 'resolve_search_projection_paths',
                        lambda data_root: SimpleNamespace(directory=tmp_path / 'projection'))
    real_read_bytes = Path.read_bytes
    altered: set[str] = set()

    def read_bytes(path):
        data = real_read_bytes(path)
        try:
            relative = path.relative_to(host.ROOT).as_posix()
        except ValueError:
            return data
        return data + b'\n# altered source\n' if relative in altered else data

    monkeypatch.setattr(Path, 'read_bytes', read_bytes)

    def identity(*, alter=()):
        altered.clear()
        altered.update(alter)
        return host.prepare_plan(_supervisor())['configurationSha256']

    return identity


@pytest.mark.parametrize('module', [
    'runtime_guards.py',
    'runtime_path_policy.py',
    'bootstrap_pkg/process_roles.py',
    'bootstrap_pkg/resource_policy_generated.py',
    'serverctl_pkg/resident_service.py',
    'lib/storage_authority_sql/native_fastpath_auto.py',
    'lib/storage_authority_sql/projection_paths.py',
    'serverctl_pkg/local_placement.py',
    'lib/storage/startup_budget.py',
    'lib/storage/generated_v2_semantics.py',
    'lib/storage/admitted_engine.py',
])
def test_modules_the_host_runs_at_daemon_launch_replace_the_host(plan_identity, module):
    # The resident host plans automatic fastpath placement at every daemon
    # launch; a stale host would keep deciding with replaced code, or mix it
    # with newly imported modules at a later relaunch.
    assert plan_identity(alter={module}) != plan_identity()


def test_existing_lifecycle_sources_still_replace_the_host(plan_identity):
    assert plan_identity(alter={'lib/storage/supervisor.py'}) != plan_identity()


@pytest.mark.parametrize('name', ['TOFU_LOCAL_DIR', 'XDG_STATE_HOME'])
def test_local_placement_roots_replace_the_host(plan_identity, monkeypatch, tmp_path, name):
    monkeypatch.delenv(name, raising=False)
    before = plan_identity()
    monkeypatch.setenv(name, str(tmp_path / 'persistent-local-disk'))
    assert plan_identity() != before


def test_settings_outside_the_storage_host_keep_it(plan_identity, monkeypatch):
    monkeypatch.delenv('TOFU_RUNTIME_REPLICA', raising=False)
    before = plan_identity()
    monkeypatch.setenv('TOFU_RUNTIME_REPLICA', 'off')
    assert plan_identity() == before
