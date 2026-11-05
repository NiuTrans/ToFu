//! Online GC epochs against open off-mutex publication windows
//! (docs/TOFUDB_ONLINE_GC.md rule S9, "Publication windows stay marked").
//! A WAL rotation (preparation to installation), a history compaction and a
//! pack adoption write loose blocks that engine state names only when they
//! install. Before the capture marked those windows, an epoch whose capture
//! fell inside a window unlinked them and the install then published
//! references to missing blocks. Epochs now keep running inside windows.

use super::tests::{metrics, run_epoch, test_budget};
use super::tests_segment_reclaim::UnfinishableMarkVfs;
use super::*;
use crate::entity::EntityKey;
use crate::vfs::DeterministicVfs;

fn key(index: u8) -> EntityKey {
    EntityKey::new(7, 11, "record", &[index]).unwrap()
}

fn put_values(database: &mut AuthorityDatabase) {
    for index in 0..4_u8 {
        let mut transaction = database.begin(7, 11).unwrap();
        database.entity_put(&mut transaction, key(index), vec![index; 64]).unwrap();
        database.commit(transaction).unwrap();
    }
}

fn prepare_rotation(database: &mut AuthorityDatabase) -> Box<crate::engine::PreparedRotationCheckpoint> {
    let engine = database.engine_mut_for_test();
    engine.attach_rotation_driver();
    match engine.try_prepare_rotation_checkpoint().unwrap() {
        crate::engine::RotationCheckpointPreparation::Ready(prepared) => prepared,
        other => panic!("expected rotation, got {other:?}"),
    }
}

fn window_ids(database: &Arc<Mutex<AuthorityDatabase>>) -> Vec<BlockId> {
    database.lock().unwrap().block_store_shared().publication_window_block_ids()
}

/// Every history segment the installed manifest names reads back, every
/// value reads back, and a crash image of the volume reopens.
fn assert_installed_history_complete(database: &Arc<Mutex<AuthorityDatabase>>, vfs: &DeterministicVfs) {
    let mut guard = database.lock().unwrap();
    let manifest = guard.engine_mut_for_test().state().history_manifest_block_id.unwrap();
    guard.read_block_for_test(manifest).expect("the installed history manifest survives");
    for segment in guard.engine_mut_for_test().history_segment_block_ids() {
        guard.read_block_for_test(segment).expect("an installed history segment survives");
    }
    drop(guard);
    let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), Arc::new(vfs.fork_durable().unwrap()))
        .expect("the installed history reopens");
    let mut transaction = reopened.begin(7, 11).unwrap();
    for index in 0..4_u8 {
        assert_eq!(reopened.entity_get(&mut transaction, &key(index)).unwrap(), Some(vec![index; 64]));
    }
}

/// Two epochs captured inside one rotation window complete, reclaim
/// unrelated garbage and keep everything the rotation encoded; before the
/// window was marked they unlinked the encoded segment.
#[test]
fn epochs_inside_a_rotation_window_keep_its_outputs_and_still_reclaim() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    put_values(&mut database);
    let orphan = database.write_orphan_block_for_test(b"garbage from before the window").unwrap();
    let prepared = prepare_rotation(&mut database);
    let database = Arc::new(Mutex::new(database));
    let outcome = crate::engine::encode_rotation_segments(&prepared).unwrap();
    assert!(!window_ids(&database).is_empty(), "the encoded outputs belong to the open window");

    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    run_epoch(&mut collector, &database);
    let inside = metrics(&collector);
    assert_eq!((inside.epochs_completed, inside.deferred_epochs), (2, 0));
    assert_eq!(
        database.lock().unwrap().read_block_for_test(orphan).unwrap_err().kind(),
        io::ErrorKind::NotFound,
        "an open window does not stop reclamation"
    );

    database
        .lock()
        .unwrap()
        .engine_mut_for_test()
        .finish_rotation_checkpoint(prepared, Ok(outcome))
        .unwrap();
    assert!(window_ids(&database).is_empty(), "installation closes the window");
    run_epoch(&mut collector, &database);
    run_epoch(&mut collector, &database);
    assert_installed_history_complete(&database, &vfs);
}

