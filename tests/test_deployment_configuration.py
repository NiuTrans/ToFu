"""Fail-closed personal/distributed topology and secret-file contract."""

from pathlib import Path

import pytest
import runtime_guards as guards

from runtime_guards import (
    SystemResourceSnapshot,
    deployment_resource_default,
    install_process_resource_defaults,
    install_runtime_resource_defaults,
    load_deployment_configuration,
    probe_system_resources,
    resource_budget_manifest,
    resolve_resource_budget,
)


pytestmark = pytest.mark.unit


def _resource_snapshot(
    *,
    cpus: int = 8,
    capacity_mb: int = 8192,
    available_mb: int = 6144,
    disk_total_mb: int = 500 * 1024,
    disk_free_mb: int = 100 * 1024,
    host_memory_mb: int | None = None,
    cgroup_memory_mb: int | None = None,
) -> SystemResourceSnapshot:
    host_memory_mb = host_memory_mb or capacity_mb
    return SystemResourceSnapshot(
        host_cpu_count=cpus,
        affinity_cpu_count=cpus,
        cgroup_cpu_count=None,
        effective_cpu_count=cpus,
        host_memory_total_mb=host_memory_mb,
        host_memory_available_mb=available_mb,
        cgroup_memory_limit_mb=cgroup_memory_mb,
        cgroup_memory_current_mb=(
            max(0, cgroup_memory_mb - available_mb)
            if cgroup_memory_mb is not None else None),
        effective_memory_capacity_mb=capacity_mb,
        effective_memory_available_mb=available_mb,
        disk_total_mb=disk_total_mb,
        disk_free_mb=disk_free_mb,
    )


def _distributed_environment(tmp_path: Path) -> dict[str, str]:
    postgres = tmp_path / 'postgres-dsn'
    redis = tmp_path / 'redis-url'
    postgres.write_text(
        'postgresql://tofu:secret@db.example/tofu?sslmode=verify-full',
        encoding='utf-8')
    redis.write_text(
        'rediss://:secret@redis.example:6380/0', encoding='utf-8')
    return {
        'TOFU_DEPLOYMENT_MODE': 'distributed',
        'TOFU_DISTRIBUTED_PREVIEW_MODE': 'read-only',
        'TOFU_PROCESS_ROLE': 'worker',
        'TOFU_POSTGRES_DSN_FILE': str(postgres),
        'TOFU_REDIS_URL_FILE': str(redis),
        'TOFU_REPLICA_ID': 'worker-0.zone-a',
    }


def test_personal_is_the_low_cost_sqlite_default():
    config = load_deployment_configuration({})

    assert config.mode == 'personal'
    assert config.process_role == 'all'
    assert config.storage_backend == 'sqlite'
    assert config.distributed_preview_read_only is False
    assert config.postgres_dsn == ''
    assert 'secret' not in repr(config)


def test_personal_process_allocator_default_is_small_and_child_visible():
    environment = {}

    resolved = install_process_resource_defaults(
        environment, snapshot=_resource_snapshot())

    assert resolved == {'malloc_arena_max': 2}
    assert environment['MALLOC_ARENA_MAX'] == '2'
    assert environment['TOFU_MAX_INFLIGHT_TASKS'] == '4'
    assert environment['TOFU_TASK_MAX_API_ROUNDS'] == '192'
    assert environment['TOFU_STORAGE_RPC_CAPACITY'] == '8'
    assert environment['TOFU_CONTROL_RPC_WORKERS'] == '8'
    assert environment['TOFU_MCP_STDIO_IDLE_SECONDS'] == '180'
    assert environment['TOFU_EXECUTOR_IDLE_SECONDS'] == '300'
    assert environment['TOFU_PROJECT_REFRESH_IDLE_SECONDS'] == '30'
    assert environment['TOFU_TREE_INDEX_WALK_JOBS'] == '8'
    assert environment['TOFU_TREE_INDEX_MAX_ENTRIES'] == '409600'
    assert environment['TOFU_TREE_INDEX_MEM_ROOTS'] == '2'
    assert environment['TOFU_MAX_SSE_PER_PRINCIPAL'] == '12'
    assert environment['TOFU_SERVER_PYTHON_CACHE_MAX_MIB'] == '64'
    assert environment['TOFU_PROCESS_RSS_RECYCLE_MB'] == '3072'


