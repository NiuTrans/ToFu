//! Redundant loose copies of live catalogued blocks (docs/TOFUDB_ONLINE_GC.md,
//! "Redundant loose copies of catalogued blocks"). A crash or a stop between a
//! catalog publication and the removal of the loose copies it made redundant,
//! or a writer that publishes identical content again, leaves a live block
//! with both a loose file and a packed copy. The garbage lane skips it as live
//! and compaction skips it as catalogued, so before the change the copy cost
//! an inode and a scan operation for good. These specifications show that the
//! next completed scan removes each such copy under its own hold, only once
//! both CONTROL slots name the catalog that holds it, and that every value
//! still reads back from either slot.

use super::tests::{backlog_budget, loose_backlog, metrics};
use super::tests_fallback_slot::{destroy_newest_slot, durable_slots};
use super::tests_segment_reclaim::UnfinishableMarkVfs;
use super::tests_short_holds::{
    backlog_still_loose, before_every_hold_attempt, count_holds, epoch, loose_path, observed_authority,
    shard_directory, unlinked_paths, unlinks_per_hold, CountedHolds, Observed, ObservedVfs,
};
use super::*;
use crate::payload_manifest::PayloadSegmentReference;
use crate::vfs::DeterministicVfs;
use std::rc::Rc;

type Backlog = Vec<(BlockId, Vec<u8>)>;

/// Publishes `ids` as one payload segment the way loose compaction does
/// (built and verified off the mutex, published through
/// `online_gc_finish_compaction`) and stops there, like a crash before the
/// copy removal: every loose copy stays beside the catalogued segment.
fn publish_keeping_loose_copies(database: &mut AuthorityDatabase, ids: &[BlockId]) -> io::Result<()> {
    let blocks = database.block_store_shared();
    let segment = blocks.payload_segment_store()?.create(Uuid::new_v4(), ids.to_vec(), |id| blocks.get(id))?;
    let reference =
        PayloadSegmentReference::new(segment.metadata(), segment.first_block_id(), segment.last_block_id())?;
    blocks.verify_payload_segment(&reference)?;
    database.online_gc_finish_compaction(ids, reference).map(|_| ())
}

fn sorted_ids(backlog: &Backlog) -> Vec<BlockId> {
    let mut ids: Vec<BlockId> = backlog.iter().map(|(block_id, _)| *block_id).collect();
    ids.sort_unstable();
    ids
}

fn copy_paths(backlog: &Backlog) -> BTreeSet<PathBuf> {
    backlog.iter().map(|(block_id, _)| loose_path(*block_id)).collect()
}

/// Crash image of a store whose live loose backlog, `segments` segments deep
/// (at least two, so that its commit spills every block to a loose file),
/// was published into the catalog while every loose copy stayed.
fn crash_leftover(segments: usize) -> (DeterministicVfs, Backlog) {
    let (vfs, database) = super::tests::authority();
    let backlog = loose_backlog(&database, segments);
    let ids = sorted_ids(&backlog);
    {
        let mut guard = database.lock().unwrap();
        for segment in ids.chunks(ids.len() / segments) {
            publish_keeping_loose_copies(&mut guard, segment).unwrap();
        }
    }
    drop(database);
    (vfs.fork_durable().unwrap(), backlog)
}

fn reopen_observed(image: DeterministicVfs) -> (Arc<ObservedVfs>, Arc<Mutex<CountedHolds>>) {
    let vfs = ObservedVfs::over(Arc::new(image));
    let database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let database = count_holds(&vfs, Arc::new(Mutex::new(database)));
    (vfs, database)
}

/// Recovers a crash image of `vfs`, from its fallback CONTROL slot when
/// `lose_newest_slot`, reads every backlog value and returns the recovered
/// catalog's segment count.
fn recover_backlog(vfs: &DeterministicVfs, lose_newest_slot: bool, backlog: &Backlog) -> usize {
    let image = Arc::new(vfs.fork_durable().unwrap());
    if lose_newest_slot {
        destroy_newest_slot(&image);
    }
    let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), image).unwrap();
    for (block_id, payload) in backlog {
        assert_eq!(&reopened.read_block_for_test(*block_id).unwrap(), payload);
    }
    reopened.payload_segment_count_for_test()
}

