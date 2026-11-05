//! Online, epoch-based physical garbage collection (docs/TOFUDB_ONLINE_GC.md).
//!
//! The daemon runs this collector on a dedicated thread. Each epoch takes a
//! frozen mark snapshot under a short authority hold, builds marks and scans
//! for suspects off-mutex, and deletes only what two consecutive marks prove
//! dead, what the relink fence shows untouched, and what the catalog still
//! confirms. Every long operation (graph traversal, directory scans, segment
//! rebuild, fsync chains) happens outside the authority mutex; holds are
//! O(in-memory capture), O(one bounded publish) or one loose-file unlink.

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use uuid::Uuid;

use crate::authority::{AuthorityDatabase, OnlineGcCapture};
use crate::authority_gc::{
    AuthorityGarbageCollectionBudget, MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED,
    MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS,
};
use crate::backup::visit_semantic_value_references;
use crate::backup_gc::SpillMarkSet;
use crate::block::{
    BlockId, BlockRemovalMetrics, BlockStore, BlockTemporaryFileCandidate, LooseRemoval,
};
use crate::engine::OnlineGcMarkSnapshot;
use crate::entity::visit_entity_page_graphs_once;
use crate::history::decode_segment;
use crate::payload_manifest::{PayloadSegmentReference, MAX_PAYLOAD_SEGMENTS};
use crate::payload_segment::{
    PayloadSegment, PayloadSegmentOrphanCandidate, MAX_SEGMENT_BLOCKS, MAX_SEGMENT_PAYLOAD_BYTES,
};
use crate::vfs::{sync_directory_barrier, FileKind, Vfs};

/// Two consecutive marks must both prove a suspect dead before deletion;
/// this floor between mark snapshots bounds how long a staged-but-
/// uncommitted write can straddle the two scans (rule S1/S4). Any ordinary
/// transaction or upload staging completes far below this interval.
pub const ONLINE_GC_MIN_EPOCH_INTERVAL: Duration = Duration::from_secs(120);
/// Segment dead-set memory kept across one epoch boundary for two-mark
/// confirmation; segments beyond the cap simply confirm in a later epoch.
const MAX_TRACKED_SEGMENT_DEAD_SETS: usize = 64;
/// Idle backoff cap after an epoch that reclaimed nothing.
const ONLINE_GC_MAX_IDLE_INTERVAL: Duration = Duration::from_secs(3600);
/// Victim cap for one pressure consolidation merge.
const PRESSURE_CONSOLIDATION_VICTIMS: usize = 8;

const WORK_DIRECTORY: &str = "authority-gc/online";
const MARKS_DIRECTORY: &str = "marks";
const SUSPECTS_DIRECTORIES: [&str; 2] = ["suspects-0", "suspects-1"];

fn would_block(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message)
}

fn ensure_directory(path: &Path, parent: &Path, vfs: &dyn Vfs) -> io::Result<()> {
    match vfs.metadata(path) {
        Ok(FileKind::Directory) => Ok(()),
        Ok(FileKind::File) => Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "online GC work path is a file",
        )),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            vfs.create_dir(path)?;
            sync_directory_barrier(vfs, parent)
        }
        Err(error) => Err(error),
    }
}

/// Cumulative online-collector state surfaced through `system.metrics` and
/// `system.reclaim`. All values are process-local.
#[derive(Clone, Debug, Default, Eq, PartialEq, serde::Serialize)]
pub struct OnlineGarbageCollectionMetrics {
    pub epochs_completed: u64,
    pub marks_built: u64,
    pub deferred_epochs: u64,
    pub epoch_in_progress: bool,
    pub last_epoch_duration_ms: u64,
    pub last_epoch_reclaimed_bytes: u64,
    pub total_reclaimed_bytes: u64,
    pub total_reclaimed_blocks: u64,
    pub total_temporary_files_removed: u64,
    pub total_orphan_segments_removed: u64,
    pub total_segments_rewritten: u64,
    pub total_segments_retired: u64,
    pub total_compacted_blocks: u64,
    /// Payload segments loose-block compaction published.
    pub total_compaction_segments: u64,
    /// Loose copies of live blocks the scan found already catalogued, removed
    /// one hold each (compaction's own copies are not counted here).
    pub total_redundant_copies_removed: u64,
    pub total_consolidations: u64,
    pub total_consolidated_victims: u64,
    /// Segment-lane passes that ended with an error other than a stop or
    /// deferral; block marks still ran in those epochs.
    pub segment_lane_failures: u64,
    /// The latest such error, cleared by the next pass that completes.
    pub segment_lane_error: Option<String>,
    /// Epochs that ended with an error other than a stop; `deferred_epochs`
    /// counts them too.
    pub failed_epochs: u64,
    /// The latest such error, cleared by the next epoch that completes.
    pub epoch_error: Option<String>,
}

/// What the driver loop learns from one `run_epoch` call.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OnlineGarbageCollectionOutcome {
    /// A full epoch finished (possibly reclaiming nothing).
    EpochComplete,
    /// The epoch could not start or finish; the collector already scheduled
    /// the next attempt. Contention is always resolved in the foreground's
    /// favour.
    Deferred,
}

/// One epoch's off-mutex working state.
struct Epoch {
    started: Instant,
    capture: OnlineGcCapture,
    marks: Option<SpillMarkSet>,
    suspects_directory: PathBuf,
    suspects_next: Option<SpillMarkSet>,
    temp_candidate_next: Option<PathBuf>,
    segment_dead_next: BTreeMap<Uuid, Vec<BlockId>>,
    confirmed_loose: Vec<BlockId>,
    /// Every block whose file this epoch's loose deletion hold removed, or
    /// may have removed.
    unlinked_loose: Vec<BlockId>,
    confirmed_temp: Option<BlockTemporaryFileCandidate>,
    rewrite_victim: Option<(PayloadSegmentReference, Vec<BlockId>)>,
    /// The open compaction segment: at most one segment's blocks, because
    /// the scan packs each segment the format limits close before admitting
    /// the next block.
    compaction_ids: Vec<BlockId>,
    /// An error other than a stop that ended compaction during the scan;
    /// reported after the deletion holds, like a phase-4 compaction error.
    compaction_failure: Option<io::Error>,
    /// An error other than a stop that ended the scan's removal of redundant
    /// loose copies; reported after the deletion holds as well.
    copy_failure: Option<io::Error>,
    confirmed_loose_planned: bool,
    rewrite_planned: bool,
    compaction_planned: bool,
    tracked_suspects: u64,
    reclaimed_bytes: u64,
    reclaimed_blocks: u64,
    temporary_files_removed: u64,
    segments_rewritten: u64,
    segments_retired: u64,
    compacted_blocks: u64,
    compaction_segments: u64,
    redundant_copies_removed: u64,
    consolidations: u64,
    consolidated_victims: u64,
    consolidation: Option<ConsolidationPlan>,
    pressure_candidates: Vec<(PayloadSegmentReference, Vec<BlockId>, usize)>,
}

/// What one completed mark walked and recorded; logged once per mark
/// (`online_gc` `step=mark_built`).
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct MarkShape {
    pub roots: usize,
    /// Distinct entity pages walked, each read once.
    pub walked_pages: usize,
    /// Page references not followed because an earlier walk covered them.
    pub skipped_pages: u64,
    /// Mark inserts, duplicates included: the unit the spill budget counts.
    pub references: u64,
    pub spill_runs: u32,
    pub spill_bytes: u64,
}

/// What the mark-independent segment lane did in one epoch.
#[derive(Default)]
struct SegmentReclaim {
    observed_orphans: usize,
    removed_orphans: u64,
    removed_bytes: u64,
    /// The error that ended this pass early, if any; marks still run.
    failure: Option<io::Error>,
}

/// One pressure-mode consolidation: several small/dead-heavy segments
/// merge into a single replacement, freeing catalog slots.
struct ConsolidationPlan {
    victims: Vec<PayloadSegmentReference>,
    retained_per_victim: Vec<Vec<BlockId>>,
}

/// Previous epoch's retained memory for two-mark confirmation (rule S1).
struct PreviousEpoch {
    suspects: Option<SpillMarkSet>,
    /// Sorted blocks a deletion hold unlinked, or may have unlinked, after
    /// `suspects` was observed. A later write can recreate such a block
    /// under the same content-addressed name without any relink decision,
    /// so `suspects` never confirms them (rule S10).
    unlinked_loose: Vec<BlockId>,
    temp_candidate: Option<PathBuf>,
    /// Orphan segment files (name -> size) seen by the last segment lane
    /// observation; marks play no part in their confirmation.
    orphan_observation: BTreeMap<String, u64>,
    segment_dead: BTreeMap<Uuid, Vec<BlockId>>,
}

/// One run of single-file deletion holds (docs/TOFUDB_ONLINE_GC.md, "One
/// file per deletion hold"). Each hold validates and unlinks one loose file.
/// Off the mutex, a shard's directory barrier runs once the run leaves that
/// shard, and every attempt waits until the collector has stayed off the
/// mutex at least as long as its previous hold took: a run holds the
/// authority at most half of its time, the collector's 50% duty-cycle rule
/// applied to its holds. A retry after a contended authority or a refused
/// (`WouldBlock`) hold also waits at least as long as the run has already
/// been contended or refused, so retries back off geometrically from the
/// loop's own cost, even before the run measured any hold.
struct SingleFileHolds<'a> {
    blocks: &'a BlockStore,
    /// A block of the shard whose unlinks no barrier made durable yet.
    unsynced: Option<BlockId>,
    previous_hold: Duration,
    released: Instant,
    /// When the run's current streak of contended or refused attempts began.
    retrying_since: Option<Instant>,
    removed: BlockRemovalMetrics,
}