def test_runtime_snapshot_materializes_defaults_without_overwriting_operator():
    environment = {'TOFU_MAX_INFLIGHT_TASKS': '7'}

    manifest = install_runtime_resource_defaults(
        environment, snapshot=_resource_snapshot())

    assert manifest['overrides'] == {'TOFU_MAX_INFLIGHT_TASKS': '7'}
    assert environment['TOFU_MAX_INFLIGHT_TASKS'] == '7'
    assert environment['TOFU_TASK_MAX_API_ROUNDS'] == '192'
    assert environment['TOFU_SYNC_WORKERS'] == '8'
    assert environment['TOFU_STORAGE_RPC_CAPACITY'] == '8'
    assert environment['TOFU_CONTROL_RPC_WORKERS'] == '8'


def test_resource_profiles_keep_personal_small_without_hardcoding_core_logic():
    personal = {}
    distributed = {'TOFU_DEPLOYMENT_MODE': 'distributed'}
    snapshot = _resource_snapshot()

    assert deployment_resource_default(
        'TOFU_MAX_INFLIGHT_TASKS', personal, snapshot=snapshot) == 4
    assert deployment_resource_default(
        'TOFU_TASK_MAX_API_ROUNDS', personal, snapshot=snapshot) == 192
    assert deployment_resource_default(
        'TOFU_STORAGE_RPC_CAPACITY', personal, snapshot=snapshot) == 8
    assert deployment_resource_default(
        'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB', personal,
        snapshot=snapshot) == 1024
    assert deployment_resource_default(
        'TOFU_CONTROL_RPC_WORKERS', personal, snapshot=snapshot) == 8
    assert deployment_resource_default(
        'TOFU_PROJECT_REFRESH_QUEUE_CAPACITY', personal,
        snapshot=snapshot) == 64
    assert deployment_resource_default(
        'TOFU_PROJECT_REFRESH_IDLE_SECONDS', personal,
        snapshot=snapshot) == 30
    assert deployment_resource_default(
        'TOFU_PROJECT_UNDO_CACHE_CAPACITY', personal,
        snapshot=snapshot) == 128
    assert deployment_resource_default(
        'TOFU_INCREMENTAL_TRANSLATE_ACTIVE', personal,
        snapshot=snapshot) == 8
    assert deployment_resource_default(
        'TOFU_INCREMENTAL_TRANSLATE_QUEUE_CAPACITY', personal,
        snapshot=snapshot) == 16
    assert deployment_resource_default(
        'TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB', personal,
        snapshot=snapshot) == 64
    assert deployment_resource_default(
        'TOFU_RUN_PYTHON_CACHE_MAX_MIB', personal,
        snapshot=snapshot) == 64
    assert deployment_resource_default(
        'TOFU_SERVER_PYTHON_CACHE_MAX_MIB', personal,
        snapshot=snapshot) == 64
    assert deployment_resource_default(
        'TOFU_TOKEN_COUNT_CACHE_CAPACITY', personal,
        snapshot=snapshot) == 256
    assert deployment_resource_default(
        'TOFU_BROWSER_STAGING_MAX_MIB', personal,
        snapshot=snapshot) == 1024
    assert deployment_resource_default(
        'TOFU_LOG_TOTAL_BUDGET_MB', personal, snapshot=snapshot) == 256
    assert deployment_resource_default(
        'TOFU_PROCESS_RSS_RELIEF_MB', personal, snapshot=snapshot) == 2048
    assert deployment_resource_default(
        'TOFU_PROCESS_RSS_RECYCLE_MB', personal, snapshot=snapshot) == 3072
    assert deployment_resource_default(
        'TOFU_MCP_STDIO_IDLE_SECONDS', personal,
        snapshot=snapshot) == 180
    assert deployment_resource_default(
        'TOFU_EXECUTOR_IDLE_SECONDS', personal,
        snapshot=snapshot) == 300
    assert deployment_resource_default(
        'TOFU_TREE_INDEX_WALK_JOBS', personal, snapshot=snapshot) == 8
    assert deployment_resource_default(
        'TOFU_TREE_INDEX_MAX_ENTRIES', personal,
        snapshot=snapshot) == 409_600
    assert deployment_resource_default(
        'TOFU_TREE_INDEX_MEM_ROOTS', personal, snapshot=snapshot) == 2
    assert deployment_resource_default(
        'TOFU_MAX_SSE_PER_PRINCIPAL', personal, snapshot=snapshot) == 12
    assert deployment_resource_default(
        'TOFU_MAX_INFLIGHT_TASKS', distributed) == 16
    assert deployment_resource_default(
        'TOFU_TASK_MAX_API_ROUNDS', distributed) == 512
    assert deployment_resource_default(
        'TOFU_STORAGE_RPC_CAPACITY', distributed) == 64
    assert deployment_resource_default(
        'TOFU_CONTROL_RPC_WORKERS', distributed) == 32
    assert deployment_resource_default(
        'TOFU_PROJECT_REFRESH_QUEUE_CAPACITY', distributed) == 512
    assert deployment_resource_default(
        'TOFU_PROJECT_REFRESH_IDLE_SECONDS', distributed) == 600
    assert deployment_resource_default(
        'TOFU_PROJECT_UNDO_CACHE_CAPACITY', distributed) == 512
    assert deployment_resource_default(
        'TOFU_INCREMENTAL_TRANSLATE_ACTIVE', distributed) == 32
    assert deployment_resource_default(
        'TOFU_INCREMENTAL_TRANSLATE_QUEUE_CAPACITY', distributed) == 64
    assert deployment_resource_default(
        'TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB', distributed) == 64
    assert deployment_resource_default(
        'TOFU_RUN_PYTHON_CACHE_MAX_MIB', distributed) == 128
    assert deployment_resource_default(
        'TOFU_SERVER_PYTHON_CACHE_MAX_MIB', distributed) == 128
    assert deployment_resource_default(
        'TOFU_TOKEN_COUNT_CACHE_CAPACITY', distributed) == 1024
    assert deployment_resource_default(
        'TOFU_BROWSER_STAGING_MAX_MIB', distributed) == 4096
    assert deployment_resource_default(
        'TOFU_PROCESS_RSS_RELIEF_MB', distributed) == 4096
    assert deployment_resource_default(
        'TOFU_PROCESS_RSS_RECYCLE_MB', distributed) == 8192
    assert deployment_resource_default(
        'TOFU_MCP_STDIO_IDLE_SECONDS', distributed) == 1800
    assert deployment_resource_default(
        'TOFU_EXECUTOR_IDLE_SECONDS', distributed) == 3600
    assert deployment_resource_default(
        'TOFU_TREE_INDEX_WALK_JOBS', distributed) == 16
    assert deployment_resource_default(
        'TOFU_TREE_INDEX_MAX_ENTRIES', distributed) == 600_000
    assert deployment_resource_default(
        'TOFU_TREE_INDEX_MEM_ROOTS', distributed) == 4
    assert deployment_resource_default(
        'TOFU_MAX_SSE_PER_PRINCIPAL', distributed) == 64


