//! Off-authority payload-segment packing.
//!
//! Staged blocks must reach durable storage before the checkpoint retires
//! the active WAL. Packing them on the commit path made rotation commits
//! stall for a full 8 MiB segment write + fsync (~16 ms p99 spikes). This
//! worker reserves a staged batch through `BlockStore::begin_background_pack`,
//! builds and fsyncs the segment file without holding the authority mutex,
//! and records the result; commits or idle maintenance publish the manifest reference
//! and retires the staged copies (`Engine::adopt_background_pack`). Every
//! failure leaves payloads staged, so the synchronous checkpoint drain
//! remains the complete deterministic fallback.

use std::io;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::{Arc, Condvar, Mutex, Weak};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use crate::block::BlockStore;
use crate::payload_manifest::PayloadSegmentReference;

const WORKER_STACK_BYTES: usize = 512 * 1024;
const IDLE_INTERVAL: Duration = Duration::from_millis(25);
const ERROR_BACKOFF: Duration = Duration::from_millis(250);
/// Smallest staged batch the worker packs on its own. Batches stopped by
/// the 8 MiB segment budget always qualify (`begin_background_pack` treats
/// them as full); this floor only keeps sub-MiB tails on the foreground
/// checkpoint path, where their fsync cost stays off the latency tail.
const MINIMUM_WORKER_BATCH_BYTES: u64 = 1024 * 1024;
/// Hard ceiling on the packer's paced write rate. The knob only throttles a
/// background optimization, so callers clamp to this bound (with a warning)
/// instead of failing storage startup on an excessive operator value.
pub(crate) const MAX_PACK_PACE_BYTES_PER_SECOND: u64 = 16 * 1024 * 1024 * 1024;

/// Adaptive pacing controller: a runtime feedback loop that replaces any
/// operator-set rate. Foreground commit rate (`STAGE_ENGINE_COMMIT`) is the
/// activity signal and foreground WAL stage latency (`STAGE_WAL`) is the
/// pain signal; the pack rate multiplicatively halves when packing hurts
/// the foreground and recovers (×1.5) when it provably does not, against a
/// self-calibrating EWMA baseline learned from packer-quiet windows. An
/// idle foreground unpaces entirely — there is nobody to hurt — and an
/// explicit `TOFU_DB_PACK_BYTES_PER_SECOND` pins the fixed escape-hatch
/// rate instead.
#[derive(Debug)]
struct AdaptivePackController {
    rate_bytes_per_second: u64,
    unpaced: bool,
    last_tick: std::time::Instant,
    last_wal: (u64, u64),
    last_commit: u64,
    baseline_nanos_per_call: f64,
    packed_since_tick: bool,
    idle_streak: u32,
    recover_streak: u32,
    foreground_hot: bool,
    pressure: crate::fsync_stats::ForegroundSampler,
    last_progress: std::time::Instant,
}

/// Controller tick cadence; short enough to react to a latency spike
/// within a quarter second, long enough for meaningful latency samples.
const PACE_TICK: Duration = Duration::from_millis(250);
/// Commit rate at or above which the foreground counts as active.
const PACE_HOT_COMMITS_PER_SECOND: f64 = 4.0;
/// Quiet windows below this WAL-fsync count never move the baseline.
const PACE_MIN_LATENCY_SAMPLES: u64 = 8;
const PACE_BASELINE_EWMA_ALPHA: f64 = 0.2;
/// Pain thresholds: the packed-window foreground latency must exceed both
/// the relative (2× learned baseline) and the absolute (2 ms) bar. The
/// absolute bar sits just above a fast local mount's fsync cost so packer
/// fsync storms on fast mounts still register as pain.
const PACE_RELATIVE_PAIN: f64 = 2.0;
const PACE_ABSOLUTE_PAIN_NANOS: f64 = 2_000_000.0;
const PACE_RELATIVE_CALM: f64 = 1.25;
const PACE_ABSOLUTE_CALM_NANOS: f64 = 1_000_000.0;
const PACE_MIN_RATE_BYTES_PER_SECOND: u64 = 1024 * 1024;
const PACE_UNPACE_THRESHOLD_BYTES_PER_SECOND: u64 = 4 * 1024 * 1024 * 1024;
/// Starting rate on network-class volumes; local volumes start unpaced.
pub(crate) const ADAPTIVE_NETWORK_START_BYTES_PER_SECOND: Option<u64> =
    Some(64 * 1024 * 1024);
/// Idle windows (~1 s) before the packer fully unpaces.
const PACE_IDLE_WINDOWS_UNTIL_UNPACED: u32 = 4;
/// Staged pressure that forces packing even while the foreground is hot;
/// the quiet-gate only defers work the checkpoint path does not need soon.
const PACK_DEFER_PRESSURE_BYTES: u64 = 32 * 1024 * 1024;
/// Calm windows (~500 ms) before one multiplicative recovery step.
const PACE_RECOVER_WINDOWS_PER_STEP: u32 = 2;

impl AdaptivePackController {
    fn new(start_rate: Option<u64>) -> Self {
        Self {
            rate_bytes_per_second: start_rate.unwrap_or(PACE_UNPACE_THRESHOLD_BYTES_PER_SECOND),
            unpaced: start_rate.is_none(),
            last_tick: std::time::Instant::now(),
            last_wal: (
                crate::fsync_stats::component_stage_calls(crate::fsync_stats::COMPONENT_FOREGROUND, crate::fsync_stats::STAGE_WAL),
                crate::fsync_stats::component_stage_nanos(crate::fsync_stats::COMPONENT_FOREGROUND, crate::fsync_stats::STAGE_WAL),
            ),
            last_commit: crate::fsync_stats::component_stage_calls(crate::fsync_stats::COMPONENT_FOREGROUND, crate::fsync_stats::STAGE_ENGINE_COMMIT),
            baseline_nanos_per_call: PACE_ABSOLUTE_CALM_NANOS,
            packed_since_tick: false,
            idle_streak: 0,
            recover_streak: 0,
            foreground_hot: false,
            pressure: crate::fsync_stats::foreground_hotness_sampler(),
            last_progress: std::time::Instant::now(),
        }
    }

