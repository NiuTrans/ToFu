//! Online incremental backup for the serving daemon.
//!
//! One dedicated `tofu-db-online-backup` thread. Each epoch anchors a
//! checkpoint under a short authority hold (overlay merge + checkpoint +
//! manifest + anchor registration), then performs the whole incremental
//! copy off-mutex — source blocks are immutable and the registered anchor
//! joins every online-GC mark, so the copy can never lose a block mid-epoch.
//! The snapshot manifest publishes last; retention prune follows. The
//! `system.backup` control operation rings the doorbell and waits on a
//! completion channel; it never touches the authority mutex itself.

use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, RwLock};
use std::time::{Duration, Instant};

use crate::authority::AuthorityDatabase;
use crate::authority::BackupAnchorCapture;
use crate::backup::{self, BackupMetrics};
use crate::block::BlockId;
use crate::history::HistorySegmentReference;
use crate::vfs::Vfs;

/// Fallback cadence when no scheduler-driven epoch succeeds. The daily
/// application scheduler is the primary trigger; this keeps a fresh
/// restore point even when the scheduler never runs (no user knob).
pub const ONLINE_BACKUP_MAX_INTERVAL: Duration = Duration::from_secs(20 * 3600);
/// Generations retained after each successful epoch (mirrors the sidecar
/// default retention of two verified restore points).
pub const ONLINE_BACKUP_RETAINED_GENERATIONS: usize = 2;

const DRIVER_POLL_INTERVAL: Duration = Duration::from_millis(250);
const DRIVER_STACK_BYTES: usize = 1024 * 1024;
const DRIVER_STOP_JOIN_BUDGET: Duration = Duration::from_secs(5);
/// Routine anchor/checkpoint contention (`WouldBlock`) is retried inside
/// the epoch before it ever surfaces as a scheduler-visible failure.
const TRANSIENT_RETRY_LIMIT: u32 = 8;
const TRANSIENT_RETRY_BACKOFF: Duration = Duration::from_millis(250);
/// Pre-anchor overlay drain bounds (2026-09-21 FUSE brownout): the anchor
/// used to run the full overlay merge + checkpoint inside ONE authority
/// hold — seconds-to-minutes on a contended mount, wedging every
/// foreground request behind its wire deadline. The epoch now drains the
/// backlog in the certified bounded chunks (one short hold per chunk,
/// foreground-deferring pauses between chunks) and only anchors below the
/// soft thresholds, so the anchor hold covers at most what foreground
/// commits appended during the final anchor race. The chunk cap bounds an
/// epoch's drain work when foreground writes outpace the drain; exceeding
/// it defers the epoch like any other contention.
const MAX_ANCHOR_DRAIN_CHUNKS: u32 = 8_192;
const ANCHOR_DRAIN_CHUNK_INTERVAL: Duration = Duration::from_millis(2);

/// Everything the online GC needs to protect while an off-mutex backup
/// copy is reading (docs/TOFUDB_ONLINE_GC.md rule S2, extended to backup
/// anchors). Captured under the anchor hold; registered on the authority.
#[derive(Clone, Debug)]
pub struct BackupAnchorProjection {
    pub root: Option<BlockId>,
    pub history_manifest_block_id: Option<BlockId>,
    pub history_segments: Vec<HistorySegmentReference>,
}

/// One completed epoch's receipt; the `system.backup` response derives
/// from this.
#[derive(Clone, Debug, serde::Serialize)]
pub struct OnlineBackupEpochReport {
    pub ok: bool,
    pub backend: &'static str,
    pub artifact_kind: &'static str,
    pub backup: String,
    pub manifest: String,
    pub bytes: u64,
    pub sha256: String,
    pub durable_sequence: u64,
    pub copied_blocks: u64,
    pub copied_block_bytes: u64,
    pub manifest_bytes: u64,
    pub estimated_bytes: u64,
    pub retained_generations: u32,
    pub pruned_generations: u32,
    pub pruned_block_bytes: u64,
}