def test_resource_override_resolution_never_becomes_unbounded():
    snapshot = _resource_snapshot()
    name = 'TOFU_PROJECT_REFRESH_QUEUE_CAPACITY'

    assert resolve_resource_budget(
        name, {name: '999999'}, maximum=4096,
        snapshot=snapshot) == 4096
    assert resolve_resource_budget(
        name, {name: 'invalid'}, maximum=4096,
        snapshot=snapshot) == 64
    assert resolve_resource_budget(
        name, {name: '0'}, maximum=4096,
        snapshot=snapshot) == 64


def test_personal_resource_probe_scales_down_cpu_memory_and_disk_together():
    snapshot = _resource_snapshot(
        cpus=2, capacity_mb=4096, available_mb=2048,
        disk_free_mb=3 * 1024)
    manifest = resource_budget_manifest({}, snapshot=snapshot)

    assert manifest['adaptive'] is True
    assert manifest['probe']['data_path']
    assert manifest['defaults'] == {
        'TOFU_MALLOC_ARENA_MAX': 1,
        'TOFU_STORAGE_RPC_CAPACITY': 4,
        'TOFU_STORAGE_SQLITE_READ_POOL': 4,
        'TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB': 32,
        'TOFU_TURN_SEARCH_PROJECTION_MAX_MIB': 128,
        'TOFU_BROWSER_STAGING_MAX_MIB': 64,
        'TOFU_BROWSER_POLL_MAX_INFLIGHT': 8,
        'TOFU_BROWSER_POLL_MAX_WAITERS': 8,
        'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY': 64,
        'TOFU_BROWSER_POLL_BODY_MAX_MIB': 16,
        'TOFU_MAX_SSE_PER_PRINCIPAL': 8,
        'TOFU_RUN_PYTHON_CACHE_MAX_MIB': 16,
        'TOFU_SERVER_PYTHON_CACHE_MAX_MIB': 16,
        'TOFU_TOKEN_COUNT_CACHE_CAPACITY': 128,
        'TOFU_CONTROL_RPC_WORKERS': 4,
        'TOFU_PROJECT_REFRESH_QUEUE_CAPACITY': 32,
        'TOFU_PROJECT_REFRESH_IDLE_SECONDS': 30,
        'TOFU_PROJECT_UNDO_CACHE_CAPACITY': 64,
        'TOFU_TREE_INDEX_WALK_JOBS': 4,
        'TOFU_TREE_INDEX_MAX_ENTRIES': 204_800,
        'TOFU_TREE_INDEX_MEM_ROOTS': 2,
        'TOFU_INCREMENTAL_TRANSLATE_ACTIVE': 4,
        'TOFU_INCREMENTAL_TRANSLATE_QUEUE_CAPACITY': 8,
        'TOFU_MAX_INFLIGHT_TASKS': 2,
        'TOFU_TASK_MAX_API_ROUNDS': 192,
        'TOFU_SYNC_WORKERS': 4,
        'TOFU_AGENT_WORKERS': 2,
        'TOOL_MAX_PARALLEL_WORKERS': 2,
        'TOFU_NUMERIC_THREADS': 1,
        'TOFU_MCP_STDIO_IDLE_SECONDS': 180,
        'TOFU_EXECUTOR_IDLE_SECONDS': 300,
        'TOFU_LOG_TOTAL_BUDGET_MB': 64,
        'TOFU_STORAGE_MIN_FREE_BYTES': 2048 * 1024 * 1024,
        'TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB': 250 * 1024,
        'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB': 64,
        'TOFU_ATTEMPT_EVENT_TTL_DAYS': 1,
        'TOFU_PROCESS_RSS_RELIEF_MB': 1024,
        'TOFU_PROCESS_RSS_RECYCLE_MB': 1536,
    }