    /// Current pacing decision for the worker's pacer.
    fn current_rate(&self) -> Option<u64> {
        if self.unpaced {
            None
        } else {
            Some(self.rate_bytes_per_second)
        }
    }

    fn note_packed(&mut self) {
        self.last_progress = std::time::Instant::now();
        self.packed_since_tick = true;
    }

    /// Quiet-gate: while the foreground is hot the packer defers ordinary
    /// batches (its fsync storm would collide with user latency at p99);
    /// only real staged pressure or an explicit drain overrides.
    fn should_pack_now(&self, staged_bytes: u64, drain_requested: bool) -> bool {
        if drain_requested || !self.foreground_hot || self.last_progress.elapsed() >= Duration::from_secs(2) {
            return true;
        }
        staged_bytes >= PACK_DEFER_PRESSURE_BYTES
    }

    /// Recomputes the rate at most once per PACE_TICK; returns true when
    /// the pacing decision changed and the worker should reconfigure.
    fn tick(&mut self) -> bool {
        let now = std::time::Instant::now();
        let elapsed = now.saturating_duration_since(self.last_tick);
        if elapsed < PACE_TICK {
            return false;
        }
        self.last_tick = now;
        let wal_calls = crate::fsync_stats::component_stage_calls(crate::fsync_stats::COMPONENT_FOREGROUND, crate::fsync_stats::STAGE_WAL);
        let wal_nanos = crate::fsync_stats::component_stage_nanos(crate::fsync_stats::COMPONENT_FOREGROUND, crate::fsync_stats::STAGE_WAL);
        let commits = crate::fsync_stats::component_stage_calls(crate::fsync_stats::COMPONENT_FOREGROUND, crate::fsync_stats::STAGE_ENGINE_COMMIT);
        let delta_calls = wal_calls.saturating_sub(self.last_wal.0);
        let delta_nanos = wal_nanos.saturating_sub(self.last_wal.1);
        let delta_commits = commits.saturating_sub(self.last_commit);
        self.last_wal = (wal_calls, wal_nanos);
        self.last_commit = commits;
        let packed = std::mem::replace(&mut self.packed_since_tick, false);
        let hot = crate::fsync_stats::foreground_recently_hot(&mut self.pressure);
        self.tick_with(delta_calls, delta_nanos, if hot { delta_commits.max((4.0 * elapsed.as_secs_f64()).ceil() as u64) } else { delta_commits }, elapsed, packed)
    }

    /// Pure decision core, split from sampling so every branch is a
    /// deterministic unit fixture.
    fn tick_with(
        &mut self,
        delta_calls: u64,
        delta_nanos: u64,
        delta_commits: u64,
        elapsed: Duration,
        packed: bool,
    ) -> bool {
        let seconds = elapsed.as_secs_f64();
        let commit_rate = delta_commits as f64 / seconds;
        let foreground_hot = commit_rate >= PACE_HOT_COMMITS_PER_SECOND;
        self.foreground_hot = foreground_hot;
        let previous = self.current_rate();

        if !foreground_hot {
            self.recover_streak = 0;
            self.idle_streak = self.idle_streak.saturating_add(1);
            if self.idle_streak >= PACE_IDLE_WINDOWS_UNTIL_UNPACED {
                self.unpaced = true;
            }
            return previous != self.current_rate();
        }
        self.idle_streak = 0;

        if delta_calls < PACE_MIN_LATENCY_SAMPLES {
            // A hot foreground without enough WAL samples cannot prove pain
            // either way; hold the current decision.
            return previous != self.current_rate();
        }
        let mean_nanos = delta_nanos as f64 / delta_calls as f64;
        if !packed {
            // Packer-quiet window under foreground load: learn the mount's
            // unloaded latency baseline.
            self.baseline_nanos_per_call = PACE_BASELINE_EWMA_ALPHA * mean_nanos
                + (1.0 - PACE_BASELINE_EWMA_ALPHA) * self.baseline_nanos_per_call;
            return previous != self.current_rate();
        }

        let pain_threshold = (PACE_RELATIVE_PAIN * self.baseline_nanos_per_call)
            .max(PACE_ABSOLUTE_PAIN_NANOS);
        let calm_threshold = (PACE_RELATIVE_CALM * self.baseline_nanos_per_call)
            .max(PACE_ABSOLUTE_CALM_NANOS);
        if mean_nanos > pain_threshold {
            self.recover_streak = 0;
            self.unpaced = false;
            self.rate_bytes_per_second = (self.rate_bytes_per_second / 2)
                .max(PACE_MIN_RATE_BYTES_PER_SECOND);
        } else if mean_nanos < calm_threshold {
            self.recover_streak = self.recover_streak.saturating_add(1);
            if self.recover_streak >= PACE_RECOVER_WINDOWS_PER_STEP {
                self.recover_streak = 0;
                self.rate_bytes_per_second = (self.rate_bytes_per_second * 3 / 2)
                    .min(MAX_PACK_PACE_BYTES_PER_SECOND);
                if self.rate_bytes_per_second >= PACE_UNPACE_THRESHOLD_BYTES_PER_SECOND {
                    self.unpaced = true;
                }
            }
        } else {
            self.recover_streak = 0;
        }
        previous != self.current_rate()
    }
}

