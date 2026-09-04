"""The backend-parity schema owned exclusively by the sidecar."""

from __future__ import annotations

import re
from types import MappingProxyType

from lib.task_event_contract import STRUCTURAL_EVENT_TYPES, TASK_STREAM_KIND
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Session


SCHEMA_VERSION = 57


_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS storage_meta (
        meta_key TEXT PRIMARY KEY,
        meta_value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_command_receipts (
        command_id TEXT PRIMARY KEY,
        operation TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        response_json BLOB NOT NULL,
        committed_at_ms BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_command_receipts_v2 (
        command_key BLOB PRIMARY KEY,
        operation TEXT NOT NULL,
        request_digest BLOB NOT NULL,
        response_json BLOB NOT NULL,
        committed_at_ms BIGINT NOT NULL,
        CHECK (length(command_key) = 32),
        CHECK (length(request_digest) = 32)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_logical_outbox (
        sequence BIGINT PRIMARY KEY,
        event_id TEXT NOT NULL UNIQUE,
        operation TEXT NOT NULL,
        schema_version BIGINT NOT NULL,
        registry_version BIGINT NOT NULL,
        request_id TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        command_id TEXT NOT NULL DEFAULT '',
        tenant_id TEXT NOT NULL,
        owner_user_id BIGINT NOT NULL,
        encryption_key_id TEXT NOT NULL,
        payload_ciphertext TEXT NOT NULL,
        record_bytes BIGINT NOT NULL,
        committed_at_ms BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_logical_replay_checkpoints (
        target_name TEXT PRIMARY KEY,
        stream_id TEXT NOT NULL,
        last_sequence BIGINT NOT NULL,
        chain_digest TEXT NOT NULL,
        updated_at_ms BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_records (
        namespace TEXT NOT NULL,
        record_key TEXT NOT NULL,
        value_json BLOB NOT NULL,
        version BIGINT NOT NULL,
        updated_at_ms BIGINT NOT NULL,
        PRIMARY KEY (namespace, record_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_events (
        task_id TEXT NOT NULL,
        sequence BIGINT NOT NULL,
        stream_kind TEXT NOT NULL DEFAULT 'task',
        event_type TEXT NOT NULL DEFAULT '',
        event_kind TEXT NOT NULL DEFAULT '',
        owner_user_id BIGINT NOT NULL DEFAULT 0,
        project_key TEXT NOT NULL DEFAULT '',
        project_sequence BIGINT NOT NULL DEFAULT 0,
        event_json BLOB NOT NULL,
        created_at_ms BIGINT NOT NULL,
        PRIMARY KEY (task_id, sequence)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_project_brain_projects (
        owner_user_id BIGINT NOT NULL,
        project_key TEXT NOT NULL,
        head_sequence BIGINT NOT NULL,
        checkpoint_sequence BIGINT NOT NULL DEFAULT 0,
        projection_json JSONDOC NOT NULL,
        updated_at_ms BIGINT NOT NULL,
        PRIMARY KEY (owner_user_id, project_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_project_brain_updated
    ON storage_project_brain_projects(owner_user_id, updated_at_ms, project_key)
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_rate_limit_events (
        event_id TEXT PRIMARY KEY,
        endpoint TEXT NOT NULL,
        client_key TEXT NOT NULL,
        occurred_at_ms BIGINT NOT NULL,
        expires_at_ms BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_autopilot_markers (
        conv_id TEXT PRIMARY KEY,
        user_id BIGINT NOT NULL,
        queue_id TEXT NOT NULL,
        config_json JSONDOC NOT NULL DEFAULT '{}',
        created_at_ms BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_queue_items (
        id TEXT PRIMARY KEY,
        user_id BIGINT NOT NULL,
        conv_id TEXT NOT NULL,
        payload_json JSONDOC NOT NULL DEFAULT '{}',
        config_json JSONDOC NOT NULL DEFAULT '{}',
        position BIGINT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'real',
        priority BIGINT NOT NULL DEFAULT 100,
        created_at_ms BIGINT NOT NULL,
        leased_until_ms BIGINT,
        lease_task_id TEXT NOT NULL DEFAULT '',
        input_turn_id TEXT NOT NULL DEFAULT '',
        output_turn_id TEXT NOT NULL DEFAULT '',
        attempt_id TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_queue_order
    ON storage_queue_items(user_id, conv_id, priority, position)
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_worker_jobs (
        task_id TEXT PRIMARY KEY,
        user_id BIGINT NOT NULL,
        tenant_id TEXT NOT NULL DEFAULT '',
        task_kind TEXT NOT NULL,
        payload_json JSONDOC NOT NULL DEFAULT '{}',
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        priority BIGINT NOT NULL DEFAULT 100,
        available_at_ms BIGINT NOT NULL,
        claim_owner TEXT NOT NULL DEFAULT '',
        lease_deadline_ms BIGINT NOT NULL DEFAULT 0,
        fencing_token BIGINT NOT NULL DEFAULT 0,
        attempt_no BIGINT NOT NULL DEFAULT 0,
        heartbeat_at_ms BIGINT NOT NULL DEFAULT 0,
        cancel_sequence BIGINT NOT NULL DEFAULT 0,
        cancel_requested_at_ms BIGINT NOT NULL DEFAULT 0,
        cancel_reason TEXT NOT NULL DEFAULT '',
        replay_cursor BIGINT NOT NULL DEFAULT 0,
        result_ref TEXT NOT NULL DEFAULT '',
        error_json JSONDOC NOT NULL DEFAULT '{}',
        created_at_ms BIGINT NOT NULL,
        updated_at_ms BIGINT NOT NULL,
        terminal_at_ms BIGINT NOT NULL DEFAULT 0,
        UNIQUE (user_id, idempotency_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_worker_jobs_queued
    ON storage_worker_jobs(status, available_at_ms, priority,
                           created_at_ms, task_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_worker_jobs_lease
    ON storage_worker_jobs(status, lease_deadline_ms, priority,
                           created_at_ms, task_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_conversations (
        id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        title TEXT NOT NULL DEFAULT 'New Chat',
        messages_json JSONDOC NOT NULL DEFAULT '[]',
        created_at_ms BIGINT NOT NULL DEFAULT 0,
        updated_at_ms BIGINT NOT NULL DEFAULT 0,
        settings_json JSONDOC NOT NULL DEFAULT '{}',
        msg_count BIGINT NOT NULL DEFAULT 0,
        search_text TEXT NOT NULL DEFAULT '',
        rev BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (id, user_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_conversations_updated
    ON storage_conversations(user_id, updated_at_ms, id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_storage_conversations_global_id
    ON storage_conversations(id)
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_conversation_trash (
        conversation_id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        title TEXT NOT NULL DEFAULT 'New Chat',
        messages_json JSONDOC NOT NULL DEFAULT '[]',
        created_at_ms BIGINT NOT NULL DEFAULT 0,
        updated_at_ms BIGINT NOT NULL DEFAULT 0,
        settings_json JSONDOC NOT NULL DEFAULT '{}',
        msg_count BIGINT NOT NULL DEFAULT 0,
        rev BIGINT NOT NULL DEFAULT 0,
        deleted_at_ms BIGINT NOT NULL,
        PRIMARY KEY (conversation_id, user_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_conversation_trash_retention
    ON storage_conversation_trash(deleted_at_ms, conversation_id, user_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_conversation_trash_turns (
        conversation_id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        turn_id TEXT NOT NULL,
        lane_id TEXT NOT NULL DEFAULT 'main',
        parent_turn_id TEXT,
        ordinal BIGINT NOT NULL,
        actor TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'reply',
        run_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        projection_json JSONDOC NOT NULL DEFAULT '{}',
        projection_revision BIGINT NOT NULL DEFAULT 0,
        settlement_json JSONDOC NOT NULL DEFAULT '{}',
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        PRIMARY KEY (conversation_id, user_id, turn_id),
        UNIQUE (conversation_id, user_id, lane_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_compaction_archives (
        archive_id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        messages_json JSONDOC NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        receipt_json JSONDOC NOT NULL DEFAULT '{}',
        trigger TEXT NOT NULL DEFAULT 'force',
        task_id TEXT NOT NULL DEFAULT '',
        round_num BIGINT NOT NULL DEFAULT 0,
        model TEXT NOT NULL DEFAULT '',
        tokens_before BIGINT NOT NULL DEFAULT 0,
        tokens_after BIGINT NOT NULL DEFAULT 0,
        msgs_before BIGINT NOT NULL DEFAULT 0,
        msgs_after BIGINT NOT NULL DEFAULT 0,
        reason TEXT NOT NULL DEFAULT '',
        payload_size BIGINT NOT NULL DEFAULT 0,
        created_at_ms BIGINT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_compaction_archives_owner
    ON storage_compaction_archives(user_id, conversation_id, created_at_ms, archive_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_timers (
        id TEXT PRIMARY KEY,
        user_id BIGINT NOT NULL,
        conv_id TEXT NOT NULL,
        source_task_id TEXT NOT NULL DEFAULT '',
        check_instruction TEXT NOT NULL,
        check_command TEXT NOT NULL DEFAULT '',
        continuation_message TEXT NOT NULL,
        poll_interval BIGINT NOT NULL DEFAULT 60,
        max_polls BIGINT NOT NULL DEFAULT 120,
        poll_count BIGINT NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        tools_config JSONDOC NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '',
        triggered_at TEXT NOT NULL DEFAULT '',
        cancelled_at TEXT NOT NULL DEFAULT '',
        execution_task_id TEXT NOT NULL DEFAULT '',
        last_poll_at TEXT NOT NULL DEFAULT '',
        last_poll_decision TEXT NOT NULL DEFAULT '',
        last_poll_reason TEXT NOT NULL DEFAULT '',
        condition_kind TEXT NOT NULL DEFAULT 'llm',
        condition_command TEXT NOT NULL DEFAULT '',
        condition_regex TEXT NOT NULL DEFAULT '',
        promotion_streak BIGINT NOT NULL DEFAULT 0,
        fallback_streak BIGINT NOT NULL DEFAULT 0,
        promoted_at TEXT NOT NULL DEFAULT '',
        origin TEXT NOT NULL DEFAULT 'background'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_timer_poll_log (
        id BIGINT PRIMARY KEY,
        timer_id TEXT NOT NULL,
        poll_time TEXT NOT NULL,
        decision TEXT NOT NULL DEFAULT 'wait',
        reason TEXT NOT NULL DEFAULT '',
        check_output TEXT NOT NULL DEFAULT '',
        tokens_used BIGINT NOT NULL DEFAULT 0,
        model TEXT NOT NULL DEFAULT '',
        poll_id TEXT NOT NULL DEFAULT '',
        raw_output TEXT NOT NULL DEFAULT '',
        tier TEXT NOT NULL DEFAULT 'llm',
        predicate_matched BIGINT NOT NULL DEFAULT -1,
        llm_agreed BIGINT NOT NULL DEFAULT -1
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_timer_poll_log
    ON storage_timer_poll_log(timer_id, poll_time)
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_scheduled_tasks (
        id TEXT PRIMARY KEY, user_id BIGINT NOT NULL,
        system_key TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL, schedule TEXT NOT NULL,
        task_type TEXT NOT NULL DEFAULT 'command', command TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '', enabled BIGINT NOT NULL DEFAULT 1,
        notify_on_failure BIGINT NOT NULL DEFAULT 1,
        notify_on_success BIGINT NOT NULL DEFAULT 0,
        max_runtime BIGINT NOT NULL DEFAULT 300, last_run TEXT NOT NULL DEFAULT '',
        last_result TEXT NOT NULL DEFAULT '', last_status TEXT NOT NULL DEFAULT 'never',
        run_count BIGINT NOT NULL DEFAULT 0, fail_count BIGINT NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
        target_conv_id TEXT NOT NULL DEFAULT '', source_conv_id TEXT NOT NULL DEFAULT '',
        tools_config JSONDOC NOT NULL DEFAULT '{}', poll_count BIGINT NOT NULL DEFAULT 0,
        last_poll_at TEXT NOT NULL DEFAULT '', last_poll_decision TEXT NOT NULL DEFAULT '',
        last_poll_reason TEXT NOT NULL DEFAULT '', last_execution_at TEXT NOT NULL DEFAULT '',
        last_execution_task_id TEXT NOT NULL DEFAULT '', last_execution_status TEXT NOT NULL DEFAULT '',
        execution_count BIGINT NOT NULL DEFAULT 0, max_executions BIGINT NOT NULL DEFAULT 0,
        expires_at TEXT NOT NULL DEFAULT '', condition_kind TEXT NOT NULL DEFAULT 'llm',
        condition_command TEXT NOT NULL DEFAULT '', condition_regex TEXT NOT NULL DEFAULT '',
        promotion_streak BIGINT NOT NULL DEFAULT 0, fallback_streak BIGINT NOT NULL DEFAULT 0,
        promoted_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_proactive_poll_log (
        id BIGINT PRIMARY KEY, task_id TEXT NOT NULL, poll_time TEXT NOT NULL,
        decision TEXT NOT NULL DEFAULT 'skip', reason TEXT NOT NULL DEFAULT '',
        status_snapshot TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
        tokens_used BIGINT NOT NULL DEFAULT 0, execution_task_id TEXT NOT NULL DEFAULT '',
        tier TEXT NOT NULL DEFAULT 'llm', predicate_matched BIGINT NOT NULL DEFAULT -1,
        llm_agreed BIGINT NOT NULL DEFAULT -1
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_proactive_poll_log
    ON storage_proactive_poll_log(task_id, poll_time)
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_conversation_turns (
        turn_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, user_id BIGINT NOT NULL,
        presentation_id TEXT NOT NULL DEFAULT '',
        lane_id TEXT NOT NULL DEFAULT 'main', parent_turn_id TEXT,
        ordinal BIGINT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'reply',
        run_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
        current_attempt_id TEXT, projection_json JSONDOC NOT NULL DEFAULT '{}',
        projection_revision BIGINT NOT NULL DEFAULT 0,
        projection_checkpoint_revision BIGINT,
        projection_materialized_revision BIGINT,
        projection_patch_count BIGINT NOT NULL DEFAULT 0,
        projection_patch_bytes BIGINT NOT NULL DEFAULT 0,
        settlement_json JSONDOC NOT NULL DEFAULT '{}',
        created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL,
        UNIQUE(conversation_id, lane_id, ordinal)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_conversation_turns_order
    ON storage_conversation_turns(conversation_id, lane_id, ordinal)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_conversation_turns_owner_order
    ON storage_conversation_turns(conversation_id, user_id, lane_id, ordinal)
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_turn_projection_checkpoints (
        turn_id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        attempt_id TEXT NOT NULL,
        projection_revision BIGINT NOT NULL,
        projection_json JSONDOC NOT NULL,
        projection_bytes BIGINT NOT NULL,
        updated_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_turn_search (
        conversation_id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        turn_id TEXT NOT NULL,
        lane_id TEXT NOT NULL DEFAULT 'main',
        ordinal BIGINT NOT NULL,
        search_text TEXT NOT NULL DEFAULT '',
        projection_revision BIGINT NOT NULL DEFAULT 0,
        updated_at BIGINT NOT NULL,
        PRIMARY KEY (conversation_id, turn_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_turn_search_owner_order
    ON storage_turn_search(user_id, conversation_id, lane_id, ordinal)
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_projection_outbox (
        projection_name TEXT NOT NULL,
        entity_kind TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        entity_key TEXT NOT NULL,
        version_token TEXT NOT NULL,
        enqueued_at_ms BIGINT NOT NULL,
        PRIMARY KEY (projection_name, entity_kind, user_id, entity_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_projection_outbox_due
    ON storage_projection_outbox(
        projection_name, enqueued_at_ms, entity_kind, user_id, entity_key)
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_search_conversations (
        id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        updated_at_ms BIGINT NOT NULL,
        generation_token TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (user_id, id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_search_conversations_order
    ON storage_search_conversations(user_id, updated_at_ms, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_search_turns (
        conversation_id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        turn_id TEXT NOT NULL,
        lane_id TEXT NOT NULL,
        ordinal BIGINT NOT NULL,
        search_text TEXT NOT NULL,
        projection_revision BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        generation_token TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (user_id, conversation_id, turn_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_search_turns_owner_order
    ON storage_search_turns(user_id, conversation_id, lane_id, ordinal)
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_turn_tombstones (
        conversation_id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        turn_id TEXT NOT NULL,
        deleted_at BIGINT NOT NULL,
        PRIMARY KEY (conversation_id, turn_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_turn_tombstones_delta
    ON storage_turn_tombstones(conversation_id, user_id, deleted_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_generation_attempts (
        attempt_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL,
        command_id TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '', operation TEXT NOT NULL,
        dispatch_mode TEXT NOT NULL DEFAULT '',
        queue_id TEXT NOT NULL DEFAULT '',
        queue_state TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending', base_projection_revision BIGINT NOT NULL,
        resume_anchor_json JSONDOC NOT NULL DEFAULT '{}', config_json JSONDOC NOT NULL DEFAULT '{}',
        error_json JSONDOC NOT NULL DEFAULT '{}',
        timing_trace_json JSONDOC NOT NULL DEFAULT '{}', created_at BIGINT NOT NULL,
        started_at BIGINT, settled_at BIGINT, superseded_at BIGINT,
        UNIQUE(conversation_id, command_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_generation_attempts_turn
    ON storage_generation_attempts(turn_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_generation_attempts_task
    ON storage_generation_attempts(task_id) WHERE task_id <> ''
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_generation_attempts_conversation_created
    ON storage_generation_attempts(
        conversation_id, created_at DESC, attempt_id DESC
    ) WHERE task_id <> ''
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_raw_archives (
        archive_id TEXT PRIMARY KEY,
        user_id BIGINT NOT NULL,
        conversation_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        round_num BIGINT NOT NULL,
        transport_attempt BIGINT NOT NULL DEFAULT 0,
        request_blob BLOB NOT NULL,
        response_blob BLOB NOT NULL,
        request_bytes BIGINT NOT NULL,
        response_bytes BIGINT NOT NULL,
        stored_bytes BIGINT NOT NULL,
        request_sha256 TEXT NOT NULL,
        response_sha256 TEXT NOT NULL,
        integrity TEXT NOT NULL,
        truncation_reason TEXT NOT NULL DEFAULT '',
        summary_json JSONDOC NOT NULL DEFAULT '{}',
        created_at_ms BIGINT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_raw_archives_task_round
    ON storage_raw_archives(user_id, task_id, round_num, created_at_ms, archive_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_raw_archives_attempt
    ON storage_raw_archives(user_id, attempt_id, created_at_ms, archive_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_attempt_events (
        attempt_id TEXT NOT NULL, sequence BIGINT NOT NULL, conversation_id TEXT NOT NULL,
        turn_id TEXT NOT NULL, projection_revision BIGINT NOT NULL, type TEXT NOT NULL,
        payload_json JSONDOC NOT NULL DEFAULT '{}', payload_bytes BIGINT NOT NULL DEFAULT 0,
        created_at BIGINT NOT NULL,
        PRIMARY KEY(attempt_id, sequence)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_conversation_sync_heads (
        conversation_id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        sync_sequence BIGINT NOT NULL DEFAULT 0,
        updated_at BIGINT NOT NULL,
        PRIMARY KEY(conversation_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_conversation_changes (
        conversation_id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        sync_sequence BIGINT NOT NULL,
        change_type TEXT NOT NULL,
        turn_id TEXT NOT NULL DEFAULT '',
        attempt_id TEXT NOT NULL DEFAULT '',
        attempt_sequence BIGINT,
        event_json JSONDOC NOT NULL,
        created_at BIGINT NOT NULL,
        PRIMARY KEY(conversation_id, user_id, sync_sequence)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_conversation_changes_retention
    ON storage_conversation_changes(created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS orchestration_runs (
        id TEXT PRIMARY KEY,
        user_id BIGINT NOT NULL,
        tenant_id TEXT NOT NULL DEFAULT '',
        orch_id TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL DEFAULT '',
        definition TEXT NOT NULL DEFAULT '{}',
        input TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        final TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL DEFAULT 0,
        updated_at BIGINT NOT NULL DEFAULT 0,
        finished_at BIGINT NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orchestration_run_events (
        run_id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        tenant_id TEXT NOT NULL DEFAULT '',
        seq BIGINT NOT NULL,
        type TEXT NOT NULL DEFAULT '',
        node_id TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL DEFAULT '{}',
        ts BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, user_id, tenant_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orchestration_definitions (
        id TEXT PRIMARY KEY,
        user_id BIGINT NOT NULL,
        tenant_id TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL DEFAULT '',
        definition_json JSONDOC NOT NULL,
        created_at_ms BIGINT NOT NULL,
        updated_at_ms BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS swarm_sessions (
        swarm_key TEXT PRIMARY KEY,
        conv_id TEXT NOT NULL DEFAULT '',
        task_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'running',
        specs_json TEXT NOT NULL DEFAULT '[]',
        config_json TEXT NOT NULL DEFAULT '{}',
        created_at BIGINT NOT NULL DEFAULT 0,
        updated_at BIGINT NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS swarm_agents (
        swarm_key TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT '',
        objective TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        messages_json TEXT NOT NULL DEFAULT '[]',
        result_json TEXT NOT NULL DEFAULT '{}',
        rounds_used BIGINT NOT NULL DEFAULT 0,
        delivered BIGINT NOT NULL DEFAULT 0,
        updated_at BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (swarm_key, agent_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_reports (
        user_id BIGINT NOT NULL,
        paper_hash TEXT NOT NULL,
        lang TEXT NOT NULL DEFAULT 'en',
        report TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        meta TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL,
        PRIMARY KEY (user_id, paper_hash, lang)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_translations (
        user_id BIGINT NOT NULL,
        paper_hash TEXT NOT NULL,
        lang TEXT NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL,
        PRIMARY KEY (user_id, paper_hash, lang)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_library (
        id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        pdf_url TEXT NOT NULL DEFAULT '',
        pdf_filename TEXT NOT NULL DEFAULT '',
        arxiv_id TEXT NOT NULL DEFAULT '',
        paper_hash TEXT NOT NULL DEFAULT '',
        parsed_text TEXT NOT NULL DEFAULT '',
        parser_version TEXT NOT NULL DEFAULT '',
        qa_history TEXT NOT NULL DEFAULT '[]',
        images TEXT NOT NULL DEFAULT '[]',
        babel_cache TEXT NOT NULL DEFAULT '{}',
        page_count BIGINT NOT NULL DEFAULT 0,
        folder_id TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        PRIMARY KEY (id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_notes (
        user_id BIGINT NOT NULL,
        id TEXT NOT NULL,
        paper_hash TEXT NOT NULL DEFAULT '',
        lang TEXT NOT NULL DEFAULT '',
        anchor TEXT NOT NULL DEFAULT '{}',
        note TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        PRIMARY KEY (user_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_cost_cache (
        user_id BIGINT NOT NULL,
        date TEXT NOT NULL,
        cost DOUBLE PRECISION NOT NULL DEFAULT 0,
        conversations_json JSONDOC NOT NULL DEFAULT '{}',
        computed_at BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_podcasts (
        user_id BIGINT NOT NULL,
        paper_hash TEXT NOT NULL,
        mode TEXT NOT NULL,
        lang TEXT NOT NULL,
        voice TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'generating',
        script_json TEXT NOT NULL DEFAULT '',
        file_path TEXT NOT NULL DEFAULT '',
        duration_sec DOUBLE PRECISION NOT NULL DEFAULT 0,
        model TEXT NOT NULL DEFAULT '',
        tts_model TEXT NOT NULL DEFAULT '',
        meta TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL,
        PRIMARY KEY (user_id, paper_hash, mode, lang, voice)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tenant_users (
        id TEXT PRIMARY KEY,
        owner_user_id BIGINT NOT NULL UNIQUE,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL DEFAULT '',
        display_name TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT 'user',
        status TEXT NOT NULL DEFAULT 'active',
        created_at BIGINT NOT NULL,
        last_login_at BIGINT NOT NULL DEFAULT 0,
        email_verified BIGINT NOT NULL DEFAULT 0,
        metadata TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_identity_sequences (
        sequence_name TEXT PRIMARY KEY,
        next_value BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_credentials (
        id TEXT PRIMARY KEY,
        owner_user_id BIGINT NOT NULL,
        account_user_id TEXT NOT NULL DEFAULT '',
        tenant_id TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL,
        prefix TEXT NOT NULL,
        secret_hash TEXT NOT NULL UNIQUE,
        scopes TEXT NOT NULL,
        rate_limit_rpm BIGINT NOT NULL DEFAULT 0,
        rate_limit_tpd BIGINT NOT NULL DEFAULT 0,
        created_at DOUBLE PRECISION NOT NULL,
        last_used_at DOUBLE PRECISION,
        expires_at DOUBLE PRECISION,
        disabled BIGINT NOT NULL DEFAULT 0,
        revoked_at DOUBLE PRECISION,
        metadata TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS byo_providers (
        id TEXT PRIMARY KEY,
        owner_user_id BIGINT NOT NULL,
        tenant_id TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL,
        base_url TEXT NOT NULL,
        api_key_ciphertext TEXT NOT NULL DEFAULT '',
        key_hint TEXT NOT NULL DEFAULT '',
        models_json JSONDOC NOT NULL DEFAULT '[]',
        extra_headers_json JSONDOC NOT NULL DEFAULT '{}',
        thinking_format TEXT NOT NULL DEFAULT '',
        disabled BIGINT NOT NULL DEFAULT 0,
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        last_used_at DOUBLE PRECISION
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_model_routing_authorities (
        owner_user_id BIGINT NOT NULL,
        tenant_id TEXT NOT NULL DEFAULT '',
        revision BIGINT NOT NULL,
        document_json JSONDOC NOT NULL,
        backup_json JSONDOC,
        migration_receipt_json JSONDOC,
        updated_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (owner_user_id, tenant_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_model_routing_secrets (
        owner_user_id BIGINT NOT NULL,
        tenant_id TEXT NOT NULL DEFAULT '',
        secret_reference TEXT NOT NULL,
        ciphertext TEXT NOT NULL,
        key_hint TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (owner_user_id, tenant_id, secret_reference)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_artifacts (
        id TEXT PRIMARY KEY,
        conv_id TEXT NOT NULL,
        task_id TEXT NOT NULL DEFAULT '',
        msg_id TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL,
        source_ref JSONDOC NOT NULL DEFAULT '{}',
        format TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        size_bytes BIGINT NOT NULL DEFAULT 0,
        version BIGINT NOT NULL DEFAULT 1,
        parent_id TEXT NOT NULL DEFAULT '',
        pinned BOOLEAN NOT NULL DEFAULT FALSE,
        meta JSONDOC NOT NULL DEFAULT '{}',
        created_at BIGINT NOT NULL,
        deleted_at BIGINT NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tool_result_artifacts (
        user_id BIGINT NOT NULL,
        content_sha256 TEXT NOT NULL,
        content TEXT NOT NULL,
        media_type TEXT NOT NULL DEFAULT 'text/plain',
        size_bytes BIGINT NOT NULL DEFAULT 0,
        created_at_ms BIGINT NOT NULL,
        expires_at_ms BIGINT NOT NULL,
        last_accessed_at_ms BIGINT NOT NULL,
        PRIMARY KEY (user_id, content_sha256)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_knowledge_settings (
        user_id BIGINT PRIMARY KEY,
        enabled BIGINT NOT NULL DEFAULT 0,
        visual_enrichment BIGINT NOT NULL DEFAULT 0,
        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_desktop_egress_preferences (
        owner_user_id BIGINT PRIMARY KEY,
        agent_id TEXT NOT NULL DEFAULT '',
        updated_at_ms BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_knowledge_documents (
        user_id BIGINT NOT NULL,
        id TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        name TEXT NOT NULL,
        stored_name TEXT NOT NULL,
        kind TEXT NOT NULL,
        size_bytes BIGINT NOT NULL,
        method TEXT NOT NULL,
        warnings_json TEXT NOT NULL DEFAULT '[]',
        text_chars BIGINT NOT NULL DEFAULT 0,
        chunk_count BIGINT NOT NULL DEFAULT 0,
        pages BIGINT NOT NULL DEFAULT 0,
        scope TEXT NOT NULL DEFAULT 'library',
        media_metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (user_id, id),
        UNIQUE (user_id, sha256),
        UNIQUE (user_id, stored_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_knowledge_chunks (
        user_id BIGINT NOT NULL,
        document_id TEXT NOT NULL,
        ordinal BIGINT NOT NULL,
        section TEXT NOT NULL DEFAULT '',
        location TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL,
        search_text TEXT NOT NULL,
        PRIMARY KEY (user_id, document_id, ordinal),
        FOREIGN KEY (user_id, document_id)
            REFERENCES storage_knowledge_documents(user_id, id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_knowledge_assets (
        user_id BIGINT NOT NULL,
        id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        ordinal BIGINT NOT NULL,
        kind TEXT NOT NULL,
        stored_name TEXT NOT NULL,
        mime_type TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size_bytes BIGINT NOT NULL,
        width BIGINT NOT NULL DEFAULT 0,
        height BIGINT NOT NULL DEFAULT 0,
        page BIGINT NOT NULL DEFAULT 0,
        pages_json TEXT NOT NULL DEFAULT '[]',
        bbox_json TEXT NOT NULL DEFAULT '[]',
        caption TEXT NOT NULL DEFAULT '',
        ocr_text TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        enrichment_status TEXT NOT NULL DEFAULT 'not_requested',
        enrichment_model TEXT NOT NULL DEFAULT '',
        enrichment_error TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (user_id, id),
        UNIQUE (user_id, document_id, ordinal),
        UNIQUE (user_id, stored_name),
        FOREIGN KEY (user_id, document_id)
            REFERENCES storage_knowledge_documents(user_id, id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_knowledge_chunk_assets (
        user_id BIGINT NOT NULL,
        document_id TEXT NOT NULL,
        chunk_ordinal BIGINT NOT NULL,
        asset_id TEXT NOT NULL,
        relation TEXT NOT NULL DEFAULT 'evidence',
        ordinal BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (
            user_id, document_id, chunk_ordinal, asset_id, relation
        ),
        FOREIGN KEY (user_id, document_id, chunk_ordinal)
            REFERENCES storage_knowledge_chunks(user_id, document_id, ordinal)
            ON DELETE CASCADE,
        FOREIGN KEY (user_id, asset_id)
            REFERENCES storage_knowledge_assets(user_id, id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_knowledge_terms (
        user_id BIGINT NOT NULL,
        term TEXT NOT NULL,
        document_id TEXT NOT NULL,
        chunk_ordinal BIGINT NOT NULL,
        PRIMARY KEY (user_id, term, document_id, chunk_ordinal),
        FOREIGN KEY (user_id, document_id, chunk_ordinal)
            REFERENCES storage_knowledge_chunks(user_id, document_id, ordinal)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_knowledge_documents_updated
    ON storage_knowledge_documents(user_id, updated_at DESC, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_knowledge_documents_created
    ON storage_knowledge_documents(user_id, created_at DESC, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_knowledge_chunks_document
    ON storage_knowledge_chunks(user_id, document_id, ordinal)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_knowledge_assets_document
    ON storage_knowledge_assets(user_id, document_id, ordinal)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_knowledge_assets_enrichment
    ON storage_knowledge_assets(user_id, enrichment_status, updated_at, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_storage_knowledge_terms_chunk
    ON storage_knowledge_terms(user_id, document_id, chunk_ordinal)
    """,
    """
    CREATE TABLE IF NOT EXISTS optimizer_proposals (
        user_id BIGINT NOT NULL,
        id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        title TEXT NOT NULL,
        rationale TEXT NOT NULL,
        action_type TEXT NOT NULL,
        action_args TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'low',
        confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
        evidence TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending_review',
        status_reason TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (user_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS optimizer_action_log (
        user_id BIGINT NOT NULL,
        id TEXT NOT NULL,
        proposal_id TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        expires_at TEXT NOT NULL DEFAULT '',
        pre_metric TEXT NOT NULL DEFAULT '',
        outcome_metric TEXT NOT NULL DEFAULT '',
        outcome_recorded_at TEXT NOT NULL DEFAULT '',
        reverted_at TEXT NOT NULL DEFAULT '',
        revert_reason TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (user_id, id),
        FOREIGN KEY (user_id, proposal_id)
            REFERENCES optimizer_proposals(user_id, id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS log_aggregates (
        fingerprint TEXT PRIMARY KEY,
        level TEXT NOT NULL,
        logger TEXT NOT NULL DEFAULT '',
        template TEXT NOT NULL DEFAULT '',
        sample TEXT NOT NULL DEFAULT '',
        count BIGINT NOT NULL DEFAULT 0,
        first_seen BIGINT NOT NULL DEFAULT 0,
        last_seen BIGINT NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_plugin_manifests (
        namespace TEXT PRIMARY KEY,
        manifest_version BIGINT NOT NULL,
        manifest_json BLOB NOT NULL,
        updated_at_ms BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_plugin_rows (
        namespace TEXT NOT NULL,
        table_name TEXT NOT NULL,
        row_key TEXT NOT NULL,
        document_json BLOB NOT NULL,
        version BIGINT NOT NULL,
        updated_at_ms BIGINT NOT NULL,
        PRIMARY KEY (namespace, table_name, row_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_plugin_unique_values (
        namespace TEXT NOT NULL,
        table_name TEXT NOT NULL,
        index_name TEXT NOT NULL,
        index_value TEXT NOT NULL,
        row_key TEXT NOT NULL,
        PRIMARY KEY (namespace, table_name, index_name, index_value)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS billing_ledger (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        ts BIGINT NOT NULL,
        amount_micro BIGINT NOT NULL,
        kind TEXT NOT NULL,
        ref_type TEXT NOT NULL DEFAULT '',
        ref_id TEXT NOT NULL DEFAULT '',
        balance_after_micro BIGINT NOT NULL,
        note TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS billing_wallets (
        user_id TEXT PRIMARY KEY,
        balance_micro BIGINT NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'CREDIT',
        low_balance_alert_micro BIGINT NOT NULL DEFAULT 0,
        updated_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS billing_redeem_codes (
        code TEXT PRIMARY KEY,
        amount_micro BIGINT NOT NULL,
        batch TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL,
        expires_at BIGINT NOT NULL DEFAULT 0,
        redeemed_by TEXT NOT NULL DEFAULT '',
        redeemed_at BIGINT NOT NULL DEFAULT 0,
        note TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS billing_payments (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        provider_id TEXT NOT NULL DEFAULT '',
        amount_minor BIGINT NOT NULL,
        currency TEXT NOT NULL DEFAULT 'USD',
        credit_micro BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at BIGINT NOT NULL,
        settled_at BIGINT NOT NULL DEFAULT 0,
        raw TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS integration_workspaces (
        id BIGINT PRIMARY KEY,
        user_id BIGINT NOT NULL,
        project_root TEXT NOT NULL,
        task_id TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        workspace_path TEXT NOT NULL,
        managed BIGINT NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'running',
        base_sha TEXT NOT NULL DEFAULT '',
        checkpoint_sha TEXT NOT NULL DEFAULT '',
        candidate_sha TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        UNIQUE(user_id, project_root, task_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS integration_events (
        id BIGINT PRIMARY KEY,
        user_id BIGINT NOT NULL,
        project_root TEXT NOT NULL,
        task_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS integration_workspace_meta (
        user_id BIGINT NOT NULL,
        project_root TEXT NOT NULL,
        task_id TEXT NOT NULL,
        origin_json TEXT NOT NULL DEFAULT '{}',
        updated_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (user_id, project_root, task_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS storage_plugin_rows_table_idx
    ON storage_plugin_rows(namespace, table_name, updated_at_ms)
    """,
    """
    CREATE INDEX IF NOT EXISTS storage_events_task_idx
    ON storage_events(task_id, sequence)
    """,
    """
    CREATE INDEX IF NOT EXISTS storage_rate_limit_bucket_idx
    ON storage_rate_limit_events(endpoint, client_key, occurred_at_ms)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ledger_user_ts
    ON billing_ledger(user_id, ts DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ledger_ref
    ON billing_ledger(ref_type, ref_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ledger_kind
    ON billing_ledger(kind)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ledger_reserve_sweep
    ON billing_ledger(user_id, ref_id)
    WHERE ref_type = 'reserve'
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_billing_ledger_idempotency
    ON billing_ledger(user_id, kind, ref_type, ref_id)
    WHERE ref_type <> '' AND ref_id <> ''
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_payments_user
    ON billing_payments(user_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_payments_status
    ON billing_payments(status)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_billing_payments_provider_id
    ON billing_payments(provider, provider_id)
    WHERE provider_id <> ''
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_integration_ready
    ON integration_workspaces(state, updated_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_integration_events_project
    ON integration_events(project_root, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_paper_lib_user
    ON paper_library(user_id, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_paper_lib_hash
    ON paper_library(paper_hash, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_paper_lib_arxiv
    ON paper_library(user_id, arxiv_id, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_chat_artifact_conv
    ON chat_artifacts(conv_id, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_chat_artifact_msg
    ON chat_artifacts(conv_id, msg_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_chat_artifact_sha
    ON chat_artifacts(conv_id, content_sha256)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_chat_artifact_task
    ON chat_artifacts(task_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tool_result_artifact_expiry
    ON tool_result_artifacts(expires_at_ms, user_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tenant_users_email
    ON tenant_users(email)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tenant_users_role
    ON tenant_users(role)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_tenant_users_status_created
    ON tenant_users(status, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_auth_credentials_owner_created
    ON auth_credentials(tenant_id, owner_user_id, created_at DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_auth_credentials_account
    ON auth_credentials(account_user_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_byo_providers_owner_created
    ON byo_providers(tenant_id, owner_user_id, created_at DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_model_routing_secrets_owner_updated
    ON storage_model_routing_secrets(
        tenant_id, owner_user_id, updated_at, secret_reference)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_swarm_sessions_status
    ON swarm_sessions(status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_swarm_agents_key
    ON swarm_agents(swarm_key)
    """,
)


_MIGRATIONS = {
    # Ownership was previously implicit. Existing personal-installation rows
    # have exactly one historical owner; new writes require it explicitly.
    16: (
        "ALTER TABLE storage_timers ADD COLUMN user_id BIGINT NOT NULL DEFAULT 1",
        "ALTER TABLE storage_scheduled_tasks ADD COLUMN user_id BIGINT NOT NULL DEFAULT 1",
    ),
    # Blocking inline watchers cannot survive their parent executor. Retire
    # historical live rows once; all new timers are durable background jobs.
    17: (
        "UPDATE storage_timers SET status='orphaned' "
        "WHERE status='active' AND origin='inline'",
    ),
    # Queue ownership is part of every key and operation. Historical rows
    # belong to the sole personal-installation principal; all new writes must
    # provide their owner explicitly.
    18: (
        "ALTER TABLE storage_queue_items "
        "ADD COLUMN user_id BIGINT NOT NULL DEFAULT 1",
        "ALTER TABLE storage_autopilot_markers "
        "ADD COLUMN user_id BIGINT NOT NULL DEFAULT 1",
        "DROP INDEX IF EXISTS idx_storage_queue_order",
        "CREATE INDEX idx_storage_queue_order ON storage_queue_items"
        "(user_id, conv_id, priority, position)",
    ),
    # Project collaboration state is owner-scoped even when two principals
    # address the same filesystem path. Historical personal-installation data
    # belongs to principal 1 and is re-keyed once during the schema upgrade.
    20: (
        "ALTER TABLE storage_board_tasks "
        "ADD COLUMN user_id BIGINT NOT NULL DEFAULT 1",
        "ALTER TABLE storage_watch_items "
        "ADD COLUMN user_id BIGINT NOT NULL DEFAULT 1",
        "DROP INDEX IF EXISTS idx_storage_board_tasks_project",
        "CREATE INDEX idx_storage_board_tasks_project ON storage_board_tasks"
        "(user_id, project_path, created_at, id)",
        "DROP INDEX IF EXISTS idx_storage_watch_items_project",
        "CREATE INDEX idx_storage_watch_items_project ON storage_watch_items"
        "(user_id, project_path, updated_at DESC)",
        "UPDATE storage_records SET record_key = '1:' || record_key "
        "WHERE namespace = 'project_charter'",
        "UPDATE storage_events SET task_id = 'project-feed:1:' || "
        "SUBSTR(task_id, LENGTH('project-feed:') + 1) "
        "WHERE task_id LIKE 'project-feed:%'",
        "UPDATE storage_events SET task_id = 'project-status:1:' || "
        "SUBSTR(task_id, LENGTH('project-status:') + 1) "
        "WHERE task_id LIKE 'project-status:%'",
    ),
    # Integration workspaces are project data, not instance-global process
    # state. Rebuild the three small tables so ownership participates in every
    # key instead of retaining the former UNIQUE(project_root, task_id)
    # constraint as an accidental cross-user coordination boundary.
    21: (
        "CREATE TABLE integration_workspaces_v21 ("
        "id BIGINT PRIMARY KEY, user_id BIGINT NOT NULL, "
        "project_root TEXT NOT NULL, task_id TEXT NOT NULL, "
        "title TEXT NOT NULL DEFAULT '', workspace_path TEXT NOT NULL, "
        "managed BIGINT NOT NULL DEFAULT 0, "
        "state TEXT NOT NULL DEFAULT 'running', "
        "base_sha TEXT NOT NULL DEFAULT '', "
        "checkpoint_sha TEXT NOT NULL DEFAULT '', "
        "candidate_sha TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', "
        "created_at DOUBLE PRECISION NOT NULL, "
        "updated_at DOUBLE PRECISION NOT NULL, "
        "UNIQUE(user_id, project_root, task_id))",
        "INSERT INTO integration_workspaces_v21("
        "id, user_id, project_root, task_id, title, workspace_path, managed, "
        "state, base_sha, checkpoint_sha, candidate_sha, error, created_at, "
        "updated_at) SELECT id, 1, project_root, task_id, title, "
        "workspace_path, managed, state, base_sha, checkpoint_sha, "
        "candidate_sha, error, created_at, updated_at "
        "FROM integration_workspaces",
        "DROP TABLE integration_workspaces",
        "ALTER TABLE integration_workspaces_v21 "
        "RENAME TO integration_workspaces",
        "CREATE TABLE integration_events_v21 ("
        "id BIGINT PRIMARY KEY, user_id BIGINT NOT NULL, "
        "project_root TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '', "
        "kind TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', "
        "detail TEXT NOT NULL DEFAULT '', created_at DOUBLE PRECISION NOT NULL)",
        "INSERT INTO integration_events_v21("
        "id, user_id, project_root, task_id, kind, message, detail, created_at) "
        "SELECT id, 1, project_root, task_id, kind, message, detail, created_at "
        "FROM integration_events",
        "DROP TABLE integration_events",
        "ALTER TABLE integration_events_v21 RENAME TO integration_events",
        "CREATE TABLE integration_workspace_meta_v21 ("
        "user_id BIGINT NOT NULL, project_root TEXT NOT NULL, "
        "task_id TEXT NOT NULL, origin_json TEXT NOT NULL DEFAULT '{}', "
        "updated_at DOUBLE PRECISION NOT NULL, "
        "PRIMARY KEY(user_id, project_root, task_id))",
        "INSERT INTO integration_workspace_meta_v21("
        "user_id, project_root, task_id, origin_json, updated_at) "
        "SELECT 1, project_root, task_id, origin_json, updated_at "
        "FROM integration_workspace_meta",
        "DROP TABLE integration_workspace_meta",
        "ALTER TABLE integration_workspace_meta_v21 "
        "RENAME TO integration_workspace_meta",
    ),
    # The former generic event table encoded its domain in task_id prefixes
    # and buried event type inside an opaque binary JSON payload. That made
    # task retention delete project feeds and made structural-vs-streaming
    # retention impossible to express backend-neutrally. New rows classify
    # both dimensions explicitly. Historical task event types remain blank
    # and receive the conservative structural retention tier until expiry.
    22: (
        "ALTER TABLE storage_events ADD COLUMN "
        "stream_kind TEXT NOT NULL DEFAULT 'task'",
        "ALTER TABLE storage_events ADD COLUMN "
        "event_type TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE storage_events ADD COLUMN "
        "event_kind TEXT NOT NULL DEFAULT ''",
        "UPDATE storage_events SET stream_kind='project_feed' "
        "WHERE task_id LIKE 'project-feed:%'",
        "UPDATE storage_events SET stream_kind='project_status' "
        "WHERE task_id LIKE 'project-status:%'",
        "DROP INDEX IF EXISTS idx_storage_events_created",
    ),
    # Record the encoded size at write time so operators can inspect new
    # event growth without re-reading/parsing payload blobs. Historical rows
    # intentionally stay zero: backfilling the former multi-hundred-GB table
    # during startup would recreate the incident this migration mitigates.
    23: (
        "ALTER TABLE storage_attempt_events ADD COLUMN "
        "payload_bytes BIGINT NOT NULL DEFAULT 0",
    ),
    # Conversation deletion is a lifecycle transition, not an irreversible
    # transcript cascade.  The latest table definitions create the normalized
    # trash authority before this version is published; no backend-specific
    # ALTER statement is required.
    24: (),
    # Account identifiers are public opaque strings; repository ownership is
    # a separate positive integer allocated transactionally.  Rebuild the
    # small account table so every historical account receives a distinct
    # owner and no former string-id coercion can alias two principals.  The
    # personal installation owner (1) remains reserved.
    25: (
        "CREATE TABLE tenant_users_v25 ("
        "id TEXT PRIMARY KEY, owner_user_id BIGINT NOT NULL UNIQUE, "
        "email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL DEFAULT '', "
        "display_name TEXT NOT NULL DEFAULT '', "
        "role TEXT NOT NULL DEFAULT 'user', "
        "status TEXT NOT NULL DEFAULT 'active', created_at BIGINT NOT NULL, "
        "last_login_at BIGINT NOT NULL DEFAULT 0, "
        "email_verified BIGINT NOT NULL DEFAULT 0, "
        "metadata TEXT NOT NULL DEFAULT '{}')",
        "INSERT INTO tenant_users_v25("
        "id, owner_user_id, email, password_hash, display_name, role, status, "
        "created_at, last_login_at, email_verified, metadata) "
        "SELECT id, ROW_NUMBER() OVER (ORDER BY created_at, id) + 1, email, "
        "password_hash, display_name, role, status, created_at, "
        "last_login_at, email_verified, metadata FROM tenant_users",
        "DROP TABLE tenant_users",
        "ALTER TABLE tenant_users_v25 RENAME TO tenant_users",
        "CREATE INDEX idx_tenant_users_email ON tenant_users(email)",
        "CREATE INDEX idx_tenant_users_role ON tenant_users(role)",
        "CREATE INDEX idx_tenant_users_status_created "
        "ON tenant_users(status, created_at DESC)",
    ),
    # Distributed workers need a shared claim authority.  The latest table
    # registry creates the new queue before the version is published; no
    # historical rows exist to rewrite.
    26: (),
    # BYO provider configuration is owner data and must be visible to every
    # process.  The former key-id-scoped JSON cache and plaintext secret file
    # are intentionally not imported: v27 establishes one encrypted Sidecar
    # authority with no compatibility read path.
    27: (),
    # Definition and run ownership are one explicit boundary. Historical run
    # rows came from the sole personal installation and are assigned owner 1;
    # user-authored JSON definitions are intentionally not imported because
    # that file carried no trustworthy owner identity.
    28: (
        "CREATE TABLE orchestration_runs_v28 ("
        "id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, "
        "tenant_id TEXT NOT NULL DEFAULT '', orch_id TEXT NOT NULL DEFAULT '', "
        "name TEXT NOT NULL DEFAULT '', definition TEXT NOT NULL DEFAULT '{}', "
        "input TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', "
        "final TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', "
        "created_by TEXT NOT NULL DEFAULT '', created_at BIGINT NOT NULL DEFAULT 0, "
        "updated_at BIGINT NOT NULL DEFAULT 0, finished_at BIGINT NOT NULL DEFAULT 0)",
        "INSERT INTO orchestration_runs_v28("
        "id, user_id, tenant_id, orch_id, name, definition, input, status, final, "
        "error, created_by, created_at, updated_at, finished_at) "
        "SELECT id, 1, '', orch_id, name, definition, input, status, final, error, "
        "created_by, created_at, updated_at, finished_at FROM orchestration_runs",
        "DROP TABLE orchestration_runs",
        "ALTER TABLE orchestration_runs_v28 RENAME TO orchestration_runs",
        "CREATE TABLE orchestration_run_events_v28 ("
        "run_id TEXT NOT NULL, user_id BIGINT NOT NULL, "
        "tenant_id TEXT NOT NULL DEFAULT '', seq BIGINT NOT NULL, "
        "type TEXT NOT NULL DEFAULT '', node_id TEXT NOT NULL DEFAULT '', "
        "payload TEXT NOT NULL DEFAULT '{}', ts BIGINT NOT NULL DEFAULT 0, "
        "PRIMARY KEY(run_id, user_id, tenant_id, seq))",
        "INSERT INTO orchestration_run_events_v28("
        "run_id, user_id, tenant_id, seq, type, node_id, payload, ts) "
        "SELECT run_id, 1, '', seq, type, node_id, payload, ts "
        "FROM orchestration_run_events",
        "DROP TABLE orchestration_run_events",
        "ALTER TABLE orchestration_run_events_v28 "
        "RENAME TO orchestration_run_events",
        "CREATE INDEX idx_orch_runs_status ON orchestration_runs"
        "(tenant_id, user_id, status, updated_at DESC)",
        "CREATE INDEX idx_orch_runs_orch ON orchestration_runs"
        "(tenant_id, user_id, orch_id, created_at DESC)",
    ),
    # Large tool results are reconstructible context artifacts, not transcript
    # authority. The latest table catalogue creates an owner-scoped CAS with a
    # finite lifecycle; no historical filesystem spills are imported.
    29: (),
    # Revocation is an auditable terminal state. Keeping the hash tombstone
    # lets authentication boundaries attribute a stale paired device to its
    # owner without ever accepting the credential or exposing its hash.
    30: (
        "ALTER TABLE auth_credentials ADD COLUMN revoked_at DOUBLE PRECISION",
    ),
    # Redemption codes join wallet and ledger mutations inside one Sidecar
    # transaction. The latest table catalogue creates the new authority before
    # publishing this version; legacy driver rows are intentionally not read.
    31: (),
    # Reports, translations, podcasts, and margin notes may contain personal
    # context or private annotations. A content hash is an identifier, never an
    # authorization boundary. Historical personal-installation rows belong to
    # owner 1; every new operation requires an explicit owner.
    32: (
        "CREATE TABLE paper_reports_v32 ("
        "user_id BIGINT NOT NULL, paper_hash TEXT NOT NULL, "
        "lang TEXT NOT NULL DEFAULT 'en', report TEXT NOT NULL DEFAULT '', "
        "model TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '', "
        "created_at BIGINT NOT NULL, "
        "PRIMARY KEY(user_id, paper_hash, lang))",
        "INSERT INTO paper_reports_v32("
        "user_id, paper_hash, lang, report, model, meta, created_at) "
        "SELECT 1, paper_hash, lang, report, model, meta, created_at "
        "FROM paper_reports",
        "DROP TABLE paper_reports",
        "ALTER TABLE paper_reports_v32 RENAME TO paper_reports",
        "CREATE TABLE paper_translations_v32 ("
        "user_id BIGINT NOT NULL, paper_hash TEXT NOT NULL, lang TEXT NOT NULL, "
        "text TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', "
        "created_at BIGINT NOT NULL, "
        "PRIMARY KEY(user_id, paper_hash, lang))",
        "INSERT INTO paper_translations_v32("
        "user_id, paper_hash, lang, text, model, created_at) "
        "SELECT 1, paper_hash, lang, text, model, created_at "
        "FROM paper_translations",
        "DROP TABLE paper_translations",
        "ALTER TABLE paper_translations_v32 RENAME TO paper_translations",
        "CREATE TABLE paper_podcasts_v32 ("
        "user_id BIGINT NOT NULL, paper_hash TEXT NOT NULL, mode TEXT NOT NULL, "
        "lang TEXT NOT NULL, voice TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL DEFAULT 'generating', "
        "script_json TEXT NOT NULL DEFAULT '', file_path TEXT NOT NULL DEFAULT '', "
        "duration_sec DOUBLE PRECISION NOT NULL DEFAULT 0, "
        "model TEXT NOT NULL DEFAULT '', tts_model TEXT NOT NULL DEFAULT '', "
        "meta TEXT NOT NULL DEFAULT '', created_at BIGINT NOT NULL, "
        "updated_at BIGINT NOT NULL, "
        "PRIMARY KEY(user_id, paper_hash, mode, lang, voice))",
        "INSERT INTO paper_podcasts_v32("
        "user_id, paper_hash, mode, lang, voice, status, script_json, file_path, "
        "duration_sec, model, tts_model, meta, created_at, updated_at) "
        "SELECT 1, paper_hash, mode, lang, voice, status, script_json, file_path, "
        "duration_sec, model, tts_model, meta, created_at, updated_at "
        "FROM paper_podcasts",
        "DROP TABLE paper_podcasts",
        "ALTER TABLE paper_podcasts_v32 RENAME TO paper_podcasts",
        "CREATE TABLE paper_notes_v32 ("
        "user_id BIGINT NOT NULL, id TEXT NOT NULL, "
        "paper_hash TEXT NOT NULL DEFAULT '', lang TEXT NOT NULL DEFAULT '', "
        "anchor TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '', "
        "created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL, "
        "PRIMARY KEY(user_id, id))",
        "INSERT INTO paper_notes_v32("
        "user_id, id, paper_hash, lang, anchor, note, created_at, updated_at) "
        "SELECT 1, id, paper_hash, lang, anchor, note, created_at, updated_at "
        "FROM paper_notes",
        "DROP TABLE paper_notes",
        "ALTER TABLE paper_notes_v32 RENAME TO paper_notes",
    ),
    # Schema 32 introduced owner-scoped paper artifacts. Some personal
    # installations had already published version 32 through the legacy
    # bootstrap before those table rebuilds ran. Version 33 gives that drift
    # a deterministic repair point; initialize_schema replays the v32 rebuild
    # only when one of the four ownership columns is actually absent.
    33: (),
    # Knowledge metadata, chunks, and visual assets now use normalized,
    # owner-scoped Sidecar tables. The former application-owned auxiliary
    # SQLite store and short-lived generic-record prototype are intentionally
    # not imported: neither carried a trustworthy cross-user authority.
    34: (),
    # A backend-neutral inverted term index replaces substring scans. Version
    # 34 existed only on the development branch, so it has no supported data
    # migration; reindexing is the explicit projection rebuild boundary.
    35: (),
    # Built-in scheduler jobs now have a stable machine identity. Human names
    # are presentation, not primary keys. The two former local-PostgreSQL task
    # types have no executor in the Sidecar architecture, so retire them at
    # the schema boundary instead of carrying runtime compatibility branches.
    36: (
        "ALTER TABLE storage_scheduled_tasks ADD COLUMN "
        "system_key TEXT NOT NULL DEFAULT ''",
        "DELETE FROM storage_scheduled_tasks "
        "WHERE task_type IN ('pg_backup', 'pg_basebackup')",
    ),
    # Optimizer proposals and their action audit rows are durable user state.
    # Historical rows came only from the personal installation owner; fresh
    # operation payloads require a positive user_id and never consult this
    # compatibility default.
    37: (
        "ALTER TABLE optimizer_proposals ADD COLUMN "
        "user_id BIGINT NOT NULL DEFAULT 1",
        "ALTER TABLE optimizer_action_log ADD COLUMN "
        "user_id BIGINT NOT NULL DEFAULT 1",
        "CREATE TABLE optimizer_proposals_v37 ("
        "user_id BIGINT NOT NULL, id TEXT NOT NULL, created_at TEXT NOT NULL, "
        "title TEXT NOT NULL, rationale TEXT NOT NULL, action_type TEXT NOT NULL, "
        "action_args TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'low', "
        "confidence DOUBLE PRECISION NOT NULL DEFAULT 0, "
        "evidence TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL DEFAULT 'pending_review', "
        "status_reason TEXT NOT NULL DEFAULT '', PRIMARY KEY(user_id, id))",
        "INSERT INTO optimizer_proposals_v37("
        "user_id, id, created_at, title, rationale, action_type, action_args, "
        "severity, confidence, evidence, status, status_reason) SELECT "
        "user_id, id, created_at, title, rationale, action_type, action_args, "
        "severity, confidence, evidence, status, status_reason "
        "FROM optimizer_proposals",
        "CREATE TABLE optimizer_action_log_v37 ("
        "user_id BIGINT NOT NULL, id TEXT NOT NULL, proposal_id TEXT NOT NULL, "
        "applied_at TEXT NOT NULL, expires_at TEXT NOT NULL DEFAULT '', "
        "pre_metric TEXT NOT NULL DEFAULT '', "
        "outcome_metric TEXT NOT NULL DEFAULT '', "
        "outcome_recorded_at TEXT NOT NULL DEFAULT '', "
        "reverted_at TEXT NOT NULL DEFAULT '', "
        "revert_reason TEXT NOT NULL DEFAULT '', PRIMARY KEY(user_id, id), "
        "FOREIGN KEY(user_id, proposal_id) "
        "REFERENCES optimizer_proposals_v37(user_id, id))",
        "INSERT INTO optimizer_action_log_v37("
        "user_id, id, proposal_id, applied_at, expires_at, pre_metric, "
        "outcome_metric, outcome_recorded_at, reverted_at, revert_reason) "
        "SELECT user_id, id, proposal_id, applied_at, expires_at, pre_metric, "
        "outcome_metric, outcome_recorded_at, reverted_at, revert_reason "
        "FROM optimizer_action_log",
        # Drop the old child before its referenced parent. The replacement
        # child points at the replacement parent, whose later rename is
        # propagated by both SQLite and PostgreSQL. This ordering is required
        # when foreign-key enforcement is actually enabled.
        "DROP TABLE optimizer_action_log",
        "DROP TABLE optimizer_proposals",
        "ALTER TABLE optimizer_proposals_v37 RENAME TO optimizer_proposals",
        "ALTER TABLE optimizer_action_log_v37 RENAME TO optimizer_action_log",
    ),
    # Derived indexes no longer share the authority writer. Mutations publish
    # compact dirty-set markers here; a backend-owned projection runtime
    # materializes them independently and acknowledges only the exact token it
    # observed. The table/index are created from the latest catalogue above,
    # so the ordered migration itself is intentionally metadata-only.
    38: (),
    # Every semantic command can now publish a replayable record from the
    # same transaction as its domain mutation. Existing domain rows remain
    # the authority and are intentionally not rewritten during this
    # constant-time expansion.
    39: (),
    # The model-visible continuation summary and the user-visible audit result
    # are separate bounded artifacts.  Existing archive rows decode as an
    # empty/legacy receipt; new rows carry a versioned structured result.
    40: (
        "ALTER TABLE storage_compaction_archives ADD COLUMN "
        "receipt_json JSONDOC NOT NULL DEFAULT '{}'",
    ),
    # Uploaded chat documents and videos reuse the knowledge source/chunk/asset
    # authority instead of creating a second file registry.  ``scope`` keeps
    # attachment-only sources out of opt-in global knowledge retrieval, while
    # bounded JSON metadata carries media processing state and frame timing.
    41: (
        "ALTER TABLE storage_knowledge_documents ADD COLUMN "
        "scope TEXT NOT NULL DEFAULT 'library'",
        "ALTER TABLE storage_knowledge_documents ADD COLUMN "
        "media_metadata_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE storage_knowledge_assets ADD COLUMN "
        "metadata_json TEXT NOT NULL DEFAULT '{}'",
    ),
    # A recoverable delete must preserve the frozen pre-turn transcript
    # archive as faithfully as the normalized turn rows; otherwise restoring
    # a conversation whose only durable transcript is messages_json comes
    # back as an empty chat (irreversible history loss on a recoverable op).
    42: (
        "ALTER TABLE storage_conversation_trash ADD COLUMN "
        "messages_json JSONDOC NOT NULL DEFAULT '[]'",
    ),
    # Rate-limit rows are reconstructible enforcement state. The old schema
    # carried no per-row expiry, so a client seen only once could occupy disk
    # forever and no backend-neutral cleanup could distinguish its window.
    # Reset that cache once, then require every new event to own an exact TTL.
    43: (
        "DROP TABLE storage_rate_limit_events",
        "CREATE TABLE storage_rate_limit_events ("
        "event_id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, "
        "client_key TEXT NOT NULL, occurred_at_ms BIGINT NOT NULL, "
        "expires_at_ms BIGINT NOT NULL)",
    ),
    # Goal Mode has one durable lifecycle owner per conversation.  The
    # semantic start operation supersedes the prior run transactionally; this
    # partial index is the cross-process/SQL-backend fence that prevents two
    # concurrent writers from publishing two active GoalRuns.
    44: (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_goal_runs_one_active "
        "ON orchestration_runs(tenant_id, user_id, orch_id) "
        "WHERE created_by = 'chat_goal_mode' "
        "AND status IN ('pending', 'running', 'paused')",
    ),
    # An accepted conversation command records whether its pending attempt is
    # eligible for the in-process model executor.  The partial index lets the
    # serving-loop recovery owner find only rows that provably never reached a
    # claim/bind, without polling through historical attempts or mistaking
    # external-channel persistence attempts for model work.
    45: (
        "ALTER TABLE storage_generation_attempts ADD COLUMN "
        "dispatch_mode TEXT NOT NULL DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_storage_generation_attempts_dispatchable "
        "ON storage_generation_attempts(created_at, attempt_id) "
        "WHERE status = 'pending' AND task_id = '' "
        "AND dispatch_mode = 'conversation_executor'",
    ),
    # Timing evidence belongs to one immutable generation attempt, not to the
    # mutable current Turn projection.  This keeps regenerated attempts
    # independently inspectable and lets frequent, bounded browser receipts
    # update a small document instead of rewriting a potentially multi-MiB
    # assistant projection.
    46: (
        "ALTER TABLE storage_generation_attempts ADD COLUMN "
        "timing_trace_json JSONDOC NOT NULL DEFAULT '{}'",
        "CREATE INDEX IF NOT EXISTS idx_storage_generation_attempts_task "
        "ON storage_generation_attempts(task_id) WHERE task_id <> ''",
    ),
    # Request Inspector discovery must not depend on a global task-result scan:
    # the durable attempt is the trace authority, so page its compact identity
    # rows directly by owner-checked conversation and creation time.  The
    # partial index contributes exactly one entry per externally addressable
    # generation attempt and stores no trace JSON or message content.
    47: (
        "CREATE INDEX IF NOT EXISTS "
        "idx_storage_generation_attempts_conversation_created "
        "ON storage_generation_attempts("
        "conversation_id, created_at DESC, attempt_id DESC) "
        "WHERE task_id <> ''",
    ),
    # A live Turn may retain its current full projection as a bounded chain of
    # already-durable attempt-event patches. Nullable materialized revision is
    # the no-backfill compatibility sentinel: NULL means projection_json is at
    # projection_revision. Constant defaults keep this expansion metadata-only
    # on both SQLite and PostgreSQL; no historical projection is decoded.
    48: (
        "ALTER TABLE storage_conversation_turns ADD COLUMN "
        "projection_materialized_revision BIGINT",
        "ALTER TABLE storage_conversation_turns ADD COLUMN "
        "projection_patch_count BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE storage_conversation_turns ADD COLUMN "
        "projection_patch_bytes BIGINT NOT NULL DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS idx_storage_attempt_events_projection_chain "
        "ON storage_attempt_events(attempt_id, projection_revision, sequence)",
    ),
    # Large live Turn projections can move to a separately stored checkpoint
    # while their hot row keeps only bounded head metadata. Version 48 was
    # already published without this column, so this expansion owns a new
    # migration number; changing an applied migration would strand established
    # authorities at a schema shape the recorded version does not describe.
    49: (
        "ALTER TABLE storage_conversation_turns ADD COLUMN "
        "projection_checkpoint_revision BIGINT",
    ),
    # Schema 49 briefly allowed an unchanged live frame to advance the Turn
    # revision while leaving its external checkpoint at the prior revision.
    # Repair only that exact one-revision/no-head cohort. The checkpoint body
    # remains the current projection, so this changes metadata without reading,
    # decoding, or rewriting the potentially multi-MiB JSON document.
    50: (
        "UPDATE storage_turn_projection_checkpoints SET "
        "projection_revision=projection_revision+1 WHERE EXISTS ("
        "SELECT 1 FROM storage_conversation_turns AS turn_row WHERE "
        "turn_row.turn_id=storage_turn_projection_checkpoints.turn_id AND "
        "turn_row.conversation_id="
        "storage_turn_projection_checkpoints.conversation_id AND "
        "turn_row.user_id=storage_turn_projection_checkpoints.user_id AND "
        "turn_row.current_attempt_id="
        "storage_turn_projection_checkpoints.attempt_id AND "
        "turn_row.projection_checkpoint_revision="
        "storage_turn_projection_checkpoints.projection_revision AND "
        "turn_row.projection_revision="
        "storage_turn_projection_checkpoints.projection_revision+1 AND "
        "turn_row.projection_materialized_revision IS NULL AND "
        "turn_row.projection_patch_count=0 AND "
        "turn_row.projection_patch_bytes=0)",
        "UPDATE storage_conversation_turns SET "
        "projection_checkpoint_revision=projection_revision WHERE "
        "projection_checkpoint_revision IS NOT NULL AND "
        "projection_revision=projection_checkpoint_revision+1 AND "
        "projection_materialized_revision IS NULL AND "
        "projection_patch_count=0 AND projection_patch_bytes=0 AND EXISTS ("
        "SELECT 1 FROM storage_turn_projection_checkpoints AS checkpoint "
        "WHERE checkpoint.turn_id=storage_conversation_turns.turn_id AND "
        "checkpoint.conversation_id="
        "storage_conversation_turns.conversation_id AND "
        "checkpoint.user_id=storage_conversation_turns.user_id AND "
        "checkpoint.attempt_id="
        "storage_conversation_turns.current_attempt_id AND "
        "checkpoint.projection_revision="
        "storage_conversation_turns.projection_revision)",
    ),
    # Attempt events already own their exact durable envelope. Conversation
    # replay rows can reference that authority while it is retained instead
    # of encoding the same potentially multi-MiB document a second time. NULL
    # is the compatibility sentinel for historical self-contained rows; no
    # existing JSON is read or rewritten by this metadata-only expansion.
    51: (
        "ALTER TABLE storage_conversation_changes ADD COLUMN "
        "attempt_sequence BIGINT",
        "CREATE INDEX IF NOT EXISTS "
        "idx_storage_conversation_changes_attempt_event_reference "
        "ON storage_conversation_changes(attempt_id, attempt_sequence) "
        "WHERE attempt_sequence IS NOT NULL",
    ),
    # Arbitrary command IDs and hexadecimal digests dominate the permanent
    # receipt key footprint. New writes use a fixed-width SHA-256 command key
    # and binary request digest while retaining operation attribution. The
    # historical table remains readable and is deliberately not backfilled at
    # startup. SQLite stores the primary key once; PostgreSQL renders the same
    # logical table without its SQLite-only physical table option.
    52: (
        "CREATE TABLE IF NOT EXISTS storage_command_receipts_v2 ("
        "command_key BLOB PRIMARY KEY, operation TEXT NOT NULL, "
        "request_digest BLOB NOT NULL, response_json BLOB NOT NULL, "
        "committed_at_ms BIGINT NOT NULL, "
        "CHECK (length(command_key) = 32), "
        "CHECK (length(request_digest) = 32)) WITHOUT ROWID",
    ),
    # Egress-agent selection is durable user preference, not ephemeral bridge
    # transport.  The explicit empty value also serves as the per-owner marker
    # that the retired oauth_egress_agents.json source has been considered.
    53: (
        "CREATE TABLE IF NOT EXISTS storage_desktop_egress_preferences ("
        "owner_user_id BIGINT PRIMARY KEY, agent_id TEXT NOT NULL DEFAULT '', "
        "updated_at_ms BIGINT NOT NULL)",
    ),
    # Model/provider configuration is one owner-scoped revisioned v2
    # aggregate. Credential plaintext remains outside that document and only
    # encrypted secret references participate in its CAS transaction.
    54: (
        "CREATE TABLE IF NOT EXISTS storage_model_routing_authorities ("
        "owner_user_id BIGINT NOT NULL, tenant_id TEXT NOT NULL DEFAULT '', "
        "revision BIGINT NOT NULL, document_json JSONDOC NOT NULL, "
        "backup_json JSONDOC, migration_receipt_json JSONDOC, "
        "updated_at DOUBLE PRECISION NOT NULL, "
        "PRIMARY KEY (owner_user_id, tenant_id))",
        "CREATE TABLE IF NOT EXISTS storage_model_routing_secrets ("
        "owner_user_id BIGINT NOT NULL, tenant_id TEXT NOT NULL DEFAULT '', "
        "secret_reference TEXT NOT NULL, ciphertext TEXT NOT NULL, "
        "key_hint TEXT NOT NULL DEFAULT '', created_at DOUBLE PRECISION NOT NULL, "
        "updated_at DOUBLE PRECISION NOT NULL, "
        "PRIMARY KEY (owner_user_id, tenant_id, secret_reference))",
        "CREATE INDEX IF NOT EXISTS idx_model_routing_secrets_owner_updated "
        "ON storage_model_routing_secrets("
        "tenant_id, owner_user_id, updated_at, secret_reference)",
    ),
    # A browser presentation identity survives optimistic acceptance, queue
    # activation and reconnect. Queue rows now point at the already-created
    # Turn pair/Attempt instead of acting as a second transcript authority.
    55: (
        "ALTER TABLE storage_conversation_turns ADD COLUMN "
        "presentation_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE storage_generation_attempts ADD COLUMN "
        "queue_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE storage_generation_attempts ADD COLUMN "
        "queue_state TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE storage_queue_items ADD COLUMN "
        "input_turn_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE storage_queue_items ADD COLUMN "
        "output_turn_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE storage_queue_items ADD COLUMN "
        "attempt_id TEXT NOT NULL DEFAULT ''",
        "DROP INDEX IF EXISTS idx_storage_generation_attempts_dispatchable",
    ),
    # Project Brain is cut over atomically to one owner/project event stream
    # plus a rebuild checkpoint.  The generic event table keeps task events
    # byte-compatible through defaults while project rows carry their owner,
    # normalized project key, and monotonic project sequence explicitly.
    56: (
        "ALTER TABLE storage_events ADD COLUMN "
        "owner_user_id BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE storage_events ADD COLUMN "
        "project_key TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE storage_events ADD COLUMN "
        "project_sequence BIGINT NOT NULL DEFAULT 0",
        "CREATE TABLE IF NOT EXISTS storage_project_brain_projects ("
        "owner_user_id BIGINT NOT NULL, project_key TEXT NOT NULL, "
        "head_sequence BIGINT NOT NULL, "
        "checkpoint_sequence BIGINT NOT NULL DEFAULT 0, "
        "projection_json JSONDOC NOT NULL, updated_at_ms BIGINT NOT NULL, "
        "PRIMARY KEY (owner_user_id, project_key))",
        "CREATE INDEX IF NOT EXISTS idx_storage_project_brain_updated "
        "ON storage_project_brain_projects("
        "owner_user_id, updated_at_ms, project_key)",
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_storage_events_project_sequence "
        "ON storage_events(owner_user_id, project_key, project_sequence) "
        "WHERE project_sequence > 0",
    ),
    # Provider-bound Request Inspector evidence is durable user data. Each
    # row belongs to an owner-checked Attempt, stores independently compressed
    # request/response bodies, and participates in an explicit global byte
    # budget rather than TTL or silent eviction.
    57: (
        "CREATE TABLE IF NOT EXISTS storage_raw_archives ("
        "archive_id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, "
        "conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL, "
        "attempt_id TEXT NOT NULL, task_id TEXT NOT NULL, "
        "round_num BIGINT NOT NULL, transport_attempt BIGINT NOT NULL DEFAULT 0, "
        "request_blob BLOB NOT NULL, response_blob BLOB NOT NULL, "
        "request_bytes BIGINT NOT NULL, response_bytes BIGINT NOT NULL, "
        "stored_bytes BIGINT NOT NULL, request_sha256 TEXT NOT NULL, "
        "response_sha256 TEXT NOT NULL, integrity TEXT NOT NULL, "
        "truncation_reason TEXT NOT NULL DEFAULT '', "
        "summary_json JSONDOC NOT NULL DEFAULT '{}', created_at_ms BIGINT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_storage_raw_archives_task_round "
        "ON storage_raw_archives("
        "user_id, task_id, round_num, created_at_ms, archive_id)",
        "CREATE INDEX IF NOT EXISTS idx_storage_raw_archives_attempt "
        "ON storage_raw_archives("
        "user_id, attempt_id, created_at_ms, archive_id)",
    ),
}


_ADD_COLUMN_STATEMENT = re.compile(
    r'^\s*ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+'
    r'ADD\s+COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)\b',
    re.IGNORECASE,
)


# These indexes reference columns introduced or tables rebuilt by ordered
# migrations. Creating them with the latest table catalogue before migrations
# run would make a recoverable older database fail startup. Install them only
# after the expansion and version publication work have succeeded; IF NOT
# EXISTS keeps current-schema startup constant-time.
_POST_MIGRATION_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_storage_events_project_sequence "
    "ON storage_events(owner_user_id, project_key, project_sequence) "
    "WHERE project_sequence > 0",
    "CREATE INDEX IF NOT EXISTS "
    "idx_storage_conversation_changes_attempt_event_reference "
    "ON storage_conversation_changes(attempt_id, attempt_sequence) "
    "WHERE attempt_sequence IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_storage_generation_attempts_dispatchable "
    "ON storage_generation_attempts(created_at, attempt_id) "
    "WHERE status = 'pending' AND task_id = '' "
    "AND dispatch_mode = 'conversation_executor' AND queue_state = ''",
    "CREATE INDEX IF NOT EXISTS storage_rate_limit_bucket_idx "
    "ON storage_rate_limit_events(endpoint, client_key, occurred_at_ms)",
    "CREATE INDEX IF NOT EXISTS storage_rate_limit_expiry_idx "
    "ON storage_rate_limit_events(expires_at_ms, event_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduler_system_task "
    "ON storage_scheduled_tasks(user_id, system_key) WHERE system_key <> ''",
    "CREATE INDEX IF NOT EXISTS idx_orch_runs_status ON orchestration_runs"
    "(tenant_id, user_id, status, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_orch_runs_orch ON orchestration_runs"
    "(tenant_id, user_id, orch_id, created_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_goal_runs_one_active "
    "ON orchestration_runs(tenant_id, user_id, orch_id) "
    "WHERE created_by = 'chat_goal_mode' "
    "AND status IN ('pending', 'running', 'paused')",
    "CREATE INDEX IF NOT EXISTS idx_orch_definitions_owner_updated "
    "ON orchestration_definitions"
    "(tenant_id, user_id, updated_at_ms DESC, id)",
    "CREATE INDEX IF NOT EXISTS idx_paper_reports_owner_latest "
    "ON paper_reports(user_id, paper_hash, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_paper_notes_owner_document "
    "ON paper_notes(user_id, paper_hash, lang, created_at, id)",
    "CREATE INDEX IF NOT EXISTS idx_opt_prop_owner_created "
    "ON optimizer_proposals(user_id, created_at DESC, id)",
    "CREATE INDEX IF NOT EXISTS idx_opt_prop_owner_status "
    "ON optimizer_proposals(user_id, status, created_at DESC, id)",
    "CREATE INDEX IF NOT EXISTS idx_opt_prop_owner_action "
    "ON optimizer_proposals(user_id, action_type, created_at DESC, id)",
    "CREATE INDEX IF NOT EXISTS idx_opt_actlog_owner_proposal "
    "ON optimizer_action_log(user_id, proposal_id, applied_at DESC, id)",
    "CREATE INDEX IF NOT EXISTS idx_opt_actlog_owner_applied "
    "ON optimizer_action_log(user_id, applied_at DESC, id)",
    "CREATE INDEX IF NOT EXISTS idx_opt_actlog_owner_expires "
    "ON optimizer_action_log(user_id, expires_at, id)",
)


def _column_exists(session: Session, table_name: str, column_name: str) -> bool:
    """Inspect one trusted migration identifier on either storage backend."""
    if session.backend == 'postgres':
        return session.fetch_one(
            'SELECT 1 AS present FROM information_schema.columns '
            'WHERE table_schema = current_schema() AND table_name = ? '
            'AND column_name = ?',
            (table_name, column_name),
        ) is not None
    rows = session.fetch_all(f'PRAGMA table_info("{table_name}")')
    return any(str(row['name'] or '') == column_name for row in rows)


def _table_exists(session: Session, table_name: str) -> bool:
    """Return whether a trusted schema-migration table exists."""
    if session.backend == 'postgres':
        return session.fetch_one(
            'SELECT 1 AS present FROM information_schema.tables '
            'WHERE table_schema = current_schema() AND table_name = ?',
            (table_name,),
        ) is not None
    return session.fetch_one(
        "SELECT 1 AS present FROM sqlite_master "
        "WHERE type = 'table' AND name = ?",
        (table_name,),
    ) is not None


def _migration_statement_already_applied(
    session: Session, statement: str,
) -> bool:
    """Recognize an additive migration already supplied by a latest table.

    Missing tables are created from the current definition before version
    deltas replay. A partially restored old database can therefore contain a
    newly created table whose additive column already exists. Detect this
    before ``ALTER TABLE`` so PostgreSQL's startup transaction is not aborted.
    """
    # The signal-driven Project Brain cut removed these legacy authorities
    # from the latest schema. A very old or partially restored database may
    # never have created one of them; historical ALTER/INDEX migrations must
    # then be treated as moot so migration 56 can perform the atomic cutover.
    for retired_table in (
        'storage_board_tasks',
        'storage_watch_items',
        'storage_watch_runs',
        'storage_watch_responses',
    ):
        if (retired_table in statement
                and not _table_exists(session, retired_table)):
            return True

    match = _ADD_COLUMN_STATEMENT.match(statement)
    if match is None:
        return False
    return _column_exists(session, match.group(1), match.group(2))


def _paper_artifact_ownership_is_current(session: Session) -> bool:
    return all(
        _column_exists(session, table_name, 'user_id')
        for table_name in (
            'paper_reports',
            'paper_translations',
            'paper_podcasts',
            'paper_notes',
        )
    )


def _optimizer_ownership_is_current(session: Session) -> bool:
    return all(
        _column_exists(session, table_name, 'user_id')
        for table_name in ('optimizer_proposals', 'optimizer_action_log')
    )


def _sql_for_backend(session: Session, sql: str) -> str:
    """Render logical types and backend-specific physical table options."""
    if session.backend == 'postgres':
        return (
            sql.replace(' BLOB ', ' BYTEA ')
            .replace(' JSONDOC ', ' JSONB ')
            .replace(' WITHOUT ROWID', '')
        )
    return sql.replace(' JSONDOC ', ' TEXT ')


def initialize_schema(session: Session) -> None:
    for sql in _TABLES:
        # PostgreSQL has BYTEA rather than BLOB and uses its ordinary primary
        # key heap where SQLite can store a fixed-width key WITHOUT ROWID.
        # JSONDOC preserves the legacy/cutover representation: JSONB on PG,
        # TEXT on SQLite, while callers still exchange decoded documents.
        session.execute(_sql_for_backend(session, sql))
    current = session.fetch_one(
        'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
        ('schema_version',),
    )
    current_version = int(current['meta_value']) if current is not None else None
    # The legacy application bootstrap can create the four historical paper
    # tables before a new Sidecar writes its own schema marker. CREATE TABLE IF
    # NOT EXISTS cannot upgrade those tables, so treating a missing marker as a
    # pristine latest schema would publish an invalid authority and make the
    # owner indexes fail. Repair the structural invariant before publishing or
    # accepting v32+ metadata. Older versioned authorities replay migration 32
    # through the normal ordered loop below.
    if ((current_version is None or current_version >= 32)
            and not _paper_artifact_ownership_is_current(session)):
        for statement in _MIGRATIONS[32]:
            session.execute(_sql_for_backend(session, statement))
    # A pre-Sidecar personal database can likewise contain the legacy
    # optimizer tables without a storage_meta marker. Rebuild them before a
    # fresh v37 marker is published; the DEFAULT 1 exists only during this
    # one-time historical backfill and is absent from the rebuilt tables.
    if (current_version is None
            and not _optimizer_ownership_is_current(session)):
        for statement in _MIGRATIONS[37]:
            if _migration_statement_already_applied(session, statement):
                continue
            session.execute(_sql_for_backend(session, statement))
    if current is None:
        session.execute(
            'INSERT INTO storage_meta(meta_key, meta_value) VALUES (?, ?)',
            ('schema_version', str(SCHEMA_VERSION)),
        )
    elif current_version > SCHEMA_VERSION:
        raise RuntimeError('unsupported storage schema version')
    elif current_version < SCHEMA_VERSION:
        for target_version in range(current_version + 1, SCHEMA_VERSION + 1):
            for statement in _MIGRATIONS.get(target_version, ()):
                if _migration_statement_already_applied(session, statement):
                    continue
                session.execute(_sql_for_backend(session, statement))
        # The surrounding startup transaction publishes the new version only
        # after every expansion has succeeded.
        session.execute(
            'UPDATE storage_meta SET meta_value = ? WHERE meta_key = ?',
            (str(SCHEMA_VERSION), 'schema_version'),
        )
    for sql in _POST_MIGRATION_INDEXES:
        session.execute(sql)


def validate_schema_version(session: Session) -> int:
    """Validate an application connection without creating or migrating data."""
    try:
        current = session.fetch_one(
            'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
            ('schema_version',),
        )
    except Exception as exc:
        raise StorageError(
            'database_integrity',
            'PostgreSQL storage schema is unavailable; run the migration job',
        ) from exc
    try:
        version = int(current['meta_value']) if current is not None else None
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageError(
            'database_integrity',
            'PostgreSQL storage schema version is invalid; run the migration job',
        ) from exc
    if version != SCHEMA_VERSION:
        raise StorageError(
            'database_integrity',
            f'PostgreSQL storage schema version {version!r} does not match '
            f'required version {SCHEMA_VERSION}; run the migration job',
        )
    return version


# Deferred indexes are installed automatically only while a SQLite authority
# is brand new and all tables are empty. They exist purely to make hot reads
# fast; every query plan works (only slower) before they appear. Adding one to
# an established authority requires an explicit offline maintenance window:
# SQLite CREATE INDEX owns the sole writer for its full scan and must never run
# as post-ready background work against tables holding years of blobs.
#
# idx_storage_conversations_meta covers the sidebar's metadata-only
# conversation.list projection.  Without it the meta poll must rowid-lookup
# every row of a table whose messages_json/search_text blobs total multiple
# GiB — on a cold network filesystem that is minutes of random reads.  The
# index holds only small columns (settings_json dominates, ~MiB-scale), so
# the list becomes a compact index-only scan.
#
# The v2 event-retention indexes are mutually exclusive partial indexes. Each
# retained row enters exactly one small age-only B-tree, so an empty tier can be
# proved with one bounded range lookup and ORDER BY created_at_ms stops at LIMIT
# without scanning the other tier or building a table-sized temporary sort.
# Superseded generic indexes retire only in explicit offline maintenance;
# populated SQLite authorities never run that DDL transition during startup.
# idx_log_aggregates_last_seen keeps the best-effort observability TTL sweep
# away from the aggregate table's text payload pages.
_STRUCTURAL_EVENT_SQL = ','.join(
    "'" + event_type.replace("'", "''") + "'"
    for event_type in sorted(STRUCTURAL_EVENT_TYPES)
)
_TASK_STREAM_SQL = "'" + TASK_STREAM_KIND.replace("'", "''") + "'"
# Blank types are the one-time v21 migration cohort. Their opaque historical
# payload cannot be classified backend-neutrally, so they retain the longer
# structural horizon rather than risking destructive misclassification.
TASK_EVENT_RETENTION_SPECS = MappingProxyType({
    'streaming': (
        'idx_storage_events_streaming_retention_v2',
        f'stream_kind = {_TASK_STREAM_SQL} '
        f'AND event_type NOT IN ({_STRUCTURAL_EVENT_SQL}) '
        "AND event_type <> ''",
    ),
    'structural': (
        'idx_storage_events_structural_retention_v2',
        f'stream_kind = {_TASK_STREAM_SQL} '
        f'AND (event_type IN ({_STRUCTURAL_EVENT_SQL}) OR event_type = \'\')',
    ),
})
TASK_EVENT_RETENTION_INDEX_NAMES = frozenset(
    spec[0] for spec in TASK_EVENT_RETENTION_SPECS.values())
LEGACY_TASK_EVENT_RETENTION_INDEX_NAME = 'idx_storage_events_retention'
LEGACY_TASK_EVENT_RETENTION_TYPE_LIMIT = 64
OBSOLETE_DEFERRED_INDEX_NAMES = frozenset({
    LEGACY_TASK_EVENT_RETENTION_INDEX_NAME,
    'idx_storage_events_retention_v2',
})


_DEFERRED_INDEXES = (
    """
    CREATE INDEX IF NOT EXISTS idx_storage_conversations_meta
    ON storage_conversations(user_id, updated_at_ms, id, title,
                             created_at_ms, settings_json, msg_count, rev)
    """,
    *(f"""
    CREATE INDEX IF NOT EXISTS {index_name}
    ON storage_events(created_at_ms) WHERE {predicate}
    """ for index_name, predicate in TASK_EVENT_RETENTION_SPECS.values()),
    """
    CREATE INDEX IF NOT EXISTS idx_log_aggregates_last_seen
    ON log_aggregates(last_seen, fingerprint)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_integration_owner_ready
    ON integration_workspaces(user_id, project_root, state, updated_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_integration_owner_events
    ON integration_events(user_id, project_root, id DESC)
    """,
)


def deferred_index_statements(backend: str) -> tuple[str, ...]:
    if backend == 'postgres':
        return tuple(
            sql.replace(' BLOB ', ' BYTEA ').replace(' JSONDOC ', ' JSONB ')
            for sql in _DEFERRED_INDEXES)
    return _DEFERRED_INDEXES


def declared_table_names() -> frozenset[str]:
    """Return the backend-neutral table catalogue declared by this schema."""
    pattern = re.compile(
        r"^\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+"
        r"([A-Za-z_][A-Za-z0-9_]*)\b",
        re.IGNORECASE,
    )
    return frozenset(
        match.group(1)
        for statement in _TABLES
        if (match := pattern.match(statement)) is not None
    )


__all__ = [
    'LEGACY_TASK_EVENT_RETENTION_INDEX_NAME',
    'LEGACY_TASK_EVENT_RETENTION_TYPE_LIMIT',
    'OBSOLETE_DEFERRED_INDEX_NAMES',
    'SCHEMA_VERSION',
    'TASK_EVENT_RETENTION_INDEX_NAMES',
    'TASK_EVENT_RETENTION_SPECS',
    'declared_table_names',
    'deferred_index_statements',
    'initialize_schema',
    'validate_schema_version',
]