impl<'a> SingleFileHolds<'a> {
    fn new(blocks: &'a BlockStore, previous_hold: Duration) -> Self {
        Self {
            blocks,
            unsynced: None,
            previous_hold,
            released: Instant::now(),
            retrying_since: None,
            removed: BlockRemovalMetrics::default(),
        }
    }

    /// The barrier of the shard the run leaves before `next`.
    fn sync_left_shard(&mut self, next: BlockId) -> io::Result<()> {
        match self.unsynced {
            Some(unsynced) if unsynced.0[0] != next.0[0] => self.finish(),
            _ => Ok(()),
        }
    }

    /// Runs `operation` under one short hold, after the run's pause. `None`
    /// when the authority was contended: the caller retries, and the retry
    /// waits at least as long as the run has been contended or refused.
    /// Every collector hold that waits for the authority goes through here.
    fn attempt<D: OnlineGcDatabase, T>(
        &mut self,
        database: &Arc<Mutex<D>>,
        operation: impl FnOnce(&mut AuthorityDatabase) -> io::Result<T>,
    ) -> Option<io::Result<T>> {
        let duty = self.previous_hold.saturating_sub(self.released.elapsed());
        let backoff = self.retrying_since.map_or(Duration::ZERO, |since| since.elapsed());
        let pause = duty.max(backoff);
        if !pause.is_zero() {
            std::thread::sleep(pause);
        }
        #[cfg(test)]
        tests_short_holds::before_single_file_hold(self.previous_hold, self.released.elapsed());
        let Ok(mut database_guard) = database.try_lock() else {
            self.released = Instant::now();
            self.retrying_since.get_or_insert(self.released);
            return None;
        };
        let acquired = Instant::now();
        let result = database_guard.gc_database_mut().and_then(operation);
        drop(database_guard);
        self.released = Instant::now();
        let held = self.released.saturating_duration_since(acquired);
        // A refused hold is retried only by runs that wait for a publication
        // to move both CONTROL slots, so it backs off like contention.
        match &result {
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                self.retrying_since.get_or_insert(self.released);
            }
            _ => self.retrying_since = None,
        }
        // A hold that failed never shortens the pause, so a retried
        // `WouldBlock` never polls faster than the run's unlinks.
        self.previous_hold = if result.is_ok() { held } else { self.previous_hold.max(held) };
        Some(result)
    }

    /// Runs `remove` for `block_id` under one short hold (`attempt`).
    fn remove<D: OnlineGcDatabase>(
        &mut self,
        database: &Arc<Mutex<D>>,
        block_id: BlockId,
        remove: impl FnOnce(&mut AuthorityDatabase) -> io::Result<LooseRemoval>,
    ) -> Option<io::Result<()>> {
        let removal = self.attempt(database, remove)?;
        Some(removal.map(|removal| match removal {
            LooseRemoval::Kept => {}
            LooseRemoval::Missing => self.removed.blocks_removed += 1,
            LooseRemoval::Unlinked { bytes } => {
                self.removed.blocks_removed += 1;
                self.removed.bytes_removed += bytes;
                self.unsynced = Some(block_id);
            }
        }))
    }

    /// The barrier of the last shard the run unlinked in. A crash before a
    /// barrier can only bring back a file that is garbage or whose block a
    /// published segment holds.
    fn finish(&mut self) -> io::Result<()> {
        match self.unsynced.take() {
            Some(unsynced) => self.blocks.sync_block_shard(unsynced),
            None => Ok(()),
        }
    }
}

/// Publishes one off-mutex-built catalog change (consolidation or segment
/// rewrite) under one short hold. A contended authority is waited for with
/// the backoff of every other collector hold instead of failing the epoch
/// and stranding the built segment. The publication's own `WouldBlock`
/// (physical lane busy, catalog moved, relink fence) and a stop both return
/// `WouldBlock`, which proves nothing was published: the caller discards
/// the built segment.
fn publish_under_hold<D: OnlineGcDatabase, T>(
    database: &Arc<Mutex<D>>,
    blocks: &BlockStore,
    stop: &AtomicBool,
    mut publish: impl FnMut(&mut AuthorityDatabase) -> io::Result<T>,
) -> io::Result<T> {
    let mut holds = SingleFileHolds::new(blocks, Duration::ZERO);
    loop {
        if stop.load(Ordering::Acquire) {
            return Err(would_block("online GC epoch stopped"));
        }
        if let Some(published) = holds.attempt(database, &mut publish) {
            return published;
        }
    }
}

/// The online collector. One instance per daemon, driven by a dedicated
/// thread; all state is collector-local except the shared metrics.
/// First-epoch grace after daemon start: startup is the latency-sensitive
/// phase, and there can be no meaningful garbage in the first minute.
pub const ONLINE_GC_BOOT_GRACE: Duration = Duration::from_secs(30);

/// A database protected by the owner's existing mutex. Projection and source
/// share the collector algorithm, while every mark and delete stays local.
pub trait OnlineGcDatabase: Send + 'static {
    fn gc_database(&self) -> io::Result<&AuthorityDatabase>;
    fn gc_database_mut(&mut self) -> io::Result<&mut AuthorityDatabase>;
    fn gc_budget(&self, budget: AuthorityGarbageCollectionBudget) -> io::Result<AuthorityGarbageCollectionBudget> {
        Ok(budget)
    }
    fn install_gc_handle(&mut self, handle: OnlineGarbageCollectionHandle) -> io::Result<()>;
}
impl OnlineGcDatabase for AuthorityDatabase {
    fn gc_database(&self) -> io::Result<&AuthorityDatabase> { Ok(self) }
    fn gc_database_mut(&mut self) -> io::Result<&mut AuthorityDatabase> { Ok(self) }
    fn install_gc_handle(&mut self, handle: OnlineGarbageCollectionHandle) -> io::Result<()> {
        self.set_online_garbage_collection(handle); Ok(())
    }
}

pub struct OnlineGarbageCollector {
    budget: AuthorityGarbageCollectionBudget,
    catalog_pressure_floor: usize,
    catalog_expedite_floor: usize,
    metrics: Arc<RwLock<OnlineGarbageCollectionMetrics>>,
    expedite: Arc<AtomicBool>,
    previous: PreviousEpoch,
    suspects_parity: usize,
    // Catalog traversal is collector-owned: idle authorities do not change
    // generation. Each window receives two completed epochs so previous-dead
    // confirmation stays consecutive; failed/cancelled attempts do not advance.
    payload_window_cursor: Option<usize>,
    payload_window_second_epoch: bool,
    next_epoch_not_before: Instant,
    min_epoch_interval: Duration,
    idle_interval: Duration,
    /// Hard pacing state (2026-09-21 FUSE brownout): doorbells and volume
    /// pressure may accelerate the idle backoff, but no path may start
    /// epochs faster than `min_epoch_interval` by start time, and the
    /// collector never exceeds a 50% duty cycle. On a degraded mount an
    /// unthrottled epoch train is a self-inflicted denial of service
    /// against the foreground (the app rings `system.reclaim` every 30 s
    /// while reclaim reports a backlog, and one full-store epoch takes
    /// minutes over FUSE — without this floor the collector ran
    /// back-to-back epochs indefinitely).
    last_epoch_started: Option<Instant>,
    last_epoch_ended: Option<Instant>,
    last_epoch_duration: Duration,
}

/// Cloneable doorbell + read model for the daemon's online collector.
/// `system.reclaim` rings the doorbell; `system.health`/`system.metrics`
/// read the metrics. Neither ever blocks on physical work.
#[derive(Clone)]
pub struct OnlineGarbageCollectionHandle {
    metrics: Arc<RwLock<OnlineGarbageCollectionMetrics>>,
    expedite: Arc<AtomicBool>,
}

impl OnlineGarbageCollectionHandle {
    pub fn metrics(&self) -> OnlineGarbageCollectionMetrics {
        self.metrics
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }

    pub fn request_expedited_epoch(&self) {
        self.expedite.store(true, Ordering::Release);
    }
}

impl OnlineGarbageCollector {
    pub fn new(budget: AuthorityGarbageCollectionBudget) -> Self {
        Self::with_min_epoch_interval(budget, ONLINE_GC_MIN_EPOCH_INTERVAL)
    }

    /// Collector with an explicit two-mark snapshot floor. The 120 s
    /// default bounds how long staged-but-uncommitted writes can straddle
    /// the two scans (rule S4); tests shrink it to keep epochs observable.
    pub fn with_min_epoch_interval(
        budget: AuthorityGarbageCollectionBudget,
        min_epoch_interval: Duration,
    ) -> Self {
        Self {
            budget,
            catalog_pressure_floor: MAX_PAYLOAD_SEGMENTS * 75 / 100,
            catalog_expedite_floor: (MAX_PAYLOAD_SEGMENTS * 90).div_ceil(100),
            metrics: Arc::new(RwLock::new(OnlineGarbageCollectionMetrics::default())),
            expedite: Arc::new(AtomicBool::new(false)),
            previous: PreviousEpoch {
                suspects: None,
                unlinked_loose: Vec::new(),
                temp_candidate: None,
                orphan_observation: BTreeMap::new(),
                segment_dead: BTreeMap::new(),
            },
            suspects_parity: 0,
            payload_window_cursor: None,
            payload_window_second_epoch: false,
            next_epoch_not_before: Instant::now(),
            min_epoch_interval,
            idle_interval: min_epoch_interval,
            last_epoch_started: None,
            last_epoch_ended: None,
            last_epoch_duration: Duration::ZERO,
        }
    }

