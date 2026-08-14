"""The backend-parity schema owned exclusively by the sidecar."""

from __future__ import annotations

from lib.storage_sidecar.adapters.base import Session


SCHEMA_VERSION = 1


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
        event_json BLOB NOT NULL,
        created_at_ms BIGINT NOT NULL,
        PRIMARY KEY (task_id, sequence)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS storage_rate_limit_events (
        event_id TEXT PRIMARY KEY,
        endpoint TEXT NOT NULL,
        client_key TEXT NOT NULL,
        occurred_at_ms BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orchestration_runs (
        id TEXT PRIMARY KEY,
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
        seq BIGINT NOT NULL,
        type TEXT NOT NULL DEFAULT '',
        node_id TEXT NOT NULL DEFAULT '',
        payload TEXT NOT NULL DEFAULT '{}',
        ts BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, seq)
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
        paper_hash TEXT NOT NULL,
        lang TEXT NOT NULL DEFAULT 'en',
        report TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        meta TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL,
        PRIMARY KEY (paper_hash, lang)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_translations (
        paper_hash TEXT NOT NULL,
        lang TEXT NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL,
        PRIMARY KEY (paper_hash, lang)
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
        PRIMARY KEY (paper_hash, mode, lang, voice)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tenant_users (
        id TEXT PRIMARY KEY,
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
    CREATE TABLE IF NOT EXISTS optimizer_proposals (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        title TEXT NOT NULL,
        rationale TEXT NOT NULL,
        action_type TEXT NOT NULL,
        action_args TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'low',
        confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
        evidence TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending_review',
        status_reason TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS optimizer_action_log (
        id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        expires_at TEXT NOT NULL DEFAULT '',
        pre_metric TEXT NOT NULL DEFAULT '',
        outcome_metric TEXT NOT NULL DEFAULT '',
        outcome_recorded_at TEXT NOT NULL DEFAULT '',
        reverted_at TEXT NOT NULL DEFAULT '',
        revert_reason TEXT NOT NULL DEFAULT ''
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
    CREATE INDEX IF NOT EXISTS idx_paper_lib_user
    ON paper_library(user_id, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_paper_lib_hash
    ON paper_library(paper_hash, updated_at DESC)
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
    CREATE INDEX IF NOT EXISTS idx_orch_runs_status
    ON orchestration_runs(status, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_orch_runs_orch
    ON orchestration_runs(orch_id, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_swarm_sessions_status
    ON swarm_sessions(status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_swarm_agents_key
    ON swarm_agents(swarm_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opt_prop_created
    ON optimizer_proposals(created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opt_prop_status
    ON optimizer_proposals(status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opt_prop_action
    ON optimizer_proposals(action_type)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opt_actlog_proposal
    ON optimizer_action_log(proposal_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opt_actlog_applied
    ON optimizer_action_log(applied_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opt_actlog_expires
    ON optimizer_action_log(expires_at)
    """,
)


def initialize_schema(session: Session) -> None:
    for sql in _TABLES:
        # PostgreSQL has BYTEA rather than BLOB.  This is the only logical
        # binary type spelling that differs in the sidecar-owned v1 schema.
        # JSONDOC preserves the legacy/cutover representation: JSONB on PG,
        # TEXT on SQLite, while callers still exchange decoded documents.
        if session.backend == 'postgres':
            sql = sql.replace(' BLOB ', ' BYTEA ')
            sql = sql.replace(' JSONDOC ', ' JSONB ')
        else:
            sql = sql.replace(' JSONDOC ', ' TEXT ')
        session.execute(sql)
    current = session.fetch_one(
        'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
        ('schema_version',),
    )
    if current is None:
        session.execute(
            'INSERT INTO storage_meta(meta_key, meta_value) VALUES (?, ?)',
            ('schema_version', str(SCHEMA_VERSION)),
        )
    elif int(current['meta_value']) != SCHEMA_VERSION:
        raise RuntimeError('unsupported storage schema version')


__all__ = ['SCHEMA_VERSION', 'initialize_schema']
