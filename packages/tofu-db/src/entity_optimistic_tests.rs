// Logical OCC over the tree and pending WAL overlay. No physical drain is
// required to serialize independent transactions; conflicting snapshots abort.
use super::*;
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
use std::path::Path;

fn database() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut db = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    db.set_write_overlay_for_test(true);
    (vfs, db)
}

fn key(namespace: &str, bytes: &[u8]) -> EntityKey {
    EntityKey::new(1, 1, namespace, bytes).unwrap()
}

fn stage(db: &AuthorityDatabase, namespace: &str, bytes: &[u8]) -> AuthorityTransaction {
    let mut tx = db.begin(1, 1).unwrap();
    db.entity_put(&mut tx, key(namespace, bytes), b"value".to_vec())
        .unwrap();
    tx
}

#[test]
fn independent_stale_writers_commit_without_draining_pending_deltas() {
    let (_, mut db) = database();
    let transactions: Vec<_> = (0..24_u8)
        .map(|index| stage(&db, "rows", &[index]))
        .collect();
    for (index, tx) in transactions.into_iter().enumerate() {
        db.commit(tx).unwrap();
        assert_eq!(db.entity_overlay_entries_for_test(), index + 1);
    }
    let mut read = db.begin(1, 1).unwrap();
    for index in 0..24_u8 {
        assert_eq!(
            db.entity_get(&mut read, &key("rows", &[index])).unwrap(),
            Some(b"value".to_vec())
        );
    }
}

#[test]
fn logical_point_and_blind_write_witnesses_reject_updates_deletes_and_aba() {
    for read_before_write in [false, true] {
        for mutation in ["update", "delete", "aba"] {
            let (_, mut db) = database();
            let seed = stage(&db, "rows", b"same");
            db.commit(seed).unwrap();
            let mut stale = db.begin(1, 1).unwrap();
            if read_before_write {
                assert!(db
                    .entity_get(&mut stale, &key("rows", b"same"))
                    .unwrap()
                    .is_some());
            }
            db.entity_put(&mut stale, key("rows", b"same"), b"stale".to_vec())
                .unwrap();
            let mut other = db.begin(1, 1).unwrap();
            if mutation == "update" {
                db.entity_put(&mut other, key("rows", b"same"), b"value".to_vec())
                    .unwrap();
            } else {
                db.entity_delete(&mut other, key("rows", b"same")).unwrap();
            }
            db.commit(other).unwrap();
            if mutation == "aba" {
                let recreated = stage(&db, "rows", b"same");
                db.commit(recreated).unwrap();
            }
            let pending = db.entity_overlay_entries_for_test();
            assert_eq!(
                db.commit(stale).unwrap_err().kind(),
                io::ErrorKind::WouldBlock
            );
            assert_eq!(db.entity_overlay_entries_for_test(), pending);
        }
    }
}

#[test]
fn range_witnesses_detect_overlay_and_materialized_phantoms_in_both_directions() {
    for reverse in [false, true] {
        for materialize in [false, true] {
            let (_, mut db) = database();
            let mut stale = db.begin(1, 1).unwrap();
            let (start, end) = EntityKey::prefix_range(1, 1, "predicate", b"a").unwrap();
            let rows = if reverse {
                db.entity_scan_reverse(&mut stale, &start, &end, 1)
            } else {
                db.entity_scan(&mut stale, &start, &end, 1)
            }
            .unwrap();
            assert!(rows.is_empty());
            db.entity_put(&mut stale, key("result", b"out"), b"empty".to_vec())
                .unwrap();
            let insert = stage(&db, "predicate", b"a-new");
            db.commit(insert).unwrap();
            if materialize {
                db.merge_entity_overlay_for_test().unwrap();
            }
            assert_eq!(
                db.commit(stale).unwrap_err().kind(),
                io::ErrorKind::WouldBlock
            );
        }
    }
}

#[test]
fn disjoint_range_writers_and_point_readers_survive_other_commits_and_merges() {
    for materialize in [false, true] {
        let (_, mut db) = database();
        let mut stale = db.begin(1, 1).unwrap();
        assert!(db
            .entity_get(&mut stale, &key("predicate", b"missing"))
            .unwrap()
            .is_none());
        if !materialize {
            let (start, end) = EntityKey::prefix_range(1, 1, "predicate", b"a").unwrap();
            assert!(db
                .entity_scan(&mut stale, &start, &end, 1)
                .unwrap()
                .is_empty());
        }
        db.entity_put(&mut stale, key("result", b"out"), b"correct".to_vec())
            .unwrap();
        let other = stage(&db, "predicate", b"b");
        db.commit(other).unwrap();
        if materialize {
            db.merge_entity_overlay_for_test().unwrap();
        }
        db.commit(stale).unwrap();
        let mut read = db.begin(1, 1).unwrap();
        assert_eq!(
            db.entity_get(&mut read, &key("result", b"out")).unwrap(),
            Some(b"correct".to_vec())
        );
    }
}