/// Leaky-bucket write pacer for mounts the foreground shares with the
/// packer. A back-to-back 8 MiB segment write + fsync saturates a network
/// mount, so foreground round trips queue behind it at the mount layer
/// (the residual `concurrent_read`/`swarm` p99 tail). Charging each packed
/// byte at a bounded rate keeps pack throughput strictly below the mount's
/// usable bandwidth; `None` (the default) keeps local volumes unpaced.
#[derive(Debug)]
struct PackPacer {
    bytes_per_second: u64,
    next_allowed: std::time::Instant,
}

impl PackPacer {
    fn new(bytes_per_second: u64) -> Option<Self> {
        if bytes_per_second == 0 {
            return None;
        }
        Some(Self {
            bytes_per_second,
            next_allowed: std::time::Instant::now(),
        })
    }

    fn set_rate(&mut self, bytes_per_second: u64) {
        self.bytes_per_second = bytes_per_second;
    }

    /// Reserve when the charged bytes are amortized at the bounded rate.
    /// The bucket never goes negative, so an idle worker resumes instantly
    /// while a bursting one is rate-limited.
    fn charge(&mut self, bytes: u64, now: std::time::Instant) -> std::time::Instant {
        let cost = std::time::Duration::from_secs_f64(
            bytes as f64 / self.bytes_per_second as f64,
        );
        self.next_allowed = if self.next_allowed > now {
            self.next_allowed + cost
        } else {
            now + cost
        };
        self.next_allowed
    }
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

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BackgroundPackerMetrics {
    pub packed_segments: u64,
    pub packed_bytes: u64,
    pub failed_rounds: u64,
    pub discarded_segments: u64,
    pub discarded_bytes: u64,
    pub retired_wals: u64,
    pub failed_wal_retirements: u64,
    pub pacing_waits: u64,
    pub pacing_wait_nanoseconds: u64,
    pub foreground_drain_unpaces: u64,
}

/// Pacing state for `system.metrics`: adaptive feedback loop or the
/// explicit fixed escape-hatch rate.
#[derive(Clone, Copy, Debug, Eq, PartialEq, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BackgroundPackerPacing {
    pub adaptive: bool,
    pub pace_bytes_per_second: Option<u64>,
}

#[derive(Default)]
struct PackerState {
    stopping: bool,
    metrics: BackgroundPackerMetrics,
    terminal_error: Option<ErrorSnapshot>,
}

struct Shared {
    blocks: Arc<BlockStore>,
    wal_retirement: Option<Arc<crate::wal_retirement::WalRetirement>>,
    state: Mutex<PackerState>,
    wake: Condvar,
    pacer: Mutex<Option<PackPacer>>,
    adaptive: bool,
    adaptive_start_rate: Option<u64>,
    pacing: Mutex<BackgroundPackerPacing>,
    /// The owning engine's per-step materialization budget at packer start;
    /// the batch-size ceiling never packs below it.
    materialize_batch_bytes: u64,
}

#[derive(Clone)]
pub struct BackgroundPackerObserver {
    shared: Weak<Shared>,
}

impl BackgroundPackerObserver {
    /// Current counters; `Ok(None)` once the packer has stopped and its shared
    /// state is gone, distinguishing a stopped packer from a poisoned one.
    pub fn metrics(&self) -> io::Result<Option<BackgroundPackerMetrics>> {
        let Some(shared) = self.shared.upgrade() else {
            return Ok(None);
        };
        let state = shared
            .state
            .lock()
            .map_err(|_| io::Error::other("background packer state poisoned"))?;
        Ok(Some(state.metrics))
    }

    pub fn pacing(&self) -> Option<BackgroundPackerPacing> {
        let shared = self.shared.upgrade()?;
        shared
            .pacing
            .lock()
            .ok()
            .map(|pacing| *pacing)
    }

    pub fn require_healthy(&self) -> io::Result<()> {
        let Some(shared) = self.shared.upgrade() else {
            return Ok(());
        };
        let state = shared
            .state
            .lock()
            .map_err(|_| io::Error::other("background packer state poisoned"))?;
        match &state.terminal_error {
            Some(error) => Err(error.clone().into_error()),
            None => Ok(()),
        }
    }
}

pub struct BackgroundPacker {
    shared: Arc<Shared>,
    worker: Mutex<Option<JoinHandle<()>>>,
}

impl BackgroundPacker {
    /// Packs a standalone block store. Authority callers use
    /// AuthorityDatabase::start_background_packer to include WAL retirement.
    pub fn start(blocks: Arc<BlockStore>) -> io::Result<Self> {
        Self::start_with_wal_retirement(blocks, None)
    }

    pub(crate) fn start_with_wal_retirement(
        blocks: Arc<BlockStore>,
        wal_retirement: Option<Arc<crate::wal_retirement::WalRetirement>>,
    ) -> io::Result<Self> {
        Self::start_paced(
            blocks,
            wal_retirement,
            0,
            crate::engine::checkpoint_materialize_batch_bytes(),
        )
    }

    /// `pace_bytes_per_second` pins a fixed escape-hatch rate (the explicit
    /// `TOFU_DB_PACK_BYTES_PER_SECOND` override). `None` engages the
    /// adaptive feedback controller: it starts at `adaptive_start_rate`
    /// (None = unpaced) and tiers the rate from foreground latency.
    /// `materialize_batch_bytes` is the owning engine's per-step
    /// materialization budget (probe-derived at authority open).
    pub(crate) fn start_paced(
        blocks: Arc<BlockStore>,
        wal_retirement: Option<Arc<crate::wal_retirement::WalRetirement>>,
        pace_bytes_per_second: u64,
        materialize_batch_bytes: u64,
    ) -> io::Result<Self> {
        Self::start_with_pacing(
            blocks,
            wal_retirement,
            Some(pace_bytes_per_second).filter(|pace| *pace > 0),
            false,
            None,
            materialize_batch_bytes,
        )
    }

