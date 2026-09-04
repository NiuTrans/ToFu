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
    assert environment['TOFU_STORAGE_RPC_CAPACITY'] == '8'
    assert environment['TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB'] == '128'
    assert environment['TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY'] == '16'
    assert environment['TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB'] == '32'
    assert environment['TOFU_CONTROL_RPC_WORKERS'] == '8'
    assert environment['TOFU_MCP_STDIO_IDLE_SECONDS'] == '180'
    assert environment['TOFU_EXECUTOR_IDLE_SECONDS'] == '300'
    assert environment['TOFU_PROJECT_REFRESH_IDLE_SECONDS'] == '30'
    assert environment['TOFU_TREE_INDEX_WALK_JOBS'] == '8'
    assert environment['TOFU_TREE_INDEX_MAX_ENTRIES'] == '409600'
    assert environment['TOFU_TREE_INDEX_MEM_ROOTS'] == '2'
    assert environment['TOFU_MAX_SSE_PER_PRINCIPAL'] == '12'
    assert environment['TOFU_SERVER_PYTHON_CACHE_MAX_MIB'] == '64'
    assert environment['TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY'] == '1024'
    assert environment['TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY'] == '1024'
    assert environment['TOFU_TOOL_RESULT_CACHE_CAPACITY'] == '128'
    assert environment['TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS'] == '600'
    assert environment['TOFU_MEMORY_METADATA_CACHE_CAPACITY'] == '2048'
    assert environment['TOFU_MEMORY_METADATA_CACHE_MAX_MIB'] == '16'
    assert environment['TOFU_PAPER_QA_SOURCE_CACHE_CAPACITY'] == '2'
    assert environment['TOFU_TRANSLATE_CACHE_MAX_MIB'] == '256'
    assert environment['TOFU_TRANSLATE_MAX_429_ATTEMPTS'] == '8'
    assert environment['TOFU_TRANSLATE_WORKERS'] == '2'
    assert environment['TOFU_TRANSLATE_QUEUE_CAPACITY'] == '16'
    assert environment['TOFU_TRANSLATE_WORKER_IDLE_SECONDS'] == '60'
    assert environment['TOFU_OPTIONAL_LLM_MAX_429_ATTEMPTS'] == '2'
    assert environment['TOFU_PRODUCTION_LLM_FANOUT'] == '2'
    assert environment['TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS'] == '8'
    assert environment['TOFU_PRODUCTION_IMAGE_FANOUT'] == '2'
    assert environment['TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS'] == '8'
    assert environment['TOFU_PRODUCTION_TTS_FANOUT'] == '2'
    assert environment['TOFU_PDF_VLM_TASK_WORKERS'] == '1'
    assert environment['TOFU_PDF_VLM_QUEUE_CAPACITY'] == '2'
    assert environment['TOFU_PDF_VLM_WORKER_IDLE_SECONDS'] == '60'
    assert environment['TOFU_PDF_VLM_CALL_WORKERS'] == '2'
    assert environment['TOFU_PDF_VLM_MAX_PAGES'] == '128'
    assert environment['TOFU_PDF_VLM_TASK_TIMEOUT_SECONDS'] == '3840'
    assert environment['TOFU_PDF_VLM_MAX_429_ATTEMPTS'] == '8'
    assert environment['TOFU_KNOWLEDGE_ENRICH_WORKERS'] == '1'
    assert environment['TOFU_KNOWLEDGE_ENRICH_OWNER_CAPACITY'] == '16'
    assert environment['TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS'] == '60'
    assert environment['TOFU_SWARM_GLOBAL_WORKERS'] == '2'
    assert environment['TOFU_SWARM_MAX_PARALLEL'] == '2'
    assert environment['TOFU_SWARM_MAX_AGENTS_PER_WAVE'] == '4'
    assert environment['TOFU_SWARM_MAX_AGENTS_PER_SESSION'] == '12'
    assert environment['TOFU_SWARM_MAX_RETRIES'] == '1'
    assert environment['TOFU_SWARM_SESSION_CAPACITY'] == '4'
    assert environment['TOFU_MCP_CRED_PROBE_WORKERS'] == '2'
    assert environment['TOFU_PROCESS_RSS_RECYCLE_MB'] == '3072'