#[test]
fn stale_overlay_commit_faults_preserve_every_acknowledged_transaction() {
    fn prepared() -> (
        Arc<DeterministicVfs>,
        AuthorityDatabase,
        AuthorityTransaction,
    ) {
        let (vfs, mut db) = database();
        let stale = stage(&db, "rows", b"stale");
        let other = stage(&db, "rows", b"other");
        db.commit(other).unwrap();
        (vfs, db, stale)
    }
    let (vfs, mut db, tx) = prepared();
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
            let (vfs, mut db, tx) = prepared();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action,
            }))
            .unwrap();
            let acknowledged = db.commit(tx).is_ok();
            let durable = Arc::new(vfs.fork_durable().unwrap());
            let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), durable).unwrap();
            let mut read = reopened.begin(1, 1).unwrap();
            assert_eq!(
                reopened
                    .entity_get(&mut read, &key("rows", b"other"))
                    .unwrap(),
                Some(b"value".to_vec())
            );
            if acknowledged {
                assert_eq!(
                    reopened
                        .entity_get(&mut read, &key("rows", b"stale"))
                        .unwrap(),
                    Some(b"value".to_vec())
                );
            }
        }
    }
}

#[test]
fn range_versions_ignore_disjoint_leaf_rewrites_and_overlay_materialization() {
    for reverse in [false, true] {
        for preexisting in [false, true] {
            let (_, mut db) = database();
            for suffix in [b"a-one".as_slice(), b"z-other"] {
                if preexisting || suffix == b"z-other" { let tx = stage(&db, "predicate", suffix); db.commit(tx).unwrap(); }
            }
            db.merge_entity_overlay_for_test().unwrap();
            // A pending value must remain equivalent when materialized too.
            if preexisting { let tx = stage(&db, "predicate", b"a-two"); db.commit(tx).unwrap(); }
            let mut stale = db.begin(1, 1).unwrap();
            let (start, end) = EntityKey::prefix_range(1, 1, "predicate", b"a").unwrap();
            let before = if reverse { db.entity_scan_reverse(&mut stale, &start, &end, 10) }
                else { db.entity_scan(&mut stale, &start, &end, 10) }.unwrap();
            assert_eq!(before.len(), if preexisting { 2 } else { 0 });
            db.entity_put(&mut stale, key("result", b"out"), b"unchanged predicate".to_vec()).unwrap();
            let other = stage(&db, "predicate", b"z-other"); db.commit(other).unwrap();
            db.merge_entity_overlay_for_test().unwrap();
            db.commit(stale).unwrap();
        }
    }
}

#[test]
fn range_versions_reject_materialized_updates_deletes_and_absent_present_absent() {
    for reverse in [false, true] {
        for mutation in ["update", "delete", "absent-aba"] {
            let (_, mut db) = database();
            let seed = stage(&db, "predicate", b"z-outside"); db.commit(seed).unwrap();
            if mutation != "absent-aba" { let seed = stage(&db, "predicate", b"a-row"); db.commit(seed).unwrap(); }
            db.merge_entity_overlay_for_test().unwrap();
            let mut stale = db.begin(1, 1).unwrap();
            let (start, end) = EntityKey::prefix_range(1, 1, "predicate", b"a").unwrap();
            if reverse { db.entity_scan_reverse(&mut stale, &start, &end, 1) }
                else { db.entity_scan(&mut stale, &start, &end, 1) }.unwrap();
            db.entity_put(&mut stale, key("result", b"out"), b"must abort".to_vec()).unwrap();
            if mutation != "delete" { let tx = stage(&db, "predicate", b"a-row"); db.commit(tx).unwrap(); }
            if mutation != "update" {
                let mut tx = db.begin(1, 1).unwrap(); db.entity_delete(&mut tx, key("predicate", b"a-row")).unwrap(); db.commit(tx).unwrap();
            }
            db.merge_entity_overlay_for_test().unwrap();
            assert_eq!(db.commit(stale).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        }
    }
}

#[test]
fn range_versions_reject_retirement_of_rows_owned_only_by_snapshot_overlay() {
    for reverse in [false, true] {
        let (_, mut db) = database();
        let seed = stage(&db, "predicate", b"a-row"); db.commit(seed).unwrap();
        let mut stale = db.begin(1, 1).unwrap();
        let (start, end) = EntityKey::prefix_range(1, 1, "predicate", b"a").unwrap();
        let rows = if reverse { db.entity_scan_reverse(&mut stale, &start, &end, 1) }
            else { db.entity_scan(&mut stale, &start, &end, 1) }.unwrap();
        assert_eq!(rows.len(), 1);
        db.entity_put(&mut stale, key("result", b"out"), b"must abort".to_vec()).unwrap();
        let mut retire = db.begin(1, 1).unwrap();
        db.entity_retire_range(&mut retire, &start, &end).unwrap();
        db.commit(retire).unwrap();
        assert_eq!(db.commit(stale).unwrap_err().kind(), io::ErrorKind::WouldBlock);
    }
}