fn assert_backlog_reads(database: &Arc<Mutex<CountedHolds>>, backlog: &Backlog) {
    let guard = database.lock().unwrap();
    for (block_id, payload) in backlog {
        assert_eq!(&guard.authority.read_block_for_test(*block_id).unwrap(), payload);
    }
}

/// Every loose block the catalog also holds; the fixtures hold a few hundred.
fn redundant_copies(database: &Arc<Mutex<CountedHolds>>) -> Vec<BlockId> {
    let blocks = database.lock().unwrap().authority.block_store_shared();
    blocks
        .list_block_ids(4096)
        .unwrap()
        .into_iter()
        .filter(|block_id| blocks.has_catalogued_payload_block(*block_id).unwrap())
        .collect()
}

/// A crash between a compaction's catalog publication and its copy removal
/// (an errored or stopped copy run, or another catalog publisher's crash
/// before its loose cleanup, leaves the same state). Both CONTROL slots
/// already name the catalog, so the next completed scan removes every copy,
/// one unlink per hold and without a CONTROL write, and every value still
/// reads back, from either slot. No catalog slot is left for compaction here,
/// as on the live store at 4,091 of 4,096 segments: the membership probe runs
/// for every live block all the same. The copies of all shards form one run,
/// so every hold after the first waits off the mutex at least as long as the
/// hold before it, across shard boundaries too (the 50% duty rule).
#[test]
fn the_next_scan_removes_every_crash_leftover_copy_one_unlink_per_hold() {
    let (image, backlog) = crash_leftover(2);
    let (vfs, database) = reopen_observed(image);
    assert_eq!(backlog_still_loose(&database, &backlog), backlog.len(), "fixture: every copy survived the crash");
    let mut collector = OnlineGarbageCollector::new(backlog_budget());
    // A third segment would reach the pressure floor; two stay below it.
    collector.set_catalog_pressure_floor_for_test(3);
    let waits: Rc<RefCell<Vec<(Duration, Duration)>>> = Rc::default();
    let reset = {
        let waits = Rc::clone(&waits);
        before_every_hold_attempt(move |previous_hold, off_mutex| waits.borrow_mut().push((previous_hold, off_mutex)))
    };
    vfs.observer.take();
    epoch(&mut collector, &database);
    drop(reset);
    let events = vfs.observer.take();
    let waits = waits.borrow();
    assert_eq!(waits.len(), backlog.len(), "one attempt per copy: nothing contended the authority");
    assert!(
        waits[1..].iter().all(|(previous_hold, off_mutex)| !previous_hold.is_zero() && off_mutex >= previous_hold),
        "every copy hold after the first waits off the mutex at least as long as the hold before it"
    );

    assert_eq!(unlinked_paths(&events), copy_paths(&backlog), "the scan unlinks exactly the redundant copies");
    let (per_hold, off_mutex) = unlinks_per_hold(&events);
    eprintln!(
        "crash leftover: {} copies loose before the scan, {} unlinked in {} holds, {} CONTROL writes",
        backlog.len(),
        per_hold.values().sum::<usize>(),
        per_hold.len(),
        events.iter().filter(|event| matches!(event, Observed::ControlWrite { .. })).count()
    );
    assert_eq!(
        (per_hold.len(), per_hold.values().copied().max(), off_mutex),
        (backlog.len(), Some(1), 0),
        "(holds that unlinked, most unlinks in one hold, unlinks off the mutex)"
    );
    assert!(
        !events.iter().any(|event| matches!(event, Observed::ControlWrite { .. })),
        "both slots already name the catalog"
    );
    let collected = metrics(&collector);
    assert_eq!(
        (collected.total_redundant_copies_removed, collected.total_compacted_blocks),
        (backlog.len() as u64, 0)
    );
    assert_eq!(backlog_still_loose(&database, &backlog), 0);
    assert_backlog_reads(&database, &backlog);
    for lose_newest_slot in [false, true] {
        assert_eq!(recover_backlog(&vfs.inner, lose_newest_slot, &backlog), 2);
    }
}