def test_personal_resource_probe_caps_large_hosts_at_useful_parallelism():
    snapshot = _resource_snapshot(
        cpus=64, capacity_mb=64 * 1024, available_mb=48 * 1024,
        disk_total_mb=2 * 1024 * 1024,
        disk_free_mb=1024 * 1024)
    defaults = resource_budget_manifest({}, snapshot=snapshot)['defaults']

    assert defaults['TOFU_MAX_INFLIGHT_TASKS'] == 8
    assert defaults['TOFU_SYNC_WORKERS'] == 16
    assert defaults['TOFU_STORAGE_RPC_CAPACITY'] == 12
    assert defaults['TOFU_NUMERIC_THREADS'] == 4
    assert defaults['TOFU_MALLOC_ARENA_MAX'] == 4
    assert defaults['TOFU_MCP_STDIO_IDLE_SECONDS'] == 600
    assert defaults['TOFU_EXECUTOR_IDLE_SECONDS'] == 1800
    assert defaults['TOFU_PROJECT_REFRESH_IDLE_SECONDS'] == 300
    assert defaults['TOFU_TREE_INDEX_WALK_JOBS'] == 8
    assert defaults['TOFU_TREE_INDEX_MAX_ENTRIES'] == 600_000
    assert defaults['TOFU_TREE_INDEX_MEM_ROOTS'] == 4
    assert defaults['TOFU_PROCESS_RSS_RECYCLE_MB'] == 6144
    assert defaults['TOFU_BROWSER_STAGING_MAX_MIB'] == 2048
    assert defaults['TOFU_BROWSER_POLL_MAX_INFLIGHT'] == 16
    assert defaults['TOFU_BROWSER_POLL_MAX_WAITERS'] == 16
    assert defaults['TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY'] == 128
    assert defaults['TOFU_BROWSER_POLL_BODY_MAX_MIB'] == 32
    assert defaults['TOFU_MAX_SSE_PER_PRINCIPAL'] == 24
    assert defaults['TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB'] == 512 * 1024
    assert defaults['TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB'] == 8192


