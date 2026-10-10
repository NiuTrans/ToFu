// Projection-head retirement: bounded WAL cleanup, isolation, and crash atomicity.
// Entry points are private Turn helpers; dependencies are Authority and deterministic VFS.
use super::*;
use std::path::Path;
use std::sync::Arc;
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};

fn head() -> ProjectionHeadDescriptor {
    ProjectionHeadDescriptor { head_id: "head".into(), attempt_id: "attempt".into(),
        base_revision: 0, patch_count: MAX_TURN_PROJECTION_HEAD_PATCHES, patch_bytes: 64,
        text_tail: None }
}

fn fixture(extra: usize) -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut db = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    db.set_write_overlay_for_test(true);
    let mut tx = db.begin(7, 11).unwrap();
    let key = projection_checkpoint_key(&tx, "conversation", "turn", "head").unwrap();
    db.entity_put(&mut tx, key, b"checkpoint".to_vec()).unwrap();
    for index in 1..=MAX_TURN_PROJECTION_HEAD_PATCHES + extra {
        let key = projection_patch_key(&tx, "conversation", "turn", "head", index as u64).unwrap();
        db.entity_put(&mut tx, key, b"patch".to_vec()).unwrap();
    }
    let key = EntityKey::new(7, 11, "unrelated", b"pending").unwrap();
    db.entity_put(&mut tx, key, b"keep".to_vec()).unwrap();
    db.commit(tx).unwrap();
    let mut other = db.begin(7, 12).unwrap();
    let key = projection_checkpoint_key(&other, "conversation", "turn", "head").unwrap();
    db.entity_put(&mut other, key, b"other owner".to_vec()).unwrap();
    db.commit(other).unwrap();
    (vfs, db)
}

fn retire(db: &AuthorityDatabase) -> AuthorityTransaction {
    let mut tx = db.begin(7, 11).unwrap();
    retire_event_projection_head(db, &mut tx, "conversation", "turn", Some(&head())).unwrap();
    let key = EntityKey::new(7, 11, "settlement", b"done").unwrap();
    db.entity_put(&mut tx, key, b"yes".to_vec()).unwrap();
    tx
}

fn head_count(db: &AuthorityDatabase, owner: u64) -> usize {
    let mut tx = db.begin(7, owner).unwrap();
    let (start, end) = projection_head_range(&tx, "conversation", "turn", "head").unwrap();
    db.entity_scan(&mut tx, &start, &end, 100).unwrap().len()
}

#[test]
fn bounded_retirement_preserves_unmerged_debt_and_owner_isolation() {
    let (_, mut db) = fixture(0);
    let replica = db.build_read_replica().unwrap();
    let before = db.entity_overlay_entries_for_test();
    let tx = retire(&db);
    db.commit(tx).unwrap();
    assert_eq!(db.entity_overlay_entries_for_test(), before + 66);
    assert_eq!(head_count(&db, 11), 0);
    assert_eq!(head_count(&db, 12), 1);
    assert_eq!(head_count(&replica, 11), 65);
}

#[test]
fn unexpected_oversized_head_keeps_complete_structural_retirement() {
    let (_, mut db) = fixture(2);
    let tx = retire(&db);
    db.commit(tx).unwrap();
    assert_eq!(head_count(&db, 11), 0);
    assert_eq!(head_count(&db, 12), 1);
}

#[test]
fn retirement_conflicts_with_a_concurrent_projection_patch() {
    let (_, mut db) = fixture(0);
    let stale = retire(&db);
    let mut insertion = db.begin(7, 11).unwrap();
    let key = projection_patch_key(&insertion, "conversation", "turn", "head", 65).unwrap();
    db.entity_put(&mut insertion, key, b"concurrent".to_vec()).unwrap();
    db.commit(insertion).unwrap();
    assert_eq!(db.commit(stale).unwrap_err().kind(), io::ErrorKind::WouldBlock);
    assert_eq!(head_count(&db, 11), 66);
}

#[test]
fn retirement_and_settlement_are_atomic_at_every_commit_io_fault() {
    let (vfs, mut baseline) = fixture(0);
    let tx = retire(&baseline);
    vfs.arm_fault(None).unwrap();
    baseline.commit(tx).unwrap();
    let trace = vfs.trace().unwrap();
    assert!(!trace.is_empty());
    for (index, operation) in trace.iter().enumerate() {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
        if *operation == Operation::Write { actions.push(FaultAction::ShortWrite(7)); }
        for action in actions {
            let (vfs, mut db) = fixture(0);
            let tx = retire(&db);
            vfs.arm_fault(Some(FaultRule { operation_number: index as u64 + 1, action })).unwrap();
            let acknowledged = db.commit(tx).is_ok();
            // Capture durable state before Drop can flush or repair it.
            let durable = Arc::new(vfs.fork_durable().unwrap());
            let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), durable).unwrap();
            let mut read = reopened.begin(7, 11).unwrap();
            let marker = EntityKey::new(7, 11, "settlement", b"done").unwrap();
            let settled = reopened.entity_get(&mut read, &marker).unwrap().is_some();
            assert_eq!(head_count(&reopened, 11), if settled { 0 } else { 65 }, "fault {index}");
            assert!(!acknowledged || settled, "lost acknowledged settlement at {index}");
            assert_eq!(head_count(&reopened, 12), 1);
            let pending = EntityKey::new(7, 11, "unrelated", b"pending").unwrap();
            assert_eq!(reopened.entity_get(&mut read, &pending).unwrap(), Some(b"keep".to_vec()));
        }
    }
}
