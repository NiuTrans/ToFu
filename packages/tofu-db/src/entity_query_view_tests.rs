//! Published query root specification: live roots, registered pins, source
//! failure, capacity and deterministic pre-publication reader handoff.
use super::*;
use crate::vfs::{DeterministicVfs, Vfs};
use std::{path::Path, sync::mpsc, thread, time::Duration};
fn database() -> EntityDatabase {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    EntityDatabase::initialize_with_vfs(Path::new("/data"), vfs).unwrap()
}
fn key(owner: u64) -> EntityKey {
    EntityKey::new(7, owner, "query-view", b"key").unwrap()
}
fn put(database: &mut EntityDatabase, bytes: &[u8]) {
    let mut tx = database.begin(7, 11).unwrap();
    database.put(&mut tx, key(11), bytes.to_vec()).unwrap();
    database.commit(tx).unwrap();
}
fn get(database: &EntityDatabase) -> Option<Vec<u8>> {
    let mut tx = database.begin(7, 11).unwrap();
    database.get(&mut tx, &key(11)).unwrap()
}
#[test]
fn published_queries_advance_through_overlay_and_tree_commits_preserve_old_pins_and_scope() {
    let mut writer = database();
    writer.set_write_overlay_enabled(true);
    let view = writer.enable_published_queries().unwrap();
    assert_eq!(writer.snapshot_pin_metrics().unwrap().active_handles, 0);
    put(&mut writer, b"first");
    let old = view.try_build().unwrap().unwrap();
    put(&mut writer, b"second");
    let mut current = view.try_build().unwrap().unwrap();
    assert_eq!(get(&old), Some(b"first".to_vec()));
    assert_eq!(get(&current), Some(b"second".to_vec()));
    let mut tx = current.begin(7, 11).unwrap();
    assert_eq!(
        current.get(&mut tx, &key(12)).unwrap_err().kind(),
        io::ErrorKind::PermissionDenied
    );
    current
        .put(&mut tx, key(11), b"private staging only".to_vec())
        .unwrap();
    assert_eq!(
        current.commit(tx).unwrap_err().kind(),
        io::ErrorKind::PermissionDenied
    );
    writer.merge_overlay_fully().unwrap();
    assert_eq!(
        get(&view.try_build().unwrap().unwrap()),
        Some(b"second".to_vec())
    );
    writer.set_write_overlay_enabled(false);
    put(&mut writer, b"tree");
    assert_eq!(
        get(&view.try_build().unwrap().unwrap()),
        Some(b"tree".to_vec())
    );
    assert!(writer
        .pinned_snapshots()
        .unwrap()
        .contains(&old.current_snapshot()));
    drop(current);
    drop(old);
    assert_eq!(writer.snapshot_pin_metrics().unwrap().active_handles, 0);
}
#[test]
fn published_queries_keep_all_live_roots_without_count_quota_or_permanent_pin() {
    let mut writer = database();
    let view = writer.enable_published_queries().unwrap();
    let mut readers: Vec<_> = (0..512)
        .map(|_| view.try_build().unwrap().unwrap())
        .collect();
    assert_eq!(
        writer.snapshot_pin_metrics().unwrap().active_handles,
        512 as u32
    );
    let extra = view.try_build().unwrap().unwrap();
    put(&mut writer, b"writer progresses with all readers retained");
    drop(readers.pop());
    assert_eq!(
        get(&view.try_build().unwrap().unwrap()),
        Some(b"writer progresses with all readers retained".to_vec())
    );
    drop(extra);
    drop(readers);
    assert_eq!(writer.snapshot_pin_metrics().unwrap().active_handles, 0);
    assert!(writer.pinned_snapshots().unwrap().is_empty());
}
#[test]
fn published_queries_source_restart_and_optional_poison_do_not_hide_errors() {
    let mut writer = database();
    let view = writer.enable_published_queries().unwrap();
    writer
        .writer_engine()
        .unwrap()
        .restart_flag()
        .store(true, std::sync::atomic::Ordering::Relaxed);
    let message = view.try_build().unwrap().err().unwrap().to_string();
    assert_eq!(
        message,
        writer.build_read_replica().err().unwrap().to_string()
    );
    assert_eq!(writer.snapshot_pin_metrics().unwrap().active_handles, 0);
    writer
        .writer_engine()
        .unwrap()
        .restart_flag()
        .store(false, std::sync::atomic::Ordering::Relaxed);
    view.poison_for_test();
    assert!(view.try_build().is_none());
    put(&mut writer, b"original authority remains usable");
    assert!(view.try_build().is_none());
    assert_eq!(
        get(&writer.build_read_replica().unwrap()),
        Some(b"original authority remains usable".to_vec())
    );
}
#[test]
fn published_reader_registers_before_old_view_replacement_and_writer_authority_release() {
    let mut writer = database();
    put(&mut writer, b"old");
    let view = writer.enable_published_queries().unwrap();
    let pins = Arc::clone(&writer.snapshot_pins);
    let writer = Arc::new(Mutex::new(writer));
    let (held_tx, held_rx) = mpsc::channel();
    let (resume_tx, resume_rx) = mpsc::channel();
    let reader_view = Arc::clone(&view);
    let reader = thread::spawn(move || {
        reader_view
            .build_with_pin_hook(|| {
                held_tx.send(()).unwrap();
                resume_rx.recv_timeout(Duration::from_secs(5)).unwrap();
            })
            .unwrap()
            .unwrap()
    });
    held_rx.recv_timeout(Duration::from_secs(5)).unwrap();
    assert_eq!(pins.lock().unwrap().active_handles, 0);
    let (replace_tx, replace_rx) = mpsc::channel();
    view.set_replace_notification(replace_tx);
    let write_owner = Arc::clone(&writer);
    let (done_tx, done_rx) = mpsc::channel();
    let write_thread = thread::spawn(move || {
        put(&mut write_owner.lock().unwrap(), b"new");
        done_tx.send(()).unwrap();
    });
    replace_rx.recv_timeout(Duration::from_secs(5)).unwrap();
    // A GC final fence uses this same authority guard. It cannot race the
    // unpublished root change while the reader is paused before registration.
    assert!(matches!(
        writer.try_lock(),
        Err(std::sync::TryLockError::WouldBlock)
    ));
    assert!(matches!(done_rx.try_recv(), Err(mpsc::TryRecvError::Empty)));
    resume_tx.send(()).unwrap();
    let old = reader.join().unwrap();
    done_rx.recv_timeout(Duration::from_secs(5)).unwrap();
    write_thread.join().unwrap();
    assert_eq!(get(&old), Some(b"old".to_vec()));
    assert_eq!(
        get(&view.try_build().unwrap().unwrap()),
        Some(b"new".to_vec())
    );
    assert!(writer
        .lock()
        .unwrap()
        .pinned_snapshots()
        .unwrap()
        .contains(&old.current_snapshot()));
    drop(old);
    assert_eq!(
        writer
            .lock()
            .unwrap()
            .snapshot_pin_metrics()
            .unwrap()
            .active_handles,
        0
    );
}