def test_explicit_cgroup_limit_is_treated_as_the_application_memory_budget():
    snapshot = _resource_snapshot(
        cpus=8, capacity_mb=4096, available_mb=4096,
        host_memory_mb=16 * 1024, cgroup_memory_mb=4096)
    defaults = resource_budget_manifest({}, snapshot=snapshot)['defaults']

    assert defaults['TOFU_PROCESS_RSS_RELIEF_MB'] == 2048
    assert defaults['TOFU_PROCESS_RSS_RECYCLE_MB'] == 2867
    assert defaults['TOFU_MAX_INFLIGHT_TASKS'] == 2


def test_probe_uses_minimum_of_affinity_cgroup_and_host_headroom(monkeypatch):
    mib = 1024 * 1024
    monkeypatch.setattr(guards.os, 'cpu_count', lambda: 16)
    monkeypatch.setattr(
        guards.os, 'sched_getaffinity', lambda _pid: set(range(8)),
        raising=False)
    monkeypatch.setattr(guards, '_cgroup_cpu_count', lambda: 2)
    monkeypatch.setattr(
        guards, '_host_memory_bytes',
        lambda: (16 * 1024 * mib, 12 * 1024 * mib))
    monkeypatch.setattr(
        guards, '_cgroup_memory_bytes',
        lambda: (4 * 1024 * mib, 1024 * mib))
    monkeypatch.setattr(
        guards, '_project_disk_bytes',
        lambda _env: (500 * 1024 * mib, 3 * 1024 * mib))

    snapshot = probe_system_resources({}, refresh=True)

    assert snapshot.host_cpu_count == 16
    assert snapshot.affinity_cpu_count == 8
    assert snapshot.cgroup_cpu_count == 2
    assert snapshot.effective_cpu_count == 2
    assert snapshot.effective_memory_capacity_mb == 4096
    assert snapshot.effective_memory_available_mb == 3072
    assert snapshot.disk_total_mb == 500 * 1024
    assert snapshot.disk_free_mb == 3072