/// Process-local collector state surfaced through `system.metrics`.
#[derive(Clone, Debug, Default, serde::Serialize)]
pub struct OnlineBackupMetrics {
    pub replicated_sequence: Option<u64>,
    pub replication_interval_ms: Option<u64>,
    pub epochs_completed: u64,
    pub epochs_failed: u64,
    pub epoch_in_progress: bool,
    pub last_success_unix_ms: Option<u64>,
    pub last_error: Option<String>,
    pub last_report: Option<OnlineBackupEpochReport>,
}

struct SharedState {
    metrics: RwLock<OnlineBackupMetrics>,
    doorbell: AtomicU64,
    served: AtomicU64,
    completed: Mutex<Option<Result<OnlineBackupEpochReport, String>>>,
    completion_signal: Condvar,
}

impl SharedState {
    fn note_start(&self) {
        if let Ok(mut metrics) = self.metrics.write() {
            metrics.epoch_in_progress = true;
        }
    }

    fn note_success(&self, report: OnlineBackupEpochReport, now_unix_ms: u64) {
        if let Ok(mut metrics) = self.metrics.write() {
            metrics.replicated_sequence = Some(report.durable_sequence);
            metrics.epochs_completed += 1;
            metrics.epoch_in_progress = false;
            metrics.last_success_unix_ms = Some(now_unix_ms);
            metrics.last_error = None;
            metrics.last_report = Some(report.clone());
        }
        self.finish(Ok(report));
    }

    fn note_failure(&self, error: &io::Error) {
        if let Ok(mut metrics) = self.metrics.write() {
            metrics.epochs_failed += 1;
            metrics.epoch_in_progress = false;
            metrics.last_error = Some(error.to_string());
        }
        self.finish(Err(error.to_string()));
    }

    fn finish(&self, result: Result<OnlineBackupEpochReport, String>) {
        self.served.fetch_add(1, Ordering::AcqRel);
        {
            let mut completed = self
                .completed
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            *completed = Some(result);
        }
        self.completion_signal.notify_all();
    }
}

/// Cloneable observer for the control operations and health checks.
#[derive(Clone)]
pub struct OnlineBackupObserver {
    shared: Arc<SharedState>,
    stopped: Arc<AtomicBool>,
}

impl OnlineBackupObserver {
    pub fn metrics(&self) -> OnlineBackupMetrics {
        self.shared
            .metrics
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }

    pub fn require_healthy(&self) -> io::Result<()> {
        if self.stopped.load(Ordering::Acquire) {
            return Err(io::Error::other(
                "online backup driver stopped unexpectedly",
            ));
        }
        Ok(())
    }

    /// Rings the doorbell and waits for the next finished epoch, at most
    /// `deadline_unix_ms`. Errors surface as `WouldBlock` (busy scheduling
    /// the epoch) or the epoch's own failure text.
    pub fn run_and_wait(&self, deadline_unix_ms: u64) -> io::Result<OnlineBackupEpochReport> {
        self.shared.doorbell.fetch_add(1, Ordering::AcqRel);
        let served_at_request = self.shared.served.load(Ordering::Acquire);
        let mut completed = self
            .shared
            .completed
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        loop {
            if self.shared.served.load(Ordering::Acquire) > served_at_request {
                return match completed.take() {
                    Some(Ok(report)) => Ok(report),
                    Some(Err(error)) => Err(io::Error::other(error)),
                    None => Err(io::Error::other("backup epoch finished without a receipt")),
                };
            }
            let now_unix_ms = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|duration| duration.as_millis() as u64)
                .unwrap_or(0);
            if now_unix_ms >= deadline_unix_ms {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "system.backup waited past its request deadline",
                ));
            }
            let remaining = Duration::from_millis((deadline_unix_ms - now_unix_ms).min(250));
            completed = self
                .shared
                .completion_signal
                .wait_timeout(completed, remaining)
                .map_err(|_| io::Error::other("backup completion lock poisoned"))?
                .0;
        }
    }
}

