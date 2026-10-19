//! Executable lifecycle, identity and crash contracts for deferred event reclamation.
use super::*;
use crate::conversation_header::{self, DeleteRequest, PurgeRequest, RestoreRequest};
use crate::vfs::{DeterministicVfs, FaultAction, Operation, Vfs};
use std::{path::Path, sync::Arc};

fn seed(db: &mut AuthorityDatabase, owner: u64, cid: &str, id: &str) {
    let mut tx = db.begin_with_identity_claim_scopes(7, owner).unwrap();
    super::super::tests::append_live_attempt(db, &mut tx, cid, id, id);
    db.commit(tx).unwrap();
    let mut tx = db.begin_with_identity_claim_scopes(7, owner).unwrap();
    attempt_bind(db, &mut tx, id, "projection-head-task", "", 2).unwrap();
    db.commit(tx).unwrap();
    for time in [3, 4, 5] {
        let mut tx = db.begin_with_identity_claim_scopes(7, owner).unwrap();
        let request = super::super::tests::live_projection_request(id, "durable evidence", time);
        let result: Value =
            serde_json::from_slice(&event_record(db, &mut tx, &request).unwrap()).unwrap();
        assert_eq!(result["applied"], true);
        db.commit(tx).unwrap();
    }
}

fn soft_delete(db: &mut AuthorityDatabase, cid: &str, time: u64) {
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    conversation_header::delete(
        db,
        &mut tx,
        &DeleteRequest {
            conversation_id: cid.into(),
            deleted_at_ms: time,
        },
    )
    .unwrap();
    db.commit(tx).unwrap();
}

fn restore(db: &mut AuthorityDatabase, cid: &str, time: u64) {
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    conversation_header::restore(
        db,
        &mut tx,
        &RestoreRequest {
            conversation_id: cid.into(),
            committed_at_ms: time,
        },
    )
    .unwrap();
    db.commit(tx).unwrap();
}

fn step(db: &mut AuthorityDatabase) -> io::Result<Value> {
    let mut tx = db.begin_with_identity_claim_scopes(7, 11)?;
    let value = serde_json::from_slice(&events_prune(
        db,
        &mut tx,
        &EventPruneRequest {
            settled_before_ms: 1,
            max_attempts: 1,
            max_rows: 1,
        },
    )?)
    .unwrap();
    if tx.has_business_mutation() {
        db.commit(tx)?;
    }
    Ok(value)
}

fn jobs(db: &AuthorityDatabase) -> Vec<(EntityKey, Vec<u8>)> {
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    let (start, end) = EntityKey::prefix_range(7, 11, NAMESPACE, b"j").unwrap();
    db.entity_scan(&mut tx, &start, &end, 20).unwrap()
}

fn drain(db: &mut AuthorityDatabase) {
    for _ in 0..100 {
        if jobs(db).is_empty() {
            return;
        }
        assert!(step(db).unwrap()["deleted_rows"].as_u64().unwrap() <= 1);
    }
    panic!("retirement failed to make bounded progress");
}

fn event_rows(db: &AuthorityDatabase, owner: u64, id: &str) -> usize {
    let mut tx = db.begin_with_identity_claim_scopes(7, owner).unwrap();
    let (start, end) = EntityKey::prefix_range(
        7,
        owner,
        ATTEMPT_EVENT_NAMESPACE,
        &attempt_event_prefix(id).unwrap(),
    )
    .unwrap();
    db.entity_scan(&mut tx, &start, &end, 100).unwrap().len()
}

fn pins(db: &AuthorityDatabase) -> Vec<Vec<u8>> {
    let tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    jobs(db).iter().map(|(key, _)| pin_id(&tx, key)).collect()
}

fn assert_reclaimed(db: &AuthorityDatabase, pins: &[Vec<u8>]) {
    assert!(jobs(db).is_empty());
    for pin in pins {
        assert!(db.begin_persistent_snapshot(7, 11, pin).unwrap().is_none());
    }
    let mut tx = db.begin(7, 11).unwrap();
    let key = entity_key(&tx, NAMESPACE, b"cursor").unwrap();
    assert!(db.entity_get(&mut tx, &key).unwrap().is_none());
}

#[test]
fn generations_resume_fairly_without_touching_new_or_foreign_events() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut db, 11, "c", "old");
    seed(&mut db, 11, "c-neighbor", "neighbor");
    seed(&mut db, 12, "foreign-c", "foreign");
    soft_delete(&mut db, "c", 10);
    restore(&mut db, "c", 11);
    seed(&mut db, 11, "c", "middle");
    soft_delete(&mut db, "c", 12);
    restore(&mut db, "c", 13);
    seed(&mut db, 11, "c", "fresh");
    let pins = pins(&db);
    assert_eq!(pins.len(), 2);
    let original = jobs(&db);
    eprintln!(
        "retirement_measurement jobs={} encoded_job_bytes={} private_pins={}",
        original.len(),
        original.iter().map(|(_, bytes)| bytes.len()).sum::<usize>(),
        pins.len()
    );
    step(&mut db).unwrap();
    let first = jobs(&db);
    assert_ne!(original[0].1, first[0].1);
    assert_eq!(original[1].1, first[1].1);
    step(&mut db).unwrap();
    assert_ne!(first[1].1, jobs(&db)[1].1);
    drop(db);
    let mut db = AuthorityDatabase::open(directory.path()).unwrap();
    drain(&mut db);
    assert_reclaimed(&db, &pins);
    for id in ["old", "middle"] {
        assert_eq!(event_rows(&db, 11, id), 0);
    }
    for (owner, id) in [(11, "fresh"), (11, "neighbor"), (12, "foreign")] {
        assert_eq!(event_rows(&db, owner, id), 4);
        let mut tx = db.begin_with_identity_claim_scopes(7, owner).unwrap();
        assert!(attempt_get(&db, &mut tx, id).unwrap().is_some());
    }
    let mut tx = db.begin(7, 11).unwrap();
    let old: Value =
        serde_json::from_slice(&get(&db, &mut tx, "c", "old").unwrap().unwrap()).unwrap();
    assert_eq!(old["projection"]["content"], "durable evidence");
    assert_eq!(old["status"], "interrupted");
}

