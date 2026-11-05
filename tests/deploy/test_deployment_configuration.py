"""Fail-closed personal/distributed topology and secret-file contract."""

import json
from pathlib import Path

import pytest
import runtime_guards as guards
from tofu_workers import resources as system_resources

from runtime_guards import (
    SystemResourceSnapshot,
    deployment_resource_default,
    install_process_resource_defaults,
    install_runtime_resource_defaults,
    load_deployment_configuration,
    probe_system_resources,
    resource_budget_manifest,
    resolve_resource_budget,
    storage_backup_timeout_seconds,
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
    assert config.tofudb_transport == 'auto'
    assert config.distributed_preview_read_only is False
    assert config.postgres_dsn == ''
    assert 'secret' not in repr(config)


def test_personal_native_backend_defaults_to_the_auto_transport():
    config = load_deployment_configuration(
        {'TOFU_PERSONAL_STORAGE_BACKEND': 'tofudb'})

    assert config.storage_backend == 'tofudb'
    assert config.tofudb_transport == 'auto'


def test_tofudb_transport_embedded_selects_the_in_process_native_authority():
    config = load_deployment_configuration({
        'TOFU_PERSONAL_STORAGE_BACKEND': 'tofudb',
        'TOFUDB_TRANSPORT': 'embedded',
    })

    # The authority stays the native engine; only the transport differs.
    assert config.storage_backend == 'tofudb'
    assert config.tofudb_transport == 'embedded'


def test_tofudb_transport_embedded_requires_the_native_backend():
    with pytest.raises(
            RuntimeError,
            match='TOFUDB_TRANSPORT=embedded requires'):
        load_deployment_configuration({'TOFUDB_TRANSPORT': 'embedded'})


def test_tofudb_transport_rejects_unknown_values():
    with pytest.raises(
            RuntimeError,
            match='TOFUDB_TRANSPORT must be auto, daemon or embedded'):
        load_deployment_configuration({
            'TOFU_PERSONAL_STORAGE_BACKEND': 'tofudb',
            'TOFUDB_TRANSPORT': 'socket',
        })


def test_tofudb_transport_auto_is_an_explicit_valid_value():
    config = load_deployment_configuration({
        'TOFU_PERSONAL_STORAGE_BACKEND': 'tofudb',
        'TOFUDB_TRANSPORT': 'auto',
    })

    assert config.tofudb_transport == 'auto'


def test_tofudb_transport_daemon_remains_an_explicit_escape():
    config = load_deployment_configuration({
        'TOFU_PERSONAL_STORAGE_BACKEND': 'tofudb',
        'TOFUDB_TRANSPORT': 'daemon',
    })

    assert config.tofudb_transport == 'daemon'


_CONTRACT = json.loads(
    (Path(__file__).resolve().parents[2] / 'contracts/resource_policy_v1.json')
    .read_text(encoding='utf-8'))['launch_settings']
_DISTRIBUTED = {'TOFU_DEPLOYMENT_MODE': 'distributed'}

# Owner decision 2026-10-24: hardware-independent (personal, distributed)
# constants. Changing one is a contract change, never a probe formula.
_FIXED_DEFAULTS = {
    'TOFU_AGENT_WORKERS': (16, 16),
    'TOFU_SYNC_WORKERS': (16, 16),
    'TOOL_MAX_PARALLEL_WORKERS': (8, 8),
    'TOFU_CONTROL_RPC_WORKERS': (32, 32),
    'TOFU_STORAGE_RPC_CAPACITY': (64, 64),
    'TOFU_STORAGE_SQLITE_READ_POOL': (16, 16),
    'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY': (128, 128),
    'TOFU_TRANSLATE_WORKERS': (16, 16),
    'TOFU_PDF_PROCESSES': (4, 4),
    'TOFU_PDF_VLM_TASK_WORKERS': (4, 4),
    'TOFU_PDF_VLM_CALL_WORKERS': (8, 8),
    'TOFU_PAPER_BUILD_WORKERS': (4, 4),
    'TOFU_KNOWLEDGE_ENRICH_WORKERS': (8, 8),
    'TOFU_PRODUCTION_IMAGE_FANOUT': (4, 4),
    'TOFU_PRODUCTION_TTS_FANOUT': (4, 4),
    'TOFU_SWARM_MAX_PARALLEL': (8, 8),
    'TOFU_TREE_INDEX_WALK_JOBS': (8, 8),
    'TOFU_MALLOC_ARENA_MAX': (8, 8),
    'TOFU_PROGRAM_HEAP_MAX_MIB': (1024, 1024),
    'TOFU_PDF_VLM_TASK_TIMEOUT_SECONDS': (14_400, 14_400),
    'TOFU_BROWSER_POLL_BODY_MAX_MIB': (64, 64),
    'TOFU_TRANSLATE_MAX_429_ATTEMPTS': (8, 16),
    'TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS': (8, 16),
    'TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS': (8, 16),
    'TOFU_PDF_VLM_MAX_429_ATTEMPTS': (8, 16),
    'TOFU_OPTIONAL_LLM_MAX_429_ATTEMPTS': (2, 8),
    'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MAX_429_ATTEMPTS': (1, 1),
    'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_DEADLINE_SECONDS': (45, 60),
    'TOFU_SWARM_MAX_RETRIES': (1, 2),
}

# Evict-only budgets of the 8 GiB / 500 GiB reference computer.
_REFERENCE_RETENTION = {
    'TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB': 128,
    'TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB': 64,
    'TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB': 32,
    'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB': 2048,
    'TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS': 17_800,
    'TOFU_PROJECT_REFRESH_IDLE_SECONDS': 30,
    'TOFU_PROJECT_UNDO_CACHE_CAPACITY': 128,
    'TOFU_TOKEN_COUNT_CACHE_CAPACITY': 256,
    'TOFU_USAGE_CACHE_CAPACITY': 256,
    'TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY': 1024,
    'TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY': 1024,
    'TOFU_TOOL_RESULT_CACHE_CAPACITY': 128,
    'TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS': 600,
    'TOFU_MEMORY_METADATA_CACHE_CAPACITY': 2048,
    'TOFU_MEMORY_METADATA_CACHE_MAX_MIB': 16,
    'TOFU_PAPER_QA_SOURCE_CACHE_CAPACITY': 2,
    'TOFU_PAPER_BUILD_CACHE_MAX_MIB': 512,
    'TOFU_PAPER_BUILD_CACHE_RECORDS': 16,
    'TOFU_TRANSLATE_CACHE_MAX_MIB': 256,
    'TOFU_TRANSLATE_WORKER_IDLE_SECONDS': 60,
    'TOFU_PDF_WORKER_IDLE_SECONDS': 60,
    'TOFU_PDF_VLM_WORKER_IDLE_SECONDS': 60,
    'TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS': 60,
    'TOFU_SWARM_SESSION_CAPACITY': 8,
    'TOFU_BROWSER_STAGING_MAX_MIB': 1024,
    'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY': 128,
    'TOFU_LOG_TOTAL_BUDGET_MB': 256,
    'TOFU_MCP_STDIO_IDLE_SECONDS': 180,
    'TOFU_EXECUTOR_IDLE_SECONDS': 300,
    'TOFU_TREE_INDEX_MAX_ENTRIES': 409_600,
    'TOFU_TREE_INDEX_MEM_ROOTS': 2,
}


def _names(*classes: str) -> list[str]:
    return sorted(name for name, spec in _CONTRACT.items()
                  if spec['class'] in classes)


def _failed_probe_snapshot() -> SystemResourceSnapshot:
    return SystemResourceSnapshot(
        host_cpu_count=1, affinity_cpu_count=None, cgroup_cpu_count=None,
        effective_cpu_count=1, host_memory_total_mb=None,
        host_memory_available_mb=None, cgroup_memory_limit_mb=None,
        cgroup_memory_current_mb=None, effective_memory_capacity_mb=None,
        effective_memory_available_mb=None, disk_total_mb=None,
        disk_free_mb=None)


_HOSTS = {
    'probe_failed': _failed_probe_snapshot(),
    'tiny': _resource_snapshot(
        cpus=1, capacity_mb=512, available_mb=128,
        disk_total_mb=8 * 1024, disk_free_mb=1024),
    'reference': _resource_snapshot(),
    'huge': _resource_snapshot(
        cpus=512, capacity_mb=4 * 1024 * 1024, available_mb=3 * 1024 * 1024,
        disk_total_mb=64 * 1024 * 1024, disk_free_mb=32 * 1024 * 1024),
}


def test_contract_classes_match_the_owner_decision():
    assert set(_names('fixed')) == set(_FIXED_DEFAULTS)
    assert set(_names('unlimited')) == {
        'TOFU_MAX_INFLIGHT_TASKS', 'TOFU_NUMERIC_THREADS'}
    assert guards.UNLIMITED_SETTINGS == frozenset(_names('unlimited'))
    assert set(_CONTRACT) == guards.RESOURCE_BUDGET_ENV_KEYS
    for name, (personal, distributed) in _FIXED_DEFAULTS.items():
        assert deployment_resource_default(name, {}) == personal, name
        assert deployment_resource_default(name, _DISTRIBUTED) == distributed, name


@pytest.mark.parametrize('environment', [{}, _DISTRIBUTED],
                         ids=['personal', 'distributed'])
def test_execution_defaults_are_identical_on_failed_tiny_and_huge_probes(
        environment):
    mode = environment.get('TOFU_DEPLOYMENT_MODE', 'personal')
    manifests = [
        resource_budget_manifest(dict(environment), snapshot=host)['defaults']
        for host in _HOSTS.values()
    ]
    for name in _names('unlimited'):
        assert {defaults[name] for defaults in manifests} == {guards.UNLIMITED}, name
    for name in _names('fixed'):
        assert {defaults[name] for defaults in manifests} == {
            _CONTRACT[name]['defaults'][mode]}, name


def test_only_personal_retention_consults_the_probe(monkeypatch):
    probes = []
    monkeypatch.setattr(
        guards, 'probe_system_resources',
        lambda *_args, **_kwargs: probes.append(1) or _resource_snapshot())
    for name, spec in _CONTRACT.items():
        expected = (guards.UNLIMITED if spec['class'] == 'unlimited'
                    else spec['defaults']['distributed'])
        assert deployment_resource_default(name, _DISTRIBUTED) == expected, name
        if spec['class'] != 'retention':
            deployment_resource_default(name, {})
    assert probes == []
    assert deployment_resource_default('TOFU_LOG_TOTAL_BUDGET_MB', {}) == 256
    assert probes == [1]


def test_unlimited_settings_are_reported_but_never_materialized():
    environment = {}

    manifest = install_runtime_resource_defaults(
        environment, snapshot=_resource_snapshot())
    install_process_resource_defaults(environment, snapshot=_resource_snapshot())

    for name in guards.UNLIMITED_SETTINGS:
        assert manifest['defaults'][name] == guards.UNLIMITED
        assert name not in environment
        assert name not in manifest['automatic']
    assert str(guards.UNLIMITED) not in environment.values()
    assert resolve_resource_budget(
        'TOFU_MAX_INFLIGHT_TASKS', environment,
        maximum=guards.UNLIMITED) == guards.UNLIMITED


def test_process_allocator_default_is_fixed_and_child_visible():
    for snapshot in _HOSTS.values():
        environment = {}

        resolved = install_process_resource_defaults(
            environment, snapshot=snapshot)

        assert resolved == {'malloc_arena_max': 8}
        assert environment['MALLOC_ARENA_MAX'] == '8'
        assert environment['TOFU_MALLOC_ARENA_MAX'] == '8'
        assert environment['TOFU_AGENT_WORKERS'] == '16'
        assert environment['TOFU_STORAGE_RPC_CAPACITY'] == '64'


def test_reference_computer_retention_defaults():
    defaults = resource_budget_manifest(
        {}, snapshot=_resource_snapshot())['defaults']

    assert {name: defaults[name] for name in _REFERENCE_RETENTION} \
        == _REFERENCE_RETENTION


def test_retention_follows_memory_and_disk_but_never_cpu_count():
    small = resource_budget_manifest({}, snapshot=_resource_snapshot(
        cpus=2, capacity_mb=4096, available_mb=2048,
        disk_total_mb=64 * 1024, disk_free_mb=3 * 1024))['defaults']
    large = resource_budget_manifest({}, snapshot=_resource_snapshot(
        cpus=2, capacity_mb=64 * 1024, available_mb=48 * 1024,
        disk_total_mb=2 * 1024 * 1024, disk_free_mb=1024 * 1024))['defaults']

    for name in (
            'TOFU_TOKEN_COUNT_CACHE_CAPACITY',
            'TOFU_TOOL_RESULT_CACHE_CAPACITY',
            'TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS',
            'TOFU_SWARM_SESSION_CAPACITY',
            'TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB',
            'TOFU_TREE_INDEX_MAX_ENTRIES',
            'TOFU_LOG_TOTAL_BUDGET_MB',
            'TOFU_BROWSER_STAGING_MAX_MIB',
            'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB'):
        assert large[name] > small[name], name
    for cpus in (1, 512):
        assert resource_budget_manifest({}, snapshot=_resource_snapshot(
            cpus=cpus, capacity_mb=64 * 1024, available_mb=48 * 1024,
            disk_total_mb=2 * 1024 * 1024,
            disk_free_mb=1024 * 1024))['defaults'] == large


def test_large_hosts_grow_retention_but_never_execution_values():
    reference = resource_budget_manifest(
        {}, snapshot=_resource_snapshot())['defaults']
    defaults = resource_budget_manifest({}, snapshot=_resource_snapshot(
        cpus=64, capacity_mb=64 * 1024, available_mb=48 * 1024,
        disk_total_mb=2 * 1024 * 1024,
        disk_free_mb=1024 * 1024))['defaults']

    assert defaults['TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB'] == 512
    assert defaults['TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY'] == 4096
    assert defaults['TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY'] == 4096
    assert defaults['TOFU_TOOL_RESULT_CACHE_CAPACITY'] == 256
    assert defaults['TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS'] == 1800
    assert defaults['TOFU_MCP_STDIO_IDLE_SECONDS'] == 600
    assert defaults['TOFU_EXECUTOR_IDLE_SECONDS'] == 1800
    assert defaults['TOFU_PROJECT_REFRESH_IDLE_SECONDS'] == 300
    assert defaults['TOFU_TREE_INDEX_MAX_ENTRIES'] == 600_000
    assert defaults['TOFU_TREE_INDEX_MEM_ROOTS'] == 4
    assert defaults['TOFU_BROWSER_STAGING_MAX_MIB'] == 2048
    assert defaults['TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY'] == 128
    assert defaults['TOFU_SWARM_SESSION_CAPACITY'] == 64
    assert defaults['TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB'] == 512 * 1024
    assert defaults['TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS'] == 21_600
    assert defaults['TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB'] == 16_384
    for name in _names('fixed', 'unlimited'):
        assert defaults[name] == reference[name], name


def test_paper_build_workers_are_fixed_while_its_cache_follows_the_disk():
    defaults = resource_budget_manifest({}, snapshot=_resource_snapshot())['defaults']
    assert defaults['TOFU_PAPER_BUILD_WORKERS'] == 4
    assert defaults['TOFU_PAPER_BUILD_CACHE_MAX_MIB'] == 512
    assert defaults['TOFU_PAPER_BUILD_CACHE_RECORDS'] == 16
    assert 'TOFU_PAPER_BUILD_QUEUE_CAPACITY' not in defaults
    assert 'TOFU_PAPER_BUILD_VALIDATION_MAX_MIB' not in defaults
    assert 'TOFU_PAPER_BUILD_COMPILE_MAX_MIB' not in defaults
    constrained = resource_budget_manifest({}, snapshot=_resource_snapshot(
        cpus=2, capacity_mb=4096, available_mb=1024, disk_free_mb=10 * 1024))['defaults']
    assert constrained['TOFU_PAPER_BUILD_WORKERS'] == 4
    assert constrained['TOFU_PAPER_BUILD_CACHE_MAX_MIB'] == 64


def test_runtime_snapshot_materializes_defaults_without_overwriting_operator():
    environment = {'TOFU_MAX_INFLIGHT_TASKS': '7', 'TOFU_AGENT_WORKERS': '3'}

    manifest = install_runtime_resource_defaults(
        environment, snapshot=_resource_snapshot())

    assert manifest['overrides'] == {
        'TOFU_MAX_INFLIGHT_TASKS': '7', 'TOFU_AGENT_WORKERS': '3'}
    assert environment['TOFU_MAX_INFLIGHT_TASKS'] == '7'
    assert environment['TOFU_AGENT_WORKERS'] == '3'
    assert environment['TOFU_SYNC_WORKERS'] == '16'
    assert environment['TOFU_STORAGE_RPC_CAPACITY'] == '64'
    assert environment['TOFU_CONTROL_RPC_WORKERS'] == '32'
    assert environment[guards.RESOURCE_BUDGET_POLICY_ENV] \
        == guards.RESOURCE_BUDGET_POLICY_VERSION
    automatic = set(environment[
        guards.RESOURCE_BUDGET_AUTOMATIC_ENV].split(','))
    assert 'TOFU_SYNC_WORKERS' in automatic
    assert not {'TOFU_MAX_INFLIGHT_TASKS', 'TOFU_AGENT_WORKERS'} & automatic
    assert manifest['automatic'] == sorted(automatic)
    assert resolve_resource_budget(
        'TOFU_MAX_INFLIGHT_TASKS', environment, maximum=guards.UNLIMITED) == 7
    assert resolve_resource_budget(
        'TOFU_AGENT_WORKERS', environment, maximum=guards.UNLIMITED) == 3


@pytest.mark.parametrize('profile', ['throughput', 'balanced', 'througput'])
def test_retired_resource_profile_is_ignored_never_an_error(profile):
    snapshot = _resource_snapshot(
        cpus=60, capacity_mb=210 * 1024, available_mb=175 * 1024)
    plain = {}
    expected = install_runtime_resource_defaults(plain, snapshot=snapshot)
    environment = {'TOFU_RESOURCE_PROFILE': profile}

    manifest = install_runtime_resource_defaults(environment, snapshot=snapshot)

    assert manifest == expected
    assert 'resource_profile' not in manifest
    assert environment.pop('TOFU_RESOURCE_PROFILE') == profile
    assert environment == plain


def test_new_policy_replaces_only_defaults_attributed_to_the_old_policy():
    environment = {
        guards.RESOURCE_BUDGET_POLICY_ENV: 'old-policy',
        guards.RESOURCE_BUDGET_AUTOMATIC_ENV: (
            'TOFU_AGENT_WORKERS,TOFU_MAX_INFLIGHT_TASKS,TOFU_MALLOC_ARENA_MAX,'
            'TOFU_PROCESS_RSS_RECYCLE_MB,TOFU_TIMER_LIVE_CAP'),
        'TOFU_AGENT_WORKERS': '4',
        'TOFU_MAX_INFLIGHT_TASKS': '4',
        'TOFU_MALLOC_ARENA_MAX': '2',
        'MALLOC_ARENA_MAX': '2',
        # Retired names an older policy materialized; its marker proves
        # Tofu wrote them even though nothing catalogs them any more.
        'TOFU_PROCESS_RSS_RECYCLE_MB': '6144',
        'TOFU_TIMER_LIVE_CAP': '8',
        # Operator values were never listed in the provenance marker.
        'TOFU_SYNC_WORKERS': '3',
        'TOFU_TASK_RSS_RESERVE_MB': '768',
    }
    snapshot = _resource_snapshot(
        cpus=64, capacity_mb=256 * 1024, available_mb=220 * 1024)

    manifest = install_runtime_resource_defaults(environment, snapshot=snapshot)

    assert environment['TOFU_AGENT_WORKERS'] == '16'
    for retired in ('TOFU_MAX_INFLIGHT_TASKS', 'TOFU_PROCESS_RSS_RECYCLE_MB',
                    'TOFU_TIMER_LIVE_CAP', 'TOFU_MALLOC_ARENA_MAX',
                    'MALLOC_ARENA_MAX'):
        assert retired not in environment, retired
    assert environment['TOFU_SYNC_WORKERS'] == '3'
    assert environment['TOFU_TASK_RSS_RESERVE_MB'] == '768'
    assert manifest['overrides'] == {'TOFU_SYNC_WORKERS': '3'}

    assert install_process_resource_defaults(
        environment, snapshot=snapshot) == {'malloc_arena_max': 8}
    settled = dict(environment)
    # The same policy keeps its own automatic values on an in-place re-exec.
    install_process_resource_defaults(environment, snapshot=_failed_probe_snapshot())
    assert environment == settled


def test_resource_policy_generator_rejects_unclassified_and_bounded_unlimited():
    import copy
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        'gen_resource_policy_under_test', root / 'scripts/gen_resource_policy.py')
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    document = json.loads(
        (root / 'contracts/resource_policy_v1.json').read_text(encoding='utf-8'))
    assert generator.generate(copy.deepcopy(document)) == (
        root / 'bootstrap_pkg/resource_policy_generated.py').read_text(encoding='utf-8')

    def broken(name, **changes):
        candidate = copy.deepcopy(document)
        setting = candidate['launch_settings'][name]
        for key, value in changes.items():
            if value is None:
                setting.pop(key)
            else:
                setting[key] = value
        return candidate

    for candidate, message in (
            (broken('TOFU_AGENT_WORKERS', **{'class': None}), 'class must be one of'),
            (broken('TOFU_AGENT_WORKERS', **{'class': 'elastic'}), 'class must be one of'),
            (broken('TOFU_AGENT_WORKERS', **{'class': 'unlimited'}), 'unlimited'),
            (broken('TOFU_MAX_INFLIGHT_TASKS', defaults={
                'personal': 'unlimited', 'distributed': 16}), 'unlimited'),
            (broken('TOFU_MAX_INFLIGHT_TASKS', **{'class': 'fixed'}),
             'defaults must be integers')):
        with pytest.raises(SystemExit, match=message):
            generator.generate(candidate)