    /// Defers the first epoch (daemon boot grace): startup is the
    /// latency-sensitive phase and a fresh authority has no garbage.
    pub(crate) fn defer_first_epoch(&mut self, grace: Duration) {
        self.next_epoch_not_before = Instant::now() + grace;
    }

    pub fn handle(&self) -> OnlineGarbageCollectionHandle {
        OnlineGarbageCollectionHandle {
            metrics: Arc::clone(&self.metrics),
            expedite: Arc::clone(&self.expedite),
        }
    }

    /// `system.reclaim` asks for the next epoch to start as soon as the
    /// driver notices; it never waits for physical work.
    pub fn request_expedited_epoch(&self) {
        self.expedite.store(true, Ordering::Release);
    }

    /// Whether the driver should start an epoch now. Volume pressure is
    /// evaluated by the driver and passed here directly. Whatever the
    /// trigger — doorbell, pressure, or idle timer — the hard pacing floor
    /// applies: epoch starts are at least `min_epoch_interval` apart and
    /// never closer than the previous epoch's own duration (≤50% duty).
    pub fn should_run_now(&mut self, volume_pressure: bool) -> bool {
        let now = Instant::now();
        let expedited = self.expedite.swap(false, Ordering::AcqRel);
        if !(volume_pressure || expedited || now >= self.next_epoch_not_before) {
            return false;
        }
        if let Some(started) = self.last_epoch_started {
            if now.duration_since(started) < self.min_epoch_interval {
                return false;
            }
        }
        if let Some(ended) = self.last_epoch_ended {
            if now < ended + self.last_epoch_duration {
                return false;
            }
        }
        true
    }

    fn set_in_progress(&self, in_progress: bool) {
        if let Ok(mut metrics) = self.metrics.write() {
            metrics.epoch_in_progress = in_progress;
        }
    }

    /// Segment-lane removals count immediately: they stand even when the
    /// block mark of the same epoch later aborts.
    fn note_segment_reclaim(&self, reclaim: &SegmentReclaim) {
        if let Ok(mut metrics) = self.metrics.write() {
            metrics.total_orphan_segments_removed += reclaim.removed_orphans;
            metrics.total_reclaimed_bytes += reclaim.removed_bytes;
        }
    }

    /// Records how the segment lane's pass ended. A failure counts and stays
    /// visible until a later pass completes; a log line marks each change of
    /// the reported error, so a persistent failure is logged once, not once
    /// per epoch.
    fn note_segment_lane_outcome(&self, failure: Option<&io::Error>) {
        let error = failure.map(ToString::to_string);
        let changed = {
            let mut metrics = self
                .metrics
                .write()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            metrics.segment_lane_failures += u64::from(error.is_some());
            let changed = metrics.segment_lane_error != error;
            metrics.segment_lane_error = error.clone();
            changed
        };
        match (changed, failure) {
            (true, Some(failure)) => crate::engine_log::warn(
                "online_gc",
                &format!(
                    "segment lane failed; block marks continue: kind={:?} error={failure}",
                    failure.kind()
                ),
            ),
            (true, None) => crate::engine_log::info("online_gc", "segment lane recovered"),
            (false, _) => {}
        }
    }

    /// Records an epoch that ended with an error other than a stop. The error
    /// stays in `epoch_error` until an epoch completes; a log line marks each
    /// change of it, so an epoch that keeps failing the same way logs once.
    fn note_epoch_failure(&self, error: &io::Error) {
        let message = format!("kind={:?} error={error}", error.kind());
        let changed = {
            let mut metrics = self
                .metrics
                .write()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            metrics.failed_epochs += 1;
            let changed = metrics.epoch_error.as_deref() != Some(message.as_str());
            metrics.epoch_error = Some(message.clone());
            changed
        };
        if changed {
            crate::engine_log::warn("online_gc", &format!("epoch failed; the next epoch retries: {message}"));
        }
    }

    fn note_epoch_finished(&self, duration: Duration, epoch: &Epoch, segment_reclaim: &SegmentReclaim) {
        crate::engine_log::info("online_gc", &format!(
            "step=epoch_complete elapsed_ms={} reclaimed_bytes={} reclaimed_blocks={} orphan_segments_removed={} \
             segments_rewritten={} segments_retired={} consolidations={} compacted_blocks={} redundant_copies_removed={}",
            duration.as_millis(),
            epoch.reclaimed_bytes + segment_reclaim.removed_bytes,
            epoch.reclaimed_blocks,
            segment_reclaim.removed_orphans,
            epoch.segments_rewritten,
            epoch.segments_retired,
            epoch.consolidations,
            epoch.compacted_blocks,
            epoch.redundant_copies_removed,
        ));
        if let Ok(mut metrics) = self.metrics.write() {
            if metrics.epoch_error.take().is_some() {
                crate::engine_log::info("online_gc", "epoch completed after failed epochs");
            }
            metrics.epochs_completed += 1;
            metrics.marks_built += 1;
            metrics.epoch_in_progress = false;
            metrics.last_epoch_duration_ms = duration.as_millis() as u64;
            metrics.last_epoch_reclaimed_bytes = epoch.reclaimed_bytes + segment_reclaim.removed_bytes;
            metrics.total_reclaimed_bytes += epoch.reclaimed_bytes;
            metrics.total_reclaimed_blocks += epoch.reclaimed_blocks;
            metrics.total_temporary_files_removed += epoch.temporary_files_removed;
            metrics.total_segments_rewritten += epoch.segments_rewritten;
            metrics.total_segments_retired += epoch.segments_retired;
            metrics.total_compacted_blocks += epoch.compacted_blocks;
            metrics.total_compaction_segments += epoch.compaction_segments;
            metrics.total_redundant_copies_removed += epoch.redundant_copies_removed;
            metrics.total_consolidations += epoch.consolidations;
            metrics.total_consolidated_victims += epoch.consolidated_victims;
        }
    }

    fn note_deferred(&self) {
        if let Ok(mut metrics) = self.metrics.write() {
            metrics.deferred_epochs += 1;
            metrics.epoch_in_progress = false;
        }
    }

    /// Runs one full epoch. Short authority holds are taken through
    /// `database`; everything expensive runs off-mutex on the caller's
    /// thread. `stop` requests prompt, safe abandonment between phases.
    pub fn run_epoch<D: OnlineGcDatabase>(
        &mut self,
        database: &Arc<Mutex<D>>,
        stop: &AtomicBool,
    ) -> io::Result<OnlineGarbageCollectionOutcome> {
        let started = Instant::now();
        self.last_epoch_started = Some(started);
        self.set_in_progress(true);
        let launch_budget = self.budget;
        let outcome = self.run_epoch_inner(database, stop);
        self.budget = launch_budget;
        if let Err(error) = &outcome {
            if !stop.load(Ordering::Acquire) {
                self.note_epoch_failure(error);
            }
        }
        let ended = Instant::now();
        self.last_epoch_ended = Some(ended);
        self.last_epoch_duration = ended.saturating_duration_since(started);
        match outcome {
            Ok((OnlineGarbageCollectionOutcome::EpochComplete, found_work)) => {
                if found_work {
                    self.idle_interval = self.min_epoch_interval;
                } else {
                    self.idle_interval =
                        (self.idle_interval * 2).min(ONLINE_GC_MAX_IDLE_INTERVAL);
                }
                self.next_epoch_not_before = ended + self.idle_interval;
            }
            Ok((OnlineGarbageCollectionOutcome::Deferred, _)) | Err(_) => {
                self.idle_interval = self.min_epoch_interval;
                self.next_epoch_not_before = ended + self.idle_interval;
            }
        }
        // The 50% duty bound applies to every later trigger, including
        // doorbells and volume pressure (see should_run_now).
        let duty_floor = ended + self.last_epoch_duration;
        if self.next_epoch_not_before < duty_floor {
            self.next_epoch_not_before = duty_floor;
        }
        self.set_in_progress(false);
        outcome.map(|(outcome, _)| outcome)
    }

