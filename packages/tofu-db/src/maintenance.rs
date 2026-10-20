//! Low-priority bounded scheduling for authority-local maintenance.
//!
//! The scheduler owns one worker, admits an explicit bounded set of
//! tenant/owner scopes, and attempts at most one already-bounded authority
//! transaction per interval for restored mounts, history, and expired
//! reconstructible tool results. Scope rounds never wait for the foreground
//! authority mutex: contention always defers them to a later round. The
//! overlay background drain is the one exception to the no-wait rule: on
//! its own fast poll cadence it takes the mutex in single-chunk slices
//! (`drain_overlay_in_background`), because deferring under continuous load
//! pushed every overlay merge into the foreground commit path (2026-09-21
//! measurement: ~46-58 µs/append amortized; with slices it is ~0.1).

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

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MaintenanceMetrics {
    pub rounds: u64,
    pub foreground_deferrals: u64,
    pub no_work_rounds: u64,
    pub conflict_deferrals: u64,
    pub failed_rounds: u64,
    pub committed_transactions: u64,
    pub completed_mounts: u64,
    pub history_compactions: u64,
    pub published_background_packs: u64,
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
    pub search_retry_deferrals: u64,
    pub search_maximum_failure_backoff_milliseconds: u64,
    pub search_page_budget_exhaustions: u64,
    pub tool_result_artifacts_pruned: u64,
    pub tool_result_prune_backlog_rounds: u64,
    pub attempt_events_pruned: u64,
    pub attempt_event_prune_attempts: u64,
    pub attempt_event_prune_backlog_rounds: u64,
    /// Background overlay drain: wake-ups of the fast poll cadence and the
    /// bounded chunks they committed (see `drain_overlay_in_background`).
    pub overlay_background_wakes: u64,
    pub overlay_background_chunks: u64,
    /// Terminal rounds whose record could not be stored because the
    /// scheduler state lock was poisoned; counted lock-free so the dropout
    /// itself stays observable (waiters are still woken, never stranded).
    pub dropped_terminal_records: u64,
    /// Tombstone compaction (L2): rounds that rewrote pages, the pages they
    /// rewrote and the tombstones they reclaimed
    /// (docs/TOFUDB_TOMBSTONE_COMPACTION.md).
    pub tombstone_compaction_rounds: u64,
    pub tombstone_pages_rewritten: u64,
    pub tombstone_entries_reclaimed: u64,
}

/// Scheduled retention horizon for the cold tier (matches the turn
/// tombstone horizon): settled attempt event chunks and conversation sync
/// events older than this are pruned by maintenance rounds through the
/// certified operator operations (`turn.events.prune`, `turn.sync.prune`).
const PRUNE_RETENTION_MS: u64 = 7 * 24 * 60 * 60 * 1_000;
const MAX_ATTEMPT_EVENT_PRUNE_ROWS_PER_ROUND: usize = 8_192;
const MAX_SYNC_PRUNE_ROWS_PER_ROUND: usize = 8_192;

/// Background overlay drain cadence: the fast poll keeps the overlay near
/// its soft thresholds under continuous load, where the scope round-robin's
/// try_lock almost always defers to the foreground (2026-09-21 swarm
/// measurement: with only the idle rounds draining, every overlay merge
/// landed inside foreground commits at ~46-58 µs/append amortized).
const OVERLAY_BACKGROUND_POLL_INTERVAL: Duration = Duration::from_millis(25);
/// One wake commits at most this many single bounded chunks, releasing the
/// authority mutex between chunks so a foreground commit never waits more
/// than one ~1-3 ms chunk behind the drain.
const OVERLAY_BACKGROUND_MAX_CHUNKS_PER_WAKE: u32 = 4;

/// Bounded background overlay drain: while the overlay sits above its soft
/// thresholds, commit single bounded chunks, releasing the authority mutex
/// between chunks. Returns the chunks committed (0 when below the soft
/// thresholds). The commit-path trigger (`merge_overlay_if_needed`) stays
/// the hard backstop above these soft levels, so a burst the drain cannot
/// keep up with degrades to exactly the pre-background-drain behavior, and
/// drain captures keep the same prefix/version rules as foreground merges;
/// physical page preparation runs outside the authority mutex.
fn drain_overlay_in_background(
    database: &Arc<Mutex<AuthorityDatabase>>,
    trigger: &crate::entity_overlay::EntityOverlaySoftTrigger,
) -> u64 {
    if !trigger.needs_drain() {
        return 0;
    }
    let mut chunks = 0_u64;
    while chunks < u64::from(OVERLAY_BACKGROUND_MAX_CHUNKS_PER_WAKE)
        && !trigger.below_soft_thresholds()
    {
        let outcome = merge_overlay_outside_authority(database);
        match outcome {
            Ok(true) => chunks += 1,
            Ok(false) => break,
            // Rotation backpressure owns the WAL lane briefly; the next
            // poll retries. Anything else is a real merge failure.
            Err(error) if error.kind() == io::ErrorKind::ResourceBusy
                || error.kind() == io::ErrorKind::WouldBlock => break,
            Err(error) => {
                crate::engine_log::warn(
                    "overlay_background_drain",
                    &format!("background overlay merge slice failed: {error}"),
                );
                break;
            }
        }
    }
    chunks
}

/// Capture and publication are short authority holds. The potentially cold
/// page traversal/encoding runs between them with only an immutable pin.
fn merge_overlay_outside_authority(database: &Arc<Mutex<AuthorityDatabase>>) -> io::Result<bool> {
    let prepared = {
        let guard = database.lock().map_err(|_| io::Error::other("maintenance authority mutex is poisoned"))?;
        let _hold = crate::authority_watch::AuthorityHoldTimer::start("overlay.capture");
        guard.prepare_entity_overlay_merge()?
    };
    let Some(prepared) = prepared else { return Ok(false); };
    let encoded = prepared.encode()?;
    let mut guard = database.lock().map_err(|_| io::Error::other("maintenance authority mutex is poisoned"))?;
    let _hold = crate::authority_watch::AuthorityHoldTimer::start("overlay.publish");
    guard.finish_entity_overlay_merge(encoded)
}

#[derive(Clone, Copy, Debug, Default)]
struct ScheduledPruneOutcome {
    events: usize,
    attempts: usize,
    events_has_more: bool,
    sync_rows: usize,
    sync_has_more: bool,
}