@pytest.mark.parametrize('retired', [
    'TOFU_TASK_RSS_RESERVE_MB',
    'TOFU_PROCESS_RSS_RELIEF_MB',
    'TOFU_PROCESS_RSS_RECYCLE_MB',
    'TOFU_SWARM_GLOBAL_WORKERS',
    'TOFU_SWARM_MAX_AGENTS_PER_WAVE',
    'TOFU_SWARM_MAX_AGENTS_PER_SESSION',
    'TOFU_TIMER_LIVE_CAP',
    'TOFU_PROJECT_REFRESH_QUEUE_CAPACITY',
    'TOFU_PDF_MAX_PAGES',
    'TOFU_PAPER_BUILD_QUEUE_CAPACITY',
    'TOFU_MAX_SSE_PER_PRINCIPAL',
    'TOFU_PRODUCTION_LLM_FANOUT',
    'TOFU_INCREMENTAL_TRANSLATE_ACTIVE',
    'TOFU_INCREMENTAL_TRANSLATE_QUEUE_CAPACITY',
    'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_SEGMENTS',
    'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MIN_CHARS',
])
def test_retired_launch_settings_are_not_cataloged(retired):
    assert retired not in _CONTRACT
    assert retired not in guards.RESOURCE_BUDGET_ENV_KEYS
    for environment in ({}, _DISTRIBUTED):
        with pytest.raises(KeyError):
            deployment_resource_default(
                retired, environment, snapshot=_resource_snapshot())
    with pytest.raises(KeyError):
        resolve_resource_budget(
            retired, {retired: '99'}, maximum=8, snapshot=_resource_snapshot())