    fn run_epoch_inner<D: OnlineGcDatabase>(
        &mut self,
        database: &Arc<Mutex<D>>,
        stop: &AtomicBool,
    ) -> io::Result<(OnlineGarbageCollectionOutcome, bool)> {
        if stop.load(Ordering::Acquire) {
            self.note_deferred();
            return Ok((OnlineGarbageCollectionOutcome::Deferred, false));
        }
        // Phase 1: snapshot hold. O(in-memory capture) plus the fence re-arm.
        let (capture, orphan_observation_inputs) = {
            let database_guard = match database.try_lock() {
                Ok(guard) => guard,
                Err(_) => {
                    self.note_deferred();
                    return Ok((OnlineGarbageCollectionOutcome::Deferred, false));
                }
            };
            let captured = database_guard.gc_budget(self.budget).and_then(|budget| {
                self.budget = budget;
                let database = database_guard.gc_database()?;
                let orphan_observation_inputs = database.online_gc_orphan_observation_inputs()?;
                // Last: a successful capture opens the relink window, which
                // every later path of this epoch closes.
                Ok((
                    database.online_gc_capture(self.budget.maximum_temporary_bytes)?,
                    orphan_observation_inputs,
                ))
            });
            match captured {
                Ok(captured) => captured,
                Err(error) => {
                    self.note_deferred();
                    return Err(error);
                }
            }
        };
        let suspects_directory = capture
            .data_dir
            .join(WORK_DIRECTORY)
            .join(SUSPECTS_DIRECTORIES[self.suspects_parity]);
        let mut epoch = Epoch {
            started: Instant::now(),
            capture,
            marks: None,
            suspects_directory,
            suspects_next: None,
            temp_candidate_next: None,
            segment_dead_next: BTreeMap::new(),
            confirmed_loose: Vec::new(),
            unlinked_loose: Vec::new(),
            confirmed_temp: None,
            rewrite_victim: None,
            compaction_ids: Vec::new(),
            compaction_failure: None,
            copy_failure: None,
            confirmed_loose_planned: false,
            rewrite_planned: false,
            compaction_planned: false,
            tracked_suspects: 0,
            reclaimed_bytes: 0,
            reclaimed_blocks: 0,
            temporary_files_removed: 0,
            segments_rewritten: 0,
            segments_retired: 0,
            compacted_blocks: 0,
            compaction_segments: 0,
            redundant_copies_removed: 0,
            consolidations: 0,
            consolidated_victims: 0,
            consolidation: None,
            pressure_candidates: Vec::new(),
        };

        // Phase 1b: superseded segment files. Their proof is the catalog,
        // the protected sets and CONTROL, never a block mark, so this runs
        // before (and regardless of) the mark build. Only a stop or another
        // deferral abandons the epoch here; any other lane failure is
        // recorded and the block marks still run.
        let blocks = Arc::clone(&epoch.capture.blocks);
        let segment_reclaim =
            match self.reclaim_superseded_segments(database, &blocks, orphan_observation_inputs, stop) {
                Ok(reclaim) => reclaim,
                Err(error) => {
                    self.cleanup_epoch(&mut epoch);
                    self.note_deferred();
                    return Err(error);
                }
            };
        self.note_segment_lane_outcome(segment_reclaim.failure.as_ref());
        // Phase 2: mark build, off-mutex.
        if let Err(error) = self.build_marks(&mut epoch, stop) {
            self.cleanup_epoch(&mut epoch);
            self.note_deferred();
            return Err(error);
        }
        // Phase 3: scan + two-mark confirm, off-mutex.
        if let Err(error) = self.scan(database, &mut epoch, stop) {
            self.cleanup_epoch(&mut epoch);
            self.note_deferred();
            return Err(error);
        }
        // Phase 4: deletion holds, then the catalog size the epoch ends with.
        let deleted = self
            .delete_confirmed(database, &mut epoch, stop)
            .and_then(|()| blocks.installed_payload_manifest());
        epoch.capture.blocks.end_relink_observation();
        // The scan finished, so its suspects are a complete observation
        // whatever the holds did. The next epoch confirms from them and never
        // from an older set, whose entries this epoch's unlinks may have made
        // stale, minus exactly the blocks this epoch unlinked (rule S10).
        let mut unlinked_loose = std::mem::take(&mut epoch.unlinked_loose);
        unlinked_loose.sort_unstable();
        self.previous.suspects = epoch.suspects_next.take();
        self.previous.unlinked_loose = unlinked_loose;
        self.suspects_parity = 1 - self.suspects_parity;
        let ending_catalog_segments = match deleted {
            Ok(catalog) => catalog.map_or(0, |catalog| catalog.segments.len()),
            Err(error) => {
                self.cleanup_epoch(&mut epoch);
                self.note_deferred();
                return Err(error);
            }
        };

        // Phase 5: rotate epoch memory and publish metrics.
        self.previous.temp_candidate = epoch.temp_candidate_next.take();
        self.previous.segment_dead = std::mem::take(&mut epoch.segment_dead_next);
        let cursor = self.payload_window_cursor
            .unwrap_or(epoch.capture.snapshot.generation as usize);
        self.payload_window_cursor = Some(if self.payload_window_second_epoch {
            cursor.wrapping_add(1)
        } else {
            cursor
        });
        self.payload_window_second_epoch = !self.payload_window_second_epoch;
        if let Some(marks) = epoch.marks.take() {
            let _ = marks.cleanup();
        }
        // Suspects recorded this epoch confirm in the next one; keep the
        // minimum interval so two-mark confirmation is never starved. A
        // catalog that ends the epoch at the expedite threshold still has
        // consolidation work pending, whatever this epoch managed.
        let found_work = self.catalog_pressure_expedited(ending_catalog_segments)
            || epoch.reclaimed_bytes > 0
            || epoch.redundant_copies_removed > 0
            || epoch.temporary_files_removed > 0
            || segment_reclaim.removed_orphans > 0
            || segment_reclaim.observed_orphans > 0
            || epoch.segments_rewritten > 0
            || epoch.segments_retired > 0
            || epoch.compacted_blocks > 0
            || epoch.consolidations > 0
            || epoch.confirmed_loose_planned
            || epoch.rewrite_planned
            || epoch.compaction_planned
            || epoch.tracked_suspects > 0
            || !epoch.segment_dead_next.is_empty()
            || epoch.temp_candidate_next.is_some();
        self.note_epoch_finished(epoch.started.elapsed(), &epoch, &segment_reclaim);
        Ok((OnlineGarbageCollectionOutcome::EpochComplete, found_work))
    }

    fn cleanup_epoch(&self, epoch: &mut Epoch) {
        epoch.capture.blocks.end_relink_observation();
        if let Some(marks) = epoch.marks.take() {
            let _ = marks.cleanup();
        }
        if let Some(suspects) = epoch.suspects_next.take() {
            let _ = suspects.cleanup();
        }
    }

