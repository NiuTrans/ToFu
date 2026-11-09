//! Reclamation throughput under foreground load (docs/TOFUDB_ONLINE_GC.md,
//! "Reclamation keeps its pace under foreground load"). On the live volume
//! 82,813 superseded segment files (9.1 TB) waited for a lane that ended each
//! pass at its first contended hold, every epoch failed after its mark without
//! a trace, a contended consolidation publish failed the epoch and left its
//! built segment behind, and an epoch deleted at most 256 MiB of loose
//! garbage. These specifications keep each of those paths moving.

use super::tests::{assert_live_value, authority, metrics, run_epoch, test_budget};
use super::tests_segment_reclaim::{
    fixture, leave_superseded_segments, live_key, run_epoch_with_aborted_mark, segment_files,
    segments_holding, small_live_segments, UnfinishableMarkVfs, UNFINISHED_MARK_ERROR,
};
use super::tests_short_holds::before_every_hold_attempt;
use super::*;
use std::sync::atomic::AtomicUsize;
use std::sync::mpsc;

/// A foreground thread holding the authority mutex until it is released,
/// and how many collector hold attempts saw it held.
#[derive(Default)]
struct Held {
    holder: Option<(mpsc::Sender<()>, std::thread::JoinHandle<()>)>,
    attempts_seen: usize,
}

type HeldAuthority = Arc<Mutex<Held>>;

/// Takes the authority on another thread and returns once it holds it.
fn hold_authority<D: Send + 'static>(database: &Arc<Mutex<D>>, held: &HeldAuthority) {
    let database = Arc::clone(database);
    let (locked, entered) = mpsc::channel();
    let (release, released) = mpsc::channel::<()>();
    let thread = std::thread::spawn(move || {
        let _guard = database.lock().unwrap();
        locked.send(()).unwrap();
        let _ = released.recv();
    });
    entered.recv().unwrap();
    *held.lock().unwrap() = Held { holder: Some((release, thread)), attempts_seen: 0 };
}

/// Lets the holder go; `true` when it still held the authority.
fn release_authority(held: &HeldAuthority) -> bool {
    let holder = held.lock().unwrap().holder.take();
    match holder {
        Some((release, thread)) => {
            let _ = release.send(());
            thread.join().unwrap();
            true
        }
        None => false,
    }
}

/// The collector hold seam: the first attempt after the authority was taken
/// finds it held and waits for it; the holder lets go on its own a moment
/// later, so that attempt succeeds only because it waited.
fn release_while_the_first_attempt_waits(held: &HeldAuthority) -> super::tests_short_holds::ResetHoldAttemptHook {
    let held = Arc::clone(held);
    before_every_hold_attempt(move |_, _| {
        let holder = {
            let mut state = held.lock().unwrap();
            if state.holder.is_none() {
                return;
            }
            state.attempts_seen += 1;
            state.holder.take()
        };
        if let Some((release, thread)) = holder {
            std::thread::spawn(move || {
                std::thread::sleep(Duration::from_millis(20));
                let _ = release.send(());
                thread.join().unwrap();
            });
        }
    })
}

/// One epoch whose mark build aborts, through any collector port.
fn aborted_epoch<D: OnlineGcDatabase>(
    collector: &mut OnlineGarbageCollector,
    database: &Arc<Mutex<D>>,
    vfs: &UnfinishableMarkVfs,
) {
    vfs.abort_marks.store(true, Ordering::Release);
    let error = collector.run_epoch(database, &AtomicBool::new(false)).unwrap_err();
    vfs.abort_marks.store(false, Ordering::Release);
    assert_eq!(error.kind(), io::ErrorKind::WouldBlock, "{error}");
    assert!(error.to_string().contains(UNFINISHED_MARK_ERROR), "{error}");
}

/// The collector's port to one authority, refusing the next `refusals`
/// mutating holds with `WouldBlock`, as an off-mutex publication that owns
/// the physical lane does for as long as it runs.
struct RefusingHolds {
    authority: AuthorityDatabase,
    refusals: Arc<AtomicUsize>,
}

impl OnlineGcDatabase for RefusingHolds {
    fn gc_database(&self) -> io::Result<&AuthorityDatabase> {
        Ok(&self.authority)
    }
    fn gc_database_mut(&mut self) -> io::Result<&mut AuthorityDatabase> {
        if self.refusals.load(Ordering::SeqCst) > 0 {
            self.refusals.fetch_sub(1, Ordering::SeqCst);
            return Err(would_block("a publication owns the physical lane"));
        }
        Ok(&mut self.authority)
    }
    fn install_gc_handle(&mut self, handle: OnlineGarbageCollectionHandle) -> io::Result<()> {
        self.authority.install_gc_handle(handle)
    }
}

