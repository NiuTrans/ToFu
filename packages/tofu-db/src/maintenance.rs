//! Low-priority bounded scheduling for authority-local maintenance.
//!
//! The scheduler owns one worker, admits an explicit bounded set of
//! tenant/owner scopes, and attempts at most one already-bounded authority
//! transaction per interval for restored mounts, history, and expired
//! reconstructible tool results. It never waits for the foreground authority
//! mutex: contention always defers maintenance to a later round.

use std::collections::BTreeSet;
use std::io;
use std::sync::{Arc, Condvar, Mutex, TryLockError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::authority::AuthorityDatabase;
use crate::generated_tofudb_ir::{
    LEAN_HISTORY_RETAINED_SEGMENTS, LEAN_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS,
    MAINTENANCE_WORKER_STACK_BYTES, MAX_HISTORY_RETAINED_SEGMENTS,
    MAX_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS, MAX_MAINTENANCE_SCOPES, MAX_MAINTENANCE_WORKERS,
    OBSERVED_HISTORY_RETAINED_SEGMENTS, OBSERVED_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS,
    TOOL_RESULT_PRUNE_ROWS_PER_MAINTENANCE_ROUND,
};
use crate::resource_probe::DaemonResourceBudget;
use crate::turn_search_projection::TurnSearchProjection;

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct MaintenanceScope {
    pub tenant_id: u64,
    pub owner_user_id: u64,
}

impl MaintenanceScope {
    pub fn new(tenant_id: u64, owner_user_id: u64) -> io::Result<Self> {
        if tenant_id == 0 || owner_user_id == 0 {
            return Err(invalid_input("maintenance scope IDs must be positive"));
        }
        Ok(Self {
            tenant_id,
            owner_user_id,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct MaintenanceSchedulerConfig {
    maximum_scopes: usize,
    idle_interval: Duration,
    worker_stack_bytes: usize,
    history_retained_segments: usize,
}

impl MaintenanceSchedulerConfig {
    pub fn from_resource_budget(resource_budget: DaemonResourceBudget) -> Self {
        debug_assert_eq!(MAX_MAINTENANCE_WORKERS, 1);
        let interval_milliseconds = if resource_budget.used_lean_fallback {
            LEAN_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS
        } else {
            OBSERVED_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS
        };
        let history_retained_segments = if resource_budget.used_lean_fallback {
            LEAN_HISTORY_RETAINED_SEGMENTS
        } else {
            OBSERVED_HISTORY_RETAINED_SEGMENTS
        };
        Self {
            maximum_scopes: resource_budget
                .maximum_connections
                .clamp(1, MAX_MAINTENANCE_SCOPES),
            idle_interval: Duration::from_millis(interval_milliseconds),
            worker_stack_bytes: MAINTENANCE_WORKER_STACK_BYTES,
            history_retained_segments,
        }
    }

    fn validate(self) -> io::Result<Self> {
        if self.maximum_scopes == 0 || self.maximum_scopes > MAX_MAINTENANCE_SCOPES {
            return Err(invalid_input("invalid maintenance scope capacity"));
        }
        if self.idle_interval.is_zero()
            || self.idle_interval
                > Duration::from_millis(LEAN_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS)
        {
            return Err(invalid_input("invalid maintenance idle interval"));
        }
        if self.worker_stack_bytes != MAINTENANCE_WORKER_STACK_BYTES {
            return Err(invalid_input("invalid maintenance worker stack size"));
        }
        if self.history_retained_segments == 0
            || self.history_retained_segments > MAX_HISTORY_RETAINED_SEGMENTS
        {
            return Err(invalid_input("invalid retained history segment bound"));
        }
        Ok(self)
    }

    pub fn maximum_scopes(self) -> usize {
        self.maximum_scopes
    }

    pub fn idle_interval(self) -> Duration {
        self.idle_interval
    }

    pub fn worker_stack_bytes(self) -> usize {
        self.worker_stack_bytes
    }

    pub fn history_retained_segments(self) -> usize {
        self.history_retained_segments
    }

    #[cfg(test)]
    fn for_test(idle_interval: Duration, maximum_scopes: usize) -> Self {
        Self {
            maximum_scopes,
            idle_interval,
            worker_stack_bytes: MAINTENANCE_WORKER_STACK_BYTES,
            history_retained_segments: MAX_HISTORY_RETAINED_SEGMENTS,
        }
    }

    #[cfg(test)]
    fn for_history_test(
        idle_interval: Duration,
        maximum_scopes: usize,
        history_retained_segments: usize,
    ) -> Self {
        Self {
            maximum_scopes,
            idle_interval,
            worker_stack_bytes: MAINTENANCE_WORKER_STACK_BYTES,
            history_retained_segments,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct MaintenanceMetrics {
    pub rounds: u64,
    pub foreground_deferrals: u64,
    pub no_work_rounds: u64,
    pub conflict_deferrals: u64,
    pub failed_rounds: u64,
    pub committed_transactions: u64,
    pub completed_mounts: u64,
    pub history_compactions: u64,
    pub retired_history_segments: u64,
    pub retained_history_segments: u32,
    pub materialized_rows: u64,
    pub materialized_bytes: u64,
    pub current_idle_interval_milliseconds: u64,
    pub search_dirty_conversations: u64,
    pub search_rebuilt_conversations: u64,
    pub search_removed_conversations: u64,
    pub search_acknowledged_tokens: u64,
    pub search_source_pages: u64,
    pub search_indexed_turns: u64,
    pub search_skipped_oversized_turns: u64,
    pub search_source_bytes: u64,
    pub search_foreground_deferrals: u64,
    pub search_failed_rounds: u64,
    pub tool_result_artifacts_pruned: u64,
    pub tool_result_prune_backlog_rounds: u64,
}

#[derive(Clone, Debug)]
struct ErrorSnapshot {
    kind: io::ErrorKind,
    message: String,
}

impl ErrorSnapshot {
    fn capture(error: &io::Error) -> Self {
        Self {
            kind: error.kind(),
            message: error.to_string(),
        }
    }

    fn into_error(self) -> io::Error {
        io::Error::new(self.kind, self.message)
    }
}

struct SchedulerState {
    stopping: bool,
    next_scope: usize,
    metrics: MaintenanceMetrics,
    next_idle_interval: Duration,
    terminal_error: Option<ErrorSnapshot>,
}

struct Shared {
    scopes: Vec<MaintenanceScope>,
    config: MaintenanceSchedulerConfig,
    state: Mutex<SchedulerState>,
    wake: Condvar,
}

#[derive(Clone)]
pub struct MaintenanceObserver {
    shared: Arc<Shared>,
}

impl MaintenanceObserver {
    pub fn metrics(&self) -> io::Result<MaintenanceMetrics> {
        self.shared
            .state
            .lock()
            .map(|state| state.metrics)
            .map_err(|_| io::Error::other("maintenance scheduler state is poisoned"))
    }

    pub fn require_healthy(&self) -> io::Result<()> {
        let state = self
            .shared
            .state
            .lock()
            .map_err(|_| io::Error::other("maintenance scheduler state is poisoned"))?;
        state
            .terminal_error
            .clone()
            .map_or(Ok(()), |error| Err(error.into_error()))
    }
}

pub struct MaintenanceScheduler {
    shared: Arc<Shared>,
    worker: Mutex<Option<JoinHandle<()>>>,
}

impl MaintenanceScheduler {
    pub fn start(
        database: Arc<Mutex<AuthorityDatabase>>,
        scopes: Vec<MaintenanceScope>,
        config: MaintenanceSchedulerConfig,
    ) -> io::Result<Self> {
        Self::start_with_search(database, None, scopes, config)
    }

    pub fn start_with_search(
        database: Arc<Mutex<AuthorityDatabase>>,
        search_projection: Option<Arc<Mutex<TurnSearchProjection>>>,
        scopes: Vec<MaintenanceScope>,
        config: MaintenanceSchedulerConfig,
    ) -> io::Result<Self> {
        let config = config.validate()?;
        let scopes = scopes.into_iter().collect::<BTreeSet<_>>();
        if scopes.is_empty() || scopes.len() > config.maximum_scopes {
            return Err(invalid_input("maintenance scopes exceed admission bounds"));
        }
        let shared = Arc::new(Shared {
            scopes: scopes.into_iter().collect(),
            config,
            state: Mutex::new(SchedulerState {
                stopping: false,
                next_scope: 0,
                metrics: MaintenanceMetrics {
                    current_idle_interval_milliseconds: duration_milliseconds(config.idle_interval),
                    ..MaintenanceMetrics::default()
                },
                next_idle_interval: config.idle_interval,
                terminal_error: None,
            }),
            wake: Condvar::new(),
        });
        let worker_shared = Arc::clone(&shared);
        let worker = thread::Builder::new()
            .name("tofu-db-maintenance".to_owned())
            .stack_size(config.worker_stack_bytes)
            .spawn(move || run_worker(&database, search_projection.as_ref(), &worker_shared))?;
        Ok(Self {
            shared,
            worker: Mutex::new(Some(worker)),
        })
    }

    pub fn observer(&self) -> MaintenanceObserver {
        MaintenanceObserver {
            shared: Arc::clone(&self.shared),
        }
    }

    pub fn stop_and_join(&self) -> io::Result<MaintenanceMetrics> {
        {
            let mut state = self
                .shared
                .state
                .lock()
                .map_err(|_| io::Error::other("maintenance scheduler state is poisoned"))?;
            state.stopping = true;
        }
        self.shared.wake.notify_all();
        let worker = self
            .worker
            .lock()
            .map_err(|_| io::Error::other("maintenance scheduler worker is poisoned"))?
            .take();
        if worker.is_some_and(|worker| worker.join().is_err()) {
            return Err(io::Error::other("maintenance scheduler worker panicked"));
        }
        self.observer().require_healthy()?;
        self.observer().metrics()
    }
}

impl Drop for MaintenanceScheduler {
    fn drop(&mut self) {
        if let Ok(mut state) = self.shared.state.lock() {
            state.stopping = true;
        }
        self.shared.wake.notify_all();
        if let Ok(mut worker) = self.worker.lock() {
            if let Some(worker) = worker.take() {
                let _ = worker.join();
            }
        }
    }
}

fn run_worker(
    database: &Arc<Mutex<AuthorityDatabase>>,
    search_projection: Option<&Arc<Mutex<TurnSearchProjection>>>,
    shared: &Shared,
) {
    loop {
        let scope = {
            let mut state = match shared.state.lock() {
                Ok(state) => state,
                Err(_) => return,
            };
            let deadline = Instant::now() + state.next_idle_interval;
            loop {
                if state.stopping {
                    return;
                }
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    break;
                }
                state = match shared.wake.wait_timeout(state, remaining) {
                    Ok((state, _)) => state,
                    Err(_) => return,
                };
            }
            let scope = shared.scopes[state.next_scope];
            state.next_scope = (state.next_scope + 1) % shared.scopes.len();
            state.metrics.rounds = state.metrics.rounds.saturating_add(1);
            scope
        };
        let mut search_did_work = false;
        if let Some(search_projection) = search_projection {
            match crate::turn_search_worker::process_turn_search_batch(
                database,
                search_projection,
                scope,
            ) {
                Ok(search) => {
                    search_did_work = search.dirty_conversations != 0;
                    update_metrics(
                        shared,
                        |metrics| {
                            metrics.search_dirty_conversations = metrics
                                .search_dirty_conversations
                                .saturating_add(search.dirty_conversations);
                            metrics.search_rebuilt_conversations = metrics
                                .search_rebuilt_conversations
                                .saturating_add(search.rebuilt_conversations);
                            metrics.search_removed_conversations = metrics
                                .search_removed_conversations
                                .saturating_add(search.removed_conversations);
                            metrics.search_acknowledged_tokens = metrics
                                .search_acknowledged_tokens
                                .saturating_add(search.acknowledged_tokens);
                            metrics.search_source_pages = metrics
                                .search_source_pages
                                .saturating_add(search.source_pages);
                            metrics.search_indexed_turns = metrics
                                .search_indexed_turns
                                .saturating_add(search.indexed_turns);
                            metrics.search_skipped_oversized_turns = metrics
                                .search_skipped_oversized_turns
                                .saturating_add(search.skipped_oversized_turns);
                            metrics.search_source_bytes = metrics
                                .search_source_bytes
                                .saturating_add(search.source_bytes);
                            metrics.search_foreground_deferrals = metrics
                                .search_foreground_deferrals
                                .saturating_add(search.foreground_deferrals);
                        },
                        Backoff::Keep,
                    )
                }
                Err(_) => update_metrics(
                    shared,
                    |metrics| {
                        metrics.search_failed_rounds =
                            metrics.search_failed_rounds.saturating_add(1);
                    },
                    Backoff::Keep,
                ),
            }
        }
        match run_round(database, scope, shared.config.history_retained_segments) {
            RoundOutcome::ForegroundBusy => update_metrics(
                shared,
                |metrics| {
                    metrics.foreground_deferrals = metrics.foreground_deferrals.saturating_add(1);
                },
                Backoff::Reset,
            ),
            RoundOutcome::NoWork => update_metrics(
                shared,
                |metrics| {
                    metrics.no_work_rounds = metrics.no_work_rounds.saturating_add(1);
                },
                if search_did_work {
                    Backoff::Reset
                } else {
                    Backoff::Increase
                },
            ),
            RoundOutcome::Conflict => update_metrics(
                shared,
                |metrics| {
                    metrics.conflict_deferrals = metrics.conflict_deferrals.saturating_add(1);
                },
                Backoff::Reset,
            ),
            RoundOutcome::Committed {
                rows,
                bytes,
                mount_completed,
                tool_result_artifacts_pruned,
                tool_result_prune_has_more,
            } => update_metrics(
                shared,
                |metrics| {
                    metrics.committed_transactions =
                        metrics.committed_transactions.saturating_add(1);
                    metrics.materialized_rows =
                        metrics.materialized_rows.saturating_add(rows as u64);
                    metrics.materialized_bytes = metrics.materialized_bytes.saturating_add(bytes);
                    if mount_completed {
                        metrics.completed_mounts = metrics.completed_mounts.saturating_add(1);
                    }
                    metrics.tool_result_artifacts_pruned = metrics
                        .tool_result_artifacts_pruned
                        .saturating_add(tool_result_artifacts_pruned as u64);
                    if tool_result_prune_has_more {
                        metrics.tool_result_prune_backlog_rounds =
                            metrics.tool_result_prune_backlog_rounds.saturating_add(1);
                    }
                },
                Backoff::Reset,
            ),
            RoundOutcome::HistoryCompacted {
                retired_segments,
                retained_segments,
            } => update_metrics(
                shared,
                |metrics| {
                    metrics.history_compactions = metrics.history_compactions.saturating_add(1);
                    metrics.retired_history_segments = metrics
                        .retired_history_segments
                        .saturating_add(retired_segments as u64);
                    metrics.retained_history_segments = retained_segments;
                },
                Backoff::Reset,
            ),
            RoundOutcome::Terminal(error) => {
                if let Ok(mut state) = shared.state.lock() {
                    state.metrics.failed_rounds = state.metrics.failed_rounds.saturating_add(1);
                    state.terminal_error = Some(ErrorSnapshot::capture(&error));
                    state.stopping = true;
                }
                shared.wake.notify_all();
                return;
            }
        }
    }
}

enum RoundOutcome {
    ForegroundBusy,
    NoWork,
    Conflict,
    Committed {
        rows: u32,
        bytes: u64,
        mount_completed: bool,
        tool_result_artifacts_pruned: usize,
        tool_result_prune_has_more: bool,
    },
    HistoryCompacted {
        retired_segments: u32,
        retained_segments: u32,
    },
    Terminal(io::Error),
}

fn run_round(
    database: &Arc<Mutex<AuthorityDatabase>>,
    scope: MaintenanceScope,
    history_retained_segments: usize,
) -> RoundOutcome {
    let mut database = match database.try_lock() {
        Ok(database) => database,
        Err(TryLockError::WouldBlock) => return RoundOutcome::ForegroundBusy,
        Err(TryLockError::Poisoned(_)) => {
            return RoundOutcome::Terminal(io::Error::other(
                "maintenance authority mutex is poisoned",
            ));
        }
    };
    let history = database.compact_history_if_checkpointed(history_retained_segments);
    match history {
        Ok(Some(metrics)) => {
            return RoundOutcome::HistoryCompacted {
                retired_segments: metrics.retired_segments,
                retained_segments: metrics.retained_segments,
            };
        }
        Ok(None) => {}
        Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
            return RoundOutcome::Conflict;
        }
        Err(error) => return RoundOutcome::Terminal(error),
    }
    let now_ms = match SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|duration| u64::try_from(duration.as_millis()).ok())
        .filter(|now_ms| *now_ms > 0)
    {
        Some(now_ms) => now_ms,
        None => {
            return RoundOutcome::Terminal(io::Error::other(
                "maintenance wall clock is outside the supported range",
            ));
        }
    };
    let result: io::Result<
        Option<(
            crate::artifact::ToolPruneProgress,
            Option<crate::entity::EntityMountConsolidationProgress>,
        )>,
    > = (|| {
        let mut transaction = database.begin(scope.tenant_id, scope.owner_user_id)?;
        let mount = database.consolidate_one_entity_range_mount(&mut transaction)?;
        if mount.is_some() {
            database.commit(transaction)?;
            return Ok(Some((
                crate::artifact::ToolPruneProgress {
                    deleted: 0,
                    has_more: false,
                },
                mount,
            )));
        }
        let tool_prune = crate::artifact::tool_prune(
            &database,
            &mut transaction,
            now_ms,
            TOOL_RESULT_PRUNE_ROWS_PER_MAINTENANCE_ROUND,
        )?;
        if tool_prune.deleted == 0 {
            return Ok(None);
        }
        database.commit(transaction)?;
        Ok(Some((tool_prune, None)))
    })();
    match result {
        Ok(None) => RoundOutcome::NoWork,
        Ok(Some((tool_prune, mount))) => {
            let (rows, bytes, mount_completed) = match mount {
                Some(progress) => (
                    progress.rows_materialized,
                    progress.materialized_bytes,
                    progress.mount_completed,
                ),
                None => (0, 0, false),
            };
            RoundOutcome::Committed {
                rows,
                bytes,
                mount_completed,
                tool_result_artifacts_pruned: tool_prune.deleted,
                tool_result_prune_has_more: tool_prune.has_more,
            }
        }
        Err(error) if error.kind() == io::ErrorKind::WouldBlock => RoundOutcome::Conflict,
        Err(error) => RoundOutcome::Terminal(error),
    }
}

enum Backoff {
    Keep,
    Reset,
    Increase,
}

fn update_metrics(shared: &Shared, update: impl FnOnce(&mut MaintenanceMetrics), backoff: Backoff) {
    if let Ok(mut state) = shared.state.lock() {
        update(&mut state.metrics);
        state.next_idle_interval = match backoff {
            Backoff::Keep => state.next_idle_interval,
            Backoff::Reset => shared.config.idle_interval,
            Backoff::Increase => increased_idle_interval(state.next_idle_interval),
        };
        state.metrics.current_idle_interval_milliseconds =
            duration_milliseconds(state.next_idle_interval);
    }
}

fn increased_idle_interval(current: Duration) -> Duration {
    current.saturating_mul(2).min(Duration::from_millis(
        MAX_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS,
    ))
}

fn duration_milliseconds(duration: Duration) -> u64 {
    u64::try_from(duration.as_millis()).unwrap_or(u64::MAX)
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::entity::EntityKey;
    use std::time::Instant;

    fn key(index: u16) -> EntityKey {
        scoped_key(7, 11, index)
    }

    fn scoped_key(tenant_id: u64, owner_user_id: u64, index: u16) -> EntityKey {
        EntityKey::new(
            tenant_id,
            owner_user_id,
            "maintenance_test",
            &index.to_be_bytes(),
        )
        .unwrap()
    }

    fn add_mount(
        database: &mut AuthorityDatabase,
        tenant_id: u64,
        owner_user_id: u64,
        pin_id: &[u8],
    ) {
        let mut seed = database.begin(tenant_id, owner_user_id).unwrap();
        for index in 0..4 {
            database
                .entity_put(
                    &mut seed,
                    scoped_key(tenant_id, owner_user_id, index),
                    vec![index as u8; 64],
                )
                .unwrap();
        }
        database.commit(seed).unwrap();

        let ranges = vec![(
            scoped_key(tenant_id, owner_user_id, 0),
            scoped_key(tenant_id, owner_user_id, 10),
        )];
        let mut pin = database.begin(tenant_id, owner_user_id).unwrap();
        database
            .stage_persistent_range_snapshot_pin(&mut pin, pin_id, &ranges)
            .unwrap();
        database.commit(pin).unwrap();
        let mut retire = database.begin(tenant_id, owner_user_id).unwrap();
        database
            .entity_retire_range(&mut retire, &ranges[0].0, &ranges[0].1)
            .unwrap();
        database.commit(retire).unwrap();
        let mut restore = database.begin(tenant_id, owner_user_id).unwrap();
        database
            .stage_persistent_range_snapshot_restore(&mut restore, pin_id, &ranges)
            .unwrap();
        database.commit(restore).unwrap();
    }

    fn authority_with_mount() -> (tempfile::TempDir, Arc<Mutex<AuthorityDatabase>>) {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        add_mount(&mut database, 7, 11, b"mount");
        (directory, Arc::new(Mutex::new(database)))
    }

    fn wait_for(
        observer: &MaintenanceObserver,
        predicate: impl Fn(MaintenanceMetrics) -> bool,
    ) -> MaintenanceMetrics {
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            let metrics = observer.metrics().unwrap();
            if predicate(metrics) {
                return metrics;
            }
            assert!(
                Instant::now() < deadline,
                "maintenance scheduler timed out: {metrics:?}"
            );
            thread::sleep(Duration::from_millis(2));
        }
    }

    #[test]
    fn scheduler_waits_one_interval_and_consolidates_one_bounded_mount() {
        let (_directory, database) = authority_with_mount();
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        assert_eq!(scheduler.observer().metrics().unwrap().rounds, 0);
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.committed_transactions == 1
        });
        assert_eq!(metrics.materialized_rows, 4);
        assert_eq!(metrics.completed_mounts, 1);
        scheduler.stop_and_join().unwrap();

        let database = database.lock().unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        assert_eq!(
            database.entity_get(&mut transaction, &key(3)).unwrap(),
            Some(vec![3; 64])
        );
    }

    #[test]
    fn scheduler_uses_the_single_worker_for_search_without_making_failure_terminal() {
        let authority_directory = tempfile::tempdir().unwrap();
        let projection_directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(authority_directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        crate::search_dirty::mark(
            &database,
            &mut transaction,
            crate::generated_tofudb_ir::CONVERSATION_SEARCH_DIRTY_NAMESPACE,
            "already-deleted",
        )
        .unwrap();
        database.commit(transaction).unwrap();
        let database = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(projection_directory.path(), 1024 * 1024).unwrap(),
        ));
        let scheduler = MaintenanceScheduler::start_with_search(
            Arc::clone(&database),
            Some(projection),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.search_removed_conversations == 1
        });
        assert_eq!(metrics.search_dirty_conversations, 1);
        assert_eq!(metrics.search_acknowledged_tokens, 1);
        assert_eq!(metrics.search_failed_rounds, 0);
        scheduler.stop_and_join().unwrap();
        assert!(crate::search_dirty::list(&database.lock().unwrap(), 7, 11)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn scheduler_prunes_expired_tool_results_in_one_bounded_owner_transaction() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let now_ms = u64::try_from(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_millis(),
        )
        .unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        for content in ["expired-a", "expired-b"] {
            crate::artifact::tool_put(
                &database,
                &mut seed,
                content,
                "text/plain",
                now_ms - 1_000,
                now_ms - 1,
            )
            .unwrap();
        }
        crate::artifact::tool_put(
            &database,
            &mut seed,
            "still-live",
            "text/plain",
            now_ms,
            now_ms + 10_000,
        )
        .unwrap();
        database.commit(seed).unwrap();
        let database = Arc::new(Mutex::new(database));
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.tool_result_artifacts_pruned == 2
        });
        assert_eq!(metrics.committed_transactions, 1);
        assert_eq!(metrics.tool_result_prune_backlog_rounds, 0);
        scheduler.stop_and_join().unwrap();

        let database = database.lock().unwrap();
        let mut verify = database.begin(7, 11).unwrap();
        let progress = crate::artifact::tool_prune(
            &database,
            &mut verify,
            now_ms,
            TOOL_RESULT_PRUNE_ROWS_PER_MAINTENANCE_ROUND,
        )
        .unwrap();
        assert_eq!(progress.deleted, 0);
    }

    #[test]
    fn scheduler_compacts_checkpointed_history_without_checkpointing_foreground_wal() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for index in 0..3_u16 {
            let mut transaction = database.begin(7, 11).unwrap();
            database
                .entity_put(&mut transaction, key(index), vec![index as u8; 64])
                .unwrap();
            database.commit(transaction).unwrap();
            database.checkpoint_for_test().unwrap();
        }
        assert_eq!(database.history_segment_count_for_test(), 3);
        let mut active = database.begin(7, 11).unwrap();
        database
            .entity_put(&mut active, key(9), b"foreground".to_vec())
            .unwrap();
        database.commit(active).unwrap();
        let database = Arc::new(Mutex::new(database));
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_history_test(Duration::from_millis(10), 1, 1),
        )
        .unwrap();
        let before_checkpoint =
            wait_for(&scheduler.observer(), |metrics| metrics.no_work_rounds >= 1);
        assert_eq!(before_checkpoint.history_compactions, 0);
        assert_eq!(database.lock().unwrap().history_segment_count_for_test(), 3);
        database.lock().unwrap().checkpoint_for_test().unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.history_compactions == 1
        });
        assert_eq!(metrics.retired_history_segments, 3);
        assert_eq!(metrics.retained_history_segments, 1);
        assert_eq!(metrics.committed_transactions, 0);
        scheduler.stop_and_join().unwrap();
        assert_eq!(database.lock().unwrap().history_segment_count_for_test(), 1);
    }

    #[test]
    fn foreground_mutex_contention_defers_without_queuing() {
        let (_directory, database) = authority_with_mount();
        let foreground = database.lock().unwrap();
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        let observer = scheduler.observer();
        let deferred = wait_for(&observer, |metrics| metrics.foreground_deferrals > 0);
        assert_eq!(deferred.committed_transactions, 0);
        drop(foreground);
        let committed = wait_for(&observer, |metrics| metrics.committed_transactions == 1);
        assert!(committed.foreground_deferrals > 0);
        scheduler.stop_and_join().unwrap();
    }

    #[test]
    fn scope_and_worker_resources_are_hard_bounded() {
        let (_directory, database) = authority_with_mount();
        assert!(MaintenanceScope::new(0, 1).is_err());
        let lean = MaintenanceSchedulerConfig::from_resource_budget(
            DaemonResourceBudget::from_snapshot(Default::default()),
        );
        assert_eq!(lean.maximum_scopes(), 4);
        assert_eq!(lean.idle_interval(), Duration::from_secs(1));
        assert_eq!(lean.worker_stack_bytes(), 512 * 1024);
        assert_eq!(lean.history_retained_segments(), 16);
        let observed = MaintenanceSchedulerConfig::from_resource_budget(
            DaemonResourceBudget::from_snapshot(crate::resource_probe::LaunchResourceSnapshot {
                logical_cpus: Some(8),
                memory_capacity_bytes: Some(8 * 1024 * 1024 * 1024),
                memory_headroom_bytes: Some(4 * 1024 * 1024 * 1024),
                volume_free_bytes: Some(100 * 1024 * 1024 * 1024),
            }),
        );
        assert_eq!(observed.history_retained_segments(), 64);
        let scopes = (1..=MAX_MAINTENANCE_SCOPES + 1)
            .map(|owner| MaintenanceScope::new(7, owner as u64).unwrap())
            .collect();
        assert!(MaintenanceScheduler::start(
            database,
            scopes,
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), MAX_MAINTENANCE_SCOPES),
        )
        .is_err());
        assert_eq!(MAX_MAINTENANCE_WORKERS, 1);
    }

    #[test]
    fn empty_authority_exponentially_backs_off() {
        let directory = tempfile::tempdir().unwrap();
        let database = Arc::new(Mutex::new(
            AuthorityDatabase::initialize(directory.path()).unwrap(),
        ));
        let scheduler = MaintenanceScheduler::start(
            database,
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| metrics.no_work_rounds >= 2);
        assert_eq!(metrics.current_idle_interval_milliseconds, 40);
        assert_eq!(
            increased_idle_interval(Duration::from_secs(40)),
            Duration::from_secs(60)
        );
        assert_eq!(
            increased_idle_interval(Duration::from_secs(60)),
            Duration::from_secs(60)
        );
        scheduler.stop_and_join().unwrap();
    }

    #[test]
    fn distinct_owner_scopes_are_serviced_round_robin() {
        let directory = tempfile::tempdir().unwrap();
        let mut authority = AuthorityDatabase::initialize(directory.path()).unwrap();
        add_mount(&mut authority, 7, 11, b"owner-11");
        add_mount(&mut authority, 7, 12, b"owner-12");
        let database = Arc::new(Mutex::new(authority));
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![
                MaintenanceScope::new(7, 12).unwrap(),
                MaintenanceScope::new(7, 11).unwrap(),
            ],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 2),
        )
        .unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.completed_mounts == 2
        });
        assert_eq!(metrics.committed_transactions, 2);
        assert_eq!(metrics.materialized_rows, 8);
        scheduler.stop_and_join().unwrap();

        let authority = database.lock().unwrap();
        for owner_user_id in [11, 12] {
            let mut transaction = authority.begin(7, owner_user_id).unwrap();
            assert_eq!(
                authority
                    .entity_get(&mut transaction, &scoped_key(7, owner_user_id, 3))
                    .unwrap(),
                Some(vec![3; 64])
            );
        }
    }

    #[test]
    fn poisoned_authority_is_a_terminal_observable_failure() {
        let directory = tempfile::tempdir().unwrap();
        let database = Arc::new(Mutex::new(
            AuthorityDatabase::initialize(directory.path()).unwrap(),
        ));
        let poison_target = Arc::clone(&database);
        assert!(thread::spawn(move || {
            let _guard = poison_target.lock().unwrap();
            panic!("intentional authority mutex poison");
        })
        .join()
        .is_err());
        let scheduler = MaintenanceScheduler::start(
            database,
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        let observer = scheduler.observer();
        let metrics = wait_for(&observer, |metrics| metrics.failed_rounds == 1);
        assert_eq!(metrics.committed_transactions, 0);
        assert_eq!(
            observer.require_healthy().unwrap_err().kind(),
            io::ErrorKind::Other
        );
        assert_eq!(
            scheduler.stop_and_join().unwrap_err().kind(),
            io::ErrorKind::Other
        );
    }
}
