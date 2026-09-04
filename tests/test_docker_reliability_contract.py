"""Static guardrails for the production Docker lifecycle contract."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def test_compose_has_bounded_self_healing_runtime():
    text = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
    for contract in (
        'restart: unless-stopped',
        'init: true',
        'mem_limit:',
        'mem_reservation:',
        'pids_limit:',
        'stop_grace_period: 45s',
        'http://localhost:15000/health/live',
        'max-size:',
        'max-file:',
        '${TOFU_BACKUP_VOLUME:-tofu-backups}:/app/data/backups',
        'TOFU_SQLITE_SNAPSHOT_DIR=/app/data/backups',
        '127.0.0.1:${PORT:-15000}:15000',
        'TOFU_OPEN_MODE_ALLOW_REMOTE=1',
        'TOFU_AUTH_MODE=${TOFU_AUTH_MODE:-}',
        'TOFU_PUBLISHED_PORT=${PORT:-15000}',
        'mem_limit: ${TOFU_MEMORY_LIMIT:-4g}',
        'mem_reservation: ${TOFU_MEMORY_RESERVATION:-512m}',
        'pids_limit: ${TOFU_PIDS_LIMIT:-512}',
        'TOFU_MAX_INFLIGHT_TASKS=${TOFU_MAX_INFLIGHT_TASKS:-}',
        'MALLOC_ARENA_MAX=${TOFU_MALLOC_ARENA_MAX:-2}',
        'TOFU_STORAGE_RPC_CAPACITY=${TOFU_STORAGE_RPC_CAPACITY:-}',
        'TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB=${TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB:-}',
        'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY=${TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY:-}',
        'TOFU_STORAGE_MIN_FREE_BYTES=${TOFU_STORAGE_MIN_FREE_BYTES:-}',
        'TOFU_LOG_TOTAL_BUDGET_MB=${TOFU_LOG_TOTAL_BUDGET_MB:-}',
        'TOFU_TRANSLATE_CACHE_MAX_MIB=${TOFU_TRANSLATE_CACHE_MAX_MIB:-}',
        'TOFU_TRANSLATE_MAX_429_ATTEMPTS=${TOFU_TRANSLATE_MAX_429_ATTEMPTS:-}',
        'TOFU_TRANSLATE_WORKERS=${TOFU_TRANSLATE_WORKERS:-}',
        'TOFU_TRANSLATE_QUEUE_CAPACITY=${TOFU_TRANSLATE_QUEUE_CAPACITY:-}',
        'TOFU_TRANSLATE_WORKER_IDLE_SECONDS=${TOFU_TRANSLATE_WORKER_IDLE_SECONDS:-}',
        'TOFU_RUN_PYTHON_CACHE=${TOFU_RUN_PYTHON_CACHE:-}',
        'TOFU_RUN_PYTHON_CACHE_MAX_MIB=${TOFU_RUN_PYTHON_CACHE_MAX_MIB:-}',
        'TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY=${TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY:-}',
        'TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY=${TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY:-}',
        'TOFU_TOOL_RESULT_CACHE_CAPACITY=${TOFU_TOOL_RESULT_CACHE_CAPACITY:-}',
        'TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS=${TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS:-}',
        'TOFU_MEMORY_METADATA_CACHE_CAPACITY=${TOFU_MEMORY_METADATA_CACHE_CAPACITY:-}',
        'TOFU_MEMORY_METADATA_CACHE_MAX_MIB=${TOFU_MEMORY_METADATA_CACHE_MAX_MIB:-}',
        'TOFU_PAPER_QA_SOURCE_CACHE_CAPACITY=${TOFU_PAPER_QA_SOURCE_CACHE_CAPACITY:-}',
    ):
        assert contract in text, f'missing Docker reliability contract: {contract}'
    for name in (
        'TOFU_SYNC_WORKERS', 'TOFU_AGENT_WORKERS',
        'TOOL_MAX_PARALLEL_WORKERS', 'TOFU_PRODUCTION_LLM_FANOUT',
        'TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS',
        'TOFU_PRODUCTION_IMAGE_FANOUT',
        'TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS',
        'TOFU_PRODUCTION_TTS_FANOUT',
        'TOFU_NUMERIC_THREADS',
        'TOFU_STORAGE_SQLITE_READ_POOL', 'TOFU_ATTEMPT_EVENT_TTL_DAYS',
        'TOFU_PAPER_REPORT_AGENT_TOKEN_BUDGET',
        'TOFU_PAPER_REPORT_AGENT_DISPATCH_BUDGET',
        'TOFU_PAPER_QA_AGENT_TOKEN_BUDGET',
        'TOFU_PAPER_QA_AGENT_DISPATCH_BUDGET',
        'TOFU_PAPER_DEEPEN_AGENT_TOKEN_BUDGET',
        'TOFU_PAPER_DEEPEN_AGENT_DISPATCH_BUDGET',
        'TOFU_PAPER_INSIGHT_AGENT_TOKEN_BUDGET',
        'TOFU_PAPER_INSIGHT_AGENT_DISPATCH_BUDGET',
        'TOFU_PAPER_RECOMMEND_AGENT_TOKEN_BUDGET',
        'TOFU_PAPER_RECOMMEND_AGENT_DISPATCH_BUDGET',
        'TOFU_RESEARCH_SURVEY_TOKEN_BUDGET',
        'TOFU_RESEARCH_SURVEY_DISPATCH_BUDGET',
        'TOFU_RESEARCH_IDEATE_TOKEN_BUDGET',
        'TOFU_RESEARCH_IDEATE_DISPATCH_BUDGET',
    ):
        assert f'{name}=${{{name}:-}}' in text


def test_compose_open_mode_bridge_exception_stays_host_loopback_only():
    text = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
    assert 'container_name:' not in text, (
        'a global container name breaks Compose project isolation')
    assert '- "${PORT:-15000}:15000"' not in text
    assert '- "0.0.0.0:${PORT:-15000}:15000"' not in text
    assert text.index('127.0.0.1:${PORT:-15000}:15000') \
        < text.index('TOFU_OPEN_MODE_ALLOW_REMOTE=1')
    assert 'git pull --ff-only && docker compose up -d --build' in text
    assert 'docker compose pull &&' not in text


def test_image_healthcheck_uses_liveness_endpoint():
    text = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    assert 'HEALTHCHECK ' in text
    assert 'http://localhost:15000/health/live' in text


def test_empty_optional_provider_environment_never_reads_legacy_provider_rows(
        monkeypatch):
    import lib

    monkeypatch.setattr(lib, '_SAVED_CONFIG', {
        'providers': [{
            'enabled': True,
            'base_url': 'https://saved.example/v1',
        }],
        'presets': {'opus': 'saved-model'},
    })
    monkeypatch.setenv('LLM_BASE_URL', '')
    monkeypatch.setenv('LLM_MODEL', '')

    assert lib._resolve_base_url() == 'https://api.openai.com/v1'
    assert lib._cfg(
        'LLM_MODEL', 'opus', 'fallback', empty_env_is_unset=True,
    ) == 'saved-model'
    # Empty remains an explicit value for settings where it has semantics,
    # such as disabling a fallback model.
    assert lib._cfg('LLM_MODEL', 'opus', 'fallback') == ''