/// History compaction encodes its manifest, routing and promotions off the
/// authority mutex. An epoch whose capture sees the open window and whose
/// deletion hold runs after the install must keep the installed manifest;
/// before the window was marked it unlinked it.
#[test]
fn epoch_spanning_a_history_compaction_install_keeps_the_installed_history() {
    let vfs = UnfinishableMarkVfs::new();
    let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    database.set_write_overlay_for_test(false);
    put_values(&mut database);
    for _ in 0..3 {
        let prepared = prepare_rotation(&mut database);
        let outcome = crate::engine::encode_rotation_segments(&prepared).unwrap();
        database.engine_mut_for_test().finish_rotation_checkpoint(prepared, Ok(outcome)).unwrap();
        database.engine_mut_for_test().commit(b"next history segment").unwrap();
    }
    let database = Arc::new(Mutex::new(database));
    let prepared = database
        .lock()
        .unwrap()
        .engine_mut_for_test()
        .try_prepare_history_compaction(1)
        .unwrap()
        .unwrap();
    let outcome = crate::engine::encode_history_compaction(&prepared);
    assert!(!window_ids(&database).is_empty(), "the encoded manifest belongs to the open window");
    let mut collector = OnlineGarbageCollector::new(test_budget());
    run_epoch(&mut collector, &database);
    // Install between the second epoch's capture and its deletion hold.
    let pending = Arc::new(Mutex::new(Some((prepared, outcome))));
    {
        let database = Arc::clone(&database);
        let pending = Arc::clone(&pending);
        *vfs.before_segment_listing.lock().unwrap() = Some(Box::new(move || {
            let (prepared, outcome) = pending.lock().unwrap().take().unwrap();
            database.lock().unwrap().engine_mut_for_test().finish_history_compaction(prepared, outcome).unwrap();
        }));
    }
    run_epoch(&mut collector, &database);
    assert!(pending.lock().unwrap().is_none(), "the install ran inside the epoch");
    assert!(window_ids(&database).is_empty());
    assert!(metrics(&collector).total_reclaimed_blocks > 0, "the pruned history is reclaimed");
    assert_installed_history_complete(&database, &vfs.inner);
}

/// Pack adoption writes its catalog root off the authority mutex; a capture
/// inside the window marks it, and finishing the adoption closes the window.
#[test]
fn pack_adoption_window_marks_its_catalog_root_until_it_installs() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let mut prepared = database.engine_mut_for_test().prepare_pack_publication_for_test();
    let outcome = crate::engine::publish_prepared_pack_adoption(&mut prepared);
    let capture = database.online_gc_capture(test_budget().maximum_temporary_bytes).unwrap();
    database.engine_mut_for_test().finish_pack_adoption(prepared, outcome).unwrap();
    let installed = database.engine_mut_for_test().state().payload_manifest_block_id.unwrap();
    assert!(capture.snapshot.committed_block_ids.contains(&installed));
    assert!(database.block_store_shared().publication_window_block_ids().is_empty());
}

/// A failed encode restores the covered prefix to the engine and closes the
/// rotation's window; nothing it wrote stays protected.
#[test]
fn aborted_rotation_closes_its_window() {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    put_values(&mut database);
    let prepared = prepare_rotation(&mut database);
    let outcome = crate::engine::encode_rotation_segments(&prepared).unwrap();
    drop(outcome);
    assert!(!database.block_store_shared().publication_window_block_ids().is_empty());
    let error = database
        .engine_mut_for_test()
        .finish_rotation_checkpoint(prepared, Err(io::Error::other("injected encode failure")))
        .unwrap_err();
    assert_eq!(error.to_string(), "injected encode failure");
    assert!(database.block_store_shared().publication_window_block_ids().is_empty());
}