fn refusing(database: Arc<Mutex<AuthorityDatabase>>, refusals: &Arc<AtomicUsize>) -> Arc<Mutex<RefusingHolds>> {
    let authority = Arc::try_unwrap(database)
        .ok()
        .expect("the fixture released every handle")
        .into_inner()
        .unwrap();
    Arc::new(Mutex::new(RefusingHolds { authority, refusals: Arc::clone(refusals) }))
}

/// A contended authority makes the next orphan removal wait instead of ending
/// the pass: every confirmed file goes in the pass that confirmed it. A
/// foreground thread takes the authority while the second observation lists
/// the directory and keeps it while the first removal attempt waits. Before
/// the change the pass ended at that attempt; on the live volume a pass removed
/// a few dozen of the confirmed files and the next pass came an hour later.
#[test]
fn contended_orphan_removals_retry_until_every_confirmed_file_goes() {
    let (vfs, database) = fixture();
    let live_files = segment_files(&vfs);
    leave_superseded_segments(&database, 0, 6);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
    assert_eq!(segment_files(&vfs), live_files + 6, "the first observation confirms nothing");

    let held = HeldAuthority::default();
    {
        let database = Arc::clone(&database);
        let held = Arc::clone(&held);
        *vfs.before_segment_listing.lock().unwrap() = Some(Box::new(move || hold_authority(&database, &held)));
    }
    let reset = release_while_the_first_attempt_waits(&held);
    run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
    drop(reset);
    let still_held = release_authority(&held);

    assert!(!still_held, "the pass waited for the contended removal");
    assert_eq!(segment_files(&vfs), live_files, "every confirmed orphan goes in the contended pass");
    assert_eq!(metrics(&collector).total_orphan_segments_removed, 6);
    assert_live_value(&database, &live_key(), b"live-value");
}

/// A removal hold refused with `WouldBlock` (a publication owns the physical
/// lane, or the CONTROL slots are briefly apart) is retried with the same
/// backoff instead of ending the pass. Before the change the pass ended at
/// the first refusal, and publications run for seconds every few seconds on
/// a full catalog.
#[test]
fn refused_orphan_removals_retry_until_every_confirmed_file_goes() {
    let (vfs, database) = fixture();
    let live_files = segment_files(&vfs);
    leave_superseded_segments(&database, 0, 4);
    let refusals = Arc::new(AtomicUsize::new(0));
    let database = refusing(database, &refusals);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    aborted_epoch(&mut collector, &database, &vfs);
    refusals.store(3, Ordering::SeqCst);
    aborted_epoch(&mut collector, &database, &vfs);

    assert_eq!(refusals.load(Ordering::SeqCst), 0, "every refusal was met by a retry");
    assert_eq!(segment_files(&vfs), live_files, "every confirmed orphan goes in the refused pass");
    assert_eq!(metrics(&collector).total_orphan_segments_removed, 4);
}

/// A failed epoch is reported, not only counted as deferred: `failed_epochs`
/// counts it and `epoch_error` keeps its error until an epoch completes. A
/// stop is a shutdown, not a failure. Before the change the driver dropped
/// every epoch error, so a live collector whose every epoch failed after an
/// hour-long mark showed nothing but a growing `deferred_epochs`.
#[test]
fn a_failed_epoch_is_reported_until_an_epoch_completes() {
    let (vfs, database) = fixture();
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch_with_aborted_mark(&mut collector, &database, &vfs);
    let failed = metrics(&collector);
    assert_eq!((failed.failed_epochs, failed.epochs_completed), (1, 0));
    assert!(
        failed.epoch_error.as_deref().is_some_and(|error| error.contains(UNFINISHED_MARK_ERROR)),
        "{failed:?}"
    );

    let stop = Arc::new(AtomicBool::new(false));
    {
        let stop = Arc::clone(&stop);
        *vfs.before_segment_listing.lock().unwrap() = Some(Box::new(move || stop.store(true, Ordering::Release)));
    }
    let stopped = collector.run_epoch(&database, &stop).unwrap_err();
    assert!(stopped.to_string().contains("online GC epoch stopped"), "{stopped}");
    let after_stop = metrics(&collector);
    assert_eq!(after_stop.failed_epochs, 1, "a stop is not a failed epoch");
    assert_eq!(after_stop.epoch_error, failed.epoch_error);

    run_epoch(&mut collector, &database);
    let recovered = metrics(&collector);
    assert_eq!((recovered.failed_epochs, recovered.epochs_completed, recovered.epoch_error), (1, 1, None));
}