/// A copy goes only once both CONTROL slots name the catalog that holds it.
/// The second segment's publication reached the newest slot only (its
/// stabilizing write failed), so the fallback slot recovers a catalog without
/// that segment and reaches its blocks only through their loose copies. The
/// first copy hold republishes CONTROL before its unlink; after the run,
/// recovery with the newest slot destroyed still reads every value.
#[test]
fn a_redundant_copy_goes_only_after_both_control_slots_name_its_catalog() {
    let seed = UnfinishableMarkVfs::new();
    let backlog = {
        let database = Arc::new(Mutex::new(
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), seed.clone()).unwrap(),
        ));
        let backlog = loose_backlog(&database, 2);
        let ids = sorted_ids(&backlog);
        let mut guard = database.lock().unwrap();
        publish_keeping_loose_copies(&mut guard, &ids[..ids.len() / 2]).unwrap();
        // Ordinal 1 is the publication's CONTROL write, 2 its stabilization.
        seed.control_writes.store(0, Ordering::SeqCst);
        seed.fail_control_writes_from.store(2, Ordering::SeqCst);
        let error = publish_keeping_loose_copies(&mut guard, &ids[ids.len() / 2..]).unwrap_err();
        assert!(error.to_string().contains("injected CONTROL write failure"), "{error}");
        backlog
    };
    let image = seed.inner.fork_durable().unwrap();
    assert_eq!(recover_backlog(&image, false, &backlog), 2);
    assert_eq!(
        recover_backlog(&image, true, &backlog),
        1,
        "fixture: the fallback slot reaches the second segment's blocks only through their loose copies"
    );
    let (vfs, database) = reopen_observed(image);
    let slots = durable_slots(&vfs.inner);
    assert_ne!(
        slots[0].payload_manifest_block_id, slots[1].payload_manifest_block_id,
        "fixture: the slots name the two- and one-segment catalogs"
    );
    let mut collector = OnlineGarbageCollector::new(backlog_budget());
    vfs.observer.take();
    epoch(&mut collector, &database);
    let events = vfs.observer.take();

    let first_unlink = events
        .iter()
        .position(|event| matches!(event, Observed::Unlink { .. }))
        .expect("the copies go");
    let Observed::Unlink { hold: first_hold, .. } = events[first_unlink].clone() else { unreachable!() };
    let control_writes: Vec<&Observed> =
        events.iter().filter(|event| matches!(event, Observed::ControlWrite { .. })).collect();
    assert_eq!(
        control_writes,
        vec![&Observed::ControlWrite { hold: first_hold }],
        "only the first copy hold writes CONTROL"
    );
    assert!(
        events[..first_unlink].contains(&Observed::ControlWrite { hold: first_hold }),
        "it republishes before its unlink"
    );
    assert_eq!(unlinked_paths(&events), copy_paths(&backlog));
    assert_eq!(unlinks_per_hold(&events).0.values().copied().max(), Some(1));
    eprintln!(
        "fallback slot: {} copies unlinked, {} CONTROL write, in the hold of the first unlink",
        unlinked_paths(&events).len(),
        control_writes.len()
    );
    let installed = database.lock().unwrap().authority.writer_engine().unwrap().state().payload_manifest_block_id;
    assert!(durable_slots(&vfs.inner).iter().all(|state| state.payload_manifest_block_id == installed));
    for lose_newest_slot in [false, true] {
        assert_eq!(recover_backlog(&vfs.inner, lose_newest_slot, &backlog), 2);
    }
}