    pub(crate) fn start_with_pacing(
        blocks: Arc<BlockStore>,
        wal_retirement: Option<Arc<crate::wal_retirement::WalRetirement>>,
        fixed_pace_bytes_per_second: Option<u64>,
        adaptive: bool,
        adaptive_start_rate: Option<u64>,
        materialize_batch_bytes: u64,
    ) -> io::Result<Self> {
        if fixed_pace_bytes_per_second.is_some_and(|pace| pace > MAX_PACK_PACE_BYTES_PER_SECOND) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "pack pace must not exceed 16 GiB/s",
            ));
        }
        let registration = blocks.register_background_packer()?;
        let retirement_registration = wal_retirement
            .as_ref()
            .map(|value| value.register_worker())
            .transpose()?;
        let fixed_pace = fixed_pace_bytes_per_second.filter(|pace| *pace > 0);
        let shared = Arc::new(Shared {
            blocks,
            wal_retirement,
            state: Mutex::new(PackerState::default()),
            wake: Condvar::new(),
            pacer: Mutex::new(if adaptive { None } else { PackPacer::new(fixed_pace.unwrap_or(0)) }),
            adaptive: adaptive && fixed_pace.is_none(),
            adaptive_start_rate,
            pacing: Mutex::new(BackgroundPackerPacing {
                adaptive: adaptive && fixed_pace.is_none(),
                pace_bytes_per_second: fixed_pace,
            }),
            materialize_batch_bytes,
        });
        let worker_shared = Arc::clone(&shared);
        let worker = thread::Builder::new()
            .name("tofu-db-background-pack".to_owned())
            .stack_size(WORKER_STACK_BYTES)
            .spawn(move || {
                let _registration = registration;
                if catch_unwind(AssertUnwindSafe(|| run_worker(&worker_shared))).is_err() {
                    if let Ok(mut state) = worker_shared.state.lock() {
                        state.terminal_error = Some(ErrorSnapshot::capture(&io::Error::other(
                            "background pack worker panicked",
                        )));
                    }
                }
                // Retain worker ownership through cleanup, so a successor
                // cannot race this exit and lose its completed reservation.
                let cleanup = worker_shared.blocks.discard_completed_background_pack();
                let retirement_cleanup = retirement_registration
                    .map(|value| {
                        catch_unwind(AssertUnwindSafe(|| value.finish())).unwrap_or_else(|_| {
                            Err(io::Error::other("WAL retirement shutdown panicked"))
                        })
                    })
                    .transpose();
                if let Ok(mut state) = worker_shared.state.lock() {
                    match cleanup {
                        Ok(Some(bytes)) => {
                            state.metrics.discarded_segments += 1;
                            state.metrics.discarded_bytes += bytes;
                        }
                        Ok(None) => {}
                        Err(error) if state.terminal_error.is_none() => {
                            state.terminal_error = Some(ErrorSnapshot::capture(&error));
                        }
                        Err(_) => {}
                    }
                    match retirement_cleanup {
                        Ok(Some(true)) => state.metrics.retired_wals += 1,
                        Ok(_) => {}
                        Err(error) => {
                            state.metrics.failed_wal_retirements += 1;
                            if state.terminal_error.is_none() {
                                state.terminal_error = Some(ErrorSnapshot::capture(&error));
                            }
                        }
                    }
                }
            })?;
        Ok(Self {
            shared,
            worker: Mutex::new(Some(worker)),
        })
    }

    pub fn observer(&self) -> BackgroundPackerObserver {
        BackgroundPackerObserver {
            shared: Arc::downgrade(&self.shared),
        }
    }

    pub(crate) fn metrics(&self) -> BackgroundPackerMetrics {
        self.shared
            .state
            .lock()
            .map(|state| state.metrics)
            .unwrap_or_default()
    }

    pub fn stop_and_join(&self) -> io::Result<()> {
        {
            let mut state = self
                .shared
                .state
                .lock()
                .map_err(|_| io::Error::other("background packer state poisoned"))?;
            state.stopping = true;
        }
        self.shared.wake.notify_all();
        let wake_result = self.shared.blocks.wake_background_packer();
        let worker = self
            .worker
            .lock()
            .map_err(|_| io::Error::other("background packer worker lock poisoned"))?
            .take();
        if worker.is_some_and(|worker| worker.join().is_err()) {
            return Err(io::Error::other("tofu-db background pack worker panicked"));
        }
        wake_result?;
        self.observer().require_healthy()
    }
}

impl Drop for BackgroundPacker {
    fn drop(&mut self) {
        let _ = self.stop_and_join();
    }
}

enum PackOutcome {
    Packed,
    Idle,
    Failed(io::Error),
    Panicked,
}

fn configure_pacer(shared: &Arc<Shared>, rate: Option<u64>) {
    if let Ok(mut pacer) = shared.pacer.lock() {
        match rate {
            Some(rate) => {
                if let Some(pacer) = pacer.as_mut() {
                    pacer.set_rate(rate);
                } else {
                    *pacer = PackPacer::new(rate);
                }
            }
            None => *pacer = None,
        }
    }
    if let Ok(mut pacing) = shared.pacing.lock() {
        pacing.pace_bytes_per_second = rate;
    }
}

