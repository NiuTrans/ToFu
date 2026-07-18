"""System domain schema bootstrap — PostgreSQL backend.

``_init_system_schema`` creates the system-domain Core tables (pricing cache,
scheduler, timers, swarm, orchestration, project brain, optimizer, rate-limit,
billing) plus their PG-only indexes and ALTER migrations. It also creates the
core-owned ``schema_meta`` table the version/domain cache writes into.
"""

from lib.log import get_logger

from lib.database._schema_pg._meta import _column_exists, _table_exists

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  System Schema
# ═══════════════════════════════════════════════════════════════════════

def _init_system_schema(conn):
    """Create system domain tables."""
    cur = conn._conn.cursor()

    # pricing_cache + recent_projects: migrated onto Core definitions
    # (lib/database/_core_schema.py). Parity-verified byte-equivalent to the
    # former hand-DDL; guarded creates are no-ops on existing DBs. See
    # tests/test_core_schema_parity.py.
    from lib.database._core_schema import (
        PRICING_CACHE, RECENT_PROJECTS, SCHEMA_META, create_if_absent,
    )
    # schema_meta is core-owned and holds the fast-startup version cache; it
    # MUST exist independently of the (optional) trading domain.
    create_if_absent(conn, SCHEMA_META, table_exists=_table_exists)
    create_if_absent(conn, PRICING_CACHE, table_exists=_table_exists)
    create_if_absent(conn, RECENT_PROJECTS, table_exists=_table_exists)

    # error_resolutions (PG-only): migrated onto Core.
    from lib.database._core_schema import ERROR_RESOLUTIONS, create_if_absent
    create_if_absent(conn, ERROR_RESOLUTIONS, table_exists=_table_exists)

    # scheduled_tasks + proactive_poll_log: migrated onto Core. The post-create
    # ALTERs below stay (upgrade-only; Core's create only fires on fresh installs).
    from lib.database._core_schema import (
        SCHEDULED_TASKS, PROACTIVE_POLL_LOG, create_if_absent,
    )
    create_if_absent(conn, SCHEDULED_TASKS, table_exists=_table_exists)
    create_if_absent(conn, PROACTIVE_POLL_LOG, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_poll_log_task ON proactive_poll_log(task_id, poll_time DESC)')
    # Migration: predicate-promotion audit columns (tier / predicate_matched /
    # llm_agreed). Pre-existing rows default to 'llm'/-1/-1 → read as pure-LLM.
    cur.execute("ALTER TABLE proactive_poll_log ADD COLUMN IF NOT EXISTS tier TEXT NOT NULL DEFAULT 'llm'")
    cur.execute('ALTER TABLE proactive_poll_log ADD COLUMN IF NOT EXISTS predicate_matched INTEGER NOT NULL DEFAULT -1')
    cur.execute('ALTER TABLE proactive_poll_log ADD COLUMN IF NOT EXISTS llm_agreed INTEGER NOT NULL DEFAULT -1')

    # ── Migration: add proactive agent columns ──
    _proactive_cols = [
        ('target_conv_id', "TEXT DEFAULT ''"),
        ('source_conv_id', "TEXT DEFAULT ''"),
        ('tools_config', "TEXT DEFAULT '{}'"),
        ('poll_count', "INTEGER NOT NULL DEFAULT 0"),
        ('last_poll_at', "TEXT DEFAULT ''"),
        ('last_poll_decision', "TEXT DEFAULT ''"),
        ('last_poll_reason', "TEXT DEFAULT ''"),
        ('last_execution_at', "TEXT DEFAULT ''"),
        ('last_execution_task_id', "TEXT DEFAULT ''"),
        ('last_execution_status', "TEXT DEFAULT ''"),
        ('execution_count', "INTEGER NOT NULL DEFAULT 0"),
        ('max_executions', "INTEGER NOT NULL DEFAULT 0"),
        ('expires_at', "TEXT DEFAULT ''"),
        # Predicate-promotion columns (scheduler condition paradigm).
        ('condition_kind', "TEXT NOT NULL DEFAULT 'llm'"),
        ('condition_command', "TEXT NOT NULL DEFAULT ''"),
        ('condition_regex', "TEXT NOT NULL DEFAULT ''"),
        ('promotion_streak', "INTEGER NOT NULL DEFAULT 0"),
        ('fallback_streak', "INTEGER NOT NULL DEFAULT 0"),
        ('promoted_at', "TEXT DEFAULT ''"),
        # ── Older installs created scheduled_tasks via a minimal CREATE
        #    TABLE in lib/scheduler/manager.py::_init_table; the columns
        #    below need to exist for create_task() to succeed. ──
        ('description', "TEXT DEFAULT ''"),
        ('notify_on_failure', "BOOLEAN NOT NULL DEFAULT TRUE"),
        ('notify_on_success', "BOOLEAN NOT NULL DEFAULT FALSE"),
        ('max_runtime', "INTEGER NOT NULL DEFAULT 300"),
        ('last_result', "TEXT"),
        ('run_count', "INTEGER NOT NULL DEFAULT 0"),
        ('fail_count', "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col_name, col_def in _proactive_cols:
        if not _column_exists(conn, 'scheduled_tasks', col_name):
            cur.execute(f'ALTER TABLE scheduled_tasks ADD COLUMN {col_name} {col_def}')
            logger.info('[DB] Migration: added column %s to scheduled_tasks', col_name)

    # ── Timer Watcher tables ──
    # timer_watchers + timer_poll_log: migrated onto Core.
    from lib.database._core_schema import (
        TIMER_WATCHERS, TIMER_POLL_LOG, create_if_absent,
    )
    create_if_absent(conn, TIMER_WATCHERS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_timer_status ON timer_watchers(status)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_timer_conv ON timer_watchers(conv_id)')
    create_if_absent(conn, TIMER_POLL_LOG, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_timer_poll_log ON timer_poll_log(timer_id, poll_time DESC)')
    # Migration (v25): record which model the poll LLM resolved to.
    if not _column_exists(conn, 'timer_poll_log', 'model'):
        cur.execute("ALTER TABLE timer_poll_log ADD COLUMN model TEXT NOT NULL DEFAULT ''")
        logger.info('[DB] Migration: added column model to timer_poll_log')
    # Migration (v27): stable per-poll id + the raw LLM output (so a
    # parse-failure poll can be located by id and its raw decision text
    # survives refresh/restart for diagnosis).
    for _tpl_col, _tpl_sql in {
        'poll_id':    "ALTER TABLE timer_poll_log ADD COLUMN poll_id TEXT NOT NULL DEFAULT ''",
        'raw_output': "ALTER TABLE timer_poll_log ADD COLUMN raw_output TEXT NOT NULL DEFAULT ''",
    }.items():
        if not _column_exists(conn, 'timer_poll_log', _tpl_col):
            cur.execute(_tpl_sql)
            logger.info('[DB] Migration: added column %s to timer_poll_log', _tpl_col)
    # Migration: predicate-promotion columns on timer_watchers. Pre-existing
    # rows default to condition_kind='llm' → unchanged pure-LLM behaviour.
    cur.execute("ALTER TABLE timer_watchers ADD COLUMN IF NOT EXISTS condition_kind TEXT NOT NULL DEFAULT 'llm'")
    cur.execute("ALTER TABLE timer_watchers ADD COLUMN IF NOT EXISTS condition_command TEXT NOT NULL DEFAULT ''")
    cur.execute("ALTER TABLE timer_watchers ADD COLUMN IF NOT EXISTS condition_regex TEXT NOT NULL DEFAULT ''")
    cur.execute('ALTER TABLE timer_watchers ADD COLUMN IF NOT EXISTS promotion_streak INTEGER NOT NULL DEFAULT 0')
    cur.execute('ALTER TABLE timer_watchers ADD COLUMN IF NOT EXISTS fallback_streak INTEGER NOT NULL DEFAULT 0')
    cur.execute("ALTER TABLE timer_watchers ADD COLUMN IF NOT EXISTS promoted_at TEXT DEFAULT ''")
    # Provenance marker: pre-existing rows default to 'inline' — every timer
    # created before this column existed WAS a parent-blocking inline
    # timer_create, so the default preserves their true origin and the resume
    # path retires them as orphans (never silent-injects a follow-up turn).
    cur.execute("ALTER TABLE timer_watchers ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'inline'")
    # Migration: predicate-promotion audit columns on timer_poll_log.
    cur.execute("ALTER TABLE timer_poll_log ADD COLUMN IF NOT EXISTS tier TEXT NOT NULL DEFAULT 'llm'")
    cur.execute('ALTER TABLE timer_poll_log ADD COLUMN IF NOT EXISTS predicate_matched INTEGER NOT NULL DEFAULT -1')
    cur.execute('ALTER TABLE timer_poll_log ADD COLUMN IF NOT EXISTS llm_agreed INTEGER NOT NULL DEFAULT -1')

    # ── Swarm durable state (see lib/swarm/persistence.py) ──
    # Persists conversation-scoped swarm sessions and per-agent message
    # checkpoints so an in-flight sub-agent can be rehydrated and resumed
    # at round granularity after a server restart. swarm_key == convId.
    # swarm_sessions + swarm_agents: migrated onto Core.
    from lib.database._core_schema import (
        SWARM_SESSIONS, SWARM_AGENTS, create_if_absent,
    )
    create_if_absent(conn, SWARM_SESSIONS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_swarm_sessions_status ON swarm_sessions(status)')
    create_if_absent(conn, SWARM_AGENTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_swarm_agents_key ON swarm_agents(swarm_key)')

    # ── Orchestration run instances (see lib/orchestration_runs.py) ──
    # Mirror of the SQLite block — see ``lib/database/_schema_sqlite.py`` for
    # the template-vs-instance design notes. A run pins a definition snapshot;
    # orchestration_run_events is the durable cursor-replay event log.
    # orchestration_runs + orchestration_run_events: migrated onto Core.
    from lib.database._core_schema import (
        ORCHESTRATION_RUNS, ORCHESTRATION_RUN_EVENTS, create_if_absent,
    )
    create_if_absent(conn, ORCHESTRATION_RUNS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_orch_runs_status ON orchestration_runs(status, updated_at DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_orch_runs_orch ON orchestration_runs(orch_id, created_at DESC)')
    create_if_absent(conn, ORCHESTRATION_RUN_EVENTS, table_exists=_table_exists)

    # ── Project Events: cross-conversation activity feed ("project brain"
    #    pulse). Mirror of the SQLite block. Append-only, keyed on
    #    project_path; seq monotonic per project. See
    #    lib/conversations/project_feed.py. ──
    from lib.database._core_schema import PROJECT_EVENTS, PROJECT_CHARTER, PROJECT_TASKS
    create_if_absent(conn, PROJECT_EVENTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_project_events_path_ts ON project_events(project_path, ts DESC)')
    # Project Charter: the north-star doc (Pillar #2). One row per project_path.
    create_if_absent(conn, PROJECT_CHARTER, table_exists=_table_exists)
    # Project Board: coordination tasks (Pillar #3). Per project_path.
    create_if_absent(conn, PROJECT_TASKS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_project_tasks_path_status ON project_tasks(project_path, status)')
    # Migration: dispatched flag (brain-dispatched claim badge). Added 2026-07.
    cur.execute('ALTER TABLE project_tasks ADD COLUMN IF NOT EXISTS dispatched INTEGER NOT NULL DEFAULT 0')
    # Migration: kind (epic|lease). Pre-existing rows default to 'epic' so an
    # old row always reads as a dispatchable epic. Added 2026-07.
    cur.execute("ALTER TABLE project_tasks ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'epic'")
    # Migration: block-cooldown columns (self-expiring escalating backoff, NOT
    # the removed park shelf). Pre-existing rows default to 0/'' → read as
    # never-blocked, so an old epic stays immediately dispatchable. Added 2026-07.
    cur.execute('ALTER TABLE project_tasks ADD COLUMN IF NOT EXISTS blocked_until BIGINT NOT NULL DEFAULT 0')
    cur.execute('ALTER TABLE project_tasks ADD COLUMN IF NOT EXISTS block_count INTEGER NOT NULL DEFAULT 0')
    cur.execute("ALTER TABLE project_tasks ADD COLUMN IF NOT EXISTS block_reason TEXT NOT NULL DEFAULT ''")
    # Migration: wait-on-path commit-dependency column (Pillar #3). Pre-existing
    # rows default to '[]' → no wait, so an old epic stays dispatchable. The
    # wait resolves against live lease rows at read time (self-expiring, no
    # reaper). Added 2026-07. See docs/PROJECT_BRAIN_WAIT_ON_PATH.md.
    cur.execute("ALTER TABLE project_tasks ADD COLUMN IF NOT EXISTS wait_paths TEXT NOT NULL DEFAULT '[]'")
    # Migration: idle-sibling migration routing column (Pillar #5). Pre-existing
    # rows default to '' → route to created_by_conv (unchanged). Added 2026-07.
    # See docs/PROJECT_BRAIN_MIGRATION.md.
    cur.execute("ALTER TABLE project_tasks ADD COLUMN IF NOT EXISTS dispatch_target TEXT NOT NULL DEFAULT ''")
    # Migration: dispatch-time write-set partitioning (Pillar #3 / worktree
    # isolation §4). Pre-existing rows default to '[]' → unknown footprint →
    # treated as non-conflicting, so an old epic stays dispatchable. Added
    # 2026-07. See docs/PROJECT_BRAIN_WORKTREE_ISOLATION.md §4.
    cur.execute("ALTER TABLE project_tasks ADD COLUMN IF NOT EXISTS write_set TEXT NOT NULL DEFAULT '[]'")
    # Migration: the shelving/park mechanism was removed (the project pushes
    # every open epic forward at full speed). Revive any retired 'deferred'
    # epic to 'open' so it dispatches again. Idempotent.
    cur.execute("UPDATE project_tasks SET status='open', owner_conv_id='', "
                "lease_expires_at=0, dispatched=0 WHERE status='deferred'")
    # Project status snapshots: the human↔brain status lane (Pillar #7).
    # Append-only, keyed on project_path; seq monotonic per project. See
    # lib/conversations/project_status.py.
    from lib.database._core_schema import PROJECT_STATUS_SNAPSHOTS
    create_if_absent(conn, PROJECT_STATUS_SNAPSHOTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_project_status_path_seq ON project_status_snapshots(project_path, seq DESC)')
    # Project watch lane (Pillar #7): human-authored watch items + append-only
    # brain responses. See lib/conversations/project_watch.py.
    from lib.database._core_schema import (
        PROJECT_WATCH_ITEMS, PROJECT_WATCH_RESPONSES)
    create_if_absent(conn, PROJECT_WATCH_ITEMS, table_exists=_table_exists)
    create_if_absent(conn, PROJECT_WATCH_RESPONSES, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_project_watch_items_path ON project_watch_items(project_path, updated_at DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_project_watch_resp_item_seq ON project_watch_responses(item_id, seq DESC)')

    # ── Daily Optimizer tables (see lib/optimizer/) ──
    # optimizer_proposals + optimizer_action_log: migrated onto Core.
    from lib.database._core_schema import (
        OPTIMIZER_PROPOSALS, OPTIMIZER_ACTION_LOG, create_if_absent,
    )
    create_if_absent(conn, OPTIMIZER_PROPOSALS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_opt_prop_created ON optimizer_proposals(created_at DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_opt_prop_status ON optimizer_proposals(status)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_opt_prop_action ON optimizer_proposals(action_type)')
    create_if_absent(conn, OPTIMIZER_ACTION_LOG, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_opt_actlog_proposal ON optimizer_action_log(proposal_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_opt_actlog_applied ON optimizer_action_log(applied_at DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_opt_actlog_expires ON optimizer_action_log(expires_at)')

    # ── Rate-limit event log (PR3c / C7 step 2) ──
    # One row per gated request.  The rate_limit decorator counts rows
    # within the per-endpoint sliding window via SELECT COUNT(*).
    # ts_ms is epoch milliseconds.  An auto-increment ``id`` is the PK
    # so multiple sibling requests in the same millisecond don't trip
    # a uniqueness collision (would have been a real bug under burst
    # traffic).  See lib/rate_limit_store.py.
    # rate_limit_events: migrated onto Core (BIGSERIAL/INTEGER AUTOINCREMENT PK).
    from lib.database._core_schema import RATE_LIMIT_EVENTS, create_if_absent
    create_if_absent(conn, RATE_LIMIT_EVENTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_rate_limit_lookup ON rate_limit_events(endpoint, ip, ts_ms)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_rate_limit_ts ON rate_limit_events(ts_ms)')

    # ─────────────────────────────────────────────────────────────────
    #  Billing / multi-tenant tables (mirror of the SQLite block — see
    #  ``lib/database/_schema_sqlite.py`` for design notes on the
    #  micro-credit unit and ledger-as-source-of-truth invariant).
    # ─────────────────────────────────────────────────────────────────
    # tenant_users: migrated onto Core.
    from lib.database._core_schema import TENANT_USERS, create_if_absent
    create_if_absent(conn, TENANT_USERS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_tenant_users_email ON tenant_users(email)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_tenant_users_role ON tenant_users(role)')

    # billing_ledger: migrated onto Core.
    from lib.database._core_schema import BILLING_LEDGER, create_if_absent
    create_if_absent(conn, BILLING_LEDGER, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ledger_user_ts ON billing_ledger(user_id, ts DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ledger_ref ON billing_ledger(ref_type, ref_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ledger_kind ON billing_ledger(kind)')
    # Partial index serving the reserve-reclaim janitor's GROUP BY (user_id,
    # ref_id) over only reserve-type rows. billing_ledger is append-only and
    # grows forever; without this the 5-minute sweep would seq-scan the whole
    # table. The WHERE clause keeps the index tiny (only reservations).
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ledger_reserve_sweep "
                "ON billing_ledger(user_id, ref_id) WHERE ref_type = 'reserve'")

    # billing_wallets + billing_redeem_codes: migrated onto Core.
    from lib.database._core_schema import (
        BILLING_WALLETS, BILLING_REDEEM_CODES, create_if_absent,
    )
    create_if_absent(conn, BILLING_WALLETS, table_exists=_table_exists)
    create_if_absent(conn, BILLING_REDEEM_CODES, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_redeem_batch ON billing_redeem_codes(batch)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_redeem_redeemed ON billing_redeem_codes(redeemed_by)')

    # billing_payments: migrated onto Core.
    from lib.database._core_schema import BILLING_PAYMENTS, create_if_absent
    create_if_absent(conn, BILLING_PAYMENTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_payments_user ON billing_payments(user_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_payments_provider ON billing_payments(provider, provider_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_payments_status ON billing_payments(status)')

    conn.commit()