#[test]
fn purge_removes_pending_generations_before_identity_reuse() {
    for purge_active in [false, true] {
        let directory = tempfile::tempdir().unwrap();
        let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed(&mut db, 11, "c", "reused");
        soft_delete(&mut db, "c", 10);
        let pins = pins(&db);
        step(&mut db).unwrap();
        if purge_active {
            restore(&mut db, "c", 11);
        }
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        conversation_header::purge(
            &mut db,
            &mut tx,
            &PurgeRequest {
                conversation_id: "c".into(),
                purged_at_ms: 12,
            },
        )
        .unwrap();
        db.commit(tx).unwrap();
        assert_reclaimed(&db, &pins);
        assert_eq!(event_rows(&db, 11, "reused"), 0);
        seed(&mut db, 11, "c", "reused");
        step(&mut db).unwrap();
        assert_eq!(event_rows(&db, 11, "reused"), 4);
    }
}

#[test]
fn tombstone_expiry_keeps_identities_reserved_until_cleanup_finishes() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut db, 11, "c", "old");
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    let mut request = super::super::tests::live_projection_request("old", "durable evidence", 6);
    request.terminal = true;
    request.status = "completed".into();
    event_record(&db, &mut tx, &request).unwrap();
    db.commit(tx).unwrap();
    soft_delete(&mut db, "c", 10);
    restore(&mut db, "c", 11);
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    delete(&db, &mut tx, "c", &["old".into()], 12).unwrap();
    db.commit(tx).unwrap();
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert_eq!(
        prune_expired_tombstones(&db, &mut tx, TOMBSTONE_RETENTION_MS + 20).unwrap(),
        0
    );
    for namespace in [TURN_ID_CLAIM_NAMESPACE, ATTEMPT_ID_CLAIM_NAMESPACE] {
        let key = global_identity_claim_key(&tx, namespace, "old").unwrap();
        assert!(db.entity_get(&mut tx, &key).unwrap().is_some());
    }
    drop(tx);
    drain(&mut db);
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert_eq!(
        prune_expired_tombstones(&db, &mut tx, TOMBSTONE_RETENTION_MS + 20).unwrap(),
        1
    );
    db.commit(tx).unwrap();
    seed(&mut db, 11, "c", "old");
    assert_eq!(event_rows(&db, 11, "old"), 4);
}

#[test]
fn multibyte_identities_survive_partial_cursor_reopen() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    // The existing recency directory further bounds CID bytes; the admitted
    // combined CID/Turn key still exceeds the old ASCII-only cursor bound.
    let cid = "🦀".repeat(63);
    let id = "🦀".repeat(120);
    seed(&mut db, 11, &cid, &id);
    soft_delete(&mut db, &cid, 10);
    let pins = pins(&db);
    step(&mut db).unwrap();
    drop(db);
    let mut db = AuthorityDatabase::open(directory.path()).unwrap();
    drain(&mut db);
    assert_reclaimed(&db, &pins);
    assert_eq!(event_rows(&db, 11, &id), 0);
    restore(&mut db, &cid, 11);
    let mut tx = db.begin(7, 11).unwrap();
    assert!(get(&db, &mut tx, &cid, &id).unwrap().is_some());
}

fn fault_database(finalize: bool) -> (Arc<DeterministicVfs>, AuthorityDatabase, Vec<Vec<u8>>) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut db = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    seed(&mut db, 11, "c", "old");
    soft_delete(&mut db, "c", 10);
    let pins = pins(&db);
    if finalize {
        for _ in 0..4 {
            step(&mut db).unwrap();
        }
        assert_eq!(event_rows(&db, 11, "old"), 0);
        assert_eq!(jobs(&db).len(), 1);
    }
    vfs.arm_fault(None).unwrap();
    (vfs, db, pins)
}

#[test]
fn every_cleanup_filesystem_fault_reopens_with_atomic_cursor_and_pin() {
    for finalize in [false, true] {
        let (vfs, mut db, _) = fault_database(finalize);
        step(&mut db).unwrap();
        let trace = vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Write));
        drop(db);
        for (index, operation) in trace.iter().enumerate() {
            let occurrence = trace[..=index].iter().filter(|op| *op == operation).count();
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
            if *operation == Operation::Write {
                actions.push(FaultAction::ShortWrite(7));
            }
            for action in actions {
                let (vfs, mut db, pins) = fault_database(finalize);
                vfs.arm_operation_fault(*operation, occurrence, action)
                    .unwrap();
                let _ = step(&mut db);
                assert!(
                    vfs.operation_fault_fired().unwrap(),
                    "fault did not fire: {index} {operation:?}"
                );
                drop(db);
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let mut db = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
                drain(&mut db);
                assert_reclaimed(&db, &pins);
                assert_eq!(event_rows(&db, 11, "old"), 0);
                restore(&mut db, "c", 11);
                let mut tx = db.begin(7, 11).unwrap();
                let restored: Value =
                    serde_json::from_slice(&get(&db, &mut tx, "c", "old").unwrap().unwrap())
                        .unwrap();
                assert_eq!(restored["projection"]["content"], "durable evidence");
            }
        }
        eprintln!("retirement_fault_matrix finalize={finalize} error_positions={} short_write_positions={}",
        trace.len(), trace.iter().filter(|op| **op == Operation::Write).count());
    }
}