fn parse_prune_outcome(bytes: &[u8]) -> (usize, usize, bool) {
    let value = serde_json::from_slice::<serde_json::Value>(bytes).ok();
    let events = value
        .as_ref()
        .and_then(|v| v.get("deleted_rows")?.as_u64())
        .unwrap_or(0) as usize;
    let attempts = value
        .as_ref()
        .and_then(|v| v.get("deleted_attempts")?.as_u64())
        .unwrap_or(0) as usize;
    let remaining = value
        .as_ref()
        .and_then(|v| v.get("remaining")?.as_u64())
        .map(|v| v > 0)
        .unwrap_or(false);
    (events, attempts, remaining)
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
    stop: Arc<std::sync::atomic::AtomicBool>,
    scopes: Vec<MaintenanceScope>,
    config: MaintenanceSchedulerConfig,
    state: Mutex<SchedulerState>,
    wake: Condvar,
    /// Lock-free overlay probe + soft thresholds, resolved best-effort at
    /// startup and lazily by the worker when the authority mutex was
    /// contended — setup never blocks on that mutex (a foreground holder
    /// starting the scheduler must not deadlock). The background drain
    /// runs on its own fast cadence, independent of the scope round-robin.
    overlay_trigger: std::sync::OnceLock<crate::entity_overlay::EntityOverlaySoftTrigger>,
    /// Lock-free count of terminal records dropped to a poisoned state lock;
    /// folded into the metrics snapshot on read.
    dropped_terminal_records: std::sync::atomic::AtomicU64,
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
            .map(|state| {
                let mut metrics = state.metrics;
                metrics.dropped_terminal_records = self
                    .shared
                    .dropped_terminal_records
                    .load(std::sync::atomic::Ordering::Relaxed);
                metrics
            })
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
            stop: Arc::new(std::sync::atomic::AtomicBool::new(false)),
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
            overlay_trigger: std::sync::OnceLock::new(),
            dropped_terminal_records: std::sync::atomic::AtomicU64::new(0),
        });
        // Best-effort probe snapshot: an uncontended startup resolves it
        // here; under contention the worker resolves it on a later poll.
        if let Ok(guard) = database.try_lock() {
            let _ = shared
                .overlay_trigger
                .set(guard.entity_overlay_soft_trigger());
        }
        if let Some(projection) = &search_projection {
            // Install only cancellation metadata, retaining poison as the
            // worker's retryable index failure. Optional search health must
            // not prevent source maintenance or service startup.
            projection.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
                .install_stop(Arc::clone(&shared.stop));
        }
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
        self.shared.stop.store(true, std::sync::atomic::Ordering::Release);
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
        self.shared.stop.store(true, std::sync::atomic::Ordering::Release);
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

/// Per-scope failure feedback for the reconstructible search projection only.
/// The existing launch-derived idle interval and maintenance ceiling bound
/// retries; foreground writes, retention, and overlay drain never wait here.
#[derive(Clone, Copy, Debug, Default)]
struct SearchRetryState {
    retry_at: Option<Instant>,
    delay: Duration,
    consecutive_failures: u32,
}

impl SearchRetryState {
    fn ready(&self, now: Instant) -> bool {
        self.retry_at.is_none_or(|retry_at| now >= retry_at)
    }

    fn failed(&mut self, now: Instant, idle_interval: Duration) -> Duration {
        self.delay = if self.consecutive_failures == 0 {
            idle_interval
        } else {
            increased_idle_interval(self.delay)
        };
        self.consecutive_failures = self.consecutive_failures.saturating_add(1);
        self.retry_at = Some(now + self.delay);
        self.delay
    }

    fn recovered(&mut self) {
        *self = Self::default();
    }
}

fn maximum_search_failure_backoff(states: &[SearchRetryState]) -> u64 {
    states.iter().map(|state| duration_milliseconds(state.delay)).max().unwrap_or(0)
}