def test_personal_fastpath_dual_wal_trigger_uses_four_percent_of_free_disk():
    snapshot = _resource_snapshot(disk_free_mb=100 * 1024)
    budget_mib = deployment_resource_default(
        'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB', {}, snapshot=snapshot)

    assert budget_mib == 2 * 1024
    assert budget_mib * 2 == int(snapshot.disk_free_mb * 0.04)


def test_valid_resource_override_does_not_probe_an_unused_default(monkeypatch):
    name = 'TOFU_PROJECT_UNDO_CACHE_CAPACITY'

    def unexpected_default(*_args, **_kwargs):
        raise AssertionError('valid explicit budget must bypass default probing')

    monkeypatch.setattr(
        guards, 'deployment_resource_default', unexpected_default)

    assert guards.resolve_resource_budget(
        name, {name: '17'}, minimum=2, maximum=32) == 17
    assert guards.resolve_resource_budget(
        name, {name: '999'}, minimum=2, maximum=32) == 32


def test_resource_override_resolution_never_becomes_unbounded():
    snapshot = _resource_snapshot()
    name = 'TOFU_PROJECT_UNDO_CACHE_CAPACITY'

    assert resolve_resource_budget(
        name, {name: '999999'}, maximum=4096,
        snapshot=snapshot) == 4096
    assert resolve_resource_budget(
        name, {name: 'invalid'}, maximum=4096,
        snapshot=snapshot) == 128
    assert resolve_resource_budget(
        name, {name: '0'}, maximum=4096,
        snapshot=snapshot) == 128
    # Operator overrides stay inside each consumer's own hard ceiling.
    for override, maximum in (
            ('TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS', 86_400),
            ('TOFU_TOOL_RESULT_CACHE_CAPACITY', 1024),
            ('TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS', 64),
            ('TOFU_PRODUCTION_IMAGE_FANOUT', 4),
            ('TOFU_PRODUCTION_TTS_FANOUT', 8),
            ('TOFU_USAGE_CACHE_CAPACITY', 8192)):
        assert resolve_resource_budget(
            override, {override: '999999'}, maximum=maximum,
            snapshot=snapshot) == maximum, override
    # An unlimited default resolves to whatever finite cap a caller names.
    assert resolve_resource_budget(
        'TOFU_MAX_INFLIGHT_TASKS', {'TOFU_MAX_INFLIGHT_TASKS': 'invalid'},
        maximum=1024) == 1024

    timeout_name = 'TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS'
    assert resolve_resource_budget(
        timeout_name,
        {timeout_name: '999999'},
        minimum=1800,
        maximum=86400,
        snapshot=snapshot,
    ) == 86400
    assert resolve_resource_budget(
        timeout_name,
        {timeout_name: 'invalid'},
        minimum=1800,
        maximum=86400,
        snapshot=snapshot,
    ) == 17_800
    assert storage_backup_timeout_seconds(
        {timeout_name: '999999'}, snapshot=snapshot) == 86_400
    assert storage_backup_timeout_seconds(
        {timeout_name: 'invalid'}, snapshot=snapshot) == 17_800