def test_disk_probe_follows_relocated_data_directory(tmp_path, monkeypatch):
    relocated_base = tmp_path / 'separate-volume'
    observed_paths = []

    class _Usage:
        total = 500 * 1024 * 1024
        free = 100 * 1024 * 1024

    def fake_disk_usage(path):
        observed_paths.append(Path(path))
        return _Usage()

    monkeypatch.setattr(guards.shutil, 'disk_usage', fake_disk_usage)

    assert guards._project_disk_bytes({
        'TOFU_PROJECT_PATH': str(tmp_path / 'source'),
        'TOFU_DATA_DIR': str(relocated_base),
    }) == (_Usage.total, _Usage.free)
    assert observed_paths == [relocated_base / 'data']


def test_disk_probe_follows_explicit_xdg_layout(tmp_path, monkeypatch):
    observed_paths = []

    class _Usage:
        total = 500 * 1024 * 1024
        free = 100 * 1024 * 1024

    def fake_disk_usage(path):
        observed_paths.append(Path(path))
        return _Usage()

    monkeypatch.setattr(guards.shutil, 'disk_usage', fake_disk_usage)

    assert guards._project_disk_bytes({
        'TOFU_PROJECT_PATH': str(tmp_path / 'source'),
        'TOFU_DATA_LAYOUT': 'xdg',
        'XDG_DATA_HOME': str(tmp_path / 'xdg'),
    }) == (_Usage.total, _Usage.free)
    assert observed_paths == [tmp_path / 'xdg' / 'Tofu' / 'data']


def test_data_probe_auto_layout_matches_fresh_and_existing_source_rules(
        tmp_path):
    existing = tmp_path / 'existing'
    (existing / 'data').mkdir(parents=True)
    (existing / 'data' / 'tofu.db').touch()
    fresh = tmp_path / 'fresh'
    fresh.mkdir()
    xdg = tmp_path / 'xdg'

    assert guards._persistent_data_path({
        'TOFU_PROJECT_PATH': str(existing),
        'XDG_DATA_HOME': str(xdg),
    }) == existing / 'data'
    assert guards._persistent_data_path({
        'TOFU_PROJECT_PATH': str(fresh),
        'XDG_DATA_HOME': str(xdg),
    }) == xdg / 'Tofu' / 'data'


def test_whole_host_cgroup_cache_does_not_hide_kernel_available_memory(
        monkeypatch):
    mib = 1024 * 1024
    monkeypatch.setattr(guards.os, 'cpu_count', lambda: 8)
    monkeypatch.setattr(
        guards.os, 'sched_getaffinity', lambda _pid: set(range(8)),
        raising=False)
    monkeypatch.setattr(guards, '_cgroup_cpu_count', lambda: None)
    monkeypatch.setattr(
        guards, '_host_memory_bytes',
        lambda: (16 * 1024 * mib, 12 * 1024 * mib))
    monkeypatch.setattr(
        guards, '_cgroup_memory_bytes',
        lambda: (16 * 1024 * mib, 15 * 1024 * mib))
    monkeypatch.setattr(
        guards, '_project_disk_bytes',
        lambda _env: (500 * 1024 * mib, 100 * 1024 * mib))

    snapshot = probe_system_resources({}, refresh=True)

    assert snapshot.effective_memory_capacity_mb == 16 * 1024
    assert snapshot.effective_memory_available_mb == 12 * 1024
    assert resource_budget_manifest(
        {}, snapshot=snapshot)['defaults']['TOFU_MAX_INFLIGHT_TASKS'] == 8


def test_cgroup_probe_resolves_non_root_process_membership(monkeypatch):
    cgroup = '0::/user.slice/tofu.service'
    mountinfo = (
        '31 24 0:27 / /sys/fs/cgroup rw - cgroup2 cgroup rw')

    def fake_read(paths):
        if paths == ('/proc/self/cgroup',):
            return cgroup
        if paths == ('/proc/self/mountinfo',):
            return mountinfo
        return None

    monkeypatch.setattr(guards, '_read_first_text', fake_read)

    assert guards._cgroup_file_paths(
        'memory', 'memory.max', ()) == (
            '/sys/fs/cgroup/user.slice/tofu.service/memory.max',)