fn run_worker(
    database: &Arc<Mutex<AuthorityDatabase>>,
    search_projection: Option<&Arc<Mutex<TurnSearchProjection>>>,
    shared: &Shared,
) {
    crate::fsync_stats::set_current_component(crate::fsync_stats::COMPONENT_MAINTENANCE);
    // The scope-round deadline is recomputed from the (adaptive) idle
    // interval after each round; overlay drain polls interleave on their own
    // fast cadence without resetting it.
    let mut search_pressure = crate::fsync_stats::foreground_hotness_sampler();
    // Exactly one bounded entry per admitted scope; dropped with this worker.
    let mut search_retries = vec![SearchRetryState::default(); shared.scopes.len()];
    // Deferred search activation starts on this already-owned worker, after
    // readiness, without waiting for an unrelated retention idle interval.
    let mut round_deadline = search_projection.map(|_| Instant::now());
    let mut next_overlay_poll = Instant::now();
    loop {
        enum Wake {
            OverlayPoll,
            ScopeRound { index: usize, scope: MaintenanceScope },
        }
        let wake = {
            let mut state = match shared.state.lock() {
                Ok(state) => state,
                Err(_) => return,
            };
            let round_deadline =
                *round_deadline.get_or_insert_with(|| Instant::now() + state.next_idle_interval);
            loop {
                if state.stopping {
                    return;
                }
                let now = Instant::now();
                if now >= next_overlay_poll {
                    break Wake::OverlayPoll;
                }
                if now >= round_deadline {
                    let index = state.next_scope;
                    let scope = shared.scopes[index];
                    state.next_scope = (state.next_scope + 1) % shared.scopes.len();
                    state.metrics.rounds = state.metrics.rounds.saturating_add(1);
                    break Wake::ScopeRound { index, scope };
                }
                let deadline = round_deadline.min(next_overlay_poll);
                let remaining = deadline.saturating_duration_since(now);
                if remaining.is_zero() {
                    // One of the two branches above fires on the re-check.
                    continue;
                }
                state = match shared.wake.wait_timeout(state, remaining) {
                    Ok((state, _)) => state,
                    Err(_) => return,
                };
            }
        };
        let (scope_index, scope) = match wake {
            Wake::OverlayPoll => {
                next_overlay_poll = Instant::now() + OVERLAY_BACKGROUND_POLL_INTERVAL;
                let trigger = match shared.overlay_trigger.get() {
                    Some(trigger) => Some(trigger),
                    None => {
                        // Lazy resolution after a contended startup: one
                        // non-blocking attempt per poll, never queued behind
                        // foreground commits.
                        match database.try_lock() {
                            Ok(guard) => {
                                let built = guard.entity_overlay_soft_trigger();
                                drop(guard);
                                let _ = shared.overlay_trigger.set(built);
                                shared.overlay_trigger.get()
                            }
                            Err(_) => None,
                        }
                    }
                };
                if let Some(trigger) = trigger {
                    let chunks = drain_overlay_in_background(database, trigger);
                    update_metrics(
                        shared,
                        |metrics| {
                            metrics.overlay_background_wakes =
                                metrics.overlay_background_wakes.saturating_add(1);
                            metrics.overlay_background_chunks = metrics
                                .overlay_background_chunks
                                .saturating_add(chunks);
                        },
                        Backoff::Keep,
                    );
                }
                continue;
            }
            Wake::ScopeRound { index, scope } => (index, scope),
        };
        let mut search_did_work = false;
        if let Some(search_projection) = search_projection {
            if !search_retries[scope_index].ready(Instant::now()) {
                update_metrics(shared, |metrics| {
                    metrics.search_retry_deferrals = metrics.search_retry_deferrals.saturating_add(1);
                }, Backoff::Keep);
            } else {
                match crate::turn_search_worker::process_turn_search_batch_with_pressure_and_stop(
                    database,
                    search_projection,
                    scope,
                    &mut search_pressure,
                    || shared.state.lock().map(|state| state.stopping).unwrap_or(true),
                ) {
                    Ok(search) => {
                        search_retries[scope_index].recovered();
                        let maximum_backoff = maximum_search_failure_backoff(&search_retries);
                        search_did_work = search.dirty_conversations != 0;
                        update_metrics(
                            shared,
                            |metrics| {
                                metrics.search_maximum_failure_backoff_milliseconds = maximum_backoff;
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
                                metrics.search_page_budget_exhaustions = metrics
                                    .search_page_budget_exhaustions
                                    .saturating_add(search.page_budget_exhaustions);
                            },
                            Backoff::Keep,
                        )
                    }
                    Err(_) if shared.stop.load(std::sync::atomic::Ordering::Acquire) => return,
                    Err(error) => {
                        let delay = search_retries[scope_index].failed(Instant::now(), shared.config.idle_interval);
                        let failures = search_retries[scope_index].consecutive_failures;
                        if failures.is_power_of_two() {
                            crate::engine_log::warn("search_retry", &format!(
                                "failure_kind={:?} consecutive_failures={} retry_after_ms={}",
                                error.kind(), failures, duration_milliseconds(delay)));
                        }
                        let maximum_backoff = maximum_search_failure_backoff(&search_retries);
                        update_metrics(shared, |metrics| {
                            metrics.search_failed_rounds = metrics.search_failed_rounds.saturating_add(1);
                            metrics.search_maximum_failure_backoff_milliseconds = maximum_backoff;
                        }, Backoff::Keep);
                    }
                }
            }
        }
        if shared.state.lock().map(|state| state.stopping).unwrap_or(true) { return; }
        let recovery_did_work = match crate::project_brain::recovery::backfill(database, scope,
            &|| shared.stop.load(std::sync::atomic::Ordering::Acquire)) {
            Ok(committed) => {
                if committed { update_metrics(shared, |metrics| {
                    metrics.committed_transactions = metrics.committed_transactions.saturating_add(1);
                }, Backoff::Keep); }
                committed
            }
            Err(error) => {
                crate::engine_log::warn("project_recovery_backfill", &format!("summary backfill deferred: {error}"));
                false
            }
        };
        if shared.stop.load(std::sync::atomic::Ordering::Acquire) { return; }
        match run_round(database, scope, shared.config.history_retained_segments) {
            RoundOutcome::ForegroundBusy => update_metrics(
                shared,
                |metrics| {
                    metrics.foreground_deferrals = metrics.foreground_deferrals.saturating_add(1);
                },
                Backoff::Reset,
            ),
            RoundOutcome::OverlayMerged => update_metrics(
                shared,
                |metrics| { metrics.overlay_background_chunks = metrics.overlay_background_chunks.saturating_add(1); },
                Backoff::Reset,
            ),
            RoundOutcome::NoWork => update_metrics(
                shared,
                |metrics| {
                    metrics.no_work_rounds = metrics.no_work_rounds.saturating_add(1);
                },
                if search_did_work || recovery_did_work {
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
                attempt_events_pruned,
                attempt_event_prune_attempts,
                attempt_event_prune_has_more,
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
                    metrics.attempt_events_pruned = metrics
                        .attempt_events_pruned
                        .saturating_add(attempt_events_pruned as u64);
                    metrics.attempt_event_prune_attempts = metrics
                        .attempt_event_prune_attempts
                        .saturating_add(attempt_event_prune_attempts as u64);
                    if attempt_event_prune_has_more {
                        metrics.attempt_event_prune_backlog_rounds = metrics
                            .attempt_event_prune_backlog_rounds
                            .saturating_add(1);
                    }
                },
                Backoff::Reset,
            ),
            RoundOutcome::TombstoneCompacted { pages, entries } => update_metrics(
                shared,
                |metrics| {
                    metrics.tombstone_compaction_rounds =
                        metrics.tombstone_compaction_rounds.saturating_add(1);
                    metrics.tombstone_pages_rewritten =
                        metrics.tombstone_pages_rewritten.saturating_add(pages as u64);
                    metrics.tombstone_entries_reclaimed = metrics
                        .tombstone_entries_reclaimed
                        .saturating_add(entries as u64);
                    metrics.committed_transactions =
                        metrics.committed_transactions.saturating_add(1);
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
            RoundOutcome::PayloadPackPublished => update_metrics(
                shared,
                |metrics| {
                    metrics.published_background_packs =
                        metrics.published_background_packs.saturating_add(1);
                },
                Backoff::Reset,
            ),
            RoundOutcome::Terminal(error) => {
                match shared.state.lock() {
                    Ok(mut state) => {
                        state.metrics.failed_rounds =
                            state.metrics.failed_rounds.saturating_add(1);
                        state.terminal_error = Some(ErrorSnapshot::capture(&error));
                        state.stopping = true;
                    }
                    Err(_) => {
                        // A poisoned state lock must not strand waiters or
                        // silently lose the terminal record: count the
                        // dropout lock-free and still wake everyone below.
                        shared
                            .dropped_terminal_records
                            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    }
                }
                shared.wake.notify_all();
                return;
            }
        }
        // Re-arm the scope-round deadline from the interval the round's
        // backoff updates just selected; overlay polls never touch it.
        round_deadline = shared
            .state
            .lock()
            .map(|state| Instant::now() + state.next_idle_interval)
            .ok();
    }
}

enum RoundOutcome {
    ForegroundBusy,
    NoWork,
    PayloadPackPublished,
    OverlayMerged,
    Conflict,
    Committed {
        rows: u32,
        bytes: u64,
        mount_completed: bool,
        tool_result_artifacts_pruned: usize,
        tool_result_prune_has_more: bool,
        attempt_events_pruned: usize,
        attempt_event_prune_attempts: usize,
        attempt_event_prune_has_more: bool,
    },
    TombstoneCompacted {
        pages: u32,
        entries: u32,
    },
    HistoryCompacted {
        retired_segments: u32,
        retained_segments: u32,
    },
    Terminal(io::Error),
}

// Tombstone compaction (L2, docs/TOFUDB_TOMBSTONE_COMPACTION.md): one hint
// per round, a bounded leaf budget per slice and a bounded slice count per
// round, so catch-up never holds the authority past a user-visible window.
const TOMBSTONE_COMPACTION_LEAVES_PER_SLICE: usize = 32;
const TOMBSTONE_COMPACTION_SLICES_PER_ROUND: usize = 4;

fn run_tombstone_compaction(
    database: &mut AuthorityDatabase,
    scope: MaintenanceScope,
) -> io::Result<Option<(u32, u32)>> {
    // Feature-gated with L1 (`TOFU_DB_TOMBSTONE_RECLAIM`): when reclamation
    // is off the round is a no-op and hints simply age out.
    if !database.entity_tombstone_reclaim_is_enabled() {
        return Ok(None);
    }
    let Some((start, end, dead_leaves)) =
        database.take_entity_tombstone_compaction_hint()?
    else {
        return Ok(None);
    };
    // Hints are entity-key encoded; a scope only vacuums its own owner.
    let owner_prefix = [
        scope.tenant_id.to_be_bytes(),
        scope.owner_user_id.to_be_bytes(),
    ]
    .concat();
    if !start.starts_with(&owner_prefix) {
        // Requeue for the owning scope (or the next scope round-robin):
        // hints are advisory and coalescing, never lost.
        database.record_entity_tombstone_compaction_hint(&start, &end, dead_leaves)?;
        return Ok(None);
    }
    let mut pages = 0_u32;
    let mut entries = 0_u32;
    let mut window = (start, end);
    let mut remaining = true;
    for _ in 0..TOMBSTONE_COMPACTION_SLICES_PER_ROUND {
        let progress = database.reclaim_entity_tombstones_in_range(
            &window.0,
            &window.1,
            TOMBSTONE_COMPACTION_LEAVES_PER_SLICE,
        )?;
        pages = pages.saturating_add(progress.pages_rewritten);
        entries = entries.saturating_add(progress.entries_reclaimed);
        match progress.resume {
            Some(next) => window = next,
            None => {
                remaining = false;
                break;
            }
        }
    }
    if remaining {
        // Partial sweep: hand the remaining window back so a later round
        // resumes instead of restarting.
        database.record_entity_tombstone_compaction_hint(
            &window.0,
            &window.1,
            dead_leaves,
        )?;
    }
    Ok(Some((pages, entries)))
}

fn run_round(
    database: &Arc<Mutex<AuthorityDatabase>>,
    scope: MaintenanceScope,
    history_retained_segments: usize,
) -> RoundOutcome {
    let database_arc = database;
    let mut database = match database.try_lock() {
        Ok(database) => database,
        Err(TryLockError::WouldBlock) => return RoundOutcome::ForegroundBusy,
        Err(TryLockError::Poisoned(_)) => {
            return RoundOutcome::Terminal(io::Error::other(
                "maintenance authority mutex is poisoned",
            ));
        }
    };
    let round_hold = crate::authority_watch::AuthorityHoldTimer::probe("maintenance.round");
    match database.try_prepare_history_compaction(history_retained_segments) {
        Ok(Some(prepared)) => {
            drop(round_hold);
            drop(database);
            let outcome = crate::engine::encode_history_compaction(&prepared);
            // The reserved physical lane must complete even when foreground
            // traffic remains hot. Only metadata publication needs this hold.
            let mut database = match database_arc.lock() {
                Ok(database) => database,
                Err(_) => return RoundOutcome::Terminal(io::Error::other("maintenance authority mutex is poisoned")),
            };
            let _hold = crate::authority_watch::AuthorityHoldTimer::probe("history.install");
            return match database.finish_history_compaction(prepared, outcome) {
                Ok(metrics) => RoundOutcome::HistoryCompacted { retired_segments: metrics.retired_segments,
                    retained_segments: metrics.retained_segments },
                Err(error) => round_engine_error(error),
            };
        }
        Ok(None) => {}
        Err(error) => return round_engine_error(error),
    }
    match database.prepare_entity_overlay_merge() {
        Ok(Some(prepared)) => {
            drop(round_hold);
            drop(database);
            let encoded = match prepared.encode() { Ok(encoded) => encoded, Err(error) => return round_engine_error(error) };
            let mut guard = match database_arc.lock() {
                Ok(guard) => guard,
                Err(_) => return RoundOutcome::Terminal(io::Error::other("maintenance authority mutex is poisoned")),
            };
            let _hold = crate::authority_watch::AuthorityHoldTimer::start("overlay.publish");
            return match guard.finish_entity_overlay_merge(encoded) {
                Ok(true) => RoundOutcome::OverlayMerged,
                Ok(false) => RoundOutcome::NoWork,
                Err(error) => round_engine_error(error),
            };
        }
        Ok(None) => {}
        Err(error) => return round_engine_error(error),
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
            ScheduledPruneOutcome,
            Option<(u32, u32)>,
        )>,
    > = (|| {
        // Tombstone compaction (L2): vacuum one scan-reported dense window
        // per round, bounded so catch-up never wedges the authority.
        if let Some((pages, entries)) = run_tombstone_compaction(&mut database, scope)? {
            return Ok(Some((
                crate::artifact::ToolPruneProgress {
                    deleted: 0,
                    has_more: false,
                },
                None,
                ScheduledPruneOutcome::default(),
                Some((pages, entries)),
            )));
        }
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
                ScheduledPruneOutcome::default(),
                None,
            )));
        }
        let tool_prune = crate::artifact::tool_prune(
            &database,
            &mut transaction,
            now_ms,
            TOOL_RESULT_PRUNE_ROWS_PER_MAINTENANCE_ROUND,
        )?;
        if tool_prune.deleted > 0 {
            database.commit(transaction)?;
        }
        // Cold-tier retention, scheduled through the certified operator
        // prune operations: settled attempts' raw streaming chunks and aged
        // conversation sync events expire after PRUNE_RETENTION_MS. Each
        // prune runs in its own bounded transaction so a large round cannot
        // couple with the tool-result prune above. Sync-referenced attempts
        // are skipped inside events_prune; replay degrades to the snapshot
        // fallback for cursors older than the horizon.
        let event_prune = {
            let mut txn =
                database.begin_with_identity_claim_scopes(scope.tenant_id, scope.owner_user_id)?;
            let outcome = crate::turn::events_prune(
                &database,
                &mut txn,
                &crate::turn::EventPruneRequest {
                    settled_before_ms: now_ms.saturating_sub(PRUNE_RETENTION_MS),
                    max_attempts: 64,
                    max_rows: MAX_ATTEMPT_EVENT_PRUNE_ROWS_PER_ROUND,
                },
            );
            let outcome = match outcome {
                Ok(bytes) => bytes,
                Err(error) if error.kind() == io::ErrorKind::InvalidInput
                    && error.to_string().contains("no durable semantic mutation") =>
                {
                    br#"{"deleted_rows":0,"deleted_attempts":0,"remaining":0}"#.to_vec()
                }
                Err(error) => return Err(error),
            };
            // Cursor-only progress and stale-index removal are durable work
            // even when this bounded slice contains no event payload rows.
            if txn.has_business_mutation() {
                database.commit(txn)?;
            }
            {
                let (events, attempts, has_more) = parse_prune_outcome(&outcome);
                (events, attempts, has_more)
            }
        };
        let sync_prune = {
            let mut txn =
                database.begin_with_identity_claim_scopes(scope.tenant_id, scope.owner_user_id)?;
            let outcome = crate::turn::sync_prune(
                &database,
                &mut txn,
                &crate::turn::SyncPruneRequest {
                    created_before_ms: now_ms.saturating_sub(PRUNE_RETENTION_MS),
                    max_rows: MAX_SYNC_PRUNE_ROWS_PER_ROUND,
                },
            );
            let outcome = match outcome {
                Ok(bytes) => bytes,
                Err(error) if error.kind() == io::ErrorKind::InvalidInput
                    && error.to_string().contains("no durable semantic mutation") =>
                {
                    br#"{"deletedRows":0,"remaining":false}"#.to_vec()
                }
                Err(error) => return Err(error),
            };
            if serde_json::from_slice::<serde_json::Value>(&outcome)
                .ok()
                .and_then(|value| value.get("deletedRows")?.as_u64())
                .unwrap_or(0)
                > 0
            {
                database.commit(txn)?;
            }
            let deleted = serde_json::from_slice::<serde_json::Value>(&outcome)
                .ok()
                .and_then(|value| value.get("deletedRows")?.as_u64())
                .unwrap_or(0) as usize;
            let remaining = serde_json::from_slice::<serde_json::Value>(&outcome)
                .ok()
                .and_then(|value| value.get("remaining")?.as_bool())
                .unwrap_or(false);
            (deleted, remaining)
        };
        if tool_prune.deleted == 0 && event_prune.0 == 0 && sync_prune.0 == 0 {
            return Ok(None);
        }
        Ok(Some((tool_prune, None, ScheduledPruneOutcome {
            events: event_prune.0,
            attempts: event_prune.1,
            events_has_more: event_prune.2,
            sync_rows: sync_prune.0,
            sync_has_more: sync_prune.1,
        }, None)))
    })();
    match result {
        Ok(None) => {
            let preparation = match database.try_prepare_pack_adoption() {
                Ok(preparation) => preparation,
                Err(error) => return round_engine_error(error),
            };
            match preparation {
                crate::engine::PackAdoptionPreparation::Empty => RoundOutcome::NoWork,
                crate::engine::PackAdoptionPreparation::Busy => RoundOutcome::Conflict,
                crate::engine::PackAdoptionPreparation::Ready(mut prepared) => {
                    // The fsync chain (manifest block, WAL cover, CONTROL
                    // chain) runs WITHOUT the authority mutex held; only the
                    // cheap prepare/finish phases hold it, so foreground
                    // commits never wait on a catalog fsync.
                    drop(round_hold);
                    drop(database);
                    let outcome = crate::engine::publish_prepared_pack_adoption(&mut prepared);
                    let mut database = match database_arc.try_lock() {
                        Ok(database) => database,
                        Err(TryLockError::WouldBlock) => match database_arc.lock() {
                            Ok(database) => database,
                            Err(_) => {
                                return RoundOutcome::Terminal(io::Error::other(
                                    "maintenance authority mutex is poisoned",
                                ))
                            }
                        },
                        Err(TryLockError::Poisoned(_)) => {
                            return RoundOutcome::Terminal(io::Error::other(
                                "maintenance authority mutex is poisoned",
                            ))
                        }
                    };
                    let _hold = crate::authority_watch::AuthorityHoldTimer::probe("pack.install");
                    match database.finish_pack_adoption(prepared, outcome) {
                        Ok(true) => RoundOutcome::PayloadPackPublished,
                        Ok(false) => RoundOutcome::NoWork,
                        Err(error) => RoundOutcome::Terminal(error),
                    }
                }
            }
        }
        Ok(Some((tool_prune, mount, event_prune, tombstone_compaction))) => {
            if let Some((pages, entries)) = tombstone_compaction {
                return RoundOutcome::TombstoneCompacted { pages, entries };
            }
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
                attempt_events_pruned: event_prune.events + event_prune.sync_rows,
                attempt_event_prune_attempts: event_prune.attempts,
                attempt_event_prune_has_more: event_prune.events_has_more
                    || event_prune.sync_has_more,
            }
        }
        Err(error) => round_engine_error(error),
    }
}