def test_personal_manifest_reports_the_probe_evidence():
    snapshot = _resource_snapshot(
        cpus=2, capacity_mb=4096, available_mb=2048,
        disk_free_mb=3 * 1024)
    manifest = resource_budget_manifest({}, snapshot=snapshot)

    assert manifest['adaptive'] is True
    assert manifest['probe']['data_path']
    assert manifest['probe']['effective_memory_capacity_mb'] == 4096
    assert 'resource_profile' not in manifest
    assert 'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_SEGMENTS' not in manifest
    assert resource_budget_manifest(
        _DISTRIBUTED, snapshot=snapshot)['adaptive'] is False


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

    monkeypatch.setattr(system_resources.shutil, 'disk_usage', fake_disk_usage)

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

    monkeypatch.setattr(system_resources.shutil, 'disk_usage', fake_disk_usage)

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
    # Eight 2-GiB retention units; a 1-GiB cgroup misreading would give one.
    assert resource_budget_manifest({}, snapshot=snapshot)['defaults'][
        'TOFU_TOKEN_COUNT_CACHE_CAPACITY'] == 512


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

    from tofu_workers import resources
    monkeypatch.setattr(resources, '_read_first_text', fake_read)

    assert resources._cgroup_file_paths(
        'memory', 'memory.max', ()) == (
            '/sys/fs/cgroup/user.slice/tofu.service/memory.max',)