/// Dedicated driver thread. Follows the maintenance lane's politeness:
/// `try_lock` for every authority hold, contention always defers the
/// backup, never the foreground.
pub struct OnlineBackupDriver {
    stop: Arc<AtomicBool>,
    thread: Mutex<Option<std::thread::JoinHandle<()>>>,
    observer: OnlineBackupObserver,
}

struct EpochContext {
    backup_dir: PathBuf,
    destination_vfs: Arc<dyn Vfs>,
    retention: usize,
    _fastpath_lease: Option<Arc<crate::fastpath::FastpathLease>>,
}

impl OnlineBackupDriver {
    pub fn start(
        database: &Arc<Mutex<AuthorityDatabase>>,
        backup_dir: PathBuf,
        at_rest_key: Option<crate::at_rest::AtRestKey>,
    ) -> io::Result<Self> {
        if !backup_dir.is_absolute() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "online backup directory must be absolute",
            ));
        }
        {
            let guard = database
                .lock()
                .map_err(|_| io::Error::other("authority mutex poisoned"))?;
            guard.validate_online_backup_directory(&backup_dir)?;
        }
        std::fs::create_dir_all(&backup_dir)?;
        Self::start_with_destination_vfs(
            database,
            backup_dir.clone(),
            crate::at_rest::destination_vfs(&[&backup_dir], at_rest_key),
        )
    }

    pub(crate) fn start_fastpath(
        database: &Arc<Mutex<AuthorityDatabase>>, backup_dir: PathBuf,
        key: Option<crate::at_rest::AtRestKey>, lease: Arc<crate::fastpath::FastpathLease>,
    ) -> io::Result<Self> {
        database.lock().map_err(|_| io::Error::other("authority mutex poisoned"))?
            .validate_online_backup_directory(&backup_dir)?;
        Self::start_scheduled(database, backup_dir.clone(),
            crate::at_rest::destination_vfs(&[&backup_dir], key), Some(lease))
    }

    /// Constructor with an explicit destination VFS; production wraps it
    /// through `at_rest::destination_vfs`, tests pass the deterministic one.
    pub(crate) fn start_with_destination_vfs(
        database: &Arc<Mutex<AuthorityDatabase>>,
        backup_dir: PathBuf,
        destination_vfs: Arc<dyn Vfs>,
    ) -> io::Result<Self> {
        Self::start_scheduled(database, backup_dir, destination_vfs, None)
    }

    fn start_scheduled(
        database: &Arc<Mutex<AuthorityDatabase>>, backup_dir: PathBuf,
        destination_vfs: Arc<dyn Vfs>, fastpath_lease: Option<Arc<crate::fastpath::FastpathLease>>,
    ) -> io::Result<Self> {
        let fastpath = fastpath_lease.is_some();
        let initial_sequence = fastpath_lease.as_ref().map(|lease| lease.shadow_sequence);
        let interval = fastpath_lease.as_ref().map(|lease| lease.interval)
            .unwrap_or(ONLINE_BACKUP_MAX_INTERVAL);
        let context = EpochContext {
            destination_vfs, backup_dir,
            retention: ONLINE_BACKUP_RETAINED_GENERATIONS,
            _fastpath_lease: fastpath_lease,
        };
        let shared = Arc::new(SharedState {
            metrics: RwLock::new(OnlineBackupMetrics {
                replicated_sequence: initial_sequence,
                replication_interval_ms: fastpath.then_some(interval.as_millis() as u64),
                ..OnlineBackupMetrics::default()
            }),
            doorbell: AtomicU64::new(0),
            served: AtomicU64::new(0),
            completed: Mutex::new(None),
            completion_signal: Condvar::new(),
        });
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = Arc::new(AtomicBool::new(false));
        let thread = std::thread::Builder::new()
            .name("tofu-db-online-backup".to_owned())
            .stack_size(DRIVER_STACK_BYTES)
            .spawn({
                let shared = Arc::clone(&shared);
                let stop = Arc::clone(&stop);
                let stopped = Arc::clone(&stopped);
                let database = Arc::clone(database);
                move || {
                    // Boot grace: startup is the latency-sensitive phase;
                    // treat boot as a fresh success for fallback purposes.
                    let mut last_attempt = Instant::now();
                    let mut cadence = interval;
                    let mut replicated_sequence = initial_sequence;
                    let mut served_count = 0_u64;
                    loop {
                        if stop.load(Ordering::Acquire) {
                            break;
                        }
                        let requested = shared.doorbell.load(Ordering::Acquire);
                        let fallback_due = last_attempt.elapsed() >= cadence;
                        if requested > served_count || fallback_due {
                            // A quiet native fastpath must not walk the entire
                            // corpus on every tick. Doorbells still force epochs.
                            if fastpath && requested <= served_count {
                                let sequence = database.try_lock().ok()
                                    .and_then(|guard| guard.writer_engine().ok().map(|engine| engine.state().durable_sequence));
                                if sequence.is_none() {
                                    std::thread::sleep(DRIVER_POLL_INTERVAL);
                                    continue;
                                }
                                if sequence == replicated_sequence {
                                    last_attempt = Instant::now();
                                    std::thread::sleep(DRIVER_POLL_INTERVAL);
                                    continue;
                                }
                            }
                            last_attempt = Instant::now();
                            shared.note_start();
                            let mut attempt = 0_u32;
                            let outcome = loop {
                                match run_epoch(&database, &context, &stop) {
                                    Err(error)
                                        if error.kind() == io::ErrorKind::WouldBlock
                                            && attempt < TRANSIENT_RETRY_LIMIT
                                            && !stop.load(Ordering::Acquire) =>
                                    {
                                        // Anchor/checkpoint contention at
                                        // boot or under load is routine;
                                        // absorb it inside the epoch so the
                                        // scheduler never sees it.
                                        attempt += 1;
                                        std::thread::sleep(TRANSIENT_RETRY_BACKOFF);
                                        continue;
                                    }
                                    outcome => break outcome,
                                }
                            };
                            let succeeded = outcome.is_ok();
                            match outcome {
                                Ok(report) => {
                                    replicated_sequence = Some(report.durable_sequence);
                                    shared.note_success(report, now_unix_ms());
                                }
                                Err(error) => {
                                    shared.note_failure(&error);
                                }
                            }
                            // Failed fallback epochs get a bounded retry cadence,
                            // never a tight full-copy/prune loop. Slow fastpath
                            // epochs adapt cadence to their measured copy cost.
                            cadence = if succeeded { interval } else { interval.min(Duration::from_secs(30)) };
                            if fastpath { cadence = cadence.max(last_attempt.elapsed()); }
                            if fastpath {
                                if let Ok(mut metrics) = shared.metrics.write() {
                                    metrics.replication_interval_ms = Some(cadence.as_millis() as u64);
                                }
                            }
                            last_attempt = Instant::now();
                            served_count = served_count.max(requested);
                            continue;
                        }
                        std::thread::sleep(DRIVER_POLL_INTERVAL);
                    }
                    stopped.store(true, Ordering::Release);
                }
            })?;
        Ok(Self {
            stop,
            thread: Mutex::new(Some(thread)),
            observer: OnlineBackupObserver {
                shared,
                stopped,
            },
        })
    }

    pub fn observer(&self) -> OnlineBackupObserver {
        self.observer.clone()
    }

    pub fn stop_and_join(&self) -> io::Result<()> {
        self.stop.store(true, Ordering::Release);
        let thread = self
            .thread
            .lock()
            .map_err(|_| io::Error::other("online driver thread lock poisoned"))?
            .take();
        if let Some(thread) = thread {
            // Bounded join: every epoch phase is crash-safe at any
            // interruption point, so a slow phase never stalls shutdown.
            let deadline = std::time::Instant::now() + DRIVER_STOP_JOIN_BUDGET;
            loop {
                if thread.is_finished() {
                    break;
                }
                if std::time::Instant::now() >= deadline {
                    // Detach: the thread exits at its next phase boundary
                    // and takes nothing the shutdown path still needs.
                    return Ok(());
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            thread
                .join()
                .map_err(|_| io::Error::other("online backup driver panicked"))?;
        }
        Ok(())
    }
}

fn now_unix_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis() as u64)
        .unwrap_or(0)
}