/// A consolidation publish that finds the authority held waits for it like
/// every other collector hold, so the epoch neither fails nor leaves the
/// replacement it built behind. A foreground thread takes the authority at
/// the replacement's directory barrier and keeps it while the publish attempt
/// waits. Before the change the epoch failed with "online GC consolidation
/// hold contended" and the built segment stayed as an unreferenced file.
#[test]
fn a_contended_consolidation_publish_neither_fails_the_epoch_nor_leaks_its_segment() {
    let (vfs, database) = fixture();
    let packed = small_live_segments(&database);
    let blocks = database.lock().unwrap().block_store_shared();
    assert_eq!(segments_holding(&blocks, &packed).len(), 3);
    let held = HeldAuthority::default();
    let reset = release_while_the_first_attempt_waits(&held);
    let mut collector =
        OnlineGarbageCollector::with_min_epoch_interval(test_budget(), Duration::from_millis(1));
    collector.set_catalog_pressure_floor_for_test(1);
    for _ in 0..64 {
        {
            let database = Arc::clone(&database);
            let held = Arc::clone(&held);
            *vfs.before_segment_directory_sync.lock().unwrap() =
                Some(Box::new(move || hold_authority(&database, &held)));
        }
        let outcome = collector.run_epoch(&database, &AtomicBool::new(false));
        release_authority(&held);
        assert_eq!(outcome.unwrap(), OnlineGarbageCollectionOutcome::EpochComplete);
        if metrics(&collector).total_consolidations > 0 {
            break;
        }
    }
    drop(reset);
    *vfs.before_segment_directory_sync.lock().unwrap() = None;
    assert!(metrics(&collector).total_consolidations > 0, "{:?}", metrics(&collector));

    let installed: BTreeSet<Uuid> = blocks
        .installed_payload_manifest()
        .unwrap()
        .unwrap()
        .segments
        .iter()
        .map(|reference| reference.segment_id)
        .collect();
    let parked: BTreeSet<Uuid> =
        blocks.payload_segment_store().unwrap().deferred_retirement_ids().unwrap().into_iter().collect();
    for path in vfs.inner.read_directory(Path::new("/data/payload-segments")).unwrap() {
        let name = path.file_name().unwrap().to_str().unwrap().to_owned();
        let segment_id = Uuid::parse_str(name.strip_suffix(".pseg").unwrap()).unwrap();
        assert!(
            installed.contains(&segment_id) || parked.contains(&segment_id),
            "segment {name} is neither catalogued nor parked"
        );
    }
    let guard = database.lock().unwrap();
    for (block_id, payload) in &packed {
        assert_eq!(&guard.read_block_for_test(*block_id).unwrap(), payload);
    }
    drop(guard);
    assert_live_value(&database, &live_key(), b"live-value");
}

/// Online loose deletion unlinks one file per hold, so the offline round's
/// victim byte cap bounds no hold: one epoch deletes every confirmed victim
/// its count admits, whatever their sizes. Before the change an epoch deleted
/// at most `maximum_victim_bytes` (256 MiB in production, about seventy
/// history routing pages) while the live volume held terabytes of them.
#[test]
fn one_epoch_deletes_every_confirmed_loose_victim_beyond_the_victim_byte_cap() {
    let (_vfs, database) = authority();
    let garbage: Vec<BlockId> = {
        let guard = database.lock().unwrap();
        (0..8_u8)
            .map(|index| guard.write_orphan_block_for_test(&[index; 2048]).unwrap())
            .collect()
    };
    let budget = AuthorityGarbageCollectionBudget { maximum_victim_bytes: 4096, ..test_budget() };
    let mut collector = OnlineGarbageCollector::new(budget);
    run_epoch(&mut collector, &database);
    run_epoch(&mut collector, &database);
    let guard = database.lock().unwrap();
    let survivors = garbage
        .iter()
        .filter(|block_id| {
            !guard
                .read_block_for_test(**block_id)
                .is_err_and(|error| error.kind() == io::ErrorKind::NotFound)
        })
        .count();
    assert_eq!(survivors, 0, "confirmed victims outlived the epoch that confirmed them");
}

fn orphan_victims(database: &Arc<Mutex<AuthorityDatabase>>, count: u8) -> Vec<(BlockId, Vec<u8>)> {
    let guard = database.lock().unwrap();
    (0..count)
        .map(|index| {
            let payload = vec![index; 64];
            (guard.write_orphan_block_for_test(&payload).unwrap(), payload)
        })
        .collect()
}