def test_probe_failure_degrades_retention_to_lean_fallbacks(monkeypatch):
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
    assert {name: defaults[name] for name in _names('fixed')} == {
        name: personal for name, (personal, _) in _FIXED_DEFAULTS.items()}
    assert defaults['TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB'] == 128
    assert defaults['TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY'] == 512
    assert defaults['TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY'] == 512
    assert defaults['TOFU_TOOL_RESULT_CACHE_CAPACITY'] == 64
    assert defaults['TOFU_TOKEN_COUNT_CACHE_CAPACITY'] == 64
    assert defaults['TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS'] == 600
    assert defaults['TOFU_MEMORY_METADATA_CACHE_CAPACITY'] == 512
    assert defaults['TOFU_MEMORY_METADATA_CACHE_MAX_MIB'] == 4
    assert defaults['TOFU_PAPER_QA_SOURCE_CACHE_CAPACITY'] == 1
    assert defaults['TOFU_PAPER_BUILD_CACHE_MAX_MIB'] == 256
    assert defaults['TOFU_PAPER_BUILD_CACHE_RECORDS'] == 16
    assert defaults['TOFU_LOG_TOTAL_BUDGET_MB'] == 128
    assert defaults['TOFU_TRANSLATE_CACHE_MAX_MIB'] == 128
    assert defaults['TOFU_SWARM_SESSION_CAPACITY'] == 2
    assert defaults['TOFU_STORAGE_MIN_FREE_BYTES'] == 256 * 1024 * 1024
    assert defaults['TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB'] == 64 * 1024
    assert defaults['TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS'] == 5896
    assert defaults['TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB'] == 512
    assert defaults['TOFU_BROWSER_STAGING_MAX_MIB'] == 256
    assert defaults['TOFU_RAW_ARCHIVE_BUDGET_MIB'] == 256
    assert defaults['TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY'] == 64
    assert defaults['TOFU_MCP_STDIO_IDLE_SECONDS'] == 300
    assert defaults['TOFU_EXECUTOR_IDLE_SECONDS'] == 600
    assert defaults['TOFU_PROJECT_REFRESH_IDLE_SECONDS'] == 60
    assert defaults['TOFU_TREE_INDEX_MAX_ENTRIES'] == 100_000
    assert defaults['TOFU_TREE_INDEX_MEM_ROOTS'] == 2