def test_probe_failure_degrades_to_one_cpu_and_bounded_memory_fallback(
        monkeypatch):
    monkeypatch.setattr(guards.os, 'cpu_count', lambda: None)
    monkeypatch.setattr(
        guards.os, 'sched_getaffinity',
        lambda _pid: (_ for _ in ()).throw(OSError('unavailable')),
        raising=False)
    monkeypatch.setattr(guards, '_cgroup_cpu_count', lambda: None)
    monkeypatch.setattr(guards, '_host_memory_bytes', lambda: (None, None))
    monkeypatch.setattr(guards, '_cgroup_memory_bytes', lambda: (None, None))
    monkeypatch.setattr(
        guards, '_project_disk_bytes', lambda _env: (None, None))

    snapshot = probe_system_resources({}, refresh=True)
    defaults = resource_budget_manifest({}, snapshot=snapshot)['defaults']

    assert snapshot.effective_cpu_count == 1
    assert snapshot.effective_memory_capacity_mb is None
    assert defaults['TOFU_MAX_INFLIGHT_TASKS'] == 1
    assert defaults['TOFU_STORAGE_RPC_CAPACITY'] == 2
    assert defaults['TOFU_LOG_TOTAL_BUDGET_MB'] == 128
    assert defaults['TOFU_STORAGE_MIN_FREE_BYTES'] == 256 * 1024 * 1024
    assert defaults['TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB'] == 64 * 1024
    assert defaults['TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB'] == 512
    assert defaults['TOFU_BROWSER_STAGING_MAX_MIB'] == 256
    assert defaults['TOFU_BROWSER_POLL_MAX_INFLIGHT'] == 8
    assert defaults['TOFU_BROWSER_POLL_MAX_WAITERS'] == 8
    assert defaults['TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY'] == 64
    assert defaults['TOFU_BROWSER_POLL_BODY_MAX_MIB'] == 32
    assert defaults['TOFU_MAX_SSE_PER_PRINCIPAL'] == 12
    assert defaults['TOFU_SERVER_PYTHON_CACHE_MAX_MIB'] == 64
    assert defaults['TOFU_MCP_STDIO_IDLE_SECONDS'] == 300
    assert defaults['TOFU_EXECUTOR_IDLE_SECONDS'] == 600
    assert defaults['TOFU_PROJECT_REFRESH_IDLE_SECONDS'] == 60
    assert defaults['TOFU_TREE_INDEX_WALK_JOBS'] == 2
    assert defaults['TOFU_TREE_INDEX_MAX_ENTRIES'] == 100_000
    assert defaults['TOFU_TREE_INDEX_MEM_ROOTS'] == 2


def test_distributed_allocator_default_and_override_are_bounded():
    distributed = {'TOFU_DEPLOYMENT_MODE': 'distributed'}
    assert install_process_resource_defaults(
            distributed, snapshot=_resource_snapshot()) == {
        'malloc_arena_max': 8,
    }
    assert distributed['MALLOC_ARENA_MAX'] == '8'

    overridden = {'TOFU_MALLOC_ARENA_MAX': '9999'}
    assert install_process_resource_defaults(
            overridden, snapshot=_resource_snapshot()) == {
        'malloc_arena_max': 64,
    }
    assert overridden['MALLOC_ARENA_MAX'] == '64'


def test_malformed_allocator_override_falls_back_to_personal_budget():
    environment = {'TOFU_MALLOC_ARENA_MAX': 'not-a-number'}

    assert install_process_resource_defaults(
            environment, snapshot=_resource_snapshot()) == {
        'malloc_arena_max': 2,
    }
    assert environment['MALLOC_ARENA_MAX'] == '2'