fn run_worker(shared: &Arc<Shared>) {
    crate::fsync_stats::set_current_component(crate::fsync_stats::COMPONENT_BACKGROUND_PACKER);
    let mut controller = shared
        .adaptive
        .then(|| AdaptivePackController::new(shared.adaptive_start_rate));
    if let Some(controller) = controller.as_ref() {
        configure_pacer(shared, controller.current_rate());
    }
    loop {
        if let Some(controller) = controller.as_mut() {
            if controller.tick() {
                configure_pacer(shared, controller.current_rate());
            }
        }
        let observed = shared.blocks.background_pack_revision();
        {
            let state = match shared.state.lock() {
                Ok(state) => state,
                Err(_) => return,
            };
            if state.stopping {
                return;
            }
        }
        let outcome = match shared
            .wal_retirement
            .as_ref()
            .map(|value| value.reclaim())
            .transpose()
        {
            Ok(retired) => {
                if retired == Some(true) {
                    if let Ok(mut state) = shared.state.lock() {
                        state.metrics.retired_wals += 1;
                    }
                }
                let defer = controller.as_ref().is_some_and(|controller| {
                    !controller.should_pack_now(
                        shared
                            .blocks
                            .pending_staged_bytes()
                            .unwrap_or(u64::MAX),
                        shared.blocks.background_drain_requested(),
                    )
                });
                if defer {
                    PackOutcome::Idle
                } else {
                    pack_once(shared)
                }
            }
            Err(error) => {
                if let Ok(mut state) = shared.state.lock() {
                    state.metrics.failed_wal_retirements += 1;
                }
                PackOutcome::Failed(error)
            }
        };
        match outcome {
            PackOutcome::Packed => {
                if let Some(controller) = controller.as_mut() {
                    controller.note_packed();
                }
                if let Ok(mut state) = shared.state.lock() {
                    state.metrics.packed_segments += 1;
                }
            }
            PackOutcome::Idle => {
                let _ = shared
                    .blocks
                    .wait_for_background_pack_change(observed, IDLE_INTERVAL);
            }
            PackOutcome::Failed(error) => {
                if let Ok(mut state) = shared.state.lock() {
                    state.metrics.failed_rounds += 1;
                }
                crate::engine_log::error(
                    "background_pack",
                    &format!("background pack failed: {error}"),
                );
                if let Ok(state) = shared.state.lock() {
                    let _ = shared.wake.wait_timeout(state, ERROR_BACKOFF);
                }
            }
            PackOutcome::Panicked => {
                if let Ok(mut state) = shared.state.lock() {
                    state.terminal_error = Some(ErrorSnapshot::capture(&io::Error::other(
                        "background pack worker panicked",
                    )));
                }
                return;
            }
        }
    }
}

/// Batch-size ceiling derived from the pacing state: an unpaced or healthy
/// mount packs full materialize-budget segments; a mount under measured
/// pain gets smaller segments so one fsync storm has a bounded duration.
/// The 32 MiB steady-state target keeps catalog segment production ~4×
/// slower than the 8 MiB materialization batch would (the 4096-entry
/// catalog ceiling is a hard data-plane bound).
fn pain_bounded_batch_bytes(shared: &Arc<Shared>) -> u64 {
    let target = (32 * 1024 * 1024_u64)
        .max(shared.materialize_batch_bytes)
        .min(crate::payload_segment::MAX_SEGMENT_PAYLOAD_BYTES);
    let full = target.max(crate::block::MAX_BLOCK_BYTES as u64);
    let Ok(pacing) = shared.pacing.lock() else {
        return full;
    };
    match pacing.pace_bytes_per_second {
        Some(rate) if rate <= 4 * 1024 * 1024 => MINIMUM_WORKER_BATCH_BYTES,
        Some(rate) if rate <= 16 * 1024 * 1024 => (2 * MINIMUM_WORKER_BATCH_BYTES).min(full),
        _ => full,
    }
}

/// Catalog backpressure: at ≥95% of the 4,096-entry catalog ceiling the
/// packer stops producing segments (except for checkpoint drains, which
/// the rotation lane requires regardless) so the online collector's
/// consolidation catches up instead of the catalog hitting the wall.
fn catalog_nearly_full(shared: &Arc<Shared>) -> bool {
    let Ok(manifest) = shared.blocks.installed_payload_manifest() else {
        return false;
    };
    manifest.is_some_and(|manifest| {
        manifest.segments.len() * 100 >= crate::payload_manifest::MAX_PAYLOAD_SEGMENTS * 95
    })
}

/// Pacing protects running foreground work. Once a rotation fences writers,
/// adaptive pacing must yield to the finite drain that reopens that lane.
/// An explicit operator rate remains binding. All waits are wakeable on stop.
fn wait_after_pack(shared: &Arc<Shared>, payload_bytes: u64) -> io::Result<()> {
    let began = std::time::Instant::now();
    let deadline = shared.pacer.lock()
        .map_err(|_| io::Error::other("packer pacer poisoned"))?
        .as_mut().map(|pacer| pacer.charge(payload_bytes, began));
    let Some(deadline) = deadline else { return Ok(()); };
    {
        let mut state = shared.state.lock()
            .map_err(|_| io::Error::other("packer state poisoned"))?;
        state.metrics.pacing_waits += 1;
    }
    let result = (|| {
        loop {
            // Observe the revision BEFORE checking predicates: a drain/stop
            // between the checks and the park must never be a lost wakeup.
            let observed = shared.blocks.background_pack_revision();
            if shared.state.lock()
                .map_err(|_| io::Error::other("packer state poisoned"))?.stopping {
                return Ok(());
            }
            if shared.adaptive && shared.blocks.foreground_drain_requested() {
                if let Some(pacer) = shared.pacer.lock()
                    .map_err(|_| io::Error::other("packer pacer poisoned"))?.as_mut() {
                    // Do not repay this debt after the write lane reopens.
                    pacer.next_allowed = std::time::Instant::now();
                }
                shared.state.lock()
                    .map_err(|_| io::Error::other("packer state poisoned"))?
                    .metrics.foreground_drain_unpaces += 1;
                return Ok(());
            }
            let remaining = deadline.saturating_duration_since(std::time::Instant::now());
            if remaining.is_zero() { return Ok(()); }
            shared.blocks.wait_for_background_pack_change(observed, remaining)?;
        }
    })();
    if let Ok(mut state) = shared.state.lock() {
        state.metrics.pacing_wait_nanoseconds = state.metrics.pacing_wait_nanoseconds
            .saturating_add(began.elapsed().as_nanos().min(u64::MAX as u128) as u64);
    }
    result
}