/// Drain the write overlay in bounded chunks before the anchor hold. Each
/// chunk takes one short `try_lock` hold (the identical slice the
/// maintenance drain commits); contention or a stop request defers the
/// epoch as routine `WouldBlock`, which the driver's transient-retry loop
/// absorbs before it ever reaches the scheduler.
fn drain_overlay_before_anchor(
    database: &Arc<Mutex<AuthorityDatabase>>,
    stop: &AtomicBool,
) -> io::Result<()> {
    drain_overlay_before_anchor_bounded(database, stop, MAX_ANCHOR_DRAIN_CHUNKS)
}

/// Bounded form: `max_chunks` caps one epoch's drain work so foreground
/// writes outpacing the drain defer the epoch like any other contention.
fn drain_overlay_before_anchor_bounded(
    database: &Arc<Mutex<AuthorityDatabase>>,
    stop: &AtomicBool,
    max_chunks: u32,
) -> io::Result<()> {
    let mut chunks = 0_u32;
    loop {
        if stop.load(Ordering::Acquire) {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "backup driver stopping",
            ));
        }
        {
            let mut guard = database.try_lock().map_err(|_| {
                io::Error::new(io::ErrorKind::WouldBlock, "backup anchor drain contended")
            })?;
            if guard.entity_overlay_soft_trigger().below_soft_thresholds() {
                return Ok(());
            }
            match guard.merge_entity_overlay() {
                Ok(true) => chunks += 1,
                // An empty/disabled overlay answers false; nothing to drain.
                Ok(false) => return Ok(()),
                // Rotation backpressure owns the WAL lane briefly; defer
                // the epoch rather than queue a long hold behind it.
                Err(error) if error.kind() == io::ErrorKind::ResourceBusy => {
                    return Err(io::Error::new(
                        io::ErrorKind::WouldBlock,
                        "backup anchor drain waited out rotation",
                    ));
                }
                Err(error) => return Err(error),
            }
        }
        if chunks >= max_chunks {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "backup anchor drain exceeded its epoch chunk budget",
            ));
        }
        std::thread::sleep(ANCHOR_DRAIN_CHUNK_INTERVAL);
    }
}

