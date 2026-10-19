//! Online, epoch-based physical garbage collection (docs/TOFUDB_ONLINE_GC.md).
//!
//! The daemon runs this collector on a dedicated thread. Each epoch takes a
//! frozen mark snapshot under a short authority hold, builds marks and scans
//! for suspects off-mutex, and deletes only what two consecutive marks prove
//! dead, what the relink fence shows untouched, and what the catalog still
//! confirms. Every long operation (graph traversal, directory scans, segment
//! rebuild, fsync chains) happens outside the authority mutex; holds are
//! O(in-memory capture) or O(one bounded batch publish).

use std::cell::RefCell;
use std::collections::BTreeMap;
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
use crate::backup::visit_semantic_value_blocks;
use crate::backup_gc::SpillMarkSet;
use crate::block::{BlockId, BlockStore, BlockTemporaryFileCandidate};
use crate::engine::OnlineGcMarkSnapshot;
use crate::entity::visit_entity_page_graph_with_values;
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
/// One mark build that cannot finish within this bound aborts and retries;
/// it holds nothing while building, so aborting is always safe.
pub const ONLINE_GC_MAX_MARK_BUILD: Duration = Duration::from_secs(900);
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

/// Cumulative online-collector state surfaced through `system.health` and
/// `system.reclaim`. All values are process-local.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, serde::Serialize)]
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
    pub total_consolidations: u64,
    pub total_consolidated_victims: u64,
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
    orphan_candidate_next: Option<String>,
    segment_dead_next: BTreeMap<Uuid, Vec<BlockId>>,
    confirmed_loose: Vec<BlockId>,
    confirmed_temp: Option<BlockTemporaryFileCandidate>,
    rewrite_victim: Option<(PayloadSegmentReference, Vec<BlockId>)>,
    compaction_ids: Vec<BlockId>,
    confirmed_loose_planned: bool,
    rewrite_planned: bool,
    compaction_planned: bool,
    tracked_suspects: u64,
    reclaimed_bytes: u64,
    reclaimed_blocks: u64,
    temporary_files_removed: u64,
    orphan_segments_removed: u64,
    segments_rewritten: u64,
    segments_retired: u64,
    compacted_blocks: u64,
    consolidations: u64,
    consolidated_victims: u64,
    consolidation: Option<ConsolidationPlan>,
    pressure_candidates: Vec<(PayloadSegmentReference, Vec<BlockId>, usize)>,
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
    temp_candidate: Option<PathBuf>,
    orphan_candidate: Option<String>,
    segment_dead: BTreeMap<Uuid, Vec<BlockId>>,
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
        *self
            .metrics
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
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
            metrics: Arc::new(RwLock::new(OnlineGarbageCollectionMetrics::default())),
            expedite: Arc::new(AtomicBool::new(false)),
            previous: PreviousEpoch {
                suspects: None,
                temp_candidate: None,
                orphan_candidate: None,
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

    fn note_epoch_finished(&self, duration: Duration, epoch: &Epoch) {
        if let Ok(mut metrics) = self.metrics.write() {
            metrics.epochs_completed += 1;
            metrics.marks_built += 1;
            metrics.epoch_in_progress = false;
            metrics.last_epoch_duration_ms = duration.as_millis() as u64;
            metrics.last_epoch_reclaimed_bytes = epoch.reclaimed_bytes;
            metrics.total_reclaimed_bytes += epoch.reclaimed_bytes;
            metrics.total_reclaimed_blocks += epoch.reclaimed_blocks;
            metrics.total_temporary_files_removed += epoch.temporary_files_removed;
            metrics.total_orphan_segments_removed += epoch.orphan_segments_removed;
            metrics.total_segments_rewritten += epoch.segments_rewritten;
            metrics.total_segments_retired += epoch.segments_retired;
            metrics.total_compacted_blocks += epoch.compacted_blocks;
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
        let capture = {
            let database_guard = match database.try_lock() {
                Ok(guard) => guard,
                Err(_) => {
                    self.note_deferred();
                    return Ok((OnlineGarbageCollectionOutcome::Deferred, false));
                }
            };
            let capture = database_guard.gc_budget(self.budget).and_then(|budget| {
                self.budget = budget;
                database_guard.gc_database()?.online_gc_capture(self.budget.maximum_temporary_bytes)
            });
            match capture {
                Ok(capture) => capture,
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
            orphan_candidate_next: None,
            segment_dead_next: BTreeMap::new(),
            confirmed_loose: Vec::new(),
            confirmed_temp: None,
            rewrite_victim: None,
            compaction_ids: Vec::new(),
            confirmed_loose_planned: false,
            rewrite_planned: false,
            compaction_planned: false,
            tracked_suspects: 0,
            reclaimed_bytes: 0,
            reclaimed_blocks: 0,
            temporary_files_removed: 0,
            orphan_segments_removed: 0,
            segments_rewritten: 0,
            segments_retired: 0,
            compacted_blocks: 0,
            consolidations: 0,
            consolidated_victims: 0,
            consolidation: None,
            pressure_candidates: Vec::new(),
        };

        // Phase 2: mark build, off-mutex.
        if let Err(error) = self.build_marks(&mut epoch, stop) {
            self.cleanup_epoch(&mut epoch);
            self.note_deferred();
            return Err(error);
        }
        // Phase 3: scan + two-mark confirm, off-mutex.
        if let Err(error) = self.scan(&mut epoch, stop) {
            self.cleanup_epoch(&mut epoch);
            self.note_deferred();
            return Err(error);
        }
        // Phase 4: deletion holds.
        if let Err(error) = self.delete_confirmed(database, &mut epoch, stop) {
            self.cleanup_epoch(&mut epoch);
            self.note_deferred();
            return Err(error);
        }

        // Phase 5: rotate epoch memory and publish metrics.
        self.previous.suspects = epoch.suspects_next.take();
        self.previous.temp_candidate = epoch.temp_candidate_next.take();
        self.previous.orphan_candidate = epoch.orphan_candidate_next.take();
        self.previous.segment_dead = std::mem::take(&mut epoch.segment_dead_next);
        self.suspects_parity = 1 - self.suspects_parity;
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
        // minimum interval so two-mark confirmation is never starved.
        let found_work = epoch.reclaimed_bytes > 0
            || epoch.temporary_files_removed > 0
            || epoch.orphan_segments_removed > 0
            || epoch.segments_rewritten > 0
            || epoch.segments_retired > 0
            || epoch.compacted_blocks > 0
            || epoch.consolidations > 0
            || epoch.confirmed_loose_planned
            || epoch.rewrite_planned
            || epoch.compaction_planned
            || epoch.tracked_suspects > 0
            || !epoch.segment_dead_next.is_empty()
            || epoch.temp_candidate_next.is_some()
            || epoch.orphan_candidate_next.is_some();
        self.note_epoch_finished(epoch.started.elapsed(), &epoch);
        Ok((OnlineGarbageCollectionOutcome::EpochComplete, found_work))
    }

    fn cleanup_epoch(&self, epoch: &mut Epoch) {
        if let Some(marks) = epoch.marks.take() {
            let _ = marks.cleanup();
        }
        if let Some(suspects) = epoch.suspects_next.take() {
            let _ = suspects.cleanup();
        }
    }

    fn build_marks(&self, epoch: &mut Epoch, stop: &AtomicBool) -> io::Result<()> {
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
            let live_cell = RefCell::new(&mut live);
            let visits = RefCell::new(0_u32);
            let mut check_stop = |visits: &RefCell<u32>| -> io::Result<()> {
                let mut visits = visits.borrow_mut();
                *visits += 1;
                // Each visit may require a network-volume read. Shutdown
                // cannot wait for another 4,096 such reads before noticing EOF.
                if stop.load(Ordering::Acquire) {
                    return Err(would_block("online GC epoch stopped"));
                }
                if *visits % 4096 == 0 {
                    if build_started.elapsed() > ONLINE_GC_MAX_MARK_BUILD {
                        return Err(would_block(
                            "online GC mark build exceeded its budget",
                        ));
                    }
                }
                Ok(())
            };
            let mut roots = vec![snapshot.authority_state_root];
            roots.extend(snapshot.pinned_roots.iter().map(|root| Some(*root)));
            for root in roots {
                if stop.load(Ordering::Acquire) {
                    return Err(would_block("online GC epoch stopped"));
                }
                if build_started.elapsed() > ONLINE_GC_MAX_MARK_BUILD {
                    return Err(would_block("online GC mark build exceeded its budget"));
                }
                visit_entity_page_graph_with_values(
                    root,
                    |block_id| blocks.get(block_id),
                    |block_id, _| {
                        check_stop(&visits)?;
                        live_cell.borrow_mut().insert(block_id)
                    },
                    |key, value| {
                        visit_semantic_value_blocks(&blocks, key, value, |block_id, _| {
                            check_stop(&visits)?;
                            live_cell.borrow_mut().insert(block_id)
                        })
                    },
                )?;
            }
            if let Some(manifest_id) = snapshot.history_manifest_block_id {
                live_cell.borrow_mut().insert(manifest_id)?;
            }
            if let Some(manifest_id) = snapshot.payload_manifest_block_id {
                live_cell.borrow_mut().insert(manifest_id)?;
            }
            for reference in &snapshot.history_segments {
                if stop.load(Ordering::Acquire) {
                    return Err(would_block("online GC epoch stopped"));
                }
                if build_started.elapsed() > ONLINE_GC_MAX_MARK_BUILD {
                    return Err(would_block("online GC mark build exceeded its budget"));
                }
                live_cell.borrow_mut().insert(reference.block_id)?;
                let segment_bytes = blocks.get(reference.block_id)?;
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
                    check_stop(&visits)?;
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
            let result = live_cell.borrow_mut().finish();
            result
        })();
        match mark_result {
            Ok(_) => {
                epoch.marks = Some(live);
                Ok(())
            }
            Err(error) => {
                let _ = live.cleanup();
                Err(error)
            }
        }
    }

    fn scan(&self, epoch: &mut Epoch, stop: &AtomicBool) -> io::Result<()> {
        let blocks = Arc::clone(&epoch.capture.blocks);
        let marks = epoch.marks.take().expect("marks are built before scan");

        // Temporary block files: two-epoch confirmation by exact path.
        let temporary_plan = blocks.plan_temporary_file(self.budget.maximum_physical_blocks)?;
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
            .scan_loose_shards(epoch, &mut suspects_next, &blocks, &marks, stop)
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

    fn scan_loose_shards(
        &self,
        epoch: &mut Epoch,
        suspects_next: &mut SpillMarkSet,
        blocks: &Arc<BlockStore>,
        marks: &SpillMarkSet,
        stop: &AtomicBool,
    ) -> io::Result<()> {
        let current_payload_manifest = epoch.capture.snapshot.payload_manifest_block_id;
        let installed_manifest = blocks.installed_payload_manifest()?;
        let catalog_has_capacity = installed_manifest
            .as_ref()
            .map_or(0, |manifest| manifest.segments.len())
            < MAX_PAYLOAD_SEGMENTS;
        let compaction_byte_limit = self
            .budget
            .maximum_temporary_bytes
            .min(MAX_SEGMENT_PAYLOAD_BYTES);
        let mut compaction_bytes = 0_u64;
        let mut confirmed_bytes = 0_u64;
        let result = blocks.visit_block_shards(
            self.budget.maximum_physical_blocks,
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
                for block_id in block_ids {
                    if stop.load(Ordering::Acquire) {
                        return Err(would_block("online GC epoch stopped"));
                    }
                    if current_payload_manifest == Some(block_id)
                        || live_for_shard.binary_search(&block_id).is_ok()
                    {
                        if catalog_has_capacity
                            && !blocks.has_catalogued_payload_block(block_id)?
                        {
                            let block_bytes = blocks.block_file_bytes(block_id)?;
                            let fits = epoch.compaction_ids.len() < MAX_SEGMENT_BLOCKS
                                && compaction_bytes
                                    .checked_add(block_bytes)
                                    .is_some_and(|bytes| bytes <= compaction_byte_limit);
                            if fits {
                                epoch.compaction_ids.push(block_id);
                                compaction_bytes += block_bytes;
                            }
                        }
                        continue;
                    }
                    suspects_next.insert(block_id)?;
                    if previous_for_shard.binary_search(&block_id).is_err() {
                        continue;
                    }
                    let block_bytes = blocks.block_file_bytes(block_id)?;
                    let fits = epoch.confirmed_loose.len()
                        < self.budget.maximum_blocks_per_round
                        && confirmed_bytes
                            .checked_add(block_bytes)
                            .is_some_and(|bytes| bytes <= self.budget.maximum_victim_bytes);
                    if fits {
                        epoch.confirmed_loose.push(block_id);
                        confirmed_bytes += block_bytes;
                    }
                }
                Ok(())
            },
        );
        result?;
        // The compaction floor mirrors the offline collector.
        if epoch.compaction_ids.len() < MIN_AUTHORITY_GC_PAYLOAD_COMPACTION_BLOCKS {
            epoch.compaction_ids.clear();
        }
        epoch.confirmed_loose_planned = !epoch.confirmed_loose.is_empty();
        epoch.compaction_planned = !epoch.compaction_ids.is_empty();
        Ok(())

    }

    /// Catalog pressure mode engages at 75% of the 4,096-entry ceiling;
    /// it expedites consolidation epochs at 90% (see the driver cadence).
    fn catalog_pressure(&self, segments_len: usize) -> bool {
        segments_len >= self.catalog_pressure_floor
    }

    #[cfg(test)]
    pub(crate) fn set_catalog_pressure_floor_for_test(&mut self, floor: usize) {
        self.catalog_pressure_floor = floor;
    }

    pub(crate) fn catalog_pressure_expedited(&self, segments_len: usize) -> bool {
        segments_len * 100 >= MAX_PAYLOAD_SEGMENTS * 90
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
        for reference in selected {
            if stop.load(Ordering::Acquire) {
                return Err(would_block("online GC epoch stopped"));
            }
            blocks.verify_payload_segment(reference)?;
            let block_ids = blocks
                .payload_segment_store()?
                .open_segment(reference.segment_id)?
                .block_ids()
                .collect::<Vec<_>>();
            let mut retained_ids = block_ids.clone();
            marks.retain_live_ids(&mut retained_ids)?;
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

        // Loose victims: one bounded hold; the fence check is inside.
        if !epoch.confirmed_loose.is_empty() {
            if stop.load(Ordering::Acquire) {
                return Err(would_block("online GC epoch stopped"));
            }
            let victims = std::mem::take(&mut epoch.confirmed_loose);
            let removal = {
                let mut database_guard = database
                    .try_lock()
                    .map_err(|_| would_block("online GC deletion hold contended"))?;
                database_guard.gc_database_mut()?.online_gc_delete_loose(&victims)?
            };
            epoch.reclaimed_blocks += removal.blocks_removed;
            epoch.reclaimed_bytes += removal.bytes_removed;
        }

        // Orphan segment: planned and removed under one hold so the
        // background-pack unpublished set is consulted atomically.
        if stop.load(Ordering::Acquire) {
            return Err(would_block("online GC epoch stopped"));
        }
        {
            let database_guard = database
                .try_lock()
                .map_err(|_| would_block("online GC orphan hold contended"))?;
            let orphan_plan = database_guard.gc_database()?.online_gc_plan_orphan()?;
            if let Some(candidate) = orphan_plan.candidate {
                let confirmed = self.previous.orphan_candidate.as_ref()
                    == Some(&candidate.file_name);
                epoch.orphan_candidate_next = Some(candidate.file_name.clone());
                if confirmed {
                    let removed = database_guard.gc_database()?.online_gc_remove_orphan(&candidate)?;
                    epoch.orphan_segments_removed += 1;
                    epoch.reclaimed_bytes += removed;
                }
            }
        }

        // Catalog-pressure consolidation: build off-mutex, publish under
        // one hold; runs ahead of the single-segment rewrite at pressure.
        if let Some(plan) = epoch.consolidation.take() {
            if stop.load(Ordering::Acquire) {
                return Err(would_block("online GC epoch stopped"));
            }
            let built = self.build_consolidation_segment(&blocks, &plan)?;
            let built_reference = built.as_ref().map(|built| built.1);
            let publish = {
                let mut database_guard = database
                    .try_lock()
                    .map_err(|_| would_block("online GC consolidation hold contended"))?;
                database_guard.gc_database_mut()?.online_gc_finish_consolidation(
                    &plan.victims,
                    &plan.retained_per_victim,
                    built_reference,
                )
            };
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
            let publish = {
                let mut database_guard = database
                    .try_lock()
                    .map_err(|_| would_block("online GC rewrite hold contended"))?;
                database_guard.gc_database_mut()?.online_gc_finish_segment_reclaim(
                    reference.segment_id,
                    &reference,
                    &retained_ids,
                    built_reference,
                )
            };
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

        // Loose compaction: build off-mutex, publish under one hold.
        if !epoch.compaction_ids.is_empty() {
            if stop.load(Ordering::Acquire) {
                return Err(would_block("online GC epoch stopped"));
            }
            let mut compaction_ids = std::mem::take(&mut epoch.compaction_ids);
            compaction_ids.sort_unstable();
            compaction_ids.dedup();
            let built = self.build_compaction_segment(&blocks, &compaction_ids)?;
            let publish = {
                let mut database_guard = database
                    .try_lock()
                    .map_err(|_| would_block("online GC compaction hold contended"))?;
                database_guard.gc_database_mut()?.online_gc_finish_compaction(&compaction_ids, built.1)
            };
            match publish {
                Ok(metrics) => {
                    epoch.compacted_blocks += u64::from(metrics.blocks_packed);
                    epoch.reclaimed_bytes += metrics.loose_bytes_reclaimed;
                }
                Err(error) => {
                    // Same rule as the rewrite path: only WouldBlock proves
                    // the publish never happened (see above).
                    if error.kind() == io::ErrorKind::WouldBlock {
                        self.discard_built_segment(&blocks, &built.0);
                    } else {
                        return Err(error);
                    }
                }
            }
        }
        Ok(())
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
            |block_id| blocks.get(block_id),
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
    ) -> io::Result<(PayloadSegment, PayloadSegmentReference)> {
        let segment_id = Uuid::new_v4();
        let segment = blocks.payload_segment_store()?.create(
            segment_id,
            sorted_ids.to_vec(),
            |block_id| blocks.get(block_id),
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
