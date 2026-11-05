//! Loose-block deletion against the fallback CONTROL slot
//! (docs/TOFUDB_ONLINE_GC.md, "Loose deletion and the fallback CONTROL
//! slot"). Recovery selects the older slot when the newest one is
//! unreadable and replays the same WAL from it, so it needs that slot's
//! history manifest and payload catalog. Loose deletion publishes no catalog
//! of its own; these fixtures prove that it never unlinks what the fallback
//! slot recovers from, and that it writes CONTROL only when that slot names
//! other roots.

use super::tests::{metrics, run_epoch, test_budget};
use super::tests_segment_reclaim::UnfinishableMarkVfs;
use super::*;
use crate::control::{ControlFile, ControlState};
use crate::entity::EntityKey;
use crate::vfs::{sync_all_barrier, DeterministicVfs, OpenRequest};

pub(super) fn key() -> EntityKey {
    EntityKey::new(7, 11, "record", b"fallback-slot").unwrap()
}

/// CONTROL states recovery can select from `vfs` as it stands, newest first.
pub(super) fn durable_slots(vfs: &DeterministicVfs) -> Vec<ControlState> {
    let mut control = ControlFile::open_with_vfs(Path::new("/data"), Arc::new(vfs.clone())).unwrap();
    control.read_current().unwrap();
    control.selectable_states_for_test()
}

/// Media loss of the newest durable slot: the next open selects the other.
pub(super) fn destroy_newest_slot(vfs: &DeterministicVfs) {
    let newest = durable_slots(vfs)[0].generation;
    let mut control = vfs
        .open(Path::new("/data/CONTROL"), OpenRequest { write: true, ..OpenRequest::default() })
        .unwrap();
    control.write_all_at(((newest - 1) % 2) * 4096, &[0xa5; 64]).unwrap();
    sync_all_barrier(control.as_mut()).unwrap();
}

/// Opens a crash image of `vfs` after destroying its newest slot, and reads
/// the value and every history segment the recovered manifest names.
pub(super) fn recover_from_fallback(vfs: &DeterministicVfs) -> io::Result<(Option<BlockId>, Vec<u8>)> {
    let image = Arc::new(vfs.fork_durable()?);
    destroy_newest_slot(&image);
    let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), image)?;
    let manifest = database.writer_engine()?.state().history_manifest_block_id;
    for segment in database.engine_mut_for_test().history_segment_block_ids() {
        database.read_block_for_test(segment)?;
    }
    let mut transaction = database.begin(7, 11)?;
    let value = database
        .entity_get(&mut transaction, &key())?
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "recovered value is missing"))?;
    Ok((manifest, value))
}

/// One checkpointed value, three checkpointed history segments without
/// carried payloads (inline-only envelopes, as overlay deltas are), then
/// history pruning to one segment. Such segments have no payload index, so
/// pruning promotes nothing, changes no catalog and publishes one CONTROL
/// generation: the fallback slot keeps naming the four-segment manifest on
/// the same WAL. No dead payload-segment content exists, so no catalog
/// publication of the collector moves that slot either.
pub(super) fn pruned_history_authority(vfs: Arc<dyn Vfs>) -> AuthorityDatabase {
    let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs).unwrap();
    // A direct tree write leaves no superseded overlay pages behind, so its
    // packed segment holds no dead content to rewrite.
    database.set_write_overlay_for_test(false);
    let mut transaction = database.begin(7, 11).unwrap();
    database.entity_put(&mut transaction, key(), b"value-v3".to_vec()).unwrap();
    database.commit(transaction).unwrap();
    database.checkpoint_for_test().unwrap();
    for round in 0..3 {
        database
            .engine_mut_for_test()
            .commit(format!("inline-only history {round}").as_bytes())
            .unwrap();
        database.checkpoint_for_test().unwrap();
    }
    database.compact_history(1).unwrap();
    database
}

#[test]
fn loose_deletion_keeps_every_block_the_fallback_control_slot_recovers_from() {
    let vfs = UnfinishableMarkVfs::new();
    let database = Arc::new(Mutex::new(pruned_history_authority(vfs.clone())));
    let current_manifest = database.lock().unwrap().writer_engine().unwrap().state().history_manifest_block_id;
    let slots = durable_slots(&vfs.inner);
    assert_eq!(slots.len(), 2);
    assert_eq!(slots[0].history_manifest_block_id, current_manifest);
    let fallback_manifest = slots[1].history_manifest_block_id;
    assert_ne!(fallback_manifest, current_manifest, "fixture: the fallback names the pruned manifest");
    assert_eq!(slots[1].active_log_generation, slots[0].active_log_generation,
        "fixture: both slots replay the same retained WAL");
    // Before any collection the fallback slot is a complete recovery path.
    assert_eq!(
        recover_from_fallback(&vfs.inner).unwrap(),
        (fallback_manifest, b"value-v3".to_vec())
    );

    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    assert_eq!(metrics(&collector).total_reclaimed_blocks, 0, "the first mark confirms nothing");
    vfs.control_writes.store(0, Ordering::SeqCst);
    run_epoch(&mut collector, &database);
    assert!(metrics(&collector).total_reclaimed_blocks > 0, "pruned history is garbage");

    // Losing the newest slot after the deletion must still recover.
    let (recovered_manifest, value) = recover_from_fallback(&vfs.inner)
        .expect("the fallback CONTROL slot must recover after loose deletion");
    assert_eq!(value, b"value-v3");
    assert_eq!(recovered_manifest, current_manifest);
    // Exactly one CONTROL publication moved the fallback slot, and nothing
    // stays retained for it: no slot names the pruned manifest any more.
    assert_eq!(vfs.control_writes.load(Ordering::SeqCst), 1);
    assert!(durable_slots(&vfs.inner)
        .iter()
        .all(|state| state.history_manifest_block_id == current_manifest));
    assert_eq!(
        database.lock().unwrap().read_block_for_test(fallback_manifest.unwrap()).unwrap_err().kind(),
        io::ErrorKind::NotFound
    );

    // Slots that already recover the current roots cost no CONTROL I/O.
    let orphan = database.lock().unwrap().write_orphan_block_for_test(b"later orphan").unwrap();
    run_epoch(&mut collector, &database);
    vfs.control_writes.store(0, Ordering::SeqCst);
    run_epoch(&mut collector, &database);
    assert_eq!(
        database.lock().unwrap().read_block_for_test(orphan).unwrap_err().kind(),
        io::ErrorKind::NotFound
    );
    assert_eq!(vfs.control_writes.load(Ordering::SeqCst), 0);
    assert_eq!(recover_from_fallback(&vfs.inner).unwrap().1, b"value-v3");
}