#[test]
fn published_query_checks_the_current_shared_background_wal_failure() {
    let mut writer = database();
    let barrier = crate::wal::WalBarrier::default();
    writer
        .writer_engine_mut()
        .unwrap()
        .attach_wal_barrier(&barrier)
        .unwrap();
    let view = writer.enable_published_queries().unwrap();
    barrier.inject_background_error_for_test(io::Error::new(
        io::ErrorKind::PermissionDenied,
        "injected asynchronous WAL failure",
    ));
    let actual = view.try_build().unwrap().err().unwrap();
    let expected = writer.build_read_replica().err().unwrap();
    assert_eq!(actual.kind(), expected.kind());
    assert_eq!(actual.to_string(), expected.to_string());
    assert_eq!(writer.snapshot_pin_metrics().unwrap().active_handles, 0);
}

#[test]
fn published_query_aborted_logical_update_disables_optional_view() {
    let mut writer = database();
    put(&mut writer, b"before");
    let view = writer.enable_published_queries().unwrap();
    let old = view.try_build().unwrap().unwrap();
    drop(writer.begin_query_publication());
    assert!(view.try_build().is_none());
    put(&mut writer, b"after");
    assert!(view.try_build().is_none());
    assert_eq!(
        get(&writer.build_read_replica().unwrap()),
        Some(b"after".to_vec())
    );
    assert_eq!(get(&old), Some(b"before".to_vec()));
    drop(old);
    assert_eq!(writer.snapshot_pin_metrics().unwrap().active_handles, 0);
}


#[test]
fn published_query_replicas_isolate_private_overlay_retirement() {
    let mut writer = database();
    writer.set_write_overlay_enabled(true);
    let view = writer.enable_published_queries().unwrap();
    put(&mut writer, b"committed");
    let left = view.try_build().unwrap().unwrap();
    let right = view.try_build().unwrap().unwrap();
    assert_eq!(get(&left), Some(b"committed".to_vec()));
    assert_eq!(get(&right), Some(b"committed".to_vec()));
    // Fault-inject retirement into one private replica. No materialized root
    // covers this value, so only that replica loses the overlay row.
    left.overlay.write().unwrap().note_merged(crate::entity_overlay::OverlayPosition {
        sequence: left.current_snapshot().sequence,
        key: None,
    });
    assert_eq!(get(&left), None);
    assert_eq!(get(&right), Some(b"committed".to_vec()));
    assert_eq!(get(&writer), Some(b"committed".to_vec()));
    let fresh = view.try_build().unwrap().unwrap();
    assert_eq!(get(&fresh), Some(b"committed".to_vec()));

    // Counterfactual only: sharing the mutable container makes private
    // retirement visible across otherwise separate pinned query replicas.
    let shared_left = view.try_build().unwrap().unwrap();
    let mut shared_right = view.try_build().unwrap().unwrap();
    shared_right.overlay = Arc::clone(&shared_left.overlay);
    shared_left.overlay.write().unwrap().note_merged(crate::entity_overlay::OverlayPosition {
        sequence: shared_left.current_snapshot().sequence,
        key: None,
    });
    assert_eq!(get(&shared_right), None);
    assert_eq!(get(&writer), Some(b"committed".to_vec()));
}