fn run_epoch(
    database: &Arc<Mutex<AuthorityDatabase>>,
    context: &EpochContext,
    stop: &AtomicBool,
) -> io::Result<OnlineBackupEpochReport> {
    if stop.load(Ordering::Acquire) {
        return Err(io::Error::new(io::ErrorKind::WouldBlock, "backup driver stopping"));
    }
    drain_overlay_before_anchor(database, stop)?;
    let capture = database
        .try_lock()
        .map_err(|_| io::Error::new(io::ErrorKind::WouldBlock, "backup anchor hold contended"))?
        .capture_backup_anchor()?;
    let result = run_epoch_off_mutex(&capture, context, stop);
    {
        let database_guard = database
            .lock()
            .map_err(|_| io::Error::other("authority mutex poisoned"))?;
        database_guard.deregister_backup_anchor(capture.anchor_token);
    }
    result
}

fn run_epoch_off_mutex(
    capture: &BackupAnchorCapture,
    context: &EpochContext,
    stop: &AtomicBool,
) -> io::Result<OnlineBackupEpochReport> {
    let (destination, snapshots_path) =
        backup::initialize_or_open_backup(&context.backup_dir, Arc::clone(&context.destination_vfs))?;
    // The lease scopes only the copy+publish phase: prune acquires the same
    // path-locked BACKUP_LOCK internally and would conflict with our hold.
    let copy_result = {
        let _lease =
            backup::acquire_backup_lease(&context.backup_dir, context.destination_vfs.as_ref())?;
        backup::ensure_no_prune(&context.backup_dir, context.destination_vfs.as_ref())?;
        run_copy_phase(capture, context, stop, &destination, &snapshots_path)
    };
    let (metrics, manifest_path) = copy_result?;
    let prune = backup::prune_backup_with_vfs(
        &context.backup_dir,
        context.retention,
        Arc::clone(&context.destination_vfs),
    )?;
    let estimated_bytes = backup::bounded_directory_bytes_with_vfs(
        &context.backup_dir,
        context.destination_vfs.as_ref(),
    )?;
    let manifest_sha256 = sha256_file(&manifest_path, context.destination_vfs.as_ref())?;
    Ok(OnlineBackupEpochReport {
        ok: true,
        backend: "tofudb",
        artifact_kind: "directory",
        backup: context.backup_dir.to_string_lossy().into_owned(),
        manifest: manifest_path.to_string_lossy().into_owned(),
        bytes: metrics.copied_block_bytes + metrics.manifest_bytes,
        sha256: manifest_sha256,
        durable_sequence: metrics.durable_sequence,
        copied_blocks: metrics.copied_blocks,
        copied_block_bytes: metrics.copied_block_bytes,
        manifest_bytes: metrics.manifest_bytes,
        estimated_bytes,
        retained_generations: prune.retained_generations,
        pruned_generations: prune.removed_generations,
        pruned_block_bytes: prune.removed_block_bytes,
    })
}