def test_runtime_snapshot_materializes_defaults_without_overwriting_operator():
    environment = {'TOFU_MAX_INFLIGHT_TASKS': '7'}

    manifest = install_runtime_resource_defaults(
        environment, snapshot=_resource_snapshot())

    assert manifest['overrides'] == {'TOFU_MAX_INFLIGHT_TASKS': '7'}
    assert environment['TOFU_MAX_INFLIGHT_TASKS'] == '7'
    assert environment['TOFU_SYNC_WORKERS'] == '8'
    assert environment['TOFU_STORAGE_RPC_CAPACITY'] == '8'
    assert environment['TOFU_CONTROL_RPC_WORKERS'] == '8'
    assert environment[guards.RESOURCE_BUDGET_POLICY_ENV] \
        == guards.RESOURCE_BUDGET_POLICY_VERSION
    automatic = set(environment[
        guards.RESOURCE_BUDGET_AUTOMATIC_ENV].split(','))
    assert 'TOFU_AGENT_WORKERS' in automatic
    assert 'TOFU_MAX_INFLIGHT_TASKS' not in automatic
    assert manifest['automatic'] == sorted(automatic)


def test_new_policy_replaces_only_defaults_attributed_to_the_old_policy():
    environment = {
        guards.RESOURCE_BUDGET_POLICY_ENV: 'old-policy',
        guards.RESOURCE_BUDGET_AUTOMATIC_ENV: (
            'TOFU_AGENT_WORKERS,TOFU_MAX_INFLIGHT_TASKS,'
            'TOFU_PROCESS_RSS_RECYCLE_MB'),
        'TOFU_AGENT_WORKERS': '4',
        'TOFU_MAX_INFLIGHT_TASKS': '4',
        'TOFU_PROCESS_RSS_RECYCLE_MB': '6144',
        # An operator value was never listed in the provenance marker.
        'TOFU_TASK_RSS_RESERVE_MB': '768',
    }
    snapshot = _resource_snapshot(
        cpus=64,
        capacity_mb=256 * 1024,
        available_mb=220 * 1024,
    )

    manifest = install_runtime_resource_defaults(
        environment, snapshot=snapshot)

    assert environment['TOFU_AGENT_WORKERS'] == '48'
    assert environment['TOFU_MAX_INFLIGHT_TASKS'] == '48'
    assert environment['TOFU_PROCESS_RSS_RECYCLE_MB'] == str(64 * 1024)
    assert environment['TOFU_TASK_RSS_RESERVE_MB'] == '768'
    assert manifest['overrides'] == {'TOFU_TASK_RSS_RESERVE_MB': '768'}