/// The common shape: the pruned versions also left dead pages in packed
/// segments, so the deleting epoch ends with a segment rewrite whose catalog
/// publication moves the fallback slot. Loose victims go first; a crash
/// before that publication (here every CONTROL write of the epoch fails)
/// must still leave the fallback slot recoverable.
#[test]
fn crash_before_the_epochs_catalog_publication_keeps_the_fallback_slot_recoverable() {
    let vfs = UnfinishableMarkVfs::new();
    let database = {
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        for value in [b"value-v1", b"value-v2", b"value-v3"] {
            let mut transaction = database.begin(7, 11).unwrap();
            database.entity_put(&mut transaction, key(), value.to_vec()).unwrap();
            database.commit(transaction).unwrap();
            database.checkpoint_for_test().unwrap();
        }
        database.compact_history(1).unwrap();
        Arc::new(Mutex::new(database))
    };
    let slots = durable_slots(&vfs.inner);
    let fallback_manifest = slots[1].history_manifest_block_id;
    assert_ne!(fallback_manifest, slots[0].history_manifest_block_id);
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    vfs.control_writes.store(0, Ordering::SeqCst);
    vfs.fail_control_writes_from.store(1, Ordering::SeqCst);
    let error = collector.run_epoch(&database, &AtomicBool::new(false)).unwrap_err();
    assert!(error.to_string().contains("injected CONTROL write failure"), "{error}");
    assert_eq!(
        recover_from_fallback(&vfs.inner)
            .expect("no loose victim may be unlinked while the fallback slot names it"),
        (fallback_manifest, b"value-v3".to_vec())
    );
}

/// Packing several segments in one epoch keeps rule S8 per segment: a
/// segment's loose copies go only after both CONTROL slots name the catalog
/// holding it. A failed stabilizing write in the middle of the epoch's
/// segments ends compaction with that segment's copies still loose, and
/// recovery from either slot reads the whole backlog.
#[test]
fn compaction_failing_between_a_middle_segments_control_publications_keeps_its_loose_copies() {
    use super::tests::{backlog_budget, loose_backlog, loose_backlog_remaining};
    let seed = {
        let vfs = UnfinishableMarkVfs::new();
        let database = Arc::new(Mutex::new(
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap(),
        ));
        let backlog = loose_backlog(&database, 3);
        drop(database);
        vfs.inner.crash().unwrap();
        (vfs.inner.clone(), backlog)
    };
    let (seed, backlog) = seed;
    let open = || {
        let vfs = UnfinishableMarkVfs::over(Arc::new(seed.fork_durable().unwrap()));
        let database =
            Arc::new(Mutex::new(AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap()));
        (vfs, database)
    };
    // Dry run on an identical fork: three segments, two CONTROL writes each,
    // and the trailing segment's two are the epoch's last.
    let control_writes = {
        let (vfs, database) = open();
        let mut collector = OnlineGarbageCollector::new(backlog_budget());
        vfs.control_writes.store(0, Ordering::SeqCst);
        run_epoch(&mut collector, &database);
        assert_eq!(metrics(&collector).total_compaction_segments, 3);
        assert_eq!(loose_backlog_remaining(&database, &backlog), 0);
        vfs.control_writes.load(Ordering::SeqCst)
    };
    let (vfs, database) = open();
    let mut collector = OnlineGarbageCollector::new(backlog_budget());
    vfs.control_writes.store(0, Ordering::SeqCst);
    vfs.fail_control_writes_from.store(control_writes - 2, Ordering::SeqCst);
    let error = collector.run_epoch(&database, &AtomicBool::new(false)).unwrap_err();
    assert!(error.to_string().contains("injected CONTROL write failure"), "{error}");
    assert_eq!(
        loose_backlog_remaining(&database, &backlog),
        2 * backlog.len() / 3,
        "only the first segment's copies may go: the second never reached both slots"
    );
    let slots = durable_slots(&vfs.inner);
    assert_eq!(slots.len(), 2);
    assert_ne!(
        slots[0].payload_manifest_block_id, slots[1].payload_manifest_block_id,
        "fixture: the slots name the one- and two-segment catalogs"
    );
    drop(database);
    for lose_newest in [false, true] {
        let image = Arc::new(vfs.inner.fork_durable().unwrap());
        if lose_newest {
            destroy_newest_slot(&image);
        }
        let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), image).unwrap();
        for (block_id, payload) in &backlog {
            assert_eq!(&reopened.read_block_for_test(*block_id).unwrap(), payload);
        }
    }
}