fn run_copy_phase(
    capture: &BackupAnchorCapture,
    context: &EpochContext,
    stop: &AtomicBool,
    destination: &crate::block::BlockStore,
    snapshots_path: &Path,
) -> io::Result<(BackupMetrics, PathBuf)> {
    let existing = backup::existing_manifests(snapshots_path, context.destination_vfs.as_ref())?;
    if existing
        .iter()
        .any(|candidate| candidate.authority_uuid != capture.manifest.authority_uuid)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "backup directory belongs to another authority",
        ));
    }
    if context._fastpath_lease.is_some() {
        if let Some(previous) = existing.iter().find(|manifest|
            manifest.durable_sequence == capture.manifest.durable_sequence
                && manifest.root_hash == capture.manifest.root_hash) {
            // A same-lineage committed sequence is already recoverable. Local
            // restore/drain may change physical witnesses without a new commit;
            // never replace the immutable shadow manifest at that identity.
            return Ok((BackupMetrics { durable_sequence: previous.durable_sequence,
                ..BackupMetrics::default() }, snapshots_path.join(previous.file_name())));
        }
    }
    if existing.len() == backup::MAX_BACKUP_GENERATIONS
        && !existing.iter().any(|candidate| candidate == &capture.manifest)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "backup generation limit reached",
        ));
    }
    if stop.load(Ordering::Acquire) {
        return Err(io::Error::new(io::ErrorKind::WouldBlock, "backup driver stopping"));
    }
    let before = destination.write_metrics();
    backup::copy_reachable_blocks(&capture.source, &destination, &capture.manifest)?;
    let after = destination.write_metrics();
    if stop.load(Ordering::Acquire) {
        return Err(io::Error::new(io::ErrorKind::WouldBlock, "backup driver stopping"));
    }
    let manifest_bytes = backup::publish_manifest(
        snapshots_path,
        &capture.manifest,
        context.destination_vfs.as_ref(),
    )?;
    let metrics: BackupMetrics = BackupMetrics {
        durable_sequence: capture.manifest.durable_sequence,
        copied_blocks: after.blocks_written - before.blocks_written,
        copied_block_bytes: after.bytes_written - before.bytes_written,
        manifest_bytes,
    };
    let manifest_path = snapshots_path.join(capture.manifest.file_name());
    Ok((metrics, manifest_path))
}

fn sha256_file(path: &Path, vfs: &dyn Vfs) -> io::Result<String> {
    use sha2::Digest;
    let mut file = vfs.open(
        path,
        crate::vfs::OpenRequest {
            read: true,
            ..crate::vfs::OpenRequest::default()
        },
    )?;
    let bytes = file.read_all(4096)?;
    let digest = sha2::Sha256::digest(&bytes);
    let mut encoded = String::with_capacity(digest.len() * 2);
    for byte in digest {
        use std::fmt::Write as _;
        let _ = write!(encoded, "{byte:02x}");
    }
    Ok(encoded)
}

#[cfg(test)]
mod tests;