    /// Mark-independent segment lane (docs/TOFUDB_ONLINE_GC.md, "Superseded
    /// payload-segment reclamation independent of block marks"). Unlinks
    /// parked retirements whose reader floor expired, observes every orphan
    /// segment file (checking `stop` before each file operation), and
    /// removes each one the previous observation saw with the same name and
    /// size. Every removal re-validates the catalog, protected sets and both
    /// CONTROL slots under its own short authority hold; a contended or
    /// refused hold is retried after the run's backoff, so every confirmed
    /// file goes in this pass. One directory barrier makes the batch of
    /// unlinks durable.
    /// `Err` is a stop or another `WouldBlock`, which abandons the epoch; any
    /// other error ends only this pass and is returned in `failure`. A failed
    /// observation, removal or barrier clears the observation memory, so
    /// confirmation restarts from two fresh observations.
    fn reclaim_superseded_segments<D: OnlineGcDatabase>(
        &mut self,
        database: &Arc<Mutex<D>>,
        blocks: &BlockStore,
        (referenced_segment_ids, skip_temporary): (BTreeSet<Uuid>, bool),
        stop: &AtomicBool,
    ) -> io::Result<SegmentReclaim> {
        let previous = std::mem::take(&mut self.previous.orphan_observation);
        // Parked files are named by neither CONTROL slot; only the reader
        // floor protects them. Publications also sweep, but an idle or
        // mark-starved authority must not depend on another publication. A
        // failed unlink keeps its park for the next sweep and is reported.
        let mut reclaim = SegmentReclaim {
            failure: blocks
                .sweep_deferred_segment_retirements(crate::payload_segment::segment_retirement_delay())
                .err(),
            ..SegmentReclaim::default()
        };
        let Ok(store) = blocks.payload_segment_store() else {
            return Ok(reclaim);
        };
        let observed = match store.observe_orphan_files(
            &referenced_segment_ids,
            skip_temporary,
            || {
                if stop.load(Ordering::Acquire) {
                    return Err(would_block("online GC epoch stopped"));
                }
                Ok(())
            },
        ) {
            Ok(observed) => observed,
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => return Err(error),
            Err(error) => {
                reclaim.failure = Some(error);
                return Ok(reclaim);
            }
        };
        reclaim.observed_orphans = observed.len();
        let mut outcome = Ok(());
        // One paced run of single-file holds: a contended authority or a
        // refused hold (a publication owns the physical lane, or the CONTROL
        // slots are briefly apart) postpones the next attempt by the run's
        // backoff. Only a stop or an error ends the pass.
        let mut holds = SingleFileHolds::new(blocks, Duration::ZERO);
        'candidates: for candidate in observed
            .iter()
            .filter(|candidate| previous.get(&candidate.file_name) == Some(&candidate.file_bytes))
        {
            loop {
                if stop.load(Ordering::Acquire) {
                    outcome = Err(would_block("online GC epoch stopped"));
                    break 'candidates;
                }
                match holds.attempt(database, |database| database.online_gc_remove_observed_orphan(candidate)) {
                    Some(Ok(Some(bytes))) => {
                        reclaim.removed_orphans += 1;
                        reclaim.removed_bytes += bytes;
                        break;
                    }
                    Some(Ok(None)) => break,
                    Some(Err(error)) if error.kind() == io::ErrorKind::WouldBlock => {}
                    Some(Err(error)) => {
                        outcome = Err(error);
                        break 'candidates;
                    }
                    None => {}
                }
            }
        }
        if reclaim.removed_orphans > 0 {
            // A crash before this barrier can only resurrect unreferenced
            // files, which later observations collect again.
            let synced = store.sync_root();
            if outcome.is_ok() {
                outcome = synced;
            }
        }
        self.note_segment_reclaim(&reclaim);
        match outcome {
            Ok(()) => {
                self.previous.orphan_observation = observed
                    .into_iter()
                    .map(|candidate| (candidate.file_name, candidate.file_bytes))
                    .collect();
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => return Err(error),
            Err(error) => reclaim.failure = Some(error),
        }
        Ok(reclaim)
    }

    fn build_marks(&self, epoch: &mut Epoch, stop: &AtomicBool) -> io::Result<MarkShape> {
        let build_started = Instant::now();
        let selected_history_metadata = epoch.capture.snapshot.selected_history_metadata.take();
        let snapshot: &OnlineGcMarkSnapshot = &epoch.capture.snapshot;
        let blocks = Arc::clone(&epoch.capture.blocks);
        let gc_root = epoch.capture.data_dir.join("authority-gc");
        ensure_directory(&gc_root, &epoch.capture.data_dir, epoch.capture.vfs.as_ref())?;
        let work_directory = epoch.capture.data_dir.join(WORK_DIRECTORY);
        ensure_directory(&work_directory, &gc_root, epoch.capture.vfs.as_ref())?;
        let marks_directory = work_directory.join(MARKS_DIRECTORY);
        ensure_directory(&marks_directory, &work_directory, epoch.capture.vfs.as_ref())?;
        let marks_runs = marks_directory.join("gc-runs");
        ensure_directory(&marks_runs, &marks_directory, epoch.capture.vfs.as_ref())?;
        let mark_workspace_bytes = self.budget.maximum_temporary_bytes
            .checked_sub(snapshot.history_metadata_resident_bytes)
            .ok_or_else(|| would_block("GC history snapshot exceeds workspace budget"))?;
        let mut live = SpillMarkSet::initialize_with_spill_limit(
            &marks_directory,
            Arc::clone(&epoch.capture.vfs),
            mark_workspace_bytes,
        )?;
        let mark_result = (|| {
            // Each catalogued segment index is read once here, instead of a
            // witness walk plus a verified index open per block (TraversalReader).
            let stopped = || -> io::Result<()> {
                if stop.load(Ordering::Acquire) {
                    return Err(would_block("online GC epoch stopped"));
                }
                Ok(())
            };
            let reader = blocks.traversal_reader(stopped)?;
            let (located_blocks, unindexed_segments, table_bytes) = reader.location_table_shape();
            if table_bytes > 0 {
                crate::engine_log::info("online_gc", &format!(
                    "step=mark_location_table located_blocks={located_blocks} \
                     unindexed_segments={unindexed_segments} resident_bytes={table_bytes} elapsed_ms={}",
                    build_started.elapsed().as_millis(),
                ));
            }
            let live_cell = RefCell::new(&mut live);
            // The mark has no wall-clock budget (docs/TOFUDB_ONLINE_GC.md,
            // "Marks run to completion"): it holds nothing while it builds,
            // and a restart would repeat the same work. Each read may be a
            // network-volume round trip and each insert may flush a run, so
            // both observe the stop flag first.
            let mut roots = vec![snapshot.authority_state_root];
            roots.extend(snapshot.pinned_roots.iter().map(|root| Some(*root)));
            // Roots share almost every page: each page is read and marked
            // once, by the first root or pin walk that reaches it.
            let graphs = visit_entity_page_graphs_once(
                &roots,
                |block_id| {
                    stopped()?;
                    reader.get(block_id)
                },
                |block_id, _| {
                    stopped()?;
                    live_cell.borrow_mut().insert(block_id)
                },
                |key, value| {
                    visit_semantic_value_references(
                        key,
                        value,
                        |block_id| {
                            stopped()?;
                            reader.get(block_id)
                        },
                        |block_id| {
                            stopped()?;
                            live_cell.borrow_mut().insert(block_id)
                        },
                    )
                },
            )?;
            if let Some(manifest_id) = snapshot.history_manifest_block_id {
                live_cell.borrow_mut().insert(manifest_id)?;
            }
            if let Some(manifest_id) = snapshot.payload_manifest_block_id {
                live_cell.borrow_mut().insert(manifest_id)?;
            }
            for reference in &snapshot.history_segments {
                stopped()?;
                live_cell.borrow_mut().insert(reference.block_id)?;
                let segment_bytes = reader.get(reference.block_id)?;
                for transaction in decode_segment(&segment_bytes, reference)? {
                    for block_id in transaction.envelope.block_ids {
                        live_cell.borrow_mut().insert(block_id)?;
                    }
                }
            }
            for id in &snapshot.history_payload_metadata_ids { live_cell.borrow_mut().insert(*id)?; }
            if let Some(selection) = selected_history_metadata {
                // Resolve the same captured manifest after releasing authority.
                // A moved current catalog never substitutes another root. Any
                // source/decode failure aborts the whole partial mark before scan.
                blocks.visit_history_payload_metadata_selection(selection, |id| {
                    #[cfg(test)]
                    tests_capture::before_selected_metadata_visit();
                    stopped()?;
                    live_cell.borrow_mut().insert(id)?;
                    #[cfg(test)]
                    tests_capture::after_selected_metadata_insert();
                    // Insertion may flush a mark run. An in-progress write
                    // may finish, but stopping during it cannot start the
                    // selected root's next fresh-source read.
                    if stop.load(Ordering::Acquire) {
                        return Err(would_block("online GC epoch stopped"));
                    }
                    Ok(())
                })?;
            }
            for reference in &snapshot.staged_history {
                live_cell.borrow_mut().insert(reference.block_id)?;
            }
            for block_id in &snapshot.committed_block_ids {
                live_cell.borrow_mut().insert(*block_id)?;
            }
            let metrics = live_cell.borrow_mut().finish()?;
            Ok(MarkShape {
                roots: roots.len(),
                walked_pages: graphs.walked_pages,
                skipped_pages: graphs.skipped_pages,
                references: metrics.references,
                spill_runs: metrics.run_count,
                spill_bytes: metrics.spill_bytes,
            })
        })();
        match mark_result {
            Ok(shape) => {
                crate::engine_log::info("online_gc", &format!(
                    "step=mark_built roots={} walked_pages={} skipped_pages={} \
                     references={} spill_runs={} spill_bytes={} elapsed_ms={}",
                    shape.roots, shape.walked_pages, shape.skipped_pages,
                    shape.references, shape.spill_runs, shape.spill_bytes,
                    build_started.elapsed().as_millis(),
                ));
                epoch.marks = Some(live);
                Ok(shape)
            }
            Err(error) => {
                let _ = live.cleanup();
                Err(error)
            }
        }
    }

    fn scan<D: OnlineGcDatabase>(
        &self,
        database: &Arc<Mutex<D>>,
        epoch: &mut Epoch,
        stop: &AtomicBool,
    ) -> io::Result<()> {
        let blocks = Arc::clone(&epoch.capture.blocks);
        let marks = epoch.marks.take().expect("marks are built before scan");

        // Temporary block files: two-epoch confirmation by exact path.
        let temporary_plan = blocks.plan_temporary_file()?;
        if let Some(candidate) = temporary_plan.candidate {
            if self.previous.temp_candidate.as_ref() == Some(&candidate.path) {
                epoch.confirmed_temp = Some(candidate.clone());
            }
            epoch.temp_candidate_next = Some(candidate.path);
        }

        // Loose shard scan: suspects feed the next epoch; previous suspects
        // that stay unmarked are confirmed victims (bounded).
        ensure_directory(
            &epoch.suspects_directory,
            &epoch.capture.data_dir.join(WORK_DIRECTORY),
            epoch.capture.vfs.as_ref(),
        )?;
        let suspects_runs = epoch.suspects_directory.join("gc-runs");
        ensure_directory(
            &suspects_runs,
            &epoch.suspects_directory,
            epoch.capture.vfs.as_ref(),
        )?;
        let mut suspects_next = SpillMarkSet::initialize_with_spill_limit(
            &epoch.suspects_directory,
            Arc::clone(&epoch.capture.vfs),
            self.budget.maximum_temporary_bytes,
        )?;
        let scan_result = self
            .scan_loose_shards(database, epoch, &mut suspects_next, &blocks, &marks, stop)
            .and_then(|()| suspects_next.finish())
            .map(|suspect_metrics| {
                epoch.tracked_suspects = suspect_metrics.references;
            });
        if let Err(error) = scan_result {
            let _ = suspects_next.cleanup();
            return Err(error);
        }
        epoch.suspects_next = Some(suspects_next);

        // Payload segment window scan: dead sets confirm across epochs.
        self.scan_segment_window(epoch, &blocks, &marks, stop)?;
        epoch.marks = Some(marks);
        Ok(())
    }

    fn scan_loose_shards<D: OnlineGcDatabase>(
        &self,
        database: &Arc<Mutex<D>>,
        epoch: &mut Epoch,
        suspects_next: &mut SpillMarkSet,
        blocks: &Arc<BlockStore>,
        marks: &SpillMarkSet,
        stop: &AtomicBool,
    ) -> io::Result<()> {
        let current_payload_manifest = epoch.capture.snapshot.payload_manifest_block_id;
        let mut compaction_open = self.compaction_has_catalog_slot(blocks)?;
        let compaction_byte_limit = self
            .budget
            .maximum_temporary_bytes
            .min(MAX_SEGMENT_PAYLOAD_BYTES);
        let mut compaction_bytes = 0_u64;
        let mut copies_open = true;
        let mut copy_holds = SingleFileHolds::new(blocks, Duration::ZERO);
        let result = blocks.visit_block_shards(
            |shard, block_ids| {
                if stop.load(Ordering::Acquire) {
                    return Err(would_block("online GC epoch stopped"));
                }
                if block_ids.is_empty() {
                    return Ok(());
                }
                let live_for_shard = marks.live_for_shard(shard)?;
                let previous_for_shard = match self.previous.suspects.as_ref() {
                    Some(previous) => previous.live_for_shard(shard)?,
                    None => Vec::new(),
                };
                // Live blocks of this shard whose loose file is a redundant
                // copy, with the catalogued segment that holds each.
                let mut redundant_copies = Vec::new();
                for block_id in block_ids {
                    if stop.load(Ordering::Acquire) {
                        return Err(would_block("online GC epoch stopped"));
                    }
                    if current_payload_manifest == Some(block_id)
                        || live_for_shard.binary_search(&block_id).is_ok()
                    {
                        // One exact membership probe per live block while
                        // either of its uses is open: a block the catalog
                        // already holds is a redundant copy; any other is a
                        // compaction candidate once its size fits a segment,
                        // and only compaction sizes a block.
                        if !(copies_open || compaction_open) {
                            continue;
                        }
                        if let Some(segment_id) = blocks.catalogued_payload_segment(block_id)? {
                            if copies_open {
                                redundant_copies.push((block_id, segment_id));
                            }
                            continue;
                        }
                        if compaction_open {
                            let block_bytes = blocks.block_file_bytes(block_id)?;
                            if block_bytes <= compaction_byte_limit {
                                // A block the open segment cannot take closes
                                // it: pack that segment, then open the next.
                                if epoch.compaction_ids.len() == MAX_SEGMENT_BLOCKS
                                    || compaction_bytes + block_bytes > compaction_byte_limit
                                {
                                    // Any error but a stop ends compaction
                                    // only; the suspects still complete.
                                    compaction_open = match self
                                        .pack_compaction_segment(database, epoch, blocks, stop)
                                    {
                                        Err(error) if !stop.load(Ordering::Acquire) => {
                                            epoch.compaction_failure = Some(error);
                                            false
                                        }
                                        packed => packed?,
                                    };
                                    compaction_bytes = 0;
                                }
                                if compaction_open {
                                    epoch.compaction_ids.push(block_id);
                                    compaction_bytes += block_bytes;
                                }
                            }
                        }
                        continue;
                    }
                    suspects_next.insert(block_id)?;
                    // A block unlinked after `previous` was observed can be
                    // listed again only as a fresh write, which no relink
                    // decision records; it confirms from this scan instead.
                    if previous_for_shard.binary_search(&block_id).is_err()
                        || self.previous.unlinked_loose.binary_search(&block_id).is_ok()
                    {
                        continue;
                    }
                    // Each victim is unlinked in its own hold, so no byte
                    // total bounds a hold; the count bounds the victim list.
                    if epoch.confirmed_loose.len() < self.budget.maximum_blocks_per_round {
                        epoch.confirmed_loose.push(block_id);
                    }
                }
                // The shard's redundant copies go before the next shard is
                // listed, so their IDs never outgrow one shard.
                if !redundant_copies.is_empty() {
                    if let Err(error) = self.remove_loose_copies(database, &mut copy_holds, redundant_copies, stop) {
                        if stop.load(Ordering::Acquire) {
                            return Err(error);
                        }
                        // Any error but a stop ends copy removal only; the
                        // suspects still complete.
                        epoch.copy_failure = Some(error);
                        copies_open = false;
                    }
                }
                Ok(())
            },
        );
        result?;
        epoch.redundant_copies_removed = copy_holds.removed.blocks_removed;
        epoch.reclaimed_bytes += copy_holds.removed.bytes_removed;
        // The compaction floor mirrors the offline collector; it applies to
        // the trailing segment only, every closed one was full.
        if epoch.compaction_ids.len() < MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS {
            epoch.compaction_ids.clear();
        }
        epoch.confirmed_loose_planned = !epoch.confirmed_loose.is_empty();
        epoch.compaction_planned = !epoch.compaction_ids.is_empty();
        Ok(())

    }

    /// Catalog pressure mode engages at 75% of the 4,096-entry ceiling. At
    /// 90% (`catalog_pressure_expedited`) its consolidation work counts as
    /// pending: an epoch that ends there keeps the minimum interval instead
    /// of backing off (`run_epoch`); the start and duty floors still apply.
    fn catalog_pressure(&self, segments_len: usize) -> bool {
        segments_len >= self.catalog_pressure_floor
    }

    #[cfg(test)]
    pub(crate) fn set_catalog_pressure_floor_for_test(&mut self, floor: usize) {
        self.catalog_pressure_floor = floor;
    }

    #[cfg(test)]
    pub(crate) fn set_catalog_expedite_floor_for_test(&mut self, floor: usize) {
        self.catalog_expedite_floor = floor;
    }

    pub(crate) fn catalog_pressure_expedited(&self, segments_len: usize) -> bool {
        segments_len >= self.catalog_expedite_floor
    }

    /// Picks up to PRESSURE_CONSOLIDATION_VICTIMS small/dead-heavy segments
    /// whose retained content fits the temporary budget into one merge.
    fn plan_consolidation(&self, epoch: &mut Epoch, segments_len: usize) {
        if !self.catalog_pressure(segments_len) || epoch.consolidation.is_some() {
            return;
        }
        let mut candidates: Vec<_> = epoch.pressure_candidates.clone();
        // Smallest files first; every victim must confirm its dead set
        // across two marks, already enforced by the scan above.
        candidates.sort_by_key(|(reference, _, _)| reference.file_bytes);
        let mut victims = Vec::new();
        let mut retained_per_victim = Vec::new();
        let mut retained_bytes = 0_u64;
        for (reference, retained_ids, _) in candidates {
            if victims.len() >= PRESSURE_CONSOLIDATION_VICTIMS {
                break;
            }
            let next_bytes = retained_bytes.saturating_add(reference.file_bytes);
            if !retained_ids.is_empty() && next_bytes > self.budget.maximum_temporary_bytes {
                continue;
            }
            victims.push(reference);
            retained_per_victim.push(retained_ids);
            retained_bytes = next_bytes;
        }
        if victims.len() >= 2 {
            // Consolidation subsumes a rewrite that shares a victim; never
            // run both against the same segment in one epoch.
            if epoch.rewrite_victim.as_ref().is_some_and(|(reference, _)| {
                victims.iter().any(|victim| victim.segment_id == reference.segment_id)
            }) {
                epoch.rewrite_planned = false;
                epoch.rewrite_victim = None;
            }
            epoch.consolidation = Some(ConsolidationPlan {
                victims,
                retained_per_victim,
            });
        }
    }

    fn scan_segment_window(
        &self,
        epoch: &mut Epoch,
        blocks: &Arc<BlockStore>,
        marks: &SpillMarkSet,
        stop: &AtomicBool,
    ) -> io::Result<()> {
        let Some(manifest) = blocks.installed_payload_manifest()? else {
            return Ok(());
        };
        let manifest_segments_len = manifest.segments.len();
        let references = manifest.segments;
        if references.is_empty() {
            return Ok(());
        }
        let window_count = references
            .len()
            .div_ceil(MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED);
        let window = self.payload_window_cursor
            .unwrap_or(epoch.capture.snapshot.generation as usize) % window_count;
        let start = window * MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED;
        let selected = &references[start
            ..references
                .len()
                .min(start + MAX_AUTHORITY_GC_PAYLOAD_SEGMENTS_SCANNED)];
        let mut window = Vec::with_capacity(selected.len());
        for reference in selected {
            if stop.load(Ordering::Acquire) {
                return Err(would_block("online GC epoch stopped"));
            }
            blocks.verify_payload_segment(reference)?;
            window.push(
                blocks
                    .payload_segment_store()?
                    .open_segment(reference.segment_id)?
                    .block_ids()
                    .collect::<Vec<_>>(),
            );
        }
        if stop.load(Ordering::Acquire) {
            return Err(would_block("online GC epoch stopped"));
        }
        // One pass over the mark runs answers every segment of the window.
        let mut retained_window = window.clone();
        marks.retain_live_ids_per_segment(&mut retained_window)?;
        for ((reference, block_ids), mut retained_ids) in
            selected.iter().zip(window).zip(retained_window)
        {
            if let Some(protected) = epoch
                .capture
                .snapshot
                .payload_manifest_block_id
                .filter(|id| block_ids.binary_search(id).is_ok())
            {
                if let Err(position) = retained_ids.binary_search(&protected) {
                    retained_ids.insert(position, protected);
                }
            }
            let mut dead_ids = block_ids
                .iter()
                .copied()
                .filter(|block_id| retained_ids.binary_search(block_id).is_err())
                .collect::<Vec<_>>();
            dead_ids.sort_unstable();
            let confirmed = self
                .previous
                .segment_dead
                .get(&reference.segment_id)
                .is_some_and(|previous_dead| *previous_dead == dead_ids);
            if !dead_ids.is_empty() && epoch.segment_dead_next.len() < MAX_TRACKED_SEGMENT_DEAD_SETS {
                epoch
                    .segment_dead_next
                    .insert(reference.segment_id, dead_ids.clone());
            }
            // Catalog-pressure consolidation candidates: any segment with
            // two-mark-confirmed dead content, or a small fully-live file
            // (nothing excluded, so no fence question at all).
            if self.catalog_pressure(manifest_segments_len)
                && ((confirmed && !dead_ids.is_empty())
                    || (dead_ids.is_empty()
                        && reference.file_bytes <= self.budget.maximum_temporary_bytes / 4))
            {
                epoch
                    .pressure_candidates
                    .push((*reference, retained_ids.clone(), dead_ids.len()));
            }
            if dead_ids.is_empty() {
                continue;
            }
            if confirmed && epoch.rewrite_victim.is_none() {
                if retained_ids.is_empty()
                    || reference.file_bytes <= self.budget.maximum_temporary_bytes
                {
                    epoch.rewrite_planned = true;
                    epoch.rewrite_victim = Some((*reference, retained_ids.clone()));
                }
            }
        }
        self.plan_consolidation(epoch, manifest_segments_len);
        Ok(())
    }

    fn delete_confirmed<D: OnlineGcDatabase>(
        &self,
        database: &Arc<Mutex<D>>,
        epoch: &mut Epoch,
        stop: &AtomicBool,
    ) -> io::Result<()> {
        let blocks = Arc::clone(&epoch.capture.blocks);

        // Confirmed temporary files need no hold: the exact `.new-*` name
        // can only belong to the crashed writer that left it behind.
        if let Some(candidate) = epoch.confirmed_temp.take() {
            match blocks.remove_temporary_file(&candidate) {
                Ok(bytes) => {
                    epoch.temporary_files_removed += 1;
                    epoch.reclaimed_bytes += bytes;
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                Err(error) => return Err(error),
            }
        }

        // Loose victims, one short hold each: every hold decides the staged
        // copy, the relink fence and both CONTROL slots again, and reports the
        // victim when it unlinked, or may have unlinked, its file (rule S10).
        // A contended authority postpones the next hold. A busy physical lane
        // or CONTROL slots that stay apart end this epoch's loose deletion;
        // the remaining victims stay suspects and confirm again next epoch.
        if !epoch.confirmed_loose.is_empty() {
            let victims = std::mem::take(&mut epoch.confirmed_loose);
            let mut holds = SingleFileHolds::new(&blocks, Duration::ZERO);
            let mut deleted = Ok(());
            'victims: for victim in victims {
                if let Err(error) = holds.sync_left_shard(victim) {
                    deleted = Err(error);
                    break;
                }
                loop {
                    if stop.load(Ordering::Acquire) {
                        deleted = Err(would_block("online GC epoch stopped"));
                        break 'victims;
                    }
                    match holds.remove(database, victim, |database| {
                        database.online_gc_delete_loose_victim(victim, &mut epoch.unlinked_loose)
                    }) {
                        Some(Ok(())) => break,
                        Some(Err(error)) if error.kind() == io::ErrorKind::WouldBlock => break 'victims,
                        Some(Err(error)) => {
                            deleted = Err(error);
                            break 'victims;
                        }
                        None => {}
                    }
                }
            }
            let synced = holds.finish();
            epoch.reclaimed_blocks += holds.removed.blocks_removed;
            epoch.reclaimed_bytes += holds.removed.bytes_removed;
            deleted.and(synced)?;
        }

        // Catalog-pressure consolidation: build off-mutex, publish under
        // one hold; runs ahead of the single-segment rewrite at pressure.
        if let Some(plan) = epoch.consolidation.take() {
            if stop.load(Ordering::Acquire) {
                return Err(would_block("online GC epoch stopped"));
            }
            let built = self.build_consolidation_segment(&blocks, &plan)?;
            let built_reference = built.as_ref().map(|built| built.1);
            let publish = publish_under_hold(database, &blocks, stop, |database| {
                database.online_gc_finish_consolidation(
                    &plan.victims,
                    &plan.retained_per_victim,
                    built_reference,
                )
            });
            match publish {
                Ok(metrics) => {
                    epoch.reclaimed_bytes += metrics.retired_file_bytes;
                    epoch.consolidations += 1;
                    epoch.consolidated_victims += u64::from(metrics.victims);
                }
                Err(error) => {
                    if error.kind() == io::ErrorKind::WouldBlock {
                        if let Some((segment, _)) = built {
                            self.discard_built_segment(&blocks, &segment);
                        }
                    } else {
                        return Err(error);
                    }
                }
            }
        }

        // Segment rewrite: build off-mutex, publish under one hold.
        if let Some((reference, retained_ids)) = epoch.rewrite_victim.take() {
            if stop.load(Ordering::Acquire) {
                return Err(would_block("online GC epoch stopped"));
            }
            let built = self.build_replacement_segment(&blocks, &reference, &retained_ids)?;
            let built_reference = built.as_ref().map(|built| built.1);
            let publish = publish_under_hold(database, &blocks, stop, |database| {
                database.online_gc_finish_segment_reclaim(
                    reference.segment_id,
                    &reference,
                    &retained_ids,
                    built_reference,
                )
            });
            match publish {
                Ok(metrics) => {
                    epoch.reclaimed_bytes += metrics.retired_file_bytes;
                    if metrics.replacement_segment_id.is_some() {
                        epoch.segments_rewritten += 1;
                    } else {
                        epoch.segments_retired += 1;
                    }
                }
                Err(error) => {
                    // WouldBlock always proves the publish never happened
                    // (physical-lane busy, catalog moved, relink fence), so
                    // the built file is still ours to discard. Every other
                    // error may follow a durable publish: the built segment
                    // is then either catalog-referenced or an orphan a later
                    // epoch sweeps — discarding it could destroy live data.
                    if error.kind() == io::ErrorKind::WouldBlock {
                        if let Some((segment, _)) = built {
                            self.discard_built_segment(&blocks, &segment);
                        }
                    } else {
                        return Err(error);
                    }
                }
            }
        }

        // Loose compaction: the scan packed every segment the format limits
        // closed; the trailing one packs here. A compaction error that ended
        // the scan's packing, or a copy-removal error that ended its removal
        // of redundant copies, surfaces now.
        if let Some(error) = epoch.compaction_failure.take().or_else(|| epoch.copy_failure.take()) {
            return Err(error);
        }
        if !epoch.compaction_ids.is_empty() {
            self.pack_compaction_segment(database, epoch, &blocks, stop)?;
        }
        Ok(())
    }

    /// Compaction is optional work: it takes a catalog slot only while the
    /// catalog stays below the pressure floor afterwards, so it never
    /// consumes the slots history promotion and pack adoption need, nor
    /// creates pressure that consolidation would have to undo.
    fn compaction_has_catalog_slot(&self, blocks: &BlockStore) -> io::Result<bool> {
        let segments = blocks
            .installed_payload_manifest()?
            .map_or(0, |manifest| manifest.segments.len());
        Ok(!self.catalog_pressure(segments + 1))
    }

    /// Packs the open compaction segment (`epoch.compaction_ids`): built and
    /// verified off the authority mutex, published under one short hold
    /// through the physical publication lane, which returns once both CONTROL
    /// slots name the new catalog; then each loose copy goes under its own
    /// short hold (`remove_loose_copies`). `Ok(false)`
    /// ends this epoch's compaction without an error: no catalog slot is
    /// left below the pressure floor, or the hold, the publication lane or
    /// the catalog was contended. Only `WouldBlock` proves that nothing was
    /// published, so only then is the built file discarded; any other error
    /// may follow a durable publish (the rewrite path's rule).
    fn pack_compaction_segment<D: OnlineGcDatabase>(
        &self,
        database: &Arc<Mutex<D>>,
        epoch: &mut Epoch,
        blocks: &Arc<BlockStore>,
        stop: &AtomicBool,
    ) -> io::Result<bool> {
        let mut compaction_ids = std::mem::take(&mut epoch.compaction_ids);
        if stop.load(Ordering::Acquire) {
            return Err(would_block("online GC epoch stopped"));
        }
        if !self.compaction_has_catalog_slot(blocks)? {
            return Ok(false);
        }
        compaction_ids.sort_unstable();
        compaction_ids.dedup();
        let (segment, reference) = self.build_compaction_segment(blocks, &compaction_ids, stop)?;
        let mut publish_hold = Duration::ZERO;
        let publish = match database.try_lock() {
            Ok(mut database_guard) => {
                let acquired = Instant::now();
                let published = match self.compaction_has_catalog_slot(blocks) {
                    Ok(true) => database_guard.gc_database_mut().and_then(|database| {
                        database.online_gc_finish_compaction(&compaction_ids, reference)
                    }),
                    Ok(false) => Err(would_block("online GC compaction catalog reached the pressure floor")),
                    Err(error) => Err(error),
                };
                drop(database_guard);
                publish_hold = acquired.elapsed();
                published
            }
            Err(_) => Err(would_block("online GC compaction hold contended")),
        };
        match publish {
            Ok(metrics) => {
                epoch.compacted_blocks += u64::from(metrics.blocks_packed);
                epoch.compaction_segments += 1;
                let mut holds = SingleFileHolds::new(blocks, publish_hold);
                self.remove_loose_copies(
                    database,
                    &mut holds,
                    compaction_ids.iter().map(|&copy| (copy, metrics.segment_id)),
                    stop,
                )?;
                epoch.reclaimed_bytes += holds.removed.bytes_removed;
                Ok(true)
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                self.discard_built_segment(blocks, &segment);
                Ok(false)
            }
            Err(error) => Err(error),
        }
    }

    /// Unlinks loose copies of blocks that catalogued segments hold, one short
    /// hold per copy (`online_gc_remove_redundant_copy`) in the caller's run,
    /// whose `removed` counts them, then makes the last shard's unlinks
    /// durable: the copies of a segment `pack_compaction_segment` just
    /// published, or one shard's copies the scan found already catalogued (one
    /// run across the scan, so its pause also spans shards). Neither a
    /// contended authority nor `WouldBlock` (CONTROL slots briefly apart while
    /// a publication owns the lane) ends the batch, only a stop or an error; a
    /// copy an ended batch leaves waits for the next scan. The copies are live
    /// in this epoch's mark, so none of them is a suspect the next epoch
    /// confirms from, and rule S10 needs no record of their unlinks.
    fn remove_loose_copies<D: OnlineGcDatabase>(
        &self,
        database: &Arc<Mutex<D>>,
        holds: &mut SingleFileHolds<'_>,
        copies: impl IntoIterator<Item = (BlockId, Uuid)>,
        stop: &AtomicBool,
    ) -> io::Result<()> {
        let mut removed = Ok(());
        'copies: for (copy, segment_id) in copies {
            if let Err(error) = holds.sync_left_shard(copy) {
                removed = Err(error);
                break;
            }
            loop {
                if stop.load(Ordering::Acquire) {
                    removed = Err(would_block("online GC epoch stopped"));
                    break 'copies;
                }
                match holds.remove(database, copy, |database| {
                    database.online_gc_remove_redundant_copy(segment_id, copy)
                }) {
                    Some(Ok(())) => break,
                    Some(Err(error)) if error.kind() == io::ErrorKind::WouldBlock => {}
                    Some(Err(error)) => {
                        removed = Err(error);
                        break 'copies;
                    }
                    None => {}
                }
            }
        }
        let synced = holds.finish();
        removed.and(synced)
    }

