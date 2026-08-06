"""Database schema initialization — SQLite backend: system domain.

System-domain tables (schema_meta, scheduling, timers, swarm, orchestration,
project brain, optimizer, rate-limit, billing) with indexes and ALTER
migrations. Native SQLite DDL.
"""

from lib.log import get_logger

from lib.database._schema_sqlite._meta import _column_exists, _table_exists

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  System Schema
# ═══════════════════════════════════════════════════════════════════════

def _init_system_schema(conn):
    """Create system domain tables."""
    cur = conn._conn.cursor()

    # pricing_cache + recent_projects: migrated onto Core definitions
    # (lib/database/_core_schema.py). _table_exists guard is REQUIRED on the
    # SQLite path (bare execute, no savepoint tolerance; Core DDL has no
    # IF NOT EXISTS). See tests/test_core_schema_parity.py.
    from lib.database._core_schema import (
        PRICING_CACHE, RECENT_PROJECTS, SCHEMA_META, create_if_absent,
    )
    # schema_meta is core-owned and holds the fast-startup version cache; it
    # MUST exist independently of the (optional) trading domain.
    create_if_absent(conn, SCHEMA_META, table_exists=_table_exists)
    create_if_absent(conn, PRICING_CACHE, table_exists=_table_exists)
    create_if_absent(conn, RECENT_PROJECTS, table_exists=_table_exists)

    # scheduled_tasks + proactive_poll_log: migrated onto Core. The post-create
    # ALTERs below stay (upgrade-only; Core's create only fires on fresh installs).
    from lib.database._core_schema import (
        SCHEDULED_TASKS, PROACTIVE_POLL_LOG, create_if_absent,
    )
    create_if_absent(conn, SCHEDULED_TASKS, table_exists=_table_exists)
    create_if_absent(conn, PROACTIVE_POLL_LOG, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_poll_log_task ON proactive_poll_log(task_id, poll_time DESC)')
    # Migration: predicate-promotion audit columns (tier / predicate_matched /
    # llm_agreed). Pre-existing rows default to 'llm'/-1/-1 → read as pure-LLM
    # polls, so historical audit rows keep their meaning.
    for _ppl_col, _ppl_sql in {
        'tier':              "ALTER TABLE proactive_poll_log ADD COLUMN tier TEXT NOT NULL DEFAULT 'llm'",
        'predicate_matched': "ALTER TABLE proactive_poll_log ADD COLUMN predicate_matched INTEGER NOT NULL DEFAULT -1",
        'llm_agreed':        "ALTER TABLE proactive_poll_log ADD COLUMN llm_agreed INTEGER NOT NULL DEFAULT -1",
    }.items():
        if not _column_exists(conn, 'proactive_poll_log', _ppl_col):
            cur.execute(_ppl_sql)
            logger.info('[DB] Migration: added column %s to proactive_poll_log', _ppl_col)

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
        # Defensive: these are created by the canonical CREATE TABLE above,
        # but older installs may have had a minimal version.
        ('description', "TEXT DEFAULT ''"),
        ('notify_on_failure', "INTEGER NOT NULL DEFAULT 1"),
        ('notify_on_success', "INTEGER NOT NULL DEFAULT 0"),
        ('max_runtime', "INTEGER NOT NULL DEFAULT 300"),
        ('last_result', "TEXT"),
        ('run_count', "INTEGER NOT NULL DEFAULT 0"),
        ('fail_count', "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col_name, col_def in _proactive_cols:
        if not _column_exists(conn, 'scheduled_tasks', col_name):
            cur.execute(f'ALTER TABLE scheduled_tasks ADD COLUMN {col_name} {col_def}')
            logger.info('[DB] Migration: added column %s to scheduled_tasks', col_name)

    # Timer Watcher tables
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
    for _tw_col, _tw_sql in {
        'condition_kind':    "ALTER TABLE timer_watchers ADD COLUMN condition_kind TEXT NOT NULL DEFAULT 'llm'",
        'condition_command': "ALTER TABLE timer_watchers ADD COLUMN condition_command TEXT NOT NULL DEFAULT ''",
        'condition_regex':   "ALTER TABLE timer_watchers ADD COLUMN condition_regex TEXT NOT NULL DEFAULT ''",
        'promotion_streak':  "ALTER TABLE timer_watchers ADD COLUMN promotion_streak INTEGER NOT NULL DEFAULT 0",
        'fallback_streak':   "ALTER TABLE timer_watchers ADD COLUMN fallback_streak INTEGER NOT NULL DEFAULT 0",
        'promoted_at':       "ALTER TABLE timer_watchers ADD COLUMN promoted_at TEXT DEFAULT ''",
        # Provenance marker: pre-existing rows default to 'inline' — every
        # timer created before this column existed WAS a parent-blocking
        # inline timer_create, so the default preserves their true origin and
        # the resume path retires them as orphans (never silent-injects).
        'origin':            "ALTER TABLE timer_watchers ADD COLUMN origin TEXT NOT NULL DEFAULT 'inline'",
    }.items():
        if not _column_exists(conn, 'timer_watchers', _tw_col):
            cur.execute(_tw_sql)
            logger.info('[DB] Migration: added column %s to timer_watchers', _tw_col)
    # Migration: predicate-promotion audit columns on timer_poll_log.
    for _tpl2_col, _tpl2_sql in {
        'tier':              "ALTER TABLE timer_poll_log ADD COLUMN tier TEXT NOT NULL DEFAULT 'llm'",
        'predicate_matched': "ALTER TABLE timer_poll_log ADD COLUMN predicate_matched INTEGER NOT NULL DEFAULT -1",
        'llm_agreed':        "ALTER TABLE timer_poll_log ADD COLUMN llm_agreed INTEGER NOT NULL DEFAULT -1",
    }.items():
        if not _column_exists(conn, 'timer_poll_log', _tpl2_col):
            cur.execute(_tpl2_sql)
            logger.info('[DB] Migration: added column %s to timer_poll_log', _tpl2_col)

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
    # A run instance is a durable, reopenable execution of a flow TEMPLATE.
    # Unlike the in-memory TaskRuntime (TTL-purged), these survive restarts:
    #   orchestration_runs        — one row per run; pins a definition SNAPSHOT
    #                               so editing the template never mutates a run.
    #   orchestration_run_events  — append-only mirror of the engine event
    #                               stream for durable cursor replay. seq is
    #                               monotonic per run (matches TaskRuntime seq).
    # 'paused' is an instance-only status (blocked on a human gate); the engine
    # thread's TaskRuntime status stays 'running' while it waits.
    # orchestration_runs + orchestration_run_events: migrated onto Core.
    from lib.database._core_schema import (
        ORCHESTRATION_RUNS, ORCHESTRATION_RUN_EVENTS, create_if_absent,
    )
    create_if_absent(conn, ORCHESTRATION_RUNS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_orch_runs_status ON orchestration_runs(status, updated_at DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_orch_runs_orch ON orchestration_runs(orch_id, created_at DESC)')
    create_if_absent(conn, ORCHESTRATION_RUN_EVENTS, table_exists=_table_exists)

    # ── Project Events: cross-conversation activity feed ("project brain"
    #    pulse). Append-only, keyed on project_path; seq is monotonic per
    #    project (matches TaskRuntime/orchestration seq). See
    #    lib/conversations/project_feed.py. ──
    from lib.database._core_schema import PROJECT_EVENTS, PROJECT_CHARTER, PROJECT_TASKS, create_if_absent
    create_if_absent(conn, PROJECT_EVENTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_project_events_path_ts ON project_events(project_path, ts DESC)')
    # Project Charter: the north-star doc (Pillar #2). One row per project_path.
    create_if_absent(conn, PROJECT_CHARTER, table_exists=_table_exists)
    # Project Board: coordination tasks (Pillar #3). Per project_path.
    create_if_absent(conn, PROJECT_TASKS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_project_tasks_path_status ON project_tasks(project_path, status)')
    # Migration: dispatched flag (brain-dispatched claim badge). Added 2026-07.
    if not _column_exists(conn, 'project_tasks', 'dispatched'):
        cur.execute('ALTER TABLE project_tasks ADD COLUMN dispatched INTEGER NOT NULL DEFAULT 0')
    # Migration: kind (epic|lease). Pre-existing rows are epics — the DEFAULT
    # 'epic' makes every old row read as a dispatchable epic (never silently
    # dropped off the board). Added 2026-07.
    if not _column_exists(conn, 'project_tasks', 'kind'):
        cur.execute("ALTER TABLE project_tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'epic'")
    # Migration: block-cooldown columns (self-expiring escalating backoff, NOT
    # the removed park shelf). Pre-existing rows default to 0/'' → read as
    # never-blocked, so an old epic stays immediately dispatchable. Added 2026-07.
    if not _column_exists(conn, 'project_tasks', 'blocked_until'):
        cur.execute('ALTER TABLE project_tasks ADD COLUMN blocked_until INTEGER NOT NULL DEFAULT 0')
    if not _column_exists(conn, 'project_tasks', 'block_count'):
        cur.execute('ALTER TABLE project_tasks ADD COLUMN block_count INTEGER NOT NULL DEFAULT 0')
    if not _column_exists(conn, 'project_tasks', 'block_reason'):
        cur.execute("ALTER TABLE project_tasks ADD COLUMN block_reason TEXT NOT NULL DEFAULT ''")
    # Migration: wait-on-path commit-dependency column (Pillar #3). Pre-existing
    # rows default to '[]' → no wait, so an old epic stays dispatchable. The
    # wait resolves against live lease rows at read time (self-expiring, no
    # reaper). Added 2026-07. See docs/PROJECT_BRAIN_WAIT_ON_PATH.md.
    if not _column_exists(conn, 'project_tasks', 'wait_paths'):
        cur.execute("ALTER TABLE project_tasks ADD COLUMN wait_paths TEXT NOT NULL DEFAULT '[]'")
    # Migration: idle-sibling migration routing column (Pillar #5). Pre-existing
    # rows default to '' → route to created_by_conv (unchanged). Added 2026-07.
    # See docs/PROJECT_BRAIN_MIGRATION.md.
    if not _column_exists(conn, 'project_tasks', 'dispatch_target'):
        cur.execute("ALTER TABLE project_tasks ADD COLUMN dispatch_target TEXT NOT NULL DEFAULT ''")
    # Migration: dispatch-time write-set partitioning (Pillar #3 / worktree
    # isolation §4). Pre-existing rows default to '[]' → unknown footprint →
    # treated as non-conflicting, so an old epic stays dispatchable. Added
    # 2026-07. See docs/PROJECT_BRAIN_WORKTREE_ISOLATION.md §4.
    if not _column_exists(conn, 'project_tasks', 'write_set'):
        cur.execute("ALTER TABLE project_tasks ADD COLUMN write_set TEXT NOT NULL DEFAULT '[]'")
    # Migration: structured human-gate columns (Pillar #3). Pre-existing rows
    # default to '' → no pending question → dispatchable exactly as before.
    # Added 2026-07. See project_board.py::answer_task.
    if not _column_exists(conn, 'project_tasks', 'block_question'):
        cur.execute("ALTER TABLE project_tasks ADD COLUMN block_question TEXT NOT NULL DEFAULT ''")
    if not _column_exists(conn, 'project_tasks', 'human_answer'):
        cur.execute("ALTER TABLE project_tasks ADD COLUMN human_answer TEXT NOT NULL DEFAULT ''")
    # Migration: block PROVENANCE column (which conversation raised the block).
    # Pre-existing rows default to '' → the attention surface falls back to
    # created_by_conv for display. Added 2026-08.
    if not _column_exists(conn, 'project_tasks', 'blocked_by'):
        cur.execute("ALTER TABLE project_tasks ADD COLUMN blocked_by TEXT NOT NULL DEFAULT ''")
    # Migration: the shelving/park mechanism was removed (the project pushes
    # every open epic forward at full speed). Any epic left in the retired
    # 'deferred' status is revived to 'open' so it dispatches again. Idempotent.
    cur.execute("UPDATE project_tasks SET status='open', owner_conv_id='', "
                'lease_expires_at=0, dispatched=0 WHERE status=\'deferred\'')
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
    # NOTE (2026-07-30): the promoted_text / promoted_at columns added here in
    # 2026-07 were DROPPED from the Core schema. They backed the three-state
    # goal promotion verdict, and a goal is no longer copied into the charter at
    # all (it injects directly from this lane), so divergence — the only thing
    # those columns could diagnose — is not a reachable state.
    #
    # Deliberately NOT issuing a DROP COLUMN: on an already-migrated database
    # the two columns are harmless (NOT NULL with defaults, and no code reads or
    # writes them), whereas SQLite's DROP COLUMN is refused outright on a column
    # that any index or view references and would turn a cosmetic tidy-up into a
    # startup failure. Leaving them costs nothing; a future consolidating
    # migration can reap them.
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
    # See the matching block in _schema_pg.py for design notes.
    # rate_limit_events: migrated onto Core.
    from lib.database._core_schema import RATE_LIMIT_EVENTS, create_if_absent
    create_if_absent(conn, RATE_LIMIT_EVENTS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_rate_limit_lookup ON rate_limit_events(endpoint, ip, ts_ms)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_rate_limit_ts ON rate_limit_events(ts_ms)')

    # ── Log fingerprint rollup (epic pt_71eaaa8d5b8243e9) ──
    # Mirror of the PG block — error.log 的去重层;lib/log_aggregates.py 的
    # flusher 写,只读端点读。
    from lib.database._core_schema import LOG_AGGREGATES, create_if_absent
    create_if_absent(conn, LOG_AGGREGATES, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_log_aggregates_last_seen ON log_aggregates(last_seen)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_log_aggregates_count ON log_aggregates(count)')

    # ─────────────────────────────────────────────────────────────────
    #  Billing / multi-tenant tables
    # ─────────────────────────────────────────────────────────────────
    # All amounts are stored in MICRO-CREDITS (1 credit = 1,000,000 micro)
    # to keep the math integer-only. 1 credit roughly equals US $0.001
    # — i.e. one US dollar = 1,000 credits = 1,000,000,000 micro. This
    # buys ~10⁹ resolution per dollar which is plenty for token billing.
    # Conversion to display currency happens only at presentation layer.
    # ``tenant_users`` is the multi-tenant relay user table. The existing
    # ``users`` table from the chat schema is a single-row stub for the
    # local UI's session — different shape, different purpose. Naming
    # collision avoided here on purpose.
    # tenant_users: migrated onto Core.
    from lib.database._core_schema import TENANT_USERS, create_if_absent
    create_if_absent(conn, TENANT_USERS, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_tenant_users_email ON tenant_users(email)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_tenant_users_role ON tenant_users(role)')

    # The ledger is the SOURCE OF TRUTH for every credit movement.
    # Append-only — never UPDATE or DELETE rows. The wallet balance is a
    # denormalized cache derived from SUM(amount) over this table.
    # billing_ledger: migrated onto Core.
    from lib.database._core_schema import BILLING_LEDGER, create_if_absent
    create_if_absent(conn, BILLING_LEDGER, table_exists=_table_exists)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ledger_user_ts ON billing_ledger(user_id, ts DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ledger_ref ON billing_ledger(ref_type, ref_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ledger_kind ON billing_ledger(kind)')
    # Partial index serving the reserve-reclaim janitor's GROUP BY (user_id,
    # ref_id) over only reserve-type rows — keeps the 5-minute sweep off a
    # full scan of the append-only, ever-growing ledger.
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

    # Migration for existing api_keys store: add user_id column so a
    # multi-tenant deployment can attribute every key to a wallet.
    # Personal/private installs leave it empty — middleware treats
    # an empty user_id as "the local single-user account".
    if not _column_exists(conn, 'rate_limit_events', 'placeholder'):
        # The api_keys table doesn't live in SQLite — it's still a JSON
        # store at data/config/api_keys.json. The user_id field is added
        # to that JSON shape directly in lib/api_keys.py. Nothing to do
        # here; this comment prevents a future maintainer from re-adding
        # ALTER TABLE api_keys.
        pass

    conn._conn.commit()