def test_distributed_allocator_default_and_override_are_bounded():
    distributed = dict(_DISTRIBUTED)
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


def test_malformed_allocator_override_falls_back_to_the_fixed_default():
    environment = {'TOFU_MALLOC_ARENA_MAX': 'not-a-number'}

    assert install_process_resource_defaults(
            environment, snapshot=_resource_snapshot()) == {
        'malloc_arena_max': 8,
    }
    assert environment['MALLOC_ARENA_MAX'] == '8'


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


@pytest.mark.parametrize(
    'daemon_name',
    [
        'TOFU_DB_FSYNC_STATS',
        'TOFU_DB_REQUEST_PROFILE_EVERY',
        'TOFU_DB_WRITE_OVERLAY',
        'TOFU_DB_OVERLAY_BUDGET_BYTES',
        'TOFU_DB_OVERLAY_WAL_WATERMARK_BYTES',
        'TOFU_DB_CHECKPOINT_MATERIALIZE_BATCH_BYTES',
        'TOFU_DB_SEGMENT_RETIREMENT_DELAY_MS',
        'TOFU_DB_TOMBSTONE_RECLAIM',
    ],
)
def test_daemon_passthrough_knobs_are_not_removed_configuration(daemon_name):
    # Daemon-consumed Rust knobs live in _DAEMON_ENV_PASSTHROUGH: the blanket
    # TOFU_DB_* stale-knob rejection must not refuse them (the write overlay
    # switch opts one daemon in, it is not a topology switch).
    config = load_deployment_configuration({daemon_name: '1'})

    assert config.mode == 'personal'


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


