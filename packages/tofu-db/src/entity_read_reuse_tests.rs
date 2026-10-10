//! Transaction-local read reuse: snapshot isolation, failure and memory boundaries.
use super::*;
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
use std::path::Path;

fn key(index: u8) -> EntityKey {
    EntityKey::new(7, 11, "reuse", &[index]).unwrap()
}

fn database(overlay: bool) -> (Arc<DeterministicVfs>, EntityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut db = EntityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    db.set_write_overlay_enabled(overlay);
    let mut tx = db.begin(7, 11).unwrap();
    db.put(&mut tx, key(1), vec![19; 512]).unwrap();
    db.commit(tx).unwrap();
    db.writer_engine()
        .unwrap()
        .block_store()
        .flush_pending()
        .unwrap();
    db.set_page_cache_capacity_bytes(0).unwrap();
    db.writer_engine()
        .unwrap()
        .block_store()
        .set_cache_capacity_bytes(0)
        .unwrap();
    (vfs, db)
}

#[test]
fn repeated_present_and_missing_reads_skip_tree_io_and_preserve_staged_values() {
    let (vfs, db) = database(false);
    let mut tx = db.begin(7, 11).unwrap();
    vfs.arm_fault(None).unwrap();
    assert_eq!(db.get(&mut tx, &key(1)).unwrap(), Some(vec![19; 512]));
    assert_eq!(db.get(&mut tx, &key(2)).unwrap(), None);
    assert!(
        !vfs.trace().unwrap().is_empty(),
        "first reads must reach storage with both shared caches disabled"
    );
    let version = tx.point_witnesses[&key(1).0].version;
    vfs.arm_fault(None).unwrap();
    for _ in 0..8 {
        assert_eq!(db.get(&mut tx, &key(1)).unwrap(), Some(vec![19; 512]));
        assert_eq!(db.get(&mut tx, &key(2)).unwrap(), None);
    }
    assert!(vfs.trace().unwrap().is_empty());
    db.put(&mut tx, key(1), b"new".to_vec()).unwrap();
    assert_eq!(db.get(&mut tx, &key(1)).unwrap(), Some(b"new".to_vec()));
    db.delete(&mut tx, key(1)).unwrap();
    assert_eq!(db.get(&mut tx, &key(1)).unwrap(), None);
    db.put(&mut tx, key(2), b"insert".to_vec()).unwrap();
    assert_eq!(db.get(&mut tx, &key(2)).unwrap(), Some(b"insert".to_vec()));
    assert_eq!(tx.point_witnesses[&key(1).0].version, version);
    assert!(vfs.trace().unwrap().is_empty());
}

#[test]
fn cached_reads_keep_snapshot_values_but_reject_concurrent_updates_deletes_and_insertions() {
    for overlay in [false, true] {
        for mutation in ["update", "delete", "aba", "insert"] {
            let (_, mut db) = database(overlay);
            let target = key(if mutation == "insert" { 2 } else { 1 });
            let mut stale = db.begin(7, 11).unwrap();
            let original = db.get(&mut stale, &target).unwrap();
            let mut other = db.begin(7, 11).unwrap();
            if matches!(mutation, "delete" | "aba") {
                db.delete(&mut other, target.clone()).unwrap();
            } else {
                db.put(&mut other, target.clone(), b"changed".to_vec())
                    .unwrap();
            }
            db.commit(other).unwrap();
            if mutation == "aba" {
                let mut other = db.begin(7, 11).unwrap();
                db.put(&mut other, target.clone(), original.clone().unwrap())
                    .unwrap();
                db.commit(other).unwrap();
            }
            assert_eq!(db.get(&mut stale, &target).unwrap(), original);
            db.put(&mut stale, key(3), b"dependent".to_vec()).unwrap();
            assert_eq!(
                db.commit(stale).unwrap_err().kind(),
                io::ErrorKind::WouldBlock
            );
            assert_eq!(
                db.snapshot_pin_metrics()
                    .unwrap()
                    .retained_transaction_bytes,
                0
            );
        }
    }
}

#[test]
fn optional_values_retain_reads_beyond_retired_quota_and_release_on_write_and_drop() {
    let (_, db) = database(false);
    let mut holder = db.begin(7, 11).unwrap();
    let mut tx = db.begin(7, 11).unwrap();
    let witness_bytes = point_witness_reservation_bytes(key(1).encoded());
    holder
        .snapshot_pin
        .reserve_transaction_bytes(LEGACY_AGGREGATE_TRANSACTION_BYTES - witness_bytes)
        .unwrap();
    for _ in 0..2 {
        assert_eq!(db.get(&mut tx, &key(1)).unwrap(), Some(vec![19; 512]));
        assert!(matches!(
            tx.point_witnesses[&key(1).0].read,
            RetainedPointRead::Value(_)
        ));
    }
    assert_eq!(tx.snapshot_pin.retained_transaction_bytes, witness_bytes + 512);
    drop(holder);
    assert_eq!(db.get(&mut tx, &key(1)).unwrap(), Some(vec![19; 512]));
    assert_eq!(
        tx.snapshot_pin.retained_transaction_bytes,
        witness_bytes + 512
    );
    db.delete(&mut tx, key(1)).unwrap();
    assert_eq!(
        tx.snapshot_pin.retained_transaction_bytes,
        witness_bytes + write_reservation_bytes(key(1).encoded(), None)
    );
    drop(tx);
    assert_eq!(
        db.snapshot_pin_metrics()
            .unwrap()
            .retained_transaction_bytes,
        0
    );
}