/// Classify an engine error escaping a maintenance round. Rotation
/// backpressure is transient by design — the rotation driver completes the
/// install and the retried round proceeds — so it defers the round exactly
/// like a `WouldBlock` publication conflict. Recording it as terminal let
/// the listener health gate recycle the whole daemon on one expected
/// backpressure signal (2026-09-22: two daemon exits, one failed round
/// each, inside slow-mount rotation windows).
fn round_engine_error(error: io::Error) -> RoundOutcome {
    if error.kind() == io::ErrorKind::WouldBlock
        || crate::engine::is_rotation_backpressure(&error)
    {
        RoundOutcome::Conflict
    } else {
        RoundOutcome::Terminal(error)
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

    #[test]
    fn search_failure_feedback_bounds_retries_and_preserves_other_scopes() {
        let start = Instant::now();
        let interval = Duration::from_millis(250);
        let mut retries = [SearchRetryState::default(); 2];
        let mut attempts = 0;
        for tick in 0..240_u32 {
            let now = start + interval * tick;
            if retries[0].ready(now) {
                retries[0].failed(now, interval);
                attempts += 1;
            }
            assert!(retries[1].ready(now), "one failing scope cannot delay another");
        }
        assert!(attempts <= 9, "persistent failure must not retry every maintenance round");
        assert_eq!(attempts, 8);
        let previous_delay = retries[0].delay;
        retries[0].recovered();
        assert!(retries[0].ready(start));
        assert_eq!(maximum_search_failure_backoff(&retries), 0);
        assert_eq!(retries[0].failed(start, interval), interval);
        assert!(previous_delay > interval);
        eprintln!("search_retry_state_bytes={} maximum_scopes={} maximum_state_bytes={}",
            std::mem::size_of::<SearchRetryState>(), MAX_MAINTENANCE_SCOPES,
            std::mem::size_of::<SearchRetryState>() * MAX_MAINTENANCE_SCOPES);
    }

    #[test]
    fn search_retry_ceiling_does_not_suspend_repair_forever() {
        let mut retry = SearchRetryState::default();
        let mut now = Instant::now();
        let ceiling = Duration::from_millis(MAX_MAINTENANCE_IDLE_INTERVAL_MILLISECONDS);
        for _ in 0..100 {
            let delay = retry.failed(now, Duration::from_millis(250));
            assert!(delay <= ceiling);
            assert!(!retry.ready(now));
            now += delay;
            assert!(retry.ready(now));
        }
        assert_eq!(retry.delay, ceiling);
    }

    #[test]
    fn search_failure_backoff_preserves_maintenance_and_resumes_after_repair() {
        let (directory, database) = authority_with_mount();
        let projection_directory = tempfile::tempdir().unwrap();
        {
            let guard = database.lock().unwrap();
            let mut transaction = guard.begin(7, 11).unwrap();
            crate::search_dirty::mark(&guard, &mut transaction,
                crate::generated_tofudb_ir::CONVERSATION_SEARCH_DIRTY_NAMESPACE,
                "already-deleted").unwrap();
            drop(guard);
            database.lock().unwrap().commit(transaction).unwrap();
        }
        let projection = Arc::new(Mutex::new(TurnSearchProjection::initialize(
            projection_directory.path(), 1024 * 1024).unwrap()));
        let poisoned = Arc::clone(&projection);
        assert!(thread::spawn(move || {
            let _guard = poisoned.lock().unwrap();
            panic!("injected derived-index failure");
        }).join().is_err());
        let scheduler = MaintenanceScheduler::start_with_search(
            Arc::clone(&database), Some(Arc::clone(&projection)),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        ).unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.search_failed_rounds >= 2 && metrics.search_retry_deferrals > 0
                && metrics.completed_mounts == 1
        });
        assert!(metrics.search_maximum_failure_backoff_milliseconds >= 20);
        scheduler.observer().require_healthy().unwrap();
        projection.clear_poison();
        let metrics = wait_for(&scheduler.observer(), |metrics| metrics.search_removed_conversations == 1);
        assert_eq!(metrics.search_acknowledged_tokens, 1);
        assert_eq!(metrics.search_maximum_failure_backoff_milliseconds, 0);
        scheduler.stop_and_join().unwrap();
        drop(database);
        let reopened = AuthorityDatabase::open(directory.path()).unwrap();
        let mut transaction = reopened.begin(7, 11).unwrap();
        assert_eq!(reopened.entity_get(&mut transaction, &key(3)).unwrap(), Some(vec![3; 64]));
    }

    #[test]
    fn idle_round_publishes_a_completed_pack_without_business_writes() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let payload = vec![0x57; 2 * 1024 * 1024];
        let id = database.commit_block_payload_for_test(&payload).unwrap();
        let store = database.block_store_shared();
        let (segment_id, ids) = store
            .begin_background_pack(1, crate::engine::MAX_CHECKPOINT_MATERIALIZE_BATCH_BYTES)
            .unwrap()
            .unwrap();
        let segment = store
            .payload_segment_store()
            .unwrap()
            .create(segment_id, ids.clone(), |id| store.get(id))
            .unwrap();
        let reference = crate::payload_manifest::PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )
        .unwrap();
        store.verify_payload_segment(&reference).unwrap();
        store.note_packed(&ids).unwrap();
        store
            .complete_background_pack(segment_id, reference)
            .unwrap();
        let state = database.engine_mut_for_test().state().clone();
        let database = Arc::new(Mutex::new(database));
        let foreground = database.lock().unwrap();
        assert!(matches!(
            run_round(&database, MaintenanceScope::new(7, 11).unwrap(), 64),
            RoundOutcome::ForegroundBusy
        ));
        drop(foreground);
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.published_background_packs == 1
        });
        assert_eq!(metrics.committed_transactions, 0);
        scheduler.stop_and_join().unwrap();
        {
            let mut database = database.lock().unwrap();
            let engine = database.engine_mut_for_test();
            assert_eq!(engine.payload_segment_count(), 1);
            assert_eq!(engine.state().durable_sequence, state.durable_sequence);
            assert_eq!(
                engine.state().checkpoint_sequence,
                state.checkpoint_sequence
            );
            assert_eq!(
                engine.state().active_log_generation,
                state.active_log_generation
            );
        }
        assert!(store.pending_payload(id).unwrap().is_none());
        drop(store);
        drop(database);
        let reopened = AuthorityDatabase::open(directory.path()).unwrap();
        assert_eq!(reopened.read_block_for_test(id).unwrap(), payload);
    }

    #[test]
    fn rotation_backpressure_defers_a_round_instead_of_stopping_the_worker() {
        // 2026-09-22: one maintenance prune commit inside a rotation window
        // recorded the transient backpressure as terminal; the listener
        // health gate then recycled the whole daemon (exit 1) on a signal
        // that is retryable by design. Rotation backpressure must classify
        // as a conflict deferral so the worker survives the install window.
        let backpressure = crate::engine::rotation_backpressure();
        assert!(matches!(
            round_engine_error(backpressure),
            RoundOutcome::Conflict
        ));
        let publication_conflict = io::Error::new(io::ErrorKind::WouldBlock, "would block");
        assert!(matches!(
            round_engine_error(publication_conflict),
            RoundOutcome::Conflict
        ));
        let fatal = io::Error::new(io::ErrorKind::InvalidData, "corrupt");
        assert!(matches!(
            round_engine_error(fatal),
            RoundOutcome::Terminal(_)
        ));
        // Only private reservation exhaustion is rewritten by its owner;
        // an unrelated real allocation failure must retain terminal policy.
        assert!(matches!(
            round_engine_error(io::Error::new(io::ErrorKind::OutOfMemory, "physical read allocation failed")),
            RoundOutcome::Terminal(_)
        ));
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

    fn tomb_key(tenant: u64, owner: u64, index: u16) -> EntityKey {
        EntityKey::new(tenant, owner, "tombstone_maintenance", &index.to_be_bytes()).unwrap()
    }

    fn seed_tombstone_dense_region(database: &mut AuthorityDatabase) {
        let mut put = database.begin(7, 11).unwrap();
        for index in 0..40_u16 {
            database
                .entity_put(
                    &mut put,
                    tomb_key(7, 11, index),
                    vec![(index % 251) as u8; 4 * 1024],
                )
                .unwrap();
        }
        database.commit(put).unwrap();
        database.merge_entity_overlay_fully().unwrap();
        let pin_a = database.begin(7, 11).unwrap();
        for chunk_start in (0..40_u16).step_by(4) {
            let mut delete = database.begin(7, 11).unwrap();
            for index in chunk_start..chunk_start + 4 {
                database
                    .entity_delete(&mut delete, tomb_key(7, 11, index))
                    .unwrap();
            }
            database.commit(delete).unwrap();
            database.merge_entity_overlay_fully().unwrap();
        }
        drop(pin_a);
    }

    #[test]
    fn compaction_round_vacuums_a_reported_dense_window() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.set_entity_tombstone_reclaim_override_for_test(Some(true));
        seed_tombstone_dense_region(&mut database);
        // The dense scan reports the window into the hint registry.
        let start = tomb_key(7, 11, 0);
        let end = tomb_key(7, 11, 1000);
        let mut scan_tx = database.begin(7, 11).unwrap();
        let rows = database
            .entity_scan(&mut scan_tx, &start, &end, 1_000)
            .unwrap();
        assert!(rows.is_empty());
        drop(scan_tx);
        let (hint_start, hint_end, count) = database
            .take_entity_tombstone_compaction_hint()
            .unwrap()
            .expect("a dense window must be reported");
        database
            .record_entity_tombstone_compaction_hint(&hint_start, &hint_end, count)
            .unwrap();
        let database = Arc::new(Mutex::new(database));
        let outcome = run_round(&database, MaintenanceScope::new(7, 11).unwrap(), 64);
        let (pages, entries) = match outcome {
            RoundOutcome::TombstoneCompacted { pages, entries } => (pages, entries),
            _ => panic!("expected TombstoneCompacted"),
        };
        assert!(pages > 0 && entries >= 40, "pages={pages} entries={entries}");
        let database = database.lock().unwrap();
        let mut probe = database.begin(7, 11).unwrap();
        for index in 0..40_u16 {
            assert!(database
                .entity_get(&mut probe, &tomb_key(7, 11, index))
                .unwrap()
                .is_none());
        }
    }

    #[test]
    fn compaction_round_requeues_foreign_scope_hints() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.set_entity_tombstone_reclaim_override_for_test(Some(true));
        let start = tomb_key(9, 3, 0);
        let end = tomb_key(9, 3, 1000);
        database
            .record_entity_tombstone_compaction_hint(start.encoded(), end.encoded(), 99)
            .unwrap();
        let database = Arc::new(Mutex::new(database));
        let outcome = run_round(&database, MaintenanceScope::new(7, 11).unwrap(), 64);
        assert!(
            !matches!(outcome, RoundOutcome::TombstoneCompacted { .. }),
            "foreign hints must not be vacuumed by this scope"
        );
        let database = database.lock().unwrap();
        let requeued = database.take_entity_tombstone_compaction_hint().unwrap();
        assert!(requeued.is_some(), "the foreign hint must be requeued");
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

    fn authority_with_expired_attempt_events(
        event_count: usize,
    ) -> (tempfile::TempDir, Arc<Mutex<AuthorityDatabase>>) {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let old_ms = 1_700_000_000_000_u64;
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::turn::append_settled(
            &database,
            &mut seed,
            &crate::turn::AppendSettledRequest {
                conversation_id: "maintenance-conversation".to_owned(),
                actor: "assistant".to_owned(),
                status: "completed".to_owned(),
                projection_json: br#"{"content":"seeded"}"#.to_vec(),
                projection_timestamp: None,
                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: "maintenance-command".to_owned(),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: "maintenance-turn".to_owned(),
                attempt_id: Some("maintenance-attempt".to_owned()),
                created_at_ms: old_ms,
                committed_at_ms: old_ms,
                defaults: crate::conversation_header::TurnDefaults {
                    allow_create: true,
                    title: "Maintenance fixture".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: old_ms,
                },
            },
        )
        .unwrap();
        for index in 0..event_count {
            crate::turn::append_attempt_event(
                &database,
                &mut seed,
                crate::turn::AttemptEventAppend {
                    conversation_id: "maintenance-conversation",
                    turn_id: "maintenance-turn",
                    attempt_id: "maintenance-attempt",
                    projection_revision: 1,
                    event_type: "text-delta",
                    payload: serde_json::Value::String(format!("chunk-{index}")),
                    occurred_at_ms: old_ms,
                    publish_conversation_sync: false,
                },
            )
            .unwrap();
        }
        database.commit(seed).unwrap();
        (directory, Arc::new(Mutex::new(database)))
    }

    #[test]
    fn scheduler_prunes_expired_attempt_events_on_idle_rounds() {
        let (_directory, database) = authority_with_expired_attempt_events(3);
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_test(Duration::from_millis(10), 1),
        )
        .unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.attempt_events_pruned >= 3
        });
        assert_eq!(metrics.attempt_event_prune_attempts, 1);
        scheduler.stop_and_join().unwrap();
    }

    #[test]
    fn idle_round_commits_event_retirement_without_payload_rows() {
        let (directory, database) = authority_with_expired_attempt_events(0);
        let namespace = crate::generated_tofudb_ir::CONVERSATION_EVENT_RETIREMENT_NAMESPACE;
        let (first, end) = EntityKey::prefix_range(7, 11, namespace, b"j").unwrap();
        {
            let mut db = database.lock().unwrap();
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            crate::conversation_header::delete(&mut db, &mut tx,
                &crate::conversation_header::DeleteRequest {
                    conversation_id: "maintenance-conversation".into(), deleted_at_ms: 2_000_000_000_000,
                }).unwrap();
            db.commit(tx).unwrap();
            let mut tx = db.begin(7, 11).unwrap();
            assert_eq!(db.entity_scan(&mut tx, &first, &end, 1).unwrap().len(), 1);
        }
        run_round(&database, MaintenanceScope::new(7, 11).unwrap(), 64);
        drop(database);
        let db = AuthorityDatabase::open(directory.path()).unwrap();
        let mut tx = db.begin(7, 11).unwrap();
        assert!(db.entity_scan(&mut tx, &first, &end, 1).unwrap().is_empty(),
            "maintenance discarded metadata-only retirement progress");
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
    fn scheduler_retires_old_history_while_preserving_foreground_wal() {
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
        let active_generation = database.engine_mut_for_test().state().active_log_generation;
        let checkpoint_sequence = database.engine_mut_for_test().state().checkpoint_sequence;
        let database = Arc::new(Mutex::new(database));
        let scheduler = MaintenanceScheduler::start(
            Arc::clone(&database),
            vec![MaintenanceScope::new(7, 11).unwrap()],
            MaintenanceSchedulerConfig::for_history_test(Duration::from_millis(10), 1, 1),
        )
        .unwrap();
        let metrics = wait_for(&scheduler.observer(), |metrics| {
            metrics.history_compactions == 1
        });
        assert_eq!(metrics.retired_history_segments, 2);
        assert_eq!(metrics.retained_history_segments, 1);
        assert_eq!(metrics.committed_transactions, 0);
        scheduler.stop_and_join().unwrap();
        {
            let mut database = database.lock().unwrap();
            assert_eq!(database.history_segment_count_for_test(), 1);
            let engine = database.engine_mut_for_test();
            assert_eq!(engine.committed_transactions().len(), 1);
            assert_eq!(engine.state().active_log_generation, active_generation);
            assert_eq!(engine.state().checkpoint_sequence, checkpoint_sequence);
        }
        drop(database);
        let reopened = AuthorityDatabase::open(directory.path()).unwrap();
        let mut read = reopened.begin(7, 11).unwrap();
        assert_eq!(
            reopened.entity_get(&mut read, &key(9)).unwrap(),
            Some(b"foreground".to_vec())
        );
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

    fn put_overlay_record(database: &mut AuthorityDatabase, index: u32) {
        let mut transaction = database.begin(7, 11).unwrap();
        database
            .entity_put(
                &mut transaction,
                EntityKey::new(7, 11, "records", &index.to_be_bytes()).unwrap(),
                index.to_be_bytes().to_vec(),
            )
            .unwrap();
        database.commit(transaction).unwrap();
    }

    fn get_overlay_record(database: &AuthorityDatabase, index: u32) -> Option<Vec<u8>> {
        let mut read = database.begin(7, 11).unwrap();
        database
            .entity_get(
                &mut read,
                &EntityKey::new(7, 11, "records", &index.to_be_bytes()).unwrap(),
            )
            .unwrap()
    }

    fn overlay_entries(database: &Arc<Mutex<AuthorityDatabase>>) -> usize {
        database
            .lock()
            .unwrap()
            .entity_overlay_entries_for_test()
    }

    /// Blocks one cold source open so the test observes the real maintenance
    /// helper's authority availability while physical preparation is stalled.
    struct BlockingMergeProbe {
        armed: std::sync::atomic::AtomicBool,
        entered: std::sync::mpsc::Sender<()>,
        release: Mutex<std::sync::mpsc::Receiver<()>>,
    }
    impl BlockingMergeProbe {
        fn pause_read(&self) -> io::Result<()> {
            if self.armed.swap(false, std::sync::atomic::Ordering::AcqRel) {
                self.entered.send(()).map_err(|_| io::Error::other("merge probe receiver closed"))?;
                self.release.lock().unwrap().recv_timeout(Duration::from_secs(10))
                    .map_err(|_| io::Error::other("merge probe release missing"))?;
            }
            Ok(())
        }
    }
    struct BlockingMergeReadVfs {
        inner: Arc<crate::vfs::DeterministicVfs>,
        probe: Arc<BlockingMergeProbe>,
    }
    struct BlockingMergeReadFile {
        inner: Box<dyn crate::vfs::VfsFile>,
        probe: Arc<BlockingMergeProbe>,
    }
    impl crate::vfs::VfsFile for BlockingMergeReadFile {
        fn len(&self) -> io::Result<u64> { self.inner.len() }
        fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> { self.probe.pause_read()?; self.inner.read_all(maximum) }
        fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> { self.probe.pause_read()?; self.inner.read_exact_at(offset, bytes) }
        fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> { self.inner.write_all_at(offset, bytes) }
        fn set_len(&mut self, length: u64) -> io::Result<()> { self.inner.set_len(length) }
        fn sync_data(&mut self) -> io::Result<()> { self.inner.sync_data() }
        fn sync_all(&mut self) -> io::Result<()> { self.inner.sync_all() }
        fn try_lock_exclusive(&mut self) -> io::Result<()> { self.inner.try_lock_exclusive() }
    }

    impl crate::vfs::Vfs for BlockingMergeReadVfs {
        fn open(&self, path: &std::path::Path, request: crate::vfs::OpenRequest) -> io::Result<Box<dyn crate::vfs::VfsFile>> {
            Ok(Box::new(BlockingMergeReadFile { inner: self.inner.open(path, request)?, probe: Arc::clone(&self.probe) }))
        }
        fn create_dir(&self, path: &std::path::Path) -> io::Result<()> { self.inner.create_dir(path) }
        fn rename(&self, source: &std::path::Path, destination: &std::path::Path) -> io::Result<()> { self.inner.rename(source, destination) }
        fn remove_file(&self, path: &std::path::Path) -> io::Result<()> { self.inner.remove_file(path) }
        fn metadata(&self, path: &std::path::Path) -> io::Result<crate::vfs::FileKind> { self.inner.metadata(path) }
        fn read_directory(&self, path: &std::path::Path) -> io::Result<Vec<std::path::PathBuf>> { self.inner.read_directory(path) }
        fn read_directory_bounded(&self, path: &std::path::Path, maximum: usize) -> io::Result<crate::vfs::BoundedDirectoryEntries> { self.inner.read_directory_bounded(path, maximum) }
        fn match_directory_prefix_bounded(&self, path: &std::path::Path, prefix: &str, scanned: usize, matched: usize) -> io::Result<crate::vfs::BoundedDirectoryMatches> { self.inner.match_directory_prefix_bounded(path, prefix, scanned, matched) }
        fn sync_directory(&self, path: &std::path::Path) -> io::Result<()> { self.inner.sync_directory(path) }
    }

    #[test]
    fn stalled_background_merge_read_releases_authority_for_new_commits() {
        use crate::vfs::Vfs;
        let (entered_sender, entered_receiver) = std::sync::mpsc::channel();
        let (release_sender, release_receiver) = std::sync::mpsc::channel();
        let vfs = Arc::new(BlockingMergeReadVfs { inner: Arc::new(crate::vfs::DeterministicVfs::new(None)),
            probe: Arc::new(BlockingMergeProbe { armed: std::sync::atomic::AtomicBool::new(false), entered: entered_sender, release: Mutex::new(release_receiver) }) });
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let source: Arc<dyn Vfs> = vfs.clone();
        let mut database = AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), source).unwrap();
        database.set_write_overlay_for_test(true);
        for index in 0..20 { put_overlay_record(&mut database, index); }
        database.merge_entity_overlay_fully().unwrap();
        database.checkpoint_wal_window().unwrap();
        put_overlay_record(&mut database, 100);
        database.configure_hot_data_caches(0, 0).unwrap();
        let database = Arc::new(Mutex::new(database));
        vfs.probe.armed.store(true, std::sync::atomic::Ordering::Release);
        let worker_database = Arc::clone(&database);
        let worker = std::thread::spawn(move || merge_overlay_outside_authority(&worker_database));
        entered_receiver.recv_timeout(Duration::from_secs(5)).expect("merge must reach the cold physical source read");
        {
            let mut foreground = database.try_lock().expect("cold merge read must not hold authority");
            put_overlay_record(&mut foreground, 101);
            assert_eq!(get_overlay_record(&foreground, 101), Some(101_u32.to_be_bytes().to_vec()));
        }
        release_sender.send(()).unwrap();
        assert!(worker.join().unwrap().unwrap());
        let guard = database.lock().unwrap();
        assert_eq!(get_overlay_record(&guard, 100), Some(100_u32.to_be_bytes().to_vec()));
        assert_eq!(get_overlay_record(&guard, 101), Some(101_u32.to_be_bytes().to_vec()));
        assert_eq!(guard.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
    }

    #[test]
    fn background_drain_commits_bounded_chunks_below_the_hard_trigger() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_for_test(true);
        // 5,000 entries stay below every commit-path trigger (12,000 scan
        // entries, 8 MiB budget, 48 MiB WAL watermark), so only the
        // background drain can merge them.
        for index in 0..5_000_u32 {
            put_overlay_record(&mut database, index);
        }
        let database = Arc::new(Mutex::new(database));
        assert_eq!(overlay_entries(&database), 5_000);
        let trigger = database.lock().unwrap().entity_overlay_soft_trigger();
        assert!(trigger.needs_drain());
        // Cold slices start small, then measured cost determines growth.
        // Every wake remains bounded and every nonempty wake makes progress.
        let mut remaining = 5_000;
        for _ in 0..remaining {
            if trigger.below_soft_thresholds() { break; }
            let chunks = drain_overlay_in_background(&database, &trigger);
            assert!((1..=u64::from(OVERLAY_BACKGROUND_MAX_CHUNKS_PER_WAKE)).contains(&chunks));
            let next = overlay_entries(&database);
            assert!(next < remaining);
            remaining = next;
        }
        assert!(trigger.below_soft_thresholds());
        // The retained entries resolve exactly through the overlay, and the
        // next wake finds nothing to do.
        assert_eq!(drain_overlay_in_background(&database, &trigger), 0);
        let guard = database.lock().unwrap();
        for index in [0_u32, 1, 2_048, 4_999] {
            assert_eq!(
                get_overlay_record(&guard, index),
                Some(index.to_be_bytes().to_vec()),
            );
        }
    }

    #[test]
    fn background_drain_bounds_each_wake_for_the_worst_sub_trigger_backlog() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_for_test(true);
        // The largest backlog the commit path can hold without firing its
        // own drain: one entry below the 12,000 scan trigger.
        for index in 0..11_999_u32 {
            put_overlay_record(&mut database, index);
        }
        let database = Arc::new(Mutex::new(database));
        assert_eq!(overlay_entries(&database), 11_999);
        let trigger = database.lock().unwrap().entity_overlay_soft_trigger();
        // One wake is bounded by four chunks, not a fixed number of rows:
        // wall-clock AIMD may shrink subsequent chunks under parallel load.
        assert_eq!(drain_overlay_in_background(&database, &trigger), 4);
        let mut remaining = overlay_entries(&database);
        assert!((3_807..11_999).contains(&remaining));
        for _ in 0..remaining {
            if trigger.below_soft_thresholds() { break; }
            let chunks = drain_overlay_in_background(&database, &trigger);
            assert!((1..=u64::from(OVERLAY_BACKGROUND_MAX_CHUNKS_PER_WAKE)).contains(&chunks));
            let next = overlay_entries(&database);
            assert!(next < remaining, "every wake must make bounded progress");
            remaining = next;
        }
        assert!(trigger.below_soft_thresholds());
        assert_eq!(drain_overlay_in_background(&database, &trigger), 0);
        let guard = database.lock().unwrap();
        for index in [0_u32, 8_191, 8_192, 11_998] {
            assert_eq!(
                get_overlay_record(&guard, index),
                Some(index.to_be_bytes().to_vec()),
            );
        }
    }

    #[test]
    fn background_drain_is_a_noop_below_the_soft_threshold() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_for_test(true);
        for index in 0..1_000_u32 {
            put_overlay_record(&mut database, index);
        }
        let database = Arc::new(Mutex::new(database));
        assert_eq!(overlay_entries(&database), 1_000);
        let trigger = database.lock().unwrap().entity_overlay_soft_trigger();
        assert!(!trigger.needs_drain());
        assert_eq!(drain_overlay_in_background(&database, &trigger), 0);
        // Nothing merged: the entries are still pending in the overlay and
        // reads resolve through it exactly.
        assert_eq!(overlay_entries(&database), 1_000);
        let guard = database.lock().unwrap();
        assert_eq!(
            get_overlay_record(&guard, 999),
            Some(999_u32.to_be_bytes().to_vec())
        );
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