def test_resource_profiles_keep_personal_small_without_hardcoding_core_logic():
    personal = {}
    distributed = {'TOFU_DEPLOYMENT_MODE': 'distributed'}
    snapshot = _resource_snapshot()

    assert deployment_resource_default(
        'TOFU_MAX_INFLIGHT_TASKS', personal, snapshot=snapshot) == 4
    assert deployment_resource_default(
        'TOFU_STORAGE_RPC_CAPACITY', personal, snapshot=snapshot) == 8
    assert deployment_resource_default(
        'TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB', personal,
        snapshot=snapshot) == 128
    assert deployment_resource_default(
        'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY', personal,
        snapshot=snapshot) == 16
    assert deployment_resource_default(
        'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB', personal,
        snapshot=snapshot) == 2048
    assert deployment_resource_default(
        'TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS', personal,
        snapshot=snapshot) == 17_800
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
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_SEGMENTS', personal,
        snapshot=snapshot) == 32
    assert deployment_resource_default(
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_DEADLINE_SECONDS', personal,
        snapshot=snapshot) == 30
    assert deployment_resource_default(
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MIN_CHARS', personal,
        snapshot=snapshot) == 256
    assert deployment_resource_default(
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MAX_429_ATTEMPTS', personal,
        snapshot=snapshot) == 1
    assert deployment_resource_default(
        'TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB', personal,
        snapshot=snapshot) == 64
    assert deployment_resource_default(
        'TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB', personal,
        snapshot=snapshot) == 32
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
        'TOFU_USAGE_CACHE_CAPACITY', personal,
        snapshot=snapshot) == 256
    assert deployment_resource_default(
        'TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY', personal,
        snapshot=snapshot) == 1024
    assert deployment_resource_default(
        'TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY', personal,
        snapshot=snapshot) == 1024
    assert deployment_resource_default(
        'TOFU_TOOL_RESULT_CACHE_CAPACITY', personal,
        snapshot=snapshot) == 128
    assert deployment_resource_default(
        'TOFU_TOOL_RESULT_CACHE_CAPACITY', distributed) == 512
    assert deployment_resource_default(
        'TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS', personal,
        snapshot=snapshot) == 600
    assert deployment_resource_default(
        'TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS', distributed) == 3600
    assert resolve_resource_budget(
        'TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS',
        {'TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS': '999999'},
        maximum=86_400,
        snapshot=snapshot,
    ) == 86_400
    assert resolve_resource_budget(
        'TOFU_TOOL_RESULT_CACHE_CAPACITY',
        {'TOFU_TOOL_RESULT_CACHE_CAPACITY': '999999'}, maximum=1024,
        snapshot=snapshot) == 1024
    assert deployment_resource_default(
        'TOFU_MEMORY_METADATA_CACHE_CAPACITY', personal,
        snapshot=snapshot) == 2048
    assert deployment_resource_default(
        'TOFU_MEMORY_METADATA_CACHE_MAX_MIB', personal,
        snapshot=snapshot) == 16
    assert deployment_resource_default(
        'TOFU_PAPER_QA_SOURCE_CACHE_CAPACITY', personal,
        snapshot=snapshot) == 2
    assert deployment_resource_default(
        'TOFU_PAPER_QA_SOURCE_CACHE_CAPACITY', distributed) == 8
    assert deployment_resource_default(
        'TOFU_TRANSLATE_CACHE_MAX_MIB', personal,
        snapshot=snapshot) == 256
    assert deployment_resource_default(
        'TOFU_TRANSLATE_MAX_429_ATTEMPTS', personal,
        snapshot=snapshot) == 8
    assert deployment_resource_default(
        'TOFU_TRANSLATE_WORKERS', personal, snapshot=snapshot) == 2
    assert deployment_resource_default(
        'TOFU_TRANSLATE_QUEUE_CAPACITY', personal, snapshot=snapshot) == 16
    assert deployment_resource_default(
        'TOFU_TRANSLATE_WORKER_IDLE_SECONDS', personal,
        snapshot=snapshot) == 60
    assert deployment_resource_default(
        'TOFU_PRODUCTION_LLM_FANOUT', personal, snapshot=snapshot) == 2
    assert deployment_resource_default(
        'TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS', personal,
        snapshot=snapshot) == 8
    assert deployment_resource_default(
        'TOFU_PRODUCTION_IMAGE_FANOUT', personal, snapshot=snapshot) == 2
    assert deployment_resource_default(
        'TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS', personal,
        snapshot=snapshot) == 8
    assert deployment_resource_default(
        'TOFU_PRODUCTION_TTS_FANOUT', personal, snapshot=snapshot) == 2
    assert resolve_resource_budget(
        'TOFU_PRODUCTION_LLM_FANOUT',
        {'TOFU_PRODUCTION_LLM_FANOUT': '99'}, maximum=8,
        snapshot=snapshot) == 8
    assert resolve_resource_budget(
        'TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS',
        {'TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS': '999'}, maximum=64,
        snapshot=snapshot) == 64
    assert resolve_resource_budget(
        'TOFU_PRODUCTION_IMAGE_FANOUT',
        {'TOFU_PRODUCTION_IMAGE_FANOUT': '99'}, maximum=4,
        snapshot=snapshot) == 4
    assert resolve_resource_budget(
        'TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS',
        {'TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS': '999'}, maximum=64,
        snapshot=snapshot) == 64
    assert resolve_resource_budget(
        'TOFU_PRODUCTION_TTS_FANOUT',
        {'TOFU_PRODUCTION_TTS_FANOUT': '99'}, maximum=8,
        snapshot=snapshot) == 8
    assert deployment_resource_default(
        'TOFU_PDF_VLM_TASK_WORKERS', personal, snapshot=snapshot) == 1
    assert deployment_resource_default(
        'TOFU_PDF_VLM_QUEUE_CAPACITY', personal, snapshot=snapshot) == 2
    assert deployment_resource_default(
        'TOFU_PDF_VLM_WORKER_IDLE_SECONDS', personal,
        snapshot=snapshot) == 60
    assert deployment_resource_default(
        'TOFU_PDF_VLM_CALL_WORKERS', personal, snapshot=snapshot) == 2
    assert deployment_resource_default(
        'TOFU_PDF_VLM_MAX_PAGES', personal, snapshot=snapshot) == 128
    assert deployment_resource_default(
        'TOFU_PDF_VLM_TASK_TIMEOUT_SECONDS', personal,
        snapshot=snapshot) == 3840
    assert deployment_resource_default(
        'TOFU_PDF_VLM_MAX_429_ATTEMPTS', personal,
        snapshot=snapshot) == 8
    assert deployment_resource_default(
        'TOFU_KNOWLEDGE_ENRICH_WORKERS', personal,
        snapshot=snapshot) == 1
    assert deployment_resource_default(
        'TOFU_KNOWLEDGE_ENRICH_OWNER_CAPACITY', personal,
        snapshot=snapshot) == 16
    assert deployment_resource_default(
        'TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS', personal,
        snapshot=snapshot) == 60
    assert deployment_resource_default(
        'TOFU_SWARM_GLOBAL_WORKERS', personal, snapshot=snapshot) == 2
    assert deployment_resource_default(
        'TOFU_SWARM_MAX_PARALLEL', personal, snapshot=snapshot) == 2
    assert deployment_resource_default(
        'TOFU_SWARM_MAX_AGENTS_PER_WAVE', personal,
        snapshot=snapshot) == 4
    assert deployment_resource_default(
        'TOFU_SWARM_MAX_AGENTS_PER_SESSION', personal,
        snapshot=snapshot) == 12
    assert deployment_resource_default(
        'TOFU_SWARM_MAX_RETRIES', personal, snapshot=snapshot) == 1
    assert deployment_resource_default(
        'TOFU_SWARM_SESSION_CAPACITY', personal, snapshot=snapshot) == 4
    assert deployment_resource_default(
        'TOFU_MCP_CRED_PROBE_WORKERS', personal, snapshot=snapshot) == 2
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
        'TOFU_STORAGE_RPC_CAPACITY', distributed) == 64
    assert deployment_resource_default(
        'TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB', distributed) == 1024
    assert deployment_resource_default(
        'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY', distributed) == 128
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
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_SEGMENTS', distributed) == 256
    assert deployment_resource_default(
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_DEADLINE_SECONDS', distributed) == 60
    assert deployment_resource_default(
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MIN_CHARS', distributed) == 256
    assert deployment_resource_default(
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MAX_429_ATTEMPTS', distributed) == 1
    assert deployment_resource_default(
        'TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB', distributed) == 64
    assert deployment_resource_default(
        'TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB', distributed) == 256
    assert deployment_resource_default(
        'TOFU_RUN_PYTHON_CACHE_MAX_MIB', distributed) == 128
    assert deployment_resource_default(
        'TOFU_SERVER_PYTHON_CACHE_MAX_MIB', distributed) == 128
    assert deployment_resource_default(
        'TOFU_TOKEN_COUNT_CACHE_CAPACITY', distributed) == 1024
    assert deployment_resource_default(
        'TOFU_USAGE_CACHE_CAPACITY', distributed) == 4096
    assert resolve_resource_budget(
        'TOFU_USAGE_CACHE_CAPACITY',
        {'TOFU_USAGE_CACHE_CAPACITY': '999999'},
        maximum=8192,
    ) == 8192
    assert deployment_resource_default(
        'TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY', distributed) == 4096
    assert deployment_resource_default(
        'TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY', distributed) == 4096
    assert deployment_resource_default(
        'TOFU_MEMORY_METADATA_CACHE_CAPACITY', distributed) == 8192
    assert deployment_resource_default(
        'TOFU_MEMORY_METADATA_CACHE_MAX_MIB', distributed) == 64
    assert deployment_resource_default(
        'TOFU_TRANSLATE_CACHE_MAX_MIB', distributed) == 1024
    assert deployment_resource_default(
        'TOFU_TRANSLATE_MAX_429_ATTEMPTS', distributed) == 16
    assert deployment_resource_default(
        'TOFU_TRANSLATE_WORKERS', distributed) == 16
    assert deployment_resource_default(
        'TOFU_TRANSLATE_QUEUE_CAPACITY', distributed) == 128
    assert deployment_resource_default(
        'TOFU_TRANSLATE_WORKER_IDLE_SECONDS', distributed) == 600
    assert deployment_resource_default(
        'TOFU_OPTIONAL_LLM_MAX_429_ATTEMPTS', distributed) == 8
    assert deployment_resource_default(
        'TOFU_PRODUCTION_LLM_FANOUT', distributed) == 4
    assert deployment_resource_default(
        'TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS', distributed) == 16
    assert deployment_resource_default(
        'TOFU_PRODUCTION_IMAGE_FANOUT', distributed) == 4
    assert deployment_resource_default(
        'TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS', distributed) == 16
    assert deployment_resource_default(
        'TOFU_PRODUCTION_TTS_FANOUT', distributed) == 4
    assert deployment_resource_default(
        'TOFU_PDF_VLM_TASK_WORKERS', distributed) == 4
    assert deployment_resource_default(
        'TOFU_PDF_VLM_QUEUE_CAPACITY', distributed) == 32
    assert deployment_resource_default(
        'TOFU_PDF_VLM_WORKER_IDLE_SECONDS', distributed) == 600
    assert deployment_resource_default(
        'TOFU_PDF_VLM_CALL_WORKERS', distributed) == 8
    assert deployment_resource_default(
        'TOFU_PDF_VLM_MAX_PAGES', distributed) == 512
    assert deployment_resource_default(
        'TOFU_PDF_VLM_TASK_TIMEOUT_SECONDS', distributed) == 14_400
    assert deployment_resource_default(
        'TOFU_PDF_VLM_MAX_429_ATTEMPTS', distributed) == 16
    assert deployment_resource_default(
        'TOFU_KNOWLEDGE_ENRICH_WORKERS', distributed) == 8
    assert deployment_resource_default(
        'TOFU_KNOWLEDGE_ENRICH_OWNER_CAPACITY', distributed) == 128
    assert deployment_resource_default(
        'TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS', distributed) == 600
    assert deployment_resource_default(
        'TOFU_SWARM_GLOBAL_WORKERS', distributed) == 16
    assert deployment_resource_default(
        'TOFU_SWARM_MAX_PARALLEL', distributed) == 8
    assert deployment_resource_default(
        'TOFU_SWARM_MAX_AGENTS_PER_WAVE', distributed) == 16
    assert deployment_resource_default(
        'TOFU_SWARM_MAX_AGENTS_PER_SESSION', distributed) == 64
    assert deployment_resource_default(
        'TOFU_SWARM_MAX_RETRIES', distributed) == 2
    assert deployment_resource_default(
        'TOFU_SWARM_SESSION_CAPACITY', distributed) == 32
    assert deployment_resource_default(
        'TOFU_MCP_CRED_PROBE_WORKERS', distributed) == 8
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
    assert deployment_resource_default(
        'TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS', distributed) == 21_600
    assert deployment_resource_default(
        'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB', distributed) == 16_384


def test_personal_fastpath_dual_wal_trigger_uses_four_percent_of_free_disk():
    snapshot = _resource_snapshot(disk_free_mb=100 * 1024)
    budget_mib = deployment_resource_default(
        'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB', {}, snapshot=snapshot)

    assert budget_mib == 2 * 1024
    assert budget_mib * 2 == int(snapshot.disk_free_mb * 0.04)


def test_valid_resource_override_does_not_probe_an_unused_default(monkeypatch):
    name = 'TOFU_PROJECT_REFRESH_QUEUE_CAPACITY'

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
        'TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB': 128,
        'TOFU_STORAGE_SQLITE_READ_POOL': 4,
        'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY': 8,
        'TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB': 32,
        'TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB': 16,
        'TOFU_TURN_SEARCH_PROJECTION_MAX_MIB': 128,
        'TOFU_BROWSER_STAGING_MAX_MIB': 64,
        'TOFU_RAW_ARCHIVE_BUDGET_MIB': 30,
        'TOFU_BROWSER_POLL_MAX_INFLIGHT': 8,
        'TOFU_BROWSER_POLL_MAX_WAITERS': 8,
        'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY': 64,
        'TOFU_BROWSER_SESSION_LEASE_CAPACITY': 64,
        'TOFU_BROWSER_POLL_BODY_MAX_MIB': 16,
        'TOFU_MAX_SSE_PER_PRINCIPAL': 8,
        'TOFU_RUN_PYTHON_CACHE_MAX_MIB': 16,
        'TOFU_SERVER_PYTHON_CACHE_MAX_MIB': 16,
        'TOFU_TOKEN_COUNT_CACHE_CAPACITY': 128,
        'TOFU_USAGE_CACHE_CAPACITY': 128,
        'TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY': 512,
        'TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY': 512,
        'TOFU_TOOL_RESULT_CACHE_CAPACITY': 64,
        'TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS': 600,
        'TOFU_TIMER_LIVE_CAP': 8,
        'TOFU_MEMORY_METADATA_CACHE_CAPACITY': 1024,
        'TOFU_MEMORY_METADATA_CACHE_MAX_MIB': 8,
        'TOFU_PAPER_QA_SOURCE_CACHE_CAPACITY': 1,
        'TOFU_TRANSLATE_CACHE_MAX_MIB': 32,
        'TOFU_TRANSLATE_MAX_429_ATTEMPTS': 4,
        'TOFU_TRANSLATE_WORKERS': 2,
        'TOFU_TRANSLATE_QUEUE_CAPACITY': 8,
        'TOFU_TRANSLATE_WORKER_IDLE_SECONDS': 60,
        'TOFU_OPTIONAL_LLM_MAX_429_ATTEMPTS': 2,
        'TOFU_PRODUCTION_LLM_FANOUT': 2,
        'TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS': 4,
        'TOFU_PRODUCTION_IMAGE_FANOUT': 2,
        'TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS': 4,
        'TOFU_PRODUCTION_TTS_FANOUT': 2,
        'TOFU_PDF_PROCESSES': 1,
        'TOFU_PDF_PARSE_CAPACITY': 3,
        'TOFU_PDF_MAX_PAGES': 256,
        'TOFU_PDF_MAX_TEXT_MIB': 2,
        'TOFU_PDF_PARSE_TIMEOUT': 512,
        'TOFU_PDF_WORKER_IDLE_SECONDS': 60,
        'TOFU_PDF_VLM_TASK_WORKERS': 1,
        'TOFU_PDF_VLM_QUEUE_CAPACITY': 2,
        'TOFU_PDF_VLM_WORKER_IDLE_SECONDS': 60,
        'TOFU_PDF_VLM_CALL_WORKERS': 1,
        'TOFU_PDF_VLM_MAX_PAGES': 64,
        'TOFU_PDF_VLM_TASK_TIMEOUT_SECONDS': 1920,
        'TOFU_PDF_VLM_MAX_429_ATTEMPTS': 4,
        'TOFU_KNOWLEDGE_ENRICH_WORKERS': 1,
        'TOFU_KNOWLEDGE_ENRICH_OWNER_CAPACITY': 8,
        'TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS': 60,
        'TOFU_SWARM_GLOBAL_WORKERS': 1,
        'TOFU_SWARM_MAX_PARALLEL': 1,
        'TOFU_SWARM_MAX_AGENTS_PER_WAVE': 2,
        'TOFU_SWARM_MAX_AGENTS_PER_SESSION': 6,
        'TOFU_SWARM_MAX_RETRIES': 1,
        'TOFU_SWARM_SESSION_CAPACITY': 2,
        'TOFU_MCP_CRED_PROBE_WORKERS': 1,
        'TOFU_CONTROL_RPC_WORKERS': 4,
        'TOFU_PROJECT_REFRESH_QUEUE_CAPACITY': 32,
        'TOFU_PROJECT_REFRESH_IDLE_SECONDS': 30,
        'TOFU_PROJECT_UNDO_CACHE_CAPACITY': 64,
        'TOFU_TREE_INDEX_WALK_JOBS': 4,
        'TOFU_TREE_INDEX_MAX_ENTRIES': 204_800,
        'TOFU_TREE_INDEX_MEM_ROOTS': 2,
        'TOFU_INCREMENTAL_TRANSLATE_ACTIVE': 4,
        'TOFU_INCREMENTAL_TRANSLATE_QUEUE_CAPACITY': 8,
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_SEGMENTS': 32,
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_DEADLINE_SECONDS': 30,
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MIN_CHARS': 256,
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_MAX_429_ATTEMPTS': 1,
        'TOFU_MAX_INFLIGHT_TASKS': 2,
        'TOFU_TASK_RSS_RESERVE_MB': 512,
        'TOFU_SYNC_WORKERS': 4,
        'TOFU_AGENT_WORKERS': 2,
        'TOOL_MAX_PARALLEL_WORKERS': 2,
        'TOFU_NUMERIC_THREADS': 1,
        'TOFU_MCP_STDIO_IDLE_SECONDS': 180,
        'TOFU_EXECUTOR_IDLE_SECONDS': 300,
        'TOFU_LOG_TOTAL_BUDGET_MB': 64,
        'TOFU_STORAGE_MIN_FREE_BYTES': 2048 * 1024 * 1024,
        'TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB': 250 * 1024,
        'TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS': 17_800,
        'TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB': 64,
        'TOFU_ATTEMPT_EVENT_TTL_DAYS': 1,
        'TOFU_PROCESS_RSS_RELIEF_MB': 1024,
        'TOFU_PROCESS_RSS_RECYCLE_MB': 1536,
    }


def test_personal_resource_probe_scales_large_hosts_with_bounded_parallelism():
    snapshot = _resource_snapshot(
        cpus=64, capacity_mb=64 * 1024, available_mb=48 * 1024,
        disk_total_mb=2 * 1024 * 1024,
        disk_free_mb=1024 * 1024)
    defaults = resource_budget_manifest({}, snapshot=snapshot)['defaults']

    assert defaults['TOFU_MAX_INFLIGHT_TASKS'] == 18
    assert defaults['TOFU_AGENT_WORKERS'] == 18
    assert defaults['TOOL_MAX_PARALLEL_WORKERS'] == 4
    assert defaults['TOFU_SYNC_WORKERS'] == 16
    assert defaults['TOFU_STORAGE_RPC_CAPACITY'] == 12
    assert defaults['TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB'] == 512
    assert defaults['TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY'] == 24
    assert defaults['TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY'] == 4096
    assert defaults['TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY'] == 4096
    assert defaults['TOFU_TOOL_RESULT_CACHE_CAPACITY'] == 256
    assert defaults['TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS'] == 1800
    assert defaults['TOFU_TIMER_LIVE_CAP'] == 16
    assert defaults['TOFU_NUMERIC_THREADS'] == 4
    assert defaults['TOFU_MALLOC_ARENA_MAX'] == 4
    assert defaults['TOFU_MCP_STDIO_IDLE_SECONDS'] == 600
    assert defaults['TOFU_EXECUTOR_IDLE_SECONDS'] == 1800
    assert defaults['TOFU_PROJECT_REFRESH_IDLE_SECONDS'] == 300
    assert defaults['TOFU_TREE_INDEX_WALK_JOBS'] == 8
    assert defaults['TOFU_TREE_INDEX_MAX_ENTRIES'] == 600_000
    assert defaults['TOFU_TREE_INDEX_MEM_ROOTS'] == 4
    assert defaults['TOFU_PROCESS_RSS_RECYCLE_MB'] == 24 * 1024
    assert defaults['TOFU_TASK_RSS_RESERVE_MB'] == 1024
    assert defaults['TOFU_BROWSER_STAGING_MAX_MIB'] == 2048
    assert defaults['TOFU_BROWSER_POLL_MAX_INFLIGHT'] == 16
    assert defaults['TOFU_BROWSER_POLL_MAX_WAITERS'] == 16
    assert defaults['TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY'] == 128
    assert defaults['TOFU_BROWSER_SESSION_LEASE_CAPACITY'] == 128
    assert defaults['TOFU_BROWSER_POLL_BODY_MAX_MIB'] == 32
    assert defaults['TOFU_MAX_SSE_PER_PRINCIPAL'] == 24
    assert defaults['TOFU_TRANSLATE_MAX_429_ATTEMPTS'] == 8
    assert defaults['TOFU_TRANSLATE_WORKERS'] == 2
    assert defaults['TOFU_TRANSLATE_QUEUE_CAPACITY'] == 32
    assert defaults['TOFU_TRANSLATE_WORKER_IDLE_SECONDS'] == 60
    assert defaults['TOFU_OPTIONAL_LLM_MAX_429_ATTEMPTS'] == 2
    assert defaults['TOFU_PRODUCTION_LLM_FANOUT'] == 2
    assert defaults['TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS'] == 8
    assert defaults['TOFU_PRODUCTION_IMAGE_FANOUT'] == 2
    assert defaults['TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS'] == 8
    assert defaults['TOFU_PRODUCTION_TTS_FANOUT'] == 2
    assert defaults['TOFU_PDF_VLM_TASK_WORKERS'] == 2
    assert defaults['TOFU_PDF_VLM_QUEUE_CAPACITY'] == 8
    assert defaults['TOFU_PDF_VLM_WORKER_IDLE_SECONDS'] == 60
    assert defaults['TOFU_PDF_VLM_CALL_WORKERS'] == 4
    assert defaults['TOFU_PDF_VLM_MAX_PAGES'] == 256
    assert defaults['TOFU_PDF_VLM_TASK_TIMEOUT_SECONDS'] == 7200
    assert defaults['TOFU_PDF_VLM_MAX_429_ATTEMPTS'] == 8
    assert defaults['TOFU_KNOWLEDGE_ENRICH_WORKERS'] == 2
    assert defaults['TOFU_KNOWLEDGE_ENRICH_OWNER_CAPACITY'] == 32
    assert defaults['TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS'] == 60
    assert defaults['TOFU_SWARM_GLOBAL_WORKERS'] == 4
    assert defaults['TOFU_SWARM_MAX_PARALLEL'] == 4
    assert defaults['TOFU_SWARM_MAX_AGENTS_PER_WAVE'] == 8
    assert defaults['TOFU_SWARM_MAX_AGENTS_PER_SESSION'] == 24
    assert defaults['TOFU_SWARM_MAX_RETRIES'] == 1
    assert defaults['TOFU_SWARM_SESSION_CAPACITY'] == 8
    assert defaults['TOFU_MCP_CRED_PROBE_WORKERS'] == 4
    assert defaults['TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB'] == 512 * 1024
    assert defaults['TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS'] == 21_600
    assert defaults['TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB'] == 16_384


def test_personal_resource_probe_uses_the_full_very_large_host_envelope():
    snapshot = _resource_snapshot(
        cpus=64, capacity_mb=256 * 1024, available_mb=220 * 1024,
        disk_total_mb=2 * 1024 * 1024,
        disk_free_mb=1024 * 1024)
    defaults = resource_budget_manifest({}, snapshot=snapshot)['defaults']

    assert defaults['TOFU_PROCESS_RSS_RECYCLE_MB'] == 64 * 1024
    assert defaults['TOFU_TASK_RSS_RESERVE_MB'] == 1024
    assert defaults['TOFU_MAX_INFLIGHT_TASKS'] == 48
    assert defaults['TOFU_AGENT_WORKERS'] == 48
    assert defaults['TOFU_TOOL_RESULT_CACHE_CAPACITY'] == 256
    assert defaults['TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS'] == 1800
    assert defaults['TOOL_MAX_PARALLEL_WORKERS'] == 4
    assert defaults['TOFU_PRODUCTION_LLM_FANOUT'] == 2
    assert defaults['TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS'] == 8
    assert defaults['TOFU_PRODUCTION_IMAGE_FANOUT'] == 2
    assert defaults['TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS'] == 8
    assert defaults['TOFU_PRODUCTION_TTS_FANOUT'] == 2


def test_explicit_cgroup_limit_is_treated_as_the_application_memory_budget():
    snapshot = _resource_snapshot(
        cpus=8, capacity_mb=4096, available_mb=4096,
        host_memory_mb=16 * 1024, cgroup_memory_mb=4096)
    defaults = resource_budget_manifest({}, snapshot=snapshot)['defaults']

    assert defaults['TOFU_PROCESS_RSS_RELIEF_MB'] == 2048
    assert defaults['TOFU_PROCESS_RSS_RECYCLE_MB'] == 2867
    assert defaults['TOFU_MAX_INFLIGHT_TASKS'] == 2
    assert defaults['TOFU_PRODUCTION_LLM_FANOUT'] == 2
    assert defaults['TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS'] == 4
    assert defaults['TOFU_PRODUCTION_IMAGE_FANOUT'] == 2
    assert defaults['TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS'] == 4
    assert defaults['TOFU_PRODUCTION_TTS_FANOUT'] == 2


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
        {}, snapshot=snapshot)['defaults']['TOFU_MAX_INFLIGHT_TASKS'] == 4


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
    assert defaults['TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB'] == 128
    assert defaults['TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY'] == 8
    assert defaults['TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY'] == 512
    assert defaults['TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY'] == 512
    assert defaults['TOFU_TOOL_RESULT_CACHE_CAPACITY'] == 64
    assert defaults['TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS'] == 600
    assert defaults['TOFU_MEMORY_METADATA_CACHE_CAPACITY'] == 512
    assert defaults['TOFU_MEMORY_METADATA_CACHE_MAX_MIB'] == 4
    assert defaults['TOFU_PAPER_QA_SOURCE_CACHE_CAPACITY'] == 1
    assert defaults['TOFU_LOG_TOTAL_BUDGET_MB'] == 128
    assert defaults['TOFU_TRANSLATE_CACHE_MAX_MIB'] == 128
    assert defaults['TOFU_TRANSLATE_MAX_429_ATTEMPTS'] == 4
    assert defaults['TOFU_TRANSLATE_WORKERS'] == 1
    assert defaults['TOFU_TRANSLATE_QUEUE_CAPACITY'] == 4
    assert defaults['TOFU_TRANSLATE_WORKER_IDLE_SECONDS'] == 60
    assert defaults['TOFU_PRODUCTION_LLM_FANOUT'] == 1
    assert defaults['TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS'] == 4
    assert defaults['TOFU_PRODUCTION_IMAGE_FANOUT'] == 1
    assert defaults['TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS'] == 4
    assert defaults['TOFU_PRODUCTION_TTS_FANOUT'] == 1
    assert defaults['TOFU_PDF_VLM_TASK_WORKERS'] == 1
    assert defaults['TOFU_PDF_VLM_QUEUE_CAPACITY'] == 2
    assert defaults['TOFU_PDF_VLM_WORKER_IDLE_SECONDS'] == 60
    assert defaults['TOFU_PDF_VLM_CALL_WORKERS'] == 1
    assert defaults['TOFU_PDF_VLM_MAX_PAGES'] == 64
    assert defaults['TOFU_PDF_VLM_TASK_TIMEOUT_SECONDS'] == 1920
    assert defaults['TOFU_PDF_VLM_MAX_429_ATTEMPTS'] == 4
    assert defaults['TOFU_KNOWLEDGE_ENRICH_WORKERS'] == 1
    assert defaults['TOFU_KNOWLEDGE_ENRICH_OWNER_CAPACITY'] == 4
    assert defaults['TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS'] == 60
    assert defaults['TOFU_SWARM_GLOBAL_WORKERS'] == 1
    assert defaults['TOFU_SWARM_MAX_PARALLEL'] == 1
    assert defaults['TOFU_SWARM_MAX_AGENTS_PER_WAVE'] == 2
    assert defaults['TOFU_SWARM_MAX_AGENTS_PER_SESSION'] == 6
    assert defaults['TOFU_SWARM_MAX_RETRIES'] == 1
    assert defaults['TOFU_SWARM_SESSION_CAPACITY'] == 2
    assert defaults['TOFU_MCP_CRED_PROBE_WORKERS'] == 1
    assert defaults['TOFU_STORAGE_MIN_FREE_BYTES'] == 256 * 1024 * 1024
    assert defaults['TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB'] == 64 * 1024
    assert defaults['TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS'] == 5896
    assert defaults['TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB'] == 512
    assert defaults['TOFU_BROWSER_STAGING_MAX_MIB'] == 256
    assert defaults['TOFU_RAW_ARCHIVE_BUDGET_MIB'] == 256
    assert defaults['TOFU_BROWSER_POLL_MAX_INFLIGHT'] == 8
    assert defaults['TOFU_BROWSER_POLL_MAX_WAITERS'] == 8
    assert defaults['TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY'] == 64
    assert defaults['TOFU_BROWSER_SESSION_LEASE_CAPACITY'] == 64
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