fn pack_once(shared: &Arc<Shared>) -> PackOutcome {
    if catalog_nearly_full(shared) && !shared.blocks.background_drain_requested() {
        return PackOutcome::Idle;
    }
    let Some((segment_id, ids)) = (match shared.blocks.begin_background_pack(
        if shared.blocks.background_drain_requested() { 1 } else { MINIMUM_WORKER_BATCH_BYTES },
        pain_bounded_batch_bytes(shared),
    ) {
        Ok(batch) => batch,
        Err(error) => return PackOutcome::Failed(error),
    }) else {
        return PackOutcome::Idle;
    };
    let built =
        catch_unwind(AssertUnwindSafe(|| {
            let segment = shared.blocks.payload_segment_store()?.create(
                segment_id,
                ids.clone(),
                |block_id| shared.blocks.get(block_id),
            )?;
            let reference = PayloadSegmentReference::new(
                segment.metadata(),
                segment.first_block_id(),
                segment.last_block_id(),
            )?;
            shared.blocks.verify_payload_segment(&reference)?;
            // The packed filter must know these IDs before any publication can
            // retire their staged copies; otherwise a post-retirement read would
            // skip the segment scan and report NotFound.
            shared.blocks.note_packed(&ids)?;
            Ok::<PayloadSegmentReference, io::Error>(reference)
        }));
    match built {
        Ok(Ok(reference)) => {
            let payload_bytes = reference.payload_bytes;
            match shared
                .blocks
                .complete_background_pack(segment_id, reference)
            {
                Ok(crate::block::BackgroundPackCompletion::Published) => {
                    if let Ok(mut state) = shared.state.lock() {
                        state.metrics.packed_bytes += payload_bytes;
                    }
                    // Rate-limit the next round: an unpaced worker would
                    // immediately start another 8 MiB segment and keep the
                    // shared mount saturated.
                    if let Err(error) = wait_after_pack(shared, payload_bytes) {
                        return PackOutcome::Failed(error);
                    }
                    PackOutcome::Packed
                }
                Ok(crate::block::BackgroundPackCompletion::Stale) => {
                    // The checkpoint drain already packed these payloads in
                    // the foreground after its wait budget ran out: this
                    // segment is redundant. Reclaim its file and count a
                    // discard — never a failure round. Backing the worker
                    // off here would leave WAL retirement further behind.
                    let reclaimed = shared
                        .blocks
                        .payload_segment_store()
                        .and_then(|store| store.remove_segment(segment_id))
                        .unwrap_or(0);
                    if let Ok(mut state) = shared.state.lock() {
                        state.metrics.discarded_segments += 1;
                        state.metrics.discarded_bytes += reclaimed;
                    }
                    PackOutcome::Idle
                }
                Err(error) => PackOutcome::Failed(error),
            }
        }
        Ok(Err(error)) => {
            let _ = shared.blocks.fail_background_pack(segment_id);
            PackOutcome::Failed(error)
        }
        Err(_) => {
            let _ = shared.blocks.fail_background_pack(segment_id);
            PackOutcome::Panicked
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pacer_accounts_for_bursts_without_accumulating_idle_credit() {
        assert!(PackPacer::new(0).is_none());
        let mut pacer = PackPacer::new(1_000_000).unwrap();
        let now = std::time::Instant::now() + Duration::from_secs(1);
        assert_eq!(pacer.charge(100_000, now), now + Duration::from_millis(100));
        assert_eq!(pacer.charge(400_000, now), now + Duration::from_millis(500));
        let later = now + Duration::from_secs(10);
        assert_eq!(pacer.charge(100_000, later), later + Duration::from_millis(100));
    }

    fn pacing_test_shared(store: Arc<BlockStore>, adaptive: bool) -> Arc<Shared> {
        Arc::new(Shared {
            blocks: store,
            wal_retirement: None,
            state: Mutex::new(PackerState::default()),
            wake: Condvar::new(),
            pacer: Mutex::new(PackPacer::new(1)),
            adaptive,
            adaptive_start_rate: Some(1),
            pacing: Mutex::new(BackgroundPackerPacing {
                adaptive, pace_bytes_per_second: Some(1),
            }),
            materialize_batch_bytes: 8 * 1024 * 1024,
        })
    }

    #[test]
    fn rotation_drain_interrupts_only_adaptive_pacing_and_stop_interrupts_both() {
        for adaptive in [true, false] {
            let directory = tempfile::tempdir().unwrap();
            let store = Arc::new(BlockStore::initialize(directory.path()).unwrap());
            let shared = pacing_test_shared(Arc::clone(&store), adaptive);
            let (done_tx, done_rx) = std::sync::mpsc::channel();
            let worker_shared = Arc::clone(&shared);
            let worker = std::thread::spawn(move || {
                done_tx.send(wait_after_pack(&worker_shared, 3600)).unwrap();
            });
            let limit = std::time::Instant::now() + Duration::from_secs(2);
            while shared.state.lock().unwrap().metrics.pacing_waits == 0
                && std::time::Instant::now() < limit {
                std::thread::yield_now();
            }
            let started = shared.state.lock().unwrap().metrics.pacing_waits == 1;
            store.request_background_drain();
            let early = done_rx.recv_timeout(Duration::from_millis(20));
            store.request_foreground_drain();
            let fenced = done_rx.recv_timeout(if adaptive {
                Duration::from_secs(2)
            } else { Duration::from_millis(20) });
            // Always unblock and join before asserting, including failed cases.
            shared.state.lock().unwrap().stopping = true;
            store.wake_background_packer().unwrap();
            if fenced.is_err() { done_rx.recv_timeout(Duration::from_secs(2)).unwrap().unwrap(); }
            worker.join().unwrap();
            assert!(started);
            assert!(matches!(early, Err(std::sync::mpsc::RecvTimeoutError::Timeout)));
            if adaptive { fenced.unwrap().unwrap(); }
            else { assert!(matches!(fenced, Err(std::sync::mpsc::RecvTimeoutError::Timeout))); }
            assert_eq!(shared.state.lock().unwrap().metrics.foreground_drain_unpaces,
                u64::from(adaptive));
            store.finish_background_drain();
            assert!(!store.foreground_drain_requested());
            assert!(!store.background_drain_requested());
        }
    }

    #[test]
    fn worker_stop_reclaims_its_unpublished_segment_and_keeps_staged_payloads() {
        let directory = tempfile::tempdir().unwrap();
        let store = Arc::new(BlockStore::initialize(directory.path()).unwrap());
        let payload = vec![0x59; 2 * 1024 * 1024];
        let id = store.put_deferred(&payload).unwrap().0;
        let packer = BackgroundPacker::start(Arc::clone(&store)).unwrap();
        for _ in 0..200 {
            if packer.metrics().packed_segments == 1 {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(packer.metrics().packed_segments, 1);
        packer.stop_and_join().unwrap();
        assert_eq!(packer.metrics().discarded_segments, 1);
        assert!(packer.metrics().discarded_bytes >= payload.len() as u64);
        assert_eq!(
            std::fs::read_dir(directory.path().join("payload-segments"))
                .unwrap()
                .count(),
            0
        );
        assert_eq!(store.get(id).unwrap(), payload);
        assert!(store.pending_payload(id).unwrap().is_some());
        assert!(store.take_completed_background_pack().unwrap().is_none());
        packer.stop_and_join().unwrap();
    }

    #[test]
    fn adaptive_controller_unpaces_when_the_foreground_is_idle() {
        let mut controller = AdaptivePackController::new(ADAPTIVE_NETWORK_START_BYTES_PER_SECOND);
        assert_eq!(
            controller.current_rate(),
            ADAPTIVE_NETWORK_START_BYTES_PER_SECOND
        );
        for _ in 0..PACE_IDLE_WINDOWS_UNTIL_UNPACED {
            controller.tick_with(0, 0, 0, PACE_TICK, false);
        }
        assert_eq!(controller.current_rate(), None, "idle foreground unpaces");
    }

    #[test]
    fn adaptive_controller_halves_on_measured_foreground_pain() {
        let mut controller = AdaptivePackController::new(ADAPTIVE_NETWORK_START_BYTES_PER_SECOND);
        let start = controller.current_rate().unwrap();
        // Learn a calm baseline from packer-quiet windows first.
        for _ in 0..4 {
            controller.tick_with(64, 64 * 500_000, 16, PACE_TICK, false);
        }
        // A packed window with latency far above the baseline halves the rate.
        controller.tick_with(64, 64 * 40_000_000, 16, PACE_TICK, true);
        let halved = controller.current_rate().unwrap();
        assert_eq!(halved, start / 2);
        // Pain persists: the floor is honored.
        for _ in 0..40 {
            controller.tick_with(64, 64 * 40_000_000, 16, PACE_TICK, true);
        }
        assert_eq!(
            controller.current_rate().unwrap(),
            PACE_MIN_RATE_BYTES_PER_SECOND
        );
    }

    #[test]
    fn adaptive_controller_recovers_to_unpaced_after_calm_windows() {
        let mut controller = AdaptivePackController::new(ADAPTIVE_NETWORK_START_BYTES_PER_SECOND);
        // Drive the rate down with pain.
        for _ in 0..4 {
            controller.tick_with(64, 64 * 500_000, 16, PACE_TICK, false);
        }
        for _ in 0..8 {
            controller.tick_with(64, 64 * 40_000_000, 16, PACE_TICK, true);
        }
        let floored = controller.current_rate().unwrap();
        assert!(floored < ADAPTIVE_NETWORK_START_BYTES_PER_SECOND.unwrap());
        // Sustained calm windows recover multiplicatively, then unpace.
        let mut rate = floored;
        let mut guard = 0;
        while controller.current_rate().is_some() && guard < 64 {
            controller.tick_with(64, 64 * 500_000, 16, PACE_TICK, true);
            rate = controller.current_rate().unwrap_or(MAX_PACK_PACE_BYTES_PER_SECOND);
            guard += 1;
        }
        assert!(controller.current_rate().is_none(), "calm mounts unpace");
        assert!(rate >= PACE_UNPACE_THRESHOLD_BYTES_PER_SECOND / 2);
    }

    #[test]
    fn adaptive_controller_holds_through_insufficient_latency_samples() {
        let mut controller = AdaptivePackController::new(ADAPTIVE_NETWORK_START_BYTES_PER_SECOND);
        let start = controller.current_rate();
        // Hot foreground but too few WAL samples to judge: hold.
        controller.tick_with(2, 80_000_000, 16, PACE_TICK, true);
        assert_eq!(controller.current_rate(), start);
    }

    #[test]
    fn adaptive_controller_recovers_baseline_only_from_quiet_windows() {
        let mut controller = AdaptivePackController::new(ADAPTIVE_NETWORK_START_BYTES_PER_SECOND);
        // Packed windows with extreme latency never move the baseline.
        controller.tick_with(64, 64 * 90_000_000, 16, PACE_TICK, true);
        let baseline_after_packed = controller.baseline_nanos_per_call;
        controller.tick_with(64, 64 * 500_000, 16, PACE_TICK, false);
        let baseline_after_quiet = controller.baseline_nanos_per_call;
        assert_eq!(baseline_after_packed, PACE_ABSOLUTE_CALM_NANOS);
        assert!(baseline_after_quiet < PACE_ABSOLUTE_CALM_NANOS);
    }

    #[test]
    fn worker_stop_surfaces_cleanup_error_and_preserves_source_payload() {
        use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Vfs};
        use std::path::Path;
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let store =
            Arc::new(BlockStore::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap());
        let payload = vec![0x58; 2 * 1024 * 1024];
        let id = store.put_deferred(&payload).unwrap().0;
        let packer = BackgroundPacker::start(Arc::clone(&store)).unwrap();
        for _ in 0..200 {
            if packer.metrics().packed_segments == 1 {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(packer.metrics().packed_segments, 1);
        vfs.arm_fault(Some(FaultRule {
            operation_number: 1,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        assert_eq!(
            packer.stop_and_join().unwrap_err().kind(),
            io::ErrorKind::Interrupted
        );
        assert_eq!(
            packer.observer().require_healthy().unwrap_err().kind(),
            io::ErrorKind::Interrupted
        );
        assert!(!store.background_packer_running());
        vfs.arm_fault(None).unwrap();
        assert_eq!(store.get(id).unwrap(), payload);
        assert!(store.pending_payload(id).unwrap().is_some());
    }

    #[test]
    fn worker_registration_tracks_exit_and_rejects_duplicate_workers() {
        let directory = tempfile::tempdir().unwrap();
        let store = Arc::new(BlockStore::initialize(directory.path()).unwrap());
        assert!(!store.background_packer_running());
        let first = BackgroundPacker::start(Arc::clone(&store)).unwrap();
        assert!(store.background_packer_running());
        assert!(
            matches!(BackgroundPacker::start(Arc::clone(&store)), Err(error)
            if error.kind() == io::ErrorKind::AlreadyExists)
        );
        first.stop_and_join().unwrap();
        assert!(!store.background_packer_running());
        let second = BackgroundPacker::start(Arc::clone(&store)).unwrap();
        // Repeated stop/drop of an old worker cannot unregister its successor.
        first.stop_and_join().unwrap();
        drop(first);
        assert!(store.background_packer_running());
        drop(second);
        assert!(!store.background_packer_running());
    }

    #[test]
    fn worker_registration_releases_on_unwind() {
        let directory = tempfile::tempdir().unwrap();
        let store = Arc::new(BlockStore::initialize(directory.path()).unwrap());
        let registration = store.register_background_packer().unwrap();
        let result = catch_unwind(AssertUnwindSafe(move || {
            let _registration = registration;
            panic!("injected worker exit");
        }));
        assert!(result.is_err());
        assert!(!store.background_packer_running());
    }

    #[test]
    fn worker_packs_staged_payloads_off_the_authority() {
        let directory = tempfile::tempdir().unwrap();
        let store = Arc::new(BlockStore::initialize(directory.path()).unwrap());
        // The worker only takes batches at the materialization threshold;
        // smaller staged sets stay with the foreground checkpoint path.
        let mut staged = Vec::new();
        for slot in 0..9_u8 {
            let payload = vec![slot; 1024 * 1024];
            staged.push((store.put_deferred(&payload).unwrap().0, payload));
        }

        let packer = BackgroundPacker::start(Arc::clone(&store)).unwrap();
        let mut published = None;
        for _ in 0..200 {
            if let Some(publication) = store.take_completed_background_pack().unwrap() {
                published = Some(publication);
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        let metrics = packer.metrics();
        let (reference, packed, _publication) = published.unwrap_or_else(|| {
            panic!(
                "worker publishes the staged batch: metrics={metrics:?} creating={:?}",
                store.background_pack_creating()
            )
        });
        // The 32 MiB steady-state batch target admits all nine 1 MiB
        // blocks (larger segments keep catalog production ~4x slower than
        // the 8 MiB materialization batch would).
        assert_eq!(packed.len(), 9);
        // The completed segment is a real durable file that passes the same
        // verification the foreground publication path requires.
        store.verify_payload_segment(&reference).unwrap();
        for (id, payload) in &staged {
            assert_eq!(&store.get(*id).unwrap(), payload);
        }

        let metrics = packer.metrics();
        assert_eq!(metrics.packed_segments, 1);
        assert!(metrics.packed_bytes > 0);
        assert_eq!(metrics.failed_rounds, 0);
        packer.observer().require_healthy().unwrap();
        packer.stop_and_join().unwrap();
        // The authority already took publication: shutdown cannot reclaim it.
        store.verify_payload_segment(&reference).unwrap();
        assert_eq!(packer.metrics().discarded_segments, 0);
        // A second join is a no-op rather than an error.
        packer.stop_and_join().unwrap();
    }

    #[test]
    fn worker_stays_idle_without_staged_payloads() {
        let directory = tempfile::tempdir().unwrap();
        let store = Arc::new(BlockStore::initialize(directory.path()).unwrap());
        let packer = BackgroundPacker::start(Arc::clone(&store)).unwrap();
        thread::sleep(Duration::from_millis(100));
        let metrics = packer.metrics();
        assert_eq!(metrics.packed_segments, 0);
        assert_eq!(metrics.failed_rounds, 0);
        packer.observer().require_healthy().unwrap();
        packer.stop_and_join().unwrap();
    }
}