@pytest.mark.parametrize(
    'removed_name',
    [
        'TOFU_DB_BACKEND', 'TOFU_DB_PATH', 'CHATUI_DB_PATH',
        'TOFU_REQUIRE_PG', 'TOFU_REPLICA_RING', 'TOFU_STORAGE_MODE',
        'CHATUI_DATA_DIR', 'CHATUI_ANY_REMOVED_SETTING',
    ],
)
def test_removed_topology_switches_fail_closed(removed_name):
    with pytest.raises(RuntimeError, match='removed deployment configuration'):
        load_deployment_configuration({removed_name: '1'})


def test_private_test_authority_does_not_reopen_removed_configuration():
    with pytest.raises(RuntimeError, match='removed deployment configuration'):
        load_deployment_configuration(
            {'TOFU_DB_BACKEND': 'sqlite'}, allow_test_backend_override=True)


def test_distributed_requires_complete_tls_verified_secret_files(tmp_path):
    environment = _distributed_environment(tmp_path)

    config = load_deployment_configuration(environment)

    assert config.mode == 'distributed'
    assert config.process_role == 'worker'
    assert config.storage_backend == 'postgres'
    assert config.distributed_preview_read_only is True
    assert config.replica_id == 'worker-0.zone-a'
    assert config.postgres_dsn_file == Path(
        environment['TOFU_POSTGRES_DSN_FILE']).resolve()
    assert config.redis_url.startswith('rediss://')
    assert 'secret' not in repr(config)


@pytest.mark.parametrize(
    ('missing_name', 'message'),
    [
        ('TOFU_POSTGRES_DSN_FILE', 'TOFU_POSTGRES_DSN_FILE is required'),
        ('TOFU_REDIS_URL_FILE', 'TOFU_REDIS_URL_FILE is required'),
        ('TOFU_REPLICA_ID', 'unique TOFU_REPLICA_ID'),
    ],
)
def test_distributed_rejects_missing_identity_or_secret(
        tmp_path, missing_name, message):
    environment = _distributed_environment(tmp_path)
    environment.pop(missing_name)

    with pytest.raises(RuntimeError, match=message):
        load_deployment_configuration(environment)


def test_distributed_requires_explicit_read_only_preview_acknowledgement(
        tmp_path):
    environment = _distributed_environment(tmp_path)
    environment.pop('TOFU_DISTRIBUTED_PREVIEW_MODE')

    with pytest.raises(RuntimeError, match='PREVIEW_MODE=read-only'):
        load_deployment_configuration(environment)


def test_distributed_rejects_unverified_postgres_tls(tmp_path):
    environment = _distributed_environment(tmp_path)
    Path(environment['TOFU_POSTGRES_DSN_FILE']).write_text(
        'host=db.example dbname=tofu sslmode=require', encoding='utf-8')

    with pytest.raises(RuntimeError, match='sslmode=verify-full'):
        load_deployment_configuration(environment)


def test_distributed_rejects_plaintext_redis(tmp_path):
    environment = _distributed_environment(tmp_path)
    Path(environment['TOFU_REDIS_URL_FILE']).write_text(
        'redis://redis.example:6379/0', encoding='utf-8')

    with pytest.raises(RuntimeError, match='rediss://'):
        load_deployment_configuration(environment)


@pytest.mark.parametrize('role', ['api', 'worker', 'scheduler', 'all'])
def test_distributed_accepts_declared_process_roles(tmp_path, role):
    environment = _distributed_environment(tmp_path)
    environment['TOFU_PROCESS_ROLE'] = role

    assert load_deployment_configuration(environment).process_role == role


def test_personal_rejects_split_role_and_distributed_secrets(tmp_path):
    with pytest.raises(RuntimeError, match='requires TOFU_PROCESS_ROLE=all'):
        load_deployment_configuration({'TOFU_PROCESS_ROLE': 'api'})

    secret = tmp_path / 'secret'
    secret.write_text('unused', encoding='utf-8')
    with pytest.raises(RuntimeError, match='distributed-only'):
        load_deployment_configuration({'TOFU_REDIS_URL_FILE': str(secret)})