    /// Builds the replacement segment off-mutex. Returns the built segment
    /// (kept open so the file cannot be reclaimed by a racing orphan scan)
    /// and its catalog reference; `None` for a fully-dead victim.
    fn build_replacement_segment(
        &self,
        blocks: &Arc<BlockStore>,
        reference: &PayloadSegmentReference,
        retained_ids: &[BlockId],
    ) -> io::Result<Option<(PayloadSegment, PayloadSegmentReference)>> {
        if retained_ids.is_empty() {
            return Ok(None);
        }
        let retired_segment = blocks
            .payload_segment_store()?
            .open_segment(reference.segment_id)?;
        let replacement_id = Uuid::new_v4();
        let replacement = blocks.payload_segment_store()?.create(
            replacement_id,
            retained_ids.to_vec(),
            |block_id| {
                retired_segment.get(block_id)?.ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "online reclaim source block is missing",
                    )
                })
            },
        )?;
        let replacement_reference = PayloadSegmentReference::new(
            replacement.metadata(),
            replacement.first_block_id(),
            replacement.last_block_id(),
        )?;
        blocks.verify_payload_segment(&replacement_reference)?;
        Ok(Some((replacement, replacement_reference)))
    }

    /// Builds the consolidation replacement off-mutex: the union of every
    /// victim's retained blocks in one new segment (`None` when every
    /// victim is fully dead, which frees all their slots).
    fn build_consolidation_segment(
        &self,
        blocks: &Arc<BlockStore>,
        plan: &ConsolidationPlan,
    ) -> io::Result<Option<(PayloadSegment, PayloadSegmentReference)>> {
        let mut retained: Vec<BlockId> = plan
            .retained_per_victim
            .iter()
            .flatten()
            .copied()
            .collect();
        if retained.is_empty() {
            return Ok(None);
        }
        retained.sort_unstable();
        retained.dedup();
        let segment_id = Uuid::new_v4();
        let segment = blocks.payload_segment_store()?.create(
            segment_id,
            retained,
            |block_id| blocks.get_traversal(block_id),
        )?;
        let reference = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )?;
        blocks.verify_payload_segment(&reference)?;
        Ok(Some((segment, reference)))
    }

    fn build_compaction_segment(
        &self,
        blocks: &Arc<BlockStore>,
        sorted_ids: &[BlockId],
        stop: &AtomicBool,
    ) -> io::Result<(PayloadSegment, PayloadSegmentReference)> {
        let segment_id = Uuid::new_v4();
        let segment = blocks.payload_segment_store()?.create(
            segment_id,
            sorted_ids.to_vec(),
            |block_id| {
                if stop.load(Ordering::Acquire) {
                    return Err(would_block("online GC epoch stopped"));
                }
                blocks.get_traversal(block_id)
            },
        )?;
        let reference = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )?;
        blocks.verify_payload_segment(&reference)?;
        Ok((segment, reference))
    }

    /// Best-effort cleanup of a segment we built but never published; the
    /// file matches the orphan naming rules, so removal reuses them.
    fn discard_built_segment(&self, blocks: &Arc<BlockStore>, segment: &PayloadSegment) {
        let metadata = segment.metadata();
        let candidate = PayloadSegmentOrphanCandidate {
            file_name: format!("{}.pseg", metadata.segment_id.simple()),
            segment_id: Some(metadata.segment_id),
            file_bytes: metadata.file_bytes,
        };
        if let Ok(store) = blocks.payload_segment_store() {
            let _ = store.remove_orphan_file(&candidate);
        }
    }
}