def test_cached_launch_probe_does_not_rescan_data_layout(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(guards, '_RESOURCE_SNAPSHOT_CACHE', {})
    def data_path(_env):
        calls.append('resolve')
        return tmp_path
    monkeypatch.setattr(guards, '_persistent_data_path', data_path)
    first = guards.probe_system_resources({'TOFU_DATA_LAYOUT': 'auto'})
    for _ in range(100):
        assert guards.probe_system_resources({'TOFU_DATA_LAYOUT': 'auto'}) is first
    assert calls == ['resolve']
    assert guards.probe_system_resources({'TOFU_DATA_LAYOUT': 'auto'}, refresh=True) is not first
    assert calls == ['resolve', 'resolve']


def test_launch_probe_cache_tracks_layout_changes_and_is_bounded(monkeypatch):
    monkeypatch.setattr(guards, '_RESOURCE_SNAPSHOT_CACHE', {})
    monkeypatch.setattr(guards, '_project_disk_bytes', lambda env: (1000, 500))
    a = guards.probe_system_resources({'TOFU_DATA_DIR': '/first'})
    assert guards.probe_system_resources({'TOFU_DATA_DIR': '/second'}) is not a
    for index in range(100):
        guards.probe_system_resources({'TOFU_DATA_DIR': f'/volume-{index}'})
    assert len(guards._RESOURCE_SNAPSHOT_CACHE) <= guards._RESOURCE_SNAPSHOT_CACHE_CAPACITY


def test_vlm_page_admission_is_retired_across_launch_profiles():
    import json
    from bootstrap_pkg.resource_policy_generated import RESOURCE_FALLBACKS

    retired = 'TOFU_PDF_VLM_MAX_PAGES'
    contract = json.loads((Path(__file__).resolve().parents[2] /
                           'contracts/resource_policy_v1.json').read_text())
    assert retired not in contract['launch_settings']
    assert all(retired not in profile for profile in RESOURCE_FALLBACKS.values())
    for capacity, available in [(4096, 2048), (8192, 6144), (65536, 49152)]:
        snapshot = _resource_snapshot(capacity_mb=capacity, available_mb=available)
        environment = {}
        install_process_resource_defaults(environment, snapshot=snapshot)
        assert retired not in environment
        assert int(environment['TOFU_PDF_VLM_TASK_TIMEOUT_SECONDS']) >= 1800
        assert int(environment['TOFU_PDF_VLM_CALL_WORKERS']) >= 1


def test_evolution_terminal_queue_admission_setting_is_retired():
    import json
    from bootstrap_pkg.resource_policy_generated import RESOURCE_FALLBACKS
    from runtime_guards import resource_budget_manifest
    root = Path(__file__).resolve().parents[2]
    retired = 'TOFU_EVOLUTION_TERMINAL_QUEUE_CAPACITY'
    assert retired not in json.loads((root / 'contracts/resource_policy_v1.json').read_text())['launch_settings']
    assert retired not in json.loads((root / 'contracts/config_knobs_v1.json').read_text())['knobs']
    assert all(retired not in values for values in RESOURCE_FALLBACKS.values())
    assert retired not in resource_budget_manifest({}, snapshot=_resource_snapshot())['defaults']


def test_evolution_terminal_idle_reuse_is_a_fixed_hint():
    import sys
    from runtime_guards import resource_budget_manifest, resolve_resource_budget
    small = resource_budget_manifest({}, snapshot=_resource_snapshot(
        cpus=2, capacity_mb=4096, available_mb=2048, disk_free_mb=3 * 1024))
    large = resource_budget_manifest({}, snapshot=_resource_snapshot(
        cpus=256, capacity_mb=512 * 1024, available_mb=400 * 1024,
        disk_free_mb=1024 * 1024))
    assert large['defaults']['TOFU_AGENT_WORKERS'] == small['defaults']['TOFU_AGENT_WORKERS'] == 16
    assert resolve_resource_budget('TOFU_AGENT_WORKERS', {'TOFU_AGENT_WORKERS': '1'}, maximum=sys.maxsize) == 1


def test_pdf_validator_memory_is_only_an_explicit_operator_override():
    import json
    from bootstrap_pkg.resource_policy_generated import RESOURCE_FALLBACKS
    from lib.paper.builds.budget import validation_memory_bytes
    name='TOFU_PAPER_BUILD_VALIDATION_MAX_MIB'
    contract=json.loads((Path(__file__).resolve().parents[2]/'contracts/resource_policy_v1.json').read_text())
    assert name not in contract['launch_settings']
    assert all(name not in profile for profile in RESOURCE_FALLBACKS.values())
    environment={}
    install_process_resource_defaults(environment,snapshot=_resource_snapshot())
    assert name not in environment and validation_memory_bytes(environment)==0
    environment[name]='2048'
    install_process_resource_defaults(environment,snapshot=_resource_snapshot())
    assert validation_memory_bytes(environment)==2048*1024*1024


def test_classic_pdf_parse_wait_is_not_injected_as_a_launch_quota():
    import json
    from bootstrap_pkg.resource_policy_generated import RESOURCE_FALLBACKS
    from lib.pdf_parser.policy import resolve_classic_pdf_budget
    name='TOFU_PDF_PARSE_TIMEOUT'
    contract=json.loads((Path(__file__).resolve().parents[2]/'contracts/resource_policy_v1.json').read_text())
    assert name not in contract['launch_settings']
    assert all(name not in profile for profile in RESOURCE_FALLBACKS.values())
    environment={};install_process_resource_defaults(environment,snapshot=_resource_snapshot())
    assert name not in environment and resolve_classic_pdf_budget(environment).timeout_seconds==0
    environment[name]='5000';install_process_resource_defaults(environment,snapshot=_resource_snapshot())
    assert resolve_classic_pdf_budget(environment).timeout_seconds==5000