fn unlinked(authority: &AuthorityDatabase, victims: &[(BlockId, Vec<u8>)]) -> Vec<usize> {
    victims
        .iter()
        .enumerate()
        .filter(|(_, (block_id, _))| {
            authority.read_block_for_test(*block_id).is_err_and(|error| error.kind() == io::ErrorKind::NotFound)
        })
        .map(|(index, _)| index)
        .collect()
}

/// A loose-victim deletion hold refused with `WouldBlock` (a publication owns
/// the physical lane) is retried after the run's backoff, as orphan segment
/// removals and redundant copies already were, instead of ending the epoch's
/// deletion. Live on 2026-11-08 every main-authority epoch since 11-07 ended
/// with `reclaimed_blocks=0` while the scan's copy removals succeeded: history
/// compaction, rotation and pack adoption own the lane most of the time.
#[test]
fn refused_loose_victim_deletions_retry_until_every_confirmed_victim_goes() {
    let (_vfs, database) = authority();
    let victims = orphan_victims(&database, 8);
    let refusals = Arc::new(AtomicUsize::new(0));
    let database = refusing(database, &refusals);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    let stop = AtomicBool::new(false);
    assert_eq!(collector.run_epoch(&database, &stop).unwrap(), OnlineGarbageCollectionOutcome::EpochComplete);
    refusals.store(3, Ordering::SeqCst);
    assert_eq!(collector.run_epoch(&database, &stop).unwrap(), OnlineGarbageCollectionOutcome::EpochComplete);
    assert_eq!(refusals.load(Ordering::SeqCst), 0, "every refusal was met by a retry");
    assert_eq!(unlinked(&database.lock().unwrap().authority, &victims).len(), victims.len());
    assert_eq!(metrics(&collector).total_reclaimed_blocks, victims.len() as u64);
}

/// The scan unlinks each shard's confirmed victims before it lists the next
/// shard, so no victim list and no count (`maximum_blocks_per_round` sizes the
/// offline round) bounds what one epoch reclaims. Live, an epoch admitted at
/// most 65,536 of ~30M garbage loose files.
#[test]
fn one_epoch_deletes_every_confirmed_loose_victim_beyond_the_offline_round_count() {
    let (_vfs, database) = authority();
    let victims = orphan_victims(&database, 8);
    let budget = AuthorityGarbageCollectionBudget { maximum_blocks_per_round: 2, ..test_budget() };
    let mut collector = OnlineGarbageCollector::new(budget);
    run_epoch(&mut collector, &database);
    run_epoch(&mut collector, &database);
    assert_eq!(unlinked(&database.lock().unwrap(), &victims).len(), victims.len());
}

/// A scan that unlinked victims and then stopped leaves the next epoch to
/// confirm from the older suspects, observed before those unlinks: a victim
/// it unlinked and that is written again is a fresh observation and never
/// confirms from them (rule S10).
#[test]
fn victims_a_stopped_scan_unlinked_never_confirm_from_the_older_suspects() {
    let (_vfs, database) = authority();
    let victims = orphan_victims(&database, 4);
    let mut collector = OnlineGarbageCollector::with_min_epoch_interval(test_budget(), Duration::from_millis(1));
    run_epoch(&mut collector, &database);
    let stop = Arc::new(AtomicBool::new(false));
    let attempts = Arc::new(AtomicUsize::new(0));
    let reset = {
        let (stop, attempts) = (Arc::clone(&stop), Arc::clone(&attempts));
        before_every_hold_attempt(move |_, _| {
            if attempts.fetch_add(1, Ordering::SeqCst) == 1 {
                stop.store(true, Ordering::Release);
            }
        })
    };
    let stopped = collector.run_epoch(&database, &stop).unwrap_err();
    drop(reset);
    assert!(stopped.to_string().contains("online GC epoch stopped"), "{stopped}");
    let first = unlinked(&database.lock().unwrap(), &victims);
    assert_eq!(first.len(), 1, "the scan stops after its first unlink");
    let rewritten = first[0];
    database.lock().unwrap().write_orphan_block_for_test(&victims[rewritten].1).unwrap();
    run_epoch(&mut collector, &database);
    let guard = database.lock().unwrap();
    assert!(guard.read_block_for_test(victims[rewritten].0).is_ok(),
        "a rewritten victim confirmed from suspects observed before its unlink");
    assert_eq!(unlinked(&guard, &victims).len(), victims.len() - 1);
}