/// A foreground writer publishes the content of a catalogued block again (a
/// rotation, history promotion's loose fallback and a checkpoint of a fresh
/// copy-on-write page all can) right after the run removed that block's copy,
/// at the shard barrier off the mutex, and commits a reference to it. The
/// commit lands, every value reads back throughout, and the next scan removes
/// the recreated copy under one more hold.
#[test]
fn a_copy_recreated_during_the_run_keeps_its_value_and_goes_in_the_next_scan() {
    let (image, backlog) = crash_leftover(2);
    let (vfs, database) = reopen_observed(image);
    let (recreated, payload) = backlog.iter().min_by_key(|(block_id, _)| *block_id).cloned().unwrap();
    let foreground: Arc<Mutex<Option<Result<(), String>>>> = Arc::default();
    {
        let database = Arc::downgrade(&database);
        let foreground = Arc::clone(&foreground);
        vfs.arm_directory_sync(
            shard_directory(recreated),
            Box::new(move || {
                let database = database.upgrade().unwrap();
                let outcome = std::thread::spawn(move || -> Result<(), String> {
                    let mut guard =
                        database.try_lock().map_err(|_| "the authority mutex is held".to_owned())?;
                    let authority = &mut guard.authority;
                    authority.write_orphan_block_for_test(&payload).map_err(|error| error.to_string())?;
                    authority.commit_block_payloads_for_test(&[payload]).map(|_| ()).map_err(|error| error.to_string())
                })
                .join()
                .unwrap();
                *foreground.lock().unwrap() = Some(outcome);
            }),
        );
    }
    let mut collector = OnlineGarbageCollector::new(backlog_budget());
    vfs.observer.take();
    epoch(&mut collector, &database);
    let first = vfs.observer.take();
    let outcome = foreground.lock().unwrap().take().expect("the run reached the recreated copy's shard barrier");
    assert_eq!(outcome, Ok(()), "the foreground recreates the copy and commits between two holds");
    assert_eq!(unlinked_paths(&first), copy_paths(&backlog));
    assert_eq!(backlog_still_loose(&database, &backlog), 1, "only the recreated copy is loose");
    assert_backlog_reads(&database, &backlog);

    epoch(&mut collector, &database);
    let second = vfs.observer.take();
    let removed: BTreeSet<PathBuf> = unlinked_paths(&second).intersection(&copy_paths(&backlog)).cloned().collect();
    assert_eq!(removed, BTreeSet::from([loose_path(recreated)]));
    assert_eq!(unlinks_per_hold(&second).0.values().copied().max(), Some(1));
    eprintln!(
        "recreated copy: {} copies unlinked in the first scan, {} recreated, {} unlinked in the next",
        unlinked_paths(&first).len(),
        1,
        removed.len()
    );
    assert_eq!(metrics(&collector).total_redundant_copies_removed, backlog.len() as u64 + 1);
    assert_eq!(backlog_still_loose(&database, &backlog), 0);
    assert_backlog_reads(&database, &backlog);
    for lose_newest_slot in [false, true] {
        assert_eq!(recover_backlog(&vfs.inner, lose_newest_slot, &backlog), 2);
    }
}

/// A stop in the middle of a compaction's copy run (a daemon shutdown during
/// the run) leaves the rest of that segment's copies, up to 65,536 files per
/// segment. The next epoch's scan finds each of them catalogued and removes
/// it, one per hold, while compaction goes on with the rest of the backlog.
#[test]
fn copies_a_stopped_compaction_run_left_go_in_the_next_epoch() {
    let (vfs, database) = observed_authority();
    let backlog = loose_backlog(&database, 2);
    let database = count_holds(&vfs, database);
    let mut collector = OnlineGarbageCollector::new(backlog_budget());
    let stop = Arc::new(AtomicBool::new(false));
    *vfs.observer.stop_after_unlinks.lock().unwrap() = Some((3, Arc::clone(&stop)));
    vfs.observer.take();
    let error = collector.run_epoch(&database, &stop).unwrap_err();
    assert!(error.to_string().contains("online GC epoch stopped"), "{error}");
    assert_eq!(unlinked_paths(&vfs.observer.take()).len(), 3, "the stop ends the copy run after its third unlink");
    assert_eq!(database.lock().unwrap().authority.payload_segment_count_for_test(), 1);
    let packed = database.lock().unwrap().authority.block_store_shared().installed_payload_manifest().unwrap()
        .expect("the first segment was published").segments[0].block_count as usize;
    let left = redundant_copies(&database);
    assert_eq!(left.len(), packed - 3, "the stopped run left the rest of its segment's copies");

    stop.store(false, Ordering::Release);
    epoch(&mut collector, &database);
    let next = vfs.observer.take();
    let unlinked = unlinked_paths(&next);
    assert!(left.iter().all(|block_id| unlinked.contains(&loose_path(*block_id))), "every leftover copy goes");
    assert_eq!(unlinks_per_hold(&next).0.values().copied().max(), Some(1));
    assert_eq!(metrics(&collector).total_redundant_copies_removed, left.len() as u64);
    assert!(redundant_copies(&database).is_empty());
    let (next_holds, _) = unlinks_per_hold(&next);
    eprintln!(
        "stopped copy run: 3 copies unlinked before the stop, {} left; the next epoch removed them ({} \
         unlinks in {} holds, its own compaction's copies included)",
        left.len(),
        next_holds.values().sum::<usize>(),
        next_holds.len()
    );
    assert_backlog_reads(&database, &backlog);
    for lose_newest_slot in [false, true] {
        recover_backlog(&vfs.inner, lose_newest_slot, &backlog);
    }
}