/// Best-effort removal of the online collector's spill runs. Only files
/// the collector itself names are touched; anything else is left in place.
pub(crate) fn wipe_work_directories(data_dir: &Path, vfs: &dyn Vfs) {
    let work = data_dir.join(WORK_DIRECTORY);
    for leaf in [MARKS_DIRECTORY, SUSPECTS_DIRECTORIES[0], SUSPECTS_DIRECTORIES[1]] {
        let runs = work.join(leaf).join("gc-runs");
        let Ok(entries) = vfs.read_directory(&runs) else {
            continue;
        };
        for path in entries {
            let removable = path
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| {
                    (name.starts_with("run-") && name.ends_with(".mark"))
                        || name.starts_with(".run-new-")
                });
            if removable {
                let _ = vfs.remove_file(&path);
            }
        }
        let _ = sync_directory_barrier(vfs, &runs);
    }
}

/// Health/metrics observer shared through `control_ops::ControlObservers`.
#[derive(Clone)]
pub struct OnlineGarbageCollectionObserver {
    handle: OnlineGarbageCollectionHandle,
    stopped: Arc<AtomicBool>,
}

impl OnlineGarbageCollectionObserver {
    pub fn metrics(&self) -> OnlineGarbageCollectionMetrics {
        self.handle.metrics()
    }