#[test]
fn mandatory_reservations_preserve_optional_values_and_read_witnesses() {
    let (_, db) = database(false);
    let mut tx = db.begin(7, 11).unwrap();
    db.get(&mut tx, &key(1)).unwrap();
    let version = tx.point_witnesses[&key(1).0].version;
    let mut holder = db.begin(7, 11).unwrap();
    let next_bytes = write_reservation_bytes(key(2).encoded(), Some(b"new"));
    holder
        .snapshot_pin
        .reserve_transaction_bytes(
            LEGACY_AGGREGATE_TRANSACTION_BYTES
                - tx.snapshot_pin.retained_transaction_bytes
                - next_bytes
                + 1,
        )
        .unwrap();
    db.put(&mut tx, key(2), b"new".to_vec()).unwrap();
    assert!(matches!(
        tx.point_witnesses[&key(1).0].read,
        RetainedPointRead::Value(_)
    ));
    assert_eq!(tx.point_witnesses[&key(1).0].version, version);
    drop(holder);
    assert_eq!(db.get(&mut tx, &key(1)).unwrap(), Some(vec![19; 512]));
    drop(tx);
    assert_eq!(
        db.snapshot_pin_metrics()
            .unwrap()
            .retained_transaction_bytes,
        0
    );
}

#[test]
fn failed_page_reads_do_not_cache_absence_or_leak_reservations() {
    let (vfs, db) = database(false);
    let mut tx = db.begin(7, 11).unwrap();
    vfs.arm_fault(Some(FaultRule {
        operation_number: 1,
        action: FaultAction::ErrorBefore(io::ErrorKind::Other),
    }))
    .unwrap();
    assert!(db.get(&mut tx, &key(1)).is_err());
    assert!(tx.point_witnesses.is_empty());
    assert_eq!(
        db.snapshot_pin_metrics()
            .unwrap()
            .retained_transaction_bytes,
        0
    );
    vfs.arm_fault(None).unwrap();
    assert_eq!(db.get(&mut tx, &key(1)).unwrap(), Some(vec![19; 512]));
}

#[test]
fn validated_rebase_reuses_unchanged_reads_and_observes_new_commit_metadata() {
    let (_, mut db) = database(true);
    let mut tx = db.begin(7, 11).unwrap();
    db.get(&mut tx, &key(1)).unwrap();
    let mut other = db.begin(7, 11).unwrap();
    db.put(&mut other, key(2), b"head".to_vec()).unwrap();
    db.commit(other).unwrap();
    db.rebase_validated_for_commit(&mut tx).unwrap();
    assert_eq!(db.get(&mut tx, &key(1)).unwrap(), Some(vec![19; 512]));
    assert_eq!(db.get(&mut tx, &key(2)).unwrap(), Some(b"head".to_vec()));
    db.put(&mut tx, key(3), b"commit".to_vec()).unwrap();
    db.commit(tx).unwrap();
}

#[test]
fn cache_hits_still_validate_authority_identity_owner_scope_and_restart_latch() {
    let (_, db) = database(true);
    let (_, other) = database(true);
    let mut tx = db.begin(7, 11).unwrap();
    db.get(&mut tx, &key(1)).unwrap();
    assert_eq!(
        other.get(&mut tx, &key(1)).unwrap_err().kind(),
        io::ErrorKind::PermissionDenied
    );
    let foreign = EntityKey::new(7, 12, "reuse", &[1]).unwrap();
    assert_eq!(
        db.get(&mut tx, &foreign).unwrap_err().kind(),
        io::ErrorKind::PermissionDenied
    );
    db.writer_engine()
        .unwrap()
        .restart_flag()
        .store(true, std::sync::atomic::Ordering::Release);
    assert!(db.get(&mut tx, &key(1)).is_err());
}

#[test]
fn cached_read_commit_faults_recover_acknowledged_state_without_partial_values() {
    fn prepared(overlay: bool) -> (Arc<DeterministicVfs>, EntityDatabase, EntityTransaction) {
        let (vfs, mut db) = database(overlay);
        let mut tx = db.begin(7, 11).unwrap();
        for _ in 0..3 {
            assert_eq!(db.get(&mut tx, &key(1)).unwrap(), Some(vec![19; 512]));
        }
        db.put(&mut tx, key(3), b"dependent".to_vec()).unwrap();
        let mut other = db.begin(7, 11).unwrap();
        db.put(&mut other, key(2), b"acknowledged".to_vec())
            .unwrap();
        db.commit(other).unwrap();
        (vfs, db, tx)
    }
    for overlay in [false, true] {
        let (vfs, mut db, tx) = prepared(overlay);
        vfs.arm_fault(None).unwrap();
        db.commit(tx).unwrap();
        let trace = vfs.trace().unwrap();
        assert!(!trace.is_empty());
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if *operation == Operation::Write {
                actions.push(FaultAction::ShortWrite(7));
            }
            for action in actions {
                let (vfs, mut db, tx) = prepared(overlay);
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let acknowledged = db.commit(tx).is_ok();
                let reopened = EntityDatabase::open_with_vfs(
                    Path::new("/data"),
                    Arc::new(vfs.fork_durable().unwrap()),
                )
                .unwrap();
                let mut read = reopened.begin(7, 11).unwrap();
                assert_eq!(
                    reopened.get(&mut read, &key(1)).unwrap(),
                    Some(vec![19; 512])
                );
                assert_eq!(
                    reopened.get(&mut read, &key(2)).unwrap(),
                    Some(b"acknowledged".to_vec())
                );
                let dependent = reopened.get(&mut read, &key(3)).unwrap();
                if acknowledged {
                    assert_eq!(dependent, Some(b"dependent".to_vec()));
                } else {
                    assert!(dependent.is_none() || dependent == Some(b"dependent".to_vec()));
                }
            }
        }
    }
}