    pub fn require_healthy(&self) -> io::Result<()> {
        if self.stopped.load(Ordering::Acquire) {
            return Err(io::Error::other(
                "online garbage collector stopped unexpectedly",
            ));
        }
        Ok(())
    }
}

/// Dedicated `tofu-db-online-gc` thread. Mirrors the maintenance lane's
/// politeness: every authority hold is a `try_lock`, contention always
/// defers the collector, never the foreground.
pub struct OnlineGarbageCollectionDriver<D: OnlineGcDatabase = AuthorityDatabase> {
    stop: Arc<AtomicBool>,
    thread: Mutex<Option<std::thread::JoinHandle<()>>>,
    observer: OnlineGarbageCollectionObserver,
    database: Arc<Mutex<D>>,
}

const DRIVER_POLL_INTERVAL: Duration = Duration::from_millis(250);
const DRIVER_STACK_BYTES: usize = 1024 * 1024;
pub(crate) const DRIVER_STOP_JOIN_BUDGET: Duration = Duration::from_secs(5);

impl<D: OnlineGcDatabase> OnlineGarbageCollectionDriver<D> {
    pub fn start(
        database: &Arc<Mutex<D>>,
        budget: AuthorityGarbageCollectionBudget,
        volume_pressure: bool,
    ) -> io::Result<Self> {
        let mut collector = OnlineGarbageCollector::new(budget);
        collector.defer_first_epoch(ONLINE_GC_BOOT_GRACE);
        Self::start_with_collector(database, collector, volume_pressure)
    }

    pub fn start_with_collector(
        database: &Arc<Mutex<D>>,
        mut collector: OnlineGarbageCollector,
        volume_pressure: bool,
    ) -> io::Result<Self> {
        let handle = collector.handle();
        {
            let mut guard = database
                .lock()
                .map_err(|_| io::Error::other("authority mutex poisoned"))?;
            guard.install_gc_handle(handle.clone())?;
        }
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = Arc::new(AtomicBool::new(false));
        let thread = std::thread::Builder::new()
            .name("tofu-db-online-gc".to_owned())
            .stack_size(DRIVER_STACK_BYTES)
            .spawn({
                let stop = Arc::clone(&stop);
                let stopped = Arc::clone(&stopped);
                let database = Arc::clone(database);
                move || {
                    crate::fsync_stats::set_current_component(crate::fsync_stats::COMPONENT_ONLINE_GC);
                    loop {
                        if stop.load(Ordering::Acquire) {
                            break;
                        }
                        if collector.should_run_now(volume_pressure) {
                            // Epoch errors are already accounted as deferred;
                            // the driver stays alive and retries on cadence.
                            let _ = collector.run_epoch(&database, &stop);
                        }
                        std::thread::sleep(DRIVER_POLL_INTERVAL);
                    }
                    stopped.store(true, Ordering::Release);
                }
            })?;
        Ok(Self {
            stop,
            thread: Mutex::new(Some(thread)),
            observer: OnlineGarbageCollectionObserver {
                handle,
                stopped,
            },
            database: Arc::clone(database),
        })
    }

    pub fn observer(&self) -> OnlineGarbageCollectionObserver {
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
                .map_err(|_| io::Error::other("online GC driver panicked"))?;
        }
        // Spill runs are reconstructible scratch, never authority: wipe the
        // work directories so a graceful stop leaves no GC residue behind.
        if let Ok(database) = self.database.lock() {
            if let Ok(database) = database.gc_database() {
                let _ = database.wipe_online_gc_work_directories();
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests;
#[cfg(test)]
mod tests_fault;

#[cfg(test)]
mod tests_capture;
#[cfg(test)]
mod tests_mark_cost;
#[cfg(test)]
mod tests_listing;
#[cfg(test)]
mod tests_segment_reclaim;
#[cfg(test)]
mod tests_fallback_slot;
#[cfg(test)]
mod tests_publication_window;
#[cfg(test)]
mod tests_rebirth;
#[cfg(test)]
mod tests_short_holds;
#[cfg(test)]
mod tests_redundant_copies;
#[cfg(test)]
mod tests_reclaim_throughput;
